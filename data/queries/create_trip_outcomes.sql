-- Per-trip outcome training table for the passenger "wait / alternative / WFH"
-- recommendation. POC scope: NSB, last 1 month.
--
-- GRAIN: one row = one planned boarding = (situation x affected journey x stop).
-- LABEL: the realized outcome at that stop's DEPARTURE, bucketed to the action:
--   CANCELLED  call/journey cancelled        -> alternative / WFH
--   SEVERE     departure delay >= 30 min      -> alternative
--   MODERATE   10-30 min                      -> wait / consider
--   ON_TIME    < 10 min                       -> go
--
-- FEATURES are strictly decision-time (knowable before the trip). Scheduled
-- (aimed*) times are timetable, so safe to use; actual*/cancellation are LABEL
-- ONLY and must never appear as features. minutes_since_onset (scheduled
-- departure - situation creationTime) is expected to be the strongest signal.
--
-- TRAINING: 4-class classifier on `outcome`; SELECT * EXCEPT(situationNumber,
-- journey); use is_eval as DATA_SPLIT_COL. Surface calibrated class
-- probabilities (esp. P(CANCELLED)+P(SEVERE)), not the argmax.
--
-- KNOWN LIMITATION (selection bias): every journey here was flagged by SX as
-- affected, so the table answers "given your trip is in a flagged disruption,
-- how bad?" -- NOT "is my trip affected at all?". There are no true negatives
-- (trips that ran fine despite a nearby situation). The v2 fix is to source
-- candidate boardings from ALL ET traffic at the affected stops in the window,
-- which yields the unaffected boardings as ON_TIME negatives (same ET cost class).
--
-- COST: reads recordedCalls -> ~0.2 TB for one month (~$1). Dry-run before
-- widening the window.

DECLARE win_start DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH);
DECLARE win_end   DATE DEFAULT CURRENT_DATE();

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.trip_outcomes_poc` AS
WITH
  -- Affected dated journeys of NSB situations (in window) + situation features.
  -- Codespace normalized (older SX uses NSB:, ET uses VYG:) by dropping "XXX:".
  aff AS (
    SELECT
      d.situationNumber,
      SAFE.TIMESTAMP(d.creationTime)    AS onset_ts,
      IFNULL(c.category, 'UNKNOWN')     AS category,
      IFNULL(d.severity, 'UNKNOWN')     AS severity,
      IFNULL(d.planned, FALSE)          AS planned,
      ARRAY_LENGTH(d.affected_stops)    AS num_affected_stops,
      ARRAY_LENGTH(d.affected_journeys) AS num_affected_journeys,
      REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey,
      PARSE_DATE('%y-%m-%d', REGEXP_EXTRACT(j, r'_(\d{2}-\d{2}-\d{2})$')) AS svc_date
    FROM `disruptions.disruptions` AS d
    LEFT JOIN `disruptions.categories` AS c
      ON d.summary = c.summary AND d.description = c.description,
    UNNEST(d.affected_journeys) AS j
    WHERE d.participantRef = 'NSB'
  ),
  aff_win AS (SELECT * FROM aff WHERE svc_date BETWEEN win_start AND win_end),

  -- Last ET snapshot per dated journey in-window (the realized outcome).
  et AS (
    SELECT
      REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
      lineRef, cancellation AS journey_cancelled, recordedCalls
    FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
    WHERE DATE(_PARTITIONTIME) BETWEEN win_start AND DATE_ADD(win_end, INTERVAL 1 DAY)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY datedVehicleJourneyRef ORDER BY recordedAtTime DESC) = 1
  ),

  -- Explode to per-stop departure outcome.
  calls AS (
    SELECT
      e.jkey, e.lineRef, e.journey_cancelled,
      rc.stopPointRef, rc.order AS stop_order,
      SAFE.TIMESTAMP(rc.aimedDepartureTime) AS aimed_dep,
      (rc.cancellation OR e.journey_cancelled) AS call_cancelled,
      TIMESTAMP_DIFF(SAFE.TIMESTAMP(rc.actualDepartureTime),
                     SAFE.TIMESTAMP(rc.aimedDepartureTime), MINUTE) AS dep_delay_min
    FROM et AS e, UNNEST(e.recordedCalls) AS rc
  )

SELECT
  -- keys (exclude from model features)
  a.situationNumber,
  c.jkey            AS journey,
  c.stopPointRef    AS boarding_stop,

  -- LABEL
  CASE
    WHEN c.call_cancelled      THEN 'CANCELLED'
    WHEN c.dep_delay_min >= 30 THEN 'SEVERE'
    WHEN c.dep_delay_min >= 10 THEN 'MODERATE'
    ELSE 'ON_TIME'
  END AS outcome,

  -- FEATURES: situation
  a.category,
  a.severity,
  a.planned,
  a.num_affected_stops,
  a.num_affected_journeys,

  -- FEATURES: trip context
  c.lineRef,
  c.stop_order,
  EXTRACT(HOUR      FROM c.aimed_dep) AS dep_hour,
  EXTRACT(DAYOFWEEK FROM c.aimed_dep) AS dep_dow,
  TIMESTAMP_DIFF(c.aimed_dep, a.onset_ts, MINUTE) AS minutes_since_onset,

  -- leakage-safe split (all rows of a situation stay on one side)
  MOD(ABS(FARM_FINGERPRINT(a.situationNumber)), 10) >= 8 AS is_eval
FROM aff_win AS a
JOIN calls AS c USING (jkey)
WHERE c.aimed_dep IS NOT NULL                            -- a boardable (non-terminal) stop
  AND (c.call_cancelled OR c.dep_delay_min IS NOT NULL)  -- has a realized outcome
  AND TIMESTAMP_DIFF(c.aimed_dep, a.onset_ts, MINUTE) BETWEEN 0 AND 720;  -- decidable trips, <= 12h after onset

-- Sanity check: class balance (run on its own; final statement returns rows).
-- SELECT outcome, COUNT(*) AS n, ROUND(COUNT(*)/SUM(COUNT(*)) OVER (), 3) AS share
-- FROM `disruptions.trip_outcomes_poc` GROUP BY outcome ORDER BY n DESC;
