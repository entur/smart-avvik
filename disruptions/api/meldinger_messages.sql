-- /meldinger-api/messages: the replay INPUTS for the same subjects, server-side only.
--
-- The split from meldinger_decisions.sql is a PRIVACY BOUNDARY, not a convenience (KTD10). That
-- file feeds the public card model, so it must never be able to project `message_input`: the stored
-- input carries the traveller's origin, destination and departure time together -- the quasi-
-- identifying trio /historikk-api redacts -- and a query that does not NAME a column cannot leak it,
-- however the model above it drifts. This file names it, and only the server ever reads the rows:
-- what leaves the process is the wording generated FROM the input, never the input.
--
-- KEEP THIS FILE AND meldinger_decisions.sql IN STEP. The `scanned` CTE, the `subjects` grain,
-- the replay pick, the outcomes join, the WHERE and the ORDER BY/LIMIT below are the same logic,
-- deliberately duplicated rather than shared, because sharing would mean one query that projects the
-- input. The two responses are aligned POSITION BY POSITION by the caller -- the page keys its list
-- on position, since nothing served identifies a decision uniquely -- so any divergence in the
-- subject set or the ordering silently attaches one train's wording to another train's card. That is
-- the failure this duplication buys, and it is why the ORDER BY here is the full subject key rather
-- than departure_time alone.
--
-- What this file deliberately does NOT do: fetch a verdict, the summary/description text, or any of
-- the per-cycle figures. It resolves a subject list and one input per subject. The verdict columns
-- are still joined because @verdict and @resolved_only narrow the subject list, and a list narrowed
-- differently from /decisions' would be the misalignment above.
--
-- See meldinger_decisions.sql's header for the axis argument (why DEPARTURE time and not
-- `decided_at`), why the DERIVED DATE(decided_at) pruning bound is load-bearing for the aggregates
-- rather than cosmetic, why the outcomes bound stays +/-1 day on `call_date`, and why the emptiness
-- test is IFNULL(ARRAY_LENGTH(...), 0) = 0 and never the bare form.
WITH scanned AS (
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    line, mode, departure_time,
    decided_at, alertworthy, pushed, message_input, prompt_version
  FROM `disruptions.varsel_decisions`
  WHERE DATE(decided_at) BETWEEN DATE_SUB(@day, INTERVAL 1 DAY) AND DATE_ADD(@day, INTERVAL 1 DAY)
    AND departure_time >= @from_ts AND departure_time < @to_ts
    AND (@operator IS NULL OR operator = @operator)
    AND (@line     IS NULL OR line     = @line)
    AND (IFNULL(ARRAY_LENGTH(@sx_summaries), 0) = 0 OR summary IN UNNEST(@sx_summaries))
    AND (IFNULL(ARRAY_LENGTH(@from_place_refs), 0) = 0 OR from_stop IN UNNEST(@from_place_refs))
    AND (IFNULL(ARRAY_LENGTH(@to_place_refs),   0) = 0 OR to_stop   IN UNNEST(@to_place_refs))
    AND (NOT @sx_only OR NOT STARTS_WITH(situation_number, 'KIX:'))
),
subjects AS (
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    ANY_VALUE(line)     AS line,
    ANY_VALUE(mode)     AS mode,
    MAX(departure_time) AS departure_time,
    LOGICAL_OR(alertworthy)             AS ever_alerted,
    COALESCE(LOGICAL_OR(pushed), FALSE) AS pushed_any,
    -- The SAME pick as meldinger_decisions.sql, with the input carried alongside: the FIRST cycle
    -- that actually delivered a push and recorded an input. `pushed` is the recorded output of
    -- varsel's four delivery filters, written after delivery -- see that file for why it is looked
    -- up rather than re-derived. ONE aggregate, so `message_input`, `would_send_at` and
    -- `prompt_version` cannot come from three different cycles -- the wording would then be written
    -- from one cycle's numbers and stated under another cycle's time.
    --
    -- NULL here is the state R7 names: no delivered cycle recorded an input, so there is nothing to
    -- replay. The caller reports that distinctly and spends no generation budget on it -- it is not
    -- "no message", and it is not an error. On this axis it covers the `sendt_uten_input` card as
    -- well as every card never sent at all; /decisions' `send_state` is what separates them.
    ARRAY_AGG(IF(pushed AND message_input IS NOT NULL, STRUCT(
                message_input  AS message_input,
                decided_at     AS would_send_at,
                prompt_version AS prompt_version), NULL)
              IGNORE NULLS ORDER BY decided_at LIMIT 1)[SAFE_OFFSET(0)] AS replay
  FROM scanned
  GROUP BY journey_digest, operator, jkey, call_date, stop_order, situation_number
)
SELECT
  -- The subject key. Not a public projection: the caller matches these against /decisions' rows by
  -- position and uses journey_digest for the audit-log record -- unhashed, because it is already
  -- llm_message.key_digest's output and hashing it again would leave replay records unable to join
  -- the live event="llm_message" trail.
  s.journey_digest, s.operator, s.jkey, s.call_date, s.stop_order, s.situation_number,
  s.line, s.mode, s.departure_time,
  s.replay.message_input,
  s.replay.would_send_at,
  s.replay.prompt_version
FROM subjects AS s
LEFT JOIN `disruptions.varsel_decision_outcomes` AS o
  ON  o.call_date BETWEEN DATE_SUB(@day, INTERVAL 1 DAY) AND DATE_ADD(@day, INTERVAL 1 DAY)
  AND o.journey_digest   = s.journey_digest
  AND o.operator         = s.operator
  AND o.jkey             = s.jkey
  AND o.call_date        = s.call_date
  AND o.stop_order       = s.stop_order
  AND o.situation_number = s.situation_number
WHERE (@verdict IS NULL OR o.verdict = @verdict)
  AND (NOT @alerted_only OR s.ever_alerted)
  AND (NOT @pushed_only  OR s.pushed_any)
  AND (NOT @resolved_only OR o.actual_trouble IS NOT NULL)
ORDER BY s.departure_time ASC,
         s.situation_number, s.operator, s.jkey, s.call_date, s.stop_order, s.journey_digest
LIMIT @limit
