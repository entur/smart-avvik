WITH v AS (
  SELECT monitoredVehicleJourney.lineRef line, monitoredVehicleJourney.vehicleRef vref,
    monitoredVehicleJourney.vehicleJourneyRef vjref,
    ARRAY_TO_STRING(monitoredVehicleJourney.vehicleModes, ',') mode
  FROM `ent-anshar-prd.realtime.siri_realtime_vm_production`
  WHERE DATE(_PARTITIONTIME) = '2026-08-25' AND monitoredVehicleJourney.dataSource = 'VYG'
),
per_v AS (SELECT vref, mode, COUNT(*) n_msg, COUNT(DISTINCT vjref) n_vj, COUNT(DISTINCT line) n_lines FROM v GROUP BY 1,2)
SELECT mode, COUNT(*) n_vref, SUM(n_msg) n_msg, ROUND(AVG(n_vj),2) avg_vj_per_vref, COUNTIF(n_vj>=2) vref_2plus_vj, COUNTIF(n_lines>=2) vref_2plus_lines,
  COUNTIF(REGEXP_CONTAINS(vref, r'^\d+-\d{4}-\d{2}-\d{2}$')) vref_trainno_date_pattern, MIN(vref) min_vref, MAX(vref) max_vref
FROM per_v GROUP BY 1 ORDER BY 2 DESC
