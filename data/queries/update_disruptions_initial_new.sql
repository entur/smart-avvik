-- Build disruptions.disruptions_initial_new: the post-training slice of the per-situation
-- INITIAL-version table.
--
-- create_disruptions_initial.sql does a full 2-year build of disruptions.disruptions_initial
-- (the training population). This script applies that SAME feature/label logic to ONLY the
-- NSB situations whose onset falls on/after the day disruptions_initial was last built, and
-- writes them to disruptions_initial_new — the population predict_initial_new_duration.sql and
-- predict_610_duration.sql score against. New incidents carry situationNumbers the training
-- table never saw, so this gives the predict_* queries a fresh, schema-identical slice to run on.
--
-- HOW (mirrors update_categories.sql):
--  1. last_update_date = the day disruptions.disruptions_initial was last modified (from the
--     dataset's __TABLES__ metadata, last_modified_time in epoch ms).
--  2. Re-scan SIRI-SX for partitions on/after that day (>= so the build day is re-covered) and
--     run create_disruptions_initial.sql's first-version feature snapshot + reopening/impact
--     labelling on it.
--  3. Keep only situations NOT already in disruptions_initial (dedup by participantRef,
--     situationNumber). Cost scales with the recent window, not the whole 2-year history.
--
-- WHY EXECUTE IMMEDIATE: same reason as update_categories.sql — inlining the date as a literal
-- lets the _PARTITIONTIME filter prune to just the recent partitions (a scripting variable in
-- the filter may not prune). The single literal '%' in LIKE 'Åpnet%' is escaped as '%%' so
-- FORMAT leaves it intact.
--
-- IMPACT LABEL uses rail_calls_new (the recent-window ET table), NOT the 2-year rail_calls —
-- new incidents' calls only live in the recent slice. Refresh rail_calls_new first
-- (create_rail_calls_new.sql) so each incident's onset + 12h window is covered, or impact_minutes
-- collapses to reopen_minutes for any incident outside rail_calls_new's 30-day range. Bump that
-- script's lookback if last_update_date is more than ~30 days ago.
--
-- EDGE CASES:
--  * Only reopened situations survive (inner JOIN end_times, same as the training build), so a
--    very recent incident that hasn't reopened yet won't appear until it does — re-run then.
--  * A situation straddling the build day (onset just before last_update_date, not yet in
--    disruptions_initial) would have its onset clipped to the pruned window. The NOT EXISTS dedup
--    removes anything already captured; the residual straddlers are a rare in-progress-at-build
--    tail.
--
-- Impact thresholds (15 min / 60 min gap / 12 h cap) are inlined literals here — identical to
-- create_disruptions_initial.sql's DECLAREs; keep them in sync if you tune them there.

DECLARE last_update_date DATE DEFAULT (
  SELECT DATE(TIMESTAMP_MILLIS(last_modified_time))
  FROM `ent-smartavvik-dev.disruptions.__TABLES__`
  WHERE table_id = 'disruptions_initial'
);

EXECUTE IMMEDIATE FORMAT(r"""
CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.disruptions_initial_new` AS
WITH all_versions AS (
  SELECT
    participantRef, situationNumber, version, creationTime,
    COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) AS event_ts,
    severity, planned, reportType, priority, validityPeriods, summaries, descriptions, affects
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
  WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) >= TIMESTAMP(DATE '%s')
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
      AND (s.value = 'Toget kjører igjen' OR s.value LIKE 'Åpnet%%')
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
    -- only situations NOT already in the training build (the incremental slice)
    AND NOT EXISTS (
      SELECT 1
      FROM `ent-smartavvik-dev.disruptions.disruptions_initial` AS di
      WHERE di.participantRef = fv.participantRef
        AND di.situationNumber = fv.situationNumber
    )
),
-- Impacted calls (cancel or big delay) at affected stops, within the look window.
-- rail_calls_new (recent ET), NOT rail_calls (2-year) — new incidents' calls live in the
-- recent slice. impact_delay_min = 15, impact_window_hour = 12 (inlined; see header).
impacted AS (
  SELECT b.situationNumber, r.aimed_dep AS ts
  FROM base AS b
  JOIN `ent-smartavvik-dev.disruptions.rail_calls_new` AS r
    ON r.stop_place IN UNNEST(b.affected_stops)
   AND r.aimed_dep BETWEEN b.onset_ts AND TIMESTAMP_ADD(b.onset_ts, INTERVAL 12 HOUR)
   AND (r.call_cancelled OR r.dep_delay_min >= 15)
),
-- Anchor each situation at onset so the first episode always starts at the clock start.
anchored AS (
  SELECT situationNumber, onset_ts, onset_ts AS ts FROM base
  UNION ALL
  SELECT i.situationNumber, b.onset_ts, i.ts FROM impacted AS i JOIN base AS b USING (situationNumber)
),
-- Sessionize: a gap > impact_gap_min (60 min) starts a new episode; episode 1 contains onset.
sessed AS (
  SELECT situationNumber, onset_ts, ts,
    SUM(CASE WHEN prev_ts IS NULL OR TIMESTAMP_DIFF(ts, prev_ts, MINUTE) > 60 THEN 1 ELSE 0 END)
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
LEFT JOIN impact_tail AS t USING (situationNumber)
""", CAST(last_update_date AS STRING));
