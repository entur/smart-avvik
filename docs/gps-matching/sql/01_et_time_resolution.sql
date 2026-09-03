-- docs/gps-matching 2026-09-02. Dry run 0.28 GB. Rail, 7 days of last_recorded: how many recorded calls carry actual times, whether they are minute-rounded, and whether arrival and departure are separate events.

-- last_recorded: coverage and resolution of recorded times, rail, 7 days
WITH r AS (
  SELECT dataSource, estimated, stopCancellation, arrivalTime, departureTime, aimedArrivalTime, aimedDepartureTime, sequenceNr
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded`
  WHERE operatingDate BETWEEN '2026-08-25' AND '2026-08-31'
    AND dataSource IN ('VYG','NSB','GOA','SJN','FLT','VY')
)
SELECT dataSource, estimated,
  COUNT(*) n,
  ROUND(COUNTIF(arrivalTime IS NOT NULL)/COUNT(*),3) f_arr,
  ROUND(COUNTIF(departureTime IS NOT NULL)/COUNT(*),3) f_dep,
  ROUND(COUNTIF(EXTRACT(SECOND FROM arrivalTime)=0)/NULLIF(COUNTIF(arrivalTime IS NOT NULL),0),3) f_arr_sec00,
  ROUND(COUNTIF(EXTRACT(SECOND FROM departureTime)=0)/NULLIF(COUNTIF(departureTime IS NOT NULL),0),3) f_dep_sec00,
  ROUND(COUNTIF(EXTRACT(MICROSECOND FROM departureTime)<>0)/NULLIF(COUNTIF(departureTime IS NOT NULL),0),3) f_dep_subsec,
  ROUND(COUNTIF(arrivalTime = departureTime)/NULLIF(COUNTIF(arrivalTime IS NOT NULL AND departureTime IS NOT NULL),0),3) f_arr_eq_dep,
  APPROX_QUANTILES(TIMESTAMP_DIFF(departureTime, arrivalTime, SECOND), 20)[OFFSET(1)] dwell_p05,
  APPROX_QUANTILES(TIMESTAMP_DIFF(departureTime, arrivalTime, SECOND), 20)[OFFSET(10)] dwell_p50,
  APPROX_QUANTILES(TIMESTAMP_DIFF(departureTime, arrivalTime, SECOND), 20)[OFFSET(19)] dwell_p95,
  APPROX_QUANTILES(TIMESTAMP_DIFF(aimedDepartureTime, aimedArrivalTime, SECOND), 20)[OFFSET(10)] aimed_dwell_p50,
  ROUND(COUNTIF(TIMESTAMP_DIFF(departureTime, arrivalTime, SECOND) < 0)/NULLIF(COUNTIF(arrivalTime IS NOT NULL AND departureTime IS NOT NULL),0),4) f_negative_dwell
FROM r
WHERE NOT IFNULL(stopCancellation, FALSE)
GROUP BY 1,2 ORDER BY 1,2
