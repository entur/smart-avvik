-- disruptions.disruptions: one row per SIRI-SX situation (all operators) over the last 2
-- years, reduced to a single best-estimate record -- the latest snapshot per
-- (participantRef, situationNumber), its validity periods collapsed to one span (earliest
-- start -> latest end = duration_minutes), the first Norwegian summary/description, and the
-- affected stops / lines / dated journeys flattened to arrays. Excludes NO_IMPACT situations
-- and caps duration to 1..10080 min (7 days). NOT participant-filtered -- downstream queries
-- restrict participantRef as needed (e.g. 'NSB'). Standard 2-year partition lookback; this is
-- a full rebuild (CREATE OR REPLACE), so re-run it to pick up newer situations.
--
-- ONSET TEXT + STATUS: besides the latest summary/description, we also carry the ONSET
-- (first-version) summary/description and a regex-derived `status`. The latest summary can
-- have drifted to a resolved state ("Toget kjører igjen") -- using it as a predictor leaks
-- the outcome -- so anything fed to an onset-time model must use the onset_* fields. `status`
-- (innstilt / forsinket / kapasitet / other) captures the OUTCOME the message states at
-- onset, which is a stronger predictor of cancellation/delay than the cause taxonomy
-- (leak-free eta2_cancel ~0.32 vs ~0.23). Both onset fields are computed in the SAME scan via
-- FIRST_VALUE -- no second ET/SX pass.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.disruptions` AS
WITH
  -- 0. Per-snapshot scalars: the first Norwegian summary/description of each snapshot, plus an
  --    onset clock (event_ts). Computed once so both the latest- and first-version picks reuse
  --    them in a single scan.
  scored AS (
    SELECT
      participantRef, situationNumber, version, creationTime, ingestionTime, versionedAtTime,
      severity, planned, validityPeriods, affects,
      COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) AS event_ts,
      (SELECT x.value FROM UNNEST(summaries) AS x
       WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
       LIMIT 1) AS no_summary,
      (SELECT x.value FROM UNNEST(descriptions) AS x
       WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
       LIMIT 1) AS no_description
    FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
    WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY)
            >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR))
  ),
  -- 1. Keep only the latest snapshot of each situation = best estimate of the
  --    final/true validity window. `version` is NULL for ~73% of situations, so
  --    order primarily by versionedAtTime, falling back to version then
  --    ingestionTime — NOT by version alone. onset_summary/onset_description are the
  --    no_summary/no_description of the EARLIEST snapshot (FIRST_VALUE over the full
  --    partition), carried onto the latest row we keep.
  latest AS (
    SELECT
      *,
      FIRST_VALUE(no_summary)     OVER w_onset AS onset_summary,
      FIRST_VALUE(no_description) OVER w_onset AS onset_description
    FROM scored
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY participantRef, situationNumber
      ORDER BY
        SAFE.TIMESTAMP(versionedAtTime) DESC,
        version DESC,
        ingestionTime DESC
    ) = 1
    WINDOW w_onset AS (
      PARTITION BY participantRef, situationNumber
      ORDER BY event_ts ASC, version ASC, creationTime ASC
      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    )
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
  -- First Norwegian summary / description (computed per-snapshot in `scored`, so a situation
  -- with only one of the two is kept -- no summary×description cross join). `summary` /
  -- `description` are from the LATEST snapshot (unchanged); `onset_*` from the FIRST snapshot.
  no_summary     AS summary,
  no_description AS description,
  onset_summary,
  onset_description,
  -- Outcome stated at ONSET (leak-free). innstilt before forsinket: a cancellation is the
  -- stronger signal when a message mentions both. No 'allclear' bucket -- "kjører igjen" is a
  -- resolved-state message that never appears at onset (falls through to 'other').
  CASE
    WHEN REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(onset_summary, ''), ' ', IFNULL(onset_description, ''))),
                         r'innstil|kansel')                         THEN 'innstilt'
    WHEN REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(onset_summary, ''), ' ', IFNULL(onset_description, ''))),
                         r'forsink|toget står')                     THEN 'forsinket'
    WHEN REGEXP_CONTAINS(LOWER(IFNULL(onset_summary, '')),
                         r'færre vogner|forreste vogn|fremste vogn|bruk de') THEN 'kapasitet'
    ELSE 'other'
  END AS status,
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