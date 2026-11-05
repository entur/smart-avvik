-- Per-situation table for the two INITIAL-version duration models, on one shared
-- population (NSB situations that emit a reopening message) so a single passenger
-- message can carry both numbers:
--   reopen_minutes  -- "trains running again in ~X min"  (onset -> first reopening
--                       summary: "Toget kjører igjen" / "Åpnet…")
--   impact_minutes  -- "traffic impacted for ~Y min"     (onset -> when cancels /
--                       big delays at affected stops actually subside; >= reopen)
--
-- WHY FEATURES COME FROM THE FIRST VERSION:
-- both numbers are predicted at onset, so every feature is read from the earliest
-- snapshot. Neither label is a feature (both are post-hoc) — no leakage.
--
-- IMPACT LABEL DEFINITION (rail_calls-derived):
-- impacted call = at an affected StopPlace, cancelled OR departure delay >= 15 min.
-- Walking forward from onset, the impact "episode" continues while consecutive
-- impacted calls are <= 60 min apart (a quiet gap > 60 min ends it); capped at 12h.
-- impact_minutes = GREATEST(reopen_minutes, episode end - onset): while the line is
-- shut, traffic IS impacted, so impact can never be shorter than the closure.
--   * >= 15 min (not >= 5): the 5-min delay base rate is 6%, so a loose gap bridges
--     ordinary delays at busy hubs and inflates impact to hours. >= 15 (0.94% base)
--     keeps the tail disruption-attributable. Validated: median 35 min, p90 273,
--     1% hit the 12h cap, 49% have a tail beyond reopening.
--   * Thresholds (15 min / 60 min gap / 12h cap) are the obvious tuning knobs.
--
-- COVERAGE: only 7.8% of NSB situations ever reopen; ~7,922 survive the filter.
-- That slice is the acute unplanned stoppages — the right population for a
-- "running again / impacted for" message. Grain: one row per situationNumber.

DECLARE impact_delay_min   INT64 DEFAULT 15;   -- delay (min) that counts as impact
DECLARE impact_gap_min     INT64 DEFAULT 60;   -- quiet gap (min) that ends an episode
DECLARE impact_window_hour INT64 DEFAULT 12;   -- max hours after onset to look

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.disruptions_initial` AS
WITH all_versions AS (
  SELECT
    participantRef, situationNumber, version, creationTime,
    COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) AS event_ts,
    severity, planned, reportType, priority, validityPeriods, summaries, descriptions, affects
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
  WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY)
          >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR))
    AND participantRef = 'NSB'
),
-- First version = feature snapshot AND clock start (onset).
first_version AS (
  SELECT *
  FROM all_versions
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY participantRef, situationNumber
    ORDER BY event_ts ASC, version ASC, creationTime ASC
  ) = 1
),
-- Observed reopening = earliest snapshot announcing the line is back.
end_times AS (
  SELECT participantRef, situationNumber, MIN(event_ts) AS reopen_ts
  FROM all_versions
  WHERE EXISTS (
    SELECT 1 FROM UNNEST(summaries) AS s
    WHERE (s.language IS NULL OR UPPER(s.language) = 'NO')
      AND (s.value = 'Toget kjører igjen' OR s.value LIKE 'Åpnet%')
  )
  GROUP BY participantRef, situationNumber
),
base AS (
  SELECT
    fv.participantRef,
    fv.situationNumber,
    fv.creationTime,
    fv.event_ts                                        AS onset_ts,
    et.reopen_ts,
    TIMESTAMP_DIFF(et.reopen_ts, fv.event_ts, MINUTE)  AS reopen_minutes,
    fv.severity,
    fv.planned,
    fv.reportType,
    fv.priority,
    (
      SELECT TIMESTAMP_DIFF(MAX(SAFE.TIMESTAMP(v.endTime)), fv.event_ts, MINUTE)
      FROM UNNEST(fv.validityPeriods) AS v
      WHERE SAFE.TIMESTAMP(v.endTime) > fv.event_ts
        AND SAFE.TIMESTAMP(v.endTime) < TIMESTAMP_ADD(fv.event_ts, INTERVAL 365 DAY)
    ) AS declared_window_minutes,
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
        FROM UNNEST(fv.affects.networks) AS n, UNNEST(n.affectedLines) AS al,
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
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys, UNNEST(journeys.datedVehicleJourneyRefs) AS dvjr
        WHERE dvjr IS NOT NULL
        UNION DISTINCT
        -- plain vehicleJourneyRefs: ServiceJourney refs (RUT/KOL, replacement buses), NOT date-scoped
        SELECT vjr
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys, UNNEST(journeys.vehicleJourneyRefs) AS vjr
        WHERE vjr IS NOT NULL
      )
    ) AS affected_journeys,
    EXTRACT(MONTH     FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS month,
    EXTRACT(DAY       FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS day,
    EXTRACT(DAYOFWEEK FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS dayofweek,
    EXTRACT(HOUR      FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS hour
  FROM first_version AS fv
  JOIN end_times AS et USING (participantRef, situationNumber)
  WHERE (fv.severity IS NULL OR fv.severity != 'NO_IMPACT')
    AND TIMESTAMP_DIFF(et.reopen_ts, fv.event_ts, MINUTE) BETWEEN 1 AND 10080
),
-- Impacted calls (cancel or big delay) at affected stops, within the look window.
impacted AS (
  SELECT b.situationNumber, r.aimed_dep AS ts
  FROM base AS b
  JOIN `ent-smartavvik-dev.disruptions.rail_calls` AS r
    ON r.stop_place IN UNNEST(b.affected_stops)
   AND r.aimed_dep BETWEEN b.onset_ts AND TIMESTAMP_ADD(b.onset_ts, INTERVAL impact_window_hour HOUR)
   AND (r.call_cancelled OR r.dep_delay_min >= impact_delay_min)
),
-- Anchor each situation at onset so the first episode always starts at the clock start.
anchored AS (
  SELECT situationNumber, onset_ts, onset_ts AS ts FROM base
  UNION ALL
  SELECT i.situationNumber, b.onset_ts, i.ts FROM impacted AS i JOIN base AS b USING (situationNumber)
),
-- Sessionize: a gap > impact_gap_min starts a new episode; episode 1 contains onset.
sessed AS (
  SELECT situationNumber, onset_ts, ts,
    SUM(CASE WHEN prev_ts IS NULL OR TIMESTAMP_DIFF(ts, prev_ts, MINUTE) > impact_gap_min THEN 1 ELSE 0 END)
      OVER (PARTITION BY situationNumber ORDER BY ts) AS session_id
  FROM (
    SELECT situationNumber, onset_ts, ts,
      LAG(ts) OVER (PARTITION BY situationNumber ORDER BY ts) AS prev_ts
    FROM anchored
  )
),
impact_tail AS (
  SELECT situationNumber, TIMESTAMP_DIFF(MAX(ts), MIN(onset_ts), MINUTE) AS tail_minutes
  FROM sessed WHERE session_id = 1 GROUP BY situationNumber
)
SELECT
  b.*,
  GREATEST(b.reopen_minutes, IFNULL(t.tail_minutes, 0)) AS impact_minutes
FROM base AS b
LEFT JOIN impact_tail AS t USING (situationNumber);
