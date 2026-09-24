-- Build disruptions.disruptions_initial_today: TODAY's NSB situations in the INITIAL-version
-- per-situation feature/label shape, so predict.sql can score the live incidents.
--
-- create_disruptions_initial.sql does the full 2-year build of disruptions.disruptions_initial
-- (the training population). This script applies the SAME first-version feature snapshot and
-- reopening/impact labelling, but scoped to today's SIRI-SX partitions only
-- (DATE(_PARTITIONTIME) = CURRENT_DATE() plus the unpartitioned streaming buffer, participantRef =
-- 'NSB'). It is a plain CREATE OR
-- REPLACE over today's partition — no EXECUTE IMMEDIATE, no 2-year scan, no dedup against the
-- training table.
--
-- IN-PROGRESS vs COMPLETED: unlike the training build (inner JOIN on the reopening message and
-- reopen_minutes BETWEEN 1 AND 10080), this uses a LEFT JOIN to end_times and applies NO
-- reopen-window filter, so situations that have NOT reopened yet are KEPT — with reopen_ts,
-- reopen_minutes and (therefore) impact_minutes NULL. That is intentional: today's incidents are
-- usually still ongoing when scored, and predict.sql reads only the feature columns, not the
-- labels.
--
-- IMPACT LABEL uses rail_calls_today (today's ET slice), NOT the 2-year rail_calls — today's
-- incidents' calls only live in the recent slice. Refresh rail_calls_today first
-- (rail_calls_today.sql) so each incident's onset + 12h window is covered; otherwise
-- impact_minutes collapses to reopen_minutes (and is NULL while a situation has not reopened).
--
-- Impact thresholds (15 min / 60 min gap / 12 h cap) are inlined literals here — identical to
-- create_disruptions_initial.sql's DECLAREs; keep them in sync if you tune them there.

CREATE OR REPLACE TABLE `disruptions.disruptions_initial_today` AS
WITH all_versions AS (
  SELECT
    participantRef, situationNumber, version, creationTime,
    COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) AS event_ts,
    severity, planned, reportType, priority, validityPeriods, summaries, descriptions, affects
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
  -- `OR _PARTITIONTIME IS NULL` reads the streaming buffer too (see last_ingestion_time.sql).
  WHERE (DATE(_PARTITIONTIME) = CURRENT_DATE() OR _PARTITIONTIME IS NULL)
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
    -- affected_lines: needed by the per-call models' num_lines feature (predict_calls.sql).
    -- Mirrors create_disruptions.sql's affected_lines; additive column, existing consumers ignore it.
    (
      SELECT ARRAY_AGG(line_ref ORDER BY line_ref)
      FROM (
        SELECT journeys.lineRef AS line_ref
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys WHERE journeys.lineRef IS NOT NULL
        UNION DISTINCT
        SELECT al.lineRef
        FROM UNNEST(fv.affects.networks) AS n, UNNEST(n.affectedLines) AS al WHERE al.lineRef IS NOT NULL
      )
    ) AS affected_lines,
    EXTRACT(MONTH     FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS month,
    EXTRACT(DAY       FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS day,
    EXTRACT(DAYOFWEEK FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS dayofweek,
    EXTRACT(HOUR      FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS hour
  FROM first_version AS fv
  LEFT JOIN end_times AS et USING (participantRef, situationNumber)
  WHERE (fv.severity IS NULL OR fv.severity != 'NO_IMPACT')
),
-- Impacted calls (cancel or big delay) at affected stops, within the look window.
-- rail_calls_today (today's ET), NOT rail_calls (2-year) — today's incidents' calls live in
-- the recent slice. impact_delay_min = 15, impact_window_hour = 12 (inlined; see header).
impacted AS (
  SELECT b.situationNumber, r.aimed_dep AS ts
  FROM base AS b
  JOIN `disruptions.rail_calls_today` AS r
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
