-- Extend the /demo dataset's NSB rows with RECENT days (everything after the static holdout
-- build, which ends 2026-06-20 — see create_demo_predictions.sql), so partner demos can show
-- yesterday's — and today's completed — journeys next to what the models predicted.
--
-- This is the MANUAL, full-fidelity NSB layer (anshar recordedCalls extraction, same-day
-- capable, ~$2.5/run). The unattended freshness path is extend_demo_recent_daily.sql
-- (all operators from last_recorded, nightly scheduled query, ~10-17 % thinner for rail):
-- its next run replaces the NSB slice this script writes, so run this right before demos.
--
-- Self-contained and safe to re-run (idempotent per run window): typically run it right
-- before a demo, via data/queries/refresh_demo_recent.sh.
--
--   1. Re-extracts the last ~25 days of realized calls from ET with the REBUILT rail_calls
--      logic (create_rail_calls.sql: recordedCalls + cancelled estimatedCalls, arrivals,
--      terminal stops, Oslo call_date, Quay -> StopPlace). NOT rail_calls_new — that table
--      is the old departure-only shape and lacks the arrival side the demo needs.
--   2. Rebuilds the situations straight from SIRI-SX first versions (the
--      disruptions_today.sql pattern over a date range): onset = creationTime, first-version
--      text, affected journeys — verbatim parity with create_training_calls.sql.
--   3. Recomputes the model features re-anchored at the three ask times (leadtime_eval.sql
--      recipe: lead 30 = the training anchoring, lead 60, and -1 = at message arrival with
--      sentinel state), scores all nine heads, applies the per-ask-horizon platt layers and
--      survival-curve intervals (create_demo_predictions.sql math), and
--   4. DELETEs + re-INSERTs every demo row after 2026-06-20 it can rebuild, then refreshes
--      demo_stops and demo_highlights over the full table.
--
-- ROLLING ACCUMULATION: each run covers onsets in [GREATEST(2026-06-20, today-25d), now].
-- Runs less than ~3 weeks apart therefore keep the window gap-free from 2026-04-01 to today;
-- if more time passes between runs, days older than 25 days stay as previously built (never
-- deleted), and days never built are simply absent from the search.
--
-- "TODAY" CAVEAT: calls appear once ET has recorded them (or declared them cancelled), so
-- today's coverage is journeys completed/underway so far; a journey shows up in the search
-- once its arrival call at the chosen to-stop is realized. The newest SX text may still be
-- 'UNKNOWN' category until the serving pipeline's AI.CLASSIFY tick has seen it.
--
-- Same out-of-sample guarantee as the holdout build: models trained on onsets < 2026-04-01,
-- platt layers fit on the Apr–Jun holdout — recent data is unseen by both.
--
-- Cost: ~26 ET partitions (~150 GB, ~$0.9) + a small SX scan + ML.PREDICT over the recent
-- calls x 3 leads. Run as one bq job (file + --nosynchronous_mode + bq wait).

CREATE TEMP FUNCTION platt(p FLOAT64, a FLOAT64, b FLOAT64) AS (
  1 / (1 + EXP(-(b + a * LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6)
                            / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))))))
);

CREATE TEMP FUNCTION stack_q(s5 FLOAT64, s10 FLOAT64, s20 FLOAT64, s40 FLOAT64, q FLOAT64) AS (
  CAST(ROUND(CASE
    WHEN s5  <= q THEN (1 - q) / GREATEST(1 - s5, 0.001) * 5
    WHEN s10 <= q THEN 5  + (s5  - q) / GREATEST(s5  - s10, 0.001) * 5
    WHEN s20 <= q THEN 10 + (s10 - q) / GREATEST(s10 - s20, 0.001) * 10
    WHEN s40 <= q THEN 20 + (s20 - q) / GREATEST(s20 - s40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s40 / q), 90) END) AS INT64)
);

