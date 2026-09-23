-- Registrations that had four pushes on Oslo day @DAY (counting a second message about the same
-- train) and then missed at least one cancelled train that day: the four pushes, in order.
WITH d AS (
  SELECT *, FORMAT('%s|%s|%t', operator, jkey, departure_time) train
  FROM `ent-kixavvik-prd.disruptions.varsel_decisions`
  WHERE call_date BETWEEN DATE_SUB(DATE '@DAY', INTERVAL 0 DAY) AND DATE_ADD(DATE '@DAY', INTERVAL 1 DAY)
    AND DATE(decided_at) BETWEEN DATE_SUB(DATE '@DAY', INTERVAL 1 DAY) AND DATE_ADD(DATE '@DAY', INTERVAL 2 DAY)),
push AS (
  SELECT journey_digest, train, situation_number, MIN(decided_at) t,
         ANY_VALUE(line) line, ANY_VALUE(departure_time) dep, ANY_VALUE(summary) summary, MAX(n_devices) dev
  FROM d WHERE pushed AND DATE(decided_at, 'Europe/Oslo') = DATE '@DAY' GROUP BY 1,2,3),
ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY journey_digest ORDER BY t, dep) rn FROM push),
capped AS (SELECT journey_digest, MAX(dev) dev FROM push GROUP BY 1 HAVING COUNT(*) >= 4),
miss AS (
  SELECT journey_digest, COUNT(*) n_missed FROM (
    SELECT journey_digest, train FROM d WHERE call_date = DATE '@DAY'
    GROUP BY 1,2 HAVING LOGICAL_OR(et_cancelled) AND NOT LOGICAL_OR(pushed)) GROUP BY 1),
def AS (SELECT journey_digest, ANY_VALUE(from_stop) fs, ANY_VALUE(to_stop) ts FROM d GROUP BY 1),
nsr AS (SELECT id, ANY_VALUE(name) name FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version` GROUP BY id)
SELECT c.journey_digest, COALESCE(a.name, def.fs) fra, COALESCE(b.name, def.ts) til, c.dev, m.n_missed,
  (SELECT COUNT(DISTINCT r2.train) FROM ranked r2 WHERE r2.journey_digest = c.journey_digest AND r2.rn <= 4) n_trains,
  r.rn, FORMAT_TIMESTAMP('%H:%M', r.t, 'Europe/Oslo') varslet, r.line, FORMAT_TIMESTAMP('%H:%M', r.dep, 'Europe/Oslo') avgang,
  CASE WHEN r.situation_number LIKE 'KIX:ETCancellation:%' THEN 'ET-innstilling'
       WHEN r.situation_number LIKE 'KIX:NoSituation:%' THEN 'prediksjon' ELSE r.summary END om
FROM capped c JOIN miss m USING (journey_digest) JOIN def USING (journey_digest)
JOIN ranked r ON r.journey_digest = c.journey_digest AND r.rn <= 4
LEFT JOIN nsr a ON a.id = def.fs LEFT JOIN nsr b ON b.id = def.ts
ORDER BY m.n_missed DESC, c.journey_digest, r.rn
