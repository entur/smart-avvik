-- Realized-outcome call table for the bus/local-transport codespaces, rail_calls-shaped
-- plus (operator, operating_date) leading columns, so it UNION ALLs with rail_calls /
-- goa_calls into the all-operator pooled training build
-- (create_training_calls_pooled_all.sql).
--
-- SOURCE: ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded,
-- sliced by dataSource (clustering key) for the codespaces that back the bus-side SX
-- publishers (docs/operator-census): GCO's per-departure notices fan out across county
-- codespaces (KOL/SOF/NOR/SKY/ATB/OST/FIN/MOR/GCO), MOR/NOR/TRO/FIN/RUT publish 1:1.
-- BRA/BFO/TEL/VKT are excluded: <1k SX refs combined, and BRA alone is 24M call rows.
--
-- operator = the serviceJourneyId PREFIX, not dataSource -- refs and delivery channel
-- disagree for a small slice (e.g. GCO:ServiceJourney rows delivered under SOF/KOL), and
-- the SX join keys on the ref's codespace. No vehicleMode filter: most bus producers
-- leave it NULL, and the codespaces are all local transport (bus/tram/metro/ferry --
-- SOF/GCO are the request-stop ferry networks behind the "Bestillingsanløp" notices).
--
-- operating_date is kept because ServiceJourney refs are NOT date-scoped (unlike rail's
-- DatedServiceJourney): the same jkey repeats every operating day, so instance identity
-- is (operator, jkey, operating_date). prev/next stop windows are scoped accordingly.
--
-- WINDOW + REALIZED-OUTCOME RULE: identical to create_goa_calls.sql -- keep a call iff
-- cancelled (either flag) or it has NON-estimated actual times; estimated "actuals" on
-- cancelled rows are nulled. Dedup on latest recordedAtTime per (journey, date, seq).

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.bus_calls`
PARTITION BY call_date
CLUSTER BY operator, lineRef, stop_place
AS
WITH src AS (
  SELECT
    REGEXP_EXTRACT(serviceJourneyId, r'^([A-Za-z]+):') AS operator,
    operatingDate AS operating_date,
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
  WHERE dataSource IN ('KOL','SOF','NOR','SKY','ATB','OST','FIN','MOR','RUT','TRO','GCO')
    AND operatingDate BETWEEN DATE '2025-02-24' AND DATE '2026-06-21'
    AND REGEXP_CONTAINS(serviceJourneyId, r'^[A-Za-z]+:ServiceJourney:')
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
  t.operator, t.operating_date,
  t.jkey, t.lineRef, t.quay,
  q.stopPlaceRef AS stop_place,
  q.name         AS stop_name,
  LAG(q.stopPlaceRef) OVER (
    PARTITION BY t.operator, t.jkey, t.operating_date ORDER BY t.stop_order
  ) AS prev_stop_place,
  LEAD(q.stopPlaceRef) OVER (
    PARTITION BY t.operator, t.jkey, t.operating_date ORDER BY t.stop_order
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
