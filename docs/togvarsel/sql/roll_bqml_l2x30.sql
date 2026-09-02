-- Rolling-origin out-of-sample evaluation of the PRODUCTION togvarsel recipe in BigQuery ML:
-- for each month M (Oct 2025 .. Aug 2026) train LOGISTIC_REG on every eligible day before M with
-- the production feature lists, log1p TRANSFORM and the l2_reg the live tuning chose, then score M.
DECLARE months ARRAY<DATE> DEFAULT GENERATE_DATE_ARRAY('2025-10-01', '2026-08-01', INTERVAL 1 MONTH);
DECLARE cal ARRAY<STRING> DEFAULT ['dow_2', 'dow_3', 'dow_4', 'dow_5', 'dow_6', 'low_season'];
DECLARE cat0630 ARRAY<STRING> DEFAULT [
  'cat_drift_ruteendring_0630', 'cat_drift_kapasitet_0630', 'cat_materiell_teknisk_0630',
  'cat_infrastruktur_signal_0630', 'cat_infrastruktur_spor_0630', 'cat_infrastruktur_strom_0630',
  'cat_infrastruktur_sporveksel_0630', 'cat_drift_personale_0630', 'cat_ytre_tredjepart_0630',
  'cat_drift_planlagt_0630', 'cat_infrastruktur_stasjon_0630', 'cat_vaer_0630', 'cat_other_0630',
  'catj_infra_am_0630', 'catj_mat_am_0630', 'catj_drift_am_0630'];
DECLARE cat0700 ARRAY<STRING> DEFAULT [
  'cat_drift_ruteendring_0700', 'cat_drift_kapasitet_0700', 'cat_materiell_teknisk_0700',
  'cat_infrastruktur_signal_0700', 'cat_infrastruktur_spor_0700', 'cat_infrastruktur_strom_0700',
  'cat_infrastruktur_sporveksel_0700', 'cat_drift_personale_0700', 'cat_ytre_tredjepart_0700',
  'cat_drift_planlagt_0700', 'cat_infrastruktur_stasjon_0700', 'cat_vaer_0700', 'cat_other_0700',
  'catj_infra_am_0700', 'catj_mat_am_0700', 'catj_drift_am_0700'];
DECLARE specs ARRAY<STRUCT<mname STRING, label STRING, l2 FLOAT64, cols ARRAY<STRING>>>;

CREATE TEMP FUNCTION xf(cols ARRAY<STRING>) AS ((
  SELECT STRING_AGG(
    IF(REGEXP_CONTAINS(c, r'^(cat_|catj_|sx_|et_cx|et_n|early_cx)'),
       FORMAT('LN(1 + GREATEST(%s, 0)) AS %s', c, c), c), ',\n    ' ORDER BY o)
  FROM UNNEST(cols) c WITH OFFSET o
));

SET specs = [
  STRUCT('am0630' AS mname, 'y_am10' AS label, 100.0 AS l2, ARRAY_CONCAT([
    'et_t5_0630', 'et_cx_0630', 'et_cxj_0630', 'et_md_0630',
    'prev_eve_t5', 'prev_eve_cancel', 'prev_pm_t5',
    'sx_inn_0630', 'sx_infra_0630', 'sx_amj_0630', 'sx_amj_inn_0630', 'sx_mt_0630', 'sx_big_0630', 'sx_n_0630',
    'et_t10_0630', 'et_max_0630', 'et_t5_last_0630', 'et_cxr_0630',
    'sx_planned_0630', 'sx_amj_rest_0630', 'sx_delay_0630', 'sx_fv_0630', 'sx_long_0630',
    'sx_pers_0630', 'sx_vedl_0630', 'sx_preveve', 'sx_preveve_inn', 'night_t5'
  ], cal, cat0630) AS cols),
  STRUCT('am0700', 'y_amrest10', 300.0, ARRAY_CONCAT([
    'et_t5_0700', 'et_t10_0700', 'et_cx_0700', 'et_cxj_0700', 'et_md_0700', 'et_max_0700', 'et_t5_last_0700',
    'prev_eve_t5', 'prev_eve_cancel', 'prev_pm_t5',
    'sx_inn_0700', 'sx_big_0700', 'sx_n_0700', 'sx_amj_0700', 'sx_amj_inn_0700', 'sx_amj_rest_0700', 'sx_planned_0700'
  ], cal, cat0700)),
  STRUCT('pm1200', 'y_pm10', 100.0, ARRAY_CONCAT([
    'et_t5_1200', 'et_cx_1200', 'et_t5_last_1200', 'et_t5_0630',
    'prev_eve_t5', 'prev_pm_t5',
    'sx_inn_1200', 'sx_big_1200', 'sx_n_1200', 'sx_infra_1200', 'sx_pmj_1200', 'sx_pmj_inn_1200', 'sx_planned_1200'
  ], cal)),
  STRUCT('pm1400', 'y_pm10', 300.0, ARRAY_CONCAT([
    'et_t5_1400', 'et_cx_1400', 'et_t5_last_1400', 'et_t5_1200', 'et_t5_0630',
    'prev_eve_t5', 'prev_pm_t5',
    'sx_inn_1400', 'sx_big_1400', 'sx_n_1400', 'sx_pmj_1400', 'sx_pmj_inn_1400', 'sx_planned_1400'
  ], cal))
];

CREATE TABLE IF NOT EXISTS `disruptions.exp_rush_roll_pred` (
  variant STRING, mname STRING, fold DATE, d DATE, label BOOL, p FLOAT64, trained_at TIMESTAMP);
DELETE FROM `disruptions.exp_rush_roll_pred` WHERE variant = 'l2x30';

FOR m IN (SELECT mname, label, l2, cols FROM UNNEST(specs)) DO
  FOR f IN (SELECT m0 FROM UNNEST(months) m0) DO
    EXECUTE IMMEDIATE FORMAT("""
      CREATE OR REPLACE MODEL `disruptions.exp_rush_roll_l2x30_%s`
      TRANSFORM(
        %s,
        label)
      OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['label'],
              data_split_method='NO_SPLIT', l2_reg=%f, max_iterations=50, early_stop=TRUE)
      AS SELECT %s, %s AS label
         FROM `disruptions.togvarsel_training`
         WHERE eligible AND %s IS NOT NULL AND d < '%t'""",
      m.mname, xf(m.cols), m.l2, ARRAY_TO_STRING(m.cols, ', '), m.label, m.label, f.m0);
    EXECUTE IMMEDIATE FORMAT("""
      INSERT INTO `disruptions.exp_rush_roll_pred`
      SELECT 'l2x30', '%s', DATE '%t', d, label,
        (SELECT prob FROM UNNEST(predicted_label_probs) WHERE LOWER(CAST(label AS STRING)) = 'true'),
        CURRENT_TIMESTAMP()
      FROM ML.PREDICT(MODEL `disruptions.exp_rush_roll_l2x30_%s`,
        (SELECT *, %s AS label FROM `disruptions.togvarsel_training`
         WHERE eligible AND %s IS NOT NULL AND d >= '%t' AND d < DATE_ADD('%t', INTERVAL 1 MONTH)))""",
      m.mname, f.m0, m.mname, m.label, m.label, f.m0, f.m0);
  END FOR;
END FOR;
