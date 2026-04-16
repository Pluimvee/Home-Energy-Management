"""
ems_bat_controller.py – Battery Target-Grid Controller
=======================================================

Deze code zal op termijn in een ESPHome worden geïmplementeerd en rechtstreeks tussen de P1 poort en de Solis omvormer zitten.
In die opzet zal de ESPHome elke seconde een P1 package krijgen. Eke 10s zal daar nieuwe meetwaardes inzitten. Op voorhand (bijvoorbeeld bij pakketje 8) 
kan de ESPHome de waardes uit de solis lezen via de rechtstreeks RS485 interface. Waneer de nieuwe P1 waardes binnen zijn kan de ESP dan de nieuwe stuurwaades berekenen en naar de Solis sturen.
Al met al verwacht ik een snellere regellus.

Voor nu is dit een AppDaemon-app om de strategie en controller te ontwikkelen en te testen in de BatSim-omgeving.
In tweede stap kan deze controller app ook de echte Solis S6 EH3P in productie aansturen.
In derde stap zal de logica in de ESPHome worden geïmplementeerd, waarbij de AppDaemon-app als grondwaarheid en testomgeving blijft dienen.

AppDaemon app. Eén handler per P1 event.

Tekenconventies:
  p1_reader    > 0 = import      solis_ac   > 0 = discharge
  battery_ac   > 0 = discharge   battery_w  > 0 = ontladen / < 0 = laden

Mode + target controller:
  mode = charge    → alleen laden; force_discharge UIT
                     stuur op target_soc aan het einde van het huidige uur
                     cap requested_power ≤ 0 (nooit discharge)
  mode = level     → bidirectioneel; stuur grid naar target_grid
                     integreer: requested_power += GAIN_LEVEL × (grid − target_grid)
  mode = discharge → alleen ontladen; force_charge UIT
                     integreer: requested_power += GAIN_UP × (grid − target_grid)
                     cap requested_power ≥ 0 (nooit charge)
  mode = hold      → beide switches UIT; requested_power = 0
  mode = auto      → bepaal zelf target_grid en gebruik daarna level

Mode-wissel reset requested_power naar 0 om integrator-overshoot te voorkomen.

RC-interface:
  Standaard: BatSim input_boolean/input_number helpers
  Productie:  switch.rc_force_* + number.solis_s6_eh3p_rc_force_*

Monitoring sensoren:
  sensor.batsim_p1_w          Huislast (W) = p1_reader + solis_ac
  sensor.batsim_grid_w        Virtueel grid resultaat (W) = huislast - battery_ac
  sensor.batsim_target_grid_w Target grid (W, van strategy)
  sensor.batsim_target_soc    Doel-SOC einde uur (%)
  sensor.batsim_mode          charging / discharging / standby
"""

import appdaemon.plugins.hass.hassapi as hass

# ── Batterij parameters ────────────────────────────────────────────────────────
BATTERY_MIN_SOC  = 7.0
BATTERY_MAX_SOC  = 100.0
BATTERY_MAX_W    = 10000
BATTERY_MIN_W    = 100      # W dode zone BatSim
SOLIS_DEADBAND_W = 100      # W eigen verbruik omvormer
BATTERY_CAPACITY_KWH = 20.0
BATTERY_USABLE       = 0.93
BATTERY_EFFICIENCY   = 0.97

# ── Controller gains ───────────────────────────────────────────────────────────
# Fractie van het foutsignaal dat per P1-cyclus (~10s) wordt gecorrigeerd.
# GAIN_UP:    discharge mode (traag; grid-spikes mogen ons niet plotseling in
#             discharge mode knallen)
# GAIN_LEVEL: level mode (bidirectioneel rond target_grid)
GAIN_UP    = 0.3
GAIN_LEVEL = 0.3

# ── Fallback als strategy niet beschikbaar is ─────────────────────────────────
GRID_DEFAULT_TARGET = 50     # W
GRID_DEFAULT_MODE   = "level"
AUTO_OVERRIDE_ENTITY = "input_boolean.batsim_controller_auto"
HOUSEHOLD_TOTAL_ENERGY_SENSOR = "sensor.p1_reader_household_total_energy"
AUTO_SOC_BIAS_W_PER_PCT = 40.0
AUTO_SOC_TARGET = 50.0
AUTO_TARGET_MIN_W = 50
AUTO_TARGET_MAX_W = 4000

