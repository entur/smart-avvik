-- Rolling 2-day refresh of disruptions.rail_calls_new: the most up-to-date ET snapshot for every
-- Vy/NSB journey from the last 2 days. Same shape/logic as create_rail_calls_new.sql (last
-- snapshot per journey -> most up-to-date; Quay -> parent StopPlace via the National Stop
-- Register) — only the window differs (2 days here, vs 30 in create_rail_calls_new.sql / 2 years
-- in create_rail_calls.sql).
--
-- CREATE OR REPLACE, not append: appending can't deliver "most up-to-date" for a journey already
-- in the table (a jkey dedup keeps the stale snapshot; no dedup duplicates it). Rebuilding the
-- 2-day slice each run guarantees the freshest snapshot per journey AND drops anything older than
-- 2 days. It costs the same scan as a merge would — both read only the last 2 ET partitions — so
-- there's no incremental-cost reason to do anything fancier. Run it as often as you want fresh
-- actuals.
--
-- The 2-day _PARTITIONTIME prune keeps this a tiny scan; no EXECUTE IMMEDIATE needed, since
-- CURRENT_DATE() in the partition filter prunes (same convention as create_rail_calls(_new).sql).
-- "Last 2 days" is by ET ingestion partition, so it captures journeys whose latest snapshot
-- landed in the last 2 days. The newest day is only as complete as ET has ingested so far
-- (in-progress windows give partial actuals); re-run as ET fills in.

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.rail_calls_new` AS
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
