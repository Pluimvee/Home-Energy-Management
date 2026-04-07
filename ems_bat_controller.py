"""
ems_bat_controller.py – Battery Levelling Controller
=====================================================
AppDaemon app. Triggert op state-change van sensor.ecogrid_connect_power.

Leest P1 en sensor.strategy_battery, berekent het gewenste batterijvermogen
via feedforward, en stuurt de RC-interface aan.

Feedforward principe (≡ ESPHome productie):
  Alleen T-1 data wordt gebruikt voor de stuurberekening:
    p1_estimate(T) = grid_result(T-1) + battery_w(T-1)
  grid_result(T-1) wordt intern opgeslagen als:
    grid_result = p1_clean - battery_w
  Dit maakt de controller onafhankelijk van instantane sensor-reads,
  en is daardoor direct porteerbaar naar ESPHome.

p1_clean (BatSim fase):
  p1_clean = ecogrid + solis_ac_grid_port - batsim_grid_port_power
  Verwijdert de echte Solis uit P1 en voegt BatSim toe.

p1_clean (productie, echte Solis als RC-slave):
  p1_clean = ecogrid
  Beide correctietermen vervallen.

RC-interface (configureerbaar via constanten onderaan):
  Standaard: BatSim input_boolean/input_number helpers
  Productie:  switch.rc_force_* + number.solis_s6_eh3p_rc_force_*

Monitoring sensoren (gerecorded door HA):
  sensor.batsim_p1_w          Gecorrigeerde P1 / huislastschatting (W)
  sensor.batsim_target_w      Grid target huidig uur (W)
  sensor.batsim_target_soc    SOC-doel einde huidig uur (%)
  sensor.batsim_battery_w     Gestuurd batterijvermogen (W, + ontladen / - laden)
  sensor.batsim_grid_w        Grid resultaat na sturing (W)
  sensor.batsim_mode          charging / discharging / standby (expliciete strategy)
                              auto_charging / auto_discharging / auto_standby (level mode)
"""

import datetime

import appdaemon.plugins.hass.hassapi as hass

# ── Batterij parameters ────────────────────────────────────────────────────────
BATTERY_CAPACITY_KWH = 20.0
BATTERY_USABLE       = 0.93
BATTERY_EFFICIENCY   = 0.97
BATTERY_MIN_SOC      = 7.0
BATTERY_MAX_SOC      = 100.0
BATTERY_MAX_W        = 10000
BATTERY_MIN_W        = 100      # W dode zone

# ── Vaste sensoren ─────────────────────────────────────────────────────────────
P1_SENSOR        = "sensor.ecogrid_connect_power"
SOLIS_PORT_SENSOR = "sensor.solis_s6_eh3p_ac_grid_port_power"   # echte Solis (in BatSim-fase)
STRATEGY_SENSOR  = "sensor.strategy_battery"

# ── Uitwisselbare entiteiten: BatSim ↔ echte Solis ────────────────────────────
# BatSim fase:
SOC_SENSOR          = "sensor.batsim_soc_pct"
BATSIM_PORT_SENSOR  = "sensor.batsim_grid_port_power"
RC_CHARGE_SWITCH    = "input_boolean.batsim_force_charge"
RC_DISCHARGE_SWITCH = "input_boolean.batsim_force_discharge"
RC_CHARGE_POWER     = "input_number.batsim_charge_power"
RC_DISCHARGE_POWER  = "input_number.batsim_discharge_power"
# Productie (vervang bovenstaande door):
# SOC_SENSOR          = "sensor.solis_s6_eh3p_battery_soc"
# BATSIM_PORT_SENSOR  = None   (correctieterm niet nodig)
# RC_CHARGE_SWITCH    = "switch.rc_force_battery_charge"
# RC_DISCHARGE_SWITCH = "switch.rc_force_battery_discharge"
# RC_CHARGE_POWER     = "number.solis_s6_eh3p_rc_force_battery_charge_power"
# RC_DISCHARGE_POWER  = "number.solis_s6_eh3p_rc_force_battery_discharge_power"


