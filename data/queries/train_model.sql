-- Trains and compares three NSB duration models that differ ONLY in their
-- geography features, to test stops vs. corridors vs. both.
--
-- Target is LN(duration_minutes) to tame the right-skew; predictions are
-- exponentiated back to minutes for evaluation. To train on raw minutes instead,
-- swap `LN(duration_minutes) AS label` -> `duration_minutes AS label` in each
-- model and drop the EXP() in the comparison query (step 4).
--
-- is_eval is passed as DATA_SPLIT_COL, so it trains on is_eval = FALSE and is
-- never used as a feature. The comparison in step 4 scores the same held-out
-- (is_eval = TRUE) rows.

DECLARE od_list   STRING;
DECLARE stop_list STRING;

-- Comma-separated column families, used to drop one encoding via SELECT * EXCEPT.
SET od_list = (
  SELECT STRING_AGG(column_name)
  FROM `disruptions`.INFORMATION_SCHEMA.COLUMNS
  WHERE table_name = 'training' AND column_name LIKE 'od_%'
);
SET stop_list = (
  SELECT STRING_AGG(column_name)
  FROM `disruptions`.INFORMATION_SCHEMA.COLUMNS
  WHERE table_name = 'training' AND column_name LIKE 'stop_%'
);

-- 1. Both encodings -----------------------------------------------------------
CREATE OR REPLACE MODEL `disruptions.dur_both`
OPTIONS(
  model_type            = 'BOOSTED_TREE_REGRESSOR',
  input_label_cols      = ['label'],
  data_split_method     = 'CUSTOM',
  data_split_col        = 'is_eval',
  max_iterations        = 50,
  early_stop            = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(duration_minutes), LN(duration_minutes) AS label
FROM `disruptions.training`;

-- 2. Stops only (drop corridor columns) ---------------------------------------
EXECUTE IMMEDIATE FORMAT("""
CREATE OR REPLACE MODEL `disruptions.dur_stops`
OPTIONS(
  model_type            = 'BOOSTED_TREE_REGRESSOR',
  input_label_cols      = ['label'],
  data_split_method     = 'CUSTOM',
  data_split_col        = 'is_eval',
  max_iterations        = 50,
  early_stop            = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(duration_minutes, %s), LN(duration_minutes) AS label
FROM `disruptions.training`
""", od_list);

-- 3. Corridors only (drop stop columns) ---------------------------------------
EXECUTE IMMEDIATE FORMAT("""
CREATE OR REPLACE MODEL `disruptions.dur_corridors`
OPTIONS(
  model_type            = 'BOOSTED_TREE_REGRESSOR',
  input_label_cols      = ['label'],
  data_split_method     = 'CUSTOM',
  data_split_col        = 'is_eval',
  max_iterations        = 50,
  early_stop            = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(duration_minutes, %s), LN(duration_minutes) AS label
FROM `disruptions.training`
""", stop_list);

-- 4. Compare on the held-out eval rows, back-transformed to real minutes ------
WITH preds AS (
  SELECT 'both' AS model, duration_minutes AS actual, EXP(predicted_label) AS pred
  FROM ML.PREDICT(MODEL `disruptions.dur_both`,
        (SELECT * FROM `disruptions.training` WHERE is_eval))
  UNION ALL
  SELECT 'stops', duration_minutes, EXP(predicted_label)
  FROM ML.PREDICT(MODEL `disruptions.dur_stops`,
        (SELECT * FROM `disruptions.training` WHERE is_eval))
  UNION ALL
  SELECT 'corridors', duration_minutes, EXP(predicted_label)
  FROM ML.PREDICT(MODEL `disruptions.dur_corridors`,
        (SELECT * FROM `disruptions.training` WHERE is_eval))
)
SELECT
  model,
  COUNT(*)                                                    AS n_eval,
  ROUND(AVG(ABS(pred - actual)), 1)                           AS mae_min,
  ROUND(APPROX_QUANTILES(ABS(pred - actual), 2)[OFFSET(1)], 1) AS median_ae_min,
  ROUND(SQRT(AVG(POW(pred - actual, 2))), 1)                  AS rmse_min,
  ROUND(AVG(ABS(pred - actual) / actual), 3)                  AS mape
FROM preds
GROUP BY model
ORDER BY mae_min;
