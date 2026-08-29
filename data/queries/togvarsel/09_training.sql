-- disruptions.togvarsel_training: the model-ready day table — features from 07 + 08, labels on
-- the product bar (§14k: trouble = cancelled OR ≥10 min), day-of-week dummies, the eligibility
-- filter and the l2-tuning holdout flag.
--
--   y_am10     AM rush (06:30–09:00) bad: am_t10 ≥ 0.10          (06:30 model)
--   y_amrest10 rest of the rush (07:00–09:00) bad: rest ≥ 0.10   (07:00 model)
--   y_pm10     PM rush (15:00–18:00) bad: pm_t10 ≥ 0.15          (12:00 and 14:00 models)
--
-- eligible = weekday, not a holiday, with a full service day on both sides (am_n/pm_n > 300 —
-- drops strike days and data gaps). is_tune = the last 56 calendar days of eligible data, held
-- out by 10_train_models.sql to pick l2_reg (the findings doc's "held-out month", widened to ~8
-- weeks so the log-loss ranking isn't decided by a handful of days), then folded back into the
-- final fit.
--
-- Count columns (sx_*, cat_*, catj_*, et_cx*) are COALESCEd to 0: a day with no SX messages is
-- a zero-count day, not a missing one. Rates and means stay NULL when their denominator is
-- empty (BQML mean-imputes those). The serving query mirrors both choices.
CREATE OR REPLACE TABLE `disruptions.togvarsel_training` AS
WITH j AS (
  SELECT f.*, c.* EXCEPT(d)
  FROM `disruptions.togvarsel_features` f
  LEFT JOIN `disruptions.togvarsel_features_cat` c USING (d)
),
z AS (
  SELECT
    * REPLACE (
      IFNULL(et_cx_0630,0) AS et_cx_0630, IFNULL(et_cxj_0630,0) AS et_cxj_0630,
      IFNULL(et_cx_0700,0) AS et_cx_0700, IFNULL(et_cxj_0700,0) AS et_cxj_0700,
      IFNULL(et_cx_1200,0) AS et_cx_1200, IFNULL(et_cx_1400,0) AS et_cx_1400,
      IFNULL(sx_n_0630,0) AS sx_n_0630, IFNULL(sx_oslo_0630,0) AS sx_oslo_0630,
      IFNULL(sx_fv_0630,0) AS sx_fv_0630, IFNULL(sx_mt_0630,0) AS sx_mt_0630,
      IFNULL(sx_inn_0630,0) AS sx_inn_0630, IFNULL(sx_big_0630,0) AS sx_big_0630,
      IFNULL(sx_delay_0630,0) AS sx_delay_0630, IFNULL(sx_infra_0630,0) AS sx_infra_0630,
      IFNULL(sx_pers_0630,0) AS sx_pers_0630, IFNULL(sx_vaer_0630,0) AS sx_vaer_0630,
      IFNULL(sx_vedl_0630,0) AS sx_vedl_0630, IFNULL(sx_long_0630,0) AS sx_long_0630,
      IFNULL(sx_amj_0630,0) AS sx_amj_0630, IFNULL(sx_pmj_0630,0) AS sx_pmj_0630,
      IFNULL(sx_amj_inn_0630,0) AS sx_amj_inn_0630,
      IFNULL(sx_n_1200,0) AS sx_n_1200, IFNULL(sx_inn_1200,0) AS sx_inn_1200,
      IFNULL(sx_big_1200,0) AS sx_big_1200, IFNULL(sx_infra_1200,0) AS sx_infra_1200,
      IFNULL(sx_pmj_1200,0) AS sx_pmj_1200,
      IFNULL(sx_preveve,0) AS sx_preveve, IFNULL(sx_preveve_inn,0) AS sx_preveve_inn,
      IFNULL(sx_planned_0630,0) AS sx_planned_0630, IFNULL(sx_amj_rest_0630,0) AS sx_amj_rest_0630,
      IFNULL(sx_n_0700,0) AS sx_n_0700, IFNULL(sx_inn_0700,0) AS sx_inn_0700,
      IFNULL(sx_big_0700,0) AS sx_big_0700, IFNULL(sx_planned_0700,0) AS sx_planned_0700,
      IFNULL(sx_amj_0700,0) AS sx_amj_0700, IFNULL(sx_amj_inn_0700,0) AS sx_amj_inn_0700,
      IFNULL(sx_amj_rest_0700,0) AS sx_amj_rest_0700,
      IFNULL(sx_planned_1200,0) AS sx_planned_1200, IFNULL(sx_pmj_inn_1200,0) AS sx_pmj_inn_1200,
      IFNULL(sx_n_1400,0) AS sx_n_1400, IFNULL(sx_inn_1400,0) AS sx_inn_1400,
      IFNULL(sx_big_1400,0) AS sx_big_1400, IFNULL(sx_planned_1400,0) AS sx_planned_1400,
      IFNULL(sx_pmj_1400,0) AS sx_pmj_1400, IFNULL(sx_pmj_inn_1400,0) AS sx_pmj_inn_1400,
      IFNULL(cat_drift_ruteendring_0630,0) AS cat_drift_ruteendring_0630,
      IFNULL(cat_drift_kapasitet_0630,0) AS cat_drift_kapasitet_0630,
      IFNULL(cat_materiell_teknisk_0630,0) AS cat_materiell_teknisk_0630,
      IFNULL(cat_infrastruktur_signal_0630,0) AS cat_infrastruktur_signal_0630,
      IFNULL(cat_infrastruktur_spor_0630,0) AS cat_infrastruktur_spor_0630,
      IFNULL(cat_infrastruktur_strom_0630,0) AS cat_infrastruktur_strom_0630,
      IFNULL(cat_infrastruktur_sporveksel_0630,0) AS cat_infrastruktur_sporveksel_0630,
      IFNULL(cat_drift_personale_0630,0) AS cat_drift_personale_0630,
      IFNULL(cat_ytre_tredjepart_0630,0) AS cat_ytre_tredjepart_0630,
      IFNULL(cat_drift_planlagt_0630,0) AS cat_drift_planlagt_0630,
      IFNULL(cat_infrastruktur_stasjon_0630,0) AS cat_infrastruktur_stasjon_0630,
      IFNULL(cat_vaer_0630,0) AS cat_vaer_0630, IFNULL(cat_other_0630,0) AS cat_other_0630,
      IFNULL(catj_infra_am_0630,0) AS catj_infra_am_0630,
      IFNULL(catj_mat_am_0630,0) AS catj_mat_am_0630,
      IFNULL(catj_drift_am_0630,0) AS catj_drift_am_0630,
      IFNULL(cat_drift_ruteendring_0700,0) AS cat_drift_ruteendring_0700,
      IFNULL(cat_drift_kapasitet_0700,0) AS cat_drift_kapasitet_0700,
      IFNULL(cat_materiell_teknisk_0700,0) AS cat_materiell_teknisk_0700,
      IFNULL(cat_infrastruktur_signal_0700,0) AS cat_infrastruktur_signal_0700,
      IFNULL(cat_infrastruktur_spor_0700,0) AS cat_infrastruktur_spor_0700,
      IFNULL(cat_infrastruktur_strom_0700,0) AS cat_infrastruktur_strom_0700,
      IFNULL(cat_infrastruktur_sporveksel_0700,0) AS cat_infrastruktur_sporveksel_0700,
      IFNULL(cat_drift_personale_0700,0) AS cat_drift_personale_0700,
      IFNULL(cat_ytre_tredjepart_0700,0) AS cat_ytre_tredjepart_0700,
      IFNULL(cat_drift_planlagt_0700,0) AS cat_drift_planlagt_0700,
      IFNULL(cat_infrastruktur_stasjon_0700,0) AS cat_infrastruktur_stasjon_0700,
      IFNULL(cat_vaer_0700,0) AS cat_vaer_0700, IFNULL(cat_other_0700,0) AS cat_other_0700,
      IFNULL(catj_infra_am_0700,0) AS catj_infra_am_0700,
      IFNULL(catj_mat_am_0700,0) AS catj_mat_am_0700,
      IFNULL(catj_drift_am_0700,0) AS catj_drift_am_0700
    )
  FROM j
)
SELECT
  *,
  IF(dow=2,1,0) dow_2, IF(dow=3,1,0) dow_3, IF(dow=4,1,0) dow_4, IF(dow=5,1,0) dow_5, IF(dow=6,1,0) dow_6,
  am_t10 >= 0.10 AS y_am10,
  am_t10_rest0700 >= 0.10 AS y_amrest10,
  pm_t10 >= 0.15 AS y_pm10,
  (weekday AND NOT holiday AND am_n > 300 AND pm_n > 300) AS eligible,
  d >= DATE_SUB((SELECT MAX(d) FROM j), INTERVAL 56 DAY) AS is_tune
FROM z
