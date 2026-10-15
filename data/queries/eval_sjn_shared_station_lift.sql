-- KIX-39: do Vy's infrastructure messages carry signal for SJ Norge trains at the stations they
-- share? An SJN call is "flagged" when an NSB situation of an infrastructure category names its
-- stop place and was published 0..180 min before the call. Compared on the SJN holdout against
-- (a) unflagged calls at the same stop places and (b) what the zero-shot model already predicted,
-- at the 30-min anchoring and at 'plan' (no observable state: the early-warning regime an SX
-- would be for). Read-only; prints one row per (lead_bucket, flagged).
WITH cat AS (
  SELECT summary, description, ANY_VALUE(category) AS category
  FROM `disruptions.categories` GROUP BY 1, 2
),
infra AS (
  SELECT d.situationNumber, SAFE.TIMESTAMP(d.creationTime) AS onset_ts, sp
  FROM `disruptions.disruptions` AS d
  JOIN cat AS c
    ON IFNULL(d.onset_summary, '') = IFNULL(c.summary, '')
   AND IFNULL(d.onset_description, '') = IFNULL(c.description, '')
  , UNNEST(d.affected_stops) AS sp
  WHERE d.participantRef = 'NSB'
    AND c.category LIKE 'infrastruktur/%'
    AND DATE(SAFE.TIMESTAMP(d.creationTime)) >= DATE '2026-03-31'
),
calls AS (
  SELECT jkey, stop_order, call_date, stop_place, COALESCE(aimed_dep, aimed_arr) AS call_ts
  FROM `disruptions.sjn_calls` WHERE call_date >= DATE '2026-04-01'
),
flag AS (
  SELECT c.jkey, c.stop_order, c.call_date, LOGICAL_OR(i.situationNumber IS NOT NULL) AS flagged
  FROM calls AS c
  LEFT JOIN infra AS i
    ON i.sp = c.stop_place
   AND i.onset_ts BETWEEN TIMESTAMP_SUB(c.call_ts, INTERVAL 180 MINUTE) AND c.call_ts
  GROUP BY 1, 2, 3
),
shared AS (SELECT DISTINCT sp FROM infra)
SELECT h.lead_bucket, f.flagged, COUNT(*) AS n,
  ROUND(AVG(CAST(h.call_cancelled OR h.dep_delay_pos >= 10 AS INT64)), 4) AS impact_rate,
  ROUND(AVG(h.p_cancel + (1 - h.p_cancel) * h.p_dep10), 4) AS mean_p_impact,
  ROUND(AVG(CAST(h.call_cancelled AS INT64)), 4) AS cancel_rate,
  ROUND(AVG(h.p_cancel), 4) AS mean_p_cancel
FROM `disruptions.sjn_horizon_scores` AS h
JOIN flag AS f USING (jkey, stop_order, call_date)
JOIN shared AS s ON s.sp = h.stop_place
WHERE h.pop = 'SJN' AND h.lead_bucket IN ('30', 'plan')
  AND NOT h.is_terminal AND (h.call_cancelled OR h.dep_delay_pos IS NOT NULL)
GROUP BY 1, 2 ORDER BY 1, 2;
