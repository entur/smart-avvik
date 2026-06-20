# Model hardening — session learnings

Session date: 2026-06-20. Goal: move toward a product-ready impact model by reviewing the
data pipeline and model training for improvements.

## What we tried

1. **Audited and rebuilt `rail_calls`** (`create_rail_calls.sql`) — the realized-outcome table
   every downstream model depends on. Switched to a fixed reproducible window
   (2024-06-01 … 2026-05-31), captured cancellations from `estimatedCalls` (not just
   `recordedCalls`), kept terminal arrival-only stops, added arrival delay, fixed `call_date`
   to Europe/Oslo, and partitioned/clustered the table. One-time scan ≈ 10 TiB / **$62.63**.
2. **Brought `update_rail_calls.sql` to parity** with the new logic and switched it to
   grow-from-fixed-start (refresh fresh-slice journeys, append recent days, no age-out).
   Ran a one-time catch-up so the table reaches today (2026-06-20).
3. **Quantified category predictiveness** (`category_predictiveness.sql`): per-category and
   taxonomy-level η² (share of between-situation severity variance explained by category),
   plus a fine-vs-coarse granularity test.
4. **Tested a cause→status taxonomy hypothesis**: many SX messages state an *outcome*
   (`Forsinket`, `Innstilt …`, `Toget kjører igjen`, `Færre vogner`) not a *cause*. Measured a
   regex-derived status dimension's predictiveness, both naively and leak-free (onset text).
5. **Added onset text + a `status` column to `disruptions`** (`create_disruptions.sql`):
   `onset_summary`/`onset_description` from the first snapshot (single-scan `FIRST_VALUE`) and
   `status ∈ {innstilt, forsinket, kapasitet, other}` by regex.
6. **A/B-tested `status` as a model feature** for the duration/impact regressors and the
   cancellation classifier (train with vs without, same data/eval split).

## What did not work, and why

- **Coarsening the taxonomy** to the 4 top-level groups (`infrastruktur/materiell/ytre/drift`):
  η² collapsed from **0.24 → 0.046** (~80% loss). The signal lives in the leaf distinctions
  (esp. separating benign `drift/kapasitet` from severe `drift/ruteendring`). Don't coarsen.
- **`status` in the duration/impact models**: no lift — impact MAE 87.7 vs 87.8, reopen MAE
  33.6 vs 33.5 (flat / a hair worse). Reverted the `create_training_initial.sql` change.
  Why: these models train on the reopening slice where status is ~88% `forsinket` (almost no
  variance), and they already carry `category` + the operator's `declared_window_minutes`.
- **The headline +36% η² did not fully transfer to the models.** It was a *full-population,
  situation-level* result; the production models train on the *reopening-slice, boarding-level*,
  a range-restricted population — so most of the separating power is absent there.
- **Operational snags (not model issues):** the project's per-user daily BigQuery quota was
  10 TiB (the full rail_calls scan needed 10.02 TiB) — raised to 20 TiB. And
  `--maximum_bytes_billed` is *cumulative across a multi-statement script*, which silently
  killed multi-model training scripts mid-way; train each model as its own job.

## What worked, and how well

- **`rail_calls` rebuild** — the biggest win. Recovered **~75% of cancellations**
  (118.6k of 159.1k cancelled calls were previously dropped), kept **426k** terminal stops, and
  corrected service dates. 730 days, 6.6M rows, 0% unmapped quays. Verified end-to-end.
- **Category predictiveness (η²)** — taxonomy explains ~**0.24** of situation-severity variance
  (0.23 cancel / 0.27 trouble); **62%** of it is one benign anchor (`drift/kapasitet` predicts
  *low* impact). Fine grain is justified. Found 5 dead categories and clear merge/split
  candidates. The 16-category taxonomy is largely sound.
- **Status dimension (leak-free, full population, situation level)** — η²_cancel
  **0.233 → 0.316 (+36%)**, η²_severity 0.240 → 0.277. Driver = onset-declared `innstilt`
  (47% cancel rate at onset). Just 4 status buckets ≈ the entire 16-category cause taxonomy.
- **`status` in the cancellation classifier** (measured on the reopening slice) — a **modest but
  real** lift: ROC-AUC **0.792 → 0.803** (+0.011) and **log-loss −8.8%** (0.524 → 0.478,
  i.e. better-calibrated P(CANCELLED), which is what the product surfaces).
- **Onset text + status added to `disruptions`** in a single ~$0.01 SX scan, backward
  compatible, leakage verified (8,553 situations whose *latest* summary is the all-clear are
  correctly captured at onset as `forsinket` from `Toget står`). Committed.

## How the scope was reduced from the original task

