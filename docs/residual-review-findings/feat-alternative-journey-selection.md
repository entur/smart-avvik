# Residual Review Findings — `feat/alternative-journey-selection`

Run: `/lfg` pipeline, 2026-08-06. Plan:
`docs/plans/2026-08-06-001-feat-alternative-journey-selection-plan.md`.
Eight code-review findings were applied in commit `9dd063d`; what follows is what was
**not** resolved. No tracker sink was configured for this run, so the items are inlined
here verbatim — this file is the durable record.

## Deferred findings

- **P3 · `disruptions/api/varsel.py:1539` · A sub-threshold tick wipes the written message,
  so borderline journeys re-write on every flap.** The not-alert-worthy branch drops
  `_messages` and `_message_meta` unconditionally. `impact_probability` is re-derived from a
  cache rebuilt every ~4.8 min, so a journey sitting near its operator gate crosses back and
  forth: each down-tick discards the text and each up-tick spends one of the eight
  `_MAX_LLM_CALLS_PER_CYCLE` slots regenerating it, for the situation's whole life (up to 12 h
  ≈ 150 cycles). `_message_attempts` resets on every success, so nothing bounds the repetition.
  A handful of borderline journeys can saturate the budget, and journeys whose numbers
  genuinely moved are then logged `capped` and pushed with the deterministic template.
  Classified `advisory` by the reviewer; deferred because the fix is a design change
  (suppress at the read site rather than delete, since `_journey_model` renders
  `alert.message` regardless of `alertworthy`), not a mechanical correction.

## Residual risks recorded but not actioned

- **`impact._score_candidate` marks a candidate risky from any scored call on any journey it
  rides**, including stops before boarding and after alighting. Deliberate and conservative,
  but it widens the `risky` population — which now matters more, since the applied F1 fix
  makes that population the one the tier logic treats specially.
- **`_reconcile_original_arrival` runs only over query 1's candidates.** When query 1's pool is
  empty and query 2 supplies the pool, the Journey Planner's own reading of the disrupted train
  is never consulted, so the original's arrival stays the optimistic model p50 in exactly the
  fallback branch.
- **`alt_status` never writes `beyond_horizon` or `error`**; both collapse into
  `not_worth_lookup`. Documented in `decisions._alternative_status`, but it means a Journey
  Planner outage is indistinguishable from the impact gate in the decision log.
- **The poller's reserved fetch budget can be consumed by unauthenticated registrations.**
  `POST /varsel-api/journeys` is unauthenticated and capped only globally; filling the registry
  with distinct pairs on a disrupted corridor would exhaust `_fetch_calls["poller"]` and
  silence `alternative_journey` for genuine registrations. The push itself still goes out with
  the deterministic wording, so the loss is the advice field, not the alert.
- **`main._varsel_impact` passes `path="poller"` unconditionally**, which is safe only because
  no request path passes `with_alternative=True`. The property is held by a comment, not by
  code; a future `/varsel-api` route enabling the lookup would let unauthenticated traffic
  spend the reserved budget the split exists to protect.
- **`/impact` remains unauthenticated with `allow_origins=["*"]`**, and the new
  `_FETCH_MAX_PER_MIN` ceiling is a single global tally, not per-IP. One caller inventing
  `to_stop` values can consume the whole request-path budget and degrade `/impact` for everyone
  else to `alternative_journey: null`. A clear improvement on the base (which had no limit at
  all), not a complete answer. Tracked as OQ4 in the plan.
- **`decisions.record` calls `insert_rows_json` with no explicit timeout** on the ingestion
  thread, while the sibling `ensure_table` was given one. Pre-existing, unchanged by this work.
- **`_KeyedTTLCache.get` discards `_refreshing[key]` only on the success and `except Exception`
  paths**; a `BaseException` out of `fetch()` would pin that key to its stale value forever.
  Pre-existing; the new two-query shape doubles the number of keys exposed to it.

## Coverage gaps in the review itself

- **Three reviewer personas did not run**: `project-standards`, `data-migration`, and
  `adversarial`. The roster was cut for run-length reasons, not because the gates did not fire —
  all three were selected. `data-migration` is the most material omission: this change adds
  seven columns and two `ALTER COLUMN … SET OPTIONS` statements to a table whose column order is
  a documented silent-corruption hazard. `validate_varsel_sql.sh` passed all 11 statements plus
  the `label_varsel_decisions` dry run, which is the mechanical half of what that persona checks.
- **No cross-model adversarial peer ran** — `independence_verified: false`. Every finding in
  this run comes from the same model family.

## Plan gates not exercised

Two Verification Contract gates from the plan were not run and remain open:

- **The JP reach + latency probe** (`arriveBy × numTripPatterns` across a dense and a sparse
  corridor, recording p50/p95, payload bytes, pattern count and departure span). The module note
  records a 2026-08-06 live curl and the reach measurement that reversed KTD2, but not the
  latency sweep.
- **The offer-rate replay** — replaying ~30 days of `varsel_decisions` through
  `fetch_candidates` + `select_best` offline and recording the offer rate beside the
  baseline. The plan calls this *"the only gate that can fail for the reason
  the plan exists"*; every other gate is a unit test over payloads we wrote ourselves.

  **Still not run, and re-scoped 2026-08-06.** The baseline was measured instead
  (`data/queries/alternative_offer_rate.sql`) and turns out never to have existed:
  `has_alternative` was not written until 2026-08-05, so the 83 subjects before that are NULL, not
  zero. The old rule's only measurement is **26 %** (13 of 50 subjects), not "effectively never".
  The replay's comparison is `26 % -> X`.

  More limiting: dev's log holds 9 journeys over 9 stop pairs in 6 days, with **2** cancelled
  subjects — so no replay, however careful, can answer the question this feature exists for.
  Letting `alt_status` accumulate in dev after deploy measures it properly; see plan A6.

  Worth keeping as a method note: the first attempt at this correction reported the uninstrumented
  era as 0 % and inferred a jump that never happened. `LOGICAL_OR` over NULL is NULL and `COUNTIF`
  drops it, so "never instrumented" renders as "never offered" in any naive rate. This repo already
  states that rule twice — `n_unsegmented` on `/historikk`, and `alt_status`'s whole reason for
  existing — and it was still got wrong while writing about it.

## Documentation drift

`CLAUDE.md`'s `/varsel` section states that `_alternatives` shares `_messages`' lifecycle. That
is now narrower — `_reconcile_caches` reconciles them on four distinct readings. The code comment
says so; the project instructions have not been updated.
