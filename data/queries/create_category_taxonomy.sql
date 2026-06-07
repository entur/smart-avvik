-- Single source of truth for the disruption-category taxonomy used by AI.CLASSIFY.
-- Both create_categories.sql (full rebuild) and update_categories.sql (incremental
-- append) read this table to build the AI.CLASSIFY `categories =>` argument, so the
-- list lives in exactly one place. Edit here to add/rename/redescribe a category, then
-- re-run those scripts. sort_order only fixes a stable presentation order.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.category_taxonomy` AS
SELECT sort_order, category, description
FROM UNNEST([
  STRUCT(1  AS sort_order, 'infrastruktur/signal'     AS category, 'signalfeil, signalstans'    AS description),
  STRUCT(2,  'infrastruktur/sporveksel', 'sporvekselfeil'),
  STRUCT(3,  'infrastruktur/strøm',      'strømfeil, kontaktledning'),
  STRUCT(4,  'infrastruktur/spor',       'sporarbeid, skinnebrudd'),
  STRUCT(5,  'infrastruktur/tunnel',     'tunnelfeil'),
  STRUCT(6,  'infrastruktur/stasjon',    'plattformfeil, heisstans'),
  STRUCT(7,  'materiell/teknisk',        'teknisk feil på tog/buss'),
  STRUCT(8,  'materiell/dør',            'dørfeil'),
  STRUCT(9,  'materiell/bremse',         'bremsefeil'),
  STRUCT(10, 'materiell/motor',          'motorfeil'),
  STRUCT(11, 'ytre/vær/vinter',          'snø, is, ras'),
  STRUCT(12, 'ytre/vær/vind',            'storm'),
  STRUCT(13, 'ytre/vær/nedbør',          'regn, flom'),
  STRUCT(14, 'ytre/person',              'personpåkjørsel'),
  STRUCT(15, 'ytre/dyr',                 'dyr på sporet'),
  STRUCT(16, 'ytre/tredjepart',          'politiaksjon, brann, ulykke'),
  STRUCT(17, 'drift/personale',          'manglende fører/personale'),
  STRUCT(18, 'drift/kapasitet',          'overbelastning'),
  STRUCT(19, 'drift/planlagt',           'vedlikehold, arbeider'),
  STRUCT(20, 'drift/ruteendring',        'midlertidig ruteendring')
]);
