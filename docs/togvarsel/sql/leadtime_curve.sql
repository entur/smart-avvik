WITH cum AS (
  SELECT hh.d, hh.h,
    SAFE_DIVIDE(SUM(IF(hh.h>=4, n_trouble5, 0)) OVER (PARTITION BY hh.d ORDER BY hh.h), SUM(IF(hh.h>=4, n_resolved, 0)) OVER (PARTITION BY hh.d ORDER BY hh.h)) AS cum_t5_from4,
    SAFE_DIVIDE(n_trouble5, n_resolved) AS hour_t5
  FROM `ent-kixavvik-dev.togvarsel.hours` hh
),
x AS (
  SELECT c.*, dd.am_t5, dd.pm_t5, dd.am_t5>=0.25 AS am_bad, dd.pm_t5>=0.30 AS pm_bad
  FROM cum c JOIN `ent-kixavvik-dev.togvarsel.days` dd ON c.d = dd.d
  WHERE dd.weekday AND dd.am_n>300 AND dd.pm_n>300
)
SELECT h, COUNT(*) n, ROUND(CORR(cum_t5_from4, am_t5),3) corr_cum_am, ROUND(CORR(cum_t5_from4, pm_t5),3) corr_cum_pm,
  ROUND(CORR(hour_t5, am_t5),3) corr_hour_am, ROUND(CORR(hour_t5, pm_t5),3) corr_hour_pm,
  ROUND(AVG(IF(cum_t5_from4>=0.15, CAST(pm_bad AS INT64), NULL)),3) p_pm_bad_cum_ge15,
  ROUND(AVG(IF(cum_t5_from4<0.15, CAST(pm_bad AS INT64), NULL)),3) p_pm_bad_cum_lt15,
  COUNTIF(cum_t5_from4>=0.15) n_cum_ge15
FROM x WHERE h BETWEEN 4 AND 17 GROUP BY h ORDER BY h
