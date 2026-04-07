"""
ems_calibration.py  –  Heat-pump and house thermal model calibrator
=======================================================================
Publishes sensor.energy_calibration with daily-updated coefficients
derived from Home Assistant long-term statistics (hourly aggregates,
last 14 days).

Coefficients
------------
  k          kWh_th/(°C·h)   House heat-loss: thermal demand per ΔT degree
  beta_irr   °C/W_irr        Solar gain: irradiance → effective indoor temp rise
  cop_a      –               COP intercept (COP at T_outdoor = 0 °C)
  cop_b      1/°C            COP slope (improvement per °C warmer outside)
  irr_scale  –               Clear-sky irradiance scale vs sensor readings
  temp_bias_a / temp_bias_b  OLS: actual_temp  = a × forecast_temp  + b
  irr_bias_a  / irr_bias_b   OLS: actual_irr   = a × forecast_irr   + b

Data sources  (hourly statistics via HA REST API)
-------------------------------------------------
  sensor.kamstrup_warmtepomp_power          thermal output power    (W,   mean)
  sensor.kamstrup_warmtepomp_energy_output  thermal output energy   (GJ,  sum→delta)
  sensor.heatpump_energy                    HP electrical energy    (kWh, sum→delta)
  sensor.heatpump_control_energy            controller + pump energy(kWh, sum→delta)
  sensor.outsidetemp_outside_temperature    outdoor temperature     (°C,  mean)
  sensor.smartcontrol_inside                room temperature        (°C,  mean)
  sensor.irradiance                         solar irradiance        (W,   mean)
  sensor.forecast_outside_temp              temperature forecast     (°C,  mean)
  sensor.forecast_irradiance                irradiance forecast      (W,   mean)

Physical models
---------------
  k      :  P_thermal [kW] = k × (T_setpoint – T_outdoor)
  beta   :  T_eff = T_outdoor + beta_irr × irr
             HP demand = max(0, k × (T_setpoint – T_eff))
  COP    :  COP = cop_a + cop_b × T_outdoor
             (linear, fitted from derived COP per hour)

COP derivation
--------------
  COP is computed per hour as:
    COP = Δthermal_energy_kWh / Δ(hp_energy_kWh + ctrl_energy_kWh)
  Energy deltas from cumulative counters avoid the COP spikes that occur
  when using instantaneous power sensors: kamstrup_warmtepomp_power has
  thermal inertia and responds slower than heatpump_power, causing high
  peaks and low valleys in the ratio.  sensor.kamstrup_cop is NOT used
  (Kamstrup pulse-counter drift + asymmetric measurement intervals).

Update schedule
---------------
  Thermal calibration: startup + 30 s, daily at 03:00
  Energy calibration:  startup + 120 s, daily at 03:30
  (split to stay within AppDaemon 10 s callback limit per run)
"""

import statistics
import datetime

import appdaemon.plugins.hass.hassapi as hass

from ems_base import (
    HP_SETPOINT_ENTITY, HH_ENERGY_SOURCES,
    I0_CLEAR,
    clear_sky_w as _clear_sky_w,
)
from ems_heating import (
    SOLAR_GAIN_WEIGHTS as _SOLAR_GAIN_WEIGHTS,
    GAMMA_DEFAULT,
    history_to_hourly_mean as _h_mean,
    history_to_hourly_cumul_change as _h_delta,
)

# ── Sensor IDs ────────────────────────────────────────────────────────────────

THERMAL_POWER_S  = "sensor.kamstrup_warmtepomp_power"          # W   thermal output (mean)
THERMAL_ENERGY_S = "sensor.kamstrup_warmtepomp_energy_output"  # GJ  thermal output (sum→delta)
HP_ENERGY_S      = "sensor.heatpump_energy"                    # kWh electrical input (sum→delta)
HP_CTRL_ENERGY_S = "sensor.heatpump_control_energy"            # kWh controller+pump (sum→delta)
HP_CH_MODE_S     = "binary_sensor.smartcontrol_ch_mode"        # on/off CH demand (binary)
T_OUT_S          = "sensor.outsidetemp_outside_temperature"    # °C  outdoor temp (mean)
T_IN_S           = "sensor.smartcontrol_inside"                # °C  room temp (mean)
IRR_S            = "sensor.irradiance"                         # W   irradiance (mean)
PV_ENERGY_S      = "sensor.pv_total_energy"                    # kWh PV production (sum→delta)
FORECAST_TEMP_S  = "sensor.forecast_outside_temp"              # °C  temperature forecast (mean)
FORECAST_IRR_S   = "sensor.forecast_irradiance"                # W   irradiance forecast (mean)

