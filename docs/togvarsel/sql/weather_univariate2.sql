WITH st AS (SELECT id FROM `ent-kixavvik-dev.disruptions.frost_stations` WHERE lat BETWEEN 59.75 AND 60.05 AND lon BETWEEN 10.35 AND 11.15),
hourly AS (
  SELECT DATE(ts, 'Europe/Oslo') d, EXTRACT(HOUR FROM DATETIME(ts,'Europe/Oslo')) h, AVG(air_temp) t, MAX(snow_depth) snow, AVG(precip_1h) precip, MAX(wind_speed) wind
  FROM `ent-kixavvik-dev.disruptions.station_weather_hourly` w JOIN st ON w.station_id = st.id GROUP BY 1,2
),
wx AS (SELECT d, MIN(t) tmin, MAX(t) tmax, MAX(snow) snow, SUM(precip) precip, MAX(wind) wind, MIN(IF(h<=6, t, NULL)) tmin_night, SUM(IF(h<=6, precip, 0)) precip_night, COUNT(*) n_h FROM hourly GROUP BY d),
f AS (
  SELECT f.*, wx.tmin, wx.tmax, wx.snow, wx.precip, wx.tmin_night, wx.precip_night, wx.wind FROM `ent-kixavvik-dev.togvarsel.features` f JOIN wx USING (d)
  WHERE weekday AND NOT holiday AND am_n>300 AND pm_n>300 AND n_h >= 20
)
SELECT COUNT(*) n, ROUND(AVG(snow),1) avg_snow, ROUND(CORR(tmin, am_t5),2) r_tmin_am, ROUND(CORR(tmin, pm_t5),2) r_tmin_pm, ROUND(CORR(snow, am_t5),2) r_snow_am, ROUND(CORR(snow, pm_t5),2) r_snow_pm,
  ROUND(CORR(precip, pm_t5),2) r_precip_pm, ROUND(CORR(precip_night, am_t5),2) r_precipn_am, ROUND(CORR(wind, pm_t5),2) r_wind_pm, ROUND(CORR(tmax, pm_t5),2) r_tmax_pm,
  COUNTIF(snow > 0) n_snow, ROUND(AVG(IF(snow>0, CAST(pm_bad AS INT64), NULL)),3) p_pmbad_snow, ROUND(AVG(IF(snow=0 OR snow IS NULL, CAST(pm_bad AS INT64), NULL)),3) p_pmbad_nosnow,
  ROUND(AVG(IF(snow>0, CAST(am_bad AS INT64), NULL)),3) p_ambad_snow, ROUND(AVG(IF(snow=0 OR snow IS NULL, CAST(am_bad AS INT64), NULL)),3) p_ambad_nosnow,
  COUNTIF(tmin < -5) n_cold, ROUND(AVG(IF(tmin < -5, CAST(pm_bad AS INT64), NULL)),3) p_pmbad_cold, ROUND(AVG(IF(tmin >= -5, CAST(pm_bad AS INT64), NULL)),3) p_pmbad_notcold,
  COUNTIF(tmax > 26) n_hot, ROUND(AVG(IF(tmax > 26, CAST(pm_bad AS INT64), NULL)),3) p_pmbad_hot, ROUND(AVG(IF(tmax > 26, pm_t5, NULL)),3) pm_t5_hot, ROUND(AVG(IF(tmax <= 26, pm_t5, NULL)),3) pm_t5_nothot,
  COUNTIF(precip > 10) n_wet, ROUND(AVG(IF(precip > 10, CAST(pm_bad AS INT64), NULL)),3) p_pmbad_wet
FROM f
