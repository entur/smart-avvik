-- Build disruptions.disruptions_bus_initial_today: TODAY's BUS/GOA situations in the first-version
-- feature snapshot the per-call bus model (_pooled_all) needs. Sibling of disruptions_today.sql
-- (which builds the NSB disruptions_initial_today), with two differences:
--   1. participantRef IN the bus/GOA publishers instead of 'NSB'. GCO fans a single situation
--      across county codespaces (KOL/SOF/NOR/SKY refs), so its participantRef is 'GCO' but its
--      affected_journeys carry other codespaces -- the per-journey operator is derived downstream
--      in predict_calls_bus.sql (from the ref codespace), NOT here.
--   2. FEATURE SNAPSHOT ONLY -- no reopen/impact labelling. The per-call predict reads only the
--      feature columns (creationTime, summary, description, planned, affected_*); the reopen label
--      in disruptions_today.sql is NSB-summary-text-specific and unused here, so it is dropped.
--
-- affected_journeys covers all three ref shapes (same extraction as disruptions_today.sql): the
-- framed datedVehicleJourneyRef, the datedVehicleJourneyRefs array, and the plain vehicleJourneyRefs
-- (ServiceJourney refs for RUT/KOL/etc. -- the 2026-07-03 extraction fix). onset is keyed off
-- creationTime downstream (predict_calls_bus.sql), for verbatim train/serve parity.
CREATE OR REPLACE TABLE `disruptions.disruptions_bus_initial_today` AS
WITH all_versions AS (
  SELECT
    participantRef, situationNumber, version, creationTime,
    COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) AS event_ts,
    severity, planned, summaries, descriptions, affects
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
  -- `OR _PARTITIONTIME IS NULL` reads the streaming buffer too (see last_ingestion_time.sql).
  WHERE (DATE(_PARTITIONTIME) = CURRENT_DATE() OR _PARTITIONTIME IS NULL)
    AND participantRef IN ('GOA', 'GCO', 'MOR', 'NOR', 'TRO', 'FIN', 'RUT')
),
-- First version = feature snapshot AND clock start (onset).
first_version AS (
  SELECT *
  FROM all_versions
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY participantRef, situationNumber
    ORDER BY event_ts ASC, version ASC, creationTime ASC
  ) = 1
)
SELECT
  fv.participantRef,
  fv.situationNumber,
  fv.creationTime,
  fv.event_ts AS onset_ts,
  fv.planned,
  (SELECT x.value FROM UNNEST(fv.summaries) AS x
   WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
   LIMIT 1) AS summary,
  (SELECT x.value FROM UNNEST(fv.descriptions) AS x
   WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
   LIMIT 1) AS description,
  -- The operator's own English, served only -- see the same columns in disruptions_today.sql.
  (SELECT x.value FROM UNNEST(fv.summaries) AS x
   WHERE x.value IS NOT NULL AND UPPER(x.language) = 'EN'
   LIMIT 1) AS summary_en,
  (SELECT x.value FROM UNNEST(fv.descriptions) AS x
   WHERE x.value IS NOT NULL AND UPPER(x.language) = 'EN'
   LIMIT 1) AS description_en,
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
  (
    SELECT ARRAY_AGG(line_ref ORDER BY line_ref)
    FROM (
      SELECT journeys.lineRef AS line_ref
      FROM UNNEST(fv.affects.vehicleJourneys) AS journeys WHERE journeys.lineRef IS NOT NULL
      UNION DISTINCT
      SELECT al.lineRef
      FROM UNNEST(fv.affects.networks) AS n, UNNEST(n.affectedLines) AS al WHERE al.lineRef IS NOT NULL
    )
  ) AS affected_lines
FROM first_version AS fv
WHERE (fv.severity IS NULL OR fv.severity != 'NO_IMPACT');
