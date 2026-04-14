"""
EMS Planner – AppDaemon app
=====================================
Orchestrator for the Energy Management System.

Triggers:
  • Every hour at HH:00:30 – revise strategy with latest actuals (SOC, PV, consumption)

Note: Nordpool tomorrow data updates sensor.forecast_epex via ems_forecasts.py.
The strategy reads sensor.forecast_epex directly; no separate Nordpool trigger
needed here.  Additional mid-hour triggers (unexpected PV surge, EV deadline
change) can be added later.
"""

import appdaemon.plugins.hass.hassapi as hass


# ── App ───────────────────────────────────────────────────────────────────────
class EmsPlanner(hass.Hass):

    def initialize(self):
        # Revise strategy every hour, 30 s after the top of the hour.
        # By then the previous hour's actuals (SOC, consumption, PV) are settled.
        self.run_hourly(self._run_strategy, "00:00:30")
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
