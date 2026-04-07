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
| Forecast | max 18 uren |
| **Totaal window** | **min 12 – max 24 uren** |
| Berekende stats | `min`, `max`, `avg`, `median`, `horizon` |
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
| 4 | `crest` | peak TP, pct ≤ 0.80 | Discharge (pct-gewogen), level als pct < 0.5 |
| 5 | `peak` | peak TP, pct > 0.80 | Discharge (pct-gewogen) |

---

## Batterij real-time aansturing (BatSim / ESPHome)

De aansturing werkt op basis van twee parameters die elk uur door de strategy worden gepubliceerd:
- `grid_target_w` — gewenste gridafname in W
- `soc_target` — gewenste SOC in % aan het einde van het uur

| Mode | Richting | Export | Doel | Gedrag als doel bereikt |
|------|----------|--------|------|------------------------|
| **level** | laden + ontladen | verboden | `grid_target_w` constant | Continu bijsturen (SOC 7–100%) |
| **charge** | laden only | verboden | `soc_target` | Laden als `p1_w < target_grid` *(min afname; 0 = uit = anti-export)* |
| **discharge** | ontladen only | verboden | `soc_target` | Ontladen als `p1_w > target_grid` *(max afname; 17000 = uit = altijd standby)* |
| **export** | ontladen only | toegestaan | `soc_target` | Standby |
| **hold** | geen | n.v.t. | — | Standby |

Tekenconventie batterijvermogen (P1 / ESPHome):
- `battery_w > 0` → ontladen (batterij levert, grid daalt)
- `battery_w < 0` → laden (batterij neemt van grid, grid stijgt)
- `grid_w = p1_clean − battery_w`

Anti-export is altijd van toepassing behalve in `export` mode.

### Signaalschema

```
┌─────────────────┐   grid_w (10s)    ┌──────────────────────────────┐
│  DSMR P1 meter  │ ────────────────→ │                              │
└─────────────────┘                   │  ESPHome / BatSim            │
                                      │                              │──→ Solis: mode + W
┌─────────────────┐  mode             │  p1_estimate =               │
│ sensor.strategy │  grid_target_w  → │  grid_w + battery_w_prev     │
│    _battery     │  soc_target        │                              │
└─────────────────┘  (elk uur)        │  battery_w = f(mode,         │
                                      │    p1_est, target, soc)      │
┌─────────────────┐   SOC (10s)       │                              │
│  Solis SOC      │ ────────────────→ │                              │
└─────────────────┘                   └──────────────────────────────┘
```

**Feedforward stabiliteit:** `p1_estimate = grid_w + battery_w_prev` annuleert de meetvertraging
(max 10s) algebraïsch. De closed loop is stabiel ongeacht delay, zolang er geen stuurwijziging
plaatsvindt binnen één sample. Tracking error bij lastsprong = maximaal één sample (10s).

**BatSim vs. ESPHome productie:**

| | BatSim (simulatie) | ESPHome (productie) |
|---|---|---|
| P1 input | `ecogrid_connect_power` | `ecogrid_connect_power` |
| Huidige Solis aftrekken | Ja: `grid_w = ecogrid − solis_ac_grid_port` | Nee: ESPHome vervangt de huidige Solis-aansturing |
| SOC input | `sensor.batsim_soc_pct` | Solis SOC sensor |
| Output | `sensor.batsim_battery_w` (virtueel) | Solis charge/discharge commando + vermogen |

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
| Min laad/ontlaadvermogen | 100 W (dode zone BatSim) |
| Max laad/ontlaadvermogen | 10.000 W |

**Gepubliceerde attributen per uur** (in `forecast` lijst):

| Attribuut | Betekenis |
|---|---|
| `mode` | charge / discharge / level / export / hold |
| `grid_target_w` | Gewenste gridafname (W) — leidend in level/charge-default/discharge-default |
| `soc_target` | Gewenste SOC (%) aan het einde van het uur — leidend in charge/discharge/export |

**Constanten**:

| Constante | Waarde | Gebruik |
|---|---|---|
| `GRID_TARGET_CHARGE` | 200 W | Min gridafname in charge mode |
| `GRID_TARGET_DISCH` | 2000 W | Max gridafname in discharge mode |
| `GRID_TARGET_LEVEL` | 200 W | Target gridafname in level mode |
| `TP_WEIGHT` | 3 | TP-uren krijgen 3× meer SOC-gain dan band-uren |

**Logica per tier** (`soc_target` berekening):

| Tier | Label | Mode | soc_target |
|------|-------|------|-----------|
| 0 | `negative` | level (vroege uren) + charge (laatste 1–2 uur) | level: n.v.t.; charge: 100% gecapped op PV headroom |
| 1 | `trough` | charge | 100% gecapped op PV headroom; gewogen: TP-uur krijgt 3× meer gain |
| 2 | `dip` | charge | survival SOC (netto vraag tot volgende tier ≤ 1); gewogen verdeling |
| 3 | `neutral` | level | n.v.t. (grid_target_w = 200 W) |
| 4 | `crest` | discharge (pct ≥ 0.5) of level | soc − (soc − min_soc) × (pct − 0.5) × 2 |
| 5 | `peak` | discharge (pct ≥ 0.5) | idem, aggressiever door hogere pct |

**Discharge verdeling over niet-cheap blok**:
- Doel: batterij op min_soc bij start van het volgende cheap segment
- Natural consumption (level-uren) trekt de SOC al omlaag
- Extra discharge verdeeld over discharge-uren gewogen naar `pct`

**PV headroom (anti-export ceiling)**:
- `soc_ceiling = max_soc − verwacht PV-surplus in niet-cheap uren na het segment`
- `soc_final = max(survival_soc, min(raw_target, soc_ceiling))`
- Voorkomt dat volladen leidt tot export als daarna PV-surplus verwacht wordt

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

---

## Warmtepompstrategie (`sensor.strategy_hp`)

| Mode | Conditie |
|---|---|
| `off` | Solar gain ≥ 90% van thermische bruto vraag |
| `off` (pre_solar) | Solar-dekking begint binnen 2 uur → alvast uitzetten |
| `on` (pre_heat) | Solar-dekking eindigt binnen 2 uur → alvast aanzetten (vloer opwarmen) |
| `on` | Anders |

> Bedoeld als thermische strategie: predictief sturen op instraling → indirect effect op elektriciteitsvraag.
