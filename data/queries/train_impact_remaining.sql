-- DURING-disruption re-prediction models for REMAINING impact. Joins checkpoint
-- features to the onset feature set (training_initial), then trains two models on
-- LN(remaining_minutes) with the SAME situation-level is_eval split (a situation is
-- entirely train or entirely eval -> no within-situation leakage, and the eval
-- situations match the onset models for a fair comparison):
--   impact_remaining_full -- onset feats + elapsed + reopen-state + ET-observed-so-far
--   impact_remaining_noet -- ABLATION: drops the ET-observed-so-far features, to
--                            isolate what watching ET DURING the disruption adds
--                            beyond just knowing elapsed time and the reopen event.
-- raw reopen_minutes / impact_minutes are NOT carried as features (future knowledge).

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.training_checkpoints` AS
SELECT
  ck.*,
  ti.* EXCEPT(situationNumber, reopen_minutes, impact_minutes, is_eval),
  ti.is_eval
FROM `disruptions.impact_checkpoints` AS ck
JOIN `disruptions.training_initial` AS ti USING (situationNumber);

CREATE OR REPLACE MODEL `disruptions.impact_remaining_full`
OPTIONS(model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, remaining_minutes), LN(remaining_minutes) AS label
FROM `disruptions.training_checkpoints`;

CREATE OR REPLACE MODEL `disruptions.impact_remaining_noet`
OPTIONS(model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, remaining_minutes,
                cum_impacted, cum_cancel, cum_maxdelay,
                rec_impacted, rec_maxdelay, mins_since_last_impact),
       LN(remaining_minutes) AS label
FROM `disruptions.training_checkpoints`;
