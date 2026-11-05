-- Augment the duration models with leakage-safe pre-onset WEATHER features and
-- retrain, to measure what ambient weather adds over the SX-onset + ET feature
-- sets. is_eval is the SAME FARM_FINGERPRINT split -> eval rows are identical, so
-- every comparison below is a fair head-to-head vs dur_init_* / dur_init_*_et.
-- Prereq: create_weather_features.sql (table `disruptions.weather_features`).
--
-- VERDICT (held-out eval, n=1568): weather does NOT improve the duration POINT
-- predictions. See docs/predict-duration/LEARNINGS.md "Weather features (2026-06)".
--   reopen  : +weather flat-to-worse (the "track clear" timing is weather-independent).
--   impact  : +weather ~= baseline; +ET+weather ~= +ET (weather is redundant with
--             category + pre-onset ET, which already price in severe weather).
-- The raw signal is real (snow / high wind ~2x median impact) but already captured.
-- Weather's only, marginal niche is long-impact RISK (tail classifier below):
-- AUC 0.767 -> 0.776, and it is the only family that helps there (ET is flat) --
-- though that lift is within the eval AUC standard error (~0.02), so it is
-- suggestive, not conclusive. Not wired into production.

-- Training tables: weather only, and ET+weather.
CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.training_initial_wx` AS
SELECT t.*, w.* EXCEPT(situationNumber)
FROM `disruptions.training_initial` t JOIN `disruptions.weather_features` w USING (situationNumber);

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.training_initial_etwx` AS
SELECT t.*, w.* EXCEPT(situationNumber)
FROM `disruptions.training_initial_et` t JOIN `disruptions.weather_features` w USING (situationNumber);

-- Regressors (LN label), same options/split as dur_init_*.
CREATE OR REPLACE MODEL `disruptions.dur_init_reopen_wx`
OPTIONS(model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), LN(reopen_minutes) AS label
FROM `disruptions.training_initial_wx`;

CREATE OR REPLACE MODEL `disruptions.dur_init_impact_wx`
OPTIONS(model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), LN(impact_minutes) AS label
FROM `disruptions.training_initial_wx`;

CREATE OR REPLACE MODEL `disruptions.dur_init_impact_etwx`
OPTIONS(model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), LN(impact_minutes) AS label
FROM `disruptions.training_initial_etwx`;

-- Long-impact (>=120 min) RISK classifiers: base / +ET / +ET+weather. base rate 25%.
-- Read roc_auc with: SELECT roc_auc FROM ML.EVALUATE(MODEL `disruptions.tail_clf_etwx`);
CREATE OR REPLACE MODEL `disruptions.tail_clf_base`
OPTIONS(model_type='BOOSTED_TREE_CLASSIFIER', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval', max_iterations=50, early_stop=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), impact_minutes>=120 AS label
FROM `disruptions.training_initial`;

CREATE OR REPLACE MODEL `disruptions.tail_clf_et`
OPTIONS(model_type='BOOSTED_TREE_CLASSIFIER', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval', max_iterations=50, early_stop=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), impact_minutes>=120 AS label
FROM `disruptions.training_initial_et`;

CREATE OR REPLACE MODEL `disruptions.tail_clf_etwx`
OPTIONS(model_type='BOOSTED_TREE_CLASSIFIER', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval', max_iterations=50, early_stop=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), impact_minutes>=120 AS label
FROM `disruptions.training_initial_etwx`;

-- Head-to-head point-model eval (MAE / median AE, all + bulk) lives in
-- /tmp/eval_wx.sql in the investigation session; reproduced results in LEARNINGS.
