WITH lc AS (
  SELECT DATE(c.aimed_local) d, c.line, EXTRACT(HOUR FROM c.aimed_local)+EXTRACT(MINUTE FROM c.aimed_local)/60 h, c.cancelled,
    COALESCE(c.arr_delay_min,c.dep_delay_min) delay_min, (c.cancelled OR c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) resolved
  FROM `ent-kixavvik-dev.togvarsel.calls` c JOIN `ent-kixavvik-dev.togvarsel.oslo_stations` USING (stop_place)
  WHERE NOT IFNULL(c.extra_journey,FALSE) AND EXTRACT(DAYOFWEEK FROM DATE(c.aimed_local)) BETWEEN 2 AND 6
),
ld AS (
  SELECT d, line,
    SAFE_DIVIDE(COUNTIF(h>=4.5 AND h<6.5 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4.5 AND h<6.5 AND resolved)) early_t5,
    SAFE_DIVIDE(COUNTIF(h>=6.5 AND h<9 AND (cancelled OR delay_min>=5)), COUNTIF(h>=6.5 AND h<9 AND resolved)) am_t5,
    SAFE_DIVIDE(COUNTIF(h>=15 AND h<18 AND (cancelled OR delay_min>=5)), COUNTIF(h>=15 AND h<18 AND resolved)) pm_t5,
    COUNTIF(h>=4.5 AND h<6.5) early_n, COUNTIF(h>=6.5 AND h<9) am_n, COUNTIF(h>=15 AND h<18) pm_n
  FROM lc GROUP BY 1,2
)
SELECT line, COUNT(*) n_days, ROUND(AVG(early_n)) early_n, ROUND(AVG(am_n)) am_n, ROUND(AVG(am_t5),3) mean_am, ROUND(AVG(pm_t5),3) mean_pm,
  ROUND(CORR(early_t5, am_t5),2) r_early_am, ROUND(CORR(early_t5, pm_t5),2) r_early_pm, ROUND(CORR(am_t5, pm_t5),2) r_am_pm
FROM ld WHERE early_n >= 10 AND am_n >= 30 AND pm_n >= 30 GROUP BY 1 HAVING n_days >= 100 ORDER BY r_early_am DESC
