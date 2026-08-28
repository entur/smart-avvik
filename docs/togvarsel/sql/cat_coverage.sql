WITH e AS (SELECT DISTINCT sn, onset_summary, onset_description FROM `ent-kixavvik-dev.disruptions.togvarsel_sx_events`),
j AS (
  SELECT e.sn, c.category
  FROM e LEFT JOIN `ent-kixavvik-dev.disruptions.categories` c
    ON IFNULL(e.onset_summary,'__NULL__')=IFNULL(c.summary,'__NULL__') AND IFNULL(e.onset_description,'__NULL__')=IFNULL(c.description,'__NULL__')
)
SELECT IFNULL(category,'(no match)') category, COUNT(DISTINCT sn) n_sit, ROUND(100*COUNT(DISTINCT sn)/(SELECT COUNT(*) FROM e),1) pct
FROM j GROUP BY 1 ORDER BY 2 DESC
