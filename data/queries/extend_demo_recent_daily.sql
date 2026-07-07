-- Extend the /demo dataset with RECENT days for ALL operators (everything after the static
-- holdout build, which ends 2026-06-20 — see create_demo_predictions.sql). This is the DAILY
-- unattended refresh (bq scheduled query "demo-recent-daily", every day 03:30 UTC).
--
-- SOURCE: ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded — the
-- all-operator per-call "final state" table (daily-refreshed; yesterday is fully recorded,
-- today partially). A ~27-day slice is partition-pruned (operatingDate) + cluster-pruned
-- (dataSource) and costs ~$0.2 — vs ~$2.5 for extend_demo_recent.sql's anshar partition
-- scans. Extraction recipe = create_bus_calls.sql / create_goa_calls.sql (realized outcomes
-- only, estimated "actuals" nulled, dedup on latest recordedAtTime); Vy rail is
-- dataSource='VYG' + vehicleMode='rail' (excludes Vy replacement buses), tagged operator
-- 'NSB' to match the demo tables.
--
-- KNOWN TRADE vs the anshar path (measured 2026-07-07): the VYG-rail slice of last_recorded
-- runs ~10-17 % THINNER than the anshar recordedCalls extraction (calls the producer never
-- confirmed are missing), and data is fresh THROUGH YESTERDAY, not same-day. That is fine
-- for the nightly freshness layer; extend_demo_recent.sql (manual, run by
-- refresh_demo_recent.sh AFTER this script before partner demos) rebuilds the NSB window at
-- full anshar fidelity including today's completed journeys. Its rows are then replaced by
-- this script's thinner ones on the next nightly run — self-consistent, just re-run the
-- manual script when full train coverage matters.
--
-- TWO-FAMILY SCORING, same split as live serving and the static build:
--   * NSB rows -> the 9 prod heads + the rail per-ask-horizon platts (ML.WEIGHTS inline).
--   * GOA + bus rows -> the 9 *_pooled_all heads + the flattened operator-aware
--     disruptions.pooled_all_platt_weights lookup (rebuild it after platt refits).
-- Features are shared and pooled-shaped: net_pre scoped to the operator's own network (for
-- NSB that IS the whole-network rail convention), own_prev pinned to the journey instance
-- via operating_date (bus ServiceJourney jkeys repeat daily; rail degrades to the sentinel).
--
-- ROLLING ACCUMULATION: each run rebuilds ALL operators' rows with onset in
-- [GREATEST(2026-06-20, today-25d), now]; the static holdout is untouched. Derived
-- demo_stops/demo_highlights are rebuilt over the full table. Safe to re-run.

CREATE TEMP FUNCTION platt(p FLOAT64, a FLOAT64, b FLOAT64) AS (
  1 / (1 + EXP(-(b + a * LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6)
                            / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))))))
);

CREATE TEMP FUNCTION platt_w(p FLOAT64, a FLOAT64, b FLOAT64, w FLOAT64) AS (
  1 / (1 + EXP(-(b + a * LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6)
                            / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) + IFNULL(w, 0))))
);

CREATE TEMP FUNCTION stack_q(s5 FLOAT64, s10 FLOAT64, s20 FLOAT64, s40 FLOAT64, q FLOAT64) AS (
  CAST(ROUND(CASE
    WHEN s5  <= q THEN (1 - q) / GREATEST(1 - s5, 0.001) * 5
    WHEN s10 <= q THEN 5  + (s5  - q) / GREATEST(s5  - s10, 0.001) * 5
    WHEN s20 <= q THEN 10 + (s10 - q) / GREATEST(s10 - s20, 0.001) * 10
    WHEN s40 <= q THEN 20 + (s20 - q) / GREATEST(s20 - s40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s40 / q), 90) END) AS INT64)
);

