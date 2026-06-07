-- Recent-window refresh of the ET-derived realized-outcome table, so the actuals in
-- predict_610_duration.sql have data. Same shape and logic as create_rail_calls.sql
-- (last ET snapshot per journey; Quay -> parent StopPlace via the National Stop
-- Register) — only the partition window and the target table differ.
--
-- Scoped to the last 30 days, so it covers disruptions_initial_new (post-training
-- disruptions) AND their onset + 12h impact windows, while overlapping the existing
-- rail_calls_new range. Partition-pruned to ~30 ET partitions => a small scan, NOT
-- the one-time 2-year build in create_rail_calls.sql.
--
-- Bump the 30-day lookback if any incident in disruptions_initial_new is older than
-- 30 days, otherwise its onset+12h window won't be covered and its actuals stay 0/NULL.
-- The newest day is only as complete as ET has ingested so far (in-progress windows
-- give partial actuals).

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.rail_calls_new` AS
WITH last_snap AS (
  SELECT
    REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
    lineRef, cancellation AS journey_cancelled, recordedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND CURRENT_DATE()
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
