CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.categories`
AS
WITH
  texts AS (
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
      ('infrastruktur/signal', 'signalfeil, signalstans'),
      ('infrastruktur/sporveksel', 'sporvekselfeil'),
      ('infrastruktur/strøm', 'strømfeil, kontaktledning'),
      ('infrastruktur/spor', 'sporarbeid, skinnebrudd'),
      ('infrastruktur/tunnel', 'tunnelfeil'),
      ('infrastruktur/stasjon', 'plattformfeil, heisstans'),
      ('materiell/teknisk', 'teknisk feil på tog/buss'),
      ('materiell/dør', 'dørfeil'),
      ('materiell/bremse', 'bremsefeil'),
      ('materiell/motor', 'motorfeil'),
      ('ytre/vær/vinter', 'snø, is, ras'),
      ('ytre/vær/vind', 'storm'),
      ('ytre/vær/nedbør', 'regn, flom'),
      ('ytre/person', 'personpåkjørsel'),
      ('ytre/dyr', 'dyr på sporet'),
      ('ytre/tredjepart', 'politiaksjon, brann, ulykke'),
      ('drift/personale', 'manglende fører/personale'),
      ('drift/kapasitet', 'overbelastning'),
      ('drift/planlagt', 'vedlikehold, arbeider'),
      ('drift/ruteendring', 'midlertidig ruteendring')]
  ) AS category
FROM texts
WHERE summary IS NOT NULL OR description IS NOT NULL