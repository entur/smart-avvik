SELECT
  IF(parent_job_id IS NULL, 'parent', 'child') lvl,
  CASE WHEN query LIKE '%BOOSTED_TREE%' THEN 'bt' WHEN query LIKE '%LOGISTIC_REG%' THEN 'lr' ELSE 'other' END kind,
  COUNT(*) n, ROUND(SUM(total_bytes_billed)/1e9,1) gb, ROUND(SUM(total_bytes_billed)/1e12*6.25,3) usd, ROUND(SUM(total_slot_ms)/3.6e6,1) slot_h
FROM `ent-kixavvik-dev`.`region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP('2026-08-27 20:00:00') AND query LIKE '%exp_togvarsel%'
GROUP BY 1,2 ORDER BY 1,2
