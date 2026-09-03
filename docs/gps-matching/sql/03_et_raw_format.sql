-- docs/gps-matching 2026-09-02. Dry run 12.1 GB (one ET partition, rail). Raw string format of the actual times and three samples per operator.

-- raw ET one day rail: format and seconds distribution of actual times, by dataSource, plus 3 samples
SELECT dataSource,
  COUNT(*) n_calls,
  COUNTIF(rc.actualArrivalTime IS NOT NULL) n_act_arr,
  COUNTIF(rc.actualDepartureTime IS NOT NULL) n_act_dep,
  ROUND(COUNTIF(ENDS_WITH(SUBSTR(rc.actualDepartureTime,1,19),':00'))/NULLIF(COUNTIF(rc.actualDepartureTime IS NOT NULL),0),3) f_dep_sec00,
  ROUND(COUNTIF(REGEXP_CONTAINS(rc.actualDepartureTime, r'\.\d'))/NULLIF(COUNTIF(rc.actualDepartureTime IS NOT NULL),0),3) f_dep_fractional,
  ROUND(COUNTIF(rc.actualDepartureTime = rc.actualArrivalTime)/NULLIF(COUNTIF(rc.actualDepartureTime IS NOT NULL AND rc.actualArrivalTime IS NOT NULL),0),3) f_arr_eq_dep,
  ROUND(COUNTIF(rc.actualDepartureTime = rc.expectedDepartureTime)/NULLIF(COUNTIF(rc.actualDepartureTime IS NOT NULL AND rc.expectedDepartureTime IS NOT NULL),0),3) f_actual_eq_expected,
  ROUND(COUNTIF(rc.actualDepartureTime = rc.aimedDepartureTime)/NULLIF(COUNTIF(rc.actualDepartureTime IS NOT NULL),0),3) f_actual_eq_aimed,
  ARRAY_AGG(STRUCT(rc.stopPointRef, rc.aimedArrivalTime AS aa, rc.expectedArrivalTime AS ea, rc.actualArrivalTime AS acta, rc.aimedDepartureTime AS ad, rc.expectedDepartureTime AS ed, rc.actualDepartureTime AS actd, rc.arrivalStatus AS ast, rc.departureStatus AS dst)
            ORDER BY rc.actualDepartureTime DESC LIMIT 3) samples
FROM `ent-anshar-prd.realtime.siri_realtime_et_production`, UNNEST(recordedCalls) rc
WHERE _PARTITIONTIME = TIMESTAMP('2026-09-01') AND dataSource IN ('VYG','GOA','SJN','FLT')
GROUP BY 1 ORDER BY 1
