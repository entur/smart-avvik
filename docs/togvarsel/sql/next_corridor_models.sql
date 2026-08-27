-- Corridor-level AM/PM forecast (pooled over west / northeast / south), two time-folds.
-- Compare against the area-wide model's prediction evaluated on the same corridor labels.
-- Run each block with sql/_q.sh (or bq query < file) once gcloud auth is refreshed.
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.togvarsel_lr_corr_am_0630_A`
OPTIONS(model_type='LOGISTIC_REG', l2_reg=0.5, max_iterations=50, data_split_method='NO_SPLIT', input_label_cols=['label']) AS
SELECT corridor, early_t5, early_cx, et_t5_0630_all, prev_eve_t5, prev_pm_t5_c, sx_inn_0630_c, sx_amj_inn_0630_c, sx_amj_0630_c, sx_big_0630_c, sx_infra_0630_c, sx_infra_0630,
  CAST(dow AS STRING) dow_s, low_season, am_t5 >= 0.25 AS label
FROM `ent-kixavvik-dev.disruptions.togvarsel_features_corridor`
WHERE weekday AND NOT holiday AND am_n > 80 AND pm_n > 80 AND d < '2026-04-01';

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_corr_am_0630_A` AS
SELECT d, corridor, label, (SELECT x.prob FROM UNNEST(predicted_label_probs) x WHERE x.label = TRUE) AS p
FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.togvarsel_lr_corr_am_0630_A`, (
  SELECT d, corridor, early_t5, early_cx, et_t5_0630_all, prev_eve_t5, prev_pm_t5_c, sx_inn_0630_c, sx_amj_inn_0630_c, sx_amj_0630_c, sx_big_0630_c, sx_infra_0630_c, sx_infra_0630,
    CAST(dow AS STRING) dow_s, low_season, am_t5 >= 0.25 AS label
  FROM `ent-kixavvik-dev.disruptions.togvarsel_features_corridor`
  WHERE weekday AND NOT holiday AND am_n > 80 AND pm_n > 80 AND d >= '2026-04-01'));

-- AUC per corridor: corridor model vs area-wide model on the same corridor labels
WITH c AS (SELECT d, corridor, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_corr_am_0630_A`),
a AS (SELECT c.d, c.corridor, c.label, ar.p FROM c JOIN `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_am_0630_A` ar USING (d)),
u AS (SELECT 'corridor_model' m, * FROM c UNION ALL SELECT 'area_model', * FROM a),
r AS (SELECT *, RANK() OVER (PARTITION BY m, corridor ORDER BY p) rk FROM u)
SELECT m, corridor, COUNT(*) n, COUNTIF(label) n_pos,
  ROUND(SAFE_DIVIDE(SUM(IF(label, rk, 0)) - COUNTIF(label)*(COUNTIF(label)+1)/2, COUNTIF(label)*COUNTIF(NOT label)),3) auc
FROM r GROUP BY 1,2 ORDER BY 2,1;
