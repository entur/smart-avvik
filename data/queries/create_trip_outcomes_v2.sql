-- v2 per-trip outcome table: UNCONDITIONAL candidate set.
--
-- Unlike v1 (which only scored the journeys SX flagged), v2 sources candidate
-- boardings from ALL rail traffic calling at the situation's affected StopPlaces
-- within the window -- so the non-flagged boardings become true GO negatives.
-- This answers "given a situation affects my area, is MY specific trip hit?"
-- rather than "given my exact journey was flagged, how bad?".
--
-- Join bridge: rail_calls.stop_place (ET Quay -> StopPlace via NSR) = SX
-- affected_stops. Equi-join on (stop_place, call_date) keeps the range join
-- bounded; the 12h window is applied after. Derived entirely from rail_calls +
-- disruptions (+ categories) -- no ET scan, so cheap to rebuild/retune.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.trip_outcomes_v2` AS
WITH sit AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime)    AS onset_ts,
    IFNULL(c.category, 'UNKNOWN')     AS category,
    ARRAY_LENGTH(d.affected_stops)    AS num_affected_stops,
    ARRAY_LENGTH(d.affected_journeys) AS num_affected_journeys,
    st AS stop_place,
    cd AS cand_date
  FROM `disruptions.disruptions` AS d
  LEFT JOIN `disruptions.categories` AS c
    ON d.summary = c.summary AND d.description = c.description,
  UNNEST(d.affected_stops) AS st,
  -- onset day and the next day cover the 12h window across midnight
  UNNEST([DATE(SAFE.TIMESTAMP(d.creationTime)),
          DATE_ADD(DATE(SAFE.TIMESTAMP(d.creationTime)), INTERVAL 1 DAY)]) AS cd
  WHERE d.participantRef = 'NSB'
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(d.creationTime)) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR)
)
SELECT
  -- keys (exclude from model features)
  s.situationNumber,
  r.jkey         AS journey,
  r.stop_place   AS boarding_stop,

  -- LABEL
  CASE
    WHEN r.call_cancelled      THEN 'CANCELLED'
    WHEN r.dep_delay_min >= 10 THEN 'DELAYED'
    ELSE 'GO'
  END AS outcome,

  -- FEATURES: situation
  s.category,
  s.num_affected_stops,
  s.num_affected_journeys,

  -- FEATURES: trip context
  r.lineRef,
  r.stop_order,
  EXTRACT(HOUR      FROM r.aimed_dep) AS dep_hour,
  EXTRACT(DAYOFWEEK FROM r.aimed_dep) AS dep_dow,
  TIMESTAMP_DIFF(r.aimed_dep, s.onset_ts, MINUTE) AS minutes_since_onset,

  -- leakage-safe split
  MOD(ABS(FARM_FINGERPRINT(s.situationNumber)), 10) >= 8 AS is_eval
FROM sit AS s
JOIN `disruptions.rail_calls` AS r
  ON r.stop_place = s.stop_place
 AND r.call_date  = s.cand_date
WHERE TIMESTAMP_DIFF(r.aimed_dep, s.onset_ts, MINUTE) BETWEEN 0 AND 720;
