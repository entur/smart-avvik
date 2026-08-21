-- Re-copy the DELIVERED wording onto already-labelled decision outcomes.
--
-- One-off, per environment. `varsel_decision_outcomes` holds a frozen copy of `message` /
-- `message_source` taken at labelling time, and until 2026-08-21 the view it copies from
-- (`varsel_decision_groups`) served the LAST wording produced for a subject rather than the one
-- actually pushed. The view is fixed; this reaches the rows that were labelled under the old rule
-- and that the nightly job will never revisit -- it deletes and re-labels only rows whose outcome
-- is still unknown, so every resolved verdict keeps its wording forever.
--
-- Measured on dev 2026-08-21 before running: 738 of 3 888 outcome rows carried text the traveller
-- was never sent, 601 of them already resolved and therefore frozen; 135 additionally attributed
-- it to the wrong writer ('Skrevet av modellen' over template text, and the reverse).
--
-- Safe to re-run: the predicate matches only rows that still differ, so a second run updates
-- nothing. Cheap -- both sides are small, and the view's full scan of varsel_decisions is the same
-- one /historikk-api does on every uncached request.
--
-- It does NOT touch the verdicts, the probabilities or anything else the labelling job decided:
-- only the two wording columns, and only where the view can name the pushed row. A subject whose
-- decision rows have aged out of `varsel_decisions` would simply not join (none had on dev --
-- the table has no partition expiry), and NULL join keys never match, which is correct: the
-- labelling job only ever writes rows with a jkey.
UPDATE `disruptions.varsel_decision_outcomes` AS o
SET message = g.message, message_source = g.message_source
FROM `disruptions.varsel_decision_groups` AS g
WHERE o.journey_digest   = g.journey_digest
  AND o.operator         = g.operator
  AND o.jkey             = g.jkey
  AND o.call_date        = g.call_date
  AND o.stop_order       = g.stop_order
  AND o.situation_number = g.situation_number
  AND (o.message        IS DISTINCT FROM g.message
    OR o.message_source IS DISTINCT FROM g.message_source);
