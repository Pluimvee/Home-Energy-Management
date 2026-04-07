"""
ems_strategy.py – Energy Management Strategy
============================================
AppDaemon app.  Runs every hour at HH:00:30.

Reads five forecast sensors and current device states.
Produces per-device strategy sensors with:
  state      = decision for the CURRENT hour  (string)
  forecast   = list of up to FORECAST_HOURS dicts for upcoming hours

Published sensors
-----------------
  sensor.strategy_battery       charge / discharge / level / hold
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

Battery power (500 W – 10 000 W)
---------------------------------
  Charge power  = (kWh needed to reach target_SOC) / (hours in block), TP-first
  Discharge pwr = only before tier-0 blocks, to survival_soc
  Both clamped to [BATTERY_MIN_KW, BATTERY_MAX_KW].

NOTE: no service calls – automations / ESPHome connect sensors to hardware.
"""

import datetime

import appdaemon.plugins.hass.hassapi as hass

# ── Constants ─────────────────────────────────────────────────────────────────
FORECAST_HOURS         = 8    # uren vooruit voor EV / HP / WPB forecast
MAX_PEAKS      = 2
MAX_TROUGHS    = 2

BATTERY_CAPACITY_KWH = 20.0
BATTERY_USABLE       = 0.93    # 100% − 7% bodemreserve = 18.6 kWh bruikbaar
BATTERY_EFFICIENCY   = 0.97    # roundtrip rendement
BATTERY_MIN_SOC      = 7       # %
BATTERY_MAX_SOC      = 100     # %
BATTERY_RESERVE_SOC  = 30.0    # % SOC na ontladen als er geen goedkoop uur volgt
BATTERY_MIN_KW       = 0.5     # kW
BATTERY_MAX_KW       = 10.0    # kW

