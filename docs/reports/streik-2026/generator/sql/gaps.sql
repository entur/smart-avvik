-- One row per cancelled train x registration (journey definition) on 18-23 Sept that got no push
-- about that train, with the evidence for why:
--   capped      the registration had spent its four pushes on four different trains before departure
--   capped_any  the same, counting a second message about the same train as a push of its own
--   last_push   NULL when the registration received no push at all from 4 to 23 Sept
WITH d AS (
  SELECT *, FORMAT('%s|%s|%t', operator, jkey, departure_time) train
  FROM `ent-kixavvik-prd.disruptions.varsel_decisions`
  WHERE call_date BETWEEN DATE '2026-09-17' AND DATE '2026-09-23'
    AND DATE(decided_at) BETWEEN DATE '2026-09-16' AND DATE '2026-09-24'),
push AS (SELECT journey_digest, train, MIN(decided_at) t FROM d WHERE pushed GROUP BY 1,2),
perday AS (SELECT journey_digest, DATE(t, 'Europe/Oslo') day, COUNT(*) n,
             ARRAY_AGG(t ORDER BY t)[SAFE_OFFSET(3)] t4 FROM push GROUP BY 1,2),
-- The same count per (train, situation): what the cap spends if a repeat about a train is charged.
push_any AS (SELECT journey_digest, train, situation_number, MIN(decided_at) t FROM d WHERE pushed GROUP BY 1,2,3),
perday_any AS (SELECT journey_digest, DATE(t, 'Europe/Oslo') day, COUNT(*) n,
                 ARRAY_AGG(t ORDER BY t)[SAFE_OFFSET(3)] t4 FROM push_any GROUP BY 1,2),
hist AS (SELECT journey_digest, MAX(IF(pushed, decided_at, NULL)) last_push
         FROM `ent-kixavvik-prd.disruptions.varsel_decisions`
         WHERE call_date BETWEEN DATE '2026-09-04' AND DATE '2026-09-23' AND DATE(decided_at) >= DATE '2026-09-03'
         GROUP BY 1),
s AS (
  SELECT call_date, train, journey_digest, ANY_VALUE(line) line, ANY_VALUE(departure_time) dep,
         ANY_VALUE(from_stop) fs, ANY_VALUE(to_stop) ts,
         MIN(IF(et_cancelled, decided_at, NULL)) first_seen,
         ARRAY_AGG(IF(et_cancelled, message_status, NULL) IGNORE NULLS ORDER BY decided_at DESC LIMIT 1)[SAFE_OFFSET(0)] st,
         MAX(n_devices) dev
  FROM d GROUP BY 1,2,3 HAVING LOGICAL_OR(et_cancelled) AND NOT LOGICAL_OR(pushed)),
nsr AS (SELECT id, ANY_VALUE(name) name FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version` GROUP BY id)
SELECT s.call_date, s.line, FORMAT_TIMESTAMP('%H:%M', s.dep, 'Europe/Oslo') avgang,
  COALESCE(a.name, s.fs) fra, COALESCE(b.name, s.ts) til, s.st, s.dev,
  FORMAT_TIMESTAMP('%d.%m %H:%M', s.first_seen, 'Europe/Oslo') first_seen,
  EXISTS (SELECT 1 FROM perday p WHERE p.journey_digest = s.journey_digest AND p.n >= 4
            AND p.day BETWEEN DATE(s.first_seen, 'Europe/Oslo') AND DATE(s.dep, 'Europe/Oslo')
            AND p.t4 < s.dep) capped,
  EXISTS (SELECT 1 FROM perday_any p WHERE p.journey_digest = s.journey_digest AND p.n >= 4
            AND p.day BETWEEN DATE(s.first_seen, 'Europe/Oslo') AND DATE(s.dep, 'Europe/Oslo')
            AND p.t4 < s.dep) capped_any,
  FORMAT_TIMESTAMP('%d.%m %H:%M', h.last_push, 'Europe/Oslo') last_push,
  h.last_push > s.first_seen AS pushed_after_seen
FROM s JOIN hist h USING (journey_digest)
LEFT JOIN nsr a ON a.id = s.fs LEFT JOIN nsr b ON b.id = s.ts
WHERE s.call_date >= DATE '2026-09-18'
ORDER BY s.call_date, s.dep, fra, s.journey_digest
