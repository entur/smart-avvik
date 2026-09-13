-- /meldinger-api/summary: how the alerting performed over the departures in one window.
--
-- The same figures, the same arithmetic and the same provenance split as historikk_summary.sql,
-- over a population selected on `departure_time` instead of `call_date`. Read that file's header
-- for the meaning of precision, recall and lead; only what differs is written out here.
--
-- WHAT DIFFERS, AND WHY IT MATTERS ON THIS PAGE MORE THAN ON /historikk:
--
-- 1. THE POPULATION IS "SUBJECTS DEPARTING IN THE WINDOW", resolved from `varsel_decisions` itself
--    and then LEFT JOINed to the verdicts, rather than read off `varsel_decision_outcomes`. A
--    subject enters because its boarding call is aimed to depart between @from_ts and @to_ts. That
--    is the same population the card list beside these rates shows, which is the only property that
--    makes the two readable together. A row with no `departure_time` cannot be placed on this axis
--    and drops out of both, exactly as it does there.
--
-- 2. A SAME-DAY WINDOW IS ALMOST ENTIRELY UNLABELLED. Verdicts arrive from the 04:00 UTC labelling
--    job the following night, so on this page's default view precision, recall and lead are computed
--    over a small labelled subset while `n_pending` holds nearly everything. /historikk avoids this
--    by defaulting to 14 days on `call_date`; this page cannot, so it reports the split instead of
--    showing confident-looking rates over a handful of rows. `n_subjects` and `n_labelled` are here
--    for exactly that: they are the denominator the page has to state, not diagnostics.
--
-- 3. EVERY FIGURE HERE IS WHOLE-SUBJECT, AND SINCE 2026-08-22 SO ARE THE CARDS'. On the old
--    `decided_at` axis a subject's cycles straddled the window, so the cards carried window-scoped
--    per-cycle figures beside these whole-subject rates and the API had to say so in every field
--    description. The departure axis puts a subject wholly in or wholly out, so the two grains
--    collapsed into one and that caveat is gone from both sides.
--
-- WHICH FILTERS REACH THIS FILE, and why not all of them. The day, the time range, operator, line,
-- from_place, to_place and sx_summary narrow the POPULATION -- which cycles, which trains, on whose
-- registered journeys, about which published disruption -- so these rates describe exactly the
-- slice the list beside them is showing. verdict,
-- alerted_only, pushed_only and resolved_only do NOT reach it, because they narrow by OUTCOME or by
-- what we chose to do: filtering to hits would make precision read 100 %, and filtering to the
-- subjects a push went out for would do the same to it from the other side. sx_only is not accepted
-- either, for the same reason /historikk does not accept it here.
--
-- Provenance resolution order matters where the buckets overlap, and it is the same order as
-- /historikk's: NULL first (its own bucket), then `relayed`, then `no_sx`, else `predicted`. A no-SX
-- call can be declared cancelled by ET between the cycle that scored it and the cycle the verdict
-- judges, at which point what the traveller got WAS a relayed fact. The discriminator for `no_sx` is
-- the situationNumber prefix rather than a column, and that is a deliberate avoidance: ADDING a
-- column to varsel_decision_outcomes breaks the registered varsel-label-daily scheduled query until
-- it is re-registered in both projects.
--
-- See meldinger_decisions.sql for the pruning argument, the measured +/-1 day outcomes bound and
-- the IFNULL(ARRAY_LENGTH(...)) rule.
WITH scanned AS (
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    summary AS cycle_summary, line, stop_place, departure_time
  FROM `disruptions.varsel_decisions`
  -- The DERIVED partition bound, then the window itself. See meldinger_decisions.sql's header:
  -- the pruning predicate is no longer the window predicate, and a too-narrow one drops cycles
  -- silently while reporting the loss as a cheaper query.
  WHERE DATE(decided_at) BETWEEN DATE_SUB(@day, INTERVAL 1 DAY) AND DATE_ADD(@day, INTERVAL 1 DAY)
    AND departure_time >= @from_ts AND departure_time < @to_ts
    AND (@operator IS NULL OR operator = @operator)
    AND (@line     IS NULL OR line     = @line)
    -- The same population filter meldinger_decisions.sql applies; see its note.
    AND (@channel  IS NULL OR channel  = @channel)
    -- The SIRI-SX population filter is applied after aggregation below so a matching subject keeps
    -- all cycles contributing to its whole-subject figures.
    AND (IFNULL(ARRAY_LENGTH(@from_place_refs), 0) = 0 OR from_stop IN UNNEST(@from_place_refs))
    AND (IFNULL(ARRAY_LENGTH(@to_place_refs),   0) = 0 OR to_stop   IN UNNEST(@to_place_refs))
),
subjects AS (
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    ANY_VALUE(line)       AS line,
    ANY_VALUE(stop_place) AS stop_place,
    MAX(departure_time)   AS departure_time
  FROM scanned
  GROUP BY journey_digest, operator, jkey, call_date, stop_order, situation_number
  HAVING IFNULL(ARRAY_LENGTH(@sx_summaries), 0) = 0
      OR COUNTIF(cycle_summary IN UNNEST(@sx_summaries)) > 0
),
-- The window's subjects, each carrying whatever the nightly job has resolved about it. A LEFT JOIN
-- rather than /historikk's UNION of two sources: there the two sides are two tables holding
-- different subjects, here the subject list comes from one place and the verdict is an attribute.
joined AS (
  SELECT
    s.operator, s.jkey, s.call_date, s.stop_place, s.stop_order, s.line, s.departure_time,
    s.situation_number,
    -- A matched row implies o.call_date = s.call_date, and s.call_date is non-NULL for any row that
    -- could match, so this is a reliable "did the join find a verdict row" flag.
    o.call_date IS NOT NULL AS is_labelled,
    o.verdict, o.actual_trouble, o.et_cancelled, o.alert_lead_min
  FROM subjects AS s
  LEFT JOIN `disruptions.varsel_decision_outcomes` AS o
    ON  o.call_date BETWEEN DATE_SUB(@day, INTERVAL 1 DAY) AND DATE_ADD(@day, INTERVAL 1 DAY)
    AND o.journey_digest   = s.journey_digest
    AND o.operator         = s.operator
    AND o.jkey             = s.jkey
    AND o.call_date        = s.call_date
    AND o.stop_order       = s.stop_order
    AND o.situation_number = s.situation_number
),
labelled AS (
  SELECT
    verdict, alert_lead_min, actual_trouble, et_cancelled,
    -- Derived once so the many COUNTIFs below cannot each spell the rule slightly differently. NULL
    -- et_cancelled still means "unsegmented", so it is tested FIRST -- a row written before the flag
    -- existed (2026-08-05) belongs to no segment, and defaulting it to 'predicted' would be right
    -- for most and would bury exactly the rows worth finding.
    CASE
      WHEN et_cancelled IS NULL                              THEN NULL
      WHEN et_cancelled IS TRUE                              THEN 'relayed'
      WHEN STARTS_WITH(situation_number, 'KIX:NoSituation:') THEN 'no_sx'
      ELSE 'predicted'
    END AS provenance,
    -- Same split as meldinger_decisions.sql's outcome_state: an unresolved row inside the
    -- labelling job's 10-day retry window is still waiting, not permanently unknown. Keeping the two
    -- in step matters -- the page shows this count next to the per-row state.
    actual_trouble IS NULL
      AND call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY) AS still_retrying
  FROM joined
  WHERE is_labelled
),
-- Subjects the nightly job has not written a row for at all, split by whether a verdict is still
-- possible. A decision with no journey key can never be joined to a realized call -- the labelling
-- job skips it permanently -- so it is already unknown, not merely waiting.
unlabelled AS (
  SELECT COUNTIF(jkey IS NOT NULL) AS n_pending,
         COUNTIF(jkey IS NULL)     AS n_unjoinable
  FROM joined
  WHERE NOT is_labelled
),
-- Lead time is the one figure here that belongs to the TRAIN rather than to the situation. Every
-- other number answers "of the decisions we took, how many were right", and a decision is per
-- situation; this one answers "how much warning did the traveller get", and the traveller gets ONE
-- alert per train however many SX messages sit behind it (varsel.one_alert_per_train). Measured over
-- 14 days of prd on 2026-08-11, the difference is not cosmetic: 514 hit rows were 156 trains, and
-- the per-situation median read 89 min against 145 per train.
--
-- alert_lead_min here comes from the outcomes row, so it is the subject's WHOLE-life first alert --
-- the same figure /historikk reports, which is what keeps the two pages' lead times comparable. It
-- is deliberately not the window-scoped lead the cards show.
hits_per_train AS (
  SELECT
    ARRAY_AGG(STRUCT(alert_lead_min, provenance) ORDER BY alert_lead_min DESC LIMIT 1)[OFFSET(0)]
      AS first_alert
  FROM (
    SELECT
      j.alert_lead_min, j.operator, j.jkey, j.call_date, j.stop_place, j.stop_order,
      j.line, j.departure_time, j.situation_number,
      CASE
        WHEN j.et_cancelled IS NULL                              THEN NULL
        WHEN j.et_cancelled IS TRUE                              THEN 'relayed'
        WHEN STARTS_WITH(j.situation_number, 'KIX:NoSituation:') THEN 'no_sx'
        ELSE 'predicted'
      END AS provenance
    FROM joined AS j
    WHERE j.is_labelled AND j.verdict = 'hit'
  )
  GROUP BY
    -- varsel.one_alert_per_train's key, in its own order of preference: the boarding call, then
    -- (operator, line, departure_time) when no journey ref names the train, and failing both the row
    -- stays on its own -- an alert identifying no train is never merged with another. call_date is
    -- in the first form because bus ServiceJourney jkeys repeat every operating day.
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
  -- Still resolvable: never labelled yet, plus labelled-but-unresolved inside the retry window. On a
  -- same-day window this is nearly the whole population, by construction rather than by fault.
  COUNTIF(verdict IS NULL AND still_retrying)
    + (SELECT n_pending FROM unlabelled)    AS n_pending,
  -- THE COVERAGE PAIR (R20). Every subject the page lists, and how many of them a verdict exists
  -- for. n_labelled + n_unknown + n_pending = n_subjects, so a reader can see at a glance what
  -- fraction of the window the rates below are actually computed over -- which on the default view
  -- is a few per cent. Served as counts rather than a ratio: a ratio hides both magnitudes, and "3
  -- of 412" reads very differently from "0.7 %".
  COUNTIF(verdict IS NOT NULL)     AS n_labelled,
  (SELECT COUNT(*) FROM joined)    AS n_subjects,
  SAFE_DIVIDE(
    COUNTIF(verdict = 'hit'),
    COUNTIF(verdict = 'hit') + COUNTIF(verdict = 'false_alarm')
  ) AS precision,
  SAFE_DIVIDE(
    COUNTIF(verdict = 'hit'),
    COUNTIF(verdict = 'hit') + COUNTIF(verdict = 'miss')
  ) AS recall,
  -- IGNORE NULLS goes after the quantile count, not after the expression: BigQuery's signature is
  -- APPROX_QUANTILES(expression, number [{IGNORE|RESPECT} NULLS]), and the other order does not
  -- parse -- which would make this whole endpoint a permanent 503.
  (SELECT APPROX_QUANTILES(first_alert.alert_lead_min, 2 IGNORE NULLS)[SAFE_OFFSET(1)]
   FROM hits_per_train) AS median_lead_min_on_hits,
  (SELECT COUNTIF(first_alert.alert_lead_min >= 30)
   FROM hits_per_train) AS n_hit_with_30min_notice,
  (SELECT COUNT(*) FROM hits_per_train) AS n_hit_trains,

  -- ---- the same rows, segmented ----------------------------------------------------------------
  -- Nothing is dropped: every labelled verdict above lands in exactly one of the three buckets below
  -- or in n_unsegmented.
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
  (SELECT APPROX_QUANTILES(
     IF(first_alert.provenance = 'predicted', first_alert.alert_lead_min, NULL), 2 IGNORE NULLS
   )[SAFE_OFFSET(1)] FROM hits_per_train) AS median_lead_min_on_hits_predicted,
  (SELECT COUNTIF(first_alert.provenance = 'predicted' AND first_alert.alert_lead_min >= 30)
   FROM hits_per_train) AS n_hit_with_30min_notice_predicted,
  (SELECT COUNTIF(first_alert.provenance = 'predicted') FROM hits_per_train)
    AS n_hit_trains_predicted,

  -- RELAYED: ET had already declared the cancellation when we decided. Precision here should sit at
  -- or near 1 -- we are repeating a published fact -- and a miss in this segment is not a model
  -- failure at all but a plumbing one: the feed told us and we said nothing.
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
  (SELECT APPROX_QUANTILES(
     IF(first_alert.provenance = 'relayed', first_alert.alert_lead_min, NULL), 2 IGNORE NULLS
   )[SAFE_OFFSET(1)] FROM hits_per_train) AS median_lead_min_on_hits_relayed,
  (SELECT COUNTIF(first_alert.provenance = 'relayed' AND first_alert.alert_lead_min >= 30)
   FROM hits_per_train) AS n_hit_with_30min_notice_relayed,
  (SELECT COUNTIF(first_alert.provenance = 'relayed') FROM hits_per_train) AS n_hit_trains_relayed,

  -- NO SX AT ALL: nobody had published anything about this train when we decided, and the numbers
  -- came from the models reading real-time data alone. Strictly less information than either segment
  -- above, against a base rate ~12x lower and through its own calibration layers, so folding it into
  -- `predicted` would drag a figure that is supposed to answer "how good are the models when an
  -- operator has published something".
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
  -- Reported, not assigned.
  COUNTIF(et_cancelled IS NULL AND verdict IS NOT NULL) AS n_unsegmented
FROM labelled
