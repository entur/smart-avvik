-- docs/gps-matching 2026-09-02. Dry run 0.06 GB. For each rail departure, the nearest other train leaving the same station for the same NEXT station. directionRef is always '0' for VYG, so direction is taken from the next stop, never from that field.

-- For each rail departure: nearest OTHER train leaving the same station for the same next station. One weekday.
WITH d AS (
  SELECT lr.datedServiceJourneyId dsj, lr.sequenceNr, IFNULL(q.stopPlaceRef, lr.stopPointRef) sp, lr.stopPointName, lr.departureTime, lr.lineRef, lr.directionRef,
    LEAD(IFNULL(q.stopPlaceRef, lr.stopPointRef)) OVER (PARTITION BY lr.datedServiceJourneyId ORDER BY lr.sequenceNr) next_sp
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded` lr
  LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` q ON q.id = lr.stopPointRef
  WHERE lr.operatingDate = '2026-09-01' AND lr.dataSource IN ('VYG','GOA','SJN','FLT')
    AND lr.estimated = FALSE AND NOT IFNULL(lr.stopCancellation, FALSE)
),
dep AS (SELECT * FROM d WHERE departureTime IS NOT NULL AND next_sp IS NOT NULL),
nearest AS (
  SELECT a.dsj, a.sp, a.stopPointName, a.next_sp, a.departureTime,
    MIN(ABS(TIMESTAMP_DIFF(a.departureTime, b.departureTime, SECOND))) min_gap_s,
    MIN(IF(a.lineRef = b.lineRef, ABS(TIMESTAMP_DIFF(a.departureTime, b.departureTime, SECOND)), NULL)) min_gap_same_line_s
  FROM dep a LEFT JOIN dep b
    ON a.sp = b.sp AND a.next_sp = b.next_sp AND a.dsj <> b.dsj
    AND ABS(TIMESTAMP_DIFF(a.departureTime, b.departureTime, SECOND)) < 900
  GROUP BY 1,2,3,4,5
)
SELECT scope, n_departures,
  ROUND(lt30/n_departures,3) f_other_train_same_next_within_30s,
  ROUND(lt60/n_departures,3) f_within_60s,
  ROUND(lt120/n_departures,3) f_within_120s,
  ROUND(lt300/n_departures,3) f_within_300s,
  ROUND(lt120_same_line/n_departures,3) f_within_120s_same_line
FROM (
  SELECT 'all stations' scope, COUNT(*) n_departures,
    COUNTIF(min_gap_s < 30) lt30, COUNTIF(min_gap_s < 60) lt60, COUNTIF(min_gap_s < 120) lt120, COUNTIF(min_gap_s < 300) lt300,
    COUNTIF(min_gap_same_line_s < 120) lt120_same_line
  FROM nearest
  UNION ALL
  SELECT stopPointName, COUNT(*),
    COUNTIF(min_gap_s < 30), COUNTIF(min_gap_s < 60), COUNTIF(min_gap_s < 120), COUNTIF(min_gap_s < 300),
    COUNTIF(min_gap_same_line_s < 120)
  FROM nearest WHERE stopPointName IN ('Oslo S','Nationaltheatret','Skøyen','Lysaker','Sandvika','Asker','Drammen','Lillestrøm','Ski','Bergen','Trondheim S','Stavanger')
  GROUP BY 1
)
ORDER BY n_departures DESC
