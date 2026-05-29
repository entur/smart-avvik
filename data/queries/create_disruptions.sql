CREATE OR REPLACE TABLE `disruptions.disruptions` AS
SELECT
  creationTime,
  participantRef,
  situationNumber,
  TIMESTAMP_DIFF(SAFE.TIMESTAMP(v.endTime), SAFE.TIMESTAMP(v.startTime), MINUTE)
    AS duration_minutes,
  s.value AS summary,
  d.value AS description,
  severity,
  (
    SELECT ARRAY_AGG(stop_ref ORDER BY stop_ref)
    FROM (
      SELECT stops.stopPointRef AS stop_ref
      FROM UNNEST(affects.stopPoints) AS stops
      WHERE stops.stopPointRef IS NOT NULL
      UNION DISTINCT
      SELECT sp.stopPointRef AS stop_ref
      FROM UNNEST(affects.networks) AS n,
           UNNEST(n.affectedLines) AS al,
           UNNEST(al.routes) AS r,
           UNNEST(r.stopPoints.stopPoints) AS sp
      WHERE sp.stopPointRef IS NOT NULL
      UNION DISTINCT
      SELECT sp.stopPointRef AS stop_ref
      FROM UNNEST(affects.vehicleJourneys) AS vj,
           UNNEST(vj.routes) AS r,
           UNNEST(r.stopPoints.stopPoints) AS sp
      WHERE sp.stopPointRef IS NOT NULL
    )
  ) AS affected_stops,
  (
    SELECT ARRAY_AGG(line_ref ORDER BY line_ref)
    FROM (
      SELECT journeys.lineRef AS line_ref
      FROM UNNEST(affects.vehicleJourneys) AS journeys
      WHERE journeys.lineRef IS NOT NULL
      UNION DISTINCT
      SELECT al.lineRef AS line_ref
      FROM UNNEST(affects.networks) AS n,
           UNNEST(n.affectedLines) AS al
      WHERE al.lineRef IS NOT NULL
      UNION DISTINCT
      SELECT REGEXP_REPLACE(dvjr, r'_\d{2}_\d{2}_\d{2}$', '') AS line_ref
      FROM UNNEST(affects.vehicleJourneys) AS journeys,
           UNNEST(journeys.datedVehicleJourneyRefs) AS dvjr
      WHERE dvjr IS NOT NULL
      UNION DISTINCT
      SELECT journeys.framedVehicleJourneyRef.datedVehicleJourneyRef AS line_ref
      FROM UNNEST(affects.vehicleJourneys) AS journeys
      WHERE journeys.framedVehicleJourneyRef.datedVehicleJourneyRef IS NOT NULL
    )
  ) AS affected_lines,
  EXTRACT(YEAR FROM SAFE.TIMESTAMP(v.startTime)) AS year,
  EXTRACT(MONTH FROM SAFE.TIMESTAMP(v.startTime)) AS month,
  EXTRACT(DAY FROM SAFE.TIMESTAMP(v.startTime)) AS day,
  EXTRACT(DAYOFWEEK FROM SAFE.TIMESTAMP(v.startTime)) AS dayofweek,
  EXTRACT(HOUR FROM SAFE.TIMESTAMP(v.startTime)) AS hour,
FROM
  `ent-anshar-prd.realtime.siri_realtime_sx_production`,
  UNNEST(validityPeriods) AS v,
  UNNEST(summaries) AS s,
  UNNEST(descriptions) AS d
WHERE
  TIMESTAMP_TRUNC(_PARTITIONTIME, DAY)
    >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR))
  AND (severity IS NULL OR severity != "NO_IMPACT")
  AND SAFE.TIMESTAMP(v.startTime) IS NOT NULL
  AND SAFE.TIMESTAMP(v.endTime) IS NOT NULL
  AND (s.language IS NULL OR UPPER(s.language) = 'NO')
  AND (d.language IS NULL OR UPPER(d.language) = 'NO');
