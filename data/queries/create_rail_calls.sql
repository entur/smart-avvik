-- Reusable realized-outcome table: every Vy/NSB rail call over the last 2 years
-- (last ET snapshot per journey), with the ET Quay mapped to its parent
-- StopPlace via the National Stop Register so it can join to SX affected_stops.
--
-- This is the one-time ~$20 ET scan (reads recordedCalls over 2y). Build once,
-- then derive trip-outcome tables from it cheaply -- no further ET scans needed.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.rail_calls` AS
WITH last_snap AS (
  SELECT
    REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
    lineRef, cancellation AS journey_cancelled, recordedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR) AND CURRENT_DATE()
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
