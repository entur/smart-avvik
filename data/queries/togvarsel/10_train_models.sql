-- Train the four togvarsel production models (§8 of docs/togvarsel/02-findings-2026-08-27.md as
-- revised by §14k): LOGISTIC_REG — NOT boosted trees — with log1p on the count columns and
-- l2_reg picked on the held-out tail of the data (is_tune in 09_training.sql), then refit on
-- everything with the winner. §14j measured that untuned BQML LOGISTIC_REG only ties the boosted
-- tree; the study's edge came from exactly these two knobs, so they are the point of this file.
--
--   togvarsel_m_am0630   P(bad AM rush 06:30–09:00)      from the 06:30 state  (F15 + extra ET + categories)
--   togvarsel_m_am0700   P(bad rest-of-rush 07:00–09:00) from the 07:00 state  (AM0700 + categories)
--   togvarsel_m_pm1200   P(bad PM rush 15:00–18:00)      from the 12:00 state  (no categories — §14b: PM categories fail placebo)
--   togvarsel_m_pm1400   P(bad PM rush 15:00–18:00)      from the 14:00 state  (no categories)
--
-- The final fits land in togvarsel_m_cand_* (candidates); retrain.sh copies them onto the live
-- togvarsel_m_* names only after all four trained, so the serving set is never half-replaced.
--
-- The log1p rule is BY PREFIX (cat_/catj_/sx_/et_cx/et_n/early_cx — the study's COUNT_PREFIX),
-- baked into each model's TRANSFORM so ML.PREDICT takes RAW feature columns. l2_reg is chosen by
-- holdout log-loss (smoother than AUC on ~40 days), ties broken toward the stronger penalty —
-- §14j: the local optimum is a strong one. Grid spans both plausible mappings of the study's
-- C=0.1 onto BQML's penalty scale. Each grid point is a ~40 s LOGISTIC_REG job; the whole script
-- is ~36 model fits and costs cents. Results land in disruptions.togvarsel_tuning.
--
-- cut_* (2026-09-21, docs/rushvarsel/04-cuts): planned Oslo calls the day's timetable dropped
-- against the standing plan, all and first-seen-today, in every model. Known at 02:06 from the
-- day's GTFS. Linear on purpose -- the rolling evaluation kept them linear and gained +0.04..+0.07
-- AUC -- so they are deliberately NOT in the log1p prefix rule below.
DECLARE l2_grid ARRAY<FLOAT64> DEFAULT [0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0];
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
DECLARE specs ARRAY<STRUCT<mname STRING, label STRING, cols ARRAY<STRING>>>;
DECLARE best_l2 FLOAT64;

-- The study's COUNT_PREFIX rule: count-ish columns get LN(1+GREATEST(x,0)) inside TRANSFORM.
CREATE TEMP FUNCTION xf(cols ARRAY<STRING>) AS ((
  SELECT STRING_AGG(
    IF(REGEXP_CONTAINS(c, r'^(cat_|catj_|sx_|et_cx|et_n|early_cx)'),
       FORMAT('LN(1 + GREATEST(%s, 0)) AS %s', c, c), c), ',\n    ' ORDER BY o)
  FROM UNNEST(cols) c WITH OFFSET o
));

SET specs = [
  -- 06:30, whole AM rush: F15 + extra ET state + categories (§14k items 1–3)
  STRUCT('am0630' AS mname, 'y_am10' AS label, ARRAY_CONCAT([
    'et_t5_0630', 'et_cx_0630', 'et_cxj_0630', 'et_md_0630',
    'prev_eve_t5', 'prev_eve_cancel', 'prev_pm_t5',
    'sx_inn_0630', 'sx_infra_0630', 'sx_amj_0630', 'sx_amj_inn_0630', 'sx_mt_0630', 'sx_big_0630', 'sx_n_0630',
    'et_t10_0630', 'et_max_0630', 'et_t5_last_0630', 'et_cxr_0630',
    'sx_planned_0630', 'sx_amj_rest_0630', 'sx_delay_0630', 'sx_fv_0630', 'sx_long_0630',
    'sx_pers_0630', 'sx_vedl_0630', 'sx_preveve', 'sx_preveve_inn', 'night_t5',
    'cut_am', 'cut_am_new'
  ], cal, cat0630) AS cols),
  -- 07:00, rest of the rush (07–09): the sharpest product in the study (§14e)
  STRUCT('am0700', 'y_amrest10', ARRAY_CONCAT([
    'et_t5_0700', 'et_t10_0700', 'et_cx_0700', 'et_cxj_0700', 'et_md_0700', 'et_max_0700', 'et_t5_last_0700',
    'prev_eve_t5', 'prev_eve_cancel', 'prev_pm_t5',
    'sx_inn_0700', 'sx_big_0700', 'sx_n_0700', 'sx_amj_0700', 'sx_amj_inn_0700', 'sx_amj_rest_0700', 'sx_planned_0700',
    'cut_amrest', 'cut_amrest_new'
  ], cal, cat0700)),
  -- 12:00 and 14:00, PM rush (§8 part 3, §14h)
  STRUCT('pm1200', 'y_pm10', ARRAY_CONCAT([
    'et_t5_1200', 'et_cx_1200', 'et_t5_last_1200', 'et_t5_0630',
    'prev_eve_t5', 'prev_pm_t5',
    'sx_inn_1200', 'sx_big_1200', 'sx_n_1200', 'sx_infra_1200', 'sx_pmj_1200', 'sx_pmj_inn_1200', 'sx_planned_1200',
    'cut_pm', 'cut_pm_new'
  ], cal)),
  STRUCT('pm1400', 'y_pm10', ARRAY_CONCAT([
    'et_t5_1400', 'et_cx_1400', 'et_t5_last_1400', 'et_t5_1200', 'et_t5_0630',
    'prev_eve_t5', 'prev_pm_t5',
    'sx_inn_1400', 'sx_big_1400', 'sx_n_1400', 'sx_pmj_1400', 'sx_pmj_inn_1400', 'sx_planned_1400',
    'cut_pm', 'cut_pm_new'
  ], cal))
];

CREATE OR REPLACE TABLE `disruptions.togvarsel_tuning` (
  model_name STRING, l2_reg FLOAT64, holdout_log_loss FLOAT64, holdout_auc FLOAT64,
  n_train INT64, n_tune INT64, chosen BOOL, trained_at TIMESTAMP
);

FOR m IN (SELECT mname, label, cols FROM UNNEST(specs)) DO
  FOR g IN (SELECT l2 FROM UNNEST(l2_grid) l2) DO
    EXECUTE IMMEDIATE FORMAT("""
      CREATE OR REPLACE MODEL `disruptions.togvarsel_tune_%s`
      TRANSFORM(
        %s,
        label, is_tune)
      OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['label'],
              data_split_method='CUSTOM', data_split_col='is_tune',
              l2_reg=%f, max_iterations=50, early_stop=TRUE)
      AS SELECT %s, %s AS label, is_tune
         FROM `disruptions.togvarsel_training`
         WHERE eligible AND %s IS NOT NULL""",
      m.mname, xf(m.cols), g.l2, ARRAY_TO_STRING(m.cols, ', '), m.label, m.label);
    EXECUTE IMMEDIATE FORMAT("""
      INSERT INTO `disruptions.togvarsel_tuning`
      SELECT '%s', %f, log_loss, roc_auc,
        (SELECT COUNTIF(NOT is_tune) FROM `disruptions.togvarsel_training` WHERE eligible AND %s IS NOT NULL),
        (SELECT COUNTIF(is_tune) FROM `disruptions.togvarsel_training` WHERE eligible AND %s IS NOT NULL),
        FALSE, CURRENT_TIMESTAMP()
      FROM ML.EVALUATE(MODEL `disruptions.togvarsel_tune_%s`)""",
      m.mname, g.l2, m.label, m.label, m.mname);
  END FOR;

  SET best_l2 = (SELECT l2_reg FROM `disruptions.togvarsel_tuning`
                 WHERE model_name = m.mname
                 ORDER BY holdout_log_loss, l2_reg DESC LIMIT 1);
  UPDATE `disruptions.togvarsel_tuning` SET chosen = TRUE
  WHERE model_name = m.mname AND l2_reg = best_l2;

  -- Final fit: everything eligible, tuning holdout folded back in, split disabled. Trains a
  -- CANDIDATE; retrain.sh promotes all four to togvarsel_m_* only after this whole script has
  -- succeeded, so a failed retrain leaves the live model family untouched (never mixed old/new).
  EXECUTE IMMEDIATE FORMAT("""
      CREATE OR REPLACE MODEL `disruptions.togvarsel_m_cand_%s`
      TRANSFORM(
        %s,
        label)
      OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['label'],
              data_split_method='NO_SPLIT',
              l2_reg=%f, max_iterations=50, early_stop=TRUE,
              enable_global_explain=TRUE)
      AS SELECT %s, %s AS label
         FROM `disruptions.togvarsel_training`
         WHERE eligible AND %s IS NOT NULL""",
      m.mname, xf(m.cols), best_l2, ARRAY_TO_STRING(m.cols, ', '), m.label, m.label);
  EXECUTE IMMEDIATE FORMAT("DROP MODEL IF EXISTS `disruptions.togvarsel_tune_%s`", m.mname);
END FOR;