-- 1 ---- recent realized calls, rebuilt-shape (create_rail_calls.sql logic, ~26-day window).
-- Partition bounds are CURRENT_DATE() expressions (not script variables) so BigQuery prunes.
CREATE TEMP TABLE rc AS
WITH last_snap AS (
  SELECT
    REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
    lineRef,
    cancellation AS journey_cancelled,
    recordedCalls,
    estimatedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 26 DAY) AND CURRENT_DATE()
    AND lineRef LIKE 'VYG:%'
    AND datedVehicleJourneyRef IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY datedVehicleJourneyRef
    ORDER BY SAFE.TIMESTAMP(recordedAtTime) DESC
  ) = 1
),
calls AS (
  SELECT
    s.jkey, s.lineRef,
    rc.stopPointRef AS quay,
    rc.order        AS stop_order,
    'recorded'      AS call_source,
    (rc.cancellation OR s.journey_cancelled) AS call_cancelled,
    SAFE.TIMESTAMP(rc.aimedDepartureTime)  AS aimed_dep,
    SAFE.TIMESTAMP(rc.aimedArrivalTime)    AS aimed_arr,
    SAFE.TIMESTAMP(rc.actualDepartureTime) AS actual_dep,
    SAFE.TIMESTAMP(rc.actualArrivalTime)   AS actual_arr
  FROM last_snap AS s, UNNEST(s.recordedCalls) AS rc
  UNION ALL
  -- cancelled stops that only ever appeared as estimated (cancellations live mostly here)
  SELECT
    s.jkey, s.lineRef,
    ec.stopPointRef AS quay,
    ec.order        AS stop_order,
    'estimated'     AS call_source,
    TRUE            AS call_cancelled,
    SAFE.TIMESTAMP(ec.aimedDepartureTime)  AS aimed_dep,
    SAFE.TIMESTAMP(ec.aimedArrivalTime)    AS aimed_arr,
    CAST(NULL AS TIMESTAMP)                AS actual_dep,
    CAST(NULL AS TIMESTAMP)                AS actual_arr
  FROM last_snap AS s, UNNEST(s.estimatedCalls) AS ec
  WHERE (ec.cancellation OR s.journey_cancelled)
),
typed AS (
  SELECT
    jkey, lineRef, quay, stop_order, call_source, call_cancelled,
    aimed_dep, aimed_arr,
    DATE(COALESCE(aimed_dep, aimed_arr), 'Europe/Oslo')  AS call_date,
    TIMESTAMP_DIFF(actual_dep, aimed_dep, MINUTE)        AS dep_delay_min,
    TIMESTAMP_DIFF(actual_arr, aimed_arr, MINUTE)        AS arr_delay_min
  FROM calls
  WHERE COALESCE(aimed_dep, aimed_arr) IS NOT NULL
    AND (call_cancelled OR actual_dep IS NOT NULL OR actual_arr IS NOT NULL)
)
SELECT
  t.jkey, t.lineRef,
  q.stopPlaceRef AS stop_place,
  n.name         AS stop_name,
  t.stop_order, t.call_date, t.aimed_dep, t.aimed_arr,
  t.call_cancelled, t.dep_delay_min, t.arr_delay_min,
  COALESCE(t.aimed_dep, t.aimed_arr) AS call_ts
FROM typed AS t
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
  ON t.quay = q.id
