# Home-Energy-Management
Home energy forecasting and management. Based on forecasted solar heat, PV production, EPEX prices, weather forecasts this module defines a strategy for EV Charging, Heatpump usage, water heating and battery charging. The module will learn from HA statistics and continue to calibrate itself. 

## Weather forecasts (temperature and irradiation). ##
Obtained from weather stations and calibrated using real measurements.
<img width="1614" height="680" alt="image" src="https://github.com/user-attachments/assets/896c6aa0-39a1-485f-b143-870526a0834d" />

## PV Solar energy production forecast (using irradiation) ##
The last 2 weeks of Solar production is used to correlate Irr with PV power by which the panel positioning, shadows on panels, inverter efficiency by temperature effects are taken into account
<img width="624" height="521" alt="image" src="https://github.com/user-attachments/assets/356eccf6-e39a-481f-b7d6-0e2decf62dc5" />

## Thermal forecast (using temperature and iridiation) ##
The heat loss of the house is calculated when during the evening/night using the correlation between delivered heat and effect on delta temprature (inside - outside). This thermal loss we use during the day the correlate solar irridiation to the delta temperature to forecast solar gain obtained from the windows. The solar gain we forecast per hour to (again) include window size, positionig, solar azimuth and inclination, shadow areas, etc.
<img width="1054" height="773" alt="image" src="https://github.com/user-attachments/assets/a56118dc-1a06-4c96-88c9-ceff038a86cb" />

## heatpump electricity forecast (using thermal demand forecast)##
We forecast the COP based on outside temperature and heatpump specs. This COP we use to convert the thermal demand to heatpump electricity usage
<img width="632" height="512" alt="image" src="https://github.com/user-attachments/assets/1aad7462-400f-43e1-9153-c210a9ea2c89" />

## Household usage ##
And as last we forecast home electricity usage using the past 2 weeks historical usage per hour.
<img width="624" height="509" alt="image" src="https://github.com/user-attachments/assets/fe91b9d4-5e12-4d5c-89c5-4c683dd75531" />

## EPEX pricing analysis ##
When we recieve new EPEX price info detect the turning points (TP) and assign these TP and surrounding hours to five tiers. This analysis is performend on a sliding window starting 5 hours before now, now and max 18 hours in the future. A wiondow ofminimal 12 and max 24 hours depending on EPEX price info availability

| Tier | Label | Description |
|------|-------|-------------|
| 0 | negative | Market price below 0 |
| 1 | trough | Lowest price |
| 2 | dip | Low price |
| 3 | neutral | Average price level |
| 4 | crest | Above average |
| 5 | peak | High price 

<img width="796" height="358" alt="image" src="https://github.com/user-attachments/assets/a3bea1d3-a798-4cfa-8fe0-dcaedbe3d3a1" />

## Strategy for CH, DHW, EV, and battery ##
These tiers together with the forecasts of house usage (thuis), PV generation (PV) and heatpump usage (WP) we set a strategy for 
- Central heating (CH): the forecasted solar gain can support the thermostate to stop/start heating predictive instead of reactive.
- DHW boiler (WPB): can be set to off, normal (heat to 50C), solar (heat to 65C) or boost (upto 80C)
- EV charger (EV): can be set to off, slow (10amps) fast (16 amps) and is using a sensor 'time-when-i-need-my-car'
- battery control (Bat): to control charge and discharge, with expected SOC and target grid export/import

<img width="974" height="771" alt="image" src="https://github.com/user-attachments/assets/01d99fc2-5619-425f-90ee-d977e8b1bfe2" />



