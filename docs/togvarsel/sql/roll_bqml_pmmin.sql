DECLARE months ARRAY<DATE> DEFAULT GENERATE_DATE_ARRAY('2025-10-01', '2026-08-01', INTERVAL 1 MONTH);
DECLARE specs ARRAY<STRUCT<mname STRING, label STRING, l2 FLOAT64, cols ARRAY<STRING>>>;
CREATE TEMP FUNCTION xf(cols ARRAY<STRING>) AS ((
  SELECT STRING_AGG(
    IF(REGEXP_CONTAINS(c, r'^(cat_|catj_|sx_|et_cx|et_n|early_cx|nlines_|ncorr_)'),
       FORMAT('LN(1 + GREATEST(%s, 0)) AS %s', c, c), c), ',\n    ' ORDER BY o)
  FROM UNNEST(cols) c WITH OFFSET o
));
SET specs = [
  STRUCT('pm1200' AS mname, 'y_pm10' AS label, 3.0 AS l2,
    ['et_t5_last_1200','et_cx_1200','prev_pm_t5','low_season','dow_6','am_t10_known'] AS cols),
  STRUCT('pm1400', 'y_pm10', 10.0,
    ['et_t5_last_1400','et_cx_1400','et_t5_1200_1350','prev_pm_t5','low_season','dow_6','am_t10_known','sx_pmj_inn_1400'])
];
CREATE TABLE IF NOT EXISTS `disruptions.exp_rush_roll_pred` (
  variant STRING, mname STRING, fold DATE, d DATE, label BOOL, p FLOAT64, trained_at TIMESTAMP);
DELETE FROM `disruptions.exp_rush_roll_pred` WHERE variant = 'pmmin';
FOR m IN (SELECT mname, label, l2, cols FROM UNNEST(specs)) DO
  FOR f IN (SELECT m0 FROM UNNEST(months) m0) DO
    EXECUTE IMMEDIATE FORMAT("""
      CREATE OR REPLACE MODEL `disruptions.exp_rush_roll_pmmin_%s`
      TRANSFORM(
        %s,
        label)
      OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['label'],
              data_split_method='NO_SPLIT', l2_reg=%f, max_iterations=50, early_stop=TRUE)
      AS SELECT %s, %s AS label
         FROM `disruptions.exp_rush_training3`
         WHERE eligible AND %s IS NOT NULL AND d < '%t'""",
      m.mname, xf(m.cols), m.l2, ARRAY_TO_STRING(m.cols, ', '), m.label, m.label, f.m0);
    EXECUTE IMMEDIATE FORMAT("""
      INSERT INTO `disruptions.exp_rush_roll_pred`
      SELECT 'pmmin', '%s', DATE '%t', d, label,
        (SELECT prob FROM UNNEST(predicted_label_probs) WHERE LOWER(CAST(label AS STRING)) = 'true'),
        CURRENT_TIMESTAMP()
      FROM ML.PREDICT(MODEL `disruptions.exp_rush_roll_pmmin_%s`,
        (SELECT *, %s AS label FROM `disruptions.exp_rush_training3`
         WHERE eligible AND %s IS NOT NULL AND d >= '%t' AND d < DATE_ADD('%t', INTERVAL 1 MONTH)))""",
      m.mname, f.m0, m.mname, m.label, m.label, f.m0, f.m0);
  END FOR;
END FOR;