LEFT JOIN (
  SELECT id, ANY_VALUE(name) AS name
  FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version`
  GROUP BY id
) AS n ON n.id = q.stopPlaceRef
WHERE t.call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 26 DAY)
-- a whole-journey cancellation can put the same stop in recordedCalls AND estimatedCalls;
-- keep one row per call, preferring the recorded one
QUALIFY ROW_NUMBER() OVER (PARTITION BY t.jkey, t.stop_order ORDER BY t.call_source DESC) = 1;

-- 2 ---- recent situations from SIRI-SX first versions (disruptions_today.sql pattern over a
-- date range; onset = creationTime, VERBATIM parity with create_training_calls.sql).
CREATE TEMP TABLE sit AS
WITH all_versions AS (
  SELECT
    situationNumber, version, creationTime,
    COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) AS event_ts,
    severity, planned, summaries, descriptions, affects
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 26 DAY) AND CURRENT_DATE()
    AND participantRef = 'NSB'
),
first_version AS (
  SELECT *
  FROM all_versions
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY situationNumber
    ORDER BY event_ts ASC, version ASC, creationTime ASC
  ) = 1
),
extracted AS (
  SELECT
    fv.situationNumber,
    SAFE.TIMESTAMP(fv.creationTime) AS onset_ts,
    IFNULL(fv.planned, FALSE)       AS planned,
    (SELECT x.value FROM UNNEST(fv.summaries) AS x
     WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
     LIMIT 1) AS summary,
    (SELECT x.value FROM UNNEST(fv.descriptions) AS x
     WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
     LIMIT 1) AS description,
    (
      SELECT ARRAY_AGG(stop_ref ORDER BY stop_ref)
      FROM (
        SELECT stops.stopPointRef AS stop_ref
        FROM UNNEST(fv.affects.stopPoints) AS stops WHERE stops.stopPointRef IS NOT NULL
        UNION DISTINCT
        SELECT sp.stopPlaceRef
        FROM UNNEST(fv.affects.stopPlaces) AS sp WHERE sp.stopPlaceRef IS NOT NULL
        UNION DISTINCT
        SELECT sp.stopPointRef
        FROM UNNEST(fv.affects.networks) AS nw, UNNEST(nw.affectedLines) AS al,
             UNNEST(al.routes) AS r, UNNEST(r.stopPoints.stopPoints) AS sp
        WHERE sp.stopPointRef IS NOT NULL
        UNION DISTINCT
        SELECT sp.stopPointRef
        FROM UNNEST(fv.affects.vehicleJourneys) AS vj, UNNEST(vj.routes) AS r,
             UNNEST(r.stopPoints.stopPoints) AS sp
        WHERE sp.stopPointRef IS NOT NULL
      )
    ) AS affected_stops,
    (
      SELECT ARRAY_AGG(journey_ref ORDER BY journey_ref)
      FROM (
        SELECT journeys.framedVehicleJourneyRef.datedVehicleJourneyRef AS journey_ref
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys
        WHERE journeys.framedVehicleJourneyRef.datedVehicleJourneyRef IS NOT NULL
        UNION DISTINCT
        SELECT dvjr
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys,
             UNNEST(journeys.datedVehicleJourneyRefs) AS dvjr
        WHERE dvjr IS NOT NULL
        UNION DISTINCT
        SELECT vjr
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys,
             UNNEST(journeys.vehicleJourneyRefs) AS vjr
        WHERE vjr IS NOT NULL
      )
    ) AS affected_journeys,
    (
      SELECT ARRAY_AGG(line_ref ORDER BY line_ref)
      FROM (
        SELECT journeys.lineRef AS line_ref
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys WHERE journeys.lineRef IS NOT NULL
        UNION DISTINCT
        SELECT al.lineRef
        FROM UNNEST(fv.affects.networks) AS nw, UNNEST(nw.affectedLines) AS al
        WHERE al.lineRef IS NOT NULL
      )
    ) AS affected_lines
  FROM first_version AS fv
  WHERE (fv.severity IS NULL OR fv.severity != 'NO_IMPACT')
),
cat AS (
  SELECT summary, description, ANY_VALUE(category) AS category
  FROM `ent-kixavvik-dev.disruptions.categories`
  GROUP BY summary, description
)
SELECT
  e.situationNumber,
  e.onset_ts,
  e.summary,
  -- status regex on the onset text: same expression as predict_calls.sql / create_disruptions.sql
  CASE
    WHEN REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(e.summary,''),' ',IFNULL(e.description,''))),
                         r'innstil|kansel')                        THEN 'innstilt'
    WHEN REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(e.summary,''),' ',IFNULL(e.description,''))),
                         r'forsink|toget står')                    THEN 'forsinket'
    WHEN REGEXP_CONTAINS(LOWER(IFNULL(e.summary,'')),
                         r'færre vogner|forreste vogn|fremste vogn|bruk de') THEN 'kapasitet'
    ELSE 'other'
  END AS status,
  e.planned,
  ARRAY_LENGTH(e.affected_journeys) AS num_journeys,
  ARRAY_LENGTH(e.affected_stops)    AS num_stops,
  ARRAY_LENGTH(e.affected_lines)    AS num_lines,
  e.affected_journeys,
  IFNULL(c.category, 'UNKNOWN')     AS category,
  IFNULL(SAFE_CAST(REGEXP_EXTRACT(
    LOWER(CONCAT(IFNULL(e.summary,''), ' ', IFNULL(e.description,''))),
    r'([0-9]+)\s*min') AS INT64), -1) AS stated_min
FROM extracted AS e
LEFT JOIN cat AS c
  ON IFNULL(e.summary,'')     = IFNULL(c.summary,'')
 AND IFNULL(e.description,'') = IFNULL(c.description,'')
WHERE e.onset_ts IS NOT NULL
  -- only onsets the holdout build does not already cover, within this run's rebuild window
  AND DATE(e.onset_ts) >= GREATEST(DATE '2026-06-20', DATE_SUB(CURRENT_DATE(), INTERVAL 25 DAY))
  AND ARRAY_LENGTH(e.affected_journeys) > 0;

-- 3 ---- model features per (situation x affected call), re-anchored at the three ask times.
-- Feature block mirrors create_training_calls.sql; the lead re-anchoring mirrors
-- leadtime_eval.sql (lead 30 = the training anchoring; -1 = at message arrival, sentinel state).
CREATE TEMP TABLE feat AS
WITH aff AS (
  SELECT s.*, REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
),
base AS (
  SELECT
    a.situationNumber, a.onset_ts, a.summary, a.category, a.stated_min,
    a.status, a.planned, a.num_journeys, a.num_stops, a.num_lines,
    EXTRACT(HOUR      FROM a.onset_ts) AS onset_hour,
    EXTRACT(DAYOFWEEK FROM a.onset_ts) AS onset_dow,
    EXTRACT(MONTH     FROM a.onset_ts) AS onset_month,
    a.jkey, r.lineRef, r.stop_place, r.stop_name, r.stop_order,
    r.call_date, r.aimed_dep, r.aimed_arr, r.call_ts,
    (r.aimed_dep IS NULL) AS is_terminal,
    (r.aimed_arr IS NULL) AS is_origin,
    TIMESTAMP_DIFF(r.call_ts, a.onset_ts, MINUTE) AS min_from_onset,
    EXTRACT(HOUR      FROM r.call_ts) AS call_hour,
    EXTRACT(DAYOFWEEK FROM r.call_ts) AS call_dow,
    COALESCE(r.call_cancelled, FALSE) AS act_cancelled,
    r.dep_delay_min AS act_dep_delay_min,
    r.arr_delay_min AS act_arr_delay_min
  FROM aff AS a
  JOIN rc AS r USING (jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
    AND TIMESTAMP_DIFF(r.call_ts, a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
sitline AS (
  SELECT DISTINCT situationNumber, lineRef, onset_ts FROM base
),
linepre AS (
  SELECT p.situationNumber, p.lineRef,
         COUNT(*) AS line_pre_n,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS line_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS line_pre_cancel
  FROM sitline AS p
  JOIN rc AS r
    ON r.lineRef = p.lineRef
   AND r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1, 2
),
netpre AS (
  SELECT p.situationNumber,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS net_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS net_pre_cancel
  FROM (SELECT DISTINCT situationNumber, onset_ts FROM sitline) AS p
  JOIN rc AS r
    ON r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1
),
linehour AS (
  SELECT lineRef, TIMESTAMP_TRUNC(aimed_dep, HOUR) AS h,
         COUNT(*) AS n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS d,
         ROUND(AVG(CAST(COALESCE(call_cancelled, FALSE) AS INT64)), 3) AS c
  FROM rc
  WHERE aimed_dep IS NOT NULL
  GROUP BY 1, 2
),
expanded AS (
  SELECT b.*, lead_min
  FROM base AS b CROSS JOIN UNNEST([-1, 30, 60]) AS lead_min
),
own AS (
  SELECT e.situationNumber, e.jkey, e.stop_order, e.lead_min,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev
  FROM expanded AS e
  JOIN rc AS r2
    ON r2.jkey = e.jkey
   AND r2.stop_order < e.stop_order
   AND COALESCE(r2.aimed_dep, r2.aimed_arr) <= TIMESTAMP_SUB(e.call_ts, INTERVAL e.lead_min MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  WHERE e.lead_min != -1
  GROUP BY 1, 2, 3, 4
)
SELECT
  e.*,
  -- ask-time line/train state: sentinels for the at-message (-1) variant, re-anchored
  -- observations for leads 30/60 (leadtime_eval.sql conventions)
  IF(e.lead_min = -1, 0,  IFNULL(lh.n, 0))       AS line_now_n,
  IF(e.lead_min = -1, -1, IFNULL(lh.d, -1))      AS line_now_delay,
  IF(e.lead_min = -1, -1, IFNULL(lh.c, -1))      AS line_now_cancel,
  IF(e.lead_min = -1, -1, IFNULL(o.own_prev, -1)) AS own_prev_delay,
  IFNULL(lp.line_pre_n, 0)       AS line_pre_n,
  IFNULL(lp.line_pre_delay, -1)  AS line_pre_delay,
  IFNULL(lp.line_pre_cancel, -1) AS line_pre_cancel,
  IFNULL(np.net_pre_delay, -1)   AS net_pre_delay,
  IFNULL(np.net_pre_cancel, -1)  AS net_pre_cancel
FROM expanded AS e
LEFT JOIN linepre AS lp ON lp.situationNumber = e.situationNumber AND lp.lineRef = e.lineRef
LEFT JOIN netpre  AS np ON np.situationNumber = e.situationNumber
LEFT JOIN linehour AS lh
  ON e.lead_min != -1
 AND lh.lineRef = e.lineRef
 AND lh.h = TIMESTAMP_TRUNC(TIMESTAMP_SUB(e.call_ts, INTERVAL (e.lead_min + 30) MINUTE), HOUR)
LEFT JOIN own AS o
  ON o.situationNumber = e.situationNumber
 AND o.jkey = e.jkey
 AND o.stop_order = e.stop_order
 AND o.lead_min = e.lead_min;

-- 4 ---- score the nine heads (raw), leadtime_scored-shape.
CREATE TEMP TABLE scored_raw AS
WITH clf AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_cancel_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`, (SELECT * FROM feat))
),
a5 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_clf`, (SELECT * FROM feat))
),
a10 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf`, (SELECT * FROM feat))
),
a20 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_clf`, (SELECT * FROM feat))
),
a40 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_clf`, (SELECT * FROM feat))
),
d5 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_clf`, (SELECT * FROM feat))
),
d10 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM feat))
),
d20 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_clf`, (SELECT * FROM feat))
),
d40 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_clf`, (SELECT * FROM feat))
)
SELECT
  f.situationNumber, f.jkey, f.stop_order, f.lead_min, f.call_date, f.min_from_onset,
  f.is_origin, f.is_terminal,
  f.lineRef, f.stop_place, f.stop_name, f.aimed_dep, f.aimed_arr,
  f.onset_ts, f.summary, f.category,
  f.act_cancelled, f.act_dep_delay_min, f.act_arr_delay_min,
  clf.p_cancel_raw,
  a5.p_raw  AS p_arr5_raw,  a10.p_raw AS p_arr10_raw,
  a20.p_raw AS p_arr20_raw, a40.p_raw AS p_arr40_raw,
  d5.p_raw  AS p_dep5_raw,  d10.p_raw AS p_dep10_raw,
  d20.p_raw AS p_dep20_raw, d40.p_raw AS p_dep40_raw
FROM feat AS f
JOIN clf USING (situationNumber, jkey, stop_order, lead_min)
JOIN a5  USING (situationNumber, jkey, stop_order, lead_min)
JOIN a10 USING (situationNumber, jkey, stop_order, lead_min)
JOIN a20 USING (situationNumber, jkey, stop_order, lead_min)
JOIN a40 USING (situationNumber, jkey, stop_order, lead_min)
JOIN d5  USING (situationNumber, jkey, stop_order, lead_min)
JOIN d10 USING (situationNumber, jkey, stop_order, lead_min)
JOIN d20 USING (situationNumber, jkey, stop_order, lead_min)
JOIN d40 USING (situationNumber, jkey, stop_order, lead_min);

-- 5 ---- calibrate per ask horizon, pivot the three ask times wide (create_demo_predictions.sql
-- math, verbatim), into the demo_predictions row shape.
CREATE TEMP TABLE recent_predictions AS
WITH sc AS (
  SELECT *, CASE lead_min WHEN 30 THEN '30' WHEN 60 THEN '60' ELSE 'plan' END AS lead_bucket
  FROM scored_raw
),
wch AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt_plan`)
),
w5h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt_plan`)
),
w10h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_plan`)
),
w20h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt_plan`)
),
w40h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt_plan`)
),
wd5h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt_plan`)
),
wd10h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_plan`)
),
wd20h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt_plan`)
),
wd40h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt_plan`)
),
scored AS (
  SELECT
    sc.*,
    platt(sc.p_cancel_raw, wch.a, wch.b)     AS p_cancel,
    platt(sc.p_dep10_raw, wd10h.a, wd10h.b)  AS p_dep_ge10,
    platt(sc.p_arr10_raw, w10h.a, w10h.b)    AS p_arr_ge10,
    GREATEST(platt(sc.p_arr5_raw,  w5h.a,  w5h.b),
             platt(sc.p_arr10_raw, w10h.a, w10h.b),
             platt(sc.p_arr20_raw, w20h.a, w20h.b),
             platt(sc.p_arr40_raw, w40h.a, w40h.b)) AS s5,
    GREATEST(platt(sc.p_arr10_raw, w10h.a, w10h.b),
             platt(sc.p_arr20_raw, w20h.a, w20h.b),
             platt(sc.p_arr40_raw, w40h.a, w40h.b)) AS s10,
    GREATEST(platt(sc.p_arr20_raw, w20h.a, w20h.b),
             platt(sc.p_arr40_raw, w40h.a, w40h.b)) AS s20,
    platt(sc.p_arr40_raw, w40h.a, w40h.b) AS s40,
    GREATEST(platt(sc.p_dep5_raw,  wd5h.a,  wd5h.b),
             platt(sc.p_dep10_raw, wd10h.a, wd10h.b),
             platt(sc.p_dep20_raw, wd20h.a, wd20h.b),
             platt(sc.p_dep40_raw, wd40h.a, wd40h.b)) AS d5,
    GREATEST(platt(sc.p_dep10_raw, wd10h.a, wd10h.b),
             platt(sc.p_dep20_raw, wd20h.a, wd20h.b),
             platt(sc.p_dep40_raw, wd40h.a, wd40h.b)) AS d10,
    GREATEST(platt(sc.p_dep20_raw, wd20h.a, wd20h.b),
             platt(sc.p_dep40_raw, wd40h.a, wd40h.b)) AS d20,
    platt(sc.p_dep40_raw, wd40h.a, wd40h.b) AS d40
  FROM sc
  JOIN wch   USING (lead_bucket)
  JOIN w5h   USING (lead_bucket)
  JOIN w10h  USING (lead_bucket)
  JOIN w20h  USING (lead_bucket)
  JOIN w40h  USING (lead_bucket)
  JOIN wd5h  USING (lead_bucket)
  JOIN wd10h USING (lead_bucket)
  JOIN wd20h USING (lead_bucket)
  JOIN wd40h USING (lead_bucket)
),
per_lead AS (
  SELECT
    situationNumber, jkey, stop_order, lead_min, call_date, min_from_onset,
    lineRef, stop_place, stop_name, aimed_dep, aimed_arr, onset_ts, summary, category,
    act_cancelled, act_dep_delay_min, act_arr_delay_min,
    ROUND(p_cancel, 4) AS p_cancel,
    IF(is_terminal, NULL, ROUND(p_dep_ge10, 4)) AS p_dep_ge10,
    IF(is_origin,   NULL, ROUND(p_arr_ge10, 4)) AS p_arr_ge10,
    IF(is_terminal, NULL, ROUND(p_cancel + (1 - p_cancel) * p_dep_ge10, 4)) AS p_impact_dep,
    IF(is_origin,   NULL, ROUND(p_cancel + (1 - p_cancel) * p_arr_ge10, 4)) AS p_impact_arr,
    IF(is_terminal, NULL, stack_q(d5, d10, d20, d40, 0.5)) AS dep_p50,
    IF(is_terminal, NULL, stack_q(d5, d10, d20, d40, 0.1)) AS dep_p90,
    IF(is_origin,   NULL, stack_q(s5, s10, s20, s40, 0.5)) AS arr_p50,
    IF(is_origin,   NULL, stack_q(s5, s10, s20, s40, 0.1)) AS arr_p90
  FROM scored
)
SELECT
  situationNumber, jkey, stop_order,
  ANY_VALUE(call_date)      AS call_date,
  ANY_VALUE(min_from_onset) AS min_from_onset,
  ANY_VALUE(lineRef)    AS lineRef,
  ANY_VALUE(stop_place) AS stop_place,
  ANY_VALUE(stop_name)  AS stop_name,
  ANY_VALUE(aimed_dep)  AS aimed_dep,
  ANY_VALUE(aimed_arr)  AS aimed_arr,
  ANY_VALUE(onset_ts)   AS onset_ts,
  ANY_VALUE(summary)    AS summary,
  ANY_VALUE(category)   AS category,
  ANY_VALUE(act_cancelled)     AS act_cancelled,
  ANY_VALUE(act_dep_delay_min) AS act_dep_delay_min,
  ANY_VALUE(act_arr_delay_min) AS act_arr_delay_min,
  MAX(IF(lead_min = -1, p_cancel, NULL))      AS onset_p_cancel,
  MAX(IF(lead_min = -1, p_dep_ge10, NULL))    AS onset_p_dep_ge10,
  MAX(IF(lead_min = -1, p_arr_ge10, NULL))    AS onset_p_arr_ge10,
  MAX(IF(lead_min = -1, p_impact_dep, NULL))  AS onset_p_impact_dep,
  MAX(IF(lead_min = -1, p_impact_arr, NULL))  AS onset_p_impact_arr,
  MAX(IF(lead_min = -1, dep_p50, NULL))       AS onset_dep_p50,
  MAX(IF(lead_min = -1, dep_p90, NULL))       AS onset_dep_p90,
  MAX(IF(lead_min = -1, arr_p50, NULL))       AS onset_arr_p50,
  MAX(IF(lead_min = -1, arr_p90, NULL))       AS onset_arr_p90,
  MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_cancel, NULL))     AS l60_p_cancel,
  MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_dep_ge10, NULL))   AS l60_p_dep_ge10,
  MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_arr_ge10, NULL))   AS l60_p_arr_ge10,
  MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_impact_dep, NULL)) AS l60_p_impact_dep,
  MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_impact_arr, NULL)) AS l60_p_impact_arr,
  MAX(IF(lead_min = 60 AND min_from_onset >= 60, dep_p50, NULL))      AS l60_dep_p50,
  MAX(IF(lead_min = 60 AND min_from_onset >= 60, dep_p90, NULL))      AS l60_dep_p90,
  MAX(IF(lead_min = 60 AND min_from_onset >= 60, arr_p50, NULL))      AS l60_arr_p50,
  MAX(IF(lead_min = 60 AND min_from_onset >= 60, arr_p90, NULL))      AS l60_arr_p90,
  MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_cancel, NULL))     AS l30_p_cancel,
  MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_dep_ge10, NULL))   AS l30_p_dep_ge10,
  MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_arr_ge10, NULL))   AS l30_p_arr_ge10,
  MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_impact_dep, NULL)) AS l30_p_impact_dep,
  MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_impact_arr, NULL)) AS l30_p_impact_arr,
  MAX(IF(lead_min = 30 AND min_from_onset >= 30, dep_p50, NULL))      AS l30_dep_p50,
  MAX(IF(lead_min = 30 AND min_from_onset >= 30, dep_p90, NULL))      AS l30_dep_p90,
  MAX(IF(lead_min = 30 AND min_from_onset >= 30, arr_p50, NULL))      AS l30_arr_p50,
  MAX(IF(lead_min = 30 AND min_from_onset >= 30, arr_p90, NULL))      AS l30_arr_p90
