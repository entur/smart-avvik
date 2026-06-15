SELECT MAX(ingestionTime) AS last_ingestion_time
FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
WHERE DATE(_PARTITIONTIME) >= CURRENT_DATE()
