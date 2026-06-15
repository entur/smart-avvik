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
