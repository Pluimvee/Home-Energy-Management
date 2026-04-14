"""
ems_base.py  –  Shared constants and pure data helpers
=========================================================
No AppDaemon or Home Assistant imports.  Standard library only.

All energy planning modules import from here so that sensor IDs and
conversion logic live in exactly one place.
"""

import math
import statistics as _statistics
import datetime as _dt
from datetime import datetime, timedelta

# ── Nordpool ──────────────────────────────────────────────────────────────────

NORDPOOL_ENTITY = "sensor.nordpool_electricity_market_price"

# ── Outdoor temperature  (first available sensor wins) ────────────────────────

TEMP_SENSORS = [
    "sensor.outsidetemp_outside_temperature",   # primary
    "sensor.smartcontrol_outside",              # backup
]

# ── Heat-pump electricity  (cumulative kWh counter) ───────────────────────────

HP_ENERGY_SENSORS = [
    "sensor.heatpump_energy",                   # primary
    "sensor.kamstrup_warmtepomp_energy_input",  # backup
]

# ── Heat-pump daily counter ───────────────────────────────────────────────────

HP_ENERGY_TODAY_SENSORS = [
    "sensor.heatpump_energy_today",
]

# ── Solar production forecast ─────────────────────────────────────────────────

SOLAR_SENSORS_TODAY = [
    "sensor.energy_production_today",
    "sensor.energy_production_today_2",
    "sensor.energy_production_today_3",
]

SOLAR_SENSORS_TOMORROW = [
    "sensor.energy_production_tomorrow",
    "sensor.energy_production_tomorrow_2",
    "sensor.energy_production_tomorrow_3",
]

# ── Solar irradiance (live measurement – calibration only) ────────────────────

IRRADIANCE_SENSOR = "sensor.irradiance"

# ── Heat-pump setpoint ────────────────────────────────────────────────────────

HP_SETPOINT_ENTITY = "number.smartcontrol_target"

# ── Location (for Open-Meteo irradiance forecast) ─────────────────────────────

LATITUDE  = 52.488441   # degrees north
LONGITUDE =  4.756142   # degrees east

ALBEDO_GROUND = 0.2   # ground reflectance (typical grass/pavement)

# Window solar-gain panel geometry (matches forecast.solar 'Zonnewarmte' configs).
# Each tuple: (tilt_deg, az_deg) — tilt from horizontal, azimuth from north clockwise.
WINDOW_PANELS = [
    (90, 126),   # Achterpui – SE-facing
    (90, 216),   # Zijpui    – SSW-facing
]


# ── Nordpool conversion ───────────────────────────────────────────────────────

def nordpool_to_hourly(raw_entries):
    """
    Convert Nordpool raw_today / raw_tomorrow attribute
    (list of dicts with 'start' and 'value') to an hourly list of
    average prices (EUR/kWh).

    Handles both 15-minute slot format (any multiple of 4, e.g. 92 on a DST
    spring-forward day) and hourly format (any count between 23 and 25).
    Returns [] when raw_entries is falsy.
    Raises ValueError for unexpected lengths.
    """
    if not raw_entries:
        return []
    prices = [float(e["value"]) for e in raw_entries]
    n = len(prices)
    if n % 4 == 0:
        # 15-minute slots: 92 (23 h DST), 96 (24 h normal), 100 (25 h DST fall)
        hours = n // 4
        return [round(sum(prices[h * 4:(h + 1) * 4]) / 4, 6) for h in range(hours)]
    if 23 <= n <= 25:
        # Hourly prices for DST or normal days
        return [round(p, 6) for p in prices]
    raise ValueError(
        f"nordpool_to_hourly: unexpected entry count {n} "
        f"(expected multiple of 4 or 23–25)"
    )


# ── Hourly entry lists ────────────────────────────────────────────────────────

def make_entries(values_24h, date):
    """
    Build a 24-element list of dicts:
        { "start": "2026-03-24T08:00:00", "value": <float> }

    'start' is the local datetime at the beginning of that hour.
    All forecast sensors use this uniform format so that dashboard
    data_generators can always use:
        new Date(item["start"]).getTime()  and  item.value

    Parameters
    ----------
    values_24h : list[float]   24 values, index 0 = 00:00
    date       : datetime.date
    """
    return [
        {
            "start": datetime(date.year, date.month, date.day, h, 0, 0).isoformat(),
            "value": values_24h[h] if values_24h[h] != 0.0 else 0.0001,
        }
        for h in range(24)
    ]


