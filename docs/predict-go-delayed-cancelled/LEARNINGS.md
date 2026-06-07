# Learnings — predicting disruption impact from SIRI-SX / SIRI-ET

Context: `kix-avvik` set out to *predict the duration of public-transport disruptions
from SIRI-SX situation messages using BigQuery ML*, for proactive disruption
handling. This document records what we tried, what worked, and what we learned
about the data along the way.

## TL;DR

- **Predicting disruption *duration in minutes* does not work** — not because of the
  features, but because the label (the SX *declared validity window*) is essentially
  noise: it correlates **0.01** with the disruption actually observed in SIRI-ET.
- **Reframing to a per-trip outcome classifier** (GO / DELAYED / CANCELLED for an
  individual planned boarding, labelled from ET) **does work** and is directly useful
  for a passenger *wait / alternative / work-from-home* recommendation.
- Best shippable result: a calibrated two-tier policy whose top tier — *"take an
  alternative / WFH"* — fires on **0.62%** of trips and is **91% correct**.

## What we tried

1. **Query/schema review** of `create_categories.sql`, `create_disruptions.sql`,
   `create_training.sql`; fixed bugs and modelling hazards (see per-file headers).
2. **Duration regression** (original framing): rebuilt the disruptions table
   (dedup to last version, collapse validity periods, bound the label), engineered
   geography features (multi-hot top-N stations + OD corridors), trained boosted-tree
   regressors (stops / corridors / both / no-geo) vs naive baselines.
3. **Re-labelling from SIRI-ET** once regression stalled: a POC measuring *observed*
   duration from real-time data.
4. **Pivot to a passenger-decision model**: per-trip outcome classification —
   first **v1 (conditional** on SX-flagged journeys), then **v2 (unconditional**, all
   traffic at affected stations, requiring an NSR Quay→StopPlace bridge).
5. **Calibration & policy**: Platt scaling + threshold sweeps + a two-tier action
   policy for both v1 and v2; plus a v2b window-tightening experiment (12h→3h).

## What did not work, and why

- **Duration-in-minutes regression (the original task).** Every variant plateaued at
  **~240 min MAE, ~63 min median AE, 66% MAPE**; geography barely moved it (no-geo
  243.8 vs both 240.7; corridors *hurt*). Root cause: the label is the **declared SX
  validity window**, which is largely defaulted (tight p75–p90 band of 305–344 min)
  and **corr 0.01 with ET-observed reality**.
- **Observed duration from the flagged journeys' time span.** Dead end: **94% of NSB
  situations flag only one journey**, so the span collapsed to one train's scheduled
  route length (~83 min median), not a disruption window.
- **Coarse geography (`region` / NeTEx authority).** Redundant with `participantRef`;
  for NSB it collapses to a single bucket (NSB never populates `affected_lines`).
- **v2b window tightening (12h→3h).** Modest mid-range precision lift but no gain at
  the high-confidence tail (the broad window captures easy all-day cancellations
  better). Temporal dilution wasn't the main issue — *spatial* dilution is.

## What worked, and how well

- **Per-trip outcome classification** is genuinely learnable: **ROC-AUC 0.81 (v1) /
  0.78 (v2)**. Strong features were the intuitive ones — `minutes_since_onset`
  (bad-rate decays 12.2% → 1.4% from 0–1h to 6–12h after onset), `lineRef`,
  `stop_order`. (`severity` and `planned` carried ~zero signal.)
- **Probability calibration (Platt)** fixed boosted-tree over-confidence cleanly
  (predicted 0.9 → 91% actual); both `P(cancelled)` and `P(not-GO)` became
  trustworthy to surface.
- **Two-tier policy (v2, held-out):**
  | message | share of trips | actually cancelled | actually delayed | ran fine |
  |---|---|---|---|---|
  | ① take alternative / WFH (`P(cancel)≥0.40`) | 0.62% | **91.0%** | 0.5% | 8.5% |
  | ② expect delays (`P(not-GO)≥0.30`) | 1.95% | 23.0% | 14.9% | 62.2% |
  | ③ looks OK (else) | 97.4% | 1.5% | 5.1% | **93.4%** |
