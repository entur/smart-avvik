-- Change-detection probe for the ingestion poller (main.py:get_last_ingestion_time).
-- Returns the latest ingestionTime across today's SIRI-SX partition; main.py polls this once a
-- minute and, when the value advances, rebuilds the downstream tables in order
-- (rail_calls_today -> disruptions_initial_today -> categories -> predictions_today).
--
-- _PARTITIONTIME >= CURRENT_DATE() prunes to today's partition so the probe stays a tiny scan;
-- ingestionTime is the per-row load time, so MAX advances every time Anshar lands new SX data.
SELECT MAX(ingestionTime) AS last_ingestion_time
FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
WHERE DATE(_PARTITIONTIME) >= CURRENT_DATE()
