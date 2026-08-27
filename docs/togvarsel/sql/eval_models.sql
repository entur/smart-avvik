WITH pp AS (
  SELECT 'am_0630' m, 'A' fold, d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_am_0630_A` UNION ALL
  SELECT 'am_0630', 'B', d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_am_0630_B` UNION ALL
  SELECT 'pm_0630', 'A', d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_0630_A` UNION ALL
  SELECT 'pm_0630', 'B', d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_0630_B` UNION ALL
  SELECT 'pm_0900', 'A', d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_0900_A` UNION ALL
  SELECT 'pm_0900', 'B', d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_0900_B` UNION ALL
  SELECT 'pm_1200', 'A', d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_1200_A` UNION ALL
  SELECT 'pm_1200', 'B', d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_1200_B` UNION ALL
  SELECT 'pm_1400', 'A', d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_1400_A` UNION ALL
  SELECT 'pm_1400', 'B', d, label, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_1400_B`
),
r AS (SELECT *, RANK() OVER (PARTITION BY m, fold ORDER BY p) rk FROM pp),
a AS (
  SELECT m, fold, COUNT(*) n, COUNTIF(label) n_pos,
    SAFE_DIVIDE(SUM(IF(label, rk, 0)) - COUNTIF(label)*(COUNTIF(label)+1)/2, COUNTIF(label)*COUNTIF(NOT label)) auc
  FROM r GROUP BY 1,2
),
top AS (
  SELECT m, fold, 
    AVG(IF(p >= 0.30, CAST(label AS INT64), NULL)) prec_at_30, COUNTIF(p >= 0.30) n_at_30, SAFE_DIVIDE(COUNTIF(p>=0.30 AND label), COUNTIF(label)) rec_at_30,
    AVG(IF(p >= 0.40, CAST(label AS INT64), NULL)) prec_at_40, COUNTIF(p >= 0.40) n_at_40, SAFE_DIVIDE(COUNTIF(p>=0.40 AND label), COUNTIF(label)) rec_at_40,
    AVG(IF(p < 0.10, CAST(label AS INT64), NULL)) rate_below_10, COUNTIF(p < 0.10) n_below_10
  FROM pp GROUP BY 1,2
)
SELECT a.m, a.fold, n, n_pos, ROUND(n_pos/n,3) base, ROUND(a.auc,3) auc, ROUND(prec_at_30,2) prec30, n_at_30, ROUND(rec_at_30,2) rec30, ROUND(prec_at_40,2) prec40, n_at_40, ROUND(rec_at_40,2) rec40, ROUND(rate_below_10,3) rate_lt10, n_below_10
FROM a JOIN top USING (m, fold) ORDER BY m, fold
