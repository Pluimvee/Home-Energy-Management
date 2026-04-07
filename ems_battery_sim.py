"""
ems_battery_sim.py – BatSim Batterij Hardware Simulator
=========================================================
AppDaemon app. Simuleert de Solis S6 EH3P accu-hardware.

Luistert op de RC-interface (input_boolean/input_number helpers) en simuleert
de hardware-respons: SOC-integratie + grid_port_power publicatie.

RC-interface (≡ echte Solis RC-entiteiten):
  input_boolean.batsim_force_charge     ≡ switch.rc_force_battery_charge
  input_boolean.batsim_force_discharge  ≡ switch.rc_force_battery_discharge
  input_number.batsim_charge_power      ≡ number.solis_s6_eh3p_rc_force_battery_charge_power
  input_number.batsim_discharge_power   ≡ number.solis_s6_eh3p_rc_force_battery_discharge_power

Gepubliceerde sensoren (≡ echte Solis sensoren):
  sensor.batsim_soc_pct          ≡ sensor.solis_s6_eh3p_battery_soc
  sensor.batsim_grid_port_power  ≡ sensor.solis_s6_eh3p_ac_grid_port_power

Tekenconventie (identiek aan Solis):
  battery_w > 0  →  ontladen (batterij levert, grid_port positief)
  battery_w < 0  →  laden    (batterij neemt, grid_port negatief)
"""

import appdaemon.plugins.hass.hassapi as hass

# ── Batterij parameters ────────────────────────────────────────────────────────
BATTERY_CAPACITY_KWH = 20.0
BATTERY_USABLE       = 0.93      # 18.6 kWh bruikbaar
BATTERY_EFFICIENCY   = 0.97      # roundtrip rendement
BATTERY_MIN_SOC      = 7.0       # %
BATTERY_MAX_SOC      = 100.0     # %
SOC_INIT             = 50.0      # % startwaarde

# ── RC-interface entiteiten ────────────────────────────────────────────────────
P1_SENSOR           = "sensor.ecogrid_connect_power"
RC_CHARGE_SWITCH    = "input_boolean.batsim_force_charge"
RC_DISCHARGE_SWITCH = "input_boolean.batsim_force_discharge"
RC_CHARGE_POWER     = "input_number.batsim_charge_power"
RC_DISCHARGE_POWER  = "input_number.batsim_discharge_power"
SOC_SET_INPUT       = "input_number.batsim_soc_instellen"


class EmsBatterySim(hass.Hass):

    def initialize(self):
        # SOC hernemen van vorige run
        try:
            stored = float(self.get_state("sensor.batsim_soc_pct") or SOC_INIT)
            self._soc = stored if BATTERY_MIN_SOC <= stored <= BATTERY_MAX_SOC else SOC_INIT
        except (TypeError, ValueError):
            self._soc = SOC_INIT

        self._last_time  = self.datetime()
        self._last_bat_w = 0.0

        # P1-trigger voor SOC-integratie tijdsdelta
        self.listen_state(self.on_p1_change, P1_SENSOR)
        # Handmatige SOC-override via slider
        self.listen_state(self.on_soc_set, SOC_SET_INPUT)

        # Initiële publicatie
        self.set_state("sensor.batsim_soc_pct",
            state=str(round(self._soc, 1)), replace=True,
            attributes={"friendly_name": "BatSim SOC", "unit_of_measurement": "%",
                        "device_class": "battery", "state_class": "measurement"})
        self.set_state("sensor.batsim_grid_port_power",
            state="0", replace=True,
            attributes={"friendly_name": "BatSim AC grid port", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.log(f"[BatterySim] gestart, SOC={self._soc:.1f}%")

    def on_p1_change(self, entity, attribute, old, new, kwargs):
        try:
            float(new)
        except (TypeError, ValueError):
            return

        now = self.datetime()

        # ── SOC integreren op basis van vorig commando (T-1) ──────────────────
        dt_hours = (now - self._last_time).total_seconds() / 3600.0
        if dt_hours > 0 and abs(self._last_bat_w) > 0:
            self._update_soc(self._last_bat_w, dt_hours)
        self._last_time = now

        # ── Huidig RC-commando lezen en clampen op SOC-grenzen ────────────────
        battery_w = self._clamp_by_soc(self._read_rc())
        self._last_bat_w = battery_w

        # ── Publiceren ────────────────────────────────────────────────────────
        self.set_state("sensor.batsim_grid_port_power",
            state=str(round(battery_w)), replace=True,
            attributes={"friendly_name": "BatSim AC grid port", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.set_state("sensor.batsim_soc_pct",
            state=str(round(self._soc, 1)), replace=True,
            attributes={"friendly_name": "BatSim SOC", "unit_of_measurement": "%",
                        "device_class": "battery", "state_class": "measurement"})

    def on_soc_set(self, entity, attribute, old, new, kwargs):
        """Handmatige SOC-override via input_number slider."""
        try:
            value = float(new)
        except (TypeError, ValueError):
            return
        self._soc = max(BATTERY_MIN_SOC, min(BATTERY_MAX_SOC, value))
        self.set_state("sensor.batsim_soc_pct",
            state=str(round(self._soc, 1)), replace=True,
            attributes={"friendly_name": "BatSim SOC", "unit_of_measurement": "%",
                        "device_class": "battery", "state_class": "measurement"})
        self.log(f"[BatterySim] SOC handmatig ingesteld op {self._soc:.1f}%")

    # ── RC-interface lezen ────────────────────────────────────────────────────

    def _read_rc(self):
        """Vertaal RC-switches + power-nummers naar battery_w."""
        charge_on    = self.get_state(RC_CHARGE_SWITCH)    == "on"
        discharge_on = self.get_state(RC_DISCHARGE_SWITCH) == "on"

        if discharge_on and not charge_on:
            try:
                power = float(self.get_state(RC_DISCHARGE_POWER) or 0)
            except (TypeError, ValueError):
                power = 0.0
            return max(0.0, power)

        elif charge_on and not discharge_on:
            try:
                power = float(self.get_state(RC_CHARGE_POWER) or 0)
            except (TypeError, ValueError):
                power = 0.0
            return -max(0.0, power)

        else:
            return 0.0

    # ── SOC berekening ────────────────────────────────────────────────────────

    def _update_soc(self, battery_w, dt_hours):
        """Integreer batterijvermogen naar SOC. battery_w > 0 = ontladen."""
        usable = BATTERY_CAPACITY_KWH * BATTERY_USABLE
        kwh = battery_w / 1000.0 * dt_hours
        if kwh > 0:
            delta_soc = -(kwh / (usable * BATTERY_EFFICIENCY)) * 100.0
        else:
            delta_soc = -(kwh * BATTERY_EFFICIENCY / usable) * 100.0
        self._soc = max(BATTERY_MIN_SOC, min(BATTERY_MAX_SOC, self._soc + delta_soc))

    def _clamp_by_soc(self, battery_w):
        """Blokkeer laden als vol, ontladen als leeg."""
        if battery_w > 0 and self._soc <= BATTERY_MIN_SOC + 0.1:
            return 0.0
        if battery_w < 0 and self._soc >= BATTERY_MAX_SOC - 0.1:
            return 0.0
        return battery_w