# ── Sensoren ───────────────────────────────────────────────────────────────────
P1_SENSOR         = "sensor.p1_reader_power"
SOLIS_PORT_SENSOR = "sensor.solis_s6_eh3p_ac_grid_port_power"
STRATEGY_SENSOR   = "sensor.strategy_battery"
HOUSEHOLD_SENSOR   = "sensor.batsim_p1_w"    # monitoring only
GRID_RESULT_SENSOR = "sensor.batsim_grid_w"  # monitoring only

# ── BatSim ↔ echte Solis (uitwisselbaar) ──────────────────────────────────────
SOC_SENSOR          = "sensor.batsim_soc_pct"
BATTERY_AC_SENSOR   = "sensor.batsim_grid_port_power"
RC_CHARGE_SWITCH    = "input_boolean.batsim_force_charge"
RC_DISCHARGE_SWITCH = "input_boolean.batsim_force_discharge"
RC_CHARGE_POWER     = "input_number.batsim_charge_power"
RC_DISCHARGE_POWER  = "input_number.batsim_discharge_power"
# Productie:
# SOC_SENSOR          = "sensor.solis_s6_eh3p_battery_soc"
# BATTERY_AC_SENSOR   = SOLIS_PORT_SENSOR
# RC_CHARGE_SWITCH    = "switch.rc_force_battery_charge"
# RC_DISCHARGE_SWITCH = "switch.rc_force_battery_discharge"
# RC_CHARGE_POWER     = "number.solis_s6_eh3p_rc_force_battery_charge_power"
# RC_DISCHARGE_POWER  = "number.solis_s6_eh3p_rc_force_battery_discharge_power"


