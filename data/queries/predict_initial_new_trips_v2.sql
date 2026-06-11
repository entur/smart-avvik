-- Per-trip outcome predictions (GO / DELAYED / CANCELLED) from the v2 UNCONDITIONAL
-- model `trip_outcome_clf_v2` for EVERY post-training NSB disruption in
-- disruptions_initial_new, set against what actually happened from SIRI-ET.
--
-- This is the v2 companion to predict_initial_new_trips.sql. Unlike v1 (which only
-- scores the journeys SX flagged), v2 scores ALL rail traffic calling at the
-- situation's affected StopPlaces within onset + 12h — answering "given a situation
-- affects my area, is MY specific trip hit?". Candidate boardings mirror
-- create_trip_outcomes_v2.sql: equi-join rail_calls_new on (stop_place, call_date)
-- over the onset day and the next day, 12h window applied after.
--
-- FEATURES mirror create_trip_outcomes_v2.sql exactly (names verbatim from
-- ML.FEATURE_INFO): boarding_stop is the NSR StopPlace (not the Quay, which is what
-- v1 uses), onset is SAFE.TIMESTAMP(creationTime) — what the model trained on —
-- and minutes_since_onset is clipped to the trained 0..720 range.
--
-- CALIBRATION: raw probabilities are auto_class_weights-inflated, so they are
-- passed through the v2 Platt scalers from calibrate_trip_model_v2.sql
-- (cal_cancelled_v2 -> P(CANCELLED), cal_disrupted_v2 -> P(not GO)). The two-tier
-- policy message uses that script's step-6 cutoffs (Tier 1 p_cancelled_cal >= 0.40
-- "alternative / WFH" at ~91% precision, Tier 2 p_disrupted_cal >= 0.30 "expect
-- delay"); tune via the threshold sweeps there.
--
-- ACTUALS: outcome realized from the same rail_calls_new row (cancelled / delay >=
-- 10 min / go — the label definition). rail_calls_new must cover each incident's
-- onset + 12h (refresh with create_rail_calls_new.sql first).
--
-- The deterministic row id (MD5 over the grain) exists because the two scaler
-- ML.PREDICT calls re-evaluate their input CTE — GENERATE_UUID() would not survive
-- that (the calibrate scripts materialize intermediate tables for the same reason).

WITH
sit AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime)    AS onset_ts,
    IFNULL(c.category, 'UNKNOWN')     AS category,
    ARRAY_LENGTH(d.affected_stops)    AS num_affected_stops,
    ARRAY_LENGTH(d.affected_journeys) AS num_affected_journeys,
    st AS stop_place,
    cd AS cand_date
  FROM `disruptions.disruptions_initial_new` AS d
  LEFT JOIN `disruptions.categories` AS c
    ON d.summary = c.summary AND d.description = c.description,
  UNNEST(d.affected_stops) AS st,
  -- onset day and the next day cover the 12h window across midnight
  UNNEST([DATE(SAFE.TIMESTAMP(d.creationTime)),
          DATE_ADD(DATE(SAFE.TIMESTAMP(d.creationTime)), INTERVAL 1 DAY)]) AS cd
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
    r.stop_place AS boarding_stop,
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
  FROM sit AS s
  JOIN `disruptions.rail_calls_new` AS r
    ON r.stop_place = s.stop_place
   AND r.call_date  = s.cand_date
  WHERE TIMESTAMP_DIFF(r.aimed_dep, s.onset_ts, MINUTE) BETWEEN 0 AND 720
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
  FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf_v2`, (SELECT * FROM feat))
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
  FROM ML.PREDICT(MODEL `disruptions.cal_cancelled_v2`,
        (SELECT id, logit_can AS logit_p FROM base))
),
dis AS (
  SELECT id, (SELECT prob FROM UNNEST(predicted_is_disrupted_probs) WHERE label = 1) AS p_disrupted_cal
  FROM ML.PREDICT(MODEL `disruptions.cal_disrupted_v2`,
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
