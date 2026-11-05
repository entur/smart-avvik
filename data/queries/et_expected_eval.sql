-- Our arrival heads vs SIRI-ET's own expected arrival time, both asked at the same moment.
--
-- For every holdout call x lead L in {30, 60, 120, 240} the question is put at
-- T = call_ts - L (call_ts = COALESCE(aimed_dep, aimed_arr), as in leadtime_eval.sql), and each
-- side answers with what it could have known at T:
--   ours -- the arr_ge{5,10,20,40} heads re-scored on ask-time features that only count what had
--           HAPPENED by T (statement 2, see below);
--   ET   -- expectedArrivalTime at the call in the journey's latest anshar snapshot recorded at or
--           before T (statement 3). Never the last snapshot: that one has seen the outcome.
-- et_expected_eval.py reads the three tables, calibrates our side with the serving's per-horizon
-- Platt layers and prints the comparison.
--
-- WHY OUR SIDE IS RE-SCORED. leadtime_eval_calls anchors its two ask-time features on AIMED times:
-- own_prev_delay takes the latest earlier stop AIMED before T, and line_now_* the hour bucket of
-- call_ts - (L+30), which reaches up to 30 min past T -- on about half the rows, since the bucket
-- end lands uniformly in (T-30, T+30]. Its header states this as a caveat. Against ET, which
-- really is read at T, it is no longer a caveat but the comparison itself: a late train is exactly
-- the case where a stop aimed before T has not been passed yet. Statement 1 keeps both features'
-- definitions and adds one condition, actual_dep <= T (a cancelled call counts once it is aimed
-- before T). Measured 2026-10-23 it costs 0.03-0.04 AUC at 30-60 min (flagged, 30 min: 0.918 ->
-- 0.878), which is how optimistic the published lead-time numbers read. ET's own lag in recording
-- a passage is ignored, so the leak-free side is still a little generous.
--
-- ET SNAPSHOTS. Both arrays are flattened, not just estimatedCalls: a call already in
-- recordedCalls at T has arrived (the analysis drops it), and reading only estimatedCalls would
-- silently fall back to an OLDER snapshot's estimate for it. No snapshot at or before T means the
-- journey was not in ET yet -- the analysis treats that as the timetable, which is what a consumer
-- would have seen.
--
-- THE WINDOW is literal, not a parameter, so BigQuery prunes partitions. Change it in all four
-- places together: rail_calls (one day wider at the front, for the earlier stops of a journey that
-- started before midnight), leadtime_eval_calls, and the anshar partitions (one day wider at each
-- end: a 240-min ask on an early call reads the day before, and late journeys spill past midnight).
-- Two weeks cost ~$1.60, almost all of it the anshar scan (254 GB); statements 1-2 are ~3.7 GB.
-- Six months (Apr 1 - Oct 12 2026, 8.8 M asks) billed ~$17, again almost all anshar, in ~30 s.
-- Aug 31 - Sep 13 2026 was picked as a fortnight before Vy's reduced plan began (2026-09-19).
--
-- MEASURED 2026-10-23 on that window (743 524 asks), arrival delay >= 10 min, AUC ours / ET:
--   flagged  0.878 / 0.733 (30)  0.808 / 0.589 (60)  0.705 / 0.520 (120)  0.645 / 0.501 (240)
--   no SX    0.861 / 0.773       0.804 / 0.602       0.732 / 0.515        0.650 / 0.504
-- ET's expected delay is >= 1 min on 10 % (flagged) / 5 % (no SX) of asks at 30 min and ~0 from
-- 120 min, and it is optimistic (mean bias -3.0 / -1.5 min; a train already 5-9 min late at T is
-- expected 1.7 min late, arrives 6.0). Its "expected >= 10" rule is precise and rare: 95 % precision
-- at 22 % recall (flagged, 30 min), and on no-SX calls at 30 min it beats us at that operating point
-- (93.5 % vs our 59 % at the same recall). ET's expected times as features on top of our score, fit
-- one week and tested on the next: no SX 0.846 -> 0.876 and flagged 0.904 -> 0.913 at 30 min,
-- <= 0.01 at 60, nothing beyond.
--
-- Run (dev only; a script, so the three statements run in order):
--   bq query --use_legacy_sql=false < data/queries/et_expected_eval.sql
--   uv run data/queries/et_expected_eval.py

