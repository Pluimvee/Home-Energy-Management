"""
ems_heating.py  –  Heat-pump thermal model + calibration helpers
================================================================
Pure Python (no AppDaemon / Home Assistant imports).

Responsibilities
----------------
  calibrate()           Auto-fit k and β from recent HP history
  hp_per_slot()         96-slot (15-min) HP forecast – used by ems_planner
  hp_hourly_forecast()  Flat n-hour HP forecast with COP / thermal breakdown
                        – used by ems_forecasts

Physical model
--------------
  solar_gain(h)   = β × irr(h) × SOLAR_GAIN_WEIGHTS[h]    (kWh/h)
  thermal_demand  = max(0, k × (T_setpoint − T_outdoor))   (kWh/h, before solar)
  thermal_net     = max(0, thermal_demand − solar_gain)     (kWh/h, after solar gain)
  thermal_solar   = thermal_demand − thermal_net            (kWh/h, solar reduction)
  hp_electric     = thermal_net / COP(T)

  k   : heat-demand coefficient  [kWh / °C / h]
  β   : solar-gain coefficient   [kWh / (W/m²·h)]  ≈ effective window area (m²) / 1000
  COP : cop_a + cop_b·T + cop_c·T²
"""

import math
import statistics
import datetime
from collections import defaultdict

from ems_base import ams_offset as _ams_offset, parse_utc_dt as _parse_utc

# ── Constants ──────────────────────────────────────────────────────────────────

SLOTS_PER_DAY = 96
SLOT_H        = 0.25      # 15-min slot in hours

I0_CLEAR = 900.0          # W/m², peak clear-sky irradiance (used by irr_scale_from_history)

# Calibration thresholds
HP_ACTIVE_THR = 0.05   # kWh/h – hours below this are treated as standby
MIN_ACTIVE    = 12     # minimum active hours for reliable calibration
MIN_SOLAR     = 5      # minimum solar-active hours for β calibration

# Safe fallback values
K_DEFAULT     = 0.05   # kWh / °C / h
BETA_DEFAULT  = 0.0    # no solar correction (conservative, beta model only)
GAMMA_DEFAULT = 0.6    # solar heat gain coefficient (g-value of glass, 0–1)

# Time-of-day solar-gain weight profile.
# Gaussian centred at SOLAR_PEAK_H: east-south-facing windows receive
# effective solar gain mainly in the morning (≈10:30 peak).
SOLAR_PEAK_H = 10.5
SOLAR_SIGMA  =  2.0

SOLAR_GAIN_WEIGHTS = [
    round(math.exp(-0.5 * ((h - SOLAR_PEAK_H) / SOLAR_SIGMA) ** 2), 4)
    for h in range(24)
]


# ── Public API ─────────────────────────────────────────────────────────────────

def irr_scale_from_history(hourly_irr, percentile=0.95):
    """
    Compute a scale factor so that clear_sky_w() predictions match the
    irradiance source used for calibration.

    Parameters
    ----------
    hourly_irr  : list[(date, hour, mean_W)]   from history_to_hourly_mean
    percentile  : float  fraction to use as "peak" (default 0.95)

    Returns
    -------
    float   scale factor in (0.05, 1.5)
    """
    values = sorted(v for _, _, v in hourly_irr if v > 10)
    if len(values) < 5:
        return 1.0
    idx = max(0, int(len(values) * percentile) - 1)
    return max(0.05, min(values[idx] / I0_CLEAR, 1.5))