Original ask: a product-ready impact model via a full pipeline + training-review pass.
This session delivered the **data-layer hardening and the analysis/measurement**, not shipped
models:

- **Done:** fixed the root data table (`rail_calls`) + its updater; analyzed the taxonomy;
  added the `status` dimension to `disruptions`; measured `status`'s effect on the models.
- **Deliberately not done (deferred):**
  - No production model was retrained or shipped with the improvements.
  - `status` was wired in only as an experiment; the duration result was negative (reverted),
    the cancellation result positive but **not yet productionized**.
  - Did **not** rebuild the cancellation training data on the full population off `rail_calls`
    (the recommended next step — see below).
  - Did not touch the `predict_initial_new_*` serving files, `duration_quantiles` calibration,
    or rebuild `categories`/`category_line_hour_impact`.
  - Did not address the duration models' inherent reopening-slice limitation.

## Data-quality / missing-data issues identified

1. **Cancellation undercount in `rail_calls` (FIXED).** Cancelled calls live mostly in
   `estimatedCalls`; reading `recordedCalls` alone dropped ~75% of them, and terminal stops and
   correct (Oslo) service dates were lost. Fixed in the rebuild.
2. **Same undercount in the cancellation model's LABELS (NOT fixed).**
   `create_trip_outcomes_initial.sql` reads ET `recordedCalls` *directly* (a ~$20 scan), not the
   rebuilt `rail_calls` — so its `CANCELLED` labels under-count the very thing the model
   predicts. Fix by sourcing from `rail_calls`.
3. **Models train on the 7.8% reopening slice** (situations that emit `Toget kjører igjen` /
   `Åpnet…`, ~7,922 situations). The duration/impact models are *inherently* slice-bound (their
   labels require a reopening event), but the **cancellation model is slice-bound only by
   choice** (`trip_outcomes_initial` ← `disruptions_initial`) — an unnecessary restriction.
   A full-population family exists (`trip_outcome_clf` ← `create_trip_outcomes.sql` ←
   `disruptions`) but is the older generation.
4. **Taxonomy conflates cause with status.** `AI.CLASSIFY` dumps outcome-only messages
   (forsinket / innstilt / all-clear / capacity) into `materiell/teknisk`, a near-baseline
   garbage-can. 5 categories are never (usefully) assigned: `materiell/{dør,bremse,motor}`,
   `ytre/vær/vind`, `ytre/person`.
5. **Latest-snapshot leakage (FIXED in `disruptions`).** Using the latest summary leaks the
   resolved state; ~21% of NSB summaries drift over a situation's life and ~9% end as all-clear.
   Now mitigated via `onset_summary`/`onset_description`.
6. **No structured outcome field in source SX.** Anshar flattened SIRI `consequences` to only
   `advice.adviceType` — no machine-readable delayed/cancelled condition — hence status is
   derived by regex over Norwegian text.
7. **η² caveat.** Predictiveness is measured against *current* `AI.CLASSIFY` assignments, so a
   low value conflates a bad category definition, misclassification, and irreducible
   within-category heterogeneity.

## Recommended next step

Rebuild the cancellation training data on the **full population** sourced from **`rail_calls`**
(correct cancellation labels + `status` for free + no $20 ET scan), then retrain
`trip_outcome_clf_initial_cancelled` and A/B against the current slice model. This compounds the
`rail_calls` fix, removes the artificial slice restriction, and is the setting where `status`
should pay off most.

## Current repo / table state

- Committed: `create_rail_calls.sql`, `update_rail_calls.sql`, `create_disruptions.sql`,
  `category_predictiveness.sql`.
- `create_training_initial.sql`: reverted (no net change).
- Tables rebuilt: `disruptions.rail_calls` (+ `rail_calls_pre_rebuild` backup),
  `disruptions.disruptions` (now with onset text + `status`).
- Experiment artifacts (dur_ab_*, trip_ab_*, toi_status) cleaned up.

---

# Follow-up: full-population per-call models ("any situation")

Goal shift (2026-06-20, same day): move off the 7.8% reopening slice to a model that works on
**any** rail situation and answers, for a passenger's **specific train**, two questions —
"will my train be cancelled?" and "how late will it be?" — separately at the **boarding** stop
("will it leave?") and the **alighting** stop ("will I reach my destination?").

## Design

- **Grain = the affected call.** One row per (situation × stop on an affected journey) whose
  scheduled time is within 12h of onset. This is the prediction unit the product needs, it
  represents **partial / turn-short cancellations** naturally (`call_cancelled` is per stop),
  and the situation-level "chance of cancellation" falls out as the mean over a situation's
  calls. Built by `create_training_calls.sql` → `disruptions.training_calls` (1.32M rows, 50k
  situations, 17% call cancel rate). Labels come from the rebuilt `rail_calls` (realized ops),
  **not** SX text — so every situation gets a label, not just the 7.8% that announce an
  all-clear.