class EmsBatController(hass.Hass):

    def initialize(self):
        self._last_grid_result = None   # W; None = eerste cyclus, geen feedforward beschikbaar
        self._last_battery_w   = 0.0

        self.listen_state(self.on_p1_change, P1_SENSOR)
        self.log("[BatController] gestart")

    def on_p1_change(self, entity, attribute, old, new, kwargs):
        try:
            ecogrid = float(new)
        except (TypeError, ValueError):
            return

        # ── p1_clean: huislast met BatSim als enige batterij ─────────────────
        # Verwijder echte Solis uit P1 en voeg BatSim toe.
        # In productie (BATSIM_PORT_SENSOR=None): p1_clean = ecogrid.
        try:
            solis_port = float(self.get_state(SOLIS_PORT_SENSOR) or 0)
        except (TypeError, ValueError):
            solis_port = 0.0
        try:
            batsim_port = float(self.get_state(BATSIM_PORT_SENSOR) or 0) if BATSIM_PORT_SENSOR else 0.0
        except (TypeError, ValueError):
            batsim_port = 0.0

        p1_clean = ecogrid + solis_port - batsim_port

        # ── Feedforward: stuur op T-1 schatting, niet op actuele P1 ──────────
        if self._last_grid_result is None:
            p1_input = p1_clean   # eerste cyclus: geen T-1 beschikbaar
        else:
            p1_input = self._last_grid_result + self._last_battery_w

        # ── Strategie lezen ───────────────────────────────────────────────────
        strategy   = self._get_strategy()
        strat_mode = strategy["mode"]
        target_w   = strategy["grid_target_w"]
        soc_after  = strategy["soc_target"]

        # ── SOC lezen ─────────────────────────────────────────────────────────
        try:
            soc = float(self.get_state(SOC_SENSOR) or BATTERY_MIN_SOC)
        except (TypeError, ValueError):
            soc = BATTERY_MIN_SOC

        # ── Batterijvermogen berekenen ─────────────────────────────────────────
        now    = self.datetime()
        usable = BATTERY_CAPACITY_KWH * BATTERY_USABLE
        battery_w = self._compute_battery_w(strat_mode, p1_input, target_w, soc, soc_after, now, usable)

        # ── Feedforward state opslaan voor volgende cyclus ────────────────────
        grid_result = round(p1_clean - battery_w)
        self._last_grid_result = grid_result
        self._last_battery_w   = battery_w

        # ── RC-interface aansturen ────────────────────────────────────────────
        self._set_rc(battery_w)

        # ── Monitoring sensoren publiceren ────────────────────────────────────
        self._publish(
            p1_w       = round(p1_clean),
            target_w   = target_w,
            target_soc = round(soc_after, 1),
            battery_w  = round(battery_w),
            grid_w     = grid_result,
            mode       = self._mode_label(strat_mode, battery_w),
        )

    # ── Batterijvermogen berekening ────────────────────────────────────────────

    def _compute_battery_w(self, strat_mode, p1_w, target_w, soc, soc_after, now, usable):
        if strat_mode == "hold":
            return 0.0

        elif strat_mode == "level":
            raw = p1_w - target_w
            if abs(raw) < BATTERY_MIN_W:
                raw = 0.0
            battery_w = max(-BATTERY_MAX_W, min(BATTERY_MAX_W, raw))
            if battery_w > 0:
                battery_w = min(battery_w, max(0.0, p1_w))  # anti-export
            return self._clamp_by_soc(battery_w, soc)

        elif strat_mode == "charge":
            next_hour   = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
            remaining_h = max(1.0 / 360.0, (next_hour - now).total_seconds() / 3600.0)
            delta_soc   = soc_after - soc
            if delta_soc > 0:
                battery_w = -(delta_soc * usable) / (100.0 * remaining_h * BATTERY_EFFICIENCY) * 1000
            else:
                battery_w = min(0.0, p1_w - target_w)
            if abs(battery_w) < BATTERY_MIN_W:
                battery_w = 0.0
            battery_w = max(-BATTERY_MAX_W, min(0.0, battery_w))
            battery_w = self._clamp_by_soc(battery_w, soc)
            # Floor-guard: grid_w >= target_w
            if p1_w - battery_w < target_w:
                battery_w = min(0.0, p1_w - target_w)
                battery_w = self._clamp_by_soc(battery_w, soc)
            return battery_w

        elif strat_mode == "discharge":
            next_hour   = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
            remaining_h = max(1.0 / 360.0, (next_hour - now).total_seconds() / 3600.0)
            delta_soc   = soc - soc_after
            if delta_soc > 0:
                battery_w = (delta_soc * usable * BATTERY_EFFICIENCY) / (100.0 * remaining_h) * 1000
            else:
                battery_w = max(0.0, p1_w - target_w)
            if abs(battery_w) < BATTERY_MIN_W:
                battery_w = 0.0
            battery_w = max(0.0, min(BATTERY_MAX_W, battery_w))
            battery_w = self._clamp_by_soc(battery_w, soc)
            # Ceiling-guard: grid_w <= target_w
            if p1_w - battery_w > target_w:
                battery_w = max(0.0, p1_w - target_w)
                battery_w = self._clamp_by_soc(battery_w, soc)
            # Anti-export
            battery_w = min(battery_w, max(0.0, p1_w))
            return battery_w

        else:  # export
            if soc <= soc_after + 0.1:
                return 0.0
            raw = p1_w - target_w
            battery_w = max(0.0, raw)
            if abs(battery_w) < BATTERY_MIN_W:
                battery_w = 0.0
            return self._clamp_by_soc(min(battery_w, BATTERY_MAX_W), soc)

    def _clamp_by_soc(self, battery_w, soc):
        if battery_w > 0 and soc <= BATTERY_MIN_SOC + 0.1:
            return 0.0
        if battery_w < 0 and soc >= BATTERY_MAX_SOC - 0.1:
            return 0.0
        return battery_w

    def _mode_label(self, strat_mode, battery_w):
        if strat_mode == "level":
            if battery_w >= BATTERY_MIN_W:
                return "auto_discharging"
            elif battery_w <= -BATTERY_MIN_W:
                return "auto_charging"
            else:
                return "auto_standby"
        elif strat_mode == "charge":
            return "charging" if abs(battery_w) >= BATTERY_MIN_W else "standby"
        elif strat_mode == "discharge":
            return "discharging" if abs(battery_w) >= BATTERY_MIN_W else "standby"
        elif strat_mode == "export":
            return "exporting" if battery_w >= BATTERY_MIN_W else "standby"
        else:
            return "standby"

    # ── RC-interface aansturen ─────────────────────────────────────────────────

    def _set_rc(self, battery_w):
        """Vertaal battery_w naar RC-commando's. Geldt voor BatSim én echte Solis."""
        if battery_w >= BATTERY_MIN_W:
            # Ontladen
            self.call_service("input_number/set_value",
                entity_id=RC_DISCHARGE_POWER, value=round(battery_w))
            self.call_service("input_boolean/turn_on",  entity_id=RC_DISCHARGE_SWITCH)
            self.call_service("input_boolean/turn_off", entity_id=RC_CHARGE_SWITCH)
        elif battery_w <= -BATTERY_MIN_W:
            # Laden
            self.call_service("input_number/set_value",
                entity_id=RC_CHARGE_POWER, value=round(abs(battery_w)))
            self.call_service("input_boolean/turn_on",  entity_id=RC_CHARGE_SWITCH)
            self.call_service("input_boolean/turn_off", entity_id=RC_DISCHARGE_SWITCH)
        else:
            # Standby
            self.call_service("input_boolean/turn_off", entity_id=RC_CHARGE_SWITCH)
            self.call_service("input_boolean/turn_off", entity_id=RC_DISCHARGE_SWITCH)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_strategy(self):
        try:
            mode      = self.get_state(STRATEGY_SENSOR) or "level"
            target    = self.get_state(STRATEGY_SENSOR, attribute="grid_target_w")
            soc_after = self.get_state(STRATEGY_SENSOR, attribute="soc_target")
            valid     = ("level", "hold", "charge", "discharge", "export")
            return {
                "mode":          mode if mode in valid else "level",
                "grid_target_w": int(target)      if target    is not None else 200,
                "soc_target":    float(soc_after) if soc_after is not None else BATTERY_MIN_SOC,
            }
        except Exception:
            return {"mode": "level", "grid_target_w": 200, "soc_target": BATTERY_MIN_SOC}

    def _publish(self, p1_w, target_w, target_soc, battery_w, grid_w, mode):
        self.set_state("sensor.batsim_p1_w",
            state=str(p1_w), replace=True,
            attributes={"friendly_name": "BatSim P1 meting", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.set_state("sensor.batsim_target_w",
            state=str(target_w), replace=True,
            attributes={"friendly_name": "BatSim grid target", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.set_state("sensor.batsim_target_soc",
            state=str(target_soc), replace=True,
            attributes={"friendly_name": "BatSim SOC doel", "unit_of_measurement": "%",
                        "device_class": "battery", "state_class": "measurement"})
        self.set_state("sensor.batsim_battery_w",
            state=str(battery_w), replace=True,
            attributes={"friendly_name": "BatSim batterijvermogen", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.set_state("sensor.batsim_grid_w",
            state=str(grid_w), replace=True,
            attributes={"friendly_name": "BatSim grid resultaat", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.set_state("sensor.batsim_mode",
            state=mode, replace=True,
            attributes={"friendly_name": "BatSim mode"})