CALIB_ENTITY     = "sensor.energy_calibration"

# Energy sensors that store a cumulative sum in long-term statistics.
# Hourly deltas are computed from consecutive sum values.
_ENERGY_SUM_SENSORS = (
    {THERMAL_ENERGY_S, HP_ENERGY_S, HP_CTRL_ENERGY_S, PV_ENERGY_S}
    | {s for s, _ in HH_ENERGY_SOURCES}
)

# Binary sensors: "on"/"off" states are converted to 1.0/0.0 before hourly mean.
_BINARY_SENSORS = {HP_CH_MODE_S}

# Attribute names from previous schema versions that must be removed on next publish.
_STALE_ATTRS = frozenset(
    [f"hh_h{h:02d}"      for h in range(24)] +
    [f"pv_eta_h{h:02d}"  for h in range(24)] +
    ["gamma", "gamma_samples", "gamma_n_off", "gamma_n_on"]
)

GJ_TO_KWH = 1000.0 / 3.6   # 1 GJ = 277.78 kWh

# ── Thresholds ────────────────────────────────────────────────────────────────

CALIB_DAYS        = 14     # days of statistics to use
HP_ACTIVE_W       = 200.0  # thermal W threshold above which HP is considered active
IRR_ACTIVE_W      = 50.0   # irradiance W threshold (general: pv_eta, k exclusion)
IRR_GAMMA_H_MIN_W = 100.0  # minimum irradiance for gamma_h calibration samples
                            # 50 W is sufficient for pv_eta (pure ratio) but not for gamma_h:
                            # at dawn/dusk the thermal-inertia noise in k×ΔT dominates
                            # the numerator, amplified by the low irr denominator
MIN_K_SAMPLES     = 12     # minimum active HP hours for reliable k
MIN_BETA_SAMPLES  = 5      # minimum standby+sun hours for reliable beta
MIN_COP_SAMPLES   = 10     # minimum active hours for reliable COP regression
MIN_ELEC_KWH      = 0.05   # kWh minimum electrical energy per hour for valid COP sample
MIN_BIAS_SAMPLES  = 24     # minimum aligned pairs for reliable forecast bias regression
MIN_HH_SAMPLES    = 3      # minimum hourly samples per hour-of-day bucket for HH baseline
MIN_PV_ETA_SAMPLES  = 3    # minimum samples per hour bucket for PV eta
MIN_GAMMA_H_SAMPLES = 1    # minimum samples per hour bucket for solar-gain gamma_h

# ── Fallback defaults (used when calibration data is insufficient) ─────────────

K_DEF            = 0.050
BETA_DEF         = 0.0
COP_A_DEF        = 3.5
COP_B_DEF        = 0.08
COP_C_DEF        = 0.0    # quadratic term; 0.0 = linear fallback
IRR_SCALE_DEF    = 1.0
SOLAR_SCALE_DEF  = 1.0
TEMP_BIAS_A_DEF  = 1.0   # no correction: actual = 1.0 × forecast + 0.0
TEMP_BIAS_B_DEF  = 0.0
IRR_BIAS_A_DEF   = 1.0
IRR_BIAS_B_DEF   = 0.0
HH_H_DEF         = 0.4    # kWh/h default when insufficient samples
GAMMA_H_DEF      = 0.001     # ×1000 scaled unit (mWh/(W/m²·h)); sentinel for uncalibrated hours
PV_ETA_DEF       = 0.001     # ×1000 scaled unit (mWh/(W/m²·h)); sentinel for uncalibrated hours