def make_forecast(today_vals, today_date, from_hour,
                  tom_vals=None, tom_date=None):
    """
    Flat list of {start, value} from *from_hour* of today through
    end of tomorrow (when provided).  Used by all forecast sensors.

    Parameters
    ----------
    today_vals : list[float]        24 values indexed by hour
    today_date : datetime.date
    from_hour  : int                first hour to include (current hour)
    tom_vals   : list[float] | None
    tom_date   : datetime.date | None
    """
    entries = []
    for h in range(from_hour, 24):
        val = today_vals[h]
        entries.append({
            "start": datetime(today_date.year, today_date.month,
                              today_date.day, h).isoformat(),
            "value": val if val != 0.0 else 0.0001,
        })
    if tom_vals is not None and tom_date is not None:
        for h in range(24):
            val = tom_vals[h]
            entries.append({
                "start": datetime(tom_date.year, tom_date.month,
                                  tom_date.day, h).isoformat(),
                "value": val if val != 0.0 else 0.0001,
            })
    return entries


def current_hour_value(entries_today, hour):
    """
    Return the forecasted value for *hour* from a today-entries list
    produced by make_entries().  Falls back to the last entry when
    hour is out of range (end of day edge case).
    """
    if not entries_today:
        return 0.0
    h = min(hour, len(entries_today) - 1)
    return entries_today[h].get("value", 0.0)




# ── Household energy component sensors ───────────────────────────────────────
#
# Each entry: (sensor_id, sign)
#   +1  = the sensor adds to household consumption
#   -1  = the sensor is a sub-metered load that must be subtracted
#
# Together they represent:
#   hh[h] = Σ(sign × Δsensor[h])
#
# Used in:
#   energy_calibration.py  – to derive per-hour-of-day medians (hh_h00..h23)
#   energy_sensors.py      – to publish sensor.forecast_household_energy
#
HH_ENERGY_SOURCES = [
    # ── Sources (net grid import + all PV produced) ───────────────────────────
    ("sensor.p1_meter_electricity_consumed",               +1),  # grid import (kWh)
    ("sensor.pv_total_energy",                             +1),  # total PV production (kWh)
    ("sensor.solis_s6_eh3p_total_energy_consumption",      +1),  # inverter DC load (kWh)
    # ── Sub-metered loads (remove from household) ─────────────────────────────
    ("sensor.solis_s6_eh3p_total_battery_charge_energy",   -1),  # battery charge (kWh)
    ("sensor.ev_charger_energy",                           -1),  # EV charger (kWh)
    ("sensor.heatpump_energy",                             -1),  # heat pump compressor (kWh)
    ("sensor.heatpump_control_energy",                     -1),  # HP controller + pump (kWh)
    ("sensor.warmtepompboiler_energy",                     -1),  # heat-pump boiler (kWh)
]

# ── HP slot-to-hour conversion ────────────────────────────────────────────────

def hp_slots_to_hourly(slots_96):
    """Sum a 96-slot (15-min) HP array into 24 hourly kWh/h values."""
    return [round(sum(slots_96[h * 4:(h + 1) * 4]), 3) for h in range(24)]


# ── Datetime helpers (shared by ems_heating and ems_forecasts) ────────────────

def ams_offset(date):
    """UTC offset for Amsterdam: CET=1 (winter), CEST=2 (summer)."""
    def _last_sunday(year, month):
        d = _dt.date(year, month + 1, 1) - _dt.timedelta(days=1)
        while d.weekday() != 6:
            d -= _dt.timedelta(days=1)
        return d
    cest_start = _last_sunday(date.year, 3)
    cest_end   = _last_sunday(date.year, 10)
    return 2 if cest_start <= date < cest_end else 1


def parse_utc_dt(s):
    """
    Parse an ISO datetime string or a timezone-aware datetime object and
    return a UTC-normalised naive datetime.
    """
    if isinstance(s, _dt.datetime):
        if s.tzinfo is not None:
            return (s - s.utcoffset()).replace(tzinfo=None)
        return s
    s = s.strip()
    if s.endswith("Z"):
        return _dt.datetime.fromisoformat(s[:-1])
    for i in range(len(s) - 1, 9, -1):
        if s[i] in "+-":
            sign   = 1 if s[i] == "+" else -1
            parts  = s[i + 1:].split(":")
            offset = int(parts[0]) + (int(parts[1]) if len(parts) > 1 else 0) / 60.0
            dt     = _dt.datetime.fromisoformat(s[:i])
            return dt - _dt.timedelta(hours=sign * offset)
    return _dt.datetime.fromisoformat(s[:19])


# ── Irradiance transposition (POA model) ─────────────────────────────────────