-- 1 ---- recent realized calls (Vy rail + GOA rail + bus) from last_recorded
-- (create_bus_calls.sql recipe, rolling ~27-day window so the 25-day onset rebuild has its
-- pre-onset 60-min lookback).
CREATE TEMP TABLE rc AS
WITH src AS (
  SELECT
    -- VYG rail refs are tagged 'NSB' — the demo tables (and the SX publisher) use NSB.
    IF(dataSource = 'VYG', 'NSB',
       REGEXP_EXTRACT(serviceJourneyId, r'^([A-Za-z]+):')) AS operator,
    -- rail refs (VYG/GOA DatedServiceJourney) are date-scoped; keep operating_date NULL to
    -- match the static shapes (the demo's journey-instance pin falls back to its sentinel).
    IF(dataSource IN ('VYG', 'GOA'), CAST(NULL AS DATE), operatingDate) AS operating_date,
    REGEXP_REPLACE(serviceJourneyId, r'^[A-Za-z]+:', '') AS jkey,
    lineRef,
    stopPointRef AS quay,
    sequenceNr   AS stop_order,
    (COALESCE(journeyCancellation, FALSE) OR COALESCE(stopCancellation, FALSE)) AS call_cancelled,
    aimedDepartureTime AS aimed_dep,
    aimedArrivalTime   AS aimed_arr,
    IF(COALESCE(estimated, FALSE), NULL, departureTime) AS actual_dep,
    IF(COALESCE(estimated, FALSE), NULL, arrivalTime)   AS actual_arr
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded`
  WHERE operatingDate BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 27 DAY) AND CURRENT_DATE()
    AND (
      (dataSource IN ('KOL','SOF','NOR','SKY','ATB','OST','FIN','MOR','RUT','TRO','GCO')
       AND REGEXP_CONTAINS(serviceJourneyId, r'^[A-Za-z]+:ServiceJourney:'))
      OR (dataSource IN ('VYG', 'GOA') AND vehicleMode = 'rail')
    )
    AND stopPointRef IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY serviceJourneyId, operatingDate, sequenceNr
    ORDER BY recordedAtTime DESC
  ) = 1
),
typed AS (
  SELECT
    *,
    DATE(COALESCE(aimed_dep, aimed_arr), 'Europe/Oslo')  AS call_date,
    TIMESTAMP_DIFF(actual_dep, aimed_dep, MINUTE)        AS dep_delay_min,
    TIMESTAMP_DIFF(actual_arr, aimed_arr, MINUTE)        AS arr_delay_min
  FROM src
  WHERE COALESCE(aimed_dep, aimed_arr) IS NOT NULL
    -- realized outcomes only: cancelled, or operated (has a real actual time)
    AND (call_cancelled OR actual_dep IS NOT NULL OR actual_arr IS NOT NULL)
)
SELECT
  t.operator, t.operating_date, t.jkey, t.lineRef,
  q.stopPlaceRef AS stop_place,
  COALESCE(q.name, n.name) AS stop_name,
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
WHERE t.call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 26 DAY);

-- 2 ---- recent situations from SIRI-SX first versions, ALL publishers
-- (extend_demo_recent.sql pattern; onset = creationTime, parity with the training builds).
CREATE TEMP TABLE sit AS
WITH all_versions AS (
  SELECT
    situationNumber, participantRef, version, creationTime,
    COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) AS event_ts,
    severity, planned, summaries, descriptions, affects
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 26 DAY) AND CURRENT_DATE()
    AND participantRef IN ('NSB', 'GOA', 'GCO', 'MOR', 'NOR', 'TRO', 'FIN', 'RUT')
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
    fv.participantRef AS participant,
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
  e.participant,
  e.onset_ts,
  e.summary,
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
  -- only onsets the static pooled build does not already cover, within this run's window
  AND DATE(e.onset_ts) >= GREATEST(DATE '2026-06-20', DATE_SUB(CURRENT_DATE(), INTERVAL 25 DAY))
  AND ARRAY_LENGTH(e.affected_journeys) > 0;

-- 3 ---- model features per (situation x affected call), re-anchored at the three ask times.
-- Feature block mirrors create_training_calls_pooled_all.sql / predict_calls_bus.sql; the
-- lead re-anchoring mirrors leadtime_eval_pooled_all.sql.
CREATE TEMP TABLE feat AS
WITH aff AS (
  -- operator: NSB/GOA rail by publisher, bus by the affected-journey ref codespace (the
  -- network the journey runs in — GCO fans out); restrict bus to the trained pooled set.
  -- Same mapping as leadtime_eval_pooled_all.sql.
  SELECT s.* EXCEPT (participant),
         IF(s.participant IN ('NSB', 'GOA'),
            s.participant,
            REGEXP_EXTRACT(j, r'^([A-Za-z]+):')) AS operator,
         REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
  WHERE s.participant IN ('NSB', 'GOA')
     OR REGEXP_EXTRACT(j, r'^([A-Za-z]+):')
        IN ('KOL','SOF','NOR','SKY','ATB','OST','FIN','MOR','RUT','TRO','GCO')
),
base AS (
  SELECT
    a.operator, a.situationNumber, a.onset_ts, a.summary, a.category, a.stated_min,
    a.status, a.planned, a.num_journeys, a.num_stops, a.num_lines,
    EXTRACT(HOUR      FROM a.onset_ts) AS onset_hour,
    EXTRACT(DAYOFWEEK FROM a.onset_ts) AS onset_dow,
    EXTRACT(MONTH     FROM a.onset_ts) AS onset_month,
    a.jkey, r.lineRef, r.stop_place, r.stop_name, r.stop_order, r.operating_date,
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
  JOIN rc AS r USING (operator, jkey)
  WHERE TIMESTAMP_DIFF(r.call_ts, a.onset_ts, MINUTE) BETWEEN 0 AND 720
  -- first instance after onset per (situation, operator, jkey, stop_order): bus jkeys repeat
  -- daily, so a 12 h onset window can straddle two operating days (same rule as the static
  -- pooled builds).
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY a.situationNumber, a.operator, a.jkey, r.stop_order
    ORDER BY r.call_ts ASC
  ) = 1
),
sitline AS (
  SELECT DISTINCT operator, situationNumber, lineRef, onset_ts FROM base
),
linepre AS (
  -- pre-onset line congestion (60 min before onset); lineRef is operator-namespaced
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
  -- pre-onset network state, scoped to the OPERATOR's own network (pooled convention)
  SELECT p.situationNumber, p.operator,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS net_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS net_pre_cancel
  FROM (SELECT DISTINCT operator, situationNumber, onset_ts FROM sitline) AS p
  JOIN rc AS r
    ON r.operator = p.operator
   AND r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1, 2
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
  -- the vehicle's own realized delay at its latest earlier stop aimed >= lead min before this
  -- call, pinned to the journey instance via operating_date (sentinel for GOA/rail)
  SELECT e.situationNumber, e.operator, e.jkey, e.stop_order, e.lead_min,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev
  FROM expanded AS e
  JOIN rc AS r2
    ON r2.operator = e.operator
   AND r2.jkey = e.jkey
   AND COALESCE(r2.operating_date, DATE '1000-01-01')
       = COALESCE(e.operating_date, DATE '1000-01-01')
   AND r2.stop_order < e.stop_order
   AND COALESCE(r2.aimed_dep, r2.aimed_arr) <= TIMESTAMP_SUB(e.call_ts, INTERVAL e.lead_min MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  WHERE e.lead_min != -1
  GROUP BY 1, 2, 3, 4, 5
)
SELECT
  e.*,
  -- ask-time line/vehicle state: sentinels for the at-message (-1) variant, re-anchored
  -- observations for leads 30/60 (leadtime_eval_pooled_all.sql conventions)
  IF(e.lead_min = -1, 0,  IFNULL(lh.n, 0))        AS line_now_n,
  IF(e.lead_min = -1, -1, IFNULL(lh.d, -1))       AS line_now_delay,
  IF(e.lead_min = -1, -1, IFNULL(lh.c, -1))       AS line_now_cancel,
  IF(e.lead_min = -1, -1, IFNULL(o.own_prev, -1)) AS own_prev_delay,
  IFNULL(lp.line_pre_n, 0)       AS line_pre_n,
  IFNULL(lp.line_pre_delay, -1)  AS line_pre_delay,
  IFNULL(lp.line_pre_cancel, -1) AS line_pre_cancel,
  IFNULL(np.net_pre_delay, -1)   AS net_pre_delay,
  IFNULL(np.net_pre_cancel, -1)  AS net_pre_cancel
FROM expanded AS e
LEFT JOIN linepre AS lp ON lp.situationNumber = e.situationNumber AND lp.lineRef = e.lineRef
LEFT JOIN netpre  AS np ON np.situationNumber = e.situationNumber AND np.operator = e.operator
LEFT JOIN linehour AS lh
  ON e.lead_min != -1
 AND lh.lineRef = e.lineRef
 AND lh.h = TIMESTAMP_TRUNC(TIMESTAMP_SUB(e.call_ts, INTERVAL (e.lead_min + 30) MINUTE), HOUR)
LEFT JOIN own AS o
  ON o.situationNumber = e.situationNumber
 AND o.operator = e.operator
 AND o.jkey = e.jkey
 AND o.stop_order = e.stop_order
 AND o.lead_min = e.lead_min;

-- 4 ---- GOA + bus: score the nine _pooled_all heads (raw), leadtime_scored_pooled_all-shape.
CREATE TEMP TABLE scored_raw AS
WITH pooled_feat AS (
  SELECT * FROM feat WHERE operator != 'NSB'
),
clf AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_cancel_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf_pooled_all`, (SELECT * FROM pooled_feat))
),
a5 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_clf_pooled_all`, (SELECT * FROM pooled_feat))
),
a10 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf_pooled_all`, (SELECT * FROM pooled_feat))
),
a20 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_clf_pooled_all`, (SELECT * FROM pooled_feat))
),
a40 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_clf_pooled_all`, (SELECT * FROM pooled_feat))
),
d5 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_clf_pooled_all`, (SELECT * FROM pooled_feat))
),
d10 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf_pooled_all`, (SELECT * FROM pooled_feat))
),
d20 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_clf_pooled_all`, (SELECT * FROM pooled_feat))
),
d40 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_clf_pooled_all`, (SELECT * FROM pooled_feat))
)
SELECT
  f.operator, f.situationNumber, f.jkey, f.stop_order, f.lead_min, f.call_date, f.min_from_onset,
  f.is_origin, f.is_terminal,
  f.lineRef, f.stop_place, f.stop_name, f.aimed_dep, f.aimed_arr,
  f.onset_ts, f.summary, f.category,
  f.act_cancelled, f.act_dep_delay_min, f.act_arr_delay_min,
  clf.p_cancel_raw,
  a5.p_raw  AS p_arr5_raw,  a10.p_raw AS p_arr10_raw,
  a20.p_raw AS p_arr20_raw, a40.p_raw AS p_arr40_raw,
  d5.p_raw  AS p_dep5_raw,  d10.p_raw AS p_dep10_raw,
  d20.p_raw AS p_dep20_raw, d40.p_raw AS p_dep40_raw
