# Predicting disruption duration — session learnings

_Team KI-eksperimentering · proaktiv avvikshåndtering · 2026-06_

## Goal

Predict, **at situation onset (the first SIRI-SX version)**, how long an NSB
disruption will last — for a passenger message of the form:

> "Trains expected to run again in ~20 min, traffic impacted for ~40 min."

Suggested inputs: free-text summary+description mapped to a category (via
`disruptions.categories`), month/day-of-week/hour, and stop place / quay.

---

## What we tried

1. **Leakage-safe, initial-version feature extraction.** The pre-existing
   `disruptions.disruptions` table reads features from the *latest* snapshot, which
   already encodes the outcome. We built `create_disruptions_initial.sql` to read
   every feature from the *first* version (the snapshot available at prediction
   time): summary, description, severity, reportType, priority, declared validity
   window, affected stops/journeys, and Oslo-local calendar fields.

2. **Two labels, one shared population.** To support the dual message we defined:
   - `reopen_minutes` — onset → first reopening summary (`Toget kjører igjen` /
     `Åpnet…`). "Trains running again in X."
   - `impact_minutes` — onset → when cancellations / ≥15-min delays at the affected
     stops actually subside, derived from `rail_calls` (sessionized with a 60-min
     quiet gap, 12h cap), floored at `reopen_minutes`. "Traffic impacted for Y."

3. **Category mapping** of high-cardinality free text via the existing
   `disruptions.categories` (AI.CLASSIFY → 20 categories), joined on
   summary+description.

4. **Geography as multi-hot** top-50 affected stops + top-25 undirected OD
   corridors (dynamically generated columns), narrowed from the latest-snapshot
   pipeline's 100/50 because this slice is small.

5. **Two BOOSTED_TREE_REGRESSOR models** (`dur_init_reopen`, `dur_init_impact`) on
   `LN(label)` with a leakage-safe `is_eval` split, benchmarked against naive
   baselines (train median, operator's declared window, and "just reuse predicted
   reopen time").

6. **A passenger-relevance cross-check** against `rail_calls`: do these
   reopening-labelled situations actually coincide with the cancellations/delays
   passengers feel?

7. **Prediction intervals** (`create_quantiles.sql`) — empirical LN-residual
   quantiles as additive offsets, for "up to Z min" comms.

8. **A two-part / hurdle impact model** (`dur_init_tail_clf` × `dur_init_tail_size`)
   to handle the point mass where `impact == reopen`.

---

## What did NOT work, and why

- **Two-part (hurdle) impact model — abandoned.** A tail classifier (does impact
  extend past reopening?) reached a respectable **AUC 0.77**, but no way of
  combining it with a tail-size regressor beat the single impact regressor on the
  bulk (MAE≤4h: 39.9 mean-blend / 42.2 decision-rule vs **38.9 single**; median AE
  30.6 / 35.2 vs **26.1**). The single regressor, trained on the floored label,
  already absorbs the tail/no-tail structure; the decomposition just compounds
  three models' errors. **Kept the single model.** (The classifier is still useful
  standalone as a "risk of extended disruption" flag.)

- **The reopening label is a poor proxy for passenger impact.** Within the strict
  onset→reopening window, only **12–18%** of cancellations and ≥15-min delays
  occur. `Toget kjører igjen` is a *"track clear"* signal, not *"service
  restored"* — impact lands mostly *after* reopening and partly *before* the SX is
  even posted. (This is what motivated the separate `impact_minutes` label.)

- **Point estimates have little headroom for impact.** `impact_minutes` is
  intrinsically high-variance; the model only modestly beats naive-median in
  aggregate, and an oracle that predicts the *true* reopen time beats the impact
  model on MAE (because ~half the rows sit exactly on the floor). Chasing a better
  single number is low-value — the honest output is an interval (see below).

---

## What worked, and how well

- **Category mapping: excellent.** 99.8% of the slice joins to a category (14 of 20
  categories populated) from 894 distinct summaries — the high-cardinality text
  problem is well solved by the lookup table.

- **Quantile calibration: the headline win.** Coverage on held-out data lands on
  target (0.50 / 0.79 / 0.89 for P50/P80/P90) and generalizes train→eval. Typical
  intervals: reopen **9 / 18 / 26 min**, impact **37 / 108 / 172 min**. This is the
  right comms artifact for impact — a range, not false precision.

- **Reopen model: usefully accurate on the bulk.** For the ≤2h cases (94% of the
  slice) MAE ≈ **9.7 min**, median error ≈ **4.6 min** (target median 10). Top
  feature: `priority`.

