-- Per operating day: registered journeys, cancelled trains on them, and how many of those trains
-- were alerted -- through the ET cancellation itself (et_pushed) or through any message about the
-- same train (any_pushed).
WITH d AS (
  SELECT *, FORMAT('%s|%s|%t', operator, jkey, departure_time) train
  FROM `ent-kixavvik-prd.disruptions.varsel_decisions`
  WHERE call_date BETWEEN DATE '2026-09-11' AND DATE '2026-09-23'
    AND DATE(decided_at) BETWEEN DATE '2026-09-10' AND DATE '2026-09-24'),
s AS (  -- one row per (train x registration)
  SELECT call_date, train, journey_digest, LOGICAL_OR(et_cancelled) canc,
         LOGICAL_OR(et_cancelled AND pushed) et_pushed, LOGICAL_OR(pushed) any_pushed
  FROM d GROUP BY 1,2,3),
r AS (SELECT call_date, COUNT(DISTINCT journey_digest) n_def FROM d GROUP BY 1)
SELECT call_date, FORMAT_DATE('%a', call_date) dow, r.n_def,
  COUNT(DISTINCT IF(canc, train, NULL)) canc,
  COUNT(DISTINCT IF(canc AND et_pushed, train, NULL)) et_pushed,
  COUNT(DISTINCT IF(canc AND any_pushed, train, NULL)) any_pushed
FROM s JOIN r USING (call_date) GROUP BY 1,2,3 ORDER BY 1
