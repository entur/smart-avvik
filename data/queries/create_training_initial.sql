-- Training table for the two INITIAL-version duration models. Reads from
-- disruptions_initial: features from the first version, plus BOTH labels —
-- reopen_minutes ("trains running again") and impact_minutes ("traffic impacted").
-- One row per situationNumber, so the is_eval split keyed on situationNumber is
-- leakage-free. Both labels are excluded from features at train time (train_*).
--
-- Geography is multi-hot encoded two ways for the ablation in train_*:
--   stop_<id>   top-50 NSB stops      (from affected_stops)
--   od_<A_B>    top-25 OD corridors    (undirected, from affected_journeys)
-- Boolean columns are generated dynamically because BigQuery ML cannot multi-hot
-- an ARRAY<STRING> directly. The widths are deliberately narrower than the
-- latest-snapshot pipeline (top-100/50): this label only yields ~7.9k rows, so a
-- wide encoding overfits. Let dur_init_nogeo vs dur_init_both decide if geography
-- earns its place at all; only then widen the two LIMITs.

DECLARE stop_cols STRING;
DECLARE od_cols   STRING;

-- Top-N stops by number of distinct disruptions that touch them.
SET stop_cols = (
  SELECT STRING_AGG(
           FORMAT('(%T IN UNNEST(b.affected_stops)) AS stop_%s',
                  stop, REGEXP_REPLACE(stop, r'[^A-Za-z0-9]+', '_')),
           ',\n    ' ORDER BY sit_count DESC)
  FROM (
    SELECT st AS stop, COUNT(DISTINCT situationNumber) AS sit_count
    FROM `disruptions.disruptions_initial`, UNNEST(affected_stops) AS st
    GROUP BY st
    ORDER BY sit_count DESC
    LIMIT 50
  )
);

-- Top-N undirected origin-destination corridors (e.g. OSL-SKI) from journey refs.
SET od_cols = (
  WITH corr AS (
    SELECT situationNumber,
           (SELECT STRING_AGG(p, '-' ORDER BY p)
            FROM UNNEST(SPLIT(REGEXP_EXTRACT(j, r':\d+_([A-Za-z]+-[A-Za-z]+)'), '-')) AS p
           ) AS corridor
    FROM `disruptions.disruptions_initial`, UNNEST(affected_journeys) AS j
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
    LIMIT 25
  )
);

EXECUTE IMMEDIATE FORMAT(r"""
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.training_initial` AS
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
  FROM `disruptions.disruptions_initial` AS d
)
SELECT
  b.situationNumber,                 -- row key (EXCEPT'd from features at train time)
  b.reopen_minutes,
  b.impact_minutes,
  IFNULL(c.category,   'UNKNOWN') AS category,
  IFNULL(b.severity,   'UNKNOWN') AS severity,
  IFNULL(b.reportType, 'UNKNOWN') AS reportType,
  -- planned is omitted: it is always NULL across the reopening-labelled slice.
  b.priority,
  -- Operator's initially-declared window: a legitimate onset-time signal.
  b.declared_window_minutes,
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
  ON  IFNULL(b.summary, '__NULL__')     = IFNULL(c.summary, '__NULL__')
  AND IFNULL(b.description, '__NULL__') = IFNULL(c.description, '__NULL__')
WHERE b.description IS NULL OR (
      b.description NOT LIKE 'Bestilling ikke mottatt%%'
  AND b.description NOT LIKE 'Ingen bestilling mottatt%%'
  AND b.description NOT LIKE 'Innstilt grunnet manglende bestilling%%')
""", stop_cols, od_cols);
