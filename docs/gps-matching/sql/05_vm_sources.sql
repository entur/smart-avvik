-- docs/gps-matching 2026-09-02. Dry run 3.2 GB (one VM partition). Which data sources and modes publish vehicle positions.

-- SIRI-VM one day: which data sources / modes publish positions; is rail in it?
SELECT monitoredVehicleJourney.dataSource ds,
  ARRAY_TO_STRING(monitoredVehicleJourney.vehicleModes, ',') modes,
  COUNT(*) n_msgs,
  COUNT(DISTINCT monitoredVehicleJourney.vehicleRef) n_vehicles,
  COUNTIF(monitoredVehicleJourney.vehicleLocation.latitude IS NOT NULL) n_with_pos,
  COUNT(DISTINCT monitoredVehicleJourney.framedVehicleJourneyRef.datedVehicleJourneyRef) n_journeys
FROM `ent-anshar-prd.realtime.siri_realtime_vm_production`
WHERE _PARTITIONTIME = TIMESTAMP('2026-09-01')
GROUP BY 1,2 ORDER BY n_msgs DESC