class EmsCalibration(hass.Hass):

    def initialize(self):
        # Publish a clean full-defaults state immediately to wipe any stale attributes
        # from previous schema versions (flat hh_hXX, pv_eta_hXX, scalar gamma, etc.).
        t_sp = self._sensor_float(HP_SETPOINT_ENTITY, 20.5)
        clean = {**self._defaults_thermal(t_sp), **self._defaults_energy()}
        self.set_state(CALIB_ENTITY, state="initializing", attributes=clean, replace=True)

        self.run_in(self._calibrate_thermal, 30)
        self.run_in(self._calibrate_energy, 120)
        self.run_daily(self._calibrate_thermal, "03:00:00")
        self.run_daily(self._calibrate_energy, "03:30:00")

    # ── Trigger handlers ──────────────────────────────────────────────────────

    def _calibrate_thermal(self, kwargs):
        """k, beta, COP, irr_scale, temp_bias, irr_bias — thermal sensor subset."""
        t_sp  = self._sensor_float(HP_SETPOINT_ENTITY, 20.5)
        end   = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(days=CALIB_DAYS)

        sensors = [THERMAL_POWER_S, THERMAL_ENERGY_S, HP_ENERGY_S,
                   HP_CTRL_ENERGY_S, T_OUT_S, IRR_S,
                   FORECAST_TEMP_S, FORECAST_IRR_S]
        try:
            hours = self._fetch_and_align(start, end, sensors)
        except Exception as exc:
            self.log(f"[EnergyCalib:thermal] history fetch failed: {exc}", level="WARNING")
            self._publish(self._defaults_thermal(t_sp))
            return
        if len(hours) < 24:
            self.log(f"[EnergyCalib:thermal] only {len(hours)} hours – keeping defaults",
                     level="WARNING")
            self._publish(self._defaults_thermal(t_sp))
            return

        k,     n_k    = self._fit_k(hours, t_sp)
        beta,  n_beta = self._fit_beta(hours)
        cop_a, cop_b, cop_c, n_cop = self._fit_cop(hours)
        irr_scale     = self._fit_irr_scale(hours)
        temp_bias_a, temp_bias_b, n_temp_bias = self._fit_temp_bias(hours)
        irr_bias_a,  irr_bias_b,  n_irr_bias  = self._fit_irr_bias(hours)

        if n_k   < MIN_K_SAMPLES:    k = K_DEF
        if n_beta < MIN_BETA_SAMPLES: beta = BETA_DEF
        if n_cop < MIN_COP_SAMPLES:  cop_a, cop_b, cop_c = COP_A_DEF, COP_B_DEF, COP_C_DEF

        source = "calibrated" if n_k >= MIN_K_SAMPLES else "default"
        solar_scale = round(
            float(self.get_state(CALIB_ENTITY, attribute="solar_scale") or SOLAR_SCALE_DEF), 4
        )
        self._publish({
            "k":               round(k, 5),
            "k_samples":       n_k,
            "beta_irr":        round(beta, 6) or 0.000001,
            "beta_samples":    n_beta,
            "cop_a":           round(cop_a, 3),
            "cop_b":           round(cop_b, 4),
            "cop_c":           round(cop_c, 5),
            "cop_samples":     n_cop,
            "irr_scale":       round(irr_scale, 4),
            "solar_scale":     solar_scale,
            "temp_bias_a":     temp_bias_a,
            "temp_bias_b":     temp_bias_b,
            "temp_bias_n":     n_temp_bias,
            "irr_bias_a":      irr_bias_a,
            "irr_bias_b":      irr_bias_b,
            "irr_bias_n":      n_irr_bias,
            "t_setpoint":      t_sp,
            "source":          source,
            "days_used":       CALIB_DAYS,
            "hours_processed": len(hours),
            "friendly_name":   "Energy Calibration",
        })
        self.log(
            f"[EnergyCalib:thermal] k={k:.4f}(n={n_k}) "
            f"beta={beta:.5f}(n={n_beta}) "
            f"cop={cop_a:.2f}+{cop_b:.3f}·T+{cop_c:.5f}·T²(n={n_cop}) "
            f"irr_scale={irr_scale:.3f} "
            f"temp_bias={temp_bias_a:.3f}×+{temp_bias_b:.2f}(n={n_temp_bias}) "
            f"irr_bias={irr_bias_a:.3f}×+{irr_bias_b:.1f}(n={n_irr_bias}) "
            f"source={source} hours={len(hours)}"
        )

    def _calibrate_energy(self, kwargs):
        """pv_eta, gamma_h, hh_base — energy sensor subset; reads k from published entity."""
        end   = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(days=CALIB_DAYS)

        _base = [T_IN_S, T_OUT_S, THERMAL_ENERGY_S, HP_ENERGY_S, IRR_S, PV_ENERGY_S]
        _hh   = [s for s, _ in HH_ENERGY_SOURCES if s not in _base]
        sensors = _base + _hh
        try:
            hours = self._fetch_and_align(start, end, sensors)
        except Exception as exc:
            self.log(f"[EnergyCalib:energy] history fetch failed: {exc}", level="WARNING")
            self._publish(self._defaults_energy())
            return
        if len(hours) < 24:
            self.log(f"[EnergyCalib:energy] only {len(hours)} hours – keeping defaults",
                     level="WARNING")
            self._publish(self._defaults_energy())
            return

        k = float(self.get_state(CALIB_ENTITY, attribute="k") or K_DEF)

        hh_baseline, n_hh_min, n_hh_max = self._fit_household_baseline(hours)
        pv_eta,      n_pv_min, n_pv_max = self._fit_pv_eta(hours)
        gamma_h, n_gamma_min, n_gamma_max = self._fit_gamma_h(hours, k)

        self._publish({
            "gamma_h":      gamma_h,
            "gamma_n_min":  n_gamma_min,
            "gamma_n_max":  n_gamma_max,
            "hh_base":      hh_baseline,
            "hh_n_min":     n_hh_min,
            "hh_n_max":     n_hh_max,
            "pv_eta":       pv_eta,
            "pv_eta_n_min": n_pv_min,
            "pv_eta_n_max": n_pv_max,
        })
        pv_cal = sum(1 for v in pv_eta if v > 0.01)
        self.log(
            f"[EnergyCalib:energy] "
            f"hh_baseline_avg={sum(hh_baseline)/24:.3f} kWh/h(n={n_hh_min}..{n_hh_max}) "
            f"pv_eta={pv_cal}/24h(n={n_pv_min}..{n_pv_max}) "
            f"gamma_h={sum(1 for v in gamma_h if v > 0.01)}/24h(n={n_gamma_min}..{n_gamma_max}) "
            f"hours={len(hours)}"
        )

    # ── History fetch + align → {dt: {sensor_id: value}} ────────────────────
    #
    #   Uses AppDaemon get_history() instead of the recorder statistics REST API
    #   (which returns 404 in HA 2026.x).
    #
    #   Mean sensors  : value = hourly mean via history_to_hourly_mean()
    #   Energy sensors: value = hourly delta via history_to_hourly_cumul_change()

    def _fetch_and_align(self, start, end, sensors):
        by_dt = {}
        for sensor_id in sensors:
            try:
                history = self.get_history(
                    entity_id=sensor_id,
                    days=CALIB_DAYS,
                )
                states = history[0] if history and history[0] else []
            except Exception as exc:
                self.log(f"[EnergyCalib] get_history({sensor_id}) failed: {exc}",
                         level="WARNING")
                continue

            if sensor_id in _BINARY_SENSORS:
                # Convert on/off → 1.0/0.0 so history_to_hourly_mean can process them
                for s in states:
                    if s.get("state") == "on":
                        s["state"] = "1.0"
                    elif s.get("state") == "off":
                        s["state"] = "0.0"
                hourly = _h_mean(states)
            elif sensor_id in _ENERGY_SUM_SENSORS:
                hourly = _h_delta(states)
            else:
                hourly = _h_mean(states)

            for date, h, val in hourly:
                dt = datetime.datetime(date.year, date.month, date.day, h,
                                       tzinfo=datetime.timezone.utc)
                by_dt.setdefault(dt, {})[sensor_id] = val

        return by_dt

    # ── k: house heat-loss coefficient ────────────────────────────────────────

    def _fit_k(self, hours, t_sp):
        """
        k  =  P_thermal_kW  /  (T_setpoint – T_outdoor)

        Sampled on HP-active hours with low irradiance (night / overcast).
        During sunny hours the HP produces less heat because solar gain already
        covers part of the demand, causing k to be underestimated if those
        hours are included.  Missing IRR_S (night) is treated as 0 W.
        """
        vals = []
        for h in hours.values():
            p_th  = h.get(THERMAL_POWER_S)
            t_out = h.get(T_OUT_S)
            irr   = h.get(IRR_S) or 0.0   # None / missing → night → 0
            if p_th is None or t_out is None:
                continue
            if p_th < HP_ACTIVE_W:
                continue
            if irr > IRR_ACTIVE_W:
                continue   # solar gain contaminates k estimate
            dt = t_sp - t_out
            if dt < 1.0:
                continue
            vals.append((p_th / 1000.0) / dt)   # kW / °C = kWh_th / (°C·h)

        if not vals:
            return K_DEF, 0
        return max(0.01, min(statistics.median(vals), 1.0)), len(vals)

    # ── beta_irr: solar-gain coefficient ─────────────────────────────────────

    def _fit_beta(self, hours):
        """
        Sampled on HP-standby hours with significant irradiance:
          beta_irr  =  (T_indoor – T_outdoor) / effective_irr

        effective_irr = irr_W × SOLAR_GAIN_WEIGHTS[hour]

        The time-of-day weight (Gaussian peaked ~10:30) accounts for east-south-
        facing windows: morning sun shines deep into the room; afternoon sun hits
        the wrong wall and contributes little to indoor temperature.
        Using the same weight as hp_per_slot() keeps calibration self-consistent.
        """
        vals = []
        for dt, h in hours.items():
            p_th  = h.get(THERMAL_POWER_S)
            irr   = h.get(IRR_S)
            t_out = h.get(T_OUT_S)
            t_in  = h.get(T_IN_S)
            if any(v is None for v in [p_th, irr, t_out, t_in]):
                continue
            if p_th >= HP_ACTIVE_W:
                continue   # HP running → not a solar-gain standby hour
            if irr < IRR_ACTIVE_W:
                continue   # no significant irradiance
            delta = t_in - t_out
            if delta < 0.5:
                continue   # indoor not meaningfully warmer than outdoor
            effective_irr = irr * _SOLAR_GAIN_WEIGHTS[dt.hour]
            if effective_irr < 1.0:
                continue   # weight ≈ 0 at this hour; skip to avoid division noise
            vals.append(delta / effective_irr)

        if not vals:
            return BETA_DEF, 0
        return max(0.0, min(statistics.median(vals), 0.10)), len(vals)

    # ── COP: quadratic model vs outdoor temperature ───────────────────────────

    def _solve3(self, A, rhs):
        """
        Solve 3×3 linear system  A·x = rhs  via Gaussian elimination with
        partial pivoting.  Returns solution vector [x0, x1, x2] or None if
        the system is singular.
        """
        M = [[A[i][j] for j in range(3)] + [rhs[i]] for i in range(3)]
        for col in range(3):
            pivot = max(range(col, 3), key=lambda r: abs(M[r][col]))
            M[col], M[pivot] = M[pivot], M[col]
            if abs(M[col][col]) < 1e-12:
                return None
            for row in range(col + 1, 3):
                f = M[row][col] / M[col][col]
                for j in range(col, 4):
                    M[row][j] -= f * M[col][j]
        x = [0.0] * 3
        for i in range(2, -1, -1):
            x[i] = (M[i][3] - sum(M[i][j] * x[j] for j in range(i + 1, 3))) / M[i][i]
        return x

    def _fit_cop(self, hours):
        """
        COP  =  cop_a  +  cop_b × T  +  cop_c × T²

        Quadratic model captures the non-linear COP drop near freezing caused by
        defrost cycles and refrigerant behaviour, and the flattening at higher
        outdoor temperatures.

        COP derived per hour as:
          COP = Δthermal_energy_kWh / Δ(hp_energy_kWh + ctrl_energy_kWh)
        """
        pts = []
        for h in hours.values():
            p_th       = h.get(THERMAL_POWER_S)
            t_out      = h.get(T_OUT_S)
            d_therm_gj = h.get(THERMAL_ENERGY_S)
            d_hp_kwh   = h.get(HP_ENERGY_S)
            d_ctrl_kwh = h.get(HP_CTRL_ENERGY_S, 0.0)

            if p_th is None or t_out is None or d_therm_gj is None or d_hp_kwh is None:
                continue
            if p_th < HP_ACTIVE_W:
                continue
            elec_kwh = d_hp_kwh + d_ctrl_kwh
            if elec_kwh < MIN_ELEC_KWH:
                continue
            thermal_kwh = d_therm_gj * GJ_TO_KWH
            cop = thermal_kwh / elec_kwh
            if not (1.0 <= cop <= 10.0):
                continue
            pts.append((t_out, cop))

        n = len(pts)
        if n < MIN_COP_SAMPLES:
            return COP_A_DEF, COP_B_DEF, COP_C_DEF, n

        # Normal equations for  y = a + b·x + c·x²
        sx   = sum(p[0]         for p in pts)
        sx2  = sum(p[0]**2      for p in pts)
        sx3  = sum(p[0]**3      for p in pts)
        sx4  = sum(p[0]**4      for p in pts)
        sy   = sum(p[1]         for p in pts)
        sxy  = sum(p[0]*p[1]    for p in pts)
        sx2y = sum(p[0]**2*p[1] for p in pts)

        A   = [[n,   sx,  sx2],
               [sx,  sx2, sx3],
               [sx2, sx3, sx4]]
        rhs = [sy, sxy, sx2y]

        coeffs = self._solve3(A, rhs)
        if coeffs is None:
            return COP_A_DEF, COP_B_DEF, COP_C_DEF, n

        a, b, c = coeffs
        a = max(1.0, min(a, 8.0))
        b = max(-0.5, min(b, 0.5))
        c = max(-0.05, min(c, 0.05))
        return round(a, 3), round(b, 4), round(c, 5), n

    # ── forecast bias: temperature ─────────────────────────────────────────────

    def _fit_temp_bias(self, hours):
        """
        Offset-only correction:  actual_temp = forecast_temp + b
        where b = mean(actual − forecast) over all hours with both sensors.

        Slope is fixed at 1.0 because a proportional scale on temperature has no
        physical basis — a systematic warm/cold bias in the weather model is a
        constant additive offset, independent of temperature magnitude.

        Returns (a=1.0, b, n_samples).
        Defaults to (1.0, 0.0) when insufficient data.
        """
        diffs = []
        for h in hours.values():
            f = h.get(FORECAST_TEMP_S)
            a = h.get(T_OUT_S)
            if f is None or a is None:
                continue
            diffs.append(a - f)
        n = len(diffs)
        if n < MIN_BIAS_SAMPLES:
            return TEMP_BIAS_A_DEF, TEMP_BIAS_B_DEF, n
        b = statistics.mean(diffs)
        b = max(-15.0, min(b, 15.0))
        return TEMP_BIAS_A_DEF, round(b, 3), n

    # ── forecast bias: irradiance ──────────────────────────────────────────────

    def _fit_irr_bias(self, hours):
        """
        Scale-only correction through the origin:  actual_irr = a × forecast_irr
        where a = Σ(f × actual) / Σ(f²)  (OLS forced through origin).

        An intercept/offset is physically wrong for irradiance: zero forecast
        (night) must map to zero actual.  Only daytime hours with both sensors
        above 50 W are used; near-zero samples would add noise and drag the
        fit toward a spurious offset.

        Returns (a, b=0.0, n_samples).
        Defaults to (1.0, 0.0) when insufficient data.
        """
        sum_ff = 0.0
        sum_fa = 0.0
        n = 0
        for h in hours.values():
            f = h.get(FORECAST_IRR_S)
            a = h.get(IRR_S)
            if f is None or a is None:
                continue
            if f < 50 and a < 50:
                continue   # near-zero / night – skip to avoid noise
            sum_ff += f * f
            sum_fa += f * a
            n += 1
        if n < MIN_BIAS_SAMPLES or sum_ff < 1e-9:
            return IRR_BIAS_A_DEF, IRR_BIAS_B_DEF, n
        a = sum_fa / sum_ff
        a = max(0.1, min(a, 3.0))
        return round(a, 4), 0.0, n

    # ── household baseline: per-hour-of-day median ────────────────────────────

    def _fit_household_baseline(self, hours):
        """
        Per-hour-of-day baseline household consumption (kWh/h).

        For each calendar hour (0..23) collect all days where every
        HH_ENERGY_SOURCES sensor has a valid delta value, compute:
            hh[h] = Σ(sign × Δsensor[h])
        then take the median across 14 days.

        A per-hour model captures genuine load patterns (morning peak,
        midday dip, evening peak) that a flat-rate EWMA misses.

        Returns (values_24h, n_min, n_max):
            values_24h : list[float]  24 hourly kWh/h medians
            n_min      : int          fewest samples across any hour bucket
            n_max      : int          most samples across any hour bucket
        """
        buckets = [[] for _ in range(24)]
        for dt, h in hours.items():
            total = 0.0
            for sensor_id, sign in HH_ENERGY_SOURCES:
                # Cumulative sensors: no update in an hour means delta = 0 (device idle).
                # Treat missing values as 0.0 rather than discarding the entire hour.
                v = h.get(sensor_id, 0.0) or 0.0
                total += sign * v
            if total < 0.0:
                continue   # physically impossible
            buckets[dt.hour].append(total)

        result = []
        for bucket in buckets:
            if len(bucket) >= MIN_HH_SAMPLES:
                result.append(round(max(0.0, statistics.median(bucket)), 3))
            else:
                result.append(HH_H_DEF)

        n_min = min(len(b) for b in buckets)
        n_max = max(len(b) for b in buckets)
        return result, n_min, n_max

    # ── pv_eta: per-hour-of-day PV efficiency ────────────────────────────────

    def _fit_pv_eta(self, hours):
        """
        Per-hour-of-day PV efficiency: eta_h = pv_kwh / irr_W [kWh/(W/m²·h)]

        Captures panel orientation, tilt, shading, and inverter losses
        implicitly from history — no physical parameters needed.

        Usage in forecast:
            pv_forecast[h] = irr_forecast[h] × eta_h[h_of_day]

        Returns (eta_24h, n_min, n_max):
            eta_24h : list[float]  24 values; PV_ETA_DEF (0.0) when < MIN_PV_ETA_SAMPLES
            n_min   : int          fewest samples across any non-zero bucket
            n_max   : int          most samples across any non-zero bucket
        """
        buckets = [[] for _ in range(24)]
        for dt, h in hours.items():
            pv_kwh = h.get(PV_ENERGY_S)
            irr    = h.get(IRR_S)
            if pv_kwh is None or irr is None:
                continue
            if irr < IRR_ACTIVE_W:
                continue   # night / overcast – ratio not meaningful
            if pv_kwh <= 0:
                continue   # no PV output (shaded, off, or inverter inactive)
            buckets[dt.hour].append(pv_kwh / irr)

        result = []
        for bucket in buckets:
            if len(bucket) >= MIN_PV_ETA_SAMPLES:
                result.append(round(max(0.0, statistics.median(bucket)) * 1000, 4))
            else:
                result.append(PV_ETA_DEF)

        counts = [len(b) for b in buckets]
        non_zero = [c for c in counts if c > 0]
        n_min = min(non_zero) if non_zero else 0
        n_max = max(counts) if counts else 0
        return result, n_min, n_max

    # ── gamma_h: per-hour solar-gain factor ──────────────────────────────────

    @staticmethod
    def _mad_filter(vals):
        """
        Remove outliers using Median Absolute Deviation (3×MAD threshold).
        Robust: the MAD itself is not affected by the outliers being removed.
        Applied only when bucket has ≥ 5 samples (too few → no filtering).
        """
        if len(vals) < 5:
            return vals
        med = statistics.median(vals)
        mad = statistics.median(abs(v - med) for v in vals)
        if mad < 1e-9:
            return vals   # all values identical, nothing to filter
        return [v for v in vals if abs(v - med) <= 3 * mad]

    def _fit_gamma_h(self, hours, k):
        """
        gamma_h[hour]  =  solar_gain [kWh/h]  /  irr_GHI [W/m²]

        All sunny hours are used regardless of HP state:
          solar_gain = k × (T_in – T_out) – hp_thermal_kWh

        When HP is off, hp_thermal_kWh = 0.
        When HP is on,  hp_thermal_kWh = THERMAL_ENERGY_S × GJ_TO_KWH.
        If THERMAL_ENERGY_S is missing, the hour is treated as HP-off (0).

        No HP on/off branching, no lag filter.  Switching-moment artefacts
        fall in different hour buckets on different days and are removed by
        the 3×MAD outlier filter once enough samples accumulate.
        """
        buckets = [[] for _ in range(24)]

        for dt, h in hours.items():
            t_out      = h.get(T_OUT_S)
            t_in       = h.get(T_IN_S)
            irr        = h.get(IRR_S)
            d_therm_gj = h.get(THERMAL_ENERGY_S)

            if any(v is None for v in [t_out, t_in, irr]):
                continue
            if irr < IRR_GAMMA_H_MIN_W:
                continue   # below 100 W, thermal-inertia noise dominates k×ΔT numerator
            delta_t = t_in - t_out
            if delta_t < 1.0:
                continue

            hp_thermal_kwh = (d_therm_gj * GJ_TO_KWH) if d_therm_gj is not None else 0.0

            solar_gain = k * delta_t - hp_thermal_kwh
            if solar_gain <= 0:
                continue

            buckets[dt.hour].append(solar_gain / irr)

        result = []
        counts = []
        for h in range(24):
            filtered = self._mad_filter(buckets[h])
            n = len(filtered)
            counts.append(n)
            val = statistics.median(filtered) * 1000 if n >= MIN_GAMMA_H_SAMPLES else GAMMA_H_DEF
            result.append(round(max(GAMMA_H_DEF, val), 4))

        non_zero = [c for c in counts if c > 0]
        n_min = min(non_zero) if non_zero else 0
        n_max = max(counts)   if counts   else 0
        return result, n_min, n_max

    # ── irr_scale ─────────────────────────────────────────────────────────────

    def _fit_irr_scale(self, hours):
        """
        Scale factor = 90th-percentile of (sensor_irr / clear_sky_model_irr)
        for daytime hours where the clear-sky model predicts > 200 W.

        This properly accounts for sensor tilt / orientation.  A south-facing
        sensor can measure more than the horizontal-plane clear-sky value, so
        dividing by I0_CLEAR (900 W) would give a falsely low scale.
        Example: sensor=715 W, clear_sky_model=572 W → scale=1.25, not 0.78.
        """
        ratios = []
        for dt, h in hours.items():
            irr_meas = h.get(IRR_S)
            if irr_meas is None or irr_meas < 10:
                continue
            cs = _clear_sky_w(dt.date())[dt.hour]
            if cs < 200:
                continue   # skip night / low-sun hours
            ratios.append(irr_meas / cs)

        if len(ratios) < 5:
            return IRR_SCALE_DEF
        ratios.sort()
        idx = max(0, int(len(ratios) * 0.90) - 1)
        return max(0.05, min(ratios[idx], 2.5))

    # ── Publish / helpers ─────────────────────────────────────────────────────

    def _publish(self, attrs):
        cur = self.get_state(CALIB_ENTITY, attribute="all") or {}
        # Start from current state, strip stale attrs from old schema versions
        merged = {
            k: v for k, v in (cur.get("attributes") or {}).items()
            if k not in _STALE_ATTRS
        }
        merged.update(attrs)
        self.set_state(
            CALIB_ENTITY,
            state=datetime.datetime.now().isoformat(timespec="minutes"),
            attributes=merged,
            replace=True,   # full replacement — prevents AppDaemon from merging old attrs back
        )

    def _defaults_thermal(self, t_sp):
        return {
            "k": K_DEF,           "k_samples": 0,
            "beta_irr": BETA_DEF or 0.000001, "beta_samples": 0,
            "cop_a": COP_A_DEF,   "cop_b": COP_B_DEF, "cop_c": COP_C_DEF, "cop_samples": 0,
            "irr_scale": IRR_SCALE_DEF,
            "solar_scale": round(float(self.get_state(CALIB_ENTITY, attribute="solar_scale") or SOLAR_SCALE_DEF), 4),
            "temp_bias_a": TEMP_BIAS_A_DEF, "temp_bias_b": TEMP_BIAS_B_DEF, "temp_bias_n": 0,
            "irr_bias_a":  IRR_BIAS_A_DEF,  "irr_bias_b":  IRR_BIAS_B_DEF,  "irr_bias_n": 0,
            "t_setpoint": t_sp,   "source": "default",
            "days_used": CALIB_DAYS, "hours_processed": 0,
            "friendly_name": "Energy Calibration",
        }

    def _defaults_energy(self):
        return {
            "gamma_h": [GAMMA_H_DEF] * 24, "gamma_n_min": 0, "gamma_n_max": 0,
            "hh_base": [HH_H_DEF]   * 24,  "hh_n_min":    0, "hh_n_max":    0,
            "pv_eta":  [PV_ETA_DEF] * 24,  "pv_eta_n_min":0, "pv_eta_n_max":0,
        }

    def _sensor_float(self, entity_id, default=0.0):
        try:
            return float(self.get_state(entity_id) or default)
        except (TypeError, ValueError):
            return default