FROM per_lead
GROUP BY situationNumber, jkey, stop_order;

-- 6 ---- merge into the demo tables: replace everything this run can rebuild. NSB-scoped:
-- the demo tables also hold GOA/bus rows from the pooled family (see the multi-operator
-- create_demo_predictions.sql), which this NSB-only refresh must leave untouched.
DELETE FROM `ent-kixavvik-dev.disruptions.demo_predictions`
WHERE operator = 'NSB'
  AND onset_ts >= TIMESTAMP(GREATEST(DATE '2026-06-20', DATE_SUB(CURRENT_DATE(), INTERVAL 25 DAY)));

INSERT INTO `ent-kixavvik-dev.disruptions.demo_predictions`
  (operator, situationNumber, jkey, stop_order, call_date, min_from_onset,
   lineRef, stop_place, stop_name, aimed_dep, aimed_arr,
   onset_ts, summary, category,
   act_cancelled, act_dep_delay_min, act_arr_delay_min,
   onset_p_cancel, onset_p_dep_ge10, onset_p_arr_ge10, onset_p_impact_dep, onset_p_impact_arr,
   onset_dep_p50, onset_dep_p90, onset_arr_p50, onset_arr_p90,
   l60_p_cancel, l60_p_dep_ge10, l60_p_arr_ge10, l60_p_impact_dep, l60_p_impact_arr,
   l60_dep_p50, l60_dep_p90, l60_arr_p50, l60_arr_p90,
   l30_p_cancel, l30_p_dep_ge10, l30_p_arr_ge10, l30_p_impact_dep, l30_p_impact_arr,
   l30_dep_p50, l30_dep_p90, l30_arr_p50, l30_arr_p90)
