# EMS Project Specificatie

## Omgeving
- Productie versie AppDaemon apps op `Z:\a0d7b954_appdaemon\apps\` (Samba-share)
- HA MCP voor directe toegang tot sensoren en services
- Wijzigingen gaan rechtstreeks in productie; AppDaemon restart bij code-wijzigingen
- Bij een stabiele versie wordt in opdracht of op jouw aanraden de files uit productie in dit project gekopieerd, als backup maar ook voor GitHub version control
---

## Forecasts

Alle forecasts beslaan **36 uur** (huidig uur + 35 uur vooruit), triggers: Nordpool-update, weather-update, dagwisseling (00:05), elk uur (:00:05).

| Sensor | Eenheid | Bron |
|---|---|---|
| `sensor.forecast_epex` | EUR/kWh | Nordpool 15-min → avg per uur |
| `sensor.forecast_irradiance` | W/m² | Open-Meteo weather |
| `sensor.forecast_outside_temp` | °C | Open-Meteo weather |
| `sensor.forecast_pv` | kWh | Irradiantie × `pv_eta` (per uur-van-dag, gecalibreerd) |
| `sensor.forecast_thermic` | kWh | Thermische vraag (K-model) + solar_gain attribuut |
| `sensor.forecast_heatpump` | kWh | Thermic / COP; COP = `cop_a` + `cop_b` × T_buiten; default COP(0°C)=3.5, slope=0.08/°C |
| `sensor.forecast_household_energy` | kWh | Gecalibreerd basispatroon per uur-van-dag; fallback 0.4 kWh/h |

---

## Statistics window

Geldt voor **alle** forecasts:

| Parameter | Waarde |
|---|---|
| Past values | 5 uren (uit HA recorder) |
| Huidig uur | 1 |
| Forecast | huidig uur + max 18 uur vooruit |
| **Totaal window** | **min 12 – max 24 uren** |
| Berekende stats | `min`, `max`, `avg`, `median`, `analysis_n` |
| Per entry | `pct` = positie in [min–max] range, 0.001–1.0 |

---

## EPEX Prijsanalyse

**TP-detectie drempels** (`ems_epex.py`):

| Constante | Waarde | Betekenis |
|---|---|---|
| `MIN_AMP` | 0.25 | TP moet >25% van spread afwijken van dichtstbijzijnde tegengestelde TP |
| `BAND_MAX` | 0.15 | Segmentbreedte = amplitude × 0.15 × spread |
| `TROUGH_MAX` | 0.20 | Valley met pct < 0.20 → `trough`; ≥ 0.20 → `dip` |
| `PEAK_MIN` | 0.80 | Peak met pct > 0.80 → `peak`; ≤ 0.80 → `crest` |

**Labels & tiers**:

| Tier | Label | Conditie | Strategie |
|---|---|---|---|
| 0 | `negative` | prijs < 0 | Level (PV absorptie); laatste 1–2 uur charge naar 100% |
| 1 | `trough` | valley TP, pct < 0.20 | Charge naar 100% (TP-gewogen); gecapped op PV headroom |
| 2 | `dip` | valley TP, pct ≥ 0.20 | Charge naar survival SOC (TP-gewogen) |
| 3 | `neutral` | geen TP | Level (pct < 0.5) of discharge (pct ≥ 0.5) |
| 4 | `crest` | peak TP, pct ≤ 0.80 | Discharge (pct-gewogen) |
| 5 | `peak` | peak TP, pct > 0.80 | Discharge (pct-gewogen) |

---

## Batterij real-time aansturing (BatSim / ESPHome)

De controller krijgt elk uur twee parameters van de strategy:
- `mode` — bepaalt welke richting is toegestaan
- `target_grid_w` — setpoint voor de proportionele regelaar

| Mode | Richting | Gedrag |
|------|----------|--------|
| **charge** | laden only | integreer naar target_grid; force_discharge altijd uit |
| **discharge** | ontladen only | integreer naar target_grid; force_charge altijd uit |
| **hold** | geen | beide switches uit; requested_power = 0 |

Controller (proportioneel, per P1-event ~10s):
- `charge`:    `requested_power -= GAIN_DOWN × (target_grid − grid_w)` ; cap ≤ 0
- `discharge`: `requested_power += GAIN_UP   × (grid_w − target_grid)` ; cap ≥ 0
- Mode-wissel reset `requested_power` naar 0 (geen integrator-overshoot)

Tekenconventie:
- `battery_w > 0` → ontladen (batterij levert, grid daalt)
- `battery_w < 0` → laden (batterij neemt van grid, grid stijgt)
- `grid_w = household_w − battery_w`

### Signaalschema

```
┌─────────────────┐   grid_w (10s)    ┌──────────────────────────────┐
│  DSMR P1 meter  │ ────────────────→ │                              │
└─────────────────┘                   │  ESPHome / BatSim            │──→ Solis: mode + W
                                      │                              │
┌─────────────────┐  mode             │  battery_w =                 │
│ sensor.strategy │  target_grid_w  → │    proportioneel(            │
│    _battery     │   target_soc      │    grid_w, target, mode)     │
└─────────────────┘  (elk uur)        │                              │
                                      │  SOC clamp: min 7% / max 100%│