# ── Levelling grid target ─────────────────────────────────────────────────────
LEVEL_WINDOW_H      = 8     # uren rolling window voor gemiddelde netto vraag
LEVEL_MIN_W         = 200   # W minimale netafname — voorkomt export
TP_WEIGHT           = 3     # TP-uren krijgen 3× meer SOC-gain dan band-uren binnen cheap segment
GRID_TARGET_W       = 200   # W grid target voor alle modes — agressief ontladen of PV absorberen
GRID_TARGET_CHARGE  = 200   # W minimale import in charge mode — charge-only leveling

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
        self.run_in(self.run_strategy, 35)

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
        # Analysis fields (pct, is_tp, label) are pre-computed in
        # sensor.forecast_epex entries by ems_forecasts / enrich_hourly().
        # n_bat = minimum van de horizons van alle battery-inputs.
        # Een forecast die volledig ontbreekt telt niet mee (of n als fallback).
        horizons = [_forecast_horizon(fc, ref) or n
                    for fc in (epex_fc, pv_fc, hp_fc, hh_fc)]
        n_bat    = max(1, min(horizons))
        n_out    = min(n, FORECAST_HOURS + 1)
        analysis = ref[:n_bat]   # batterij ziet de gemeenschappelijke horizon

        peaks   = sorted([h for h in ref if h.get("is_tp") and h.get("label") == "peak"],
                         key=lambda x: -x.get("pct", 0))[:MAX_PEAKS]
        troughs = sorted([h for h in ref if h.get("is_tp") and h.get("label") == "trough"],
                         key=lambda x:  x.get("pct", 1))[:MAX_TROUGHS]

        # EV eerder berekenen zodat we het laden meenemen in de batterij-vraag
        ev_decisions_full = ev_strategy(analysis[:n_bat], hours_to_need, ev_capacity_kwh)
        ev_kw_bat = [
            EV_FAST_KW if d["mode"] == "fast" else
            EV_SLOW_KW if d["mode"] == "slow" else 0.0
            for d in ev_decisions_full
        ]
        ev_decisions = ev_decisions_full[:n_out]

        bat_decisions = battery_strategy(
            pv_h[:n_bat], hp_h[:n_bat], hh_h[:n_bat], ev_kw_bat,
            analysis[:n_bat], soc,
            BATTERY_MIN_SOC, BATTERY_MAX_SOC,
            BATTERY_CAPACITY_KWH, BATTERY_USABLE,
            BATTERY_MIN_KW, BATTERY_MAX_KW,
            BATTERY_EFFICIENCY, BATTERY_RESERVE_SOC,
        )
        hp_decisions = hp_strategy(
            net_thermal[:n_out], solar_gain[:n_out], HP_SOLAR_COVER_RATIO,
            HP_LOOKAHEAD_OFF, HP_LOOKAHEAD_ON,
        )
        wpb_decisions = wpb_strategy(
            prices[:n_out], pv_h[:n_out], hp_h[:n_out], hh_h[:n_out],
            analysis[:n_out], boiler_temp,
            WPB_BOOST_MAX_C, WPB_BOOST_MAX_PRICE, WPB_SOLAR_MIN_SURPLUS,
        )

        # ── Publish ───────────────────────────────────────────────────────────
        self.set_state(
            "sensor.strategy_battery",
            state=bat_decisions[0]["mode"],
            attributes={
                "friendly_name":     "Batterij strategie",
                "grid_target_w":     bat_decisions[0]["grid_target_w"],
                "soc_target":        bat_decisions[0]["soc_target"],
                "charge_power_w":    bat_decisions[0]["charge_power_w"],
                "discharge_power_w": bat_decisions[0]["discharge_power_w"],
                "soc_pct":           round(soc, 1),
                "forecast":          _make_forecast(bat_decisions, starts,
                                         ["grid_target_w", "soc_target",
                                          "charge_power_w", "discharge_power_w"]),
                "last_updated":      now.isoformat(),
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
            f"[Strategy] bat={bat_decisions[0]['mode']}({bat_decisions[0]['charge_power_w'] or bat_decisions[0]['discharge_power_w']}W)"
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
    for i in range(0, len(decisions)):
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
                     min_kw, max_kw, efficiency, reserve_soc,
                     level_window=LEVEL_WINDOW_H, level_min_w=LEVEL_MIN_W,
                     tp_weight=TP_WEIGHT):
    """
    SOC-target battery strategy per uur.

    Mode per uur:
      charge    tier 0/1/2 → laden naar soc_target
      discharge pct ≥ 0.5  → ontladen naar soc_target
      level     pct < 0.5  → grid-gestuurd levellen via grid_target_w

    soc_target binnen cheap segment (tier ≤ 2):
      - Tier 0: vroege uren → min_soc+10% (ruimte voor PV), laatste uur → max_soc
      - Tier 1: SOC-gain verdeeld; TP-uren krijgen tp_weight × meer dan band-uren
      - Tier 2: idem maar soc_final = survival SOC (netto vraag tot volgende tier ≤ 1)
      - pct ≥ 0.5: soc_target = soc − (soc − min_soc) × (pct − 0.5) × 2
      - pct < 0.5 (level): grid_target_w is leidend, soc_target n.v.t.

    charge_power_w / discharge_power_w zijn 0 (gereserveerd voor expliciete
    last-minute push).
    """
    n      = len(analysis)
    usable = capacity_kwh * usable_ratio

    net_kw_arr = [hh[i] + hp[i] + ev_kw[i] - pv[i] for i in range(n)]
    _LABEL_TIER = {"negative": 0, "trough": 1, "dip": 2, "neutral": 3, "crest": 4, "peak": 5}
    tiers      = [int(h["tier"]) if "tier" in h and h["tier"] != "" else
                  _LABEL_TIER.get(h.get("label", "neutral"), 3)
                  for h in analysis]
    is_tp_arr  = [bool(h.get("is_tp", False)) for h in analysis]

    def _rolling_avg_w(i):
        """Informatief rolling average netto vraag (voor dashboard/debug)."""
        window = net_kw_arr[i: i + level_window]
        avg_kw = sum(window) / len(window)
        return max(level_min_w, round(avg_kw * 1000))

    def _kwh_to_soc(kwh):
        return (kwh / usable) * 100.0

    def _survival_soc_from(start_idx):
        """SOC nodig bij start_idx om met min_soc de volgende tier ≤ 1 te bereiken."""
        demand_kwh = 0.0
        for j in range(start_idx, n):
            if tiers[j] <= 1:
                break
            demand_kwh += max(0.0, net_kw_arr[j])
        return min(max_soc, min_soc + _kwh_to_soc(demand_kwh))

    def _pv_headroom_soc(seg_end):
        """
        Maximale SOC na cheap segment zodat PV surplus in de volgende niet-cheap uren
        niet leidt tot export.
        = max_soc - verwacht PV surplus (netto negatieve uren) tot het volgende cheap segment.
        """
        surplus_kwh = 0.0
        for k in range(seg_end, n):
            if tiers[k] <= 2:
                break
            surplus_kwh += max(0.0, -(net_kw_arr[k]))  # netto negatief = PV surplus
        return max(min_soc, max_soc - _kwh_to_soc(surplus_kwh))

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

            ceiling    = _pv_headroom_soc(j)
            survival   = _survival_soc_from(j)

            if seg_min_tier == 0:
                # Tier 0: alle uren in charge-mode (nooit ontladen).
                # - Vroege uren: soc_target = huidig soc → geen actief laden,
                #   fallback op grid_target=200 W (absorbeert PV-surplus).
                # - Vanaf de valley-TP (diepste prijs): geleidelijk opladen naar max_soc.
                # - Laatste uur: soc_target = max_soc (volledig vol).
                # Zonder TP: laad de laatste n_charge_end uren actief.
                import math
                max_soc_gain_per_h = max_kw * efficiency / usable * 100.0
                n_charge_end = min(2, max(1, math.ceil((max_soc - soc) / max_soc_gain_per_h)))
                n_charge_end = min(n_charge_end, len(seg_hours))

                # Zoek eerste valley-TP in het segment (na ems_epex fix: elke TP in tier 0 is een valley)
                tp_idx_in_seg = None
                for idx, k in enumerate(seg_hours):
                    if is_tp_arr[k]:
                        tp_idx_in_seg = idx
                        break

                charge_start = tp_idx_in_seg if tp_idx_in_seg is not None \
                               else len(seg_hours) - n_charge_end

                for idx, k in enumerate(seg_hours):
                    if idx < charge_start:
                        # Wacht: charge mode met huidige soc als target → geen actief laden
                        decisions.append({
                            "mode": "charge", "grid_target_w": GRID_TARGET_CHARGE,
                            "soc_target": round(soc, 1),
                            "charge_power_w": 0, "discharge_power_w": 0,
                        })
                    else:
                        # Actief laden: verdeel resterende gain gelijkmatig tot einde segment
                        # ceiling: niet verder laden dan nodig om nakomend PV-surplus te absorberen
                        remaining = len(seg_hours) - idx
                        gain_per_h = max(0.0, (ceiling - soc) / remaining)
                        soc = min(ceiling, soc + gain_per_h)
                        decisions.append({
                            "mode": "charge", "grid_target_w": GRID_TARGET_CHARGE,
                            "soc_target": round(soc, 1),
                            "charge_power_w": 0, "discharge_power_w": 0,
                        })

            else:
                # Tier 1/2: verdeel SOC-gain gewogen over TP/band uren
                # ceiling: nooit zoveel laden dat PV surplus in volgende uren niet past
                raw_target = max_soc if seg_min_tier <= 1 else _survival_soc_from(j)
                soc_final  = max(survival, min(raw_target, ceiling))
                total_gain = max(0.0, soc_final - soc)
                tp_set     = {k for k in seg_hours if is_tp_arr[k]}
                n_tp_s     = len(tp_set)
                n_band_s   = len(seg_hours) - n_tp_s
                total_w    = n_tp_s * tp_weight + n_band_s
                gain_unit  = total_gain / total_w if total_w > 0 else 0.0

                for k in seg_hours:
                    w   = tp_weight if k in tp_set else 1
                    soc = min(max_soc, soc + gain_unit * w)
                    decisions.append({
                        "mode": "charge", "grid_target_w": GRID_TARGET_CHARGE,
                        "soc_target": round(soc, 1),
                        "charge_power_w": 0, "discharge_power_w": 0,
                    })
            i = j

        else:
            # ── Non-cheap blok: groepeer alle aaneengesloten niet-cheap uren ──
            j = i
            while j < n and tiers[j] > 2:
                j += 1
            seg_hours = list(range(i, j))

            # Doel: zo veel mogelijk ontladen tijdens dure uren.
            #
            # soc_target = realistisch (natural consumption per uur).
            # grid_target_w = 200 W voor alle uren → controller ontlaadt zo agressief
            # mogelijk en gaat door na soc_target (p1_w > 200 W → blijven ontladen).
            #
            # PV-surplus uren (net_kw ≤ 0): anti-export voorkomt ontladen;
            # level mode laat de batterij PV absorberen → SOC stijgt.
            # Volgt er een cheap segment? Dan pre-emptyen (batterij leeg voor tier ≤ 2).
            cheap_follows = j < n

            for k in seg_hours:
                pct_k     = analysis[k].get("pct", 0.5)
                natural_k = max(0.0, net_kw_arr[k] / usable * 100)

                if net_kw_arr[k] <= 0:
                    # PV-surplus uur
                    pv_gain_k = max(0.0, -net_kw_arr[k] / usable * 100)
                    if cheap_follows:
                        # Pre-empty: exporteer batterij zodat er maximaal ruimte is voor de
                        # volgende charge-periode. Export mode = enige mode zonder anti-export.
                        soc_t = max(min_soc, soc - 0.0)  # controller ontlaadt tot min_soc
                        mode  = "export"
                    else:
                        # Geen cheap segment in horizon: absorbeer PV in batterij
                        soc_t = min(max_soc, soc + pv_gain_k)
                        mode  = "charge"
                elif pct_k >= 0.5:
                    # Netto vraag + duur uur: zo agressief mogelijk ontladen
                    soc_t = max(min_soc, soc - natural_k)
                    mode  = "discharge"
                else:
                    # Netto vraag, niet duur genoeg voor actief ontladen: standby
                    soc_t = max(min_soc, soc - natural_k)
                    mode  = "charge"  # charge met huidige soc als target = standby

                soc = soc_t
                decisions.append({
                    "mode": mode, "grid_target_w": GRID_TARGET_W,
                    "soc_target": round(soc_t, 1),
                    "charge_power_w": 0, "discharge_power_w": 0,
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
        # Flexible: slow bij tier ≤ 1 (trough / negative)
        return [
            {"mode": "slow" if int(ha.get("tier", 3) or 3) <= 1 else "off"}
            for ha in analysis
        ]

    # Deadline mode
    charge_hours  = math.ceil(capacity_kwh / EV_FAST_KW)
    deadline_idx  = min(n, int(hours_to_need))          # last usable slot index

    # Sort candidate hours by pct (cheapest first), pick the required count
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
                   Vloer is dan al op temperatuur → geen WP nodig tijdens solar.
      pre_heat   – WP gaat lookahead_on uur eerder aan als solar-dekking bijna eindigt.
                   Opwarmen vóór de dure avondpiek zodat de WP daarna niet hoeft.
    """
    n = len(net_thermal)

    # Bereken basis solar-dekking per uur
    solar_covers = []
    for net, gain in zip(net_thermal, solar_gain):
        gross = net + gain
        solar_covers.append(gross > 0.05 and gain / gross >= solar_cover_ratio)

    decisions = []
    for i in range(n):
        if not solar_covers[i]:
            # Kijk of solar binnen lookahead_off uur begint
            if any(solar_covers[i + 1: i + 1 + lookahead_off]):
                mode, reason = "off", "pre_solar"
            else:
                mode, reason = "on", ""
        else:
            # Kijk of solar binnen lookahead_on uur eindigt
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
