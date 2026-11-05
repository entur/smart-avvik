-- Backfill disruptions.categories for any (summary, description) pair that is present in
-- disruptions.disruptions but not yet classified. Unlike update_categories.sql (which scans
-- SIRI-SX from the day categories was last modified, and so can miss pairs from a gap period
-- if categories was touched more recently than those pairs appeared), this sources the pairs
-- straight from disruptions, so it closes the gap regardless of categories' last-modified day
-- or whether the live API poller has been running.
--
-- AI.CLASSIFY runs only on the missing pairs (NOT EXISTS dedup, NULL-safe via a '__NULL__'
-- sentinel), so cost scales with the gap, not the table. Textless situations (NULL summary
-- AND NULL description) have nothing to classify and are skipped -- they stay 'UNKNOWN'.
--
-- Run it after rebuilding disruptions and before the tables that join to categories
-- (trip_outcomes, category_line_hour_impact). The taxonomy is read from
-- disruptions.category_taxonomy and rendered into the AI.CLASSIFY `categories =>` literal via
-- FORMAT('%T'), identical to create_categories.sql / update_categories.sql.

DECLARE cat_array STRING DEFAULT (
  SELECT STRING_AGG(FORMAT('(%T, %T)', category, description), ',\n      ' ORDER BY sort_order)
  FROM `ent-smartavvik-dev.disruptions.category_taxonomy`
);

EXECUTE IMMEDIATE FORMAT(r"""
INSERT INTO `ent-smartavvik-dev.disruptions.categories` (summary, description, category)
WITH missing AS (
  SELECT DISTINCT d.summary, d.description
  FROM `ent-smartavvik-dev.disruptions.disruptions` AS d
  WHERE (d.summary IS NOT NULL OR d.description IS NOT NULL)
    AND NOT EXISTS (
      SELECT 1 FROM `ent-smartavvik-dev.disruptions.categories` AS c
      WHERE IFNULL(c.summary,     '__NULL__') = IFNULL(d.summary,     '__NULL__')
        AND IFNULL(c.description, '__NULL__') = IFNULL(d.description, '__NULL__')
    )
)
SELECT
  summary, description,
  AI.CLASSIFY(
    CONCAT(IFNULL(summary, ''), ' ', IFNULL(description, '')),
    categories => [
      %s
    ]
  ) AS category
FROM missing
""", cat_array);