- **Impact model (single): modest but honest.** ≤4h cases (88%) MAE ≈ **38.9 min**,
  median error ≈ **26 min**. Top features: `declared_window`, `num_affected_stops`,
  `category`, `hour`.

- **Relevance confirmed.** Within onset+12h at affected stops, the reopening slice
  coincides with **~61% of cancellations and ~64% of ≥15-min delays** — so it is
  genuinely the disruptive subset, not noise.

---

## How the scope narrowed from the original task

The original ask ("predict the duration of *a situation*") narrowed substantially —
each step defensible, but worth stating plainly:

1. **Population: all SIRI-SX → NSB only → the 7.8% reopening-labelled slice.** Only
   situations that emit a machine-readable reopening summary get a label (~7,922
   trainable rows). This is the acute unplanned-stoppage population; **planned works
   and any disruption that never "reopens" are excluded.**
2. **"Duration" was redefined and split.** The original implied a single
   validity-period duration; we instead deliver two operational targets
   (time-to-reopening and rail_calls-derived impact), because validity windows are
   unreliable (see data quality) and "duration" means different things to ops vs.
   passengers.
3. **Stop place, not quay.** Everything is at **StopPlace** granularity
   (`affected_stops` ∪ `rail_calls.stop_place` mapped to parent StopPlace via NSR).
   The requested quay-level resolution was not used.
4. **Geography contributes only modestly.** Multi-hot widths were cut (100→50,
   50→25) for the small slice; the models barely beat naive-median in aggregate, so
   location earns less than hoped.
5. **One operator.** NSB/Vy rail only — consistent with `rail_calls` (`VYG:%`).

---

## Data-quality & missing-data issues found

- **Reopening signal is sparse:** only **7.8%** of NSB situations ever emit a
  reopening summary — the core constraint on label coverage.
- **`planned` is 100% NULL** across the slice — dropped as a dead feature.
- **`version` is NULL for ~73%** of situations — ordering to find the first/last
  version must use `versionedAtTime`/`ingestionTime`, never `version` alone.
- **Unparseable timestamps:** ~2,782 of 10,861 labelled situations had NULL/
  malformed `versionedAtTime` and (on old partitions) missing `ingestionTime`,
  dropped by the 1–10080 min filter → ~7,922 usable.
- **Declared validity windows are unreliable:** populated for only **80%**, badly
  miscalibrated (median declared **114 min** vs observed reopen **10 min** —
  operators over-declare ~10×), and placeholder end times reach ~year 9999. Useful
  as a feature, not as a label.
- **The rail_calls impact label under-counts:** at the ≥15-min threshold, **47%** of
  the slice shows zero measured impact. Causes: sparse rural departures (a short
  closure may hit no scheduled train), affected stops that are empty or not present
  in `rail_calls` (bus-replacement / non-rail), and trains merely held a few
  minutes falling under the threshold. The ≥15-min / 60-min-gap / 12h-cap knobs
  trade this under-count against busy-hub baseline noise (the 5-min-delay base rate
  is 6%, which a loose gap would bridge into spurious multi-hour impact).
- **Reopening ≠ service restored** (timing): impact precedes onset and lingers after
  the reopening message — so neither label fully captures the passenger experience.
- **`affected_lines` is never populated by NSB** — no line/region feature available.
- **High-cardinality free text:** 894 distinct summaries — unusable raw; requires
  the category mapping.

### Technical gotcha

`create_training_initial.sql` uses `EXECUTE IMMEDIATE FORMAT(...)`; any literal `%`
in the templated SQL (even inside a comment) breaks `FORMAT`. Write percentages as
words or `%%`. Also: `training_initial` carries `situationNumber` as a row key and
**both** labels — every training SELECT must `EXCEPT(situationNumber,
reopen_minutes, impact_minutes)` to avoid leaking one label into the other model.

---

## Artifacts

Queries (`data/queries/`, run in order):
`create_disruptions_initial.sql` → `create_training_initial.sql` →
`train_dual_models.sql` → `create_quantiles.sql` (+ `train_impact_twopart.sql`,
explored then shelved).

Tables/models (`ent-kixavvik-dev:disruptions`): `disruptions_initial`,
`training_initial`, `dur_init_reopen`, `dur_init_impact`, `duration_quantiles`,
`dur_init_tail_clf`, `dur_init_tail_size`.

## Recommended next steps

- Ship the **reopen model + calibrated intervals**; present impact as a **range**.
- Try **per-category quantile offsets** for tighter intervals on common causes.
- Expose the **tail classifier** as a standalone "risk of extended disruption" flag.
- Add **weather / stop-reliability** features; revisit the impact threshold knobs.
