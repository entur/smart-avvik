# SJ Norge (SJN) in Smarte reisevarsler — KIX-39

Session date: 2026-10-15. Ticket: SJN is the one rail operator `/varsel` does not support. The
ticket's premise was that SJN sends no SIRI-SX, so there was nothing to train on. It also raised two
other possible sources: Vy messages about shared stations (Oslo S, Lillestrøm), and Bane NOR
publishing SX on SJ's behalf.

**Decision: serve SJN through the existing rail family, zero-shot.** No new model and no retrain.
SJN trains enter the planned rail table, and three paths serve them unchanged: the no-SX path
(`has_situation = FALSE`, the 0.85 gate), the ET-cancellation relay and the «I dag» strip. On the
test snapshot (2026-10-15, 71 SJN trains scored) the no-SX path produced rows and nothing cleared
the gate. No SJN call was cancelled that day, so the relay has no SJN-specific predicate but was not
exercised. Every
number below was measured this session; the session billed about $1.30 of BigQuery.

## TL;DR

- **SJN's real-time and historical data were there all along, under Bane NOR's name.** SJN's trains
  publish ET through `dataSource = 'BNR'` with `lineRef = 'SJN:Line:*'` and the legacy ref
  `<train number>:<date>` (e.g. `47:2026-10-14`), in the top-level `datedVehicleJourneyRef`.
  That is about 200 trains a day on seven lines: F6, F7, R60, R65, R70, R71 and R75.
  `last_recorded` holds the same trains for the whole window built here (2024-06 onward), at ~30k
  realized calls and ~2,500 trains a month. The 2026-07-03 operator census looked under `dataSource = 'SJN'` and found only SJN's
  replacement buses, which is how it concluded SJN had no rail history. That conclusion was wrong.
- **The production rail heads work on SJN unchanged, and better than on Vy.** They were scored on
  the models' own temporal holdout (call_date ≥ 2026-04-01) as no-SX calls, with the no-SX Platt
  layer. Results are in the table below.
- **No special SJ model.** The one weakness is the cancel head on line R60. Retraining could fix it,
  but a retrain would collide with the in-flight `p_cancel` serving fix, and it would put NSB's
  production models at risk for ~0.3 false «innstilt» calls a day.
- **Bane NOR does not publish SX on SJ's behalf**: there is no `BNR` participantRef in 12 months of
  SX. SJN does publish its own messages, about 25–30 a month, and nobody else names an SJN train.
- **Vy's infrastructure messages carry real signal for SJN at shared stations (2.4×), but it is not
  alert-grade.** This is a follow-up, not part of this change.

## Data

| Source | What is there for SJN |
|---|---|
| Live ET (`siri_realtime_et_production`) | `dataSource = 'BNR'`, `lineRef LIKE 'SJN:%'`, ~200 trains/day, ref in `datedVehicleJourneyRef` as `<nr>:<date>`. Every estimated call has aimed times and populated boarding activities (no pass-through rows live). |
| History (`last_recorded`) | Same `dataSource`/`lineRef`, ref in `serviceJourneyId`. A cents-cheap scan (clustered on `dataSource`). 22 % of rows have no times at all and are not estimated; they are dropped by the realized-outcomes rule, as for GOA. |
| GTFS | `agency_id = 'SJN:Authority:SJN'` (stop names). |
| `dataSource = 'SJN'` | Replacement buses only (`SJN:DatedServiceJourney:B…`), and dead since 2026-05-13. |

Two other operators appear under `BNR` as well: `SJV` (SJ AB, F1/F8, ~10 trains/day) and `TM`
(a few trains/day). They are out of scope here, but they would take the same path.

