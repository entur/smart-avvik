-- Trains and compares NSB duration models that differ ONLY in their geography
-- features, plus a no-geography model and naive baselines, to settle whether
-- location earns its place at all.
--
--   dur_both        category/severity/planned/counts/time + stops + corridors
--   dur_stops       ... + stops only
--   dur_corridors   ... + corridors only
--   dur_nogeo       ... no geography at all   <- the key comparison
--   naive_median    predict the train-set median duration for everyone
--   naive_mean      predict the train-set mean duration for everyone
--
-- Geography's true contribution = (dur_nogeo error) - (dur_both error).
-- If that gap is ~0, drop the ~150 stop_/od_ columns and keep dur_nogeo.
--
-- Target is LN(duration_minutes) (skew fix); predictions are EXP()'d back to
-- minutes. is_eval is the leakage-safe split column (trains on is_eval = FALSE).

DECLARE od_list      STRING;
DECLARE stop_list    STRING;
DECLARE train_median FLOAT64;
DECLARE train_mean   FLOAT64;

-- Comma-separated column families, used to drop an encoding via SELECT * EXCEPT.
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

-- Naive constants, computed on the TRAIN rows only (no leakage).
SET train_median = (
  SELECT CAST(APPROX_QUANTILES(duration_minutes, 2)[OFFSET(1)] AS FLOAT64)
  FROM `disruptions.training` WHERE NOT is_eval
);
SET train_mean = (
  SELECT AVG(duration_minutes) FROM `disruptions.training` WHERE NOT is_eval
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

-- 4. No geography (the key baseline) ------------------------------------------
EXECUTE IMMEDIATE FORMAT("""
CREATE OR REPLACE MODEL `disruptions.dur_nogeo`
OPTIONS(
  model_type            = 'BOOSTED_TREE_REGRESSOR',
  input_label_cols      = ['label'],
  data_split_method     = 'CUSTOM',
  data_split_col        = 'is_eval',
  max_iterations        = 50,
  early_stop            = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(duration_minutes, %s, %s), LN(duration_minutes) AS label
FROM `disruptions.training`
""", stop_list, od_list);

-- 5. Feature importance for the full model ------------------------------------
-- (Run on its own to view; a script only returns the final statement's rows.)
SELECT *
FROM ML.GLOBAL_EXPLAIN(MODEL `disruptions.dur_both`)
ORDER BY attribution DESC
LIMIT 25;

-- 6. Compare everything on the held-out eval rows, in real minutes ------------
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
  UNION ALL
  SELECT 'nogeo', duration_minutes, EXP(predicted_label)
  FROM ML.PREDICT(MODEL `disruptions.dur_nogeo`,
        (SELECT * FROM `disruptions.training` WHERE is_eval))
  UNION ALL
  SELECT 'naive_median', duration_minutes, train_median
  FROM `disruptions.training` WHERE is_eval
  UNION ALL
  SELECT 'naive_mean', duration_minutes, train_mean
  FROM `disruptions.training` WHERE is_eval
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
