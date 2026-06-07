-- Incremental refresh of disruptions.categories.
--
-- create_categories.sql does a full 2-year AI.CLASSIFY rebuild; this script instead
-- APPENDS only the summary/description pairs that have appeared since the table was
-- last written. New disruptions (e.g. the post-training disruptions_initial_new slice)
-- carry texts the table has never seen, so they join to nothing and fall through to
-- 'UNKNOWN' in the prediction queries — this closes that gap cheaply.
--
-- HOW:
--  1. last_update_date = the day disruptions.categories was last modified (from the
--     dataset's __TABLES__ metadata, last_modified_time in epoch ms).
--  2. Re-scan SIRI-SX for partitions on/after that day (>= so the last-updated day is
--     re-covered), collect distinct Norwegian summary/description pairs.
--  3. INSERT only the pairs NOT already present (NULL-safe equality dedup), classifying
--     just those with AI.CLASSIFY. Cost scales with NEW pairs only, not the whole table.
--
-- The category taxonomy is read from disruptions.category_taxonomy (single source of
-- truth; see create_category_taxonomy.sql) and rendered into the AI.CLASSIFY
-- `categories =>` literal via FORMAT('%T'), identical to create_categories.sql.
--
-- EXECUTE IMMEDIATE inlines the date as a literal so the _PARTITIONTIME filter prunes to
-- just the recent partitions (a scripting variable in the filter may not prune), and
-- keeps the rendered category list a literal array (AI.CLASSIFY requires that).
-- Re-run whenever new disruptions need categorizing (e.g. before re-running predict_*).

DECLARE last_update_date DATE DEFAULT (
  SELECT DATE(TIMESTAMP_MILLIS(last_modified_time))
  FROM `ent-kixavvik-dev.disruptions.__TABLES__`
  WHERE table_id = 'categories'
);

DECLARE cat_array STRING DEFAULT (
  SELECT STRING_AGG(FORMAT('(%T, %T)', category, description), ',\n      ' ORDER BY sort_order)
  FROM `ent-kixavvik-dev.disruptions.category_taxonomy`
);

EXECUTE IMMEDIATE FORMAT(r"""
INSERT INTO `ent-kixavvik-dev.disruptions.categories` (summary, description, category)
WITH texts AS (
  SELECT DISTINCT
    (SELECT s.value
     FROM UNNEST(summaries) AS s
     WHERE s.value IS NOT NULL
       AND (s.language IS NULL OR UPPER(s.language) = 'NO')
     LIMIT 1) AS summary,
    (SELECT d.value
     FROM UNNEST(descriptions) AS d
     WHERE d.value IS NOT NULL
       AND (d.language IS NULL OR UPPER(d.language) = 'NO')
     LIMIT 1) AS description
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
  WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) >= TIMESTAMP(DATE '%s')
)
SELECT
  summary,
  description,
  AI.CLASSIFY(
    CONCAT(IFNULL(summary, ''), ' ', IFNULL(description, '')),
    categories => [
      %s
    ]
  ) AS category
FROM texts AS t
WHERE (summary IS NOT NULL OR description IS NOT NULL)
  -- only classify pairs not already in the table (NULL-safe equality; a sentinel
  -- stands in for NULL so missing summary/description still dedup correctly)
  AND NOT EXISTS (
    SELECT 1
    FROM `ent-kixavvik-dev.disruptions.categories` AS c
    WHERE IFNULL(c.summary,     '__NULL__') = IFNULL(t.summary,     '__NULL__')
      AND IFNULL(c.description, '__NULL__') = IFNULL(t.description, '__NULL__')
  )
""", CAST(last_update_date AS STRING), cat_array);
