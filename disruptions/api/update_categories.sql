-- Incrementally classify each disruption's free text (summary + description) into a category and
-- append it to disruptions.categories — the category lookup predict.sql joins on (a miss there
-- falls back to 'UNKNOWN'). In the main.py pipeline this runs after disruptions_initial_today is
-- rebuilt and before predict.sql, so new incidents get a real category before scoring.
--
-- cat_array: the candidate categories, built from disruptions.category_taxonomy as a list of
-- (category, description) tuples ordered by sort_order. AI.CLASSIFY needs its category set as a
-- literal in the query text, so we inline cat_array via EXECUTE IMMEDIATE FORMAT (the r"""...%s"""
-- raw string keeps backslashes intact and drops the tuple list in verbatim).
--
-- Only NEW (summary, description) pairs are classified: the NOT EXISTS guard skips any pair
-- already in disruptions.categories, using NULL-safe equality (a '__NULL__' sentinel) so a
-- missing summary or description still dedups correctly. Re-runs only pay to classify unseen text.
DECLARE cat_array STRING DEFAULT (
  SELECT STRING_AGG(FORMAT('(%T, %T)', category, description), ',\n      ' ORDER BY sort_order)
  FROM `ent-kixavvik-dev.disruptions.category_taxonomy`
);

EXECUTE IMMEDIATE FORMAT(r"""
INSERT INTO `ent-kixavvik-dev.disruptions.categories` (summary, description, category)
WITH texts AS (
  SELECT summary, description FROM `ent-kixavvik-dev.disruptions.disruptions_initial_today`
  WHERE summary IS NOT NULL OR description IS NOT NULL
  GROUP BY summary, description
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
""", cat_array);
