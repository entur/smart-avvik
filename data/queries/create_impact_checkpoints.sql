-- Checkpoint table for the DURING-disruption re-prediction model. For each
-- impacted situation we emit one row per re-prediction checkpoint t (elapsed
-- minutes since onset) while the impact episode is still ongoing (t <
-- impact_minutes). The label is the REMAINING impact (impact_minutes - t) -- the
-- passenger message "trains impacted for ~Y more min", refreshed as the
-- disruption unfolds.
--
-- LEAKAGE BOUNDARY (anchored at the checkpoint cp_ts = onset + t, not at onset):
--   * ET features use only calls with aimed_dep < cp_ts, and the knowable_delay
--     transform LEAST(final_delay, minutes overdue AT cp_ts) -- so a train still
--     waiting at cp_ts contributes only its lateness-so-far.
--   * reopened_by_t / minutes_since_reopen are observed events (reopen_ts <= cp_ts).
--   * raw reopen_minutes and impact_minutes are NOT exposed as features (they are
--     future knowledge until they happen); only the derived safe versions are.
-- Features (onset-time) come later by joining training_initial; here we add only
-- the time-varying during-disruption signal + the leakage-safe reopen state.

DECLARE checkpoints ARRAY<INT64> DEFAULT [10,20,30,45,60,90,120,180,240,300,360];

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.impact_checkpoints` AS
WITH sit AS (
  SELECT situationNumber, onset_ts, reopen_minutes, impact_minutes, affected_stops
  FROM `disruptions.disruptions_initial`
  WHERE impact_minutes > 10            -- need room for >=1 checkpoint
),
cp AS (
  SELECT s.situationNumber, s.onset_ts, s.reopen_minutes, s.impact_minutes,
         s.affected_stops, t,
         TIMESTAMP_ADD(s.onset_ts, INTERVAL t MINUTE) AS cp_ts
  FROM sit AS s, UNNEST(checkpoints) AS t
  WHERE t < s.impact_minutes
),
calls AS (
  SELECT
    c.situationNumber, c.t, c.cp_ts,
    r.aimed_dep,
    r.call_cancelled,
    LEAST(r.dep_delay_min, TIMESTAMP_DIFF(c.cp_ts, r.aimed_dep, MINUTE)) AS kdelay
  FROM cp AS c
  JOIN `disruptions.rail_calls` AS r
    ON r.stop_place IN UNNEST(c.affected_stops)
   AND r.aimed_dep >= c.onset_ts
   AND r.aimed_dep <  c.cp_ts
),
agg AS (
  SELECT
    situationNumber, t, cp_ts,
    COUNTIF(call_cancelled OR kdelay >= 15)                       AS cum_impacted,
    COUNTIF(call_cancelled)                                       AS cum_cancel,
    MAX(IF(call_cancelled, NULL, kdelay))                         AS cum_maxdelay,
    COUNTIF((call_cancelled OR kdelay >= 15)
            AND aimed_dep >= TIMESTAMP_SUB(cp_ts, INTERVAL 30 MINUTE)) AS rec_impacted,
    MAX(IF((call_cancelled OR kdelay >= 15)
           AND aimed_dep >= TIMESTAMP_SUB(cp_ts, INTERVAL 30 MINUTE),
           kdelay, NULL))                                         AS rec_maxdelay,
    MAX(IF(call_cancelled OR kdelay >= 15, aimed_dep, NULL))      AS last_impact_ts
  FROM calls
  GROUP BY situationNumber, t, cp_ts
)
SELECT
  c.situationNumber,
  c.t                                              AS elapsed_min,
  (c.impact_minutes - c.t)                         AS remaining_minutes,   -- LABEL
  (c.t >= c.reopen_minutes)                        AS reopened_by_t,
  GREATEST(c.t - c.reopen_minutes, 0)              AS minutes_since_reopen,
  IFNULL(a.cum_impacted, 0)                        AS cum_impacted,
  IFNULL(a.cum_cancel, 0)                          AS cum_cancel,
  IFNULL(a.cum_maxdelay, 0)                        AS cum_maxdelay,
  IFNULL(a.rec_impacted, 0)                        AS rec_impacted,
  IFNULL(a.rec_maxdelay, 0)                        AS rec_maxdelay,
  IFNULL(TIMESTAMP_DIFF(c.cp_ts, a.last_impact_ts, MINUTE), 999) AS mins_since_last_impact
FROM cp AS c
LEFT JOIN agg AS a USING (situationNumber, t);