def calibrate(hourly_hp, hourly_temp, hourly_irr, t_target):
    """
    Auto-calibrate *k* and *β* from parallel hourly data lists.

    Parameters
    ----------
    hourly_hp   : list[(date, hour, kwh_change)]
    hourly_temp : list[(date, hour, mean_degC)]
    hourly_irr  : list[(date, hour, mean_W)]
    t_target    : float   HP thermostat setpoint (°C)

    Returns
    -------
    (k, beta_W, info)
    """
    hp_lup  = {(d, h): v for d, h, v in hourly_hp}
    tmp_lup = {(d, h): v for d, h, v in hourly_temp}
    irr_lup = {(d, h): v for d, h, v in hourly_irr}

    active = []
    for key in hp_lup:
        hp    = hp_lup[key]
        t     = tmp_lup.get(key)
        irr   = irr_lup.get(key, 0.0)
        if t is None:
            continue
        delta_t = t_target - t
        if hp >= HP_ACTIVE_THR and delta_t > 0.5:
            active.append((hp, t, irr, delta_t))

    n_active = len(active)

    if n_active < MIN_ACTIVE:
        return K_DEFAULT, BETA_DEFAULT, {
            "k": K_DEFAULT, "beta_W": BETA_DEFAULT,
            "active_hours": n_active, "source": "default",
        }

    # Step 1: first-pass k (ignoring solar gain)
    k_vals = [hp / dt for hp, _, _, dt in active if dt > 0]
    k      = max(0.01, min(statistics.median(k_vals), 0.30))

    # Step 2: β from solar-active residuals
    solar_hrs = [(hp, irr, dt) for hp, _, irr, dt in active if irr > 50]
    n_solar   = len(solar_hrs)
    beta_W    = BETA_DEFAULT

    if n_solar >= MIN_SOLAR:
        beta_vals = []
        for hp, irr, dt in solar_hrs:
            residual = hp - k * dt   # negative when solar reduces demand
            if irr > 0:
                beta_est = -residual / irr   # kWh / (W/m²·h) ≈ window area / 1000
                beta_vals.append(beta_est)
        if beta_vals:
            beta_W = max(0.0, min(statistics.median(beta_vals), 0.03))

    return k, beta_W, {
        "k":            round(k, 5),
        "beta_W":       round(beta_W, 6),
        "active_hours": n_active,
        "solar_hours":  n_solar,
        "source":       "calibrated",
    }


def hp_per_slot(k, beta_W, t_target, forecast_temp_24h, forecast_irr_24h):
    """
    Generate 96-slot (15-min) HP consumption forecast (kWh per slot).
    Used by ems_planner for 24-hour dispatch planning.

    Parameters
    ----------
    k, beta_W           : calibrated model coefficients
    t_target            : thermostat setpoint (°C)
    forecast_temp_24h   : list[float]  24 hourly temperatures (°C)
    forecast_irr_24h    : list[float]  24 hourly irradiance estimates (W)

    Returns
    -------
    list[float]  96 values (kWh per 15-min slot)
    """
    result = []
    for h in range(24):
        t_out         = forecast_temp_24h[h] if h < len(forecast_temp_24h) else 10.0
        irr           = forecast_irr_24h[h]  if h < len(forecast_irr_24h)  else 0.0
        effective_irr = irr * SOLAR_GAIN_WEIGHTS[h]
        demand        = max(0.0, k * (t_target - t_out))
        solar_gain    = beta_W * effective_irr
        thermal_net   = max(0.0, demand - solar_gain)
        result.extend([round(thermal_net * SLOT_H, 4)] * 4)
    return result


def hp_hourly_forecast(k, beta_W, cop_a, cop_b, cop_c, t_target,
                       temp_nh, irr_nh=None, start_h=0, solar_gain_nh=None):
    """
    Compute a flat n-hour HP forecast from temperature and solar gain arrays.

    Parameters
    ----------
    k, beta_W           : calibrated heat-demand / solar-gain coefficients
                          (beta_W only used when solar_gain_nh is None)
    cop_a, cop_b, cop_c : COP quadratic model coefficients
    t_target            : thermostat setpoint (°C)
    temp_nh             : list[float]  n hourly outdoor temperatures (°C)
    irr_nh              : list[float] or None
                          n hourly irradiance values (W); only used when
                          solar_gain_nh is None (beta fallback model)
    start_h             : int  hour-of-day of temp_nh[0]
                          (needed for SOLAR_GAIN_WEIGHTS when using beta model)
    solar_gain_nh       : list[float] or None
                          pre-computed solar gain (kWh/h) from POA geometry model.
                          When provided, overrides the beta_W × irr model.

    Returns four n-element lists
    ----------------------------
    electric[n]       kWh/h electric consumption
    cop[n]            COP per hour
    thermal_demand[n] kWh/h raw house heat demand (before solar gain)
    thermal_solar[n]  kWh/h heat demand reduced by solar gain
    """
    if irr_nh is None:
        irr_nh = [0.0] * len(temp_nh)

    electric       = []
    cop_out        = []
    thermal_demand = []
    thermal_solar  = []

    for i, (t, irr) in enumerate(zip(temp_nh, irr_nh)):
        demand_no_solar = max(0.0, k * (t_target - t))

        if solar_gain_nh is not None:
            solar_gain = solar_gain_nh[i] if i < len(solar_gain_nh) else 0.0
        else:
            h_of_day   = (start_h + i) % 24
            eff_irr    = irr * SOLAR_GAIN_WEIGHTS[h_of_day]
            solar_gain = beta_W * eff_irr

        thermal_net = max(0.0, demand_no_solar - solar_gain)
        solar_red   = demand_no_solar - thermal_net

        cop  = max(1.5, cop_a + cop_b * t + cop_c * t * t)
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
