CREATE OR REPLACE MODEL `ent-kixavvik-dev.togvarsel.lr_pm_1400_A`
OPTIONS(model_type='LOGISTIC_REG', l2_reg=0.5, max_iterations=50, data_split_method='NO_SPLIT', input_label_cols=['label']) AS
SELECT et_t5_1400, et_t5_last_1400, et_cx_1400, sx_inn_1200, sx_infra_1200, sx_pmj_1200, CAST(dow AS STRING) dow_s, low_season, pm_bad AS label FROM `ent-kixavvik-dev.togvarsel.features` WHERE weekday AND NOT holiday AND am_n > 300 AND pm_n > 300 AND d < '2026-04-01';
CREATE OR REPLACE TABLE `ent-kixavvik-dev.togvarsel.pred_lr_pm_1400_A` AS
SELECT d, label, predicted_label_probs[OFFSET(0)].prob AS p_raw, (SELECT p.prob FROM UNNEST(predicted_label_probs) p WHERE p.label = TRUE) AS p
FROM ML.PREDICT(MODEL `ent-kixavvik-dev.togvarsel.lr_pm_1400_A`, (SELECT d, et_t5_1400, et_t5_last_1400, et_cx_1400, sx_inn_1200, sx_infra_1200, sx_pmj_1200, CAST(dow AS STRING) dow_s, low_season, pm_bad AS label FROM `ent-kixavvik-dev.togvarsel.features` WHERE weekday AND NOT holiday AND am_n > 300 AND pm_n > 300 AND d >= '2026-04-01'));