`data/queries/create_sjn_calls.sql` → `disruptions.sjn_calls` (rail_calls schema, 2024-06 → 2026-10,
~30k calls/month). The realized cancel rate is ~1 % (Vy's no-SX rate is 0.19 %), with spikes in
Jan 2025, Jan 2026 and Oct 2026.

## Zero-shot gate

`create_training_calls_sjn.sql` builds SJN's calls in the exact shape of `training_calls`'
no-SX rows. `line_now_*` and `own_prev_delay` read the call's own line and train from `sjn_calls`.
`eval_sjn_zero_shot.sql` and `eval_sjn_horizons.sql` score SJN and Vy's no-SX holdout side by side.

At the training anchoring (lead 30):

| Head | Pop | n | Rate | Mean p | AUC | Prec @0.85 | Recall @0.85 |
|---|---|---|---|---|---|---|---|
| cancel | SJN | 195 440 | 0.0174 | 0.0132 | 0.938 | 0.80 | 0.10 |
| cancel | Vy | 1 821 079 | 0.0061 | 0.0045 | 0.939 | 0.98 | 0.04 |
| dep ≥ 10 | SJN | 187 463 | 0.0497 | 0.0553 | 0.902 | 0.93 | 0.43 |
| dep ≥ 10 | Vy | 1 656 693 | 0.0298 | 0.0286 | 0.873 | 0.95 | 0.15 |
| arr ≥ 10 | SJN | 168 014 | 0.0472 | 0.0529 | 0.920 | 0.93 | 0.48 |
| arr ≥ 10 | Vy | 1 643 125 | 0.0284 | 0.0277 | 0.888 | 0.95 | 0.18 |
| **p_impact_dep** | SJN | 190 544 | 0.0651 | 0.0673 | **0.900** | **0.918** | 0.36 |
| **p_impact_dep** | Vy | 1 666 641 | 0.0356 | 0.0330 | **0.876** | **0.951** | 0.14 |

The composite the alert gate uses (`p_impact_dep`), per ask horizon, each calibrated with its own
no-SX Platt bucket:

| Lead | SJN AUC | Vy AUC | SJN prec @0.85 (n) | Vy prec @0.85 (n) |
|---|---|---|---|---|
| 30 | 0.900 | 0.876 | 0.918 (4 842) | 0.951 (8 840) |
| 60 | 0.832 | 0.796 | 0.884 (3 368) | 0.879 (2 987) |
| 120 | 0.759 | 0.730 | 0.913 (1 498) | 0.838 (439) |
| 240 | 0.673 | 0.649 | — (0) | — (0) |
| plan | 0.506 | 0.527 | — (0) | — (0) |

SJN ranks better than Vy at every horizon. Its trains are long-distance, so delay propagates and
`own_prev_delay` carries more. At long leads the model under-predicts SJN (mean p 0.028 against a
0.065 rate at 240 min), because the no-SX layers were fit on Vy's lower base rate. Under-prediction
means fewer alerts, not false ones.

The weak spot is **«innstilt» wording on line R60**. `p_cancel ≥ 0.85` at lead 30 is right 75 % of
the time on SJN (Vy 98 %). R60 accounts for most of the misses: 135 calls flagged, 78 cancelled.
The other six lines are 84–100 % right. The R60 misses are trains observed running on time 30 min
earlier, in the evening, spread along the whole line. Raising the gate does not help, because the
misses sit at p ≥ 0.95 too. Teaching the trees R60 is the proper fix, by pooling SJN into
`training_calls`. That pool is nameable through `lineRef`, so it follows the rule in `docs/pooled-models/LEARNINGS.md`.
Do it after the `p_cancel` serving fix (`79a88cb`) has landed and been measured, not alongside it.

## The ticket's other two questions

**Bane NOR publishing on SJ's behalf: no.** Across 12 months of SX, the participantRefs are NSB,
VYG, GOA, SJN, FLT and the bus publishers; there is no `BNR`. No publisher other than SJN names an
SJN journey ref or an SJN line.

**SJN's own SX** (239 situations in 12 months, rising to ~25–30 a month): these are templated
per-departure notices such as «SJ Nord kjører buss for tog», «Vi anbefaler at du tar neste tog» or
«Toger er innstilt», with refs like `SJN:DatedServiceJourney:433_LMO-STK_26-10-13`. Stripping the
codespace does not bridge them to ET. A train-number + date extract does, for 69 % of the 948
refs (658), and the rest name trains ET never published that day. Of the joined trains, 160 were
cancelled outright, 274 partly, 105 ran ≥ 10 min late and 119 ran normally. The messages report
outcomes that have already happened. So their value is **the explanation text on an alert ET
already triggers**, not prediction. That is a follow-up.

**Vy messages at shared stations** (`eval_sjn_shared_station_lift.sql`). An SJN call counts as
flagged when an NSB situation of an `infrastruktur/*` category names its stop place 0–180 min
before the call. The comparison runs on the SJN holdout at stations that appear in such messages:

| | n | Impact rate | Model mean p (lead 30) | Model mean p (plan) |
|---|---|---|---|---|
| flagged | 689 | 0.253 | 0.145 | 0.022 |
| not flagged | 17 016 | 0.106 | 0.116 | 0.021 |

That is a 2.4× lift, and the model misses ~10 points of it. But 25 % is nowhere near the 0.85 gate,
and only ~3.5 calls a day are flagged. The lift is worth having as a feature: a stop-keyed situation
branch with its own training population. As an alert trigger on its own it is not. Either way it
belongs in a follow-up ticket.

## What changed in serving

| File | Change | Why |
|---|---|---|
| `rail_calls_planned_today.sql` | `lineRef LIKE 'SJN:%'` beside `VYG:%`; GTFS names from `SJN:Authority:SJN` too | SJN trains enter `/impact`, `/varsel`, the ET relay and the «I dag» strip. The ET scan reads the same bytes; GTFS adds 0.3 GB to 31.8 GB. |
| `predict_calls.sql` | `netpre` filtered to `VYG:%` | On one live snapshot, 1 110 of 8 694 Vy prediction rows changed when SJN joined the network average. With the filter, none did. |
| `togvarsel_outcomes_provisional.sql` | both planned reads filtered to `VYG:%` | F6 calls at Oslo S and Lillestrøm would otherwise count in Rushvarsel's provisional labels, while the settled label is Vy-only. |
| `alternatives.py` | `cache_key` rewrites JP's SJN dated ref to the ET key | JP says `SJN:DatedServiceJourney:49_OSL-TND_26-10-15`, ET says `49:2026-10-15`. Without the rewrite, a late SJ train can be offered as its own alternative. |
| `data/queries/label_varsel_decisions.sql` | reads SJN from `dataSource = 'BNR'` | Otherwise SJN decisions stay «ukjent» in `/historikk` forever. **The scheduled query must be re-registered** in dev and prd. |
| `historikk.py`, `notification_pull.py`, `journey_status.py`, «I dag» caption | SJN added to the operator lists; «Ingen direkte tog» | `/historikk`'s operator filter returned 400 for SJN, and the coverage lists said Vy only. |

Checked and left alone: `rail_calls_removed_today.sql` (GTFS filtered to VYG, and SJN jkeys never
match its regexes); `varsel_connectivity.sql` (SJN-only stop pairs correctly become servable);
`line_modes.py` (already maps SJN to «Tog»).

## Not covered by the measurement

- **Live feature skew.** The numbers above come from the training pipeline. Live serving on `main`
  still measures `own_prev_delay` and `line_now_*` from `CURRENT_TIMESTAMP()`, which the unmerged fix
  `79a88cb` (branch `feat/p-cancel-recalibration`) corrects. SJN's stops are far apart (Oslo S →
  Lillestrøm → Gardermoen → Hamar), so "no observed stop ≥ 30 min back" is more common per call than
  on L1. Expect live SJN `p_cancel` to run high until that fix lands. Land it before taking SJN to
  prd.