-- 1. One row per (holdout call x lead), with T and the leak-free ask-time features ---------------

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.exp_etcmp_asks` AS
WITH rc AS (
  SELECT jkey, stop_order, call_date, lineRef, aimed_dep, aimed_arr, actual_dep, actual_arr,
         dep_delay_min, arr_delay_min, call_cancelled,
         COALESCE(aimed_dep, aimed_arr) AS call_ts
  FROM `ent-smartavvik-dev.disruptions.rail_calls`
  WHERE call_date BETWEEN '2026-08-30' AND '2026-09-13'
),
asks AS (
  -- sid stands in for situationNumber as a key, as in create_leadtime_calibration.sql: an
  -- unflagged row's situationNumber is NULL and every USING join below would drop it.
  SELECT e.*, IFNULL(e.situationNumber, '') AS sid,
         r.call_ts, r.aimed_arr, r.actual_arr,
         TIMESTAMP_SUB(r.call_ts, INTERVAL e.lead_min MINUTE) AS ask_ts
  FROM `ent-smartavvik-dev.disruptions.leadtime_eval_calls` AS e
  JOIN rc AS r USING (jkey, stop_order, call_date)
  WHERE e.call_date BETWEEN '2026-08-31' AND '2026-09-13'
    AND e.lead_min IN (30, 60, 120, 240)
    AND NOT e.is_origin
),
own_fair AS (
  SELECT a.sid, a.jkey, a.stop_order, a.call_date, a.lead_min,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)] AS own_prev
  FROM asks AS a
  JOIN rc AS r2
    ON r2.jkey = a.jkey
   AND r2.stop_order < a.stop_order
   AND r2.actual_dep <= a.ask_ts
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  GROUP BY 1, 2, 3, 4, 5
),
line_fair AS (
  SELECT a.sid, a.jkey, a.stop_order, a.call_date, a.lead_min,
         COUNT(*) AS n,
         ROUND(AVG(GREATEST(r3.dep_delay_min, 0)), 2) AS d,
         ROUND(AVG(CAST(COALESCE(r3.call_cancelled, FALSE) AS INT64)), 3) AS c
  FROM asks AS a
  JOIN rc AS r3
    ON r3.lineRef = a.lineRef
   -- the hour bucket as an equality, not a range: BigQuery hashes on it, while a range leaves
   -- lineRef as the only join key and pairs every call on the line across the whole window
   AND TIMESTAMP_TRUNC(r3.aimed_dep, HOUR) = TIMESTAMP_TRUNC(TIMESTAMP_SUB(a.call_ts, INTERVAL (a.lead_min + 30) MINUTE), HOUR)
   AND (r3.actual_dep <= a.ask_ts OR (COALESCE(r3.call_cancelled, FALSE) AND r3.aimed_dep <= a.ask_ts))
  GROUP BY 1, 2, 3, 4, 5
)
SELECT a.* EXCEPT(line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
       a.own_prev_delay  AS own_prev_delay_built,
       a.line_now_delay  AS line_now_delay_built,
       IFNULL(lf.n, 0)        AS line_now_n,
       IFNULL(lf.d, -1)       AS line_now_delay,
       IFNULL(lf.c, -1)       AS line_now_cancel,
       IFNULL(o.own_prev, -1) AS own_prev_delay
FROM asks AS a
LEFT JOIN own_fair  AS o  USING (sid, jkey, stop_order, call_date, lead_min)
LEFT JOIN line_fair AS lf USING (sid, jkey, stop_order, call_date, lead_min);

-- 2. The four arrival heads on the leak-free features ------------------------------------------
-- ML.PREDICT ignores the columns a model does not know, so the *_built twins ride along harmlessly.

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.exp_etcmp_fair_scored` AS
WITH inp AS (SELECT * FROM `ent-smartavvik-dev.disruptions.exp_etcmp_asks`),
a5 AS (SELECT sid, jkey, stop_order, call_date, lead_min, (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
       FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge5_clf`, (SELECT * FROM inp))),
a10 AS (SELECT sid, jkey, stop_order, call_date, lead_min, (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
       FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge10_clf`, (SELECT * FROM inp))),
