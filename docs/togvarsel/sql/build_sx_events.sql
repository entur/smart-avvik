CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.togvarsel_sx_events` AS
WITH sx AS (
  SELECT participantRef p, situationNumber sn, SAFE.TIMESTAMP(creationTime) created_ts,
    DATETIME(SAFE.TIMESTAMP(creationTime), 'Europe/Oslo') created_local,
    duration_minutes dur, planned, severity, status, onset_summary, onset_description, summary,
    affected_journeys, affected_stops,
    LOWER(CONCAT(IFNULL(onset_summary,''),' ',IFNULL(onset_description,''))) txt
  FROM `ent-kixavvik-dev.disruptions.disruptions`
  WHERE participantRef IN ('NSB','VYG') AND SAFE.TIMESTAMP(creationTime) >= '2025-04-01'
),
cls AS (
  SELECT *,
    CASE
      WHEN txt LIKE 'færre vogner%' THEN 'faerre_vogner'
      WHEN status='innstilt' THEN 'innstilt'
      WHEN txt LIKE 'toget står%' THEN 'toget_staar'
      WHEN txt LIKE 'forsinkelse forventes%' THEN 'forsinkelse_forventes'
      WHEN txt LIKE 'forsinket%' THEN 'forsinket'
      WHEN REGEXP_CONTAINS(txt, r'^(buss for tog|planlegger buss|buss i tillegg|planlegger alternativ|ta ruters|vy kjører taxi|taxi)') THEN 'buss_for_tog'
      WHEN txt LIKE 'stengt%' THEN 'stengt'
      WHEN txt LIKE 'ta neste tog%' OR txt LIKE 'ta andre tog%' THEN 'ta_neste_tog'
      WHEN txt LIKE 'ny kjørerute%' OR txt LIKE 'bruk de forreste%' THEN 'ny_kjoererute'
      WHEN txt LIKE 'færre avganger%' OR txt LIKE 'redusert%' THEN 'faerre_avganger'
      ELSE 'other' END AS cls,
    CASE
      WHEN txt LIKE '%mangler et togsett%' OR txt LIKE '%mangler togsett%' THEN 'mangler_togsett'
      WHEN txt LIKE '%teknisk feil med toget%' OR txt LIKE '%feil på toget%' OR txt LIKE '%feil med toget%' THEN 'feil_tog'
      WHEN txt LIKE '%signalfeil%' OR txt LIKE '%signalanlegg%' THEN 'signalfeil'
      WHEN txt LIKE '%feil på infrastruktur%' OR txt LIKE '%infrastruktur%' OR txt LIKE '%sporveksel%' OR txt LIKE '%feil på sporet%' OR txt LIKE '%skinnebrudd%' OR txt LIKE '%kjøreledning%' THEN 'infrastruktur'
      WHEN txt LIKE '%strøm%' THEN 'stroem'
      WHEN txt LIKE '%personell%' OR txt LIKE '%personale%' OR txt LIKE '%mangler lokfører%' OR txt LIKE '%konduktør%' THEN 'personell'
      WHEN txt LIKE '%vedlikehold%' OR txt LIKE '%arbeid på%' OR txt LIKE '%planlagt arbeid%' OR txt LIKE '%anleggsarbeid%' THEN 'vedlikehold'
      WHEN txt LIKE '%tidligere stans%' OR txt LIKE '%andre tog%forsinket%' OR txt LIKE '%kø%' OR txt LIKE '%tett trafikk%' THEN 'foelgeforsinkelse'
      WHEN txt LIKE '%dyr i sporet%' OR txt LIKE '%påkjørsel%' OR txt LIKE '%person i sporet%' OR txt LIKE '%uvedkommende%' THEN 'sporet'
      WHEN txt LIKE '%vær%' OR txt LIKE '%snø%' OR txt LIKE '%flom%' OR txt LIKE '%uvær%' OR txt LIKE '%is %' OR txt LIKE '%varme%' OR txt LIKE '%kulde%' THEN 'vaer'
      WHEN txt LIKE '%politi%' OR txt LIKE '%brann%' OR txt LIKE '%ulykke%' OR txt LIKE '%syk%' THEN 'hendelse'
      ELSE 'ukjent' END AS cause
  FROM sx
)
SELECT c.p, c.sn, c.created_ts, c.created_local, DATE(c.created_local) created_date,
  EXTRACT(HOUR FROM c.created_local) + EXTRACT(MINUTE FROM c.created_local)/60.0 created_h,
  c.dur, c.planned, c.severity, c.status, c.cls, c.cause, c.onset_summary, c.onset_description,
  ARRAY_LENGTH(c.affected_journeys) n_journeys_in_msg, ARRAY_LENGTH(c.affected_stops) n_stops_in_msg,
  j_ref,
  j.operating_date, j.line, j.first_aimed_local, j.first_oslo_aimed_local, j.last_oslo_aimed_local, j.n_oslo_calls, j.corridors,
  j.n_oslo_calls > 0 AS oslo,
  j.n_cancelled, j.n_calls, j.oslo_mean_delay
FROM cls c
LEFT JOIN UNNEST(c.affected_journeys) j_ref
LEFT JOIN `ent-kixavvik-dev.disruptions.togvarsel_journeys` j ON j.sj = j_ref AND j.operating_date BETWEEN DATE(c.created_local) - 1 AND DATE(c.created_local) + 1
