"""
ems_battery_sim.py – BatSim Batterij Hardware Simulator
=========================================================
AppDaemon app. Simuleert de Solis S6 EH3P accu-hardware inclusief
de vertragingen van de echte omvormer:

  Switch change  →  2.0s vertraging voor AC port update
  Power change   →  0.5s vertraging (alleen als betreffende switch al aan is)

SOC-integratie via 10s periodieke timer (niet P1-afhankelijk).

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
BATTERY_USABLE       = 0.93
BATTERY_EFFICIENCY   = 0.97
BATTERY_MIN_SOC      = 7.0
BATTERY_MAX_SOC      = 100.0
SOC_INIT             = 50.0

# ── Vertragingen (simulatie van Solis gedrag) ──────────────────────────────────
DELAY_SWITCH_S = 3.0   # s na switch-wissel
DELAY_POWER_S  = 1.0   # s na power-update (switch al aan)

# ── RC-interface entiteiten ────────────────────────────────────────────────────
RC_CHARGE_SWITCH    = "input_boolean.batsim_force_charge"
RC_DISCHARGE_SWITCH = "input_boolean.batsim_force_discharge"
RC_CHARGE_POWER     = "input_number.batsim_charge_power"
RC_DISCHARGE_POWER  = "input_number.batsim_discharge_power"
SOC_SET_INPUT       = "input_number.batsim_soc_instellen"


class EmsBatterySim(hass.Hass):

    def initialize(self):
        # SOC hernemen van vorige run via input_number (persisteert bij HA herstart).
        # sensor.batsim_soc_pct is een virtual sensor en verdwijnt bij herstart.
        try:
            stored = float(self.get_state(SOC_SET_INPUT) or SOC_INIT)
            self._soc = stored if BATTERY_MIN_SOC <= stored <= BATTERY_MAX_SOC else SOC_INIT
        except (TypeError, ValueError):
            self._soc = SOC_INIT

        self._last_bat_w    = 0.0
        self._last_time     = self.datetime()
        self._pending_handle = None

        # RC triggers: switch-wissel → 2s vertraging
        self.listen_state(self.on_switch_change, RC_CHARGE_SWITCH)
        self.listen_state(self.on_switch_change, RC_DISCHARGE_SWITCH)

        # RC triggers: power-update → 0.5s vertraging (als switch al aan)
        self.listen_state(self.on_power_change, RC_CHARGE_POWER)
        self.listen_state(self.on_power_change, RC_DISCHARGE_POWER)

        # SOC-integratie: elke 10 seconden
        self.run_every(self._integrate_soc, self.datetime(), 10)

        # Handmatige SOC-override
        self.listen_state(self.on_soc_set, SOC_SET_INPUT)

        # Initiële publicatie
        self._publish_soc()
        self.set_state("sensor.batsim_grid_port_power",
            state="0", replace=True,
            attributes={"friendly_name": "BatSim AC grid port", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.log(f"[BatterySim] gestart, SOC={self._soc:.1f}%")

    # ── RC triggers ───────────────────────────────────────────────────────────

    def on_switch_change(self, entity, attribute, old, new, kwargs):
        """Switch aan/uit → 2s vertraging voor AC port update."""
        self._schedule_apply(DELAY_SWITCH_S)

    def on_power_change(self, entity, attribute, old, new, kwargs):
        """Power gewijzigd → 0.5s vertraging, alleen als betreffende switch al aan is."""
        if entity == RC_CHARGE_POWER and self.get_state(RC_CHARGE_SWITCH) == "on":
            self._schedule_apply(DELAY_POWER_S)
        elif entity == RC_DISCHARGE_POWER and self.get_state(RC_DISCHARGE_SWITCH) == "on":
            self._schedule_apply(DELAY_POWER_S)

    def _schedule_apply(self, delay):
        """Plan een AC port update; annuleer eerder geplande update."""
        if self._pending_handle is not None:
            try:
                self.cancel_timer(self._pending_handle)
            except Exception:
                pass
        self._pending_handle = self.run_in(self._apply_rc, delay)

    def _apply_rc(self, kwargs):
        """Verwerk huidig RC-commando naar AC port output."""
        self._pending_handle = None
        battery_w = self._clamp_by_soc(self._read_rc())
        self._last_bat_w = battery_w
        self.set_state("sensor.batsim_grid_port_power",
            state=str(round(battery_w)), replace=True,
            attributes={"friendly_name": "BatSim AC grid port", "unit_of_measurement": "W",
                        "state_class": "measurement"})

    # ── SOC integratie ────────────────────────────────────────────────────────

    def _integrate_soc(self, kwargs):
        """Periodieke SOC-integratie op basis van _last_bat_w."""
        now = self.datetime()
        dt_hours = (now - self._last_time).total_seconds() / 3600.0
        self._last_time = now
        if dt_hours > 0 and abs(self._last_bat_w) > 0:
            self._update_soc(self._last_bat_w, dt_hours)
        self._publish_soc()

    def on_soc_set(self, entity, attribute, old, new, kwargs):
        """Handmatige SOC-override via input_number slider."""
        try:
            value = float(new)
        except (TypeError, ValueError):
            return
        # Skip eigen writes vanuit _publish_soc (afwijking < 0.05% = onze eigen update)
        if abs(value - self._soc) < 0.05:
            return
        self._soc = max(BATTERY_MIN_SOC, min(BATTERY_MAX_SOC, value))
        self._publish_soc()
        self.log(f"[BatterySim] SOC handmatig ingesteld op {self._soc:.1f}%")

    # ── Interne helpers ───────────────────────────────────────────────────────

    def _read_rc(self):
        """Vertaal RC-switches + power naar battery_w."""
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

    def _publish_soc(self):
        self.set_state("sensor.batsim_soc_pct",
            state=str(round(self._soc, 1)), replace=True,
            attributes={"friendly_name": "BatSim SOC", "unit_of_measurement": "%",
                        "device_class": "battery", "state_class": "measurement"})
        # Persisteer naar input_number zodat de waarde HA herstart overleeft
        self.call_service("input_number/set_value",
            entity_id=SOC_SET_INPUT, value=round(self._soc, 1))
