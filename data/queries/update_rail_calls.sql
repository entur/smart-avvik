-- Cheap incremental refresh of disruptions.rail_calls (the rolling 2-year table) WITHOUT the
-- ~$20 full ET re-scan that create_rail_calls.sql does.
--
-- The fresh-slice logic is identical to update_rail_calls_new.sql (last snapshot per Vy/NSB
-- journey from the last 2 ET partitions; Quay -> parent StopPlace via the National Stop
-- Register). The difference is what we do with it: rail_calls holds 2 YEARS of history, so we
-- must NOT CREATE OR REPLACE it (that would drop everything but the last 2 days). Instead we
-- splice the fresh slice in:
--   1. stage the fresh last-2-day slice into a temp table,
--   2. delete the stale rows of those journeys (jkey identifies a journey + service date, so
--      a refreshed journey's old calls are fully removed) AND any rows that have aged out of
--      the 2-year window,
--   3. insert the fresh rows.
-- This scans only the last 2 ET partitions (tiny) plus the ~0.7 GB rail_calls table, so it is
-- a few cents -- run it as often as you want fresh actuals.
--
-- LITERAL INTERVAL 2 DAY (not a variable): a literal keeps the _PARTITIONTIME filter
-- prunable, so the ET read stays tiny. Using a DECLARE'd variable here can defeat pruning and
-- turn this back into a ~$20 full-table scan. To widen the window, edit the literal.
--
-- CADENCE: the 2-day window must overlap between runs, so run at least daily. The newest ET
-- day is only as complete as ET has ingested so far (in-progress windows give partial
-- actuals); re-running is idempotent (a journey's rows are deleted and re-inserted from its
-- freshest in-window snapshot), so re-run as ET fills in.

CREATE TEMP TABLE fresh AS
WITH last_snap AS (
  SELECT
    REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
    lineRef, cancellation AS journey_cancelled, recordedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY) AND CURRENT_DATE()
    AND lineRef LIKE 'VYG:%'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY datedVehicleJourneyRef ORDER BY recordedAtTime DESC) = 1
),
calls AS (
  SELECT
    s.jkey, s.lineRef,
    rc.stopPointRef AS quay,
    rc.order AS stop_order,
    SAFE.TIMESTAMP(rc.aimedDepartureTime) AS aimed_dep,
    DATE(SAFE.TIMESTAMP(rc.aimedDepartureTime)) AS call_date,
    (rc.cancellation OR s.journey_cancelled) AS call_cancelled,
    TIMESTAMP_DIFF(SAFE.TIMESTAMP(rc.actualDepartureTime),
                   SAFE.TIMESTAMP(rc.aimedDepartureTime), MINUTE) AS dep_delay_min
  FROM last_snap AS s, UNNEST(s.recordedCalls) AS rc
  WHERE SAFE.TIMESTAMP(rc.aimedDepartureTime) IS NOT NULL
    AND (rc.cancellation OR s.journey_cancelled OR rc.actualDepartureTime IS NOT NULL)
)
SELECT
  c.jkey, c.lineRef, c.quay,
  q.stopPlaceRef AS stop_place,
  q.name         AS stop_name,
  c.stop_order, c.aimed_dep, c.call_date, c.call_cancelled, c.dep_delay_min
FROM calls AS c
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
  ON c.quay = q.id;

-- Splice the fresh slice in atomically: drop stale versions of the refreshed journeys (and
-- anything older than the 2-year window), then insert the fresh rows.
BEGIN TRANSACTION;

DELETE FROM `ent-kixavvik-dev.disruptions.rail_calls`
WHERE jkey IN (SELECT jkey FROM fresh)
   OR call_date < DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR);

INSERT INTO `ent-kixavvik-dev.disruptions.rail_calls`
  (jkey, lineRef, quay, stop_place, stop_name, stop_order, aimed_dep, call_date,
   call_cancelled, dep_delay_min)
SELECT
  jkey, lineRef, quay, stop_place, stop_name, stop_order, aimed_dep, call_date,
  call_cancelled, dep_delay_min
FROM fresh;

COMMIT TRANSACTION;
