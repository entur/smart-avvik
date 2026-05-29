CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.training_cat2` AS

WITH disruptions AS (SELECT
  TIMESTAMP_DIFF(TIMESTAMP(v.endTime), TIMESTAMP(v.startTime), MINUTE) AS duration_minutes,
  d.value AS description,
  severity,
  (
    SELECT ARRAY_AGG(stops.stopPointRef ORDER BY stops.stopPointRef)
    FROM UNNEST(affects.stopPoints) AS stops
    WHERE stops.stopPointRef IS NOT NULL
  ) AS affected_stops,
  (
    SELECT ARRAY_AGG(journeys.lineRef ORDER BY journeys.lineRef)
    FROM UNNEST(affects.vehicleJourneys) AS journeys
    WHERE journeys.lineRef IS NOT NULL
  ) AS affected_lines,
  EXTRACT(YEAR FROM TIMESTAMP(v.startTime)) AS year,
  EXTRACT(MONTH FROM TIMESTAMP(v.startTime)) AS month,
  EXTRACT(DAY FROM TIMESTAMP(v.startTime)) AS day,
  EXTRACT(DAYOFWEEK FROM TIMESTAMP(v.startTime)) AS dayofweek,
  EXTRACT(HOUR FROM TIMESTAMP(v.startTime)) AS hour,
FROM
  `ent-anshar-prd.realtime.siri_realtime_sx_production`,
  UNNEST(validityPeriods) AS v,
  UNNEST(descriptions) AS d
WHERE 
  TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR))
  AND severity != "NO_IMPACT"
  AND v.startTime IS NOT NULL
  AND v.endTime IS NOT NULL
  AND (d.language IS NULL OR UPPER(d.language) = 'NO'))

SELECT 
  c.category,
  d.*
FROM 
  disruptions d
JOIN
  disruptions.categories c
ON
  d.description = c.description
WHERE 
  (ARRAY_LENGTH(affected_stops) > 0
  OR ARRAY_LENGTH(affected_lines) > 0)
  AND duration_minutes IS NOT NULL
 AND duration_minutes > 0
 AND duration_minutes <= 600
