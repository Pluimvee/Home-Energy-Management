"""
ems_strategy.py – Energy Management Strategy
============================================
AppDaemon app.  Runs every hour at HH:00:30.

Reads five forecast sensors and current device states.
Produces per-device strategy sensors with:
  state      = decision for the CURRENT hour  (string)
  forecast   = list of dicts for upcoming hours (horizon = EPEX analysis window)

Published sensors
-----------------
  sensor.strategy_battery       charge / discharge / level
  sensor.strategy_ev            off / slow / fast
  sensor.strategy_hp            on / off
  sensor.strategy_wpb           normal / solar / boost

EPEX analysis fields (pre-computed in sensor.forecast_epex by ems_forecasts)
-----------------------------------------------------------------------------
  value   float  price EUR/kWh
  pct     float  0 = cheapest, 1 = most expensive within analysis window
  is_tp   bool   True when this hour is a structural turning point
  label   str    "negative" price < 0                 — absorb all, no export; charge max
                 "trough"   valley TP, score < 0.27   — deep valley, charge here
                 "dip"      valley TP, score ≥ 0.27   — moderate valley, charge if SOC low
                 "neutral"  no TP                     — self-consumption
                 "crest"    peak TP,   score ≤ 0.75   — moderate peak, avoid non-essentials
                 "peak"     peak TP,   score > 0.75   — strong peak, discharge

Battery strategy output (per uur)
----------------------------------
  target_grid_w  Doelwaarde grid (W); negatief = export; controller stuurt hierop
  expected_soc   Verwachte SOC aan einde van het uur (%, monitoring)

Controller (ems_bat_controller) logica:
  mode = charge    → laden als grid < target_grid_w, nooit ontladen
  mode = discharge → ontladen als grid > target_grid_w, nooit laden
  mode = hold      → doe niets

NOTE: no service calls – automations / ESPHome connect sensors to hardware.
"""

import datetime

import appdaemon.plugins.hass.hassapi as hass

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_PEAKS      = 2
MAX_TROUGHS    = 2

BATTERY_CAPACITY_KWH = 20.0
BATTERY_USABLE       = 0.93    # 100% − 7% bodemreserve = 18.6 kWh bruikbaar
BATTERY_EFFICIENCY   = 0.97    # roundtrip rendement
BATTERY_MIN_SOC      = 7       # %
BATTERY_MAX_SOC      = 100     # %
BATTERY_MIN_KW       = 0.5     # kW
BATTERY_MAX_KW       = 10.0    # kW

TP_WEIGHT           = 3     # TP-uren krijgen 3× meer SOC-gain dan band-uren binnen cheap segment

# ── Target-grid constanten (strategy → controller) ────────────────────────────
GRID_MIN_W        = 50    # W anti-export target in charge-mode standby
GRID_EXPORT_LIMIT = 5000  # W max export bij pre-empty / surplus (negatieve target)


EV_SLOW_KW = 2.3   # 10 A × 230 V
EV_FAST_KW = 3.68  # 16 A × 230 V
EV_CAPACITY_ENTITY = "input_number.car_battery_capacity"

WPB_BOOST_MAX_C       = 65     # °C – skip boost if already this hot
WPB_BOOST_MAX_PRICE   = 0.08   # €/kWh
WPB_SOLAR_MIN_SURPLUS = 0.2    # kWh net surplus to activate solar mode

HP_SOLAR_COVER_RATIO  = 0.90
HP_LOOKAHEAD_OFF      = 2    # uur eerder uitzetten vóór solar-dekking begint
HP_LOOKAHEAD_ON       = 2    # uur eerder aanzetten vóór solar-dekking eindigt (vloer opwarmen)

SOC_SENSOR    = "sensor.batsim_soc_pct"   # sim-mode; vervang door sensor.solis_s6_eh3p_battery_soc voor productie
WPB_SENSOR    = "sensor.aqua_heatpump_boiler"
EV_NEED_ENTITY = "input_datetime.need_to_use_car"
HH_FALLBACK   = 0.4    # kWh/h fallback when household forecast unavailable


# ── AppDaemon App ─────────────────────────────────────────────────────────────