a20 AS (SELECT sid, jkey, stop_order, call_date, lead_min, (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
       FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge20_clf`, (SELECT * FROM inp))),
a40 AS (SELECT sid, jkey, stop_order, call_date, lead_min, (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
       FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge40_clf`, (SELECT * FROM inp)))
SELECT sid, jkey, stop_order, call_date, lead_min,
       a5.p AS f_arr5_raw, a10.p AS f_arr10_raw, a20.p AS f_arr20_raw, a40.p AS f_arr40_raw
FROM a5 JOIN a10 USING (sid, jkey, stop_order, call_date, lead_min)
        JOIN a20 USING (sid, jkey, stop_order, call_date, lead_min)
        JOIN a40 USING (sid, jkey, stop_order, call_date, lead_min);

-- 3. What ET said about each ask's call at T ----------------------------------------------------

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.exp_etcmp_et_asof` AS
WITH asks AS (
  SELECT sid, jkey, stop_order, call_date, lead_min, ask_ts
  FROM `ent-smartavvik-dev.disruptions.exp_etcmp_asks`
),
need AS (SELECT DISTINCT jkey, stop_order FROM asks),
snaps AS (
  SELECT REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
         SAFE.TIMESTAMP(recordedAtTime) AS rec,
         COALESCE(cancellation, FALSE)  AS j_cancel,
         ARRAY_LENGTH(recordedCalls)    AS n_rec,   -- > 0: the train had started by this snapshot
         estimatedCalls, recordedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  WHERE _PARTITIONTIME >= TIMESTAMP('2026-08-30') AND _PARTITIONTIME < TIMESTAMP('2026-09-15')
    AND lineRef LIKE 'VYG:%'
    AND datedVehicleJourneyRef IS NOT NULL
),
flat AS (
  SELECT s.jkey, s.rec, s.j_cancel, s.n_rec, ec.order AS stop_order, 'est' AS src,
         SAFE.TIMESTAMP(ec.expectedArrivalTime) AS exp_arr,
         COALESCE(ec.cancellation, FALSE) AS c_cancel
  FROM snaps AS s, UNNEST(s.estimatedCalls) AS ec
  UNION ALL
  SELECT s.jkey, s.rec, s.j_cancel, s.n_rec, rc.order AS stop_order, 'rec' AS src,
         SAFE.TIMESTAMP(COALESCE(rc.actualArrivalTime, rc.expectedArrivalTime)) AS exp_arr,
         COALESCE(rc.cancellation, FALSE) AS c_cancel
  FROM snaps AS s, UNNEST(s.recordedCalls) AS rc
),
flat_need AS (
  SELECT f.* FROM flat AS f JOIN need USING (jkey, stop_order)
)
SELECT a.sid, a.jkey, a.stop_order, a.call_date, a.lead_min,
       ARRAY_AGG(STRUCT(f.rec, f.src, f.exp_arr, f.c_cancel, f.j_cancel, f.n_rec)
                 ORDER BY f.rec DESC LIMIT 1)[OFFSET(0)] AS et,
       MIN(f.rec) AS first_rec
FROM asks AS a
JOIN flat_need AS f
  ON f.jkey = a.jkey AND f.stop_order = a.stop_order AND f.rec <= a.ask_ts
GROUP BY 1, 2, 3, 4, 5;