def solar_position(date_or_doy, hour, lat_deg=LATITUDE):
    """
    Compute sun elevation and azimuth for a given date and hour.

    Uses the standard solar elevation formula (no atmospheric refraction or
    equation-of-time correction).  Accuracy ≈ 1° for European latitudes.

    Parameters
    ----------
    date_or_doy : datetime.date or int   date or day-of-year (1–365)
    hour        : int                    local solar hour (0–23)
    lat_deg     : float                  latitude in degrees north

    Returns
    -------
    (elevation_deg, azimuth_deg)
        elevation_deg : float  sun elevation above horizon (negative = below)
        azimuth_deg   : float  degrees from north, clockwise (0=N,90=E,180=S,270=W)
    """
    doy  = (date_or_doy if isinstance(date_or_doy, int)
            else date_or_doy.timetuple().tm_yday)
    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (doy - 80))))
    lat  = math.radians(lat_deg)
    ha   = math.radians(15 * (hour - 12))   # negative = morning, positive = afternoon

    sin_el = (math.sin(lat) * math.sin(decl)
              + math.cos(lat) * math.cos(decl) * math.cos(ha))
    el     = math.asin(max(-1.0, min(1.0, sin_el)))
    el_deg = math.degrees(el)

    if el_deg <= 0:
        return el_deg, 180.0   # below horizon; azimuth undefined → default south

    cos_el  = math.cos(el)
    cos_az_s = ((math.sin(decl) - sin_el * math.sin(lat))
                / (cos_el * math.cos(lat) + 1e-9))
    az_from_s = math.degrees(math.acos(max(-1.0, min(1.0, cos_az_s))))
    # ha > 0 → afternoon → sun is west of south
    az_from_s_pos_west = az_from_s if ha >= 0 else -az_from_s
    # Convert to from-north, clockwise (0=N, 90=E, 180=S, 270=W)
    az_from_n_cw = (180.0 + az_from_s_pos_west) % 360.0

    return el_deg, az_from_n_cw


def irr_on_plane(dni, dhi, ghi, el_deg, az_sun_deg,
                 tilt_deg, az_panel_deg, albedo=ALBEDO_GROUND):
    """
    Compute plane-of-array (POA) irradiance on a tilted surface.

    Uses the isotropic sky diffuse model (Liu–Jordan 1963).

    Parameters
    ----------
    dni          : float  Direct Normal Irradiance (W/m²)
    dhi          : float  Diffuse Horizontal Irradiance (W/m²)
    ghi          : float  Global Horizontal Irradiance (W/m²)
    el_deg       : float  Sun elevation angle (degrees)
    az_sun_deg   : float  Sun azimuth from north, clockwise (degrees)
    tilt_deg     : float  Surface tilt from horizontal (0=flat, 90=vertical)
    az_panel_deg : float  Surface azimuth from north, clockwise (degrees)
    albedo       : float  Ground reflectance (default 0.2)

    Returns
    -------
    float   POA irradiance (W/m²), ≥ 0
    """
    if el_deg <= 0:
        return 0.0

    tlt     = math.radians(tilt_deg)
    az_diff = math.radians(az_sun_deg - az_panel_deg)
    el      = math.radians(el_deg)

    cos_aoi   = (math.sin(el) * math.cos(tlt)
                 + math.cos(el) * math.cos(az_diff) * math.sin(tlt))
    beam      = max(0.0, dni * cos_aoi)
    diffuse   = dhi * (1.0 + math.cos(tlt)) / 2.0
    reflected = ghi * albedo * (1.0 - math.cos(tlt)) / 2.0

    return max(0.0, beam + diffuse + reflected)


def ghi_to_dni_dhi(ghi_nh, start_dt, lat_deg=LATITUDE):
    """
    Estimate DNI and DHI from GHI only using the Erbs decomposition model.

    Used as a fallback when Open-Meteo DNI/DHI data is unavailable.

    Parameters
    ----------
    ghi_nh   : list[float]  Global Horizontal Irradiance (W/m²) per hour
    start_dt : datetime     Timestamp of index 0 (local time)
    lat_deg  : float        Latitude in degrees north

    Returns
    -------
    (dhi_nh, dni_nh)  two lists of float, same length as ghi_nh
    """
    I0_solar = 1361.0   # solar constant (W/m²)
    dhi_nh   = []
    dni_nh   = []
    base_doy = start_dt.timetuple().tm_yday

    for i, ghi in enumerate(ghi_nh):
        total_h  = start_dt.hour + i
        h_of_day = total_h % 24
        doy      = base_doy + total_h // 24

        el, _  = solar_position(doy, h_of_day, lat_deg)
        sin_el = math.sin(math.radians(max(0.0, el)))
        I0_h   = I0_solar * sin_el   # extraterrestrial horizontal irradiance

        if I0_h < 1.0 or ghi < 1.0:
            dhi_nh.append(0.0)
            dni_nh.append(0.0)
            continue

        kt = min(1.0, ghi / I0_h)   # clearness index
        # Erbs decomposition (Erbs et al. 1982)
        if kt <= 0.22:
            df = 1.0 - 0.09 * kt
        elif kt <= 0.80:
            df = (0.9511 - 0.1604 * kt + 4.388 * kt ** 2
                  - 16.638 * kt ** 3 + 12.336 * kt ** 4)
        else:
            df = 0.165
        dhi = round(ghi * df, 1)
        dni = round(max(0.0, min((ghi - dhi) / max(sin_el, 0.01), 1000.0)), 1)
        dhi_nh.append(dhi)
        dni_nh.append(dni)

    return dhi_nh, dni_nh


