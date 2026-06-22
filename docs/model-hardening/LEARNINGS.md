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
- **P(cancel) recalibration (`create_cancel_calibration.sql`).** The classifier trains through
  2026-03 (~13.9% rate) but recent conditions run ~10%, so raw P(cancel) over-predicts low /
  under-predicts high. A Platt layer (`cancel_platt`, logistic of the realized outcome on the raw
  score) fit on the post-training trailing window re-centers it. Validated honestly (fit
  2026-04..05, test unseen 2026-06): log-loss 0.164 → 0.157, mean P 0.113 → 0.103 vs actual
  0.095, high-confidence deciles markedly closer. Serve as a 2nd step: `p_cal =
  predict(cancel_platt, logit(p_raw))`. Refit periodically to track the base rate.
- **Delay regressors — valid but weak; needed a log target.** Raw-minute regressors LOST to a
  trivial "predict the median" baseline on MAE (3.67/3.79 vs 3.34/3.20) — classic
  mean-seeking-on-skew. Retraining on **`LN(1+delay)`** (back-transform `EXP(pred)−1`) flipped
  it: MAE **3.05 / 3.04**, now beating the median baseline (~5–9% lift). They predict the
  *typical* delay (~2 min).
- **p90 "bad case" delay — added (`create_delay_p90_calib.sql`).** BQML boosted trees have no
  native quantile loss, and the delay tail is heteroscedastic (log-residual p90 grows with the
  predicted level), so a single multiplier is wrong. Built `disruptions.delay_p90_calib`: a
  per-side, per-prediction-bucket (`ROUND(predicted_log,1)`) empirical-p90 lookup over the point
  models. Validated on the holdout: **93.7% coverage at the nominal p90** (97% at p95) — slightly
  conservative (integer ties + the seasonal shift), which is the safe side for a worst case. The
  worst predicted decile has a typical delay ~7 min but a p90 of ~38 min — the gap worth showing.
- **Serving layer (`predict_calls.sql` → `disruptions.call_predictions`).** Mirrors the
  per-situation path (`predict.sql` → `predictions_today` → API cache): one product-shaped row
  per (situation × affected call) with calibrated `p_cancel`, `dep_delay_typ/p90`, and
  `arr_delay_typ/p90`. Feature construction is copied verbatim from `create_training_calls.sql`
  (no train/serve skew); calibration is applied inline (Platt closed form from `ML.WEIGHTS`,
  p90 lookup from `delay_p90_calib`). 110k rows / 3.6k situations / 5.1k trains over a 30-day
  window; mean `p_cancel` 0.096 = the realized recent rate. A worked turn-short example reads
  correctly: a train ~98%/93% cancelled at its first two stops but ~3-9% from stop 3 on — so
  "boarding here? take the next train" vs "boarding downstream? ~2 min late" falls straight out
  of the per-call grain.
- **Forward (live) serving works — planned call source + live query.**
  `rail_calls_planned_today.sql` → `disruptions.rail_calls_planned_today` takes the AIMED times of
  every call of each journey in today's ET snapshot, including all `estimatedCalls` (the UPCOMING
  stops `rail_calls` skips), giving an ~49h forward schedule (≫ the 12h window) with
  `aimed_dep`/`aimed_arr`/`stop_order`/`stop_place`. `predict_calls_today.sql` is `predict_calls`
  pointed at it over recent situations → `call_predictions_today`, scoring trains that have NOT
  run yet. Demonstrated end-to-end: future R31 departures (22:06–23:00) score 4 min typ / 24 min
  p90 under a "Forsinkelse forventes" situation vs 1 min / 3–5 min under "Normal hastighet" —
  the right contrast, from onset features, before departure.

## What did not work / limits found

- **Delay magnitude is only weakly predictable** (R² ~0.13; spread-thin feature importances).
  Conditional on a train running, exact per-call lateness from onset features carries a lot of
  irreducible noise. Cancellation (yes/no) is far more learnable than delay (how many minutes).
