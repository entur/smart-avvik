. This shoudl
  -- 1. Keep only the latest snapshot of each situation = best estimate of the
  --    final/true validity window. `version` is NULL for ~73% of situations, so
  --    order primarily by versionedAtTime, falling back to version then
  --    ingestionTime — NOT by version alone.
  latest AS (
    SELECT *
    FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
    WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY)
            >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR))
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY participantRef, situationNumber
      ORDER BY
        SAFE.TIMESTAMP(versionedAtTime) DESC,
        version DESC,
        ingestionTime DESC
    ) = 1
  ),
  -- 2. Collapse all validity periods of a situation into a single span:
  --    earliest start -> latest end = total situation length.
  spans AS (
    SELECT
      s.*,
      TIMESTAMP_DIFF(s.end_ts, s.start_ts, MINUTE) AS duration_minutes
    FROM (
      SELECT
        *,
        (SELECT MIN(SAFE.TIMESTAMP(v.startTime)) FROM UNNEST(validityPeriods) AS v) AS start_ts,
        (SELECT MAX(SAFE.TIMESTAMP(v.endTime))   FROM UNNEST(validityPeriods) AS v) AS end_ts
      FROM latest
    ) AS s
  )
SELECT
  creationTime,
  participantRef,
  situationNumber,
  duration_minutes,
  planned,
  severity,
  -- First Norwegian summary / description as independent scalar subqueries, so a
  -- situation with only one of the two is kept (no summary×description cross join).
  (SELECT x.value FROM UNNEST(summaries) AS x
   WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
   LIMIT 1) AS summary,
  (SELECT x.value FROM UNNEST(descriptions) AS x
   WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
   LIMIT 1) AS description,
  (
    SELECT ARRAY_AGG(stop_ref ORDER BY stop_ref)
    FROM (
      SELECT stops.stopPointRef AS stop_ref
      FROM UNNEST(affects.stopPoints) AS stops
      WHERE stops.stopPointRef IS NOT NULL
      UNION DISTINCT
      SELECT sp.stopPointRef
      FROM UNNEST(affects.networks) AS n,
           UNNEST(n.affectedLines) AS al,
           UNNEST(al.routes) AS r,
           UNNEST(r.stopPoints.stopPoints) AS sp
      WHERE sp.stopPointRef IS NOT NULL
      UNION DISTINCT
      SELECT sp.stopPointRef
      FROM UNNEST(affects.vehicleJourneys) AS vj,
           UNNEST(vj.routes) AS r,
           UNNEST(r.stopPoints.stopPoints) AS sp
      WHERE sp.stopPointRef IS NOT NULL
    )
  ) AS affected_stops,
  -- Real LineRefs only.
  (
    SELECT ARRAY_AGG(line_ref ORDER BY line_ref)
    FROM (
      SELECT journeys.lineRef AS line_ref
      FROM UNNEST(affects.vehicleJourneys) AS journeys
      WHERE journeys.lineRef IS NOT NULL
      UNION DISTINCT
      SELECT al.lineRef
      FROM UNNEST(affects.networks) AS n,
           UNNEST(n.affectedLines) AS al
      WHERE al.lineRef IS NOT NULL
    )
  ) AS affected_lines,
  -- Dated journey refs, kept separately.
  (
    SELECT ARRAY_AGG(journey_ref ORDER BY journey_ref)
    FROM (
      SELECT journeys.framedVehicleJourneyRef.datedVehicleJourneyRef AS journey_ref
      FROM UNNEST(affects.vehicleJourneys) AS journeys
      WHERE journeys.framedVehicleJourneyRef.datedVehicleJourneyRef IS NOT NULL
      UNION DISTINCT
      SELECT dvjr
      FROM UNNEST(affects.vehicleJourneys) AS journeys,
           UNNEST(journeys.datedVehicleJourneyRefs) AS dvjr
      WHERE dvjr IS NOT NULL
    )
  ) AS affected_journeys,
  EXTRACT(YEAR      FROM start_ts) AS year,
  EXTRACT(MONTH     FROM start_ts) AS month,
  EXTRACT(DAY       FROM start_ts) AS day,
  EXTRACT(DAYOFWEEK FROM start_ts) AS dayofweek,
  EXTRACT(HOUR      FROM start_ts) AS hour,
FROM spans
WHERE
  (severity IS NULL OR severity != "NO_IMPACT")
  AND start_ts IS NOT NULL
  AND end_ts IS NOT NULL
  -- Drop non-positive labels and cap the long tail (placeholder end times reach
  -- ~year 9999). 10080 = 7 days; raise it, or add `AND planned = FALSE`, to taste.
  AND duration_minutes BETWEEN 1 AND 10080;