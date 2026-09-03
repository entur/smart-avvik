-- docs/gps-matching 2026-09-02. Dry run 0.01 GB. Quay coordinates at Oslo S, Asker and Drammen with distance to the stop-place point.

SELECT r.name, r.id, q.id qid, q.publicCode, q.location_latitude lat, q.location_longitude lon,
  ROUND(ST_DISTANCE(ST_GEOGPOINT(r.location_longitude, r.location_latitude), ST_GEOGPOINT(q.location_longitude, q.location_latitude)),1) m_from_station_point
FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version` r
JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` q ON q.stopPlaceRef = r.id
WHERE LOWER(r.transportMode) = 'rail' AND r.name IN ('Oslo S','Asker stasjon','Drammen stasjon')
ORDER BY r.name, q.publicCode
