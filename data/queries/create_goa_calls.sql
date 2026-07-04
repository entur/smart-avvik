-- Realized-outcome call table for Go-Ahead (GOA) rail, schema-identical to
-- disruptions.rail_calls so the two can be UNION ALLed into a pooled training build
-- (create_training_calls_pooled.sql).
--
-- SOURCE: ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded --
-- the all-operator per-call "final state" table (one row per call, daily refresh),
-- NOT the anshar ET snapshot stream. A per-operator slice costs ~cents (clustered by
-- dataSource) vs ~$63 for a 2-year anshar scan; this is the training-source of choice
-- for every operator beyond the existing VYG-based rail_calls (docs/operator-census).
--
-- WINDOW: 2025-02-25 .. 2026-06-20 (Oslo call_date). GOA only exists in last_recorded
-- as rail DatedServiceJourney refs from ~Feb 2025 (partial) / March 2025 (full);
-- before that its rows are replacement-bus ServiceJourney refs only. End matches
-- rail_calls' current extent so pooled onsets share one right-censoring boundary.
-- KNOWN GAP: Jan 2026 carries normal journey counts but ~half the usual call rows
-- (~6.7 calls/journey vs ~14) -- a producer gap month; affected-call labels in Jan
-- 2026 GOA situations under-count.
--
-- REALIZED OUTCOMES ONLY, mirroring rail_calls' recordedCalls + cancelled-estimated
-- rule: keep a call iff it is cancelled (journeyCancellation OR stopCancellation) or
-- it has NON-estimated actual times. Rows with estimated = TRUE and no cancellation
-- are unrealized predictions (the producer stopped emitting before the journey ended)
-- and are dropped; estimated "actual" times on cancelled rows are nulled -- they are
-- predictions, not operations (matches rail_calls' estimated branch).
--
-- vehicleMode = 'rail' excludes GOA replacement buses (extraJourney bus rows).
-- ~0.02% duplicate (journey, date, sequenceNr) keys -> dedup on latest recordedAtTime.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.goa_calls`
PARTITION BY call_date
CLUSTER BY lineRef, stop_place
AS
WITH src AS (
  SELECT
    REGEXP_REPLACE(serviceJourneyId, r'^[A-Za-z]+:', '') AS jkey,
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
  WHERE dataSource = 'GOA'
    AND vehicleMode = 'rail'
    AND operatingDate BETWEEN DATE '2025-02-24' AND DATE '2026-06-21'
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
    -- realized outcomes only: cancelled, or operated (has a real actual time)
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
WHERE t.call_date BETWEEN DATE '2025-02-25' AND DATE '2026-06-20';
