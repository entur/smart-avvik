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

---

# Follow-up: delay-regressor diagnosis by category/line + feature pruning

Session date: 2026-07-02. Goal: understand *where* the delay regressors (`dep_delay_reg`) work
and where they don't — by category and by line — and test whether a simple grouped-median lookup
rivals the model. Ended with an accuracy-neutral feature prune shipped to prod.

## Methodology trap found first (the most reusable lesson)

To attach a `category` to each `call_predictions` / `dep_delay_reg_eval` row, join through
**`disruptions`** (all NSB situations) on `situationNumber` and match `categories` on
`d.onset_summary`/`d.onset_description` (leak-free first-version text) — **99.5% coverage**
(109,741/110,265 rows; 3,618/3,621 situations).
**Do NOT join through `disruptions_initial` / `disruptions_initial_new`.** Those keep only
*reopened* situations (inner `JOIN end_times` on "Toget kjører igjen"/"Åpnet…"), so they reach only
**~6% (445/3,621)** of the predicted situations — a biased slice. `disruptions_initial_new` is not
"the whole range" — it's the recent **incremental complement** of `disruptions_initial` (the
`NOT EXISTS` dedup), 413 recent NSB situations. All 3,621 `call_predictions` situations are NSB, so
operator isn't the filter — the reopen requirement is. Routing category through the `_initial`
tables silently biased an earlier per-category read and flipped its conclusions.

## What we found (all on the temporal holdout, onset ≥ 2026-04-01)

- **Prediction quality is strongly category-dependent, and the model's real skill is narrow.**
  Per-category MAE vs a "predict this category's median" baseline (skill = 1 − MAE_model/MAE_median):
  the model beats the median baseline meaningfully in **only one large category** —
  `materiell/teknisk` (+0.15, n=37.7k). Skill ≈ 0 for the biggest categories
  (`infrastruktur/signal` n=55.5k +0.012, `drift/kapasitet` n=53k −0.002, `infrastruktur/spor`
  n=25k −0.021), i.e. a category-median lookup would match the model on **~60% of volume**. The
  model is **actively worse** than the median for `ytre/vær/vinter` (−0.33) and
  `infrastruktur/stasjon` (−0.10) → those should fall back to the median.
- **Systematic under-prediction everywhere** (bias < 0), expected from the median-targeting
  `LN(1+delay)`→`EXP−1` transform on a right-skewed target. Pathological for
  **`infrastruktur/strøm`**: predicts ~2.4 min vs 7.5 actual (bias −5.0, RMSE 22.9) — blind to the
  power-incident tail.
- **p90 coverage holds up out-of-sample** — ~92–95% for the bulk (slightly conservative, safe
  side), and it *rescues weather* (point model harmful there, but p90 covers 96.8%). **One real
  failure: `infrastruktur/strøm` at 85.4%** — both the point estimate *and* the interval under-cover
  the power tail. Root cause is architectural: `delay_p90_calib` is keyed only on the predicted-log
  bucket (`create_delay_p90_calib.sql:13`, category-blind), so strøm calls land in low-pred buckets
  whose p90 is calibrated to the thinner general tail. Fix = add category/strøm-flag or a tail
  multiplier to the p90 bucket key. (`drift/planlagt` also under-covers at 0.84, small n.)
- **`lineRef` is important; `stop_place` is not.** `ML.GLOBAL_EXPLAIN` on `dep_delay_reg`:
  `stop_order` 0.128, **`lineRef` 0.124**, `min_from_onset` 0.119, `num_stops` 0.102,
  `num_journeys` 0.084, `status` 0.029, then small; **`stop_place` ~0.004 (near-dead)**; and
  `planned` / `num_lines` / `is_terminal` / `is_origin` = **0**. The model uses *which line* and
  *position in the trip*, not the specific station. (`category` is not a feature — `status` is.)
- **Grouped-median baseline** (built on train, applied to holdout with min-support-30 back-off):
  overall MAE model **3.03** < `category+line+stop` 3.169 < `category+line` 3.221 < `category` 3.265
  — model wins overall, but adding `stop_place` barely helps (matches its ~0 importance), and
  adding `line` to the median *hurts* sparse categories (vær 1.59→2.01, strøm 7.50→7.86) by thinning
  the data. Per-category it splits exactly as the skill numbers say: median ties/beats the model on
  the low-delay high-volume infra categories; model wins the high-delay ones.