SELECT
  'NSB', situationNumber, jkey, stop_order, call_date, min_from_onset,
  lineRef, stop_place, stop_name, aimed_dep, aimed_arr,
  onset_ts, summary, category,
  act_cancelled, act_dep_delay_min, act_arr_delay_min,
  onset_p_cancel, onset_p_dep_ge10, onset_p_arr_ge10, onset_p_impact_dep, onset_p_impact_arr,
  onset_dep_p50, onset_dep_p90, onset_arr_p50, onset_arr_p90,
  l60_p_cancel, l60_p_dep_ge10, l60_p_arr_ge10, l60_p_impact_dep, l60_p_impact_arr,
  l60_dep_p50, l60_dep_p90, l60_arr_p50, l60_arr_p90,
  l30_p_cancel, l30_p_dep_ge10, l30_p_arr_ge10, l30_p_impact_dep, l30_p_impact_arr,
  l30_dep_p50, l30_dep_p90, l30_arr_p50, l30_arr_p90
FROM recent_predictions;

DELETE FROM `ent-kixavvik-dev.disruptions.demo_calls`
WHERE operator = 'NSB'
  AND call_date > DATE '2026-06-20'
  AND call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 25 DAY);

INSERT INTO `ent-kixavvik-dev.disruptions.demo_calls`
  (operator, operating_date, jkey, lineRef, stop_place, stop_name, stop_order, call_date,
   aimed_dep, aimed_arr, call_cancelled, dep_delay_min, arr_delay_min)