class EmsStrategy(hass.Hass):

    def initialize(self):
        self.run_hourly(self.run_strategy, "00:00:30")
        self.run_every(self.run_strategy, self.datetime(), 15 * 60)

        # Herbereken direct als een van de onderliggende forecasts wijzigt
        for sensor in (
            "sensor.forecast_epex",
            "sensor.forecast_pv",
            "sensor.forecast_heatpump",
            "sensor.forecast_thermic",
            "sensor.forecast_household_energy",
        ):
            self.listen_state(self._on_forecast_change, sensor,
                              attribute="last_updated")

    def _on_forecast_change(self, entity, attribute, old, new, kwargs):
        if new and new != old:
            self.run_strategy({})

    def run_strategy(self, kwargs):
        now = self.datetime()
        self.log(f"[Strategy] Running at {now.strftime('%H:%M:%S')}")

        # ── Read forecast sensors ─────────────────────────────────────────────
        epex_fc    = self._read_forecast("sensor.forecast_epex")
        pv_fc      = self._read_forecast("sensor.forecast_pv")
        hp_fc      = self._read_forecast("sensor.forecast_heatpump")
        thermic_fc = self._read_forecast("sensor.forecast_thermic")
        hh_fc      = self._read_forecast("sensor.forecast_household_energy")

        if not epex_fc:
            self.log("[Strategy] sensor.forecast_epex has no entries", level="WARNING")
            return

        # ── Current states ────────────────────────────────────────────────────
        soc         = float(self.get_state(SOC_SENSOR) or 50.0)
        boiler_temp = float(self.get_state(WPB_SENSOR) or 45.0)
        hours_to_need   = _hours_to_ev_need(self.get_state(EV_NEED_ENTITY), now)
        ev_capacity_kwh = float(self.get_state(EV_CAPACITY_ENTITY) or 60.0)

        # ── Align arrays to EPEX time axis ───────────────────────────────────
        n      = min(len(epex_fc), 36)
        ref    = epex_fc[:n]
        prices = [e["value"] for e in ref]
        starts = [e["start"] for e in ref]

        pv_h       = _align_values(pv_fc,      ref, default=0.0)
        hp_h       = _align_values(hp_fc,      ref, default=0.0)
        hh_h       = _align_values(hh_fc,      ref, default=HH_FALLBACK)
        net_thermal = _align_values(thermic_fc, ref, default=0.0)
        solar_gain  = _align_attr(thermic_fc,  ref, "solar_gain", default=0.0)

        # ── Per-hour strategy decisions ───────────────────────────────────────
        horizons = [_forecast_horizon(fc, ref) or n
                    for fc in (epex_fc, pv_fc, hp_fc, hh_fc)]
        n_bat    = max(1, min(horizons))
        analysis = ref[:n_bat]

        peaks   = sorted([h for h in ref if h.get("is_tp") and h.get("label") == "peak"],
                         key=lambda x: -x.get("pct", 0))[:MAX_PEAKS]
        troughs = sorted([h for h in ref if h.get("is_tp") and h.get("label") == "trough"],
                         key=lambda x:  x.get("pct", 1))[:MAX_TROUGHS]

        ev_decisions = ev_strategy(analysis[:n_bat], hours_to_need, ev_capacity_kwh)
        ev_kw_bat = [
            EV_FAST_KW if d["mode"] == "fast" else
            EV_SLOW_KW if d["mode"] == "slow" else 0.0
            for d in ev_decisions
        ]

        bat_decisions = battery_strategy(
            pv_h[:n_bat], hp_h[:n_bat], hh_h[:n_bat], ev_kw_bat,
            analysis[:n_bat], soc,
            BATTERY_MIN_SOC, BATTERY_MAX_SOC,
            BATTERY_CAPACITY_KWH, BATTERY_USABLE,
            BATTERY_MIN_KW, BATTERY_MAX_KW,
            BATTERY_EFFICIENCY,
        )
        hp_decisions = hp_strategy(
            net_thermal[:n_bat], solar_gain[:n_bat], HP_SOLAR_COVER_RATIO,
            HP_LOOKAHEAD_OFF, HP_LOOKAHEAD_ON,
        )
        wpb_decisions = wpb_strategy(
            prices[:n_bat], pv_h[:n_bat], hp_h[:n_bat], hh_h[:n_bat],
            analysis[:n_bat], boiler_temp,
            WPB_BOOST_MAX_C, WPB_BOOST_MAX_PRICE, WPB_SOLAR_MIN_SURPLUS,
        )

        # ── Publish ───────────────────────────────────────────────────────────
        cur = bat_decisions[0]
        self.set_state(
            "sensor.strategy_battery",
            state=cur["mode"],
            attributes={
                "friendly_name":  "Batterij strategie",
                "target_grid_w":  cur["target_grid_w"],
                "expected_soc":   cur["expected_soc"],
                "soc_pct":        round(soc, 1),
                "forecast":       _make_forecast(bat_decisions, starts,
                                      ["target_grid_w", "expected_soc"]),
                "last_updated":   now.isoformat(),
            },
        )

        self.set_state(
            "sensor.strategy_ev",
            state=ev_decisions[0]["mode"],
            attributes={
                "friendly_name":  "EV laad strategie",
                "hours_to_need":  round(hours_to_need, 1) if hours_to_need is not None else None,
                "forecast":       _make_forecast(ev_decisions, starts, []),
                "last_updated":   now.isoformat(),
            },
        )

        self.set_state(
            "sensor.strategy_hp",
            state=hp_decisions[0]["mode"],
            attributes={
                "friendly_name": "Warmtepomp strategie",
                "reason":        hp_decisions[0].get("reason", ""),
                "forecast":      _make_forecast(hp_decisions, starts, ["reason"]),
                "last_updated":  now.isoformat(),
            },
        )

        self.set_state(
            "sensor.strategy_wpb",
            state=wpb_decisions[0]["mode"],
            attributes={
                "friendly_name": "WPB boiler strategie",
                "boiler_temp_c": round(boiler_temp, 1),
                "reason":        wpb_decisions[0].get("reason", ""),
                "forecast":      _make_forecast(wpb_decisions, starts, ["reason"]),
                "last_updated":  now.isoformat(),
            },
        )

        self.log(
            f"[Strategy] bat={cur['mode']}"
            f" target={cur['target_grid_w']}W"
            f" exp_soc={cur['expected_soc']:.0f}%"
            f" ev={ev_decisions[0]['mode']}"
            f" hp={hp_decisions[0]['mode']}"
            f" wpb={wpb_decisions[0]['mode']}"
            f" soc={soc:.0f}% boiler={boiler_temp:.0f}°C"
            f" peaks={[p.get('start','')[:16] for p in peaks]}"
            f" troughs={[t.get('start','')[:16] for t in troughs]}"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _read_forecast(self, sensor_id):
        try:
            fc = self.get_state(sensor_id, attribute="forecast")
            return fc if isinstance(fc, list) else []
        except Exception:
            return []


# ── Module-level helpers ──────────────────────────────────────────────────────

def _forecast_horizon(fc, reference):
    """Aantal opeenvolgende ref-uren waarvoor fc daadwerkelijk data heeft."""
    if not fc:
        return 0
    starts = {e["start"] for e in fc}
    for i, r in enumerate(reference):
        if r["start"] not in starts:
            return i
    return len(reference)


def _align_values(fc, reference, default=0.0):
    if not fc:
        return [default] * len(reference)
    lup = {e["start"]: float(e.get("value", default)) for e in fc}
    return [lup.get(ref["start"], default) for ref in reference]


def _align_attr(fc, reference, attr, default=0.0):
    if not fc:
        return [default] * len(reference)
    lup = {e["start"]: float(e.get(attr, default)) for e in fc}
    return [lup.get(ref["start"], default) for ref in reference]


def _make_forecast(decisions, starts, extra_keys):
    result = []
    for i in range(len(decisions)):
        entry = {"start": starts[i] if i < len(starts) else "", "mode": decisions[i]["mode"]}
        for k in extra_keys:
            if k in decisions[i]:
                entry[k] = decisions[i][k]
        result.append(entry)
    return result


def _hours_to_ev_need(raw_state, now):
    if not raw_state or raw_state in ("unknown", "unavailable", ""):
        return None
    try:
        need_dt   = datetime.datetime.fromisoformat(str(raw_state))
        now_naive = now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now
        if hasattr(need_dt, "tzinfo") and need_dt.tzinfo is not None:
            need_dt = need_dt.replace(tzinfo=None)
        delta = (need_dt - now_naive).total_seconds() / 3600.0
        return None if delta < 0 else delta
    except Exception:
        return None


# ── Pure strategy functions ───────────────────────────────────────────────────

def battery_strategy(pv, hp, hh, ev_kw, analysis, soc_now,
                     min_soc, max_soc, capacity_kwh, usable_ratio,
                     min_kw, max_kw, efficiency,
                     tp_weight=TP_WEIGHT):
    """
    Grid-band battery strategy per uur.

    Output per uur: {mode, target_grid_w, expected_soc}

    Controllerlogica (in ems_bat_controller):
      mode = charge    → laden als grid < target_grid_w, nooit ontladen
      mode = discharge → ontladen als grid > target_grid_w, nooit laden
      mode = hold      → doe niets

    Target per scenario:
      charge (tier ≤ 2):
        target_grid_w = forecast_huislast_w + benodigd_laadvermogen_w
        (standby / anti-export: target_grid_w = GRID_MIN_W = 100W)
      discharge (niet-goedkoop, verbruiksuur) — tier-waterfall:
        Beschikbare kWh = (SOC − min_soc) / 100 × usable
        Verdeling: tier 5 krijgt eerst, dan 4, dan 3.
          fraction_t    = alloc_t / cons_t  (0..1)
          target_grid_w = (1 − fraction_t) × net_load × 1000
          → tier 5 (peak):    laagste target, meeste batterijinzet
          → tier 3 (neutral): hoogste target, minste batterijinzet
        Surplus (available > total cons): negatieve target bij hoogste tier
          → target_grid_w = −surplus_kwh × 1000 / n_export_uren
      discharge + export (PV-surplus uur vóór goedkoop segment):
        target_grid_w = −GRID_EXPORT_LIMIT (−5000)
      charge (PV surplus, geen goedkoop segment):
        mode = charge, target_grid_w = GRID_MIN_W (100)
    """
    n      = len(analysis)
    usable = capacity_kwh * usable_ratio

    net_kw_arr = [hh[i] + hp[i] + ev_kw[i] - pv[i] for i in range(n)]
    _LABEL_TIER = {"negative": 0, "trough": 1, "dip": 2, "neutral": 3, "crest": 4, "peak": 5}
    tiers     = [int(h["tier"]) if "tier" in h and h["tier"] != "" else
                 _LABEL_TIER.get(h.get("label", "neutral"), 3)
                 for h in analysis]
    is_tp_arr = [bool(h.get("is_tp", False)) for h in analysis]


    def _kwh_to_soc(kwh):
        return (kwh / usable) * 100.0

    def _survival_soc_from(start_idx, pct_threshold=None):
        """
        SOC nodig bij start_idx om met min_soc het volgende uur te bereiken
        met pct < pct_threshold (een goedkopere laadfase dan het huidige uur).
        Als pct_threshold=None of het uur heeft geen pct-data, wordt de gehele
        resterende horizon meegeteld (meest conservatief).
        """
        demand_kwh = 0.0
        for j in range(start_idx, n):
            if pct_threshold is not None:
                hour_pct = analysis[j].get("pct")
                if hour_pct is not None and hour_pct < pct_threshold:
                    break
            demand_kwh += max(0.0, net_kw_arr[j])
        return min(max_soc, min_soc + _kwh_to_soc(demand_kwh))

    def _pv_headroom_soc(seg_start, seg_end):
        """
        Maximale SOC zodat verwacht PV-surplus (zowel binnen als na het segment)
        volledig geabsorbeerd kan worden zonder export.

        Binnen het segment (seg_start..seg_end-1): uren met netto PV-surplus
        die NIET actief geladen hoeven te worden — de batterij absorbeert ze
        vanzelf via anti-export. Die uren verlagen de ceiling.

        Na het segment (seg_end..): niet-cheap uren met PV-surplus verlagen
        de ceiling ook (klassieke headroom berekening).
        """
        surplus_kwh = 0.0
        # PV-surplus uren binnen het segment (anti-export laadt de batterij sowieso)
        for k in range(seg_start, seg_end):
            surplus_kwh += max(0.0, -(net_kw_arr[k]))
        # PV-surplus uren na het segment (niet-cheap)
        for k in range(seg_end, n):
            if tiers[k] <= 2:
                break
            surplus_kwh += max(0.0, -(net_kw_arr[k]))
        return max(min_soc, max_soc - _kwh_to_soc(surplus_kwh))

    def _charge_min_grid(soc_gain, net_w):
        """
        target_grid_w voor actief laden (charge mode).
        net_w = (hh + hp + ev − pv) × 1000  [W, mag negatief zijn bij PV-surplus]

        Bij soc_gain = 0 (batterij al op ceiling): alleen anti-export, geen forcing.
        Anders: target = net_w + laadvermogen
          → controller laadt exact het geplande vermogen, ongeacht PV-bijdrage.
        Voorbeeld: net_w=−2500W (PV-surplus), rate=6000W → target=3500W.
          Grid importeert 3500W: 6000W laad = 2500W PV + 3500W grid. ✓
        """
        if soc_gain <= 0:
            return GRID_MIN_W   # al op target: alleen anti-export, geen grid forcing
        rate_w = soc_gain / 100.0 * usable * 1000.0 / efficiency
        return round(net_w + rate_w)  # mag negatief zijn bij PV-surplus


    decisions = []
    soc = soc_now
    i   = 0

    while i < n:
        tier = tiers[i]

        if tier <= 2:
            # ── Cheap segment: vind volledige omvang ──────────────────────────
            j = i
            while j < n and tiers[j] <= 2:
                j += 1
            seg_hours    = list(range(i, j))
            seg_min_tier = min(tiers[k] for k in seg_hours)

            # Tier 0/1: altijd laden naar 100%, geen headroom-reductie.
            # Tier 2: laden naar survival; headroom voorkomt export na het segment.
            ceiling  = max_soc if seg_min_tier <= 1 else _pv_headroom_soc(i, j)
            seg_tp_pct = min(
                (analysis[k].get("pct") for k in seg_hours
                 if analysis[k].get("pct") is not None),
                default=None,
            )
            survival = _survival_soc_from(j, seg_tp_pct)

            if seg_min_tier == 0:
                # Tier 0: wacht-uren eerst, daarna actief laden naar ceiling
                import math
                max_soc_gain_per_h = max_kw * efficiency / usable * 100.0
                n_charge_end = min(2, max(1, math.ceil((max_soc - soc) / max_soc_gain_per_h)))
                n_charge_end = min(n_charge_end, len(seg_hours))

                tp_idx_in_seg = None
                for idx, k in enumerate(seg_hours):
                    if is_tp_arr[k]:
                        tp_idx_in_seg = idx
                        break

                charge_start = tp_idx_in_seg if tp_idx_in_seg is not None \
                               else len(seg_hours) - n_charge_end

                for idx, k in enumerate(seg_hours):
                    soc_before = soc
                    net_w      = net_kw_arr[k] * 1000
                    if idx < charge_start:
                        # Wacht: anti-export target; batterij absorbeert eventueel PV-surplus
                        target_w = GRID_MIN_W
                    else:
                        remaining  = len(seg_hours) - idx
                        gain_per_h = max(0.0, (ceiling - soc) / remaining)
                        soc        = min(max_soc, soc + gain_per_h)
                        target_w   = _charge_min_grid(soc - soc_before, net_w)
                    decisions.append({
                        "mode":           "charge",
                        "target_grid_w":  target_w,
                        "expected_soc":   round(soc, 1),
                    })

            elif seg_min_tier <= 1:
                # Trough (tier 1): charge naar ceiling, SOC-gain TP-gewogen.
                raw_target = max_soc
                soc_final  = max(survival, min(raw_target, ceiling))
                total_gain = max(0.0, soc_final - soc)
                tp_set     = {k for k in seg_hours if is_tp_arr[k]}
                n_tp_s     = len(tp_set)
                n_band_s   = len(seg_hours) - n_tp_s
                total_w    = n_tp_s * tp_weight + n_band_s
                gain_unit  = total_gain / total_w if total_w > 0 else 0.0

                for k in seg_hours:
                    soc_before = soc
                    net_w      = net_kw_arr[k] * 1000
                    w          = tp_weight if k in tp_set else 1
                    soc        = min(max_soc, soc + gain_unit * w)
                    gain       = soc - soc_before
                    if gain > 0:
                        mode     = "charge"
                        target_w = _charge_min_grid(gain, net_w)
                    else:
                        mode     = "hold"
                        target_w = GRID_MIN_W
                    decisions.append({
                        "mode":           mode,
                        "target_grid_w":  target_w,
                        "expected_soc":   round(soc, 1),
                    })

            else:
                # Tier 2 (dip): per-uur survival check.
                # survival_here berekend INCLUSIEF het huidige uur én resterende dip-uren
                # (niet alleen na het segment). Dit geeft de juiste bodem voor de SOC.
                # Boven survival → discharge op MIN_GRID (geen hold).
                # Onder survival → charge naar survival_here.
                for k in seg_hours:
                    soc_before    = soc
                    net_w         = net_kw_arr[k] * 1000
                    pct_k         = analysis[k].get("pct")
                    survival_here = min(_survival_soc_from(k, pct_k), ceiling)

                    if soc >= survival_here:
                        # Genoeg SOC: ontladen (batterij levert netto verbruik)
                        soc      = max(min_soc, soc - _kwh_to_soc(max(0.0, net_kw_arr[k])))
                        mode     = "discharge"
                        target_w = GRID_MIN_W
                    else:
                        # Te weinig SOC: laden naar survival_here
                        gain     = survival_here - soc_before
                        soc      = min(max_soc, soc_before + gain)
                        gain     = soc - soc_before
                        mode     = "charge"
                        target_w = _charge_min_grid(gain, net_w)

                    decisions.append({
                        "mode":           mode,
                        "target_grid_w":  target_w,
                        "expected_soc":   round(soc, 1),
                    })

            i = j

        else:
            # ── Non-cheap segment (tier 3/4/5): altijd discharge ─────────────
            # Alle tiers: batterij levert netto verbruik; target = GRID_MIN_W (≈0W)
            # Alleen als tier 0 volgt: waterfall over tier 4/5 → target kan negatief
            j = i
            while j < n and tiers[j] > 2:
                j += 1
            seg_hours     = list(range(i, j))
            cheap_follows = j < n

            # Volgt er een tier-0 segment? Alleen dan draining to min_soc via waterfall.
            draining_to_empty = False
            if cheap_follows:
                seg_j_end = j
                while seg_j_end < n and tiers[seg_j_end] <= 2:
                    seg_j_end += 1
                if min(tiers[k] for k in range(j, seg_j_end)) == 0:
                    draining_to_empty = True

            # Waterfall alleen over tier 4/5 discharge uren
            tier_cons = {5: 0.0, 4: 0.0}
            n_dis     = {5: 0,   4: 0}
            for k in seg_hours:
                t = min(5, max(3, tiers[k]))
                if t >= 4 and net_kw_arr[k] > 0:
                    tier_cons[t] += net_kw_arr[k]
                    n_dis[t]     += 1

            if draining_to_empty:
                needed_kwh = max(0.0, (soc - min_soc) / 100.0 * usable)
            else:
                needed_kwh = tier_cons[5] + tier_cons[4]

            remaining = needed_kwh
            alloc = {}
            for t in [5, 4]:
                alloc[t]  = min(remaining, tier_cons[t])
                remaining = max(0.0, remaining - alloc[t])

            export_per_h = {5: 0.0, 4: 0.0}
            if draining_to_empty and remaining > 0:
                exp_rem = remaining
                for t in [5, 4]:
                    if n_dis[t] > 0:
                        max_exp         = n_dis[t] * GRID_EXPORT_LIMIT / 1000.0
                        exp_t           = min(exp_rem, max_exp)
                        export_per_h[t] = exp_t / n_dis[t]
                        exp_rem         = max(0.0, exp_rem - exp_t)

            frac = {}
            for t in [5, 4]:
                bat_total = alloc[t] + export_per_h[t] * n_dis[t]
                frac[t]   = (bat_total / tier_cons[t]) if tier_cons[t] > 0 else 1.0

            for k in seg_hours:
                t   = min(5, max(3, tiers[k]))
                net = net_kw_arr[k]

                if net > 0:
                    if t >= 4:
                        bat_kw        = frac[t] * net
                        _raw          = round((1.0 - frac[t]) * net * 1000)
                        target_grid_w = max(-GRID_EXPORT_LIMIT,
                                            _raw if _raw < 0 else max(GRID_MIN_W, _raw))
                    else:
                        # Tier 3: batterij levert alles, target = GRID_MIN_W
                        bat_kw        = net
                        target_grid_w = GRID_MIN_W
                    soc = max(min_soc, soc - bat_kw / usable * 100.0)
                else:
                    # PV surplus: batterij standby
                    target_grid_w = GRID_MIN_W

                decisions.append({
                    "mode":           "discharge",
                    "target_grid_w":  target_grid_w,
                    "expected_soc":   round(soc, 1),
                })

            i = j

    return decisions


def ev_strategy(analysis, hours_to_need, capacity_kwh):
    """
    Modes: fast | slow | off

    Flexible (hours_to_need is None or in the past):
      slow  – tier ≤ 1 (trough / negative); sessions may be split
      off   – otherwise

    Deadline (hours_to_need > 0):
      Assume empty battery.  Charge at fast speed (EV_FAST_KW).
      charge_hours = ceil(capacity_kwh / EV_FAST_KW)
      Pick the cheapest charge_hours hours within [0, deadline).
      fast  – selected hours
      off   – otherwise
    """
    import math
    n = len(analysis)

    if hours_to_need is None or hours_to_need <= 0:
        return [
            {"mode": "slow" if int(ha.get("tier", 3) or 3) <= 1 else "off"}
            for ha in analysis
        ]

    charge_hours  = math.ceil(capacity_kwh / EV_FAST_KW)
    deadline_idx  = min(n, int(hours_to_need))

    candidates    = sorted(range(deadline_idx), key=lambda i: analysis[i].get("pct", 0.5))
    cheap_set     = set(candidates[:charge_hours])

    return [
        {"mode": "fast" if i in cheap_set else "off"}
        for i in range(n)
    ]


def hp_strategy(net_thermal, solar_gain, solar_cover_ratio,
                lookahead_off=1, lookahead_on=2):
    """
    Bepaal per uur of de WP aan of uit moet.

    Basis: off wanneer solar_gain >= solar_cover_ratio van de bruto thermische vraag.

    Pre-empting:
      pre_solar  – WP gaat lookahead_off uur eerder uit als solar-dekking eraan komt.
      pre_heat   – WP gaat lookahead_on uur eerder aan als solar-dekking bijna eindigt.
    """
    n = len(net_thermal)

    solar_covers = []
    for net, gain in zip(net_thermal, solar_gain):
        gross = net + gain
        solar_covers.append(gross > 0.05 and gain / gross >= solar_cover_ratio)

    decisions = []
    for i in range(n):
        if not solar_covers[i]:
            if any(solar_covers[i + 1: i + 1 + lookahead_off]):
                mode, reason = "off", "pre_solar"
            else:
                mode, reason = "on", ""
        else:
            if any(not solar_covers[j]
                   for j in range(i + 1, min(i + 1 + lookahead_on, n))):
                mode, reason = "on", "pre_heat"
            else:
                mode, reason = "off", "solar_covers_demand"

        decisions.append({"mode": mode, "reason": reason})

    return decisions


def wpb_strategy(prices, pv, hp, hh, analysis, boiler_temp,
                 boost_max_c, boost_max_price, solar_min_surplus):
    """
    boost  – cheap trough + boiler below max temp
    solar  – net PV surplus available
    normal – otherwise
    """
    decisions = []
    for i, ha in enumerate(analysis):
        net   = pv[i] - hp[i] - hh[i]
        price = prices[i]
        cls   = ha["label"]

        if cls == "trough" and price <= boost_max_price and boiler_temp < boost_max_c:
            mode   = "boost"
            reason = f"cheap {price:.3f} €/kWh boiler {boiler_temp:.0f}°C"
        elif net >= solar_min_surplus:
            mode   = "solar"
            reason = f"surplus {net:.2f} kWh"
        else:
            mode   = "normal"
            reason = ""

        decisions.append({"mode": mode, "reason": reason})

    return decisions
