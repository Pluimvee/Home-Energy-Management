"""
EMS Planner – AppDaemon app
=====================================
Orchestrator for the Energy Management System.

Triggers:
  • Every hour at HH:00:30 – revise strategy with latest actuals (SOC, PV, consumption)
  • Daily at 23:55      – calibrate solar scale factor (EWMA)

Note: Nordpool tomorrow data updates sensor.forecast_epex via ems_forecasts.py.
The strategy reads sensor.forecast_epex directly; no separate Nordpool trigger
needed here.  Additional mid-hour triggers (unexpected PV surge, EV deadline
change) can be added later.

Output:
  • sensor.energy_calibration – solar_scale attribute updated by _calibrate()
  • (strategy outputs added when ems_strategy is implemented)
"""

import appdaemon.plugins.hass.hassapi as hass
from ems_base import SOLAR_SENSORS_TODAY

# ── Constants ────────────────────────────────────────────────────────────────
LEARNING_RATE = 0.10   # EWMA alpha for solar_scale calibration


# ── App ───────────────────────────────────────────────────────────────────────
class EmsPlanner(hass.Hass):

    def initialize(self):
        # Revise strategy every hour, 30 s after the top of the hour.
        # By then the previous hour's actuals (SOC, consumption, PV) are settled.
        self.run_hourly(self._run_strategy, "00:00:30")
        # Calibrate solar scale against today's actuals
        self.run_daily(self._calibrate, "23:55:00")
        # Initial run after startup
        self.run_in(self._run_strategy, 30)

    # ── Strategy orchestration ────────────────────────────────────────────────

    def _run_strategy(self, kwargs):
        """
        Placeholder – will call ems_strategy once implemented.
        ems_strategy will read sensor.forecast_epex and the other forecast sensors
        and publish strategy outputs (peaks/troughs, battery setpoint, EV/HP/WPB modes).
        """
        self.log("[Planner] Strategy run triggered (ems_strategy not yet implemented)")

    # ── Calibration (EWMA solar scale) ────────────────────────────────────────

    def _calibrate(self, kwargs):
        """
        Update the solar_scale factor on sensor.energy_calibration by comparing
        today's actual inverter production (SOLAR_SENSORS_TODAY, kWh/day) against
        the Solcast forecast for today (SOLAR_SENSORS_TOMORROW read yesterday ≈
        sensor.forecast_pv today-only slice).

        Approximation: uses sensor.forecast_pv state (36 h total) as proxy for
        predicted today kWh.  Both values are totals in kWh; the ratio drives the
        EWMA update.

        new_scale = old_scale × (1 − α + α × actual/predicted)
        Clamped to [0.3, 2.5].
        """
        alpha = LEARNING_RATE

        actual_solar = sum(
            float(self.get_state(s) or 0.0) for s in SOLAR_SENSORS_TODAY
        )
        # sensor.forecast_pv state = total kWh over the 36 h forecast window
        # published by ems_forecasts around 00:05; used as proxy for today's
        # predicted production.
        pred_solar = float(
            self.get_state("sensor.forecast_pv") or 0.0
        )

        if pred_solar > 0.5 and actual_solar > 0.0:
            ratio  = actual_solar / pred_solar
            sc_cur = float(
                self.get_state("sensor.energy_calibration", attribute="solar_scale") or 1.0
            )
            sc_new = max(0.3, min(sc_cur * (1 - alpha + alpha * ratio), 2.5))

            cur       = self.get_state("sensor.energy_calibration", attribute="all") or {}
            cur_attrs = dict(cur.get("attributes", {}))
            cur_attrs["solar_scale"] = round(sc_new, 4)
            self.set_state(
                "sensor.energy_calibration",
                state=cur.get("state", ""),
                attributes=cur_attrs,
            )
            self.log(
                f"[Calibrate] Solar scale: {sc_cur:.4f} → {sc_new:.4f}  "
                f"(predicted={pred_solar:.1f} actual={actual_solar:.1f})"
            )
        else:
            self.log(
                f"[Calibrate] Skipped solar scale update "
                f"(predicted={pred_solar:.1f} actual={actual_solar:.1f})",
                level="DEBUG",
            )
