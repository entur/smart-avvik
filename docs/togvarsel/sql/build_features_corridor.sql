CREATE OR REPLACE TABLE `ent-kixavvik-dev.togvarsel.features_corridor` AS
WITH w AS (
  SELECT d, corridor,
    MAX(IF(win='early', trouble5, NULL)) early_t5, MAX(IF(win='early', n_cancelled, NULL)) early_cx,
    MAX(IF(win='am_rush', trouble5, NULL)) am_t5, MAX(IF(win='pm_rush', trouble5, NULL)) pm_t5,
    MAX(IF(win='am_rush', n_calls, NULL)) am_n, MAX(IF(win='pm_rush', n_calls, NULL)) pm_n,
    MAX(IF(win='midday', trouble5, NULL)) mid_t5
  FROM `ent-kixavvik-dev.togvarsel.day_windows` WHERE corridor IN ('west','northeast','south') GROUP BY 1,2
),
sx AS (
  SELECT CASE WHEN created_h >= 18 THEN DATE_ADD(created_date, INTERVAL 1 DAY) ELSE created_date END fday,
    CASE WHEN created_h >= 18 THEN created_h - 24 ELSE created_h END fh, corridor, sn, j_ref, cls, cause, first_oslo_aimed_local, operating_date
  FROM `ent-kixavvik-dev.togvarsel.sx_events`, UNNEST(corridors) corridor
  WHERE corridor IN ('west','northeast','south')
),
sxa AS (
  SELECT fday d, corridor,
    COUNT(DISTINCT IF(fh < 6.5 AND cls='innstilt', sn, NULL)) sx_inn_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cls='innstilt' AND operating_date=fday AND EXTRACT(HOUR FROM first_oslo_aimed_local)+EXTRACT(MINUTE FROM first_oslo_aimed_local)/60 BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_inn_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND operating_date=fday AND EXTRACT(HOUR FROM first_oslo_aimed_local)+EXTRACT(MINUTE FROM first_oslo_aimed_local)/60 BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cause IN ('signalfeil','infrastruktur','stroem'), sn, NULL)) sx_infra_0630_c
  FROM sx GROUP BY 1,2
)
SELECT w.*, f.weekday, f.holiday, f.low_season, f.dow, f.prev_eve_t5, f.et_t5_0630 et_t5_0630_all, f.sx_infra_0630, f.sx_amj_0630,
  IFNULL(sxa.sx_inn_0630_c,0) sx_inn_0630_c, IFNULL(sxa.sx_amj_inn_0630_c,0) sx_amj_inn_0630_c, IFNULL(sxa.sx_amj_0630_c,0) sx_amj_0630_c, IFNULL(sxa.sx_big_0630_c,0) sx_big_0630_c, IFNULL(sxa.sx_infra_0630_c,0) sx_infra_0630_c,
  LAG(w.pm_t5) OVER (PARTITION BY w.corridor ORDER BY w.d) prev_pm_t5_c
FROM w JOIN `ent-kixavvik-dev.togvarsel.features` f USING (d) LEFT JOIN sxa ON sxa.d = w.d AND sxa.corridor = w.corridor
