-- disruptions.togvarsel_features_cat: the second feature block — the 07:00 ET cutoff and the
-- rest-of-rush (07–09) outcome the 07:00 model is labelled on, the AI-classified category
-- counters (disruptions.categories, joined on onset text with a NULL sentinel), and the SX
-- counters at the 07:00 / 14:00 cutoffs that 06_sx_day.sql does not carry. Only columns some
-- production model actually reads are kept (§14k: no weather, no rolling persistence, PM models
-- without categories); columns already in togvarsel_features are NOT repeated here.
--
-- The 13 merged category groups follow §14d of the findings doc: the weather subtree is pooled,
-- rare subtrees (dyr, tunnel, person, dør, bremse, motor) and unclassified text fall into
-- 'other', slashes/æ/ø are normalised into column-name-safe tokens.
CREATE OR REPLACE TABLE `disruptions.togvarsel_features_cat` AS
WITH oc AS (
  SELECT DATE(c.aimed_local) d, c.dsj, c.cancelled,
    EXTRACT(HOUR FROM c.aimed_local) + EXTRACT(MINUTE FROM c.aimed_local)/60.0 h,
    COALESCE(c.arr_delay_min, c.dep_delay_min) delay_min,
    (c.cancelled OR c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) resolved
  FROM `disruptions.togvarsel_calls` c JOIN `disruptions.togvarsel_oslo_stations` USING (stop_place)
  WHERE NOT IFNULL(c.extra_journey,FALSE)
),
et AS (
  SELECT d,
    -- cutoff 07:00 (calls aimed 04:00-06:50)
    COUNTIF(h>=4 AND h<6.83) et_n_0700,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<6.83 AND resolved)) et_t5_0700,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.83 AND (cancelled OR delay_min>=10)), COUNTIF(h>=4 AND h<6.83 AND resolved)) et_t10_0700,
    COUNTIF(h>=4 AND h<6.83 AND cancelled) et_cx_0700,
    COUNT(DISTINCT IF(h>=4 AND h<6.83 AND cancelled, dsj, NULL)) et_cxj_0700,
    AVG(IF(h>=4 AND h<6.83 AND NOT cancelled, delay_min, NULL)) et_md_0700,
    MAX(IF(h>=4 AND h<6.83 AND NOT cancelled, delay_min, NULL)) et_max_0700,
    SAFE_DIVIDE(COUNTIF(h>=6.33 AND h<6.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=6.33 AND h<6.83 AND resolved)) et_t5_last_0700,
    -- rest of the rush (07:00-09:00): the 07:00 model's label source
    SAFE_DIVIDE(COUNTIF(h>=7 AND h<9 AND (cancelled OR delay_min>=5)), COUNTIF(h>=7 AND h<9 AND resolved)) am_t5_rest0700,
    SAFE_DIVIDE(COUNTIF(h>=7 AND h<9 AND (cancelled OR delay_min>=10)), COUNTIF(h>=7 AND h<9 AND resolved)) am_t10_rest0700,
    SAFE_DIVIDE(COUNTIF(h>=7 AND h<9 AND cancelled), COUNTIF(h>=7 AND h<9)) am_cancel_rest0700,
    COUNTIF(h>=7 AND h<9) am_n_rest0700
  FROM oc GROUP BY d
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
  SELECT e.sn, IFNULL(e.oslo,FALSE) oslo, e.j_ref, e.operating_date, e.cls, e.planned,
    EXTRACT(HOUR FROM e.first_oslo_aimed_local)+EXTRACT(MINUTE FROM e.first_oslo_aimed_local)/60 amh,
    CASE WHEN e.created_h >= 18 THEN DATE_ADD(e.created_date, INTERVAL 1 DAY) ELSE e.created_date END fday,
    CASE WHEN e.created_h >= 18 THEN e.created_h - 24 ELSE e.created_h END fh,
    cat.cat
  FROM `disruptions.togvarsel_sx_events` e JOIN cat USING (sn)
),
sxc AS (
  SELECT fday d,
    -- categories known by 06:30 (the 06:30 model's category block)
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_ruteendring', sn, NULL)) cat_drift_ruteendring_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_kapasitet', sn, NULL)) cat_drift_kapasitet_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='materiell_teknisk', sn, NULL)) cat_materiell_teknisk_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_signal', sn, NULL)) cat_infrastruktur_signal_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_spor', sn, NULL)) cat_infrastruktur_spor_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_strom', sn, NULL)) cat_infrastruktur_strom_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_sporveksel', sn, NULL)) cat_infrastruktur_sporveksel_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_personale', sn, NULL)) cat_drift_personale_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='ytre_tredjepart', sn, NULL)) cat_ytre_tredjepart_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_planlagt', sn, NULL)) cat_drift_planlagt_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_stasjon', sn, NULL)) cat_infrastruktur_stasjon_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='vaer', sn, NULL)) cat_vaer_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='other', sn, NULL)) cat_other_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat LIKE 'infrastruktur%' AND oslo AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_infra_am_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='materiell_teknisk' AND oslo AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_mat_am_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat LIKE 'drift%' AND oslo AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_drift_am_0630,
    -- SX at 06:30 not carried by 06_sx_day.sql
    COUNT(DISTINCT IF(fh < 6.5 AND IFNULL(planned,FALSE), sn, NULL)) sx_planned_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND operating_date=fday AND amh BETWEEN 7 AND 9, j_ref, NULL)) sx_amj_rest_0630,
    -- categories known by 07:00 (the 07:00 model's category block)
    COUNT(DISTINCT IF(fh < 7.0 AND cat='drift_ruteendring', sn, NULL)) cat_drift_ruteendring_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='drift_kapasitet', sn, NULL)) cat_drift_kapasitet_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='materiell_teknisk', sn, NULL)) cat_materiell_teknisk_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_signal', sn, NULL)) cat_infrastruktur_signal_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_spor', sn, NULL)) cat_infrastruktur_spor_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_strom', sn, NULL)) cat_infrastruktur_strom_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_sporveksel', sn, NULL)) cat_infrastruktur_sporveksel_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='drift_personale', sn, NULL)) cat_drift_personale_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='ytre_tredjepart', sn, NULL)) cat_ytre_tredjepart_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='drift_planlagt', sn, NULL)) cat_drift_planlagt_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_stasjon', sn, NULL)) cat_infrastruktur_stasjon_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='vaer', sn, NULL)) cat_vaer_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='other', sn, NULL)) cat_other_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat LIKE 'infrastruktur%' AND oslo AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_infra_am_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='materiell_teknisk' AND oslo AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_mat_am_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat LIKE 'drift%' AND oslo AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_drift_am_0700,
    -- SX at 07:00
    COUNT(DISTINCT IF(fh < 7.0, sn, NULL)) sx_n_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND cls='innstilt', sn, NULL)) sx_inn_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND IFNULL(planned,FALSE), sn, NULL)) sx_planned_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND cls='innstilt' AND operating_date=fday AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_inn_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND operating_date=fday AND amh BETWEEN 7 AND 9, j_ref, NULL)) sx_amj_rest_0700,
    -- SX at 12:00 not carried by 06_sx_day.sql
    COUNT(DISTINCT IF(fh < 12 AND IFNULL(planned,FALSE), sn, NULL)) sx_planned_1200,
    COUNT(DISTINCT IF(fh < 12 AND oslo AND cls='innstilt' AND operating_date=fday AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_inn_1200,
    -- SX at 14:00
    COUNT(DISTINCT IF(fh < 14, sn, NULL)) sx_n_1400,
    COUNT(DISTINCT IF(fh < 14 AND oslo AND cls='innstilt', sn, NULL)) sx_inn_1400,
    COUNT(DISTINCT IF(fh < 14 AND oslo AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_1400,
    COUNT(DISTINCT IF(fh < 14 AND IFNULL(planned,FALSE), sn, NULL)) sx_planned_1400,
    COUNT(DISTINCT IF(fh < 14 AND oslo AND operating_date=fday AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_1400,
    COUNT(DISTINCT IF(fh < 14 AND oslo AND cls='innstilt' AND operating_date=fday AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_inn_1400
  FROM ev GROUP BY fday
)
SELECT f.d, et.* EXCEPT(d), sxc.* EXCEPT(d)
FROM `disruptions.togvarsel_days` f
LEFT JOIN et USING (d) LEFT JOIN sxc USING (d)