- The delay point model under-predicts the tail by design (predicts ~median) — addressed by the
  separate `delay_p90_calib` p90 layer (see above).
- **GTFS could not replace ET as the journey/call source.** The GTFS timetable
  (`timetable_gtfs.gtfs_last_recorded_ent_v1`) is a richer, cheaper schedule (names,
  StopPlace, dep+arr, ±weeks horizon, 14 MB vs ET's 7.8 GB), but its `trip_id` is a NeTEx
  `ServiceJourney` ref — **0 native matches** to SX `affected_journeys` (`DatedServiceJourney`).
  A parsed train-number+date bridge finds only **~57%** of SX-named trains (GTFS is the BASE
  schedule; SX skews to replacement/renumbered/extra runs that only ET carries). So ET stays the
  journey source; GTFS is used **only to resolve `stop_name`** (join by StopPlace, not journey) —
  now 100% named.

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

- Done: p90 delay layer (`delay_p90_calib`), P(cancel) recalibration (`cancel_platt`), the
  serving query (`predict_calls.sql` → `call_predictions`), and the live forward path
  (`rail_calls_planned_today.sql` + `predict_calls_today.sql` → `call_predictions_today`).
- **API wiring — done.** `disruptions/api/` now serves per-train predictions: `disruptions_today.sql`
  gained `affected_lines`; `rail_calls_planned_today.sql` (planned source) + `predict_calls.sql`
  (live scoring, status derived inline from onset text, fed by `disruptions_initial_today`) build
  `call_predictions_today`; `main.py` adds an in-process cache (indexed by jkey) and a **`/journey`**
  endpoint (`?id=&from_stop=&to_stop=`) returning per-stop P(cancel)+typ/p90 delay, picking the
  most-severe situation when several touch a train. Cost-shaped: the ~8 GB ET scan
  (`rail_calls_planned_today`) is TTL-gated to ~10 min; the scoring query is **2.4 MB** so it runs
  every ingestion. Validated end-to-end (runtime smoke test: 55 journeys cached; a turn-/start-short
  L2 train reads 0.94-0.99 cancel on its early stops). **Only `gcloud run deploy` remains** (see
  CLAUDE.md) — not deployed from here.
- Horizon note: the planned source is ET-bounded (~today + ~2 days); a NeTEx planned-timetable
  feed would extend it, but ET already exceeds the 12h window the model uses.
- Keep `predict_calls.sql` and `predict_calls_today.sql` in sync (same scoring; only the call
  source + situation window differ).
- Optional v2: a **joint trip-completion** probability (boarding served AND alighting served),
  which the two per-call probabilities approximate but don't model jointly.
- Re-fit `cancel_platt` (and `delay_p90_calib`) on a rolling trailing window as data arrives.

## State (per-call family)

- New, committed: `create_training_calls.sql`, `train_call_models.sql`, `create_delay_p90_calib.sql`,
  `create_cancel_calibration.sql`, `predict_calls.sql`, `rail_calls_planned_today.sql`,
  `predict_calls_today.sql`.
- Serving tables: `disruptions.call_predictions` (retrospective, over `rail_calls`) and
  `disruptions.call_predictions_today` (live forward, over `rail_calls_planned_today`); plus the
  planned call source `disruptions.rail_calls_planned_today`.
- API (`disruptions/api/`): new `rail_calls_planned_today.sql`, `predict_calls.sql`; modified
  `disruptions_today.sql` (+`affected_lines`) and `main.py` (per-call cache + `/journey`). Built
  but not yet deployed.
- Tables/models: `disruptions.training_calls`; models `call_cancel_clf` + `cancel_platt`
  (Platt recalibration), `dep_delay_reg`, `arr_delay_reg` (delay models trained on `LN(1+delay)`
  — serving must back-transform); table `disruptions.delay_p90_calib` (p90 lookup keyed on
  `ROUND(predicted_log,1)`, clamp to per-side range at serving).