SELECT
  'NSB', CAST(NULL AS DATE), jkey, lineRef, stop_place, stop_name, stop_order, call_date,
  aimed_dep, aimed_arr, COALESCE(call_cancelled, FALSE), dep_delay_min, arr_delay_min
FROM rc
WHERE call_date > DATE '2026-06-20'
  AND call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 25 DAY);

-- 7 ---- refresh the derived picker/scenario tables over the full window
-- (same definitions as the multi-operator create_demo_predictions.sql).
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.demo_stops` AS
SELECT stop_place,
       ANY_VALUE(stop_name) AS stop_name,
       COUNT(*) AS n_calls,
       CASE
         WHEN LOGICAL_OR(operator IN ('NSB', 'GOA')) AND LOGICAL_OR(operator NOT IN ('NSB', 'GOA'))
           THEN 'Tog/Buss'
         WHEN LOGICAL_OR(operator IN ('NSB', 'GOA')) THEN 'Tog'
         ELSE 'Buss'
       END AS mode
FROM `ent-kixavvik-dev.disruptions.demo_calls`
WHERE stop_place IS NOT NULL
GROUP BY stop_place
HAVING COUNT(*) >= 200;

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.demo_highlights` AS
WITH impact AS (
  SELECT situationNumber, operator, jkey,
         ANY_VALUE(summary)  AS summary,
         ANY_VALUE(category) AS category,
         ANY_VALUE(onset_ts) AS onset_ts,
         COUNTIF(act_cancelled) AS n_cancelled,
         COUNTIF(NOT act_cancelled AND act_dep_delay_min >= 10) AS n_delayed10,
         COUNT(*) AS n_calls,
         MIN(aimed_dep) AS first_dep,
         MAX(aimed_dep) AS last_dep
  FROM `ent-kixavvik-dev.disruptions.demo_predictions`
  GROUP BY situationNumber, operator, jkey
),
per_sit AS (
  SELECT situationNumber,
         ANY_VALUE(summary)  AS summary,
         ANY_VALUE(category) AS category,
         ANY_VALUE(onset_ts) AS onset_ts,
         SUM(n_cancelled)  AS n_cancelled,
         SUM(n_delayed10)  AS n_delayed10,
         SUM(n_calls)      AS n_calls,
         MIN(first_dep)    AS window_start,
         MAX(last_dep)     AS window_end,
         ARRAY_AGG(STRUCT(operator, jkey)
                   ORDER BY n_cancelled + n_delayed10 DESC, n_calls DESC LIMIT 1)[OFFSET(0)] AS top
  FROM impact
  GROUP BY situationNumber
),
exemplar AS (
  SELECT s.situationNumber,
         ARRAY_AGG(d.stop_place ORDER BY d.stop_order ASC  LIMIT 1)[OFFSET(0)] AS from_stop,
         ARRAY_AGG(d.stop_name  ORDER BY d.stop_order ASC  LIMIT 1)[OFFSET(0)] AS from_name,
         ARRAY_AGG(d.stop_place ORDER BY d.stop_order DESC LIMIT 1)[OFFSET(0)] AS to_stop,
         ARRAY_AGG(d.stop_name  ORDER BY d.stop_order DESC LIMIT 1)[OFFSET(0)] AS to_name
  FROM per_sit AS s
  JOIN `ent-kixavvik-dev.disruptions.demo_predictions` AS d
    ON d.situationNumber = s.situationNumber
   AND d.operator = s.top.operator AND d.jkey = s.top.jkey
  GROUP BY s.situationNumber
)
SELECT p.situationNumber, p.top.operator AS operator,
       p.summary, p.category, p.onset_ts,
       p.n_cancelled, p.n_delayed10, p.n_calls,
       p.window_start, p.window_end,
       e.from_stop, e.from_name, e.to_stop, e.to_name
FROM per_sit AS p
JOIN exemplar AS e USING (situationNumber)
WHERE p.n_cancelled + p.n_delayed10 >= 5
QUALIFY ROW_NUMBER() OVER (PARTITION BY p.top.operator
                           ORDER BY p.n_cancelled + p.n_delayed10 DESC) <= 10
ORDER BY p.n_cancelled + p.n_delayed10 DESC
LIMIT 40;