┌─────────────────┐   SOC (10s)       │                              │
│  Solis SOC      │ ────────────────→ │                              │
└─────────────────┘                   └──────────────────────────────┘
```

**BatSim vs. ESPHome productie:**

| | BatSim (simulatie) | ESPHome (productie) |
|---|---|---|
| P1 input | `sensor.p1_reader_power` | `sensor.p1_reader_power` |
| Huislast | `p1_reader + solis_ac_port` | n.v.t. — ESPHome vervangt Solis-aansturing |
| SOC input | `sensor.batsim_soc_pct` | `sensor.solis_s6_eh3p_battery_soc` |
| Output | `input_boolean/number.batsim_*` | `switch/number.solis_s6_eh3p_rc_force_*` |

---

## Batterijstrategie (`sensor.strategy_battery`)

**Hardware** (Solis S6 EH3P):

| Parameter | Waarde |
|---|---|
| Capaciteit | 20.0 kWh |
| Bruikbaar | 93% = 18.6 kWh |
| Rendement | 97% (roundtrip) |
| Min SOC | 7% |
| Max SOC | 100% |
| Min vermogen | 50 W (dode zone) |
| Max vermogen | 10.000 W |

**Gepubliceerde attributen per uur** (in `forecast` lijst):

| Attribuut | Betekenis |
|---|---|
| `mode` | `charge` / `discharge` / `hold` |
| `target_grid_w` | Setpoint gridafname (W); negatief = export toegestaan |
| `target_soc` | Doel-SOC (%) aan het **einde** van het uur |

**Constanten**:

| Constante | Waarde | Gebruik |
|---|---|---|
| `GRID_MIN_W` | 50 W | Anti-export vloer; target bij hold/standby |
| `GRID_EXPORT_LIMIT` | 5000 W | Max export (min target_grid = −5000 W) |
| `TP_WEIGHT` | 3 | TP-uren krijgen 3× meer SOC-gain dan band-uren (tier 1) |

### Logica per tier

De gewenste eind-SOC per uur is leidend. `target_grid_w` volgt daaruit:
```
delta_soc   = soc_end − soc_begin  (positief = laden, negatief = ontladen)
battery_kw  = delta_soc / 100 × usable_kwh          (voor discharge: / efficiency)
target_grid = net_kw × 1000 + battery_kw × 1000     (net = hh + hp − pv)
```

**Tier 0 — `negative` — charge / level**
- Eind-SOC: 100% in het laatste actieve laaduur
- Niet-actieve uren binnen het segment: `level` op 0W
- Actieve laaduren: `charge`; TP-uur als ankerpunt, daarna goedkoopste aangrenzende uur (bij gelijke prijs voorkeur voor later uur)
- Ceiling: 100% (geen PV-headroom reductie bij tier 0/1)
- target_grid = net_W + laadvermogen_W — negatief bij PV-surplus (batterij absorbeert PV én grid)
- Start-SOC doel = 7%: dit wordt bereikt door de voorafgaande tier 4/5 drain (zie aldaar)

**Tier 1 — `trough` — charge / level**
- Eind-SOC: 100%
- Alleen uren die echt een SOC-stap dragen staan op `charge`
- Als het TP-uur het segmentdoel alleen kan halen, staan de omliggende trough-uren op `level`
- Als hulp nodig is, krijgt eerst het goedkoopste aangrenzende uur de SOC-gain; bij gelijke prijs het latere uur
- Ceiling: 100% (geen headroom reductie)
- target_grid = net_W + laadvermogen_W; negatief bij PV-surplus

**Tier 2 — `dip` — charge of discharge**
- Per uur twee survival-groottes:
  - `survival_start` = SOC nodig aan het BEGIN van dit uur om dit uur plus de resterende dip-uren te overleven tot het eerste toekomstige uur met `pct < pct_huidig` (goedkopere laadfase); bij ontbrekende pct: gehele horizon
  - `survival_end` = SOC nodig aan het EINDE van dit uur om vanaf het volgende uur diezelfde goedkopere laadfase te halen
- Ceiling: max_soc − verwacht PV-surplus na het segment (anti-export); begrenst beide survival-groottes
- SOC ≥ `survival_start` → level op 0W (batterij volgt netto verbruik bidirectioneel)
- SOC < `survival_start` → charge naar `survival_end`
- Hold bestaat niet meer; als geen lading nodig, wordt `level`

**Tier 3 — `neutral` — charge of discharge**
- Drie gevallen op basis van netto load en pct:

| Situatie | Mode | Eind-SOC | target_grid |
|----------|------|----------|-------------|
| net ≤ 0 (PV surplus) | charge | SOC + PV-absorptie × eff | GRID_MIN_W |
| net > 0, pct < 0.5 | charge | ongewijzigd (anti-export) | GRID_MIN_W |
| net > 0, pct ≥ 0.5 | discharge | ongewijzigd (SOC hold) | net × 1000 W |

- Als tier 0 volgt: PV-winst in tier-3 charge uren telt mee in drain-berekening voor tier 4/5

**Tier 4 — `crest` — discharge**
- Normaal (geen tier 0 volgt): batterij levert alle netto consumptie
  - Eind-SOC daalt met `net / usable × 100%` per uur
  - target_grid = 0W
- Als tier 0 volgt (waterfall, zie onder):
  - Eind-SOC doel = 7% aan het begin van tier 0
  - target_grid kan negatief worden (export)
- net ≤ 0: discharge mode, batterij standby; target_grid = 0W

**Tier 5 — `peak` — discharge**
- Identiek aan tier 4
- Krijgt eerste prioriteit in de waterfall bij drain naar tier 0 → laagste (meest negatieve) target_grid

### Drain-waterfall (alleen als tier 0 volgt)

Doel: SOC = 7% aan het begin van het tier-0 segment.

```
soc_piek      = huidige SOC + verwachte PV-absorptie in tier-3 charge uren
needed_kwh    = (soc_piek − 7%) × usable
cons_kwh_t5   = Σ net_kw voor tier-5 discharge uren
cons_kwh_t4   = Σ net_kw voor tier-4 discharge uren

