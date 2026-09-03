-- docs/gps-matching 2026-09-02. Dry run 0.28 GB. Coverage of actual times split by first / intermediate / last call of the journey.

-- coverage of actual times on INTERMEDIATE calls (first call has no arrival, last no departure by construction)
WITH r AS (
  SELECT dataSource, datedServiceJourneyId, operatingDate, sequenceNr, arrivalTime, departureTime,
    MIN(sequenceNr) OVER (PARTITION BY datedServiceJourneyId, operatingDate) s_min,
    MAX(sequenceNr) OVER (PARTITION BY datedServiceJourneyId, operatingDate) s_max
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded`
  WHERE operatingDate BETWEEN '2026-08-25' AND '2026-08-31'
    AND dataSource IN ('VYG','GOA','FLT') AND estimated = FALSE AND NOT IFNULL(stopCancellation, FALSE)
)
SELECT dataSource,
  CASE WHEN sequenceNr = s_min THEN 'first' WHEN sequenceNr = s_max THEN 'last' ELSE 'intermediate' END pos,
  COUNT(*) n,
  ROUND(COUNTIF(arrivalTime IS NOT NULL)/COUNT(*),4) f_arr,
  ROUND(COUNTIF(departureTime IS NOT NULL)/COUNT(*),4) f_dep,
  ROUND(COUNTIF(arrivalTime IS NOT NULL AND departureTime IS NOT NULL)/COUNT(*),4) f_both
FROM r GROUP BY 1,2 ORDER BY 1,2