def panel_kwh_forecast(ghi_nh, dhi_nh, dni_nh, start_dt,
                       tilt_deg, az_panel_deg, kwp=1.0,
                       lat_deg=LATITUDE):
    """
    Compute hourly panel output (kWh/h) for any tilted surface.

    Applies isotropic sky POA transposition to convert horizontal irradiance
    to plane-of-array irradiance, then scales to kWp.

    Suitable for PV panels (tilt = actual inclination, kwp = rated capacity)
    and windows (tilt = 90°, kwp = 1.0 — caller scales by gamma).

    Parameters
    ----------
    ghi_nh       : list[float]  Global Horizontal Irradiance (W/m²) per hour
    dhi_nh       : list[float]  Diffuse Horizontal Irradiance (W/m²) per hour
    dni_nh       : list[float]  Direct Normal Irradiance (W/m²) per hour
    start_dt     : datetime     Timestamp of index 0 (local time)
    tilt_deg     : float        Surface tilt from horizontal (0=flat, 90=vertical)
    az_panel_deg : float        Surface azimuth from north, clockwise (degrees)
    kwp          : float        Capacity in kWp (1 kWp → 1 kWh/h at 1000 W/m² POA)
    lat_deg      : float        Latitude in degrees north

    Returns
    -------
    list[float]  kWh/h per hour, same length as ghi_nh
    """
    result   = []
    base_doy = start_dt.timetuple().tm_yday

    for i, (ghi, dhi, dni) in enumerate(zip(ghi_nh, dhi_nh, dni_nh)):
        total_h  = start_dt.hour + i
        h_of_day = total_h % 24
        doy      = base_doy + total_h // 24

        el, az = solar_position(doy, h_of_day, lat_deg)
        poa    = irr_on_plane(dni, dhi, ghi, el, az, tilt_deg, az_panel_deg)
        result.append(round(poa * kwp / 1000.0, 4))

    return result


# ── Flat forecast builder ─────────────────────────────────────────────────────

def make_flat_forecast(vals, start_dt):
    """
    Build a flat list of {start, value} dicts from a values array.

    vals     : list[float]   hourly values
    start_dt : datetime      timestamp of vals[0]

    Returns list of {start: ISO string, value: float}.
    """
    return [
        {
            "start": (start_dt + timedelta(hours=i)).isoformat(),
            "value": v if v != 0.0 else 0.0001,
        }
        for i, v in enumerate(vals)
    ]

# ── Generic forecast window statistics ───────────────────────────────────────

def forecast_window_stats(values, min_window=12, max_window=24, past_values=None):
    """
    Compute window statistics and per-entry pct for any forecast array.

    Analysis window = past_values + values[:max_window - n_past], capped at
    max_window entries total.  When past_values is omitted the window is
    values[:max_window] (original behaviour).

    Returns (stats, pcts) where:

    stats : dict  {"min", "max", "avg", "median", "horizon"} rounded to 4
                  decimals, or all-None when the analysis window < min_window.
    pcts  : list[float]  per-entry pct for values[] only (0=lowest, 1=highest
                  in the full window); 0.5 for entries beyond the window.

    Parameters
    ----------
    values      : list[float]  forecast values (index 0 = current hour)
    min_window  : int          minimum window size for valid stats (default 12)
    max_window  : int          maximum window size (default 24)
    past_values : list[float]  historical values preceding values[0],
                               oldest-first.  Up to max_window entries total.
    """
    past       = list(past_values) if past_values else []
    n_past     = len(past)
    n          = len(values)
    analysis   = past + values[:max_window - n_past]
    n_analysis = len(analysis)
    invalid    = {"min": None, "max": None, "avg": None, "median": None, "horizon": None}

    if n_analysis < min_window:
        return invalid, [0.5] * n

    lo     = min(analysis)
    hi     = max(analysis)
    spread = (hi - lo) or 1e-9

    stats = {
        "min":     round(lo, 4),
        "max":     round(hi, 4),
        "avg":     round(sum(analysis) / n_analysis, 4),
        "median":  round(_statistics.median(analysis), 4),
        "horizon": n_analysis,
    }

    n_future = n_analysis - n_past   # forecast entries inside the window
    pcts = [max(0.001, round((v - lo) / spread, 3)) if i < n_future else 0.5
            for i, v in enumerate(values)]

    return stats, pcts
