-- docs/gps-matching 2026-09-02. Dry run 8.6 GB (one VM partition). Rail only: update cadence, journey linkage, fields carried.

-- rail in SIRI-VM, one day: update cadence, journey linkage, what a record carries
WITH v AS (
  SELECT monitoredVehicleJourney.dataSource ds, monitoredVehicleJourney.vehicleRef vref,
    SAFE.TIMESTAMP(recordedAtTime) rec_at, SAFE.TIMESTAMP(monitoredVehicleJourney.locationRecordedAtTime) loc_at,
    monitoredVehicleJourney.lineRef lineRef, monitoredVehicleJourney.vehicleJourneyRef vjr,
    monitoredVehicleJourney.framedVehicleJourneyRef.datedVehicleJourneyRef dvj,
    monitoredVehicleJourney.monitoredCall.stopPointRef mc_stop,
    monitoredVehicleJourney.vehicleLocation.latitude lat, monitoredVehicleJourney.vehicleLocation.longitude lon,
    monitoredVehicleJourney.velocity velocity, monitoredVehicleJourney.bearing bearing, monitoredVehicleJourney.delay delay,
    monitoredVehicleJourney.vehicleStatus vstatus
  FROM `ent-anshar-prd.realtime.siri_realtime_vm_production`
  WHERE _PARTITIONTIME = TIMESTAMP('2026-09-01')
    AND (monitoredVehicleJourney.dataSource IN ('VYG','FLT','GOA','SJN') AND ('RAIL' IN UNNEST(monitoredVehicleJourney.vehicleModes) OR ARRAY_LENGTH(monitoredVehicleJourney.vehicleModes) = 0))
),
g AS (
  SELECT *, TIMESTAMP_DIFF(rec_at, LAG(rec_at) OVER (PARTITION BY ds, vref ORDER BY rec_at), SECOND) gap_s FROM v
)
SELECT ds, COUNT(*) n, COUNT(DISTINCT vref) n_vehicles,
  ROUND(COUNTIF(lineRef IS NOT NULL)/COUNT(*),3) f_line,
  ROUND(COUNTIF(vjr IS NOT NULL OR dvj IS NOT NULL)/COUNT(*),3) f_journey_ref,
  ROUND(COUNTIF(mc_stop IS NOT NULL)/COUNT(*),3) f_monitored_call,
  ROUND(COUNTIF(velocity IS NOT NULL)/COUNT(*),3) f_velocity,
  ROUND(COUNTIF(delay IS NOT NULL)/COUNT(*),3) f_delay,
  APPROX_QUANTILES(gap_s, 100)[OFFSET(50)] p50_gap_s,
  APPROX_QUANTILES(gap_s, 100)[OFFSET(90)] p90_gap_s,
  APPROX_QUANTILES(TIMESTAMP_DIFF(rec_at, loc_at, SECOND), 100)[OFFSET(50)] p50_loc_age_s,
  ARRAY_AGG(STRUCT(vref, lineRef, vjr, dvj, mc_stop, lat, lon, velocity, bearing, delay, vstatus) ORDER BY rec_at DESC LIMIT 2) samples
FROM g GROUP BY 1 ORDER BY n DESC
