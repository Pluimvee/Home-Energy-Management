"""
ems_forecasts.py  –  Forecast sensor publisher
================================================
Publishes one HA sensor per forecast type.

  state     = forecasted value for the current hour  (recorded by HA history)
  forecast  = [{start, value, ...}, ...]  36 entries: current hour + next 35 h

Published sensors
-----------------
  sensor.forecast_epex              EUR/kWh  hourly-averaged EPEX prices
  sensor.forecast_outside_temp      °C       temperature per hour
  sensor.forecast_irradiance        W/m²     shortwave irradiance per hour
  sensor.forecast_pv                kWh      PV production per hour
  sensor.forecast_thermic           kWh      Net thermal demand per hour
                                             extra fields per entry:
                                               solar_gain kWh  thermal demand reduction from solar
                                               pct        0–1  relative position in 24 h window
  sensor.forecast_heatpump          kWh      Heat pump electrical consumption per hour
                                             (derived from forecast_thermic / COP)
                                             extra fields per entry:
                                               cop  COP for that hour
                                               pct  0–1  relative position in 24 h window
  sensor.forecast_household_energy  kWh      Baseline household per hour

All forecasts span exactly 36 hours from the current hour (entry 0 = now,
entry 35 = 35 h ahead).  After midnight and at every weather update the
full 36-hour window is rebuilt from fresh data.

Update triggers
---------------
  sensor.nordpool_electricity_market_price  state or tomorrow_valid change → EPEX rebuild
  weather.athome                            state change → weather forecasts rebuild
  sensor.energy_calibration                 new coefficients → weather forecasts rebuild
  Daily at 00:05                            new day → full rebuild
  Hourly at :00:05                          tick → advance state + trim past entries
  On startup (15 s delay)                   full rebuild
"""

import os
import datetime as _dt

import appdaemon.plugins.hass.hassapi as hass
import requests as _requests
from datetime import timedelta

from ems_base import (
    NORDPOOL_ENTITY,
    WEATHER_ENTITY,
    HP_SETPOINT_ENTITY,
    SOLAR_SENSORS_TODAY,
    SOLAR_SENSORS_TOMORROW,
    LATITUDE,
    LONGITUDE,
    nordpool_to_hourly,
    solar_per_hour_irr,
    hp_slots_to_hourly,
    irr_from_weather_forecast,
    temp_from_weather_forecast,
    make_flat_forecast,
    forecast_window_stats,
)
from ems_epex import enrich_hourly
from ems_heating import (
    hp_hourly_forecast,
    K_DEFAULT,
    BETA_DEFAULT,

)

CALIB_ENTITY = "sensor.energy_calibration"

# Forecast sensors managed by this module (hourly state update + trim).
# sensor.forecast_epex is excluded – it is fully managed by _publish_epex()
# (Nordpool rebuild) and _advance_epex() (hourly trim + re-analysis).
_FORECAST_SENSORS = [
    "sensor.forecast_pv",
    "sensor.forecast_thermic",
    "sensor.forecast_heatpump",
    "sensor.forecast_outside_temp",
    "sensor.forecast_irradiance",
    "sensor.forecast_household_energy",
]


