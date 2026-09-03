-- docs/gps-matching 2026-09-02. Dry run 0.01 GB. NSR: coordinate coverage for rail stop places and their quays; note transportMode is UPPERCASE ('RAIL').

-- NSR: coordinates per rail station and its quays
WITH rail AS (
  SELECT id, name, location_longitude lon, location_latitude lat
  FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version`
  WHERE LOWER(transportMode) = 'rail'
),
q AS (
  SELECT q.stopPlaceRef, q.id qid, q.publicCode, q.location_longitude lon, q.location_latitude lat
  FROM `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` q
  JOIN rail r ON r.id = q.stopPlaceRef
),
per_station AS (
  SELECT r.id, r.name, r.lat IS NOT NULL has_coord,
    COUNT(q.qid) n_quays,
    COUNTIF(q.lat IS NOT NULL) n_quays_with_coord,
    MAX(ST_DISTANCE(ST_GEOGPOINT(r.lon, r.lat), ST_GEOGPOINT(q.lon, q.lat))) max_quay_to_station_m,
    COUNT(DISTINCT CONCAT(CAST(q.lat AS STRING), ',', CAST(q.lon AS STRING))) n_distinct_quay_points
  FROM rail r LEFT JOIN q ON q.stopPlaceRef = r.id
  GROUP BY 1,2,3
)
SELECT COUNT(*) n_rail_stations,
  COUNTIF(has_coord) n_with_coord,
  ROUND(AVG(n_quays),2) avg_quays,
  COUNTIF(n_quays > 0 AND n_quays_with_coord = n_quays) n_all_quays_have_coord,
  COUNTIF(n_quays > 1 AND n_distinct_quay_points = 1) n_multi_quay_but_one_point,
  APPROX_QUANTILES(max_quay_to_station_m, 100)[OFFSET(50)] p50_max_quay_offset_m,
  APPROX_QUANTILES(max_quay_to_station_m, 100)[OFFSET(90)] p90_max_quay_offset_m,
  MAX(max_quay_to_station_m) max_quay_offset_m
FROM per_station
