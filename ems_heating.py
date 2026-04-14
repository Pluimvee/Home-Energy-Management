"""
ems_heating.py  –  Heat-pump thermal model + calibration helpers
================================================================
Pure Python (no AppDaemon / Home Assistant imports).

Responsibilities
----------------
  hp_hourly_forecast()  Flat n-hour HP forecast with COP / thermal breakdown
                        – used by ems_forecasts

Physical model
--------------
  solar_gain(h)   = gamma_h[h] × irr(h)                    (kWh/h, from POA geometry)
  thermal_demand  = max(0, k × (T_setpoint − T_outdoor))   (kWh/h, before solar)
  thermal_net     = max(0, thermal_demand − solar_gain)     (kWh/h, after solar gain)
  thermal_solar   = thermal_demand − thermal_net            (kWh/h, solar reduction)
  hp_electric     = thermal_net / COP(T)

  k   : heat-demand coefficient  [kWh / °C / h]
  COP : cop_a + cop_b·T + cop_c·T²
"""

import statistics
import datetime
from collections import defaultdict

from ems_base import ams_offset as _ams_offset, parse_utc_dt as _parse_utc

# ── Constants ──────────────────────────────────────────────────────────────────

SLOTS_PER_DAY = 96
SLOT_H        = 0.25      # 15-min slot in hours

# Calibration thresholds
HP_ACTIVE_THR = 0.05   # kWh/h – hours below this are treated as standby
MIN_ACTIVE    = 12     # minimum active hours for reliable calibration

# Safe fallback values
K_DEFAULT     = 0.05   # kWh / °C / h
GAMMA_DEFAULT = 0.6    # solar heat gain coefficient (g-value of glass, 0–1)


# ── Public API ─────────────────────────────────────────────────────────────────

def hp_hourly_forecast(k, cop_a, cop_b, cop_c, t_target,
                       temp_nh, solar_gain_nh=None):
    """
    Compute a flat n-hour HP forecast from temperature and solar gain arrays.

    Parameters
    ----------
    k                   : heat-demand coefficient [kWh / °C / h]
    cop_a, cop_b, cop_c : COP quadratic model coefficients
    t_target            : thermostat setpoint (°C)
    temp_nh             : list[float]  n hourly outdoor temperatures (°C)
    solar_gain_nh       : list[float] or None
                          pre-computed solar gain (kWh/h) from gamma_h model.
                          Defaults to zero (no solar gain) when None.

    Returns four n-element lists
    ----------------------------
    electric[n]       kWh/h electric consumption
    cop[n]            COP per hour
    thermal_demand[n] kWh/h raw house heat demand (before solar gain)
    thermal_solar[n]  kWh/h heat demand reduced by solar gain
    """
    electric       = []
    cop_out        = []
    thermal_demand = []
    thermal_solar  = []

    for i, t in enumerate(temp_nh):
        demand_no_solar = max(0.0, k * (t_target - t))
        solar_gain      = solar_gain_nh[i] if solar_gain_nh and i < len(solar_gain_nh) else 0.0
        thermal_net     = max(0.0, demand_no_solar - solar_gain)
        solar_red       = demand_no_solar - thermal_net

        cop  = max(2.7, min(cop_a + cop_b * t + cop_c * t * t, 7.0))
        elec = round(thermal_net / cop, 4)

        electric.append(elec)
        cop_out.append(round(cop, 3))
        thermal_demand.append(round(demand_no_solar, 4))
        thermal_solar.append(round(solar_red, 4))

    return electric, cop_out, thermal_demand, thermal_solar


def slots_to_hourly(slots_96):
    """Sum 96-slot array into 24 hourly values."""
    return [round(sum(slots_96[h * 4:(h + 1) * 4]), 4) for h in range(24)]


# ── History helpers ────────────────────────────────────────────────────────────

def history_to_hourly_cumul_change(states, tz_offset=None):
    """
    Convert AppDaemon raw-history states for a *total_increasing* (cumulative)
    sensor into per-hour consumption tuples (local_date, local_hour, kwh_change).
    """
    parsed = []
    for s in states:
        try:
            v  = float(s["state"])
            dt = _parse_utc(s["last_changed"])
            parsed.append((dt, v))
        except Exception:
            continue

    if len(parsed) < 2:
        return []

    parsed.sort(key=lambda x: x[0])

    by_hour = defaultdict(list)
    for dt_utc, v in parsed:
        tz    = tz_offset if tz_offset is not None else _ams_offset(dt_utc.date())
        local = dt_utc + datetime.timedelta(hours=tz)
        key   = (local.date(), local.hour)
        by_hour[key].append(v)

    result = []
    keys   = sorted(by_hour)
    for i in range(1, len(keys)):
        prev_k, curr_k = keys[i - 1], keys[i]
        prev_dt = datetime.datetime.combine(prev_k[0], datetime.time(prev_k[1]))
        curr_dt = datetime.datetime.combine(curr_k[0], datetime.time(curr_k[1]))
        if (curr_dt - prev_dt).total_seconds() > 7200:
            continue
        change = max(0.0, by_hour[curr_k][-1] - by_hour[prev_k][-1])
        result.append((curr_k[0], curr_k[1], round(change, 4)))

    return result


def history_to_hourly_mean(states, tz_offset=None):
    """
    Convert AppDaemon raw-history states for a *measurement* sensor into
    per-hour mean-value tuples (local_date, local_hour, mean_value).
    """
    by_hour = defaultdict(list)
    for s in states:
        try:
            v     = float(s["state"])
            dt    = _parse_utc(s["last_changed"])
            tz    = tz_offset if tz_offset is not None else _ams_offset(dt.date())
            local = dt + datetime.timedelta(hours=tz)
            key   = (local.date(), local.hour)
            by_hour[key].append(v)
        except Exception:
            continue

    return [
        (date, h, round(sum(vs) / len(vs), 3))
        for (date, h), vs in sorted(by_hour.items())
        if vs
    ]
