-- Per-trip outcome predictions (GO / DELAYED / CANCELLED) from the v1 CONDITIONAL
-- model `trip_outcome_clf` for EVERY post-training NSB disruption in
-- disruptions_initial_new, set against what actually happened from SIRI-ET.
--
-- v1 is the conditional model: it scores only the journeys SX itself flagged
-- ("given your exact journey was flagged, how bad?"). Candidate boardings are the
-- disruption's affected_journeys joined to rail_calls_new by jkey — one row per
-- (situation x flagged journey x boardable stop), the trip_outcomes grain. The v2
-- companion (predict_initial_new_trips_v2.sql) scores ALL traffic at the affected
-- stops instead.
--
-- FEATURES mirror create_trip_outcomes.sql exactly (names verbatim from
-- ML.FEATURE_INFO): boarding_stop is the ET Quay (stopPointRef), onset is
-- SAFE.TIMESTAMP(creationTime) — NOT onset_ts/event_ts, because that's what the
-- model trained on — and minutes_since_onset is clipped to the trained 0..720 range.
--
-- CALIBRATION: raw boosted-tree probabilities are auto_class_weights-inflated, so
-- they are passed through the Platt scalers fitted in calibrate_trip_model.sql
-- (cal_cancelled -> P(CANCELLED), cal_disrupted -> P(not GO)) before being surfaced
-- or thresholded. The two-tier policy message uses the cutoffs documented in
-- calibrate_trip_model_v2.sql step 6 (Tier 1 p_cancelled_cal >= 0.40 "alternative /
-- WFH", Tier 2 p_disrupted_cal >= 0.30 "expect delay"); tune via the threshold
-- sweeps in the calibrate scripts.
--
-- ACTUALS: outcome realized from the same rail_calls_new row (cancelled / delay >=
-- 10 min / go — the label definition). rail_calls_new must cover each incident's
-- onset + 12h (refresh with create_rail_calls_new.sql first). NOTE rail_calls_new
-- is VYG-only, so flagged journeys on other operators' lines are absent; v1
-- training scanned all ET, but the affected NSB journeys are VYG in practice.
--
-- The deterministic row id (MD5 over the grain) exists because the two scaler
-- ML.PREDICT calls re-evaluate their input CTE — GENERATE_UUID() would not survive
-- that (the calibrate scripts materialize intermediate tables for the same reason).

WITH
src AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime)    AS onset_ts,
    IFNULL(c.category, 'UNKNOWN')     AS category,
    ARRAY_LENGTH(d.affected_stops)    AS num_affected_stops,
    ARRAY_LENGTH(d.affected_journeys) AS num_affected_journeys,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM `disruptions.disruptions_initial_new` AS d
  LEFT JOIN `disruptions.categories` AS c
    ON  IFNULL(d.summary, '__NULL__')     = IFNULL(c.summary, '__NULL__')
    AND IFNULL(d.description, '__NULL__') = IFNULL(c.description, '__NULL__'),
  UNNEST(d.affected_journeys) AS j
  WHERE d.participantRef = 'NSB'
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
),
feat AS (
  SELECT
    -- pass-through (not features; ML.PREDICT carries them to the output)
    s.situationNumber,
    r.jkey   AS journey,
    r.aimed_dep,
    -- features (exact trained shape)
    s.category,
    s.num_affected_stops,
    s.num_affected_journeys,
    r.quay   AS boarding_stop,
    r.lineRef,
    r.stop_order,
    EXTRACT(HOUR      FROM r.aimed_dep) AS dep_hour,
    EXTRACT(DAYOFWEEK FROM r.aimed_dep) AS dep_dow,
    TIMESTAMP_DIFF(r.aimed_dep, s.onset_ts, MINUTE) AS minutes_since_onset,
    -- realized outcome (label definition; NOT a feature)
    CASE
      WHEN r.call_cancelled       THEN 'CANCELLED'
      WHEN r.dep_delay_min >= 10  THEN 'DELAYED'
      ELSE 'GO'
    END AS actual_outcome
  FROM src AS s
  JOIN `disruptions.rail_calls_new` AS r USING (jkey)
  WHERE r.aimed_dep IS NOT NULL
    AND TIMESTAMP_DIFF(r.aimed_dep, s.onset_ts, MINUTE) BETWEEN 0 AND 720
),
scored AS (
  SELECT
    TO_HEX(MD5(CONCAT(situationNumber, '|', journey, '|', boarding_stop, '|',
                      CAST(aimed_dep AS STRING)))) AS id,
    situationNumber, journey, boarding_stop, aimed_dep,
    category, lineRef, minutes_since_onset, actual_outcome,
    predicted_outcome,
    (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'CANCELLED') AS p_cancelled,
    (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'DELAYED')   AS p_delayed
  FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf`, (SELECT * FROM feat))
),
base AS (
  SELECT
    *,
    LOG(GREATEST(LEAST(p_cancelled, 0.999), 0.001)
        / (1 - GREATEST(LEAST(p_cancelled, 0.999), 0.001))) AS logit_can,
    LOG(GREATEST(LEAST(p_delayed + p_cancelled, 0.999), 0.001)
        / (1 - GREATEST(LEAST(p_delayed + p_cancelled, 0.999), 0.001))) AS logit_dis
  FROM scored
),
can AS (
  SELECT id, (SELECT prob FROM UNNEST(predicted_is_cancelled_probs) WHERE label = 1) AS p_cancelled_cal
  FROM ML.PREDICT(MODEL `disruptions.cal_cancelled`,
        (SELECT id, logit_can AS logit_p FROM base))
),
dis AS (
  SELECT id, (SELECT prob FROM UNNEST(predicted_is_disrupted_probs) WHERE label = 1) AS p_disrupted_cal
  FROM ML.PREDICT(MODEL `disruptions.cal_disrupted`,
        (SELECT id, logit_dis AS logit_p FROM base))
)
SELECT
  b.situationNumber,
  b.journey,
  b.boarding_stop,
  b.aimed_dep,
  b.category,
  b.lineRef,
  b.minutes_since_onset,
  b.predicted_outcome,
  ROUND(can.p_cancelled_cal, 3) AS p_cancelled_cal,
  ROUND(dis.p_disrupted_cal, 3) AS p_disrupted_cal,
  -- two-tier policy cascade: severe message wins (cutoffs: see header)
  CASE
    WHEN can.p_cancelled_cal >= 0.40 THEN '1_ALTERNATIVE_WFH'
    WHEN dis.p_disrupted_cal >= 0.30 THEN '2_EXPECT_DELAY'
    ELSE '3_OK'
  END AS message,
  b.actual_outcome
FROM base AS b
JOIN can USING (id)
JOIN dis USING (id)
ORDER BY p_cancelled_cal DESC, b.situationNumber, b.aimed_dep;