alloc_t5      = min(needed_kwh, cons_kwh_t5)
alloc_t4      = min(needed_kwh − alloc_t5, cons_kwh_t4)
export_t5     = max(0, needed_kwh − cons_kwh_t5 − cons_kwh_t4) / n_t5_uren
export_t4     = restant / n_t4_uren  (als tier 5 onvoldoende)

frac_t        = (alloc_t + export_t × n_t) / cons_kwh_t
target_grid   = (1 − frac_t) × net × 1000  (negatief bij frac > 1)
```

**PV headroom (tier 2)**:
- `ceiling = max_soc − verwacht PV-surplus in niet-cheap uren na het segment`
- Voorkomt dat volladen bij tier 2 leidt tot export als daarna PV-surplus verwacht wordt
- Niet van toepassing bij tier 0/1 (ceiling = 100%)

---

## WPB Boilersstrategie (`sensor.strategy_wpb`)

| Mode | Trigger | Setpoint | COP |
|---|---|---|---|
| `normal` | Default | 50°C via WP | ~2.5 |
| `solar` | PV-surplus ≥ 0.2 kWh netto | 65°C (55°C via WP + 10°C via weerstand) | WP: ~2.5 / weerstand: 1.0 |
| `boost` | label=`trough` AND prijs ≤ 0.08 €/kWh AND boiler < 65°C | max 80°C via weerstand | 1.0 |

> Toelichting: `boost` activeert alleen als boiler < 65°C (niet 80°C). De 80°C is het absolute maximum setpoint tijdens boost.
> `solar` gaat tot 65°C, waarvan de laatste 10°C (55→65°C) via weerstand.

---

## EV-strategie (`sensor.strategy_ev`)

| Mode | Stroom | Trigger |
|---|---|---|
| `slow` | 10A = 2.3 kW | Tier ≤ 1 (trough/negative), geen deadline → mag gesplitst over segmenten |
| `fast` | 16A = 3.68 kW | Deadline ingesteld (`input_datetime.need_to_use_car`), goedkoopste uren vóór deadline |
| `off` | – | Geen goedkoop uur of EV niet nodig |

### EV en batterijplanning

De EV-laadmode wordt **niet** meegenomen in de netto batterijforecast.

Voor de batterijstrategie geldt nu:

```text
net = household + heatpump - pv
```

Dus expliciet:
- **zonder EV charger**
- **met** household forecast
- **met** heatpump forecast
- **minus** PV forecast

Reden:
- zonder EV-SOC of resterende laadbehoefte is `slow = 2.3 kW` / `fast = 3.68 kW` te grof
- die aanname heeft te veel impact op de geplande batterij-SOC

Runtime-aandachtspunt:
- als EV-laden start terwijl de batterij in `charge` staat, stijgt alleen de gridimport; in een trough is dat acceptabel
- als EV-laden start terwijl de batterij in `level` of `discharge` staat, compenseert de batterij dat extra verbruik en daalt de SOC sneller

Dashboard-opmerking:
- het dashboard berekent `net` zelfstandig en leest deze niet uit een strategy-sensor
- aandachtspunt: als de nettoformule in code of dashboard wijzigt, moeten beide bewust gelijkgetrokken worden
- een dashboard dat `net = household + hp + ev - pv` toont, loopt dus niet synchroon met de huidige strategy-code
- voor vergelijking met de strategy moet EV dus uit die nettoformule blijven

---

## Warmtepompstrategie (`sensor.strategy_hp`)

| Mode | Conditie |
|---|---|
| `off` | Solar gain ≥ 90% van thermische bruto vraag |
| `off` (pre_solar) | Solar-dekking begint binnen 2 uur → alvast uitzetten |
| `on` (pre_heat) | Solar-dekking eindigt binnen 2 uur → alvast aanzetten (vloer opwarmen) |
| `on` | Anders |

> Bedoeld als thermische strategie: predictief sturen op instraling → indirect effect op elektriciteitsvraag.
