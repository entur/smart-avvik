-- Augment the initial-version training table with leakage-safe pre-onset ET
-- features and retrain both duration models, to measure what realized ET delays
-- add over the pure SX-onset feature set. is_eval is the SAME FARM_FINGERPRINT
-- split, so eval rows are identical -> a fair head-to-head vs dur_init_*.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.training_initial_et` AS
SELECT t.*, f.* EXCEPT(situationNumber)
FROM `disruptions.training_initial` AS t
JOIN `disruptions.preonset_features` AS f USING (situationNumber);

CREATE OR REPLACE MODEL `disruptions.dur_init_reopen_et`
OPTIONS(model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), LN(reopen_minutes) AS label
FROM `disruptions.training_initial_et`;

CREATE OR REPLACE MODEL `disruptions.dur_init_impact_et`
OPTIONS(model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), LN(impact_minutes) AS label
FROM `disruptions.training_initial_et`;
