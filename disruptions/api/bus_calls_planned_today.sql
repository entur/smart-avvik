-- PLANNED-timetable call source for live per-call BUS/GOA serving: the bus/local-transport sibling
-- of rail_calls_planned_today.sql. Every UPCOMING call of every journey of the served bus/GOA
-- operators currently in the ET feed, with scheduled (aimed) times, stop and order, plus the
-- realized dep_delay_min on operated stops (the live-observable state the propagation features read).
--
-- THREE DELTAS vs rail_calls_planned_today.sql:
--   1. JOURNEY REF COLUMN differs by mode (verified on anshar ET): rail (VYG/GOA) carries the ref
--      in the TOP-LEVEL datedVehicleJourneyRef (<CS>:DatedServiceJourney:...); bus carries it in
--      framedVehicleJourneyRef.datedVehicleJourneyRef (<CS>:ServiceJourney:..., NOT date-scoped).
--      So the ref = COALESCE(datedVehicleJourneyRef, framedVehicleJourneyRef.datedVehicleJourneyRef).
--   2. OPERATOR is emitted (= the ref codespace prefix), because bus ServiceJourney jkeys collide
--      across operators (not date-scoped) -- predict_calls_bus.sql joins on (operator, jkey) and the
--      cache keys on (operator, jkey). operator MUST match the SX affected-journey ref codespace.
--   3. OPERATOR ALLOWLIST: only the operators the _pooled_all family was trained on AND that appear
--      live in anshar ET. VYG is excluded (stays on the NSB/prod rail path); high-volume but
--      UNTRAINED codespaces (AKT/BRA/INN/VKT/TEL/NBU/AVI/FLT/FLI/BOR) are excluded so they are never
--      scored by a head that never saw them. SOF/GCO are kept in the list but do not appear as live
--      ET journey-ref codespaces today (GCO fans across KOL/SOF/NOR/SKY on the SX side).
--
-- BOARDING / ALIGHTING: same two columns and the same fail-open reading as the rail source (see
-- its header for the F4 case that motivated them and for why a NULL must mean "not stated"). The
-- rule is duplicated rather than shared because these two files already duplicate everything else
-- about a call; keep them identical. Measured on 2026-09-01, the served bus operators mark between
-- 0.0 % and 6.3 % of their mid-journey calls with a prohibition -- MOR highest, most of them 0 --
-- so nothing here is a mass exclusion.
--
-- STOP NAMES: NSR quays give stopPlaceRef but a NULL name; GTFS gtfs_last_recorded_ent_v1 supplies
-- the StopPlace name (by parent_station) for ALL agencies here (no VYG filter), COALESCE'd with the
-- quay name. CURRENT_DATE() partition prune keeps the ET scan small; re-run on a TTL from main.py.
CREATE OR REPLACE TABLE `disruptions.bus_calls_planned_today` AS
WITH last_snap AS (
  SELECT
    REGEXP_REPLACE(COALESCE(datedVehicleJourneyRef, framedVehicleJourneyRef.datedVehicleJourneyRef),
                   r'^[A-Za-z]+:', '') AS jkey,
    REGEXP_EXTRACT(COALESCE(datedVehicleJourneyRef, framedVehicleJourneyRef.datedVehicleJourneyRef),
                   r'^([A-Za-z]+):') AS operator,
    lineRef,
    cancellation AS journey_cancelled,
    recordedCalls,
    estimatedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  -- `OR _PARTITIONTIME IS NULL` reads the streaming buffer too (see last_ingestion_time.sql).
  WHERE (DATE(_PARTITIONTIME) = CURRENT_DATE() OR _PARTITIONTIME IS NULL)
    AND COALESCE(datedVehicleJourneyRef, framedVehicleJourneyRef.datedVehicleJourneyRef) IS NOT NULL
    AND REGEXP_EXTRACT(COALESCE(datedVehicleJourneyRef, framedVehicleJourneyRef.datedVehicleJourneyRef),
                       r'^([A-Za-z]+):')
        IN ('GOA','RUT','KOL','SOF','NOR','SKY','ATB','OST','FIN','MOR','TRO','GCO')
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY COALESCE(datedVehicleJourneyRef, framedVehicleJourneyRef.datedVehicleJourneyRef)
    ORDER BY SAFE.TIMESTAMP(recordedAtTime) DESC) = 1
),
calls AS (
  -- recorded (operated / past) stops -- planned times + the REALIZED departure delay
  SELECT
    s.operator, s.jkey, s.lineRef, rc.stopPointRef AS quay, rc.order AS stop_order,
    'recorded' AS call_source,
    (rc.cancellation OR s.journey_cancelled) AS call_cancelled,
    (IFNULL(rc.departureBoardingActivity, 'BOARDING')  NOT IN ('NO_BOARDING',  'PASS_THRU')) AS boarding_allowed,
    (IFNULL(rc.arrivalBoardingActivity,   'ALIGHTING') NOT IN ('NO_ALIGHTING', 'PASS_THRU')) AS alighting_allowed,
    SAFE.TIMESTAMP(rc.aimedDepartureTime) AS aimed_dep,
    SAFE.TIMESTAMP(rc.aimedArrivalTime)   AS aimed_arr,
    TIMESTAMP_DIFF(SAFE.TIMESTAMP(rc.actualDepartureTime),
                   SAFE.TIMESTAMP(rc.aimedDepartureTime), MINUTE) AS dep_delay_min
  FROM last_snap AS s, UNNEST(s.recordedCalls) AS rc
  UNION ALL
  -- estimated (upcoming / future) stops -- ALL of them (the forward schedule), planned times
  SELECT
    s.operator, s.jkey, s.lineRef, ec.stopPointRef, ec.order, 'estimated',
    (ec.cancellation OR s.journey_cancelled),
    (IFNULL(ec.departureBoardingActivity, 'BOARDING')  NOT IN ('NO_BOARDING',  'PASS_THRU')),
    (IFNULL(ec.arrivalBoardingActivity,   'ALIGHTING') NOT IN ('NO_ALIGHTING', 'PASS_THRU')),
    SAFE.TIMESTAMP(ec.aimedDepartureTime),
    SAFE.TIMESTAMP(ec.aimedArrivalTime),
    CAST(NULL AS INT64)
  FROM last_snap AS s, UNNEST(s.estimatedCalls) AS ec
),
dedup AS (
  -- a stop can appear in both lists across the journey's life; keep one (prefer recorded)
  SELECT *
  FROM calls
  WHERE COALESCE(aimed_dep, aimed_arr) IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY operator, jkey, stop_order ORDER BY (call_source = 'recorded') DESC) = 1
),
gtfs_names AS (
  SELECT parent_station AS stop_place, ANY_VALUE(stop_name) AS stop_name
  FROM `ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1`
  WHERE operating_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
                           AND DATE_ADD(CURRENT_DATE(), INTERVAL 2 DAY)
    AND parent_station IS NOT NULL
  GROUP BY parent_station
)
SELECT
  d.operator, d.jkey, d.lineRef, d.quay,
  q.stopPlaceRef            AS stop_place,
  COALESCE(n.stop_name, q.name) AS stop_name,
  d.stop_order, d.aimed_dep, d.aimed_arr, d.dep_delay_min,
  DATE(COALESCE(d.aimed_dep, d.aimed_arr), 'Europe/Oslo') AS call_date,
  d.call_cancelled, d.call_source, d.boarding_allowed, d.alighting_allowed,
  -- When this state was read. The predict files measure "the last hour" and the ask horizon from
  -- it rather than from CURRENT_TIMESTAMP(), because this table is rebuilt on a TTL and is up to
  -- that old when they run (see `asof` in predict_calls.sql).
  CURRENT_TIMESTAMP() AS built_at
FROM dedup AS d
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
  ON d.quay = q.id
LEFT JOIN gtfs_names AS n
  ON q.stopPlaceRef = n.stop_place;