class EmsBatController(hass.Hass):

    def initialize(self):
        self._requested_power = 0.0   # W; battery conventie: negatief = laden
        self._prev_mode       = None  # detecteer mode-wissel → reset integrator
        self._auto_day        = None
        self._auto_day_start_kwh = None

        # Hele keten triggert alleen op P1 events (grondwaarheid).
        self.listen_state(self._on_p1_event, P1_SENSOR)

        self.log("[BatController] gestart")

    # ── P1 event: volledige berekeningscyclus ─────────────────────────────────

    def _on_p1_event(self, entity, attribute, old, new, kwargs):
        self._cycle()

    def _cycle(self):
        """Één atomaire cyclus per P1 event."""
        # 1. Echte huislast (monitoring only)
        try:
            ecogrid = float(self.get_state(P1_SENSOR) or 0)
        except (TypeError, ValueError):
            return
        try:
            solis_port = float(self.get_state(SOLIS_PORT_SENSOR) or 0)
        except (TypeError, ValueError):
            solis_port = 0.0
        if abs(solis_port) < SOLIS_DEADBAND_W:
            solis_port = 0.0
        real_household_w = ecogrid + solis_port

        # 2. AC-vermogen van de batterij/inverter-port.
        # In sim is dit de BatSim-port; in productie de echte Solis AC-port.
        try:
            battery_ac = float(self.get_state(BATTERY_AC_SENSOR) or 0) if BATTERY_AC_SENSOR else 0.0
        except (TypeError, ValueError):
            battery_ac = 0.0

        # 3. Grid resultaat
        grid_w = real_household_w - battery_ac

        # 4. Strategie en SOC
        strategy    = self._get_strategy()
        mode        = strategy["mode"]
        target_grid = strategy["target_grid_w"]
        target_soc  = strategy["target_soc"]

        try:
            soc = float(self.get_state(SOC_SENSOR) or BATTERY_MIN_SOC)
        except (TypeError, ValueError):
            soc = BATTERY_MIN_SOC

        if mode == "auto":
            target_grid = self._auto_target_grid(soc)
            mode = "level"

        # 5. Mode-wissel: reset integrator om overshoot te voorkomen
        if mode != self._prev_mode:
            self._requested_power = 0.0
            self._prev_mode = mode

        # 6. Integrerende controller — richting bepaald door mode
        if mode == "charge":
            # Charge is SOC-led: hit het target_soc aan het einde van het huidige uur.
            self._requested_power = self._charge_request_for_target_soc(soc, target_soc)
        elif mode == "level":
            # Level is bidirectioneel: stuur grid rond target_grid.
            self._requested_power += GAIN_LEVEL * (grid_w - target_grid)
        elif mode == "discharge":
            # Ontlaad totdat grid ≈ target_grid; nooit laden
            self._requested_power += GAIN_UP * (grid_w - target_grid)
            self._requested_power  = max(0.0, self._requested_power)   # nooit negatief
        else:  # hold
            self._requested_power = 0.0

        self._requested_power = max(-BATTERY_MAX_W, min(float(BATTERY_MAX_W), self._requested_power))

        battery_w = self._compute_battery_w(self._requested_power, soc)
        self._set_rc(battery_w, mode)

        # 7. Monitoring
        self.set_state(HOUSEHOLD_SENSOR,
            state=str(round(real_household_w)), replace=True,
            attributes={"friendly_name": "BatSim huislast (referentie)", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.set_state(GRID_RESULT_SENSOR,
            state=str(round(grid_w)), replace=True,
            attributes={"friendly_name": "BatSim grid resultaat", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.set_state("sensor.batsim_target_grid_w",
            state=str(target_grid), replace=True,
            attributes={"friendly_name": "BatSim target grid", "unit_of_measurement": "W",
                        "state_class": "measurement"})
        self.set_state("sensor.batsim_target_soc",
            state=str(round(target_soc, 1)), replace=True,
            attributes={"friendly_name": "BatSim target SOC", "unit_of_measurement": "%",
                        "device_class": "battery", "state_class": "measurement"})
        self.set_state("sensor.batsim_mode",
            state=self._mode_label(battery_w), replace=True,
            attributes={"friendly_name": "BatSim mode"})

    # ── Batterijvermogen berekening ────────────────────────────────────────────

    def _compute_battery_w(self, requested_power, soc):
        """Past deadband en SOC clamp toe op requested_power."""
        if abs(requested_power) < BATTERY_MIN_W:
            return 0.0
        return self._clamp_by_soc(
            max(-BATTERY_MAX_W, min(float(BATTERY_MAX_W), requested_power)), soc)

    def _charge_request_for_target_soc(self, soc, target_soc):
        """Bereken het laadvermogen om target_soc aan het einde van dit uur te halen."""
        try:
            target_soc = float(target_soc)
        except (TypeError, ValueError):
            return 0.0

        target_soc = max(BATTERY_MIN_SOC, min(BATTERY_MAX_SOC, target_soc))
        delta_soc = target_soc - soc
        if delta_soc <= 0.05:
            return 0.0

        now = self.datetime()
        seconds_left = 3600.0 - (now.minute * 60 + now.second + now.microsecond / 1_000_000)
        seconds_left = max(1.0, seconds_left)
        hours_left = seconds_left / 3600.0

        usable_kwh = BATTERY_CAPACITY_KWH * BATTERY_USABLE
        required_w = (
            (delta_soc / 100.0) * usable_kwh * 1000.0
            / hours_left
            / BATTERY_EFFICIENCY
        )
        return -min(BATTERY_MAX_W, required_w)

    def _auto_target_grid(self, soc):
        """
        Bepaal target_grid voor default controllerbedrijf zonder strategy.

        Basis: projecteer het huishoudverbruik van vandaag naar een gemiddeld
        continu vermogen voor de rest van de dag.
        SOC bias: hoge SOC verlaagt target_grid (meer ontladen), lage SOC
        verhoogt target_grid (meer import / minder ontladen).
        """
        now = self.datetime()
        total_kwh = self._read_float(HOUSEHOLD_TOTAL_ENERGY_SENSOR)
        if total_kwh is None:
            return GRID_DEFAULT_TARGET

        day_key = now.date().isoformat()
        if self._auto_day != day_key or self._auto_day_start_kwh is None:
            self._auto_day = day_key
            self._auto_day_start_kwh = total_kwh

        today_kwh = max(0.0, total_kwh - self._auto_day_start_kwh)
        elapsed_hours = max(1.0 / 60.0, now.hour + now.minute / 60.0 + now.second / 3600.0)
        avg_w = (today_kwh / elapsed_hours) * 1000.0

        soc_bias = (AUTO_SOC_TARGET - soc) * AUTO_SOC_BIAS_W_PER_PCT
        target = avg_w + soc_bias
        return int(max(AUTO_TARGET_MIN_W, min(AUTO_TARGET_MAX_W, round(target))))

    def _clamp_by_soc(self, battery_w, soc):
        if battery_w > 0 and soc <= BATTERY_MIN_SOC + 0.1:
            return 0.0
        if battery_w < 0 and soc >= BATTERY_MAX_SOC - 0.1:
            return 0.0
        return battery_w

    def _mode_label(self, battery_w):
        if battery_w >= BATTERY_MIN_W:
            return "discharging"
        elif battery_w <= -BATTERY_MIN_W:
            return "charging"
        else:
            return "standby"

    # ── RC-interface ──────────────────────────────────────────────────────────

    def _set_rc(self, battery_w, mode):
        """
        Vertaal battery_w naar RC-commando's.

        Mode bepaalt welke switch actief mag zijn:
          charge    → force_charge kan aan,    force_discharge altijd uit
          level     → force_charge / force_discharge volgen de richting van battery_w
          discharge → force_discharge kan aan, force_charge    altijd uit
          hold      → beide switches uit

        Power is altijd ≥ 0 (richting via switch, niet via sign).
        """
        if mode == "charge":
            force_charge    = battery_w < -BATTERY_MIN_W
            force_discharge = False
            charge_power    = max(0.0, -battery_w)
            discharge_power = 0.0
        elif mode == "level":
            force_charge    = battery_w < -BATTERY_MIN_W
            force_discharge = battery_w > BATTERY_MIN_W
            charge_power    = max(0.0, -battery_w)
            discharge_power = max(0.0, battery_w)
        elif mode == "discharge":
            force_charge    = False
            force_discharge = battery_w > BATTERY_MIN_W
            charge_power    = 0.0
            discharge_power = max(0.0, battery_w)
        else:  # hold
            force_charge    = False
            force_discharge = False
            charge_power    = 0.0
            discharge_power = 0.0

        # Powers eerst — switch-changes annuleren lopende power-timers in de sim
        self.call_service("input_number/set_value",
            entity_id=RC_CHARGE_POWER,    value=round(charge_power))
        self.call_service("input_number/set_value",
            entity_id=RC_DISCHARGE_POWER, value=round(discharge_power))

        # Switches
        self.call_service(
            "input_boolean/turn_on" if force_charge else "input_boolean/turn_off",
            entity_id=RC_CHARGE_SWITCH)
        self.call_service(
            "input_boolean/turn_on" if force_discharge else "input_boolean/turn_off",
            entity_id=RC_DISCHARGE_SWITCH)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_strategy(self):
        try:
            auto_override = self.get_state(AUTO_OVERRIDE_ENTITY)
            if str(auto_override).lower() == "on":
                return {
                    "mode": "auto",
                    "target_grid_w": GRID_DEFAULT_TARGET,
                    "target_soc": BATTERY_MIN_SOC,
                }
            target = self.get_state(STRATEGY_SENSOR, attribute="target_grid_w")
            mode   = self.get_state(STRATEGY_SENSOR, attribute=None)   # state = mode string
            target_soc = self.get_state(STRATEGY_SENSOR, attribute="target_soc")
            return {
                "mode":          str(mode)    if mode   is not None else GRID_DEFAULT_MODE,
                "target_grid_w": int(target)  if target is not None else GRID_DEFAULT_TARGET,
                "target_soc":    float(target_soc) if target_soc is not None else BATTERY_MIN_SOC,
            }
        except Exception:
            return {"mode": GRID_DEFAULT_MODE, "target_grid_w": GRID_DEFAULT_TARGET,
                    "target_soc": BATTERY_MIN_SOC}

    def _read_float(self, entity_id):
        try:
            value = self.get_state(entity_id)
            return float(value) if value not in (None, "unknown", "unavailable", "") else None
        except (TypeError, ValueError):
            return None
