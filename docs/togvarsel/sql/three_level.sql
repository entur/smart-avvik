WITH pp AS (
  SELECT 'AM@06:30' fc, d, label, p FROM `ent-kixavvik-dev.togvarsel.pred_lr_am_0630_A` UNION ALL SELECT 'AM@06:30', d, label, p FROM `ent-kixavvik-dev.togvarsel.pred_lr_am_0630_B`
  UNION ALL SELECT 'PM@06:30', d, label, p FROM `ent-kixavvik-dev.togvarsel.pred_lr_pm_0630_A` UNION ALL SELECT 'PM@06:30', d, label, p FROM `ent-kixavvik-dev.togvarsel.pred_lr_pm_0630_B`
  UNION ALL SELECT 'PM@14:00', d, label, p FROM `ent-kixavvik-dev.togvarsel.pred_lr_pm_1400_A` UNION ALL SELECT 'PM@14:00', d, label, p FROM `ent-kixavvik-dev.togvarsel.pred_lr_pm_1400_B`
)
SELECT fc, CASE WHEN p < 0.10 THEN '1 green' WHEN p < 0.30 THEN '2 yellow' ELSE '3 red' END level, COUNT(*) n_days, ROUND(COUNT(*)/SUM(COUNT(*)) OVER (PARTITION BY fc),2) share,
  ROUND(AVG(CAST(label AS INT64)),3) obs_bad_rate,
  ROUND(AVG(IF(fc LIKE 'AM%', f.am_t5, f.pm_t5)),3) mean_trouble, ROUND(AVG(IF(fc LIKE 'AM%', f.am_cancel, f.pm_cancel)),3) mean_cancel
FROM pp JOIN `ent-kixavvik-dev.togvarsel.features` f USING (d) GROUP BY 1,2 ORDER BY 1,2
