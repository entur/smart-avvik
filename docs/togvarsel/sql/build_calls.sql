CREATE OR REPLACE TABLE `ent-kixavvik-dev.togvarsel.calls`
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
WHERE e.operatingDate BETWEEN '2024-08-01' AND '2026-08-25' AND e.dataSource='VYG' AND e.vehicleMode='rail'
