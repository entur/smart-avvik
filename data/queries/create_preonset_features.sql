-- Leakage-safe pre-onset SIRI-ET features, one row per situationNumber.
--
-- IDEA: by the time an SX is posted, the disruption is often already visible in
-- ET as delayed/cancelled departures. These features quantify that "the trouble
-- is already underway" signal at onset, from three angles the user asked about:
--   * affected STOPS  (congestion at the affected location)
--   * affected LINES  (the line is already running late, incl. earlier journeys)
--   * affected JOURNEYS themselves (the specific trips named by the SX)
--
-- LEAKAGE CONTROL. rail_calls holds the FINAL ET snapshot, so dep_delay_min is the
-- realized delay -- not necessarily knowable at onset. A train aimed BEFORE onset
-- that actually departs AFTER onset would leak its future delay. So we use
--   knowable_delay = LEAST(final_delay, minutes the train was already overdue at onset)
-- which for a train that had departed by onset (aimed+delay < onset) equals the
-- true delay, and for one still waiting equals only its lateness-so-far. Cancels
-- are counted only for calls whose aimed_dep had already passed at onset (still a
-- mild assumption that the cancel was known by then -- noted in findings).
-- Window: [onset-60min, onset) for stops/lines; [onset-180min, onset) for the
-- named journeys (a single trip is sparse, so a wider lookback).

DECLARE win_min INT64 DEFAULT 60;

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.preonset_features` AS
WITH sit AS (
  SELECT
    situationNumber, onset_ts, affected_stops,
    ARRAY(SELECT REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') FROM UNNEST(affected_journeys) AS j) AS jkeys
  FROM `disruptions.disruptions_initial`
),
-- Lines touched by the named journeys -> lets us look at OTHER (earlier) trips on
-- the same line, not just the named journeys.
sit_lines AS (
  SELECT s.situationNumber, ARRAY_AGG(DISTINCT r.lineRef IGNORE NULLS) AS lines
  FROM sit AS s
  JOIN `disruptions.rail_calls` AS r ON r.jkey IN UNNEST(s.jkeys)
  GROUP BY s.situationNumber
),
-- Affected-stop window.
stopf AS (
  SELECT
    s.situationNumber,
    COUNT(*)                                                          AS pre_stop_calls,
    COUNTIF(r.call_cancelled AND r.aimed_dep < s.onset_ts)            AS pre_stop_cancel,
    COUNTIF(NOT r.call_cancelled
            AND LEAST(r.dep_delay_min,
                      TIMESTAMP_DIFF(s.onset_ts, r.aimed_dep, MINUTE)) >= 15) AS pre_stop_del15,
    MAX(IF(r.call_cancelled, NULL,
           LEAST(r.dep_delay_min,
                 TIMESTAMP_DIFF(s.onset_ts, r.aimed_dep, MINUTE))))   AS pre_stop_maxdelay,
    AVG(IF(r.call_cancelled, NULL,
           GREATEST(LEAST(r.dep_delay_min,
                          TIMESTAMP_DIFF(s.onset_ts, r.aimed_dep, MINUTE)), 0))) AS pre_stop_avgdelay
  FROM sit AS s
  JOIN `disruptions.rail_calls` AS r
    ON r.stop_place IN UNNEST(s.affected_stops)
   AND r.aimed_dep >= TIMESTAMP_SUB(s.onset_ts, INTERVAL win_min MINUTE)
   AND r.aimed_dep <  s.onset_ts
  GROUP BY s.situationNumber
),
-- Same-line window (all journeys on the affected lines).
linef AS (
  SELECT
    s.situationNumber,
    COUNT(*)                                                          AS pre_line_calls,
    COUNTIF(r.call_cancelled AND r.aimed_dep < s.onset_ts)            AS pre_line_cancel,
    COUNTIF(NOT r.call_cancelled
            AND LEAST(r.dep_delay_min,
                      TIMESTAMP_DIFF(s.onset_ts, r.aimed_dep, MINUTE)) >= 15) AS pre_line_del15,
    MAX(IF(r.call_cancelled, NULL,
           LEAST(r.dep_delay_min,
                 TIMESTAMP_DIFF(s.onset_ts, r.aimed_dep, MINUTE))))   AS pre_line_maxdelay
  FROM sit AS s
  JOIN sit_lines AS sl USING (situationNumber)
  JOIN `disruptions.rail_calls` AS r
    ON r.lineRef IN UNNEST(sl.lines)
   AND r.aimed_dep >= TIMESTAMP_SUB(s.onset_ts, INTERVAL win_min MINUTE)
   AND r.aimed_dep <  s.onset_ts
  GROUP BY s.situationNumber
),
-- The named journeys themselves (wider 180-min lookback; a single trip is sparse).
jourf AS (
  SELECT
    s.situationNumber,
    MAX(IF(r.call_cancelled, NULL,
           LEAST(r.dep_delay_min,
                 TIMESTAMP_DIFF(s.onset_ts, r.aimed_dep, MINUTE))))   AS pre_jour_maxdelay,
    COUNTIF(r.call_cancelled AND r.aimed_dep < s.onset_ts)            AS pre_jour_cancel
  FROM sit AS s
  JOIN `disruptions.rail_calls` AS r
    ON r.jkey IN UNNEST(s.jkeys)
   AND r.aimed_dep >= TIMESTAMP_SUB(s.onset_ts, INTERVAL 180 MINUTE)
   AND r.aimed_dep <  s.onset_ts
  GROUP BY s.situationNumber
)
SELECT
  s.situationNumber,
  IFNULL(st.pre_stop_calls, 0)    AS pre_stop_calls,
  st.pre_stop_calls IS NOT NULL   AS pre_stop_has_data,
  IFNULL(st.pre_stop_cancel, 0)   AS pre_stop_cancel,
  IFNULL(st.pre_stop_del15, 0)    AS pre_stop_del15,
  IFNULL(st.pre_stop_maxdelay, 0) AS pre_stop_maxdelay,
  IFNULL(st.pre_stop_avgdelay, 0) AS pre_stop_avgdelay,
  IFNULL(lf.pre_line_calls, 0)    AS pre_line_calls,
  IFNULL(lf.pre_line_cancel, 0)   AS pre_line_cancel,
  IFNULL(lf.pre_line_del15, 0)    AS pre_line_del15,
  IFNULL(lf.pre_line_maxdelay, 0) AS pre_line_maxdelay,
  IFNULL(jf.pre_jour_maxdelay, 0) AS pre_jour_maxdelay,
  IFNULL(jf.pre_jour_cancel, 0)   AS pre_jour_cancel
FROM sit AS s
LEFT JOIN stopf AS st USING (situationNumber)
LEFT JOIN linef AS lf USING (situationNumber)
LEFT JOIN jourf AS jf USING (situationNumber);
