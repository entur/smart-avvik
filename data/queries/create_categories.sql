-- Full rebuild of disruptions.categories: AI.CLASSIFY every distinct Norwegian
-- summary/description pair from the last 2 years of SIRI-SX.
--
-- The category taxonomy is NOT inlined here — it is read from
-- disruptions.category_taxonomy (single source of truth; see create_category_taxonomy.sql)
-- and rendered into the AI.CLASSIFY `categories =>` literal via FORMAT('%T'), so the list
-- lives in exactly one place. EXECUTE IMMEDIATE is used because AI.CLASSIFY needs that
-- argument as a literal array; injecting the rendered text keeps it a literal.
--
-- For incremental top-ups (classify only newly-seen pairs) use update_categories.sql
-- instead of this full, ~2-year, all-pairs rebuild.

DECLARE cat_array STRING DEFAULT (
  SELECT STRING_AGG(FORMAT('(%T, %T)', category, description), ',\n      ' ORDER BY sort_order)
  FROM `ent-kixavvik-dev.disruptions.category_taxonomy`
);

EXECUTE IMMEDIATE FORMAT(r"""
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.categories` AS
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
  WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR))
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
FROM texts
WHERE summary IS NOT NULL OR description IS NOT NULL
""", cat_array);
