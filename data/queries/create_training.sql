-- Training table for the NSB disruption-duration model.
--
-- One row per disruption (situationNumber) — no entity-level flattening — so a
-- train/eval split keyed on situationNumber (is_eval below) is leakage-free.
--
-- Geography is encoded two ways so they can be compared as model inputs:
--   stop_<id>   multi-hot of the top-100 NSB stations  (from affected_stops)
--   od_<A_B>    multi-hot of the top-50  OD corridors   (from affected_journeys)
-- Corridors are canonicalized to be UNDIRECTED: the two endpoints are sorted,
-- so SKI-OSL and OSL-SKI both become OSL-SKI (one column, double the support).
-- NSB never populates affected_lines, so no line/region feature is derived.
--
-- The boolean columns are generated dynamically because BigQuery ML cannot
-- multi-hot an ARRAY<STRING> directly. Tune the two LIMITs to trade coverage vs.
-- width (top-50 stops ~= 76% coverage of stop occurrences, top-100 ~= 91%).

DECLARE stop_cols STRING;
DECLARE od_cols   STRING;

-- Top-N stations by number of distinct disruptions that touch them.
SET stop_cols = (
  SELECT STRING_AGG(
           FORMAT('(%T IN UNNEST(b.affected_stops)) AS stop_%s',
                  stop, REGEXP_REPLACE(stop, r'[^A-Za-z0-9]+', '_')),
           ',\n    ' ORDER BY sit_count DESC)
  FROM (
    SELECT st AS stop, COUNT(DISTINCT situationNumber) AS sit_count
    FROM `disruptions.disruptions`, UNNEST(affected_stops) AS st
    WHERE participantRef = 'NSB'
    GROUP BY st
    ORDER BY sit_count DESC
    LIMIT 100
  )
);

-- Top-N undirected origin-destination corridors (e.g. OSL-SKI) from journey refs.
SET od_cols = (
  WITH corr AS (
    SELECT situationNumber,
           (SELECT STRING_AGG(p, '-' ORDER BY p)
            FROM UNNEST(SPLIT(REGEXP_EXTRACT(j, r':\d+_([A-Za-z]+-[A-Za-z]+)'), '-')) AS p
           ) AS corridor
    FROM `disruptions.disruptions`, UNNEST(affected_journeys) AS j
    WHERE participantRef = 'NSB'
  )
  SELECT STRING_AGG(
           FORMAT('(%T IN UNNEST(b.od_corridors)) AS od_%s',
                  corridor, REGEXP_REPLACE(corridor, r'[^A-Za-z0-9]+', '_')),
           ',\n    ' ORDER BY sit_count DESC)
  FROM (
    SELECT corridor, COUNT(DISTINCT situationNumber) AS sit_count
    FROM corr
    WHERE corridor IS NOT NULL
    GROUP BY corridor
    ORDER BY sit_count DESC
    LIMIT 50
  )
);

EXECUTE IMMEDIATE FORMAT(r"""
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.training` AS
WITH base AS (
  SELECT
    d.*,
    ARRAY(
      SELECT DISTINCT
        (SELECT STRING_AGG(p, '-' ORDER BY p)
         FROM UNNEST(SPLIT(REGEXP_EXTRACT(j, r':\d+_([A-Za-z]+-[A-Za-z]+)'), '-')) AS p)
      FROM UNNEST(d.affected_journeys) AS j
      WHERE REGEXP_CONTAINS(j, r':\d+_([A-Za-z]+-[A-Za-z]+)')
    ) AS od_corridors
  FROM `disruptions.disruptions` AS d
  WHERE d.participantRef = 'NSB'
)
SELECT
  b.duration_minutes,
  IFNULL(c.category, 'UNKNOWN') AS category,
  IFNULL(b.severity, 'UNKNOWN') AS severity,
  IFNULL(b.planned,  FALSE)     AS planned,
  ARRAY_LENGTH(b.affected_stops)    AS num_affected_stops,
  ARRAY_LENGTH(b.affected_journeys) AS num_affected_journeys,
  ARRAY_LENGTH(b.od_corridors)      AS num_corridors,
  b.month,
  b.dayofweek,
  b.hour,
  MOD(ABS(FARM_FINGERPRINT(b.situationNumber)), 10) >= 8 AS is_eval,
  %s,
  %s
FROM base AS b
LEFT JOIN `disruptions.categories` AS c
  ON b.summary = c.summary AND b.description = c.description
WHERE b.description IS NULL OR (
      b.description NOT LIKE 'Bestilling ikke mottatt%%'
  AND b.description NOT LIKE 'Ingen bestilling mottatt%%'
  AND b.description NOT LIKE 'Innstilt grunnet manglende bestilling%%')
""", stop_cols, od_cols);