FROM pooled_feat AS f
JOIN clf USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN a5  USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN a10 USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN a20 USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN a40 USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN d5  USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN d10 USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN d20 USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN d40 USING (operator, situationNumber, jkey, stop_order, lead_min);

-- 4b ---- NSB: score the nine PROD heads (raw), leadtime_scored-shape
-- (extend_demo_recent.sql pattern; NSB jkeys are date-scoped, so operator is a constant
-- passthrough, not a join key).
CREATE TEMP TABLE scored_raw_nsb AS
WITH nsb_feat AS (
  SELECT * FROM feat WHERE operator = 'NSB'
),
clf AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_cancel_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`, (SELECT * FROM nsb_feat))
),
a5 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_clf`, (SELECT * FROM nsb_feat))
),
a10 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf`, (SELECT * FROM nsb_feat))
),
a20 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_clf`, (SELECT * FROM nsb_feat))
),
a40 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_clf`, (SELECT * FROM nsb_feat))
),
d5 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_clf`, (SELECT * FROM nsb_feat))
),
d10 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM nsb_feat))
),
d20 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_clf`, (SELECT * FROM nsb_feat))
),
d40 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_clf`, (SELECT * FROM nsb_feat))
)
SELECT
  f.operator, f.situationNumber, f.jkey, f.stop_order, f.lead_min, f.call_date, f.min_from_onset,
  f.is_origin, f.is_terminal,
  f.lineRef, f.stop_place, f.stop_name, f.aimed_dep, f.aimed_arr,
  f.onset_ts, f.summary, f.category,
  f.act_cancelled, f.act_dep_delay_min, f.act_arr_delay_min,
  clf.p_cancel_raw,
  a5.p_raw  AS p_arr5_raw,  a10.p_raw AS p_arr10_raw,
  a20.p_raw AS p_arr20_raw, a40.p_raw AS p_arr40_raw,
  d5.p_raw  AS p_dep5_raw,  d10.p_raw AS p_dep10_raw,
  d20.p_raw AS p_dep20_raw, d40.p_raw AS p_dep40_raw
FROM nsb_feat AS f
JOIN clf USING (situationNumber, jkey, stop_order, lead_min)
JOIN a5  USING (situationNumber, jkey, stop_order, lead_min)
JOIN a10 USING (situationNumber, jkey, stop_order, lead_min)
JOIN a20 USING (situationNumber, jkey, stop_order, lead_min)
JOIN a40 USING (situationNumber, jkey, stop_order, lead_min)
JOIN d5  USING (situationNumber, jkey, stop_order, lead_min)
JOIN d10 USING (situationNumber, jkey, stop_order, lead_min)
JOIN d20 USING (situationNumber, jkey, stop_order, lead_min)
JOIN d40 USING (situationNumber, jkey, stop_order, lead_min);

-- 5 ---- calibrate per (ask horizon, operator) via the flattened weights lookup, pivot the
-- three ask times wide (create_demo_predictions.sql pooled statement, verbatim math).
CREATE TEMP TABLE recent_predictions AS
WITH sc AS (
  SELECT *, CASE lead_min WHEN 30 THEN '30' WHEN 60 THEN '60' ELSE 'plan' END AS lead_bucket
  FROM scored_raw
),
pwt AS (SELECT * FROM `ent-kixavvik-dev.disruptions.pooled_all_platt_weights`),
scored AS (
  SELECT
    sc.*,
    platt_w(sc.p_cancel_raw, pc.a,   pc.b,   pc.w)   AS p_cancel,
    platt_w(sc.p_dep10_raw,  pd10.a, pd10.b, pd10.w) AS p_dep_ge10,
    platt_w(sc.p_arr10_raw,  pa10.a, pa10.b, pa10.w) AS p_arr_ge10,
    GREATEST(platt_w(sc.p_arr5_raw,  pa5.a,  pa5.b,  pa5.w),
             platt_w(sc.p_arr10_raw, pa10.a, pa10.b, pa10.w),
             platt_w(sc.p_arr20_raw, pa20.a, pa20.b, pa20.w),
             platt_w(sc.p_arr40_raw, pa40.a, pa40.b, pa40.w)) AS s5,
    GREATEST(platt_w(sc.p_arr10_raw, pa10.a, pa10.b, pa10.w),
             platt_w(sc.p_arr20_raw, pa20.a, pa20.b, pa20.w),
             platt_w(sc.p_arr40_raw, pa40.a, pa40.b, pa40.w)) AS s10,
    GREATEST(platt_w(sc.p_arr20_raw, pa20.a, pa20.b, pa20.w),
             platt_w(sc.p_arr40_raw, pa40.a, pa40.b, pa40.w)) AS s20,
    platt_w(sc.p_arr40_raw, pa40.a, pa40.b, pa40.w) AS s40,
    GREATEST(platt_w(sc.p_dep5_raw,  pd5.a,  pd5.b,  pd5.w),
             platt_w(sc.p_dep10_raw, pd10.a, pd10.b, pd10.w),
             platt_w(sc.p_dep20_raw, pd20.a, pd20.b, pd20.w),
             platt_w(sc.p_dep40_raw, pd40.a, pd40.b, pd40.w)) AS d5,
    GREATEST(platt_w(sc.p_dep10_raw, pd10.a, pd10.b, pd10.w),
             platt_w(sc.p_dep20_raw, pd20.a, pd20.b, pd20.w),
             platt_w(sc.p_dep40_raw, pd40.a, pd40.b, pd40.w)) AS d10,
    GREATEST(platt_w(sc.p_dep20_raw, pd20.a, pd20.b, pd20.w),
             platt_w(sc.p_dep40_raw, pd40.a, pd40.b, pd40.w)) AS d20,
    platt_w(sc.p_dep40_raw, pd40.a, pd40.b, pd40.w) AS d40
  FROM sc
  JOIN pwt AS pc   ON pc.head  ='cancel'   AND pc.lead_bucket  =sc.lead_bucket AND pc.operator  =sc.operator
  JOIN pwt AS pa5  ON pa5.head ='arr_ge5'  AND pa5.lead_bucket =sc.lead_bucket AND pa5.operator =sc.operator
  JOIN pwt AS pa10 ON pa10.head='arr_ge10' AND pa10.lead_bucket=sc.lead_bucket AND pa10.operator=sc.operator
  JOIN pwt AS pa20 ON pa20.head='arr_ge20' AND pa20.lead_bucket=sc.lead_bucket AND pa20.operator=sc.operator
  JOIN pwt AS pa40 ON pa40.head='arr_ge40' AND pa40.lead_bucket=sc.lead_bucket AND pa40.operator=sc.operator
  JOIN pwt AS pd5  ON pd5.head ='dep_ge5'  AND pd5.lead_bucket =sc.lead_bucket AND pd5.operator =sc.operator
  JOIN pwt AS pd10 ON pd10.head='dep_ge10' AND pd10.lead_bucket=sc.lead_bucket AND pd10.operator=sc.operator
  JOIN pwt AS pd20 ON pd20.head='dep_ge20' AND pd20.lead_bucket=sc.lead_bucket AND pd20.operator=sc.operator
  JOIN pwt AS pd40 ON pd40.head='dep_ge40' AND pd40.lead_bucket=sc.lead_bucket AND pd40.operator=sc.operator
),
-- NSB branch: rail per-ask-horizon platt weights (ML.WEIGHTS inline, not operator-aware)
sc_nsb AS (
  SELECT *, CASE lead_min WHEN 30 THEN '30' WHEN 60 THEN '60' ELSE 'plan' END AS lead_bucket
  FROM scored_raw_nsb
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
scored_nsb AS (
  SELECT
    sc_nsb.*,
    platt(sc_nsb.p_cancel_raw, wch.a, wch.b)    AS p_cancel,
    platt(sc_nsb.p_dep10_raw, wd10h.a, wd10h.b) AS p_dep_ge10,
    platt(sc_nsb.p_arr10_raw, w10h.a, w10h.b)   AS p_arr_ge10,
    GREATEST(platt(sc_nsb.p_arr5_raw,  w5h.a,  w5h.b),
             platt(sc_nsb.p_arr10_raw, w10h.a, w10h.b),
             platt(sc_nsb.p_arr20_raw, w20h.a, w20h.b),
             platt(sc_nsb.p_arr40_raw, w40h.a, w40h.b)) AS s5,
    GREATEST(platt(sc_nsb.p_arr10_raw, w10h.a, w10h.b),
             platt(sc_nsb.p_arr20_raw, w20h.a, w20h.b),
             platt(sc_nsb.p_arr40_raw, w40h.a, w40h.b)) AS s10,
    GREATEST(platt(sc_nsb.p_arr20_raw, w20h.a, w20h.b),
             platt(sc_nsb.p_arr40_raw, w40h.a, w40h.b)) AS s20,
    platt(sc_nsb.p_arr40_raw, w40h.a, w40h.b) AS s40,
    GREATEST(platt(sc_nsb.p_dep5_raw,  wd5h.a,  wd5h.b),
             platt(sc_nsb.p_dep10_raw, wd10h.a, wd10h.b),
             platt(sc_nsb.p_dep20_raw, wd20h.a, wd20h.b),
             platt(sc_nsb.p_dep40_raw, wd40h.a, wd40h.b)) AS d5,
    GREATEST(platt(sc_nsb.p_dep10_raw, wd10h.a, wd10h.b),
             platt(sc_nsb.p_dep20_raw, wd20h.a, wd20h.b),
             platt(sc_nsb.p_dep40_raw, wd40h.a, wd40h.b)) AS d10,
    GREATEST(platt(sc_nsb.p_dep20_raw, wd20h.a, wd20h.b),
             platt(sc_nsb.p_dep40_raw, wd40h.a, wd40h.b)) AS d20,
    platt(sc_nsb.p_dep40_raw, wd40h.a, wd40h.b) AS d40
  FROM sc_nsb
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
-- both families in one shape for the shared per-lead/pivot below
scored_all AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min, call_date, min_from_onset,
         lineRef, stop_place, stop_name, aimed_dep, aimed_arr, onset_ts, summary, category,
         act_cancelled, act_dep_delay_min, act_arr_delay_min, is_origin, is_terminal,
         p_cancel, p_dep_ge10, p_arr_ge10, s5, s10, s20, s40, d5, d10, d20, d40
  FROM scored
  UNION ALL
  SELECT operator, situationNumber, jkey, stop_order, lead_min, call_date, min_from_onset,
         lineRef, stop_place, stop_name, aimed_dep, aimed_arr, onset_ts, summary, category,
         act_cancelled, act_dep_delay_min, act_arr_delay_min, is_origin, is_terminal,
         p_cancel, p_dep_ge10, p_arr_ge10, s5, s10, s20, s40, d5, d10, d20, d40
  FROM scored_nsb
),
per_lead AS (
  SELECT
    operator, situationNumber, jkey, stop_order, lead_min, call_date, min_from_onset,
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
  FROM scored_all
)
SELECT
  operator, situationNumber, jkey, stop_order,
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
GROUP BY operator, situationNumber, jkey, stop_order;

-- 6 ---- merge into the demo tables: replace EVERY operator's rows this run can rebuild
-- (the manual extend_demo_recent.sql may later overwrite the NSB slice at anshar fidelity).
DELETE FROM `ent-kixavvik-dev.disruptions.demo_predictions`
WHERE onset_ts >= TIMESTAMP(GREATEST(DATE '2026-06-20', DATE_SUB(CURRENT_DATE(), INTERVAL 25 DAY)));

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
  operator, situationNumber, jkey, stop_order, call_date, min_from_onset,
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
WHERE call_date > DATE '2026-06-20'
  AND call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 25 DAY);

INSERT INTO `ent-kixavvik-dev.disruptions.demo_calls`
  (operator, operating_date, jkey, lineRef, stop_place, stop_name, stop_order, call_date,
   aimed_dep, aimed_arr, call_cancelled, dep_delay_min, arr_delay_min)
SELECT
  operator, operating_date, jkey, lineRef, stop_place, stop_name, stop_order, call_date,
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