- **Three models** (`train_call_models.sql`): `call_cancel_clf` (BOOSTED_TREE_CLASSIFIER),
  `dep_delay_reg`, `arr_delay_reg` (BOOSTED_TREE_REGRESSOR). Boarding answer = classifier +
  `dep_delay_reg`; destination answer = classifier + `arr_delay_reg`. Window keeps terminal
  arrival-only stops via `COALESCE(aimed_dep, aimed_arr)` (the destinations — the old
  `category_predictiveness.sql` join dropped them).
- **Leak-free, onset-only features:** `status` (onset regex), `planned`, affected-counts,
  onset time parts, and per-call **planned** attributes (line, stop, stop_order, scheduled
  time, `min_from_onset`). Deliberately EXCLUDED as leaky: `duration_minutes` and `severity`
  (latest-snapshot = resolved state), latest text, and the realized call fields.
- **Temporal split** (not random): train onset < 2026-04-01, test on/after. Keyed on onset date
  so a whole situation's calls land on one side — no within-situation grouping leak.

## What worked

- **Cancellation classifier — strong and productionizable.** On the temporal holdout:
  **ROC-AUC 0.885** (vs ~0.79 for the old slice model), **log-loss 0.197 — 42% better** than the
  base-rate baseline (0.338), and **well-calibrated** (predicted P(cancel) tracks actual
  monotonically across all deciles). Top drivers: `num_journeys`, **`status`**, `num_stops` —
  disruption size + onset-declared outcome. This is a real, calibrated per-train cancellation
  probability — the headline result.
- **Delay regressors — valid but weak; needed a log target.** Raw-minute regressors LOST to a
  trivial "predict the median" baseline on MAE (3.67/3.79 vs 3.34/3.20) — classic
  mean-seeking-on-skew. Retraining on **`LN(1+delay)`** (back-transform `EXP(pred)−1`) flipped
  it: MAE **3.05 / 3.04**, now beating the median baseline (~5–9% lift). They predict the
  *typical* delay (~2 min).

## What did not work / limits found

- **Delay magnitude is only weakly predictable** (R² ~0.13; spread-thin feature importances).
  Conditional on a train running, exact per-call lateness from onset features carries a lot of
  irreducible noise. Cancellation (yes/no) is far more learnable than delay (how many minutes).
- The delay point model under-predicts the tail by design (predicts ~median). A **p90 quantile
  model** for the "bad case" is the natural next add, not done in v1.

## Data-quality / missing-data issues identified

1. **SX↔ET journey-ref format boundary (caps usable history at ~13 months).** `rail_calls`
   covers 2024-06+, but SX `affected_journeys` only join to ET calls from **~April/May 2025**
   onward. Before that, ET used a short `<line>:<date>` ref (e.g. `66:2024-08-06`) while SX used
   the NeTEx `DatedServiceJourney:…` form — **no shared key**, unrecoverable without a timetable
   crosswalk. (The operator prefix also flipped `NSB:`→`VYG:`.) So the SX↔ET join — used here
   AND by `category_predictiveness.sql` / `category_line_hour_impact` — is effectively
   2025-05 → present. Plenty for v1, but a hard ceiling on history.
2. **Seasonal cancellation shift, exposed by the temporal split.** Call cancel rate is ~13.9%
   in the train period but ~10.3% in the spring-2026 holdout. The model handles it via
   `onset_month` (spring 2025 is in train) and stays calibrated, but a periodic recalibration on
   a recent window would tighten the slight low-end over-prediction. A random split would have
   hidden this entirely.
3. **`call_cancelled` is NULL for served calls** (SIRI convention) — must `COALESCE(...,FALSE)`
   or the classifier label errors out. Fixed in `create_training_calls.sql`.

## Recommended next steps (per-call family)

- Recalibrate P(cancel) on a trailing window; add a **p90 delay quantile** model for "bad case".
- Optional v2: a **joint trip-completion** probability (boarding served AND alighting served),
  which the two per-call probabilities approximate but don't model jointly.
- Serving: enumerate planned calls on a situation's affected journeys, score the three models,
  expose per-stop P(cancel) + typical delay. (Not built — training-side only this pass.)

## State (per-call family)

- New, committed: `create_training_calls.sql`, `train_call_models.sql`.
- Tables/models: `disruptions.training_calls`; models `call_cancel_clf`, `dep_delay_reg`,
  `arr_delay_reg` (delay models trained on `LN(1+delay)` — serving must back-transform).
