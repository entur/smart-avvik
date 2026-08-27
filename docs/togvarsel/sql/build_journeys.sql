CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.togvarsel_journeys`
PARTITION BY operating_date CLUSTER BY line AS
SELECT c.operating_date, c.sj, c.dsj, ANY_VALUE(c.line) line, ANY_VALUE(c.direction) direction,
  MIN(c.aimed_local) first_aimed_local, MAX(c.aimed_local) last_aimed_local,
  MIN(IF(s.stop_place IS NOT NULL, c.aimed_local, NULL)) first_oslo_aimed_local,
  MAX(IF(s.stop_place IS NOT NULL, c.aimed_local, NULL)) last_oslo_aimed_local,
  COUNT(*) n_calls, COUNTIF(s.stop_place IS NOT NULL) n_oslo_calls,
  COUNTIF(c.cancelled) n_cancelled, COUNTIF(c.cancelled AND s.stop_place IS NOT NULL) n_oslo_cancelled,
  LOGICAL_OR(c.journey_cancelled) journey_cancelled,
  ARRAY_AGG(DISTINCT s.corridor IGNORE NULLS) corridors,
  ROUND(AVG(IF(s.stop_place IS NOT NULL AND NOT c.cancelled, COALESCE(c.arr_delay_min, c.dep_delay_min), NULL)),2) oslo_mean_delay,
  MAX(IF(s.stop_place IS NOT NULL AND NOT c.cancelled, COALESCE(c.arr_delay_min, c.dep_delay_min), NULL)) oslo_max_delay,
  LOGICAL_OR(IFNULL(c.extra_journey,FALSE)) extra_journey
FROM `ent-kixavvik-dev.disruptions.togvarsel_calls` c
LEFT JOIN `ent-kixavvik-dev.disruptions.togvarsel_oslo_stations` s USING (stop_place)
GROUP BY 1,2,3