- **Per-line (all VYG/Vy lines):** the model beats a line-median on nearly every line, but its
  *skill* is concentrated in volatile regional/long lines (R31 +0.128, RE20 +0.124, R22 +0.119) and
  is ~0/negative on the most punctual commuter line (R21 −0.035). Under-prediction scales with line
  delay level: bias **−8.7 on F4** (mean 16.5 min), −7.0 on R40, −4.1 on R31.

## What we changed and shipped

- **Pruned 5 zero/near-zero-signal features** (`planned`, `num_lines`, `is_terminal`, `is_origin`,
  `stop_place`) from **both** `dep_delay_reg` and `arr_delay_reg` in `train_call_models.sql`
  (classifier `call_cancel_clf` left unchanged — its features weren't validated). First proved it
  on a throwaway `dep_delay_reg_pruned`: holdout **MAE 3.0317 vs 3.0326**, RMSE marginally better,
  **84.8% of predictions byte-identical** — zero accuracy cost. `stop_place` is still emitted by
  `create_training_calls.sql`/`predict_calls.sql` as an output key, just no longer a model feature,
  so serving is unaffected (`ML.PREDICT` ignores extra columns).
- **Committed** (`7c46b73`), then **made live**: retrained both regressors
  (`CREATE OR REPLACE MODEL`, each as its own `bq` job) — `dep_delay_reg` now **11 features**,
  holdout **MAE 3.046 / RMSE 6.797** (≈ original 3.033 / 6.817, within boosted-tree run variance,
  no regression); `arr_delay_reg` also 11 features, **MAE 3.047 / RMSE 7.040** (healthy, consistent
  with dep — but *reasonableness-checked only*, no strict before/after: the old arr model was
  overwritten before a baseline was captured).
- **Rebuilt the downstream calib chain** against the new models so there's no model/calib skew:
  `dep_delay_reg_eval`, `arr_delay_reg_eval` (Stage 1, read the models) → `delay_p90_calib` (Stage 1,
  reads models directly) → `delay_point_calib` (Stage 2, reads the fresh eval tables).
  `apply_delay_point_calib.sql` is SELECT-only (no persisted table) — not part of the rebuild.

## Recommended next steps

- **Hybrid serving:** use the model for high-delay segments (`materiell/teknisk`, volatile
  regional lines) and a `category`(+`line`) median lookup for the low-delay high-volume bulk where
  they're statistically interchangeable; explicitly fall back to the median for `ytre/vær/vinter`
  and `infrastruktur/stasjon`, where the model is worse than a constant.
- **Category/line-aware p90** (or a tail multiplier) to fix `infrastruktur/strøm` coverage (85%→~90%)
  and the long-distance under-prediction (F4/R40/R31) without disturbing the already-calibrated bulk.
- If a strict arrival-side before/after is wanted, retrain a 16-feature `arr` variant once and diff;
  low priority given the dep-side symmetry.

## State

- Committed: `train_call_models.sql` (both regressors' `EXCEPT` lists pruned; `7c46b73`).
- Live models retrained: `dep_delay_reg`, `arr_delay_reg` (11 features each). Test model
  `dep_delay_reg_pruned` created then dropped.
- Calib chain rebuilt against the new models: `dep_delay_reg_eval`, `arr_delay_reg_eval`,
  `delay_p90_calib`, `delay_point_calib`.
- No serving-code change needed; `predict_calls.sql` picks up the new models + consistent calib tables.

---

# Follow-up: exceedance reframing + delay-propagation features

Session date: 2026-07-02 (afternoon). Goal: step back and look for *new approaches* to the
actual product question — "for a journey from_stop → to_stop under an active SX situation,
what delay should the passenger expect (as an interval), ignoring minor delays (<5–10 min),
without crying wolf?" All experiments on the usual temporal holdout (onset ≥ 2026-04-01),
NSB only, arrival side (= what the passenger experiences at to_stop).

## The reframing: the product question is classification, not regression

Every result in this project says cancellation (yes/no, AUC 0.885) is learnable while delay
minutes (R² ~0.13) are not. But "will my arrival be ≥10 min late?" **is a yes/no question** —
the current stack just answers it by thresholding a median-seeking regressor + a p90 lookup.
Quantified on the holdout (225,610 non-cancelled calls, 9.7% of them ≥10 min late), the current
serving path offers only two operating points for a ≥10-min alert, both bad:

| alert rule | alerts | precision | recall |
|---|---|---|---|
| point estimate ≥ 10 (`EXP(pred)−1`) | 944 | 63.1% | **2.7%** — never fires |
| p90 lookup ≥ 10 | 81,307 (36% of all calls!) | **19.7%** | 73.2% — cries wolf |

The regressor's ranking power for the ≥10 event is AUC 0.781. Everything below replaces the
decision layer with **direct exceedance classifiers** `P(arr_delay ≥ t)` for t ∈ {5,10,20,40}
(BOOSTED_TREE_CLASSIFIER on the same population, same leak-free features).

## Feature experiments (≥10-min model, holdout AUC / log-loss)

| variant | features | AUC | log-loss |
|---|---|---|---|
| v1 | current 11 (as pruned regressors) | 0.788 | 0.280 |
| v2 | + `category` (16-way, leak-free via onset-text join, 99.99% coverage) | 0.787 | 0.276 |
| v3 | + category + text (`stated_min`, flags) + **pre-onset line congestion** | **0.801** | 0.259 |
| v4 | v3 + **ask-time propagation** (`own_prev_delay`, `line_now_*`) | **0.910** | 0.173 |

- **`category` adds nothing** on top of `status`+`lineRef` — "informative in isolation,
  redundant in the model" strikes again. Its global-explain weight (0.063) is real but AUC-flat.
- **Text features are marginal:** only ~5% of onset texts state minutes (`"ca. 30 minutter"`);
  `stated_min` earns modest weight (0.035 > `status` 0.019); `buss for tog`/`stengt`/`redusert
  hastighet` flags are dead (≤0.0006).
- **Pre-onset line congestion is the first genuinely additive onset-time feature found since
  the original set.** `line_pre_delay` = mean positive dep-delay on the same line in the 60 min
  *before* onset (from `rail_calls`, leak-free, +~1 GB to build). Single-feature corr with the
  outcome 0.37 ≈ the entire old model's explanatory power; #1 global-explain feature in v3 (0.186).
- **Ask-time propagation features are a step change.** `own_prev_delay` (the train's own dep
  delay at its latest stop with aimed time ≥30 min before the predicted call) has **corr 0.88**
  with the outcome; `line_now_delay` (line state in the hour ending ~30–90 min before the call,
  via hourly buckets) corr 0.56. v4 = AUC **0.910** overall, **0.928** on the 64% of calls where
  `own_prev_delay` exists, **0.842** on the rest — and it is **calibrated raw** (0.14→0.11,
  0.45→0.44, 0.96→0.99 across deciles; the earlier variants over-predict at the low end due to
  the seasonal base-rate shift and would want a Platt layer).
- v4 PR curve (≥10): **91.5% precision @ 43% recall** (thr 0.7), **98.6% @ 28%** (thr 0.9).
  v3 at the same precision: ~11% recall. The current point path: 63% @ 2.7%.

## Interval layer: discrete survival curve from the stack

Monotonicity-enforce the four probabilities (cummax; raw violation rate 1.9%), then read
quantiles off the piecewise-linear survival curve. At matched ~94% coverage the stack's mean
p90 width **ties** the existing `delay_p90_calib` (11.35 vs 11.19 min) — but coverage is far
better *distributed*: `infrastruktur/strøm` 85.7%→88.9%, `drift/personale` same coverage at
width 17.5 vs 27.7, `materiell/teknisk` 15.0 vs 16.8. The stack is conditionally sharp where
the plog-bucket lookup is category-blind, and the alert probabilities come for free.

## Product-level alert ("should the passenger worry?")

`p_impact = p_cancel + (1−p_cancel)·p_ge10` (existing `call_cancel_clf` × v3 exceedance model),
label = cancelled OR ≥10 min late (holdout base rate 18.6%): **AUC 0.841**, well-calibrated in
the alert region, and the threshold policy *abstains by itself* in the low-signal categories:

| thr | alerts | precision | recall |
|---|---|---|---|
| 0.5 | 30,902 | 77.3% | 48.7% |
| 0.7 | 17,313 | 91.0% | 32.1% |
| 0.9 | 8,222 | 98.6% | 16.5% |

Per category at thr 0.7: precision 76–95% everywhere it fires; **zero alerts** in
`infrastruktur/stasjon` (base rate 3%) and `ytre/dyr` — the categories where the old point
model was worse than a constant. `drift/kapasitet` (benign anchor): 117 alerts out of 57k calls.
This is the requested "don't cry wolf" shape: alert only above a precision-picked threshold,
otherwise stay quiet; `p_impact` itself is the certainty estimate.

## Caveats (be honest when productionizing)

- **Realized-vs-live optimism.** The backtest uses *realized* delays for `own_prev_delay` /
  `line_now_*`. Live serving would use ET's current estimated delays; for a train sitting still
  the estimate lags reality, so live performance will be somewhat below the backtest. The
  ≥30-min observation cutoff and the hourly line buckets (state ends 30–90 min before the call)
  are conservative in the other direction.
- **Two regimes, one model.** v4's propagation features use `-1` sentinels when unavailable, so
  one model serves both: plan-ahead (features absent → falls back to ~v3 quality, AUC ~0.84 on
  that slice) and near-term (≤~1–2h before the call → AUC ~0.93). `predict_calls_today`'s
  10-min refresh cadence already matches the near-term regime; it would need to compute the two
  live features from ET at scoring time.
- Interval interpolation beyond t=40 is an extrapolation heuristic; the ≥40 base rate in the
  holdout is only 0.6%.

## Recommended next steps

1. ~~Productionize the exceedance stack (arr side first)~~ — **DONE same day, see next section.**
2. ~~Dep-side twins for the boarding stop.~~ — **DONE same day, see "Dep-side twins" below.**
3. Retrain `call_cancel_clf` with the propagation features — likely a similar (smaller) lift.
4. Optional headroom check: local XGBoost/LightGBM on the same table to see if BQML is leaving
   anything on the table; and a joint trip-completion model (boarding AND alighting served).

## Productionization (same day, 2026-07-02)

The stack is now wired end-to-end (SQL + API code; **not yet committed or deployed**):

- **`create_training_calls.sql`** computes all validated features in the one canonical place:
  `category` (leak-free onset-text join, deduped — `categories` has ~4 duplicate text pairs that
  otherwise fan out the join), `stated_min`, `line_pre_*`, `net_pre_*`, `line_now_*` (hourly
  buckets), `own_prev_delay`. Rebuilt: 1,317,843 rows — byte-count parity with the old base.
- **`train_call_models.sql`**: the three legacy models' EXCEPT lists now spell out the new
  columns (contracts frozen — a verbatim retrain reproduces the validated models); section 4
  adds `arr_ge{5,10,20,40}_clf`. Production `arr_ge10_clf` holdout: **AUC 0.9107 / log-loss
  0.1725** (= experiment; dropping the dead text flags cost nothing).
- **`create_exceedance_calibration.sql`** (new): `arr_ge{t}_platt` per threshold, cancel_platt
  recipe (fit on the post-training holdout; REFIT AFTER ANY MODEL RETRAIN — serving reads the
  coefficients live from ML.WEIGHTS). Validated: p_ge10 calibration tracks actuals within ~1pt
  across deciles; the q=0.10 interval covers **93.8% / 94.4%** (Apr–May / June) at mean width
  **8.6 min** vs the legacy p90 lookup's 11.2 min at the same coverage — ~23% narrower.
- **`rail_calls_planned_today.sql`** (both copies) now carries `dep_delay_min` on recorded
  calls (actual vs aimed, same definition as rail_calls) = the live-observable state.
- **Serving** (`predict_calls.sql` retrospective; `predict_calls_today.sql` +
  `disruptions/api/predict_calls.sql` live): per-threshold Platt closed forms from ML.WEIGHTS,
  GREATEST-cascade monotonicity, interpolated p50/p90 crossings (tail `40*SQRT(s40/q)` capped
  at 90), new columns `p_arr_ge{5,10,20,40}`, `arr_delay_p50/p90_stack_min`, `p_impact`.
  Live variants compute state features under the live information constraint: only OBSERVED
  outcomes from the planned snapshot; `line_now_*` is a per-line NOW-CAST of the last observed
  60 min (converges to training semantics as the refresh cycle approaches the call);
  `own_prev_delay`'s `-1` sentinel already means "no upstream observation" in training, so
  not-yet-started trains are in-distribution.
- **API (`main.py`)**: `/journey` and `/impact` responses gained `arr_delay_p50_min` /
  `delay_p50_min`, `arr_ge10_probability` / `ge10_probability`, `impact_probability`; the
  existing `*_p90` field now serves the (narrower, better-calibrated) stack p90 when available.
  Refresh order is deploy-safe: the scoring query recreates the table schema before the cache
  reads it, inside the existing try/except.
- **Validated end-to-end**: retrospective `call_predictions` (110k rows: mean p_ge10 0.105 ≈
  base rate, mean p_impact 0.184 ≈ realized 18.6%, 0 monotonicity violations / interval
  inversions) and live `call_predictions_today` (75 situations / 2,259 calls; a currently-stuck
  RE20 reads P(≥10)=0.99 with a 39–87 min interval toward Göteborg while quiet calls sit ~0).
- **Remaining to ship**: commit, then `gcloud run deploy` from `disruptions/api/` (CLAUDE.md).
  Post-deploy hygiene: refit `arr_ge*_platt` + `cancel_platt` on a rolling window; the
  exceedance stack's own eval artifacts are `exp_prod_stack_eval` (+ the earlier `exp_*`
  experiment tables/models, droppable).

## Dep-side twins (boarding stop), same day (2026-07-02, evening)

Next-step 2 executed: the departure-side exceedance stack `dep_ge{5,10,20,40}_clf` +
`dep_ge*_platt`, answering "will my train LEAVE >= t min late?" at the boarding stop. Same
feature contract, population = non-cancelled calls with a departure (mirrors `dep_delay_reg`).
Dep base rates track arr almost exactly (holdout >=10: 9.9% vs 9.7%; >=40: 0.61% vs 0.6%).

- **Holdout (onset >= 2026-04-01) AUC / log-loss:** >=5 0.844/0.361, **>=10 0.899/0.187**,
  >=20 0.930/0.068, >=40 0.969/0.014 — a hair under the arr twins (arr >=10: 0.911), expected:
  departure delay at the boarding stop has less accumulated propagation signal than arrival.
- **Calibrated (Platt on the holdout):** mean P(>=10) 0.103 vs actual 0.102 (Apr–May),
  0.094 vs 0.088 (June). Interval p90 coverage **93.3% / 94.0%** at mean width ~9 min
  (arr stack: 93.8/94.4 @ 8.6). p50 coverage 75% — conservative on the low side, same shape
  as arr. Zero monotonicity violations after the GREATEST cascade.
- **Boarding alert `p_impact_dep` = p_cancel + (1−p_cancel)·P(dep>=10)** (label: cancelled OR
  departs >=10 late; holdout base rate 18.9%): thr 0.5 = 83.5% precision @ 58% recall,
  **thr 0.7 = 91.5% @ 48%**, thr 0.9 = 97.5% @ 32%. Notably BETTER recall than the arrival
  alert at matched precision (arr: 32% @ 91%) — the departure event is easier to rank because
  the boarding stop is closer to the observed upstream state.
- **Serving:** all three `predict_calls*.sql` gained `dge*`/`wd*` CTEs, the dep survival
  cascade, and columns `p_dep_ge{5,10,20,40}`, `dep_delay_p50/p90_stack_min`, `p_impact_dep`
  (NULL at terminals, mirroring the arr stack's NULL-at-origins). `main.py`: `CallPrediction`
  gains `dep_delay_p50_min` / `dep_ge10_probability` / `dep_impact_probability`;
  `dep_delay_p90_min` now serves the stack value when available; `/impact`'s
  `departure_prediction` (StopPrediction) now populates `delay_p50_min` / `ge10_probability` /
  `impact_probability` on the departure side too.
- **Validated:** rebuilt `call_predictions` (110,135 rows — mean p_dep_ge10 0.108 ≈ base rate,
  mean p_impact_dep 0.188 ≈ realized 0.189, 0 interval inversions, dep fields NULL exactly at
  terminals) and the live `call_predictions_today` via the API query.
- **Ops note found while validating:** `data/queries/predict_calls_today.sql` reads situations
  from `disruptions.disruptions`, which is the MANUALLY-rebuilt training extract (last rebuild
  2026-06-20) — so that variant is empty whenever the extract is older than its 4-day window.
  Not a regression (the deployed API reads `disruptions_initial_today`, refreshed every
  ingestion), but rebuild `disruptions.disruptions` first if you want the repo live variant.
- Same hygiene as arr: refit `dep_ge*_platt` after any dep-exceedance retrain (ML.WEIGHTS is
  read live at serving), and periodically on a trailing window.

## Artifacts (all experiment-prefixed, cheap to keep or drop)

- Tables: `exp_training_calls_v2` (+category/text/pre-onset), `exp_training_calls_v4`
  (+ask-time propagation), `exp_arr_ge10_eval{,_v3,_v4}`, `exp_survival_eval`, `exp_impact_eval`.
- Models: `exp_arr_ge10_clf{,_v2,_v3,_v4}`, `exp_arr_ge{5,20,40}_clf_v3`.
- Total session cost ≈ $1 (8 trainings ≈ 15 GB billed each; table builds ~1–3 GB each).
- Ops gotcha: two long inline `bq query` CREATE TABLE calls hung client-side without ever
  submitting a job (0 CPU, empty output; exit 144 on kill). Workaround that worked: write the
  SQL to a file and submit with `--nosynchronous_mode`, then poll the job. Also: a naive
  line×time-range join against `rail_calls` blows up (each line joins its full 2-year history);
  use hourly line-state buckets + partition-pruned self-joins instead (1.05 GB, seconds).
