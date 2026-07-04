# Pooled NSB + GOA per-call models: train + eval

**Date:** 2026-07-03 · **Status:** experiment complete, gate PASSED — nothing serves yet.
Follow-up to [the operator census](../operator-census/LEARNINGS.md), executing its agreed
first build step: pool GOA (Go-Ahead, Sørlandsbanen/Jærbanen) with NSB into one per-call
model family, evaluate per operator, and gate on NSB parity before touching serving.

## TL;DR

Pooling works, at zero cost to NSB and large benefit to GOA:

- **NSB parity is exact.** Pooled-vs-prod differences on the NSB holdout are ≤ 0.0004 AUC
  on all three headline metrics — statistical ties. Adding GOA (2% of rows) to the pool
  changes nothing for NSB.
- **GOA gets strong models immediately.** Cancellation AUC **0.986**, alert-composite
  p_impact AUC **0.97** both sides, and at the serving threshold (0.65) **~95% precision at
  91–95% recall**. GOA reads *easier* than NSB because its journey-scoped situations are
  mostly explicit cancellation notices (holdout base rates: 51% of affected calls cancelled,
  65% impacted, vs NSB's 10%/19%).
- **Pooling clearly beats zero-shot.** Prod NSB models applied blind to GOA rows manage
  0.936/0.930 impact AUC (features transfer!), but pooling adds ~+3 AUC points and +5–9
  points of precision at the threshold.
- **Total cost ≈ $1.6** (training $1.40, GOA calls table $0.10, rest pennies) and ~40 min
  wall — because the historical build came from `last_recorded`, not anshar, and
  `enable_global_explain` stayed off.

## What was built

| Artifact | Script | Notes |
|---|---|---|
| `disruptions.goa_calls` (833k calls, 60k journeys) | `data/queries/create_goa_calls.sql` | rail_calls-shaped, from `last_recorded` (`dataSource='GOA' AND vehicleMode='rail'`), window 2025-02-25 → 2026-06-20 |
| `disruptions.training_calls_pooled` (1.34M rows) | `data/queries/create_training_calls_pooled.sql` | training_calls logic + `operator` feature; per-operator calls join, net_pre_*, own_prev_delay |
| 11 `*_pooled` models | `data/queries/train_call_models_pooled.sql` | identical contracts + `operator`; tuned cancel opts; `enable_global_explain=FALSE` |
| 9 `*_platt_pooled` layers | `data/queries/create_pooled_calibration.sql` | one shared fit on the pooled holdout (≈95% NSB rows) |
| Eval | `data/queries/eval_pooled_models.sql` | 3 families × 3 metrics × operator, one metric pipeline |

Key build facts:

- **GOA in `last_recorded`**: rail rows are `GOA:DatedServiceJourney:` refs from Feb 2025
  (partial) / **March 2025** (full) — nothing labeled exists earlier, in any source (census
  finding). Replacement buses separate cleanly as `ServiceJourney`+`bus`. Realized-outcome
  rule mirrors rail_calls: keep cancelled rows (either flag) + non-`estimated` operated rows;
  non-cancelled `estimated=true` rows are unrealized predictions (~21k dropped). ~0.02%
  duplicate keys deduped on latest `recordedAtTime`. **Jan 2026 is a producer gap month**
  (normal journey counts, ~half the call rows).
- **NSB rows are bit-compatible with prod**: the pooled table's NSB slice is row-identical
  to `training_calls` (1,055,196 train / 262,647 eval rows, same situation counts) — the
  2026-07-03 `disruptions` rebuild (vehicleJourneyRefs extraction) added only non-joining
  uuid refs for NSB, so the parity comparison is clean.
- **The join is scoped `(operator, jkey)`** — DatedServiceJourney numeric ids are not
  guaranteed unique across codespaces, and cross-operator label leakage must be impossible.
  `category` needed no work: `disruptions.categories` already covers 99.7% of GOA onset
  texts (shared message templates).

## Eval (temporal holdout, onset ≥ 2026-04-01; thr = 0.65 = serving alert threshold)

| metric | family | operator | n | base | AUC | prec@.65 | rec@.65 |
|---|---|---|---|---|---|---|---|
| cancel | **pooled** | GOA | 5,420 | .505 | **.9859** | .947 | .950 |
| cancel | zeroshot | GOA | 5,420 | .505 | .9552 | .858 | .946 |
| cancel | **pooled** | NSB | 262,647 | .103 | **.9606** | .934 | .589 |
| cancel | prod | NSB | 262,647 | .103 | .9603 | .933 | .587 |
| impact_arr | **pooled** | GOA | 5,039 | .655 | **.9685** | .951 | .918 |
| impact_arr | zeroshot | GOA | 5,039 | .655 | .9357 | .901 | .926 |
| impact_arr | **pooled** | NSB | 250,697 | .187 | **.9307** | .913 | .592 |
| impact_arr | prod | NSB | 250,697 | .187 | .9304 | .912 | .589 |
| impact_dep | **pooled** | GOA | 4,973 | .649 | **.9671** | .948 | .911 |
| impact_dep | zeroshot | GOA | 4,973 | .649 | .9298 | .888 | .924 |
| impact_dep | **pooled** | NSB | 245,176 | .189 | **.9231** | .909 | .567 |
| impact_dep | prod | NSB | 245,176 | .189 | .9234 | .911 | .567 |

The prod-NSB rows recomputed here match the promoted numbers in
`docs/model-hardening`/memory (0.9603 / 0.930 / 0.923, ~91% precision) — the metric
pipeline is consistent with `eval/impact_eval.py`.

**Calibration** (mean calibrated P(cancel) vs realized, shared platt): NSB 0.1037 vs
0.1034 — excellent; GOA 0.519 vs 0.505 — mild +1.4pt overshoot. The NSB-dominated shared
platt is acceptable for GOA; a per-operator platt (`WHERE operator='GOA'`) is the first
knob to turn if GOA reliability ever needs tightening.

## Reading the results

- **Why GOA is "easy":** GOA publishes per-journey cancellation notices, so its affected
  calls are majority-positive, and the model mostly needs to read `status`/journey scope
  correctly. High precision at high recall follows. NSB's situations are broader
  (line/stop-scoped, mixed outcomes), hence lower base rates and recall at the threshold.
- **Why zero-shot already works:** the strongest features (line_now_*, own_prev_delay,
  status, category, timing) are operator-generic; unseen GOA lineRef/stop_place values fall
  to default branches and cost ~3 AUC points — which is what pooling recovers.
- **Caveats:** GOA holdout is small (461 situations / ~5k calls → AUC ±~0.005); the
  holdout-fit platt reads slightly optimistic on precision (same caveat as prod, both
  families identically affected); Jan 2026 GOA labels under-count (gap month); delay
  regressors were trained for family completeness but not separately evaluated (weak heads,
  R²~0.13 at NSB — the impact stack is the gate).

## Not done — gates before any serving change

1. **Per-ask-horizon platt layers** (36 `*_platt_{lead60,120,240,plan}` models) exist only
   for the prod family; the pooled family needs the same refit if promoted.
2. **Serving path is NSB/VYG-hard-coded**: `disruptions_today.sql` (`participantRef='NSB'`),
   `rail_calls_planned_today.sql`/`rail_calls_today.sql` (`lineRef LIKE 'VYG:%'`, anshar
   TTL scans — GOA live calls would need `'GOA:%'` too), `predict_calls*.sql` model names,
   `main.py:1393` (`VYG:Line:` prefix). Parameterizing these is its own phase.
3. **GOA lead-time behaviour unknown** — the census showed GOA messages are journey-scoped;
   whether they arrive early enough to alert ≥30 min pre-departure needs the leadtime
   analysis re-run on GOA.
4. **Promotion decision**: pooled models are tied-or-better for NSB, so promoting the pooled
   family (rather than maintaining two) is attractive — but requires the horizon platts,
   a `predict_calls.sql` that emits/consumes `operator`, and re-running the global-explain
   retrain overnight if attributions are wanted.

## Repro

```sh
bq query --nouse_legacy_sql < data/queries/create_goa_calls.sql              # ~$0.10
bq query --nouse_legacy_sql < data/queries/create_training_calls_pooled.sql  # ~$0.01
# train: run each statement as its own job (see file header)                 # ~$1.40 total
# calibrate: each statement its own job
bq query --nouse_legacy_sql < data/queries/eval_pooled_models.sql            # ~$0.01
```