- **Scoring cost.** The planned build reads the same ET bytes, but `predict_calls.sql`'s input
  grows: SJN was 2 781 of 17 979 planned rows (15 %) on the test snapshot. Read `poller_phase` for
  `predict_calls_*` after the dev deploy to see what that does to the ~0.9 GB of `ML.PREDICT` per
  rebuild.
- **Conflict surface.** This branch and `feat/p-cancel-recalibration` both touch
  `predict_calls.sql` and `rail_calls_planned_today.sql`, in different hunks, so whichever lands
  second rebases cleanly.

## KIX-42: SJ's own SX (2026-10-24)

SJN situations through the **flagged** path: `data/queries/eval_sjn_flagged.sql` builds them in
`training_calls`' `base` shape (situation × call, 0–720 min from onset) for all of 2024-06 to
2026-10. SJN was in no training row and no Platt fit, so all of it is out of sample. Network state
comes from Vy (as serving's `netpre` does), and category from the same AI.CLASSIFY call serving
makes. 240 situations, 341 trains, 4 518 calls, 48 % impacted. Vy's flagged holdout sits beside it.

Composite `p_impact_dep` at the default 0.65 bar, with «innstilt» wording (`p_cancel` ≥ the same bar):

| Lead | SJN AUC | Vy AUC | SJN prec / recall | Vy prec / recall | SJN «innstilt» | Vy «innstilt» |
|---|---|---|---|---|---|---|
| 30 | 0.912 | 0.930 | 0.870 / 0.731 | 0.903 / 0.619 | 0.774 | 0.902 |
| 60 | 0.878 | 0.877 | 0.929 / 0.521 | 0.937 / 0.355 | 0.835 | 0.961 |
| 120 | 0.801 | 0.830 | 0.908 / 0.290 | 0.960 / 0.143 | 0.812 | 0.978 |
| 240 | 0.743 | 0.804 | 0.875 / 0.141 | 0.977 / 0.075 | 0.788 | 0.980 |
| plan | 0.662 | 0.799 | 0.867 / 0.207 | 0.939 / 0.242 | 0.759 | 0.962 |

Recall is far above SJN's no-SX path (0.73 vs 0.36 at 30 min, 0.52 vs 0.24 at 60): a message
makes SJ trains visible. The weak spot is «innstilt»: many SJN messages describe a part-cancelled
train («buss for tog fra Mørkved»), so whole-train certainty is wrong for the calls that ran.
**SJN's flagged bar is 0.75** (`alert_policy._OPERATOR_ALERT_THRESHOLD`): precision 0.922 / 0.931 /
0.944 at 30 / 60 / 120 min, «innstilt» 0.819 / 0.890 / 0.908, recall 0.70 / 0.46 / 0.22. Even the
wrong «innstilt» were 90 %+ trains that ran ≥ 10 min late.

Two data quirks the serving change handles:
- SJ names one train under two refs (`…_STK-LMO_26-10-13` and `…_STK-LMO_M_26-10-13`). Both bridge to
  one jkey, so the join is DISTINCT and `num_journeys` counts trains, not refs. Uncorrected, the
  rows tripled in the eval (7 149 rows, 4 518 distinct).
- 6 of 267 SJN situations (2.2 %) carry unfilled template fields (`{START1}`, `{STASJON}`); they are
  dropped. 0 of 119 807 NSB messages do.

End to end on scratch tables (2026-10-24): today's one SJN situation, a capacity notice for train 43
Oslo – Trondheim, reached trains 43 and 48 as 30 flagged rows (max `p_impact_dep` 0.57, under the
bar). All 8 136 Vy prediction rows came out byte-identical to `main`'s.

## Follow-ups

1. **R60's false «innstilt»**: pool SJN into `training_calls` once the `p_cancel` serving fix has
   landed and been measured. `create_training_calls_sjn.sql` is already the pool's shape.
2. ~~**SJN's own SX as explanation text.**~~ Done in KIX-42, above.
3. **Vy infrastructure messages as a stop-keyed feature for SJN** (the 2.4× lift above).
4. **Fresh actuals for SJN**: `rail_calls_today` is still Vy-only, so the «I dag» strip reads SJN's
   recorded calls from the planned table, up to 60 min old. Widening it touches Rushvarsel and the
   labeller, so it is a change of its own.
5. **SJV and TM** (also under `BNR`) would take the same path if wanted.
