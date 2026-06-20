-- Reusable realized-outcome table: every Vy/NSB rail call whose Oslo-local
-- aimed time falls in the FIXED window 2024-06-01 .. 2026-05-31, taken from the
-- last ET snapshot per dated journey, with the ET Quay mapped to its parent
-- StopPlace via the National Stop Register so it can join to SX affected_stops.
--
-- A fixed window (not CURRENT_DATE) is used so the table is reproducible.
--
-- What a "call" is here -- realized outcomes only:
--   * every call in recordedCalls (operated stops, plus any recorded as
--     cancelled), AND
--   * every call in estimatedCalls that is cancelled (rc.cancellation or the
--     whole journey cancelled). Cancellations live mostly in estimatedCalls
--     -- a cancelled stop never becomes "recorded" -- so reading recordedCalls
--     alone drops the large majority of them.
-- Non-cancelled estimatedCalls are NOT included: they are unrecorded
-- predictions (the producer often stops emitting ET before the journey ends),
-- not realized outcomes.
--
-- Terminal/arrival-only stops are kept (they have no departure), and both
-- arrival and departure delay are recorded. call_date is the Europe/Oslo date
-- of the call so it matches the service date and date-keyed SX joins.
--
-- This is the one-time ET scan over recordedCalls + estimatedCalls leaves
-- (~2y of VYG partitions). Build once, then derive trip-outcome tables from it
-- cheaply -- no further ET scans needed.
--
-- Partition scan is widened to 2026-06-02 to catch the last snapshots of
-- journeys that finish after midnight; the output is trimmed back to the window
-- by Oslo-local call_date. Bounds are literals (not scripting variables) so
-- BigQuery prunes partitions.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.rail_calls`
PARTITION BY call_date
CLUSTER BY lineRef, stop_place
AS
WITH last_snap AS (
  SELECT
    REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
    lineRef,
    cancellation AS journey_cancelled,
    recordedCalls,
    estimatedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  WHERE _PARTITIONTIME >= TIMESTAMP('2024-06-01')
    AND _PARTITIONTIME <  TIMESTAMP('2026-06-03')   -- 2026-06-02 inclusive
    AND lineRef LIKE 'VYG:%'
    AND datedVehicleJourneyRef IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY datedVehicleJourneyRef
    ORDER BY SAFE.TIMESTAMP(recordedAtTime) DESC
  ) = 1
),
calls AS (
  -- Stops actually present in recordedCalls (operated, plus recorded-cancelled)
  SELECT
    s.jkey, s.lineRef,
    rc.stopPointRef AS quay,
    rc.order        AS stop_order,
    'recorded'      AS call_source,
    (rc.cancellation OR s.journey_cancelled) AS call_cancelled,
    SAFE.TIMESTAMP(rc.aimedDepartureTime)  AS aimed_dep,
    SAFE.TIMESTAMP(rc.aimedArrivalTime)    AS aimed_arr,
    SAFE.TIMESTAMP(rc.actualDepartureTime) AS actual_dep,
    SAFE.TIMESTAMP(rc.actualArrivalTime)   AS actual_arr
  FROM last_snap AS s, UNNEST(s.recordedCalls) AS rc

  UNION ALL

  -- Cancelled stops that only ever appeared as estimated (never recorded)
  SELECT
    s.jkey, s.lineRef,
    ec.stopPointRef AS quay,
    ec.order        AS stop_order,
    'estimated'     AS call_source,
    TRUE            AS call_cancelled,
    SAFE.TIMESTAMP(ec.aimedDepartureTime)  AS aimed_dep,
    SAFE.TIMESTAMP(ec.aimedArrivalTime)    AS aimed_arr,
    CAST(NULL AS TIMESTAMP)                AS actual_dep,
    CAST(NULL AS TIMESTAMP)                AS actual_arr
  FROM last_snap AS s, UNNEST(s.estimatedCalls) AS ec
  WHERE (ec.cancellation OR s.journey_cancelled)
),
typed AS (
  SELECT
    jkey, lineRef, quay, stop_order, call_source, call_cancelled,
    aimed_dep, aimed_arr, actual_dep, actual_arr,
    DATE(COALESCE(aimed_dep, aimed_arr), 'Europe/Oslo')        AS call_date,
    TIMESTAMP_DIFF(actual_dep, aimed_dep, MINUTE)              AS dep_delay_min,
    TIMESTAMP_DIFF(actual_arr, aimed_arr, MINUTE)              AS arr_delay_min
  FROM calls
  WHERE COALESCE(aimed_dep, aimed_arr) IS NOT NULL
    -- realized outcomes only: cancelled, or operated (has an actual time)
    AND (call_cancelled OR actual_dep IS NOT NULL OR actual_arr IS NOT NULL)
)
SELECT
  t.jkey, t.lineRef, t.quay,
  q.stopPlaceRef AS stop_place,
  q.name         AS stop_name,
  t.stop_order,
  t.aimed_dep,
  t.call_date,
  t.call_cancelled,
  t.dep_delay_min,
  -- new in the product-ready rebuild:
  t.call_source,
  t.aimed_arr,
  t.actual_dep,
  t.actual_arr,
  t.arr_delay_min
FROM typed AS t
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
  ON t.quay = q.id
WHERE t.call_date BETWEEN DATE '2024-06-01' AND DATE '2026-05-31';
