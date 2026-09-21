-- disruptions.exp_rush_area_training: one row per (day, area) for the rush-area experiment
-- (docs/rushvarsel/01-omraader-2026-09-21.md). Areas are the station corridors of
-- togvarsel_oslo_stations — west (Asker–Skøyen), northeast (Bryn–Lillestrøm), south
-- (Nordstrand–Ski) and central (Oslo S, Nationaltheatret). The 3-area scheme is the three
-- directions; the 4-area scheme adds central as its own area.
--
-- Every area-wide column of disruptions.togvarsel_training rides along unchanged (so the
-- production feature lists still resolve); the AREA's own state carries the suffix _c:
--   labels   am_t10_c / am_t10_rest_c / pm_t10_c on the product bar, over the area's calls only
--   ET       et_*_<cutoff>_c at the same aimed-time boundaries 07/08_features use (h < 6.33 for
--            06:30, < 6.83 for 07:00, < 11.83 for 12:00, < 13.83 for 14:00), "last" = the final
--            30 (AM) / 80 (PM) minutes before the cutoff, exactly as the area-wide columns
--   SX       sx_*_<cutoff>_c over situations whose affected journeys touch the area
--            (togvarsel_journeys.corridors), same fday/fh convention as 06_sx_day.sql
--   cat_*    the 13 merged category groups of 08_features_cat.sql, per area, 06:30 and 07:00
--   prev_*_c the previous calendar day's evening/PM state in the area (04_days.sql's LAG)
-- eligible_c = the area-wide eligibility AND the area carried at least half its median call
-- count in both rushes that day — the track-closure weeks (April, July) where a corridor has
-- ~30 AM calls are not a forecastable rush for that corridor.
CREATE OR REPLACE TABLE `disruptions.exp_rush_area_training` AS
WITH oc AS (
  SELECT DATE(c.aimed_local) d, s.corridor area, c.dsj, c.cancelled,
    EXTRACT(HOUR FROM c.aimed_local) + EXTRACT(MINUTE FROM c.aimed_local)/60.0 h,
    COALESCE(c.arr_delay_min, c.dep_delay_min) delay_min,
    (c.cancelled OR c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) resolved
  FROM `disruptions.togvarsel_calls` c JOIN `disruptions.togvarsel_oslo_stations` s USING (stop_place)
  WHERE NOT IFNULL(c.extra_journey, FALSE)
),
et AS (
  SELECT d, area,
    -- labels (the area's own rushes)
    SAFE_DIVIDE(COUNTIF(h>=6.5 AND h<9 AND (cancelled OR delay_min>=10)), COUNTIF(h>=6.5 AND h<9 AND resolved)) am_t10_c,
    SAFE_DIVIDE(COUNTIF(h>=7 AND h<9 AND (cancelled OR delay_min>=10)), COUNTIF(h>=7 AND h<9 AND resolved)) am_t10_rest_c,
    SAFE_DIVIDE(COUNTIF(h>=15 AND h<18 AND (cancelled OR delay_min>=10)), COUNTIF(h>=15 AND h<18 AND resolved)) pm_t10_c,
    SAFE_DIVIDE(COUNTIF(h>=6.5 AND h<9 AND cancelled), COUNTIF(h>=6.5 AND h<9)) am_cancel_c,
    SAFE_DIVIDE(COUNTIF(h>=15 AND h<18 AND cancelled), COUNTIF(h>=15 AND h<18)) pm_cancel_c,
    COUNTIF(h>=6.5 AND h<9) am_n_c, COUNTIF(h>=7 AND h<9) am_n_rest_c, COUNTIF(h>=15 AND h<18) pm_n_c,
    -- 06:30 cutoff (04:00–06:20)
    COUNTIF(h>=4 AND h<6.33) et_n_0630_c,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<6.33 AND resolved)) et_t5_0630_c,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND (cancelled OR delay_min>=10)), COUNTIF(h>=4 AND h<6.33 AND resolved)) et_t10_0630_c,
    COUNTIF(h>=4 AND h<6.33 AND cancelled) et_cx_0630_c,
    COUNT(DISTINCT IF(h>=4 AND h<6.33 AND cancelled, dsj, NULL)) et_cxj_0630_c,
    AVG(IF(h>=4 AND h<6.33 AND NOT cancelled, delay_min, NULL)) et_md_0630_c,
    MAX(IF(h>=4 AND h<6.33 AND NOT cancelled, delay_min, NULL)) et_max_0630_c,
    SAFE_DIVIDE(COUNTIF(h>=5.83 AND h<6.33 AND (cancelled OR delay_min>=5)), COUNTIF(h>=5.83 AND h<6.33 AND resolved)) et_t5_last_0630_c,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND cancelled), COUNTIF(h>=4 AND h<6.33)) et_cxr_0630_c,
    -- 07:00 cutoff (04:00–06:50)
    COUNTIF(h>=4 AND h<6.83) et_n_0700_c,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<6.83 AND resolved)) et_t5_0700_c,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.83 AND (cancelled OR delay_min>=10)), COUNTIF(h>=4 AND h<6.83 AND resolved)) et_t10_0700_c,
    COUNTIF(h>=4 AND h<6.83 AND cancelled) et_cx_0700_c,
    COUNT(DISTINCT IF(h>=4 AND h<6.83 AND cancelled, dsj, NULL)) et_cxj_0700_c,
    AVG(IF(h>=4 AND h<6.83 AND NOT cancelled, delay_min, NULL)) et_md_0700_c,
    MAX(IF(h>=4 AND h<6.83 AND NOT cancelled, delay_min, NULL)) et_max_0700_c,
    SAFE_DIVIDE(COUNTIF(h>=6.33 AND h<6.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=6.33 AND h<6.83 AND resolved)) et_t5_last_0700_c,
    -- 12:00 cutoff (04:00–11:50)
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<11.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<11.83 AND resolved)) et_t5_1200_c,
    COUNTIF(h>=4 AND h<11.83 AND cancelled) et_cx_1200_c,
    SAFE_DIVIDE(COUNTIF(h>=10.5 AND h<11.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=10.5 AND h<11.83 AND resolved)) et_t5_last_1200_c,
    -- 14:00 cutoff (04:00–13:50)
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<13.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<13.83 AND resolved)) et_t5_1400_c,
    COUNTIF(h>=4 AND h<13.83 AND cancelled) et_cx_1400_c,
    SAFE_DIVIDE(COUNTIF(h>=12.5 AND h<13.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=12.5 AND h<13.83 AND resolved)) et_t5_last_1400_c,
    -- the morning rush as realised, for the PM slots (the precision note's am_t10_known lead)
    SAFE_DIVIDE(COUNTIF(h>=6.5 AND h<9 AND (cancelled OR delay_min>=10)), COUNTIF(h>=6.5 AND h<9 AND resolved)) am_t10_known_c
  FROM oc GROUP BY d, area
),
-- previous day's evening/PM/night in the area, from the window table (all calendar days)
pw AS (
  SELECT d, corridor area,
    MAX(IF(win='evening', trouble5, NULL)) eve_t5, MAX(IF(win='evening', cancel_rate, NULL)) eve_cancel,
    MAX(IF(win='pm_rush', trouble5, NULL)) pm_t5, MAX(IF(win='night', trouble5, NULL)) night_t5
  FROM `disruptions.togvarsel_day_windows` WHERE corridor != 'all' GROUP BY 1,2
),
prev AS (
  SELECT d, area, night_t5 night_t5_c,
    LAG(eve_t5) OVER (PARTITION BY area ORDER BY d) prev_eve_t5_c,
    LAG(eve_cancel) OVER (PARTITION BY area ORDER BY d) prev_eve_cancel_c,
    LAG(pm_t5) OVER (PARTITION BY area ORDER BY d) prev_pm_t5_c
  FROM pw
),
cat AS (
  SELECT e.sn,
    CASE
      WHEN c.category IN ('ytre/vær/vinter','ytre/vær/nedbør','ytre/vær/vind') THEN 'vaer'
      WHEN c.category IS NULL OR c.category IN ('ytre/dyr','infrastruktur/tunnel','ytre/person','materiell/dør','materiell/bremse','materiell/motor') THEN 'other'
      ELSE REPLACE(REPLACE(REPLACE(c.category,'/','_'),'ø','o'),'æ','ae') END AS cat
  FROM (SELECT DISTINCT sn, onset_summary, onset_description FROM `disruptions.togvarsel_sx_events`) e
  LEFT JOIN `disruptions.categories` c
    ON IFNULL(e.onset_summary,'__NULL__')=IFNULL(c.summary,'__NULL__') AND IFNULL(e.onset_description,'__NULL__')=IFNULL(c.description,'__NULL__')
),
ev AS (
  SELECT e.sn, area, e.j_ref, e.operating_date, e.cls, e.cause, e.planned,
    EXTRACT(HOUR FROM e.first_oslo_aimed_local)+EXTRACT(MINUTE FROM e.first_oslo_aimed_local)/60 amh,
    CASE WHEN e.created_h >= 18 THEN DATE_ADD(e.created_date, INTERVAL 1 DAY) ELSE e.created_date END fday,
    CASE WHEN e.created_h >= 18 THEN e.created_h - 24 ELSE e.created_h END fh,
    cat.cat
  FROM `disruptions.togvarsel_sx_events` e, UNNEST(e.corridors) area
  JOIN cat USING (sn)
),
sxa AS (
  SELECT fday d, area,
    -- 06:30
    COUNT(DISTINCT IF(fh < 6.5, sn, NULL)) sx_n_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cls='innstilt', sn, NULL)) sx_inn_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cause IN ('signalfeil','infrastruktur','stroem'), sn, NULL)) sx_infra_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND IFNULL(planned,FALSE), sn, NULL)) sx_planned_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cls='innstilt' AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_inn_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND operating_date=fday AND amh BETWEEN 7 AND 9, j_ref, NULL)) sx_amj_rest_0630_c,
    COUNT(DISTINCT IF(fh < 0, sn, NULL)) sx_preveve_c,
    COUNT(DISTINCT IF(fh < 0 AND cls='innstilt', sn, NULL)) sx_preveve_inn_c,
    -- 07:00
    COUNT(DISTINCT IF(fh < 7, sn, NULL)) sx_n_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cls='innstilt', sn, NULL)) sx_inn_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND IFNULL(planned,FALSE), sn, NULL)) sx_planned_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cls='innstilt' AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_inn_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND operating_date=fday AND amh BETWEEN 7 AND 9, j_ref, NULL)) sx_amj_rest_0700_c,
    -- 12:00
    COUNT(DISTINCT IF(fh < 12, sn, NULL)) sx_n_1200_c,
    COUNT(DISTINCT IF(fh < 12 AND cls='innstilt', sn, NULL)) sx_inn_1200_c,
    COUNT(DISTINCT IF(fh < 12 AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_1200_c,
    COUNT(DISTINCT IF(fh < 12 AND cause IN ('signalfeil','infrastruktur','stroem'), sn, NULL)) sx_infra_1200_c,
    COUNT(DISTINCT IF(fh < 12 AND IFNULL(planned,FALSE), sn, NULL)) sx_planned_1200_c,
    COUNT(DISTINCT IF(fh < 12 AND operating_date=fday AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_1200_c,
    COUNT(DISTINCT IF(fh < 12 AND cls='innstilt' AND operating_date=fday AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_inn_1200_c,
    -- 14:00
    COUNT(DISTINCT IF(fh < 14, sn, NULL)) sx_n_1400_c,
    COUNT(DISTINCT IF(fh < 14 AND cls='innstilt', sn, NULL)) sx_inn_1400_c,
    COUNT(DISTINCT IF(fh < 14 AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_1400_c,
    COUNT(DISTINCT IF(fh < 14 AND IFNULL(planned,FALSE), sn, NULL)) sx_planned_1400_c,
    COUNT(DISTINCT IF(fh < 14 AND operating_date=fday AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_1400_c,
    COUNT(DISTINCT IF(fh < 14 AND cls='innstilt' AND operating_date=fday AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_inn_1400_c,
    -- categories per area, 06:30 and 07:00 (08_features_cat.sql's 13 groups + 3 journey counters)
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_ruteendring', sn, NULL)) cat_drift_ruteendring_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_kapasitet', sn, NULL)) cat_drift_kapasitet_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='materiell_teknisk', sn, NULL)) cat_materiell_teknisk_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_signal', sn, NULL)) cat_infrastruktur_signal_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_spor', sn, NULL)) cat_infrastruktur_spor_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_strom', sn, NULL)) cat_infrastruktur_strom_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_sporveksel', sn, NULL)) cat_infrastruktur_sporveksel_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_personale', sn, NULL)) cat_drift_personale_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='ytre_tredjepart', sn, NULL)) cat_ytre_tredjepart_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_planlagt', sn, NULL)) cat_drift_planlagt_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_stasjon', sn, NULL)) cat_infrastruktur_stasjon_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='vaer', sn, NULL)) cat_vaer_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='other', sn, NULL)) cat_other_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat LIKE 'infrastruktur%' AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_infra_am_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='materiell_teknisk' AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_mat_am_0630_c,
    COUNT(DISTINCT IF(fh < 6.5 AND cat LIKE 'drift%' AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_drift_am_0630_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='drift_ruteendring', sn, NULL)) cat_drift_ruteendring_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='drift_kapasitet', sn, NULL)) cat_drift_kapasitet_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='materiell_teknisk', sn, NULL)) cat_materiell_teknisk_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='infrastruktur_signal', sn, NULL)) cat_infrastruktur_signal_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='infrastruktur_spor', sn, NULL)) cat_infrastruktur_spor_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='infrastruktur_strom', sn, NULL)) cat_infrastruktur_strom_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='infrastruktur_sporveksel', sn, NULL)) cat_infrastruktur_sporveksel_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='drift_personale', sn, NULL)) cat_drift_personale_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='ytre_tredjepart', sn, NULL)) cat_ytre_tredjepart_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='drift_planlagt', sn, NULL)) cat_drift_planlagt_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='infrastruktur_stasjon', sn, NULL)) cat_infrastruktur_stasjon_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='vaer', sn, NULL)) cat_vaer_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='other', sn, NULL)) cat_other_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat LIKE 'infrastruktur%' AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_infra_am_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat='materiell_teknisk' AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_mat_am_0700_c,
    COUNT(DISTINCT IF(fh < 7 AND cat LIKE 'drift%' AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_drift_am_0700_c
  FROM ev GROUP BY fday, area
),
areas AS (SELECT DISTINCT corridor area FROM `disruptions.togvarsel_oslo_stations`),
j AS (
  SELECT t.*, a.area, et.* EXCEPT(d, area), prev.* EXCEPT(d, area), sxa.* EXCEPT(d, area)
  FROM `disruptions.togvarsel_training` t
  CROSS JOIN areas a
  LEFT JOIN et ON et.d = t.d AND et.area = a.area
  LEFT JOIN prev ON prev.d = t.d AND prev.area = a.area
  LEFT JOIN sxa ON sxa.d = t.d AND sxa.area = a.area
),
-- count columns: a day with no message touching the area is a zero-count day, not a missing one
z AS (
  SELECT * REPLACE (
    IFNULL(et_cx_0630_c,0) AS et_cx_0630_c, IFNULL(et_cxj_0630_c,0) AS et_cxj_0630_c,
    IFNULL(et_cx_0700_c,0) AS et_cx_0700_c, IFNULL(et_cxj_0700_c,0) AS et_cxj_0700_c,
    IFNULL(et_cx_1200_c,0) AS et_cx_1200_c, IFNULL(et_cx_1400_c,0) AS et_cx_1400_c,
    IFNULL(sx_n_0630_c,0) AS sx_n_0630_c, IFNULL(sx_inn_0630_c,0) AS sx_inn_0630_c, IFNULL(sx_big_0630_c,0) AS sx_big_0630_c,
    IFNULL(sx_infra_0630_c,0) AS sx_infra_0630_c, IFNULL(sx_planned_0630_c,0) AS sx_planned_0630_c,
    IFNULL(sx_amj_0630_c,0) AS sx_amj_0630_c, IFNULL(sx_amj_inn_0630_c,0) AS sx_amj_inn_0630_c, IFNULL(sx_amj_rest_0630_c,0) AS sx_amj_rest_0630_c,
    IFNULL(sx_preveve_c,0) AS sx_preveve_c, IFNULL(sx_preveve_inn_c,0) AS sx_preveve_inn_c,
    IFNULL(sx_n_0700_c,0) AS sx_n_0700_c, IFNULL(sx_inn_0700_c,0) AS sx_inn_0700_c, IFNULL(sx_big_0700_c,0) AS sx_big_0700_c,
    IFNULL(sx_planned_0700_c,0) AS sx_planned_0700_c,
    IFNULL(sx_amj_0700_c,0) AS sx_amj_0700_c, IFNULL(sx_amj_inn_0700_c,0) AS sx_amj_inn_0700_c, IFNULL(sx_amj_rest_0700_c,0) AS sx_amj_rest_0700_c,
    IFNULL(sx_n_1200_c,0) AS sx_n_1200_c, IFNULL(sx_inn_1200_c,0) AS sx_inn_1200_c, IFNULL(sx_big_1200_c,0) AS sx_big_1200_c,
    IFNULL(sx_infra_1200_c,0) AS sx_infra_1200_c, IFNULL(sx_planned_1200_c,0) AS sx_planned_1200_c,
    IFNULL(sx_pmj_1200_c,0) AS sx_pmj_1200_c, IFNULL(sx_pmj_inn_1200_c,0) AS sx_pmj_inn_1200_c,
    IFNULL(sx_n_1400_c,0) AS sx_n_1400_c, IFNULL(sx_inn_1400_c,0) AS sx_inn_1400_c, IFNULL(sx_big_1400_c,0) AS sx_big_1400_c,
    IFNULL(sx_planned_1400_c,0) AS sx_planned_1400_c,
    IFNULL(sx_pmj_1400_c,0) AS sx_pmj_1400_c, IFNULL(sx_pmj_inn_1400_c,0) AS sx_pmj_inn_1400_c,
    IFNULL(cat_drift_ruteendring_0630_c,0) AS cat_drift_ruteendring_0630_c, IFNULL(cat_drift_kapasitet_0630_c,0) AS cat_drift_kapasitet_0630_c,
    IFNULL(cat_materiell_teknisk_0630_c,0) AS cat_materiell_teknisk_0630_c, IFNULL(cat_infrastruktur_signal_0630_c,0) AS cat_infrastruktur_signal_0630_c,
    IFNULL(cat_infrastruktur_spor_0630_c,0) AS cat_infrastruktur_spor_0630_c, IFNULL(cat_infrastruktur_strom_0630_c,0) AS cat_infrastruktur_strom_0630_c,
    IFNULL(cat_infrastruktur_sporveksel_0630_c,0) AS cat_infrastruktur_sporveksel_0630_c, IFNULL(cat_drift_personale_0630_c,0) AS cat_drift_personale_0630_c,
    IFNULL(cat_ytre_tredjepart_0630_c,0) AS cat_ytre_tredjepart_0630_c, IFNULL(cat_drift_planlagt_0630_c,0) AS cat_drift_planlagt_0630_c,
    IFNULL(cat_infrastruktur_stasjon_0630_c,0) AS cat_infrastruktur_stasjon_0630_c, IFNULL(cat_vaer_0630_c,0) AS cat_vaer_0630_c,
    IFNULL(cat_other_0630_c,0) AS cat_other_0630_c, IFNULL(catj_infra_am_0630_c,0) AS catj_infra_am_0630_c,
    IFNULL(catj_mat_am_0630_c,0) AS catj_mat_am_0630_c, IFNULL(catj_drift_am_0630_c,0) AS catj_drift_am_0630_c,
    IFNULL(cat_drift_ruteendring_0700_c,0) AS cat_drift_ruteendring_0700_c, IFNULL(cat_drift_kapasitet_0700_c,0) AS cat_drift_kapasitet_0700_c,
    IFNULL(cat_materiell_teknisk_0700_c,0) AS cat_materiell_teknisk_0700_c, IFNULL(cat_infrastruktur_signal_0700_c,0) AS cat_infrastruktur_signal_0700_c,
    IFNULL(cat_infrastruktur_spor_0700_c,0) AS cat_infrastruktur_spor_0700_c, IFNULL(cat_infrastruktur_strom_0700_c,0) AS cat_infrastruktur_strom_0700_c,
    IFNULL(cat_infrastruktur_sporveksel_0700_c,0) AS cat_infrastruktur_sporveksel_0700_c, IFNULL(cat_drift_personale_0700_c,0) AS cat_drift_personale_0700_c,
    IFNULL(cat_ytre_tredjepart_0700_c,0) AS cat_ytre_tredjepart_0700_c, IFNULL(cat_drift_planlagt_0700_c,0) AS cat_drift_planlagt_0700_c,
    IFNULL(cat_infrastruktur_stasjon_0700_c,0) AS cat_infrastruktur_stasjon_0700_c, IFNULL(cat_vaer_0700_c,0) AS cat_vaer_0700_c,
    IFNULL(cat_other_0700_c,0) AS cat_other_0700_c, IFNULL(catj_infra_am_0700_c,0) AS catj_infra_am_0700_c,
    IFNULL(catj_mat_am_0700_c,0) AS catj_mat_am_0700_c, IFNULL(catj_drift_am_0700_c,0) AS catj_drift_am_0700_c
  ) FROM j
),
fl AS (
  -- per-area call floor: half the area's median rush call count over area-wide-eligible days
  SELECT area,
    0.5 * PERCENTILE_CONT(IF(eligible, am_n_c, NULL), 0.5) OVER (PARTITION BY area) am_floor_c,
    0.5 * PERCENTILE_CONT(IF(eligible, pm_n_c, NULL), 0.5) OVER (PARTITION BY area) pm_floor_c
  FROM z QUALIFY ROW_NUMBER() OVER (PARTITION BY area) = 1
)
SELECT z.*,
  IF(area='west',1,0) area_west, IF(area='northeast',1,0) area_northeast, IF(area='south',1,0) area_south, IF(area='central',1,0) area_central,
  am_t10_c >= 0.10 AS y_am10_c,
  am_t10_rest_c >= 0.10 AS y_amrest10_c,
  pm_t10_c >= 0.15 AS y_pm10_c,
  fl.am_floor_c, fl.pm_floor_c,
  (eligible AND am_n_c > fl.am_floor_c AND pm_n_c > fl.pm_floor_c) AS eligible_c
FROM z JOIN fl USING (area)
