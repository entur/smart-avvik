-- Per operating day: VYG rail trains in the standing timetable (>= 3 of the 4 previous same
-- weekdays) that are absent from the day's GTFS plan, trains new that day, and bus-for-train trips.
WITH g AS (
  SELECT DISTINCT operating_date od, route_short_name line, trip_id,
         REGEXP_EXTRACT(trip_id, r'ServiceJourney:(\d+)[-_]') trainno, STARTS_WITH(trip_id, 'VYG:ServiceJourney:BUS') bus
  FROM `ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1`
  WHERE agency_id LIKE 'VYG%' AND route_type = 'Train' AND operating_date BETWEEN '2026-08-10' AND '2026-09-23'),
trips AS (SELECT DISTINCT od, line, trainno FROM g WHERE trainno IS NOT NULL),
days AS (SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2026-09-08', '2026-09-23')) d),
standing AS (
  SELECT x.d, t.line, t.trainno FROM days x
  JOIN trips t ON t.od IN (DATE_SUB(x.d, INTERVAL 7 DAY), DATE_SUB(x.d, INTERVAL 14 DAY), DATE_SUB(x.d, INTERVAL 21 DAY), DATE_SUB(x.d, INTERVAL 28 DAY))
  GROUP BY 1,2,3 HAVING COUNT(*) >= 3),
cut AS (SELECT s.d, s.line FROM standing s LEFT JOIN trips t ON t.od = s.d AND t.trainno = s.trainno WHERE t.trainno IS NULL),
added AS (SELECT t.od d, t.line FROM trips t JOIN days ON days.d = t.od LEFT JOIN standing s ON s.d = t.od AND s.trainno = t.trainno WHERE s.trainno IS NULL),
buses AS (SELECT od d, COUNT(DISTINCT trip_id) n FROM g WHERE bus GROUP BY 1),
st AS (SELECT d, COUNT(*) standing FROM standing GROUP BY 1),
pl AS (SELECT od d, COUNT(*) planned FROM trips GROUP BY 1),
ct AS (SELECT d, COUNT(*) cut FROM cut GROUP BY 1),
ad AS (SELECT d, COUNT(*) added FROM added GROUP BY 1),
cl AS (SELECT d, STRING_AGG(CONCAT(line, ':', CAST(n AS STRING)), ' ' ORDER BY n DESC LIMIT 8) cut_lines
       FROM (SELECT d, line, COUNT(*) n FROM cut GROUP BY 1,2) GROUP BY 1)
SELECT x.d, FORMAT_DATE('%a', x.d) dow, standing, planned, IFNULL(cut, 0) cut, IFNULL(added, 0) added, b.n bus_trips, cut_lines
FROM days x LEFT JOIN st USING (d) LEFT JOIN pl USING (d) LEFT JOIN ct USING (d) LEFT JOIN ad USING (d)
LEFT JOIN buses b USING (d) LEFT JOIN cl USING (d) ORDER BY 1
