-- Trains and bus-for-train trips per Vy rail line in each day's GTFS plan.
SELECT operating_date d, route_short_name line,
  COUNT(DISTINCT IF(NOT STARTS_WITH(trip_id, 'VYG:ServiceJourney:BUS'), trip_id, NULL)) tog,
  COUNT(DISTINCT IF(STARTS_WITH(trip_id, 'VYG:ServiceJourney:BUS'), trip_id, NULL)) buss
FROM `ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1`
WHERE operating_date BETWEEN '2026-09-11' AND '2026-09-23' AND agency_id LIKE 'VYG%' AND route_type = 'Train'
GROUP BY 1,2 ORDER BY 2,1
