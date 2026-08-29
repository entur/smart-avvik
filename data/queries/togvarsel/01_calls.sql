-- disruptions.togvarsel_calls: every Vy/NSB rail call over the training window, from the
-- daily-batch ET snapshot table (realtime_siri_et_last_recorded = the most up-to-date state of
-- each journey as finally recorded). One row per (journey, stop) with aimed/actual times, delay
-- minutes and cancellation flags. This is the base of the whole togvarsel training chain
-- (docs/togvarsel/02-findings-2026-08-27.md); the serving-time sibling computes the same shapes
-- from the LIVE feed (disruptions/api/togvarsel_features_today.sql) — keep their semantics in
-- sync (delay = COALESCE(arr, dep); cancelled = journey OR stop level; extra journeys excluded
-- downstream, not here).
--
-- The window starts 2024-08-01, but last_recorded only carries VYG rail from 2025-04-10 — the
-- earlier months contribute nothing and cost nothing extra (partition prune). Full rebuild:
-- last_recorded restates history, and the scan is the same either way.
CREATE OR REPLACE TABLE `disruptions.togvarsel_calls`
PARTITION BY operating_date
CLUSTER BY line, stop_place AS
SELECT
  e.operatingDate AS operating_date,
  REGEXP_REPLACE(e.lineRef, r'^VYG:Line:', '') AS line,
  e.datedServiceJourneyId AS dsj,
  e.serviceJourneyId AS sj,
  e.directionRef AS direction,
  e.sequenceNr AS seq,
  e.stopPointRef AS quay,
  q.stopPlaceRef AS stop_place,
  e.stopPointName AS stop_name_et,
  q.name AS stop_name_nsr,
  e.extraJourney AS extra_journey,
  e.journeyCancellation AS journey_cancelled,
  e.stopCancellation AS stop_cancelled,
  (IFNULL(e.journeyCancellation,FALSE) OR IFNULL(e.stopCancellation,FALSE)) AS cancelled,
  e.extraCall AS extra_call,
  e.estimated,
  e.aimedArrivalTime AS aimed_arr, e.arrivalTime AS actual_arr,
  e.aimedDepartureTime AS aimed_dep, e.departureTime AS actual_dep,
  e.recordedAtTime AS recorded_at,
  COALESCE(e.aimedDepartureTime, e.aimedArrivalTime) AS aimed_ts,
  DATETIME(COALESCE(e.aimedDepartureTime, e.aimedArrivalTime), 'Europe/Oslo') AS aimed_local,
  TIMESTAMP_DIFF(e.arrivalTime, e.aimedArrivalTime, SECOND)/60.0 AS arr_delay_min,
  TIMESTAMP_DIFF(e.departureTime, e.aimedDepartureTime, SECOND)/60.0 AS dep_delay_min
FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded` e
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` q ON e.stopPointRef = q.id
WHERE e.operatingDate BETWEEN '2024-08-01' AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  AND e.dataSource='VYG' AND e.vehicleMode='rail'
