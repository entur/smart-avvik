CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.togvarsel_hours` AS
SELECT DATE(c.aimed_local) d, EXTRACT(HOUR FROM c.aimed_local) h,
  COUNT(*) n_calls, COUNTIF(c.cancelled) n_cancelled,
  COUNTIF(c.cancelled OR COALESCE(c.arr_delay_min,c.dep_delay_min)>=5) n_trouble5,
  COUNTIF(c.cancelled OR c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) n_resolved,
  COUNTIF(COALESCE(c.arr_delay_min,c.dep_delay_min)>=10) n_ge10,
  ROUND(AVG(IF(c.cancelled,NULL,COALESCE(c.arr_delay_min,c.dep_delay_min))),2) mean_delay
FROM `ent-kixavvik-dev.disruptions.togvarsel_calls` c JOIN `ent-kixavvik-dev.disruptions.togvarsel_oslo_stations` s USING (stop_place)
WHERE NOT IFNULL(c.extra_journey,FALSE)
GROUP BY 1,2
