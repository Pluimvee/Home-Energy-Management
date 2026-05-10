# Home-Energy-Management
Home energy forecasting and management. Based on forecasted solar heat, PV production, EPEX prices and weather forecasts a strategy is defined for EV Charging, CH by Heatpump, DHW by heatpumpboiler and battery charging. The module will learn from HA statistics and continue to calibrate itself.

## Weather forecasts (temperature and irradiation). ##
Forecasts obtained from weather stations and calibrated using real measurements.
<img width="1614" height="680" alt="image" src="https://github.com/user-attachments/assets/896c6aa0-39a1-485f-b143-870526a0834d" />

## PV Solar energy production forecast (using irradiation) ##
The last 2 weeks of Solar production is used to correlate Irradation with generated PV power by which the panel positioning, shadows on panels, inverter efficiency and temperature effects are taken into account
<img width="624" height="521" alt="image" src="https://github.com/user-attachments/assets/356eccf6-e39a-481f-b7d6-0e2decf62dc5" />

## Thermal forecast (using temperature and iridiation) ##
The isolation (heat loss) of the house is determined in the evening/night using the measured thermal energy delivered by the heatpump and the effect on delta temprature (inside - outside). This thermal loss we use during the day and the  temperature differences we correlate against solar irridiation to forecast solar gain obtained from the windows. The solar gain we forecast per hour to (again) include window size, positionig, solar azimuth and inclination, shadow areas, etc. Other heat sources (like people, cooking, candles) will corrupt this forecast but be filtered using the 2 week window.
<img width="1054" height="773" alt="image" src="https://github.com/user-attachments/assets/a56118dc-1a06-4c96-88c9-ceff038a86cb" />

## heatpump electricity forecast (using thermal demand forecast)##
We forecast the COP based on the measured COP and outside temperatures, clamped by heatpump specs. This COP we use to convert the thermal demand to heatpump electricity usage
<img width="632" height="512" alt="image" src="https://github.com/user-attachments/assets/1aad7462-400f-43e1-9153-c210a9ea2c89" />

## Household usage ##
And as last we forecast home electricity usage using the past 2 weeks historical usage per hour.
<img width="624" height="509" alt="image" src="https://github.com/user-attachments/assets/fe91b9d4-5e12-4d5c-89c5-4c683dd75531" />

## EPEX pricing analysis ##
Each hour we shift the analysis window on EPEX price info, detect the turning points (TP) and assign these TP and surrounding hours to five pricing tiers. This analysis is performend on a sliding window starting 5 hours before now, now and max 18 hours in the future, depending on EPEX price availability. As a result the window is minimal 12 and max 24 hours, and a max, mi, avg en pct for each hour is determined. These statistics are used for TP and tier assignments

| Tier | Label | Description |
|------|-------|-------------|
| 0 | negative | Market price below 0 |
| 1 | trough | Lowest price |
| 2 | dip | Low price |
| 3 | neutral | Average price level |
| 4 | crest | Above average |
| 5 | peak | High price 

<img width="1060" height="355" alt="image" src="https://github.com/user-attachments/assets/097668c8-87fe-43ff-871c-194a22131efa" />

## Strategy for CH, DHW, EV, and battery ##
Withfor each hour the pricig tier, forecasts of house usage (thuis), PV generation (PV) and heatpump usage (WP) usage, we define a strategy for 
- Central heating (CH): the forecasted solar gain can support the thermostate to stop/start heating predictive instead of only reactive and prevent over-/undershoots
- DHW boiler (WPB): can be set to off, normal (heat to 50C), solar (heat to 65C) or boost (upto 80C) depending on price info, battery SOC and shower usage
- EV charger (EV): can be set to off, slow (10amps) fast (16 amps) and the need to use the car (sensor 'time-when-i-need-my-car')
- battery control (Bat): can be set to standby, level, charge and discharge to control the battery. Charge mode is target_soc led, ensuring the batterij will end up at the specified SOC by the end of the tier constantly compensating for PV flucuations and power usage. While level and discharge are target_grid led, ensuring the grid usage is at the target_grid level. Discharge is the asymetric version of level, to prevent any charge when consumption is below target_grid (meaning in discharge mode the target_grid acts as a ceil)

<img width="984" height="770" alt="image" src="https://github.com/user-attachments/assets/39195ccc-aabc-4072-abd8-c79fdce71bb3" />