class EmsForecasts(hass.Hass):

    def initialize(self):
        self._k           = K_DEFAULT
        self._beta_w      = BETA_DEFAULT
        self._cop_a       = 3.5
        self._cop_b       = 0.08
        self._cop_c       = 0.0
        self._irr_scale   = 1.0
        self._temp_bias_a = 1.0
        self._temp_bias_b = 0.0
        self._irr_bias_a  = 1.0
        self._irr_bias_b  = 0.0
        self._hh_base     = [0.4]    * 24  # kWh/h per hour-of-day
        self._pv_eta      = [0.0]    * 24  # ×1000 scaled (mWh/(W/m²·h)) per hour-of-day
        self._gamma_h     = [0.0]    * 24  # ×1000 scaled (mWh/(W/m²·h)) per hour-of-day
        self._calib_src   = "default"

        self.listen_state(self._on_nordpool, NORDPOOL_ENTITY)
        self.listen_state(self._on_nordpool, NORDPOOL_ENTITY,
                          attribute="tomorrow_valid")
        self.listen_state(self._on_weather, WEATHER_ENTITY)
        self.listen_state(self._on_calib, CALIB_ENTITY)

        self.run_daily(self._on_new_day, "00:05:00")
        self.run_hourly(self._on_hour, "00:00:05")
        self.run_in(self._on_startup, 15)

    # ── Trigger handlers ──────────────────────────────────────────────────────

    def _on_startup(self, kwargs):
        self._load_calibration()
        self._build_epex_raw()
        self._refresh_epex()
        self._publish_weather_forecasts()

    def _on_nordpool(self, entity, attribute, old, new, kwargs):
        self._build_epex_raw()
        self._refresh_epex()

    def _on_weather(self, entity, attribute, old, new, kwargs):
        self._publish_weather_forecasts(update_state=False)

    def _on_calib(self, entity, attribute, old, new, kwargs):
        self._load_calibration()
        self._publish_weather_forecasts(update_state=False)

    def _on_hour(self, kwargs):
        """
        Hourly tick at :00:05.
          1. Refresh sensor.forecast_epex: window shift + re-analysis.
          2. For all other forecast sensors:
             a. Read stored forecast attribute.
             b. Write current-hour value as sensor state (triggers recorder).
             c. Trim past entries from the forecast attribute.
        """
        now     = self.datetime()
        today   = now.date()
        cur_h   = now.hour
        today_s = today.isoformat()
        now_iso = _dt.datetime(today.year, today.month, today.day, cur_h).isoformat()

        self._refresh_epex()

        updated = 0
        for entity_id in _FORECAST_SENSORS:
            try:
                all_state = self.get_state(entity_id, attribute="all")
                if not all_state:
                    continue
                attrs    = dict(all_state.get("attributes", {}))
                forecast = attrs.get("forecast", [])
                if not forecast:
                    continue

                cur_val = None
                for entry in forecast:
                    start = entry.get("start", "")
                    if start[:10] == today_s and int(start[11:13]) == cur_h:
                        cur_val = entry.get("value")
                        break

                if cur_val is None:
                    continue

                trimmed = [e for e in forecast if e.get("start", "") >= now_iso]

                # Recompute window stats + pct after trimming
                vals_trimmed = [float(e.get("value", 0.0)) for e in trimmed]
                stats, pcts  = forecast_window_stats(vals_trimmed)
                for i, e in enumerate(trimmed):
                    e["pct"] = pcts[i]

                attrs["forecast"]  = trimmed
                attrs["horizon"]   = stats["horizon"]
                attrs["min"]          = stats["min"]
                attrs["max"]          = stats["max"]
                attrs["avg"]          = stats["avg"]
                attrs["median"]       = stats["median"]
                attrs["last_updated"] = now.isoformat()

                self.set_state(
                    entity_id,
                    state=round(float(cur_val), 5),
                    attributes=attrs,
                    replace=True,
                )
                updated += 1
            except Exception as exc:
                self.log(
                    f"[EmsForecasts._on_hour] {entity_id}: {exc}",
                    level="WARNING",
                )
        self.log(
            f"[EmsForecasts] Hourly tick h={cur_h}: "
            f"{updated}/{len(_FORECAST_SENSORS)} sensors updated"
        )

    def _on_new_day(self, kwargs):
        self._build_epex_raw()
        self._refresh_epex()
        self._publish_weather_forecasts()

    # ── Calibration loader ────────────────────────────────────────────────────

    def _load_calibration(self):
        try:
            def _f(attr):
                v = self.get_state(CALIB_ENTITY, attribute=attr)
                return float(v) if v is not None else None

            if (v := _f("k"))           is not None: self._k           = v
            if (v := _f("beta_irr"))    is not None: self._beta_w      = v

            if (v := _f("cop_a"))       is not None: self._cop_a       = v
            if (v := _f("cop_b"))       is not None: self._cop_b       = v
            if (v := _f("cop_c"))       is not None: self._cop_c       = v
            if (v := _f("irr_scale"))   is not None: self._irr_scale   = v
            if (v := _f("temp_bias_a")) is not None: self._temp_bias_a = v
            if (v := _f("temp_bias_b")) is not None: self._temp_bias_b = v
            if (v := _f("irr_bias_a"))  is not None: self._irr_bias_a  = v
            if (v := _f("irr_bias_b"))  is not None: self._irr_bias_b  = v
            hh = self.get_state(CALIB_ENTITY, attribute="hh_base")
            if isinstance(hh, list) and len(hh) == 24:
                self._hh_base = [float(v) for v in hh]
            pv_eta = self.get_state(CALIB_ENTITY, attribute="pv_eta")
            if isinstance(pv_eta, list) and len(pv_eta) == 24:
                self._pv_eta = [float(v) for v in pv_eta]
            gamma_h = self.get_state(CALIB_ENTITY, attribute="gamma_h")
            if isinstance(gamma_h, list) and len(gamma_h) == 24:
                self._gamma_h = [float(v) for v in gamma_h]
            self._calib_src = self.get_state(CALIB_ENTITY, attribute="source") or "default"
            pv_calibrated    = sum(1 for v in self._pv_eta    if v > 0.01)
            gamma_calibrated = sum(1 for v in self._gamma_h   if v > 0.01)
            self.log(
                f"[EmsForecasts] Calibration loaded: k={self._k:.4f} "
                f"beta={self._beta_w:.6f} "
                f"cop={self._cop_a:.2f}+{self._cop_b:.3f}·T+{self._cop_c:.5f}·T² "
                f"irr_scale={self._irr_scale:.3f} "
                f"temp_bias={self._temp_bias_a:.3f}×+{self._temp_bias_b:.2f} "
                f"irr_bias={self._irr_bias_a:.3f}×+{self._irr_bias_b:.1f} "
                f"pv_eta={pv_calibrated}/24h "
                f"gamma_h={gamma_calibrated}/24h "
                f"source={self._calib_src}"
            )
        except Exception as exc:
            self.log(f"[EmsForecasts] Could not load calibration: {exc}",
                     level="WARNING")

    # ── EPEX sensor ───────────────────────────────────────────────────────────

    def _build_epex_raw(self):
        """
        Laad Nordpool-prijzen en schrijf ruwe entries naar sensor.forecast_epex.
        Geen analyse — _refresh_epex() doet dat altijd daarna.
        """
        raw_today      = self.get_state(NORDPOOL_ENTITY, attribute="raw_today")
        raw_tomorrow   = self.get_state(NORDPOOL_ENTITY, attribute="raw_tomorrow")
        tomorrow_valid = self.get_state(NORDPOOL_ENTITY, attribute="tomorrow_valid")

        today_prices = nordpool_to_hourly(raw_today or [])
        if not today_prices:
            self.log("[EmsForecasts] No Nordpool prices available", level="WARNING")
            return

        now   = self.datetime()
        today = now.date()
        cur_h = now.hour

        tom_prices = []
        if tomorrow_valid:
            try:
                tp = nordpool_to_hourly(raw_tomorrow or [])
                if tp:
                    tom_prices = tp
            except Exception as exc:
                self.log(f"[EmsForecasts] Nordpool raw_tomorrow parse error: {exc}",
                         level="WARNING")

        all_prices = today_prices[cur_h:] + tom_prices
        epex_fc    = all_prices[:36]
        n          = len(epex_fc)
        start_dt   = _dt.datetime(today.year, today.month, today.day, cur_h)

        entries = [
            {
                "start": (start_dt + _dt.timedelta(hours=i)).isoformat(),
                "value": round(p, 4) if p != 0.0 else 0.0001,
            }
            for i, p in enumerate(epex_fc)
        ]

        self.set_state(
            "sensor.forecast_epex",
            state=round(today_prices[cur_h], 5),
            attributes={
                "unit_of_measurement": "EUR/kWh",
                "friendly_name":       "EPEX uurprijs forecast",
                "forecast":            entries,
                "last_updated":        now.isoformat(),
            },
            replace=True,
        )
        self.log(f"[EmsForecasts] forecast_epex raw built: entries={n}")

    def _refresh_epex(self):
        """
        Lees sensor.forecast_epex, aligneer het window naar het huidige uur,
        haal past-prijzen op en voer de volledige analyse uit.

        Window-shift (entry[0] is verlopen uur):
          old_val = entry[0].value   → meest recente verlopen uurprijs
          entries = entries[idx:]    → trim naar huidig uur
          past    = 4 recorder-uren + old_val

        Geen shift nodig (entry[0] is al huidig uur, bijv. net na _build_epex_raw):
          past    = 5 recorder-uren
        """
        try:
            now     = self.datetime()
            cur_h   = now.hour
            today   = now.date()
            now_h13 = _dt.datetime(today.year, today.month, today.day, cur_h).isoformat()[:13]

            all_state = self.get_state("sensor.forecast_epex", attribute="all")
            if not all_state:
                return
            attrs   = dict(all_state.get("attributes", {}))
            entries = list(attrs.get("forecast", []))
            if not entries:
                return

            # ── Window alignment ──────────────────────────────────────────────
            if entries[0].get("start", "")[:13] != now_h13:
                idx = next(
                    (i for i, e in enumerate(entries)
                     if e.get("start", "")[:13] == now_h13),
                    None,
                )
                if idx is None:
                    self.log("[EmsForecasts] _refresh_epex: huidig uur niet in forecast",
                             level="WARNING")
                    return
                entries = entries[idx:]

            # ── Past prices uit recorder ──────────────────────────────────────
            past_prices = self._epex_past_prices(5)

            self.log(
                f"[EmsForecasts] _refresh_epex: "
                f"past={[round(p,4) for p in past_prices]}"
            )
            prices     = [float(e["value"]) for e in entries]
            ann, stats = enrich_hourly(prices, past_values=past_prices)

            for i, e in enumerate(entries):
                e["pct"]   = ann[i]["pct"]
                e["is_tp"] = ann[i]["is_tp"]
                e["label"] = ann[i]["label"]
                e["tier"]  = ann[i]["tier"] if ann[i]["tier"] != 0 else "0"  # str "0" voorkomt dat AppDaemon integer 0 weglaat

            cur_val = float(entries[0]["value"]) or 0.0001
            attrs["forecast"]     = entries
            attrs["horizon"]      = stats["horizon"]
            attrs["min"]          = stats["min"]
            attrs["max"]          = stats["max"]
            attrs["avg"]          = stats["avg"]
            attrs["median"]       = stats["median"]
            attrs["last_updated"] = now.isoformat()

            self.set_state(
                "sensor.forecast_epex",
                state=round(cur_val, 5),
                attributes=attrs,
                replace=True,
            )
            self.log(
                f"[EmsForecasts] forecast_epex refreshed: "
                f"{round(cur_val * 1000):.0f} EUR/MWh entries={len(entries)}"
            )
        except Exception as exc:
            self.log(f"[EmsForecasts._refresh_epex] {exc}", level="WARNING")


    def _epex_past_prices(self, n):
        """
        Return up to n past hourly EPEX prices from the HA recorder REST API,
        oldest-first.

        Voor elk doeluur H: zoek de laatste state-change vóór H+1:00.
        Gebruikt de REST API direct (AppDaemon's get_history is async en
        werkt niet betrouwbaar vanuit synchrone context).
        """
        now_local = self.datetime()
        start     = (now_local - _dt.timedelta(hours=n + 2)).isoformat()
        token     = os.environ.get("SUPERVISOR_TOKEN", "")

        try:
            resp = _requests.get(
                f"http://supervisor/core/api/history/period/{start}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                },
                params={
                    "filter_entity_id": "sensor.forecast_epex",
                    "minimal_response": "true",
                    "no_attributes":    "true",
                },
                timeout=5,
            )
            resp.raise_for_status()
            data   = resp.json()
            states = data[0] if data else []
        except Exception as exc:
            self.log(f"[EmsForecasts] _epex_past_prices REST failed: {exc}",
                     level="WARNING")
            return []

        changes = []
        for s in states:
            try:
                v  = float(s["state"])
                dt = _dt.datetime.fromisoformat(
                    s.get("last_changed", "").replace("Z", "+00:00")
                ).astimezone(now_local.tzinfo)
                changes.append((dt, v))
            except Exception:
                continue

        if not changes:
            return []
        changes.sort()

        # Zorg dat cur_slot dezelfde tzinfo heeft als de changes
        tz       = changes[0][0].tzinfo if changes else None
        cur_slot = now_local.replace(minute=0, second=0, microsecond=0)
        if tz is not None and cur_slot.tzinfo is None:
            import zoneinfo
            cur_slot = cur_slot.replace(tzinfo=zoneinfo.ZoneInfo("Europe/Amsterdam"))
        elif tz is None and cur_slot.tzinfo is not None:
            cur_slot = cur_slot.replace(tzinfo=None)

        result = []
        for offset in range(n, 0, -1):
            slot_end = cur_slot - _dt.timedelta(hours=offset - 1)
            val = next(
                (v for dt, v in reversed(changes) if dt < slot_end),
                None,
            )
            if val is not None:
                result.append(val)

        return result

    # ── Weather-based forecast sensors ───────────────────────────────────────

    def _publish_weather_forecasts(self, update_state=True):
        now      = self.datetime()
        today    = now.date()
        tomorrow = today + timedelta(days=1)
        cur_h    = now.hour
        start_dt = _dt.datetime(today.year, today.month, today.day, cur_h)
        n        = 36

        scale    = float(self.get_state(CALIB_ENTITY, attribute="solar_scale") or 1.0)
        t_target = self._sensor_float(HP_SETPOINT_ENTITY, 20.5)

        # Open-Meteo: flat 48h arrays (index 0..23 = today, 24..47 = tomorrow)
        irr_48h, temp_48h = self._get_openmeteo_forecast(today, tomorrow)
        if irr_48h is None:
            fc_entries = self._get_weather_entries()
            irr_today  = irr_from_weather_forecast(fc_entries, today,
                                                    scale=self._irr_scale)
            irr_tom    = irr_from_weather_forecast(fc_entries, tomorrow,
                                                    scale=self._irr_scale)
            temp_today = temp_from_weather_forecast(fc_entries, today)
            temp_tom   = temp_from_weather_forecast(fc_entries, tomorrow)
            irr_48h    = irr_today + irr_tom
            temp_48h   = temp_today + temp_tom
            irr_source = "weather.athome"
        else:
            irr_source = "open-meteo"

        # 36-hour windows from cur_h (may be shorter at late hours — irr_48h is 48h)
        irr_fc  = irr_48h[cur_h:cur_h + n]
        temp_fc = temp_48h[cur_h:cur_h + n]
        n       = len(irr_fc)   # actual available hours (≤ 36)

        # Bias-corrected inputs for model (published sensors stay raw)
        irr_fc_c  = [max(0.0, self._irr_bias_a * v + self._irr_bias_b) for v in irr_fc]
        temp_fc_c = [self._temp_bias_a * t + self._temp_bias_b for t in temp_fc]

        # ── Past 5 hours for window normalisation context ─────────────────────
        # Same model as future; irr_48h/temp_48h already contain full-day data.
        n_past    = min(5, cur_h)
        past_irr  = irr_48h[cur_h - n_past:cur_h]   # raw — matches irr_fc units
        past_temp = temp_48h[cur_h - n_past:cur_h]
        past_irr_c  = [max(0.0, self._irr_bias_a * v + self._irr_bias_b) for v in past_irr]
        past_temp_c = [self._temp_bias_a * t + self._temp_bias_b for t in past_temp]

        # PV: eta_h model when calibrated; fall back to solar_per_hour_irr
        # pv_eta stored ×1000 for readability; divide back here
        if any(v > 0.01 for v in self._pv_eta):
            pv_fc = [
                round(max(0.0, irr_fc_c[i] * self._pv_eta[(cur_h + i) % 24] / 1000), 4)
                for i in range(n)
            ]
            past_pv = [
                round(max(0.0, past_irr_c[i] * self._pv_eta[(cur_h - n_past + i) % 24] / 1000), 4)
                for i in range(n_past)
            ]
            pv_source = "eta"
        else:
            solar_today = sum(self._sensor_float(s) for s in SOLAR_SENSORS_TODAY)
            solar_tom   = sum(self._sensor_float(s) for s in SOLAR_SENSORS_TOMORROW)
            pv_today    = solar_per_hour_irr(solar_today, irr_48h[:24], scale)
            pv_tom      = solar_per_hour_irr(solar_tom,   irr_48h[24:48], scale)
            pv_fc       = (pv_today + pv_tom)[cur_h:cur_h + n]
            past_pv     = (pv_today + pv_tom)[cur_h - n_past:cur_h]
            pv_source   = "irr_scale"

        # Solar gain: gamma_h[hour] × irr / 1000  (gamma_h stored ×1000 for readability)
        # gamma_h implicitly encodes window orientation, incidence angle, shading.
        if any(v > 0.01 for v in self._gamma_h):
            solar_gain_nh = [
                round(max(0.0, irr_fc_c[i] * self._gamma_h[(cur_h + i) % 24] / 1000), 4)
                for i in range(n)
            ]
            past_gain = [
                max(0.0, past_irr_c[i] * self._gamma_h[(cur_h - n_past + i) % 24] / 1000)
                for i in range(n_past)
            ]
            gain_source = "gamma_h"
        else:
            # Not yet calibrated: assume no solar gain (conservative — avoids
            # applying the old g-value (0.6) which has different units than gamma_h)
            solar_gain_nh = [0.0] * n
            past_gain     = [0.0] * n_past
            gain_source   = "default"

        # HP: flat forecast with COP / thermal breakdown
        hp_elec, cop_fc, th_demand_fc, th_solar_fc = hp_hourly_forecast(
            self._k, self._beta_w,
            self._cop_a, self._cop_b, self._cop_c,
            t_target, temp_fc_c, solar_gain_nh=solar_gain_nh,
        )
        past_hp, _, past_th_demand, past_th_solar = hp_hourly_forecast(
            self._k, self._beta_w,
            self._cop_a, self._cop_b, self._cop_c,
            t_target, past_temp_c, solar_gain_nh=past_gain,
        )

        # Household baseline: repeat 24h pattern over the 36h window
        hh_fc   = [self._hh_base[(cur_h + i) % 24]          for i in range(n)]
        past_hh = [self._hh_base[(cur_h - n_past + i) % 24] for i in range(n_past)]

        # Thermal: net demand per hour = gross thermal demand minus solar gain
        net_thermal_fc = [max(0.0, round(th_demand_fc[i] - th_solar_fc[i], 3))
                          for i in range(n)]
        past_thermic   = [max(0.0, round(past_th_demand[i] - past_th_solar[i], 3))
                          for i in range(n_past)]

        # sensor.forecast_thermic – net thermal kWh; solar_gain as context
        # gross thermal demand is implicit: value + solar_gain
        thermic_entries = make_flat_forecast(net_thermal_fc, start_dt)
        for i, e in enumerate(thermic_entries):
            e["solar_gain"] = round(th_solar_fc[i], 4) or 0.0001

        # sensor.forecast_heatpump – electrical kWh derived from thermic / COP
        heatpump_entries = make_flat_forecast(hp_elec, start_dt)
        for i, e in enumerate(heatpump_entries):
            e["cop"] = cop_fc[i]

        self._publish_sensor(
            "sensor.forecast_outside_temp", "Buitentemperatuur forecast", "°C",
            temp_fc, start_dt, update_state=update_state,
            past_vals=past_temp,
        )
        self._publish_sensor(
            "sensor.forecast_irradiance", "Instraling forecast", "W/m2",
            irr_fc, start_dt,
            extra={"source": irr_source},
            update_state=update_state,
            past_vals=past_irr,
        )
        self._publish_sensor(
            "sensor.forecast_pv", "PV productie forecast", "kWh",
            pv_fc, start_dt, update_state=update_state,
            past_vals=past_pv,
        )
        self._publish_sensor(
            "sensor.forecast_thermic", "Thermische vraag forecast", "kWh",
            net_thermal_fc, start_dt,
            extra={"t_target": t_target},
            update_state=update_state,
            entries_override=thermic_entries,
            past_vals=past_thermic,
        )
        self._publish_sensor(
            "sensor.forecast_heatpump", "Warmtepomp verbruik forecast", "kWh",
            hp_elec, start_dt,
            extra={"t_target": t_target},
            update_state=update_state,
            entries_override=heatpump_entries,
            past_vals=past_hp,
        )
        self._publish_sensor(
            "sensor.forecast_household_energy", "Huishouden verbruik forecast", "kWh",
            hh_fc, start_dt, update_state=update_state,
            past_vals=past_hh,
        )
        self.log(
            f"[EmsForecasts] weather forecasts published "
            f"(irr={irr_source} pv={pv_source} gain={gain_source} "
            f"temp_now={temp_fc[0]:.1f}°C "
            f"irr_now={irr_fc[0]:.0f}W "
            f"pv_now={pv_fc[0]:.3f}kWh "
            f"solar_gain_now={solar_gain_nh[0]:.3f}kWh "
            f"thermic_now={net_thermal_fc[0]:.3f}kWh "
            f"hp_now={hp_elec[0]:.3f}kWh "
            f"hh_now={hh_fc[0]:.3f}kWh "
            f"cur_h={cur_h})"
        )

    def _publish_sensor(self, entity_id, name, unit, vals, start_dt,
                        extra=None, update_state=True, entries_override=None,
                        past_vals=None):
        """
        Publish a forecast sensor.

        vals           : list[float]  hourly values (index 0 = current hour)
        start_dt       : datetime     timestamp of vals[0]
        entries_override: pre-built list of dicts (used for thermic/heatpump with extra fields)
        past_vals      : list[float]  up to 5 model values preceding vals[0],
                         oldest-first.  Extends the analysis window backwards so
                         the current hour is no longer on the window boundary.
        """
        entries = (entries_override if entries_override is not None
                   else make_flat_forecast(vals, start_dt))

        # Window stats + per-entry pct; past context shifts current hour off boundary
        stats, pcts = forecast_window_stats(vals, past_values=past_vals)
        for i, e in enumerate(entries):
            e["pct"] = pcts[i]

        cur_val = (vals[0] if vals else 0.0) or 0.0001

        attrs = {
            "unit_of_measurement": unit,
            "friendly_name":       name,
            "forecast":            entries,
            "horizon":             stats["horizon"],
            "min":                 stats["min"],
            "max":                 stats["max"],
            "avg":                 stats["avg"],
            "median":              stats["median"],
            "last_updated":        start_dt.isoformat(),
        }
        if extra:
            attrs.update(extra)

        if update_state:
            state_val = cur_val
        else:
            cur = self.get_state(entity_id)
            if cur not in (None, "unknown", "unavailable"):
                state_val = round(float(cur), 5)
            else:
                state_val = cur_val

        self.set_state(entity_id, state=state_val, attributes=attrs, replace=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_openmeteo_forecast(self, today, tomorrow):
        """
        Fetch GHI, DHI, DNI and temperature from Open-Meteo.

        Returns (irr_48h, temp_48h) where each list has 48 elements:
        index 0..23 = today hours, 24..47 = tomorrow hours.
        Returns (None, None) on failure.  Cached for 1 hour.
        """
        now = self.datetime()
        if (hasattr(self, "_om_cache_time")
                and (now - self._om_cache_time).total_seconds() < 3600
                and hasattr(self, "_om_cache")):
            return self._om_cache

        try:
            resp = _requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude":      LATITUDE,
                    "longitude":     LONGITUDE,
                    "hourly":        "shortwave_radiation,temperature_2m",
                    "forecast_days": 3,
                    "timezone":      "Europe/Amsterdam",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data  = resp.json()
            times = data["hourly"]["time"]
            rads  = data["hourly"]["shortwave_radiation"]
            temps = data["hourly"]["temperature_2m"]

            irr_48h  = [0.0]  * 48
            temp_48h = [10.0] * 48
            today_s  = today.isoformat()
            tom_s    = tomorrow.isoformat()

            for t, r, tmp in zip(times, rads, temps):
                d = t[:10]
                h = int(t[11:13])
                if d == today_s:
                    irr_48h[h]  = float(r   or 0.0)
                    temp_48h[h] = float(tmp or 10.0)
                elif d == tom_s:
                    irr_48h[24 + h]  = float(r   or 0.0)
                    temp_48h[24 + h] = float(tmp or 10.0)

            result = (irr_48h, temp_48h)
            self._om_cache      = result
            self._om_cache_time = now
            self.log(
                f"[EmsForecasts] Open-Meteo: "
                f"today_peak={max(irr_48h[:24]):.0f}W {temp_48h[12]:.1f}°C "
                f"tom_peak={max(irr_48h[24:48]):.0f}W {temp_48h[36]:.1f}°C"
            )
            return result

        except Exception as exc:
            self.log(f"[EmsForecasts] Open-Meteo failed: {exc}", level="WARNING")
            return None, None

    def _get_weather_entries(self):
        """Fetch hourly weather forecast from weather.athome."""
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if token:
            try:
                resp = _requests.post(
                    "http://supervisor/core/api/services/weather/get_forecasts"
                    "?return_response",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"entity_id": WEATHER_ENTITY, "type": "hourly"},
                    timeout=10,
                )
                if resp.ok:
                    body    = resp.json()
                    entries = (
                        body.get("service_response", {})
                            .get(WEATHER_ENTITY, {})
                            .get("forecast", [])
                        if isinstance(body, dict) else []
                    )
                    if entries:
                        return entries
            except Exception as exc:
                self.log(
                    f"[EmsForecasts] REST weather/get_forecasts failed: {exc}",
                    level="WARNING",
                )

        try:
            result  = self.call_service(
                "weather/get_forecasts",
                entity_id=WEATHER_ENTITY,
                type="hourly",
                return_response=True,
            )
            entries = (result or {}).get(WEATHER_ENTITY, {}).get("forecast", [])
            if entries:
                return entries
        except Exception as exc:
            self.log(
                f"[EmsForecasts] weather/get_forecasts failed: {exc}",
                level="WARNING",
            )

        try:
            entries = self.get_state(WEATHER_ENTITY, attribute="forecast")
            if entries:
                return entries
        except Exception:
            pass

        self.log(
            "[EmsForecasts] No weather forecast available, using defaults",
            level="WARNING",
        )
        return []

    def _sensor_float(self, entity_id, default=0.0):
        try:
            return float(self.get_state(entity_id) or default)
        except (TypeError, ValueError):
            return default