- **Reusable ET architecture.** One `rail_calls` scan (~$19; 6.08M calls, **100%
  Quay→StopPlace mapped**) made all downstream v2 iterations cheap.

## How the scope narrowed from the original task

| dimension | original | where we ended |
|---|---|---|
| target | disruption **duration (minutes)** | per-trip **outcome (3-class)** |
| decision unit | situation | individual planned boarding (journey × stop) |
| operator / mode | all | **NSB rail only** (228 stations vs 34k bus stops) |
| data | SX only | SX **+ ET** (ET supplies the real label) |
| coverage | — | v1 conditional on SX-flagged journeys; v2 unconditional but harder |

## Data-quality / missing-data issues identified

- **Label quality (biggest finding):** SX declared-validity duration is unusable as
  ground truth (defaulted; corr 0.01 with ET). A real label must come from ET.
- **Versioning:** SX stores every version; `version` is **NULL ~73%** of the time and
  there are **~4.3 duplicate rows per situation** — dedup on `situationNumber` ordered
  by `versionedAtTime`, *not* `version`.
- **Sparse `affects` (NSB):** `affected_lines` 0%, `affected_journeys` 71%,
  `affected_stops` 54%.
- **Identifier mismatch:** ET is keyed by **`NSR:Quay`**, SX `affected_stops` by
  **`NSR:StopPlace`** — no in-project bridge; unblocked via the National Stop Register
  (`ent-data-sharing-ext-prd.national_stop_registry.quays_last_version`).
- **ET physical layout:** 26 TB / 5.4B rows, day-partitioned, **no clustering** — so
  operator/stop/journey filters don't prune bytes (only partition pruning does). This
  dictated the "scan once into `rail_calls`" design.
- **Junk values:** ~0.44% negative durations; placeholder end-times reaching ~year
  9999 (max 4.2B minutes) — need bounding.
- **Brittle join:** `categories` joins on exact `(summary, description)` text equality
  (inner join silently drops non-matches / NULL text).
- **Minor:** OD extraction from journey refs is inconsistent for `_M`-variant refs;
  `affected_lines` was originally polluted with journey refs.
- **Selection bias:** v1 is conditional on SX flagging the journey; v2 addresses this
  but at a much lower base rate (harder precision).

## Artifacts

Tables (`ent-kixavvik-dev.disruptions`):
- `disruptions` — one row per situation (deduped, bounded label, lines/journeys split)
- `categories` — AI.CLASSIFY incident categories
- `trip_outcomes` (v1, 1.06M), `trip_outcomes_v2` (34.3M), `trip_outcomes_v2b` (3h, 9.9M)
- `rail_calls` — reusable 2y rail call outcomes with StopPlace mapping (the one ET scan)
- Models: `trip_outcome_clf` (v1), `trip_outcome_clf_v2`, `trip_outcome_clf_v2b`
- Calibrators: `cal_cancelled`, `cal_disrupted`, `cal_cancelled_v2`, `cal_disrupted_v2`

SQL (`data/queries/`): `create_disruptions.sql`, `create_categories.sql`,
`create_training.sql`, `train_model.sql`, `create_trip_outcomes.sql`,
`create_rail_calls.sql`, `create_trip_outcomes_v2.sql`, `train_trip_model.sql`,
`calibrate_trip_model.sql`, `calibrate_trip_model_v2.sql`.

## Open follow-ups

- **Spatial sharpening** (highest-leverage untried lever): define the candidate area
  from the *corridor the flagged journeys actually traverse* rather than all declared
  `affected_stops`, to cut the spatial dilution that limits Tier-2 precision.
- Apply the v2 calibration + policy recipe to **v2b (3h scope)** for a
  tighter-horizon product.
- Add NSR municipality/county as features (names/coords already available).
- Calibrate and threshold on the actual product horizon; revisit the DELAYED band.
