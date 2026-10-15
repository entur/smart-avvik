-- Realized-outcome call table for SJ Norge (SJN) rail, schema-identical to
-- disruptions.rail_calls -- the SJN sibling of create_goa_calls.sql (KIX-39).
--
-- SOURCE: last_recorded, like goa_calls. SJN's trains are NOT under dataSource 'SJN':
-- Bane NOR relays them, so they sit under dataSource = 'BNR' with lineRef 'SJN:Line:*'
-- and the legacy ref '<train number>:<date>' in serviceJourneyId (e.g. '471:2025-01-04').
-- dataSource 'SJN' holds only SJN's replacement buses (and died 2026-05-13), which is
-- why the 2026-07-03 operator census concluded SJN had no rail history. It has ~45k
-- calls / ~2,500 trains a month from 2022 on. The live anshar ET carries the same ref in
-- the top-level datedVehicleJourneyRef, so jkey here == jkey in rail_calls_planned_today.
--
-- REALIZED OUTCOMES ONLY, the goa_calls rule: cancelled, or a NON-estimated actual time.
-- Estimated non-cancelled rows are unrealized predictions (last_recorded fills estimated
-- tails with EXPECTED times) and are dropped.
--
-- WINDOW: training_calls' window, 2024-06-01 .. 2026-10-11 (Oslo call_date).

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.sjn_calls`
PARTITION BY call_date
CLUSTER BY lineRef, stop_place
AS
WITH src AS (
  SELECT
    serviceJourneyId AS jkey,
    lineRef,
    stopPointRef AS quay,
    sequenceNr   AS stop_order,
    IF(COALESCE(estimated, FALSE), 'estimated', 'recorded') AS call_source,
    (COALESCE(journeyCancellation, FALSE) OR COALESCE(stopCancellation, FALSE)) AS call_cancelled,
    aimedDepartureTime AS aimed_dep,
    aimedArrivalTime   AS aimed_arr,
    IF(COALESCE(estimated, FALSE), NULL, departureTime) AS actual_dep,
    IF(COALESCE(estimated, FALSE), NULL, arrivalTime)   AS actual_arr
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded`
  WHERE dataSource = 'BNR'
    AND lineRef LIKE 'SJN:%'
    AND vehicleMode = 'rail'
    AND operatingDate BETWEEN DATE '2024-05-31' AND DATE '2026-10-12'
    AND serviceJourneyId IS NOT NULL
    AND stopPointRef IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY serviceJourneyId, operatingDate, sequenceNr
    ORDER BY recordedAtTime DESC
  ) = 1
),
typed AS (
  SELECT
    *,
    DATE(COALESCE(aimed_dep, aimed_arr), 'Europe/Oslo')  AS call_date,
    TIMESTAMP_DIFF(actual_dep, aimed_dep, MINUTE)        AS dep_delay_min,
    TIMESTAMP_DIFF(actual_arr, aimed_arr, MINUTE)        AS arr_delay_min
  FROM src
  WHERE COALESCE(aimed_dep, aimed_arr) IS NOT NULL
    AND (call_cancelled OR actual_dep IS NOT NULL OR actual_arr IS NOT NULL)
)
SELECT
  t.jkey, t.lineRef, t.quay,
  q.stopPlaceRef AS stop_place,
  q.name         AS stop_name,
  LAG(q.stopPlaceRef) OVER (
    PARTITION BY t.jkey ORDER BY t.stop_order
  ) AS prev_stop_place,
  LEAD(q.stopPlaceRef) OVER (
    PARTITION BY t.jkey ORDER BY t.stop_order
  ) AS next_stop_place,
  t.stop_order,
  t.aimed_dep,
  t.call_date,
  t.call_cancelled,
  t.dep_delay_min,
  t.call_source,
  t.aimed_arr,
  t.actual_dep,
  t.actual_arr,
  t.arr_delay_min
FROM typed AS t
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
  ON t.quay = q.id
WHERE t.call_date BETWEEN DATE '2024-06-01' AND DATE '2026-10-11';
