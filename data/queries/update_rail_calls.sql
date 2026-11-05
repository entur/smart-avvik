-- Cheap incremental refresh of disruptions.rail_calls WITHOUT the ~$63 full ET re-scan that
-- create_rail_calls.sql does.
--
-- WINDOW POLICY: grow from a fixed start. rail_calls is seeded by create_rail_calls.sql over
-- the fixed window 2024-06-01 .. 2026-05-31; this updater appends fresh recent days on top of
-- it and refreshes the journeys in the fresh slice. It does NOT age out old rows -- the lower
-- bound stays anchored at 2024-06-01 and the table grows open-ended at the top. (The fresh
-- slice is always the last few ET days, well above the start, so no lower-bound guard is
-- needed and nothing below the anchor is ever added.)
--
-- The fresh-slice logic MUST mirror create_rail_calls.sql so appended days don't regress:
--   * recordedCalls (operated + recorded-cancelled) UNION cancelled estimatedCalls -- most
--     cancellations live in estimatedCalls and would otherwise be dropped,
--   * terminal arrival-only stops kept; both arrival and departure delay recorded,
--   * call_date is the Europe/Oslo date (not UTC),
--   * all 15 columns populated (same names/order as the table).
-- Keep this slice in sync with create_rail_calls.sql.
--
-- Splice:
--   1. stage the fresh last-few-day slice into a temp table,
--   2. delete the stale rows of those journeys (jkey identifies a journey + service date, so a
--      refreshed journey's old calls are fully removed),
--   3. insert the fresh rows.
-- Scans only the last few ET partitions (recordedCalls + estimatedCalls leaves) plus the
-- rail_calls jkey column -- a few tens of cents. Run it as often as you want fresh actuals.
--
-- LITERAL INTERVAL 2 DAY (not a variable): a literal keeps the _PARTITIONTIME filter prunable,
-- so the ET read stays tiny. A DECLARE'd variable here can defeat pruning and turn this back
-- into a full-table scan. To widen the window, edit the literal.
--
-- CADENCE: the window must overlap between runs, so run at least daily. The newest ET day is
-- only as complete as ET has ingested so far (in-progress windows give partial actuals);
-- re-running is idempotent (a journey's rows are deleted and re-inserted from its freshest
-- in-window snapshot), so re-run as ET fills in.

CREATE TEMP TABLE fresh AS
WITH last_snap AS (
  SELECT
    REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
    lineRef,
    cancellation AS journey_cancelled,
    recordedCalls,
    estimatedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY) AND CURRENT_DATE()
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
  t.call_source,
  t.aimed_arr,
  t.actual_dep,
  t.actual_arr,
  t.arr_delay_min
FROM typed AS t
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
  ON t.quay = q.id;

-- Splice the fresh slice in atomically: drop stale versions of the refreshed journeys, then
-- insert the fresh rows. No age-out -- the table grows from its fixed 2024-06-01 start.
BEGIN TRANSACTION;

DELETE FROM `ent-smartavvik-dev.disruptions.rail_calls`
WHERE jkey IN (SELECT jkey FROM fresh);

INSERT INTO `ent-smartavvik-dev.disruptions.rail_calls`
  (jkey, lineRef, quay, stop_place, stop_name, stop_order, aimed_dep, call_date,
   call_cancelled, dep_delay_min, call_source, aimed_arr, actual_dep, actual_arr, arr_delay_min)
SELECT
  jkey, lineRef, quay, stop_place, stop_name, stop_order, aimed_dep, call_date,
  call_cancelled, dep_delay_min, call_source, aimed_arr, actual_dep, actual_arr, arr_delay_min
FROM fresh;

COMMIT TRANSACTION;
