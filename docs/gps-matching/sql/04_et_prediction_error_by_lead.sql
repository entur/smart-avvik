-- docs/gps-matching 2026-09-02. Dry run 15.4 GB (one ET partition, VYG). Every snapshot's expectedDepartureTime against the actualDepartureTime later recorded, bucketed by lead time.

-- ET prediction error: expectedDepartureTime in a snapshot vs the actualDepartureTime later recorded, by lead time. VYG, one day.
WITH msgs AS (
  SELECT recordedAtTime, datedVehicleJourneyRef AS dvj, estimatedCalls, recordedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  WHERE _PARTITIONTIME = TIMESTAMP('2026-09-01') AND dataSource = 'VYG' AND datedVehicleJourneyRef IS NOT NULL
),
last_msg AS (
  SELECT dvj, recordedCalls FROM msgs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY dvj ORDER BY recordedAtTime DESC) = 1
),
actual AS (
  SELECT dvj, rc.stopPointRef, rc.order, SAFE.TIMESTAMP(rc.actualDepartureTime) act_dep
  FROM last_msg, UNNEST(recordedCalls) rc
  WHERE rc.actualDepartureTime IS NOT NULL AND NOT IFNULL(rc.cancellation, FALSE)
),
preds AS (
  SELECT dvj, ec.stopPointRef, ec.order, SAFE.TIMESTAMP(m.recordedAtTime) rec_at,
         SAFE.TIMESTAMP(ec.expectedDepartureTime) exp_dep, SAFE.TIMESTAMP(ec.aimedDepartureTime) aimed_dep
  FROM msgs m, UNNEST(estimatedCalls) ec
  WHERE ec.expectedDepartureTime IS NOT NULL AND NOT IFNULL(ec.cancellation, FALSE)
),
j AS (
  SELECT p.*, a.act_dep,
         TIMESTAMP_DIFF(a.act_dep, p.exp_dep, SECOND) err_s,          -- >0: train left later than predicted
         TIMESTAMP_DIFF(a.act_dep, p.aimed_dep, SECOND) aimed_err_s,  -- the timetable as predictor
         TIMESTAMP_DIFF(a.act_dep, p.rec_at, SECOND) lead_s
  FROM preds p JOIN actual a USING (dvj, stopPointRef, `order`)
)
SELECT CASE WHEN lead_s < 120 THEN 'a 0-2 min' WHEN lead_s < 300 THEN 'b 2-5' WHEN lead_s < 600 THEN 'c 5-10'
            WHEN lead_s < 1200 THEN 'd 10-20' WHEN lead_s < 2400 THEN 'e 20-40' WHEN lead_s < 4800 THEN 'f 40-80' ELSE 'g 80+' END lead,
  COUNT(*) n,
  APPROX_QUANTILES(err_s, 100)[OFFSET(50)] med_signed_err_s,
  APPROX_QUANTILES(ABS(err_s), 100)[OFFSET(50)] p50_abs_err_s,
  APPROX_QUANTILES(ABS(err_s), 100)[OFFSET(90)] p90_abs_err_s,
  ROUND(COUNTIF(ABS(err_s) <= 30)/COUNT(*),3) f_within_30s,
  ROUND(COUNTIF(ABS(err_s) <= 60)/COUNT(*),3) f_within_60s,
  APPROX_QUANTILES(ABS(aimed_err_s), 100)[OFFSET(50)] p50_abs_err_timetable_s,
  ROUND(COUNTIF(ABS(aimed_err_s) <= 60)/COUNT(*),3) f_timetable_within_60s
FROM j WHERE lead_s > 0
GROUP BY 1 ORDER BY 1
