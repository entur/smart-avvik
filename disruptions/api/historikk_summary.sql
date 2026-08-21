-- /historikk-api/summary: how the alerting has actually performed over a window.
--
-- Counts are over LABELLED decisions only. `unknown` and `pending` are reported alongside rather
-- than folded into a verdict bucket, so a reader can see how much of the window the numbers
-- actually cover — a precision figure computed over 20 % of the decisions is a different claim
-- from one computed over 95 %, and the page must be able to say which it is showing.
--
-- precision = hit / (hit + false_alarm)   — of the alerts we sent, how many were right.
-- recall    = hit / (hit + miss)          — of the trouble we could see, how much we caught.
-- lead      = departure - first alert     — how much warning the traveller got, in minutes,
--                                           counted per TRAIN rather than per situation.
--
-- What lead is measured AGAINST: the AIMED departure. So it is exact for a cancellation, where no
-- train ever leaves, and UNDERSTATES the usable warning for a delayed one. It goes negative when
-- the first alert lands after the aimed departure, and that is not automatically too late: an
-- alert is dropped by the train's PREDICTED departure (varsel's `catchable_after`), so one running
-- 45 min late is still worth sending at aimed+10. Measured 2026-08-11 over 14 days of prd, 4 of
-- 156 trains had a negative lead, and most still reached the traveller before the train actually
-- left. They stay in the median: dropping them would flatter it, and the one that was a
-- cancellation really was too late -- by two minutes, on a call the app had no chance to score
-- earlier, so there is nothing behind it to fix.
--
-- What recall here does NOT mean: true recall over all disruptions. The models only score calls an
-- SX message flagged, and only on journeys somebody registered, so trouble that never produced an
-- SX message is invisible to this table. It is recall among SX-flagged calls on registered
-- journeys. The UI states this next to the number.
--
-- The same window is ALSO reported split two ways, because one figure cannot answer both questions
-- the page is asked. A decision on a call SIRI-ET had already declared cancelled relayed a fact:
-- both probabilities were 1.0 by construction, and the verdict is a gimme for the models but a real
-- hit for the traveller, who is not carrying the ET feed on their phone. So:
--   * blended  — did the app tell the truth? Includes the relayed ones, and should.
--   * predicted — did we see it coming? Excludes them.
-- Neither is quoted alone. The blended figure moves with the MIX of the populations, so it can
-- swing with nothing about the app having changed; the page shows the mix beside it for that reason.
--
-- A THIRD PROVENANCE SINCE 2026-08-20: a decision about a train NO SX MESSAGE NAMED AT ALL, scored
-- from real-time data alone (see the no-SX branch in predict_calls.sql). It has to be its own
-- segment for the same reason the relayed one does, in the opposite direction: those decisions are
-- taken with strictly less information than any other row here, against a base rate ~12x lower, and
-- through their own calibration layers. Folded into `predicted` they would drag a number that is
-- supposed to answer "how good are the models when an operator has published something".
--
-- THE DISCRIMINATOR IS THE situationNumber PREFIX, not a new column, and that is a deliberate
-- avoidance: `varsel_decision_outcomes` already carries situation_number, while ADDING a column to
-- it breaks the registered scheduled query until it is re-registered (measured 2026-08-05:
-- "Inserted row has wrong column count; Has 35, expected 36"). The synthetic ids are the same
-- `KIX:` scheme the ET relay established, one namespace per provenance.
--
-- WHICH FILTERS REACH THIS FILE, and why it is not all of them. operator, line, from_place and
-- to_place narrow the POPULATION -- which trains, on whose registered journeys -- so the rates here
-- are computed over exactly the decisions the list beside them is showing, and both answer the same
-- question about the same slice. verdict, alerted_only and resolved_only do NOT reach it, because
-- they narrow by OUTCOME: filtering to hits would make precision read 100 %. The distinction is the
-- whole reason api.ts hands /summary a different subset of the filter object than /decisions.
--
-- Resolution order matters where they overlap: `relayed` wins. A no-SX call can be declared
-- cancelled by ET between the cycle that scored it and the cycle the verdict judges, at which point
-- what the traveller got WAS a relayed fact, whatever produced the first row about that train.
WITH labelled AS (
  SELECT
    verdict, alert_lead_min, actual_trouble, et_cancelled,
    -- Derived once here so the eleven COUNTIFs below cannot each spell the rule slightly
    -- differently. NULL et_cancelled still means "unsegmented", so it is tested first.
    CASE
      WHEN et_cancelled IS NULL                                     THEN NULL
      WHEN et_cancelled IS TRUE                                     THEN 'relayed'
      WHEN STARTS_WITH(situation_number, 'KIX:NoSituation:')        THEN 'no_sx'
      ELSE 'predicted'
    END AS provenance,
    -- Same split as historikk_decisions.sql: an unresolved row inside the labelling job's 10-day
    -- retry window is still waiting, not permanently unknown. Keeping the two in step matters --
    -- the page shows this count next to the per-row state, and they must tell the same story.
    actual_trouble IS NULL
      AND call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY) AS still_retrying
  FROM `disruptions.varsel_decision_outcomes`
  WHERE call_date BETWEEN @from_date AND @to_date
    AND (@operator IS NULL OR operator = @operator)
    AND (@line     IS NULL OR line     = @line)
    AND (@from_place IS NULL OR from_stop = @from_place)
    AND (@to_place   IS NULL OR to_stop   = @to_place)
),
-- Unlabelled groups, split by whether a verdict is still possible. A decision with no journey key
-- can never be joined to a realized call -- the labelling job skips it permanently -- so it is
-- already unknown, not merely waiting. Counting it as pending would understate how much of the
-- window can never be resolved.
unlabelled AS (
  SELECT COUNTIF(g.jkey IS NOT NULL) AS n_pending,
         COUNTIF(g.jkey IS NULL)     AS n_unjoinable
  FROM `disruptions.varsel_decision_groups` AS g
  WHERE g.call_date BETWEEN @from_date AND @to_date
    AND (@operator IS NULL OR g.operator = @operator)
    AND (@line     IS NULL OR g.line     = @line)
    AND (@from_place IS NULL OR g.from_stop = @from_place)
    AND (@to_place   IS NULL OR g.to_stop   = @to_place)
    AND NOT EXISTS (
      SELECT 1
      FROM `disruptions.varsel_decision_outcomes` AS o
      WHERE o.journey_digest   = g.journey_digest
        AND o.operator         = g.operator
        AND o.jkey             = g.jkey
        AND o.call_date        = g.call_date
        AND o.stop_order       = g.stop_order
        AND o.situation_number = g.situation_number
    )
),
-- Lead time is the one figure here that belongs to the TRAIN, not to the situation, and the rest
-- of this file deliberately does not. Every other number answers "of the decisions we took, how
-- many were right", and a decision is per situation. This one answers "how much warning did the
-- traveller get", and the traveller gets ONE alert per train however many SX messages sit behind
-- it -- varsel.one_alert_per_train collapses them for both the push and the card.
--
-- Measured 2026-08-11 over 14 days of prd, the difference is not cosmetic: 514 hit rows are 156
-- trains, 136 of which carry more than one situation. Operators publish repeatedly about one
-- disruption, and a duplicate message that lands after the train has gone contributes a row with a
-- NEGATIVE lead beside the row that alerted two hours early. Taking the median over rows therefore
-- UNDERSTATES the warning: 89 min per situation against 145 per train, and the predicted segment
-- reads 6 against 15. The four rows that looked like "we alerted after departure on a cancelled
-- train" were three trains, two of which had been alerted 117 and 109 min ahead under a different
-- situation about the same cancellation.
--
-- Read the predicted segment's median with its denominator in view: those 156 trains are 133
-- relayed against 19 predicted, so the figure the threshold is judged on rests on a couple of
-- dozen trains a fortnight. That is why n_hit_trains is served beside it.
hits_per_train AS (
  SELECT
    -- The alert the traveller actually got -- the earliest one -- and the segment IT belonged to,
    -- rather than a LOGICAL_OR across situations that could file a train under a segment no alert
    -- of it was ever in. NULL leads sort last under DESC, so one can only win if they all are.
    ARRAY_AGG(STRUCT(alert_lead_min, CASE
      WHEN et_cancelled IS NULL                              THEN NULL
      WHEN et_cancelled IS TRUE                              THEN 'relayed'
      WHEN STARTS_WITH(situation_number, 'KIX:NoSituation:') THEN 'no_sx'
      ELSE 'predicted'
    END AS provenance) ORDER BY alert_lead_min DESC LIMIT 1)[OFFSET(0)]
      AS first_alert
  FROM `disruptions.varsel_decision_outcomes`
  WHERE call_date BETWEEN @from_date AND @to_date
    AND (@operator IS NULL OR operator = @operator)
    AND (@line     IS NULL OR line     = @line)
    AND (@from_place IS NULL OR from_stop = @from_place)
    AND (@to_place   IS NULL OR to_stop   = @to_place)
    AND verdict = 'hit'
  GROUP BY
    -- varsel.one_alert_per_train's key, in its own order of preference: the boarding call, then
    -- (operator, line, departure_time) when no journey ref names the train, and failing both the
    -- row stays on its own -- an alert identifying no train is never merged with another.
    -- call_date is in the first form because bus ServiceJourney jkeys repeat every operating day;
    -- rail's DatedServiceJourney refs are date-scoped already, so it costs nothing there.
    CASE
      WHEN jkey IS NOT NULL THEN FORMAT('call|%t|%t|%t|%t|%t',
                                        operator, jkey, call_date, stop_place, stop_order)
      WHEN line IS NOT NULL AND departure_time IS NOT NULL
        THEN FORMAT('line|%t|%t|%t', operator, line, departure_time)
      ELSE FORMAT('sit|%t', situation_number)
    END
)
SELECT
  COUNTIF(verdict = 'hit')            AS n_hit,
  COUNTIF(verdict = 'false_alarm')    AS n_false_alarm,
  COUNTIF(verdict = 'miss')           AS n_miss,
  COUNTIF(verdict = 'correct_reject') AS n_correct_reject,
  -- Permanently unknown: labelled rows past the retry window, plus unlabelled rows with no join key.
  COUNTIF(verdict IS NULL AND NOT still_retrying)
    + (SELECT n_unjoinable FROM unlabelled) AS n_unknown,
  -- Still resolvable: never labelled yet, plus labelled-but-unresolved inside the retry window.
  COUNTIF(verdict IS NULL AND still_retrying)
    + (SELECT n_pending FROM unlabelled)    AS n_pending,
  SAFE_DIVIDE(
    COUNTIF(verdict = 'hit'),
    COUNTIF(verdict = 'hit') + COUNTIF(verdict = 'false_alarm')
  ) AS precision,
  SAFE_DIVIDE(
    COUNTIF(verdict = 'hit'),
    COUNTIF(verdict = 'hit') + COUNTIF(verdict = 'miss')
  ) AS recall,
  -- Lead time on the trains we got right: how much warning a traveller who needed it actually got.
  -- Median rather than mean — a single alert fired days ahead would otherwise flatter it. Counted
  -- per TRAIN (see hits_per_train), so these two are the only figures here on that grain; the UI
  -- says "avganger" beside them where the rest of the page says "varsler".
  -- IGNORE NULLS goes after the quantile count, not after the expression: BigQuery's signature is
  -- APPROX_QUANTILES(expression, number [{IGNORE|RESPECT} NULLS]), and the other order does not
  -- parse -- which would have made this whole endpoint a permanent 503.
  (SELECT APPROX_QUANTILES(first_alert.alert_lead_min, 2 IGNORE NULLS)[SAFE_OFFSET(1)]
   FROM hits_per_train) AS median_lead_min_on_hits,
  (SELECT COUNTIF(first_alert.alert_lead_min >= 30)
   FROM hits_per_train) AS n_hit_with_30min_notice,
  -- The denominator those two are over. Without it the page would show "5" beside a "59" drawn
  -- from the same segment and mean two different things by them.
  (SELECT COUNT(*) FROM hits_per_train) AS n_hit_trains,

  -- ---- the same rows, segmented ---------------------------------------------------------------
  -- Nothing is dropped: every labelled verdict above lands in exactly one of the three buckets
  -- below. `IS FALSE` / `IS TRUE` rather than `=` so the NULLs fall through to n_unsegmented
  -- instead of silently vanishing from both segments -- `et_cancelled = FALSE` is NULL, not TRUE,
  -- for a row that predates the column, and COUNTIF counts only TRUE.
  --
  -- MODEL SKILL: the trouble we actually forecast. What a threshold should be tuned on.
  COUNTIF(provenance = 'predicted' AND verdict = 'hit')            AS n_hit_predicted,
  COUNTIF(provenance = 'predicted' AND verdict = 'false_alarm')    AS n_false_alarm_predicted,
  COUNTIF(provenance = 'predicted' AND verdict = 'miss')           AS n_miss_predicted,
  COUNTIF(provenance = 'predicted' AND verdict = 'correct_reject') AS n_correct_reject_predicted,
  SAFE_DIVIDE(
    COUNTIF(provenance = 'predicted' AND verdict = 'hit'),
    COUNTIF(provenance = 'predicted' AND verdict IN ('hit', 'false_alarm'))
  ) AS precision_predicted,
  SAFE_DIVIDE(
    COUNTIF(provenance = 'predicted' AND verdict = 'hit'),
    COUNTIF(provenance = 'predicted' AND verdict IN ('hit', 'miss'))
  ) AS recall_predicted,
  -- Lead time splits harder than either rate does, which is why it is segmented at all: measured
  -- 2026-08-11 over 14 days of prd, the median train here gave 15 minutes of warning against the
  -- relayed segment's 177. The blended figure is the relayed one wearing the whole page's name.
  (SELECT APPROX_QUANTILES(
     IF(first_alert.provenance = 'predicted', first_alert.alert_lead_min, NULL), 2 IGNORE NULLS
   )[SAFE_OFFSET(1)] FROM hits_per_train) AS median_lead_min_on_hits_predicted,
  (SELECT COUNTIF(first_alert.provenance = 'predicted' AND first_alert.alert_lead_min >= 30)
   FROM hits_per_train) AS n_hit_with_30min_notice_predicted,
  (SELECT COUNTIF(first_alert.provenance = 'predicted') FROM hits_per_train) AS n_hit_trains_predicted,

  -- RELAYED: ET had already declared the cancellation when we decided. Precision here should sit at
  -- or near 1 — we are repeating a published fact — and anything below it is worth looking at: a
  -- reinstated train, which now gets a flat "Innstilt" where the old wording hedged. A miss in this
  -- segment is not a model failure at all but a plumbing one: the feed told us and we said nothing.
  COUNTIF(provenance = 'relayed' AND verdict = 'hit')            AS n_hit_relayed,
  COUNTIF(provenance = 'relayed' AND verdict = 'false_alarm')    AS n_false_alarm_relayed,
  COUNTIF(provenance = 'relayed' AND verdict = 'miss')           AS n_miss_relayed,
  COUNTIF(provenance = 'relayed' AND verdict = 'correct_reject') AS n_correct_reject_relayed,
  SAFE_DIVIDE(
    COUNTIF(provenance = 'relayed' AND verdict = 'hit'),
    COUNTIF(provenance = 'relayed' AND verdict IN ('hit', 'false_alarm'))
  ) AS precision_relayed,
  SAFE_DIVIDE(
    COUNTIF(provenance = 'relayed' AND verdict = 'hit'),
    COUNTIF(provenance = 'relayed' AND verdict IN ('hit', 'miss'))
  ) AS recall_relayed,
  -- Hours, not minutes, and legitimately so: a situation is scored over a 12 h window, so a
  -- cancellation ET declares the evening before is alerted on as soon as it enters that window.
  (SELECT APPROX_QUANTILES(
     IF(first_alert.provenance = 'relayed', first_alert.alert_lead_min, NULL), 2 IGNORE NULLS
   )[SAFE_OFFSET(1)] FROM hits_per_train) AS median_lead_min_on_hits_relayed,
  (SELECT COUNTIF(first_alert.provenance = 'relayed' AND first_alert.alert_lead_min >= 30)
   FROM hits_per_train) AS n_hit_with_30min_notice_relayed,
  (SELECT COUNTIF(first_alert.provenance = 'relayed') FROM hits_per_train) AS n_hit_trains_relayed,

  -- NO SX AT ALL: nobody had published anything about this train when we decided, and the numbers
  -- came from the models reading real-time data alone. Precision here is the honest answer to "can
  -- we warn people the operator has not warned?", and it is the number the no-SX alert gate is
  -- tuned against. Expect the volume to be small and the lead times long: these rows are scored
  -- over the same 12 h forward window as everything else but without a message to wait for, so
  -- nothing gates how early they can fire.
  COUNTIF(provenance = 'no_sx' AND verdict = 'hit')            AS n_hit_no_sx,
  COUNTIF(provenance = 'no_sx' AND verdict = 'false_alarm')    AS n_false_alarm_no_sx,
  COUNTIF(provenance = 'no_sx' AND verdict = 'miss')           AS n_miss_no_sx,
  COUNTIF(provenance = 'no_sx' AND verdict = 'correct_reject') AS n_correct_reject_no_sx,
  SAFE_DIVIDE(
    COUNTIF(provenance = 'no_sx' AND verdict = 'hit'),
    COUNTIF(provenance = 'no_sx' AND verdict IN ('hit', 'false_alarm'))
  ) AS precision_no_sx,
  SAFE_DIVIDE(
    COUNTIF(provenance = 'no_sx' AND verdict = 'hit'),
    COUNTIF(provenance = 'no_sx' AND verdict IN ('hit', 'miss'))
  ) AS recall_no_sx,
  (SELECT APPROX_QUANTILES(
     IF(first_alert.provenance = 'no_sx', first_alert.alert_lead_min, NULL), 2 IGNORE NULLS
   )[SAFE_OFFSET(1)] FROM hits_per_train) AS median_lead_min_on_hits_no_sx,
  (SELECT COUNTIF(first_alert.provenance = 'no_sx' AND first_alert.alert_lead_min >= 30)
   FROM hits_per_train) AS n_hit_with_30min_notice_no_sx,
  (SELECT COUNTIF(first_alert.provenance = 'no_sx') FROM hits_per_train) AS n_hit_trains_no_sx,

  -- Decided before the flag existed (2026-08-05), so which segment they belong to is unrecoverable.
  -- Reported, not assigned: defaulting them to "predicted" would be right for most and would hide
  -- exactly the rows worth finding — the ones where ET had already published and we stayed quiet.
  COUNTIF(et_cancelled IS NULL AND verdict IS NOT NULL) AS n_unsegmented
FROM labelled
