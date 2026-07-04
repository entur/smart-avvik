# All-operator pooled per-call models: rail + bus/local transport

**Date:** 2026-07-04 · **Status:** experiment complete — bus works, but the strict NSB
parity gate FAILS; nothing serves from this family.
Phase 2 of the pooled-models plan: after [the rail NSB+GOA build passed its parity
gate](../pooled-models/LEARNINGS.md), this build extends the pool to the bus-side SX
publishers — GCO, MOR, NOR, TRO, FIN and RUT — which resolve to eleven local-transport
codespaces on the ET side.

## TL;DR

- **Bus per-call models work.** Ten new operators get usable impact stacks in one build:
  cancel AUC 0.88–0.997, p_impact AUC 0.81–0.97, and at the serving threshold (0.65)
  precision holds at 0.84–0.96 for every real operator. RUT — the "maybe also" operator —
  is among the best (cancel 0.977, impact ~0.916, ~91% precision at 77–81% recall), on
  104k holdout calls.
- **But pooling is no longer free for NSB.** With bus at ~63% of training rows (vs 2%
  GOA in the rail pool), NSB drops 0.4–0.5 AUC points and 3–6 recall points at the
  threshold vs prod (cancel 0.9553 vs 0.9603; impact 0.926/0.920 vs 0.930/0.923).
  Precision is intact (−0.3 to −0.5 pt); the family just alerts less. GOA slips ~0.2–0.6
  pts vs the rail-only pool for the same reason. The `operator` feature isn't enough —
  boosted-tree capacity is shared, and NSB is now a minority class.
- **Consequence: one family should not serve everyone.** Rail keeps its gate-passed
  family (prod or `_pooled`); bus gets its own family — these `_pooled_all` models are
  perfectly good AS that bus family (the rail rows in the pool don't hurt bus).
- **Operator-aware platts calibrate everyone at once**: every operator's mean calibrated
  P(cancel) lands within ~1.5 pt of realized (NSB 10.7 vs 10.3, KOL 32.4 vs 31.1,
  TRO 60.2 vs 58.6) from ONE fit per head with per-operator intercepts.
- **Cost ≈ $3.7 total** (models+platts $2.43, bus_calls $0.57, training table $0.39,
  recon/eval ~$0.3); ~75 min wall, dominated by the 300-iteration cancel model (~25 min)
  and everything else in parallel beside it.

## Who publishes what (SX side → ET side)

The bus participants publish plain `ServiceJourney` refs (NOT date-scoped, unlike rail's
`DatedServiceJourney`). GCO — the automated per-departure notice tool from the census —
fans out across county codespaces; the others are 1:1:

| participant | ref codespaces (situations since 2025-03) | joined sits | affected calls | cancel rate | dep≥10 rate |
|---|---|---|---|---|---|
| RUT | RUT (74k) | 58k (78%) | 704k | .12 | .37 |
| GCO | KOL (46k), SOF (43k), NOR (37k), SKY (15k), ATB (4k), OST (3k), FIN/MOR/GCO (2k) | ~140k (~91%) | 944k | .13–.91 | .02–.13 |
| MOR | MOR (26k) | 22k (87%) | 397k | .04 | .36 |
| NOR | NOR (7k, ~3 journeys/sit) | 6.9k (98%) | 339k | .26 | .07 |
| TRO | TRO (3.1k) | 3.0k (97%) | 146k | .30 | .21 |
| FIN | FIN (1.8k) | 1.8k (98%) | 71k | .30 | .24 |

`operator` (the pooling feature) is therefore the **ref's codespace**, not the publisher:
propagation features must key on the network the journey runs in, and NOR carries two
publishers (NOR + GCO) that fold into one network. Rail keeps `operator` = participantRef
(NSB→rail_calls/VYG, GOA) exactly as in the rail-only pooled build. BRA/BFO/TEL/VKT
codespaces were dropped (<1k refs combined; BRA alone would add 24M call rows).

## What was built

| Artifact | Script | Notes |
|---|---|---|
| `disruptions.bus_calls` (439M calls, 13 prefix operators) | `data/queries/create_bus_calls.sql` | last_recorded slice of 11 dataSources, ~90 GB / ~$0.57; operator = serviceJourneyId prefix; keeps `operating_date` |
| `disruptions.training_calls_pooled_all` (3.63M rows, 13 operators) | `data/queries/create_training_calls_pooled_all.sql` | 62 GB / ~$0.39; rail slices **bit-identical** to `training_calls_pooled` (BIT_XOR FARM_FINGERPRINT match) |
| 11 `*_pooled_all` models | `data/queries/train_call_models_pooled_all.sql` | identical contracts, `enable_global_explain=FALSE` |
| 9 `*_platt_pooled_all` layers | `data/queries/create_pooled_all_calibration.sql` | **operator-aware**: logit + operator → per-operator intercepts (bus is the majority of the holdout at 3–5× NSB base rates; a shared scalar fit would mis-center everyone) |
| Eval | `data/queries/eval_pooled_all_models.sql` | pooled_all by operator + prod NSB baseline, same pipeline as the rail eval |

### The three structural deltas vs the rail-only pooled build

All three are provable no-ops for rail — verified by the fingerprint match above.

1. **Date-bounded label join + first-instance rule.** A bus jkey repeats every operating
   day (~470 instances in the window). Each ref expands to the 4 candidate `call_date`s
   around onset and equi-joins `(operator, jkey, call_date)`; a `QUALIFY` then keeps only
   the FIRST instance after onset per (situation, jkey, stop_order) — a per-departure
   notice is about the next departure, not tomorrow's same-numbered one. For rail the
   0..720-min filter already confined matches to that window, and `rail_calls` has zero
   duplicate (jkey, stop_order) keys, so both changes leave rail untouched.
2. **Hour-bucket propagation joins.** `line_pre_*`/`net_pre_*` were lineRef/operator
   range-joins; against 439M bus calls they expand to ~1e13 candidate pairs. A 60-min
   window spans at most 2 hour buckets, so joining on (key, hour-bucket) and filtering
   the exact window afterwards returns identical values cheaply.
3. **Instance-scoped `own_prev_delay`.** The own-journey join pins
   `operating_date` (NULL sentinel for rail, where DSJ jkeys are already date-scoped).

## Data facts worth remembering

- **`vehicleMode` is NULL for most bus producers** (RUT's 195M rows, SKY, TRO, OST, NOR,
  FIN all NULL) — you cannot mode-filter bus codespaces; slice by dataSource and take
  everything. SOF/GCO are the request-stop ferry networks behind the "Bestillingsanløp"
  notices (GCO participant = mostly ferry+bus counties: KOL/SOF/NOR/SKY).
- **Ref prefix ≠ dataSource for a small slice**: GCO:ServiceJourney journeys ride under
  SOF/KOL delivery. bus_calls keys operator on the prefix, which is what SX refs join.
- **SKY under-delivers**: ~34M usable calls of ~97M raw rows (non-ServiceJourney ref
  formats); SX join rate for SKY refs still ~85%.
- **Realized-outcome labels are available everywhere**: 80–99% of rows carry recorded
  (non-estimated) actuals; ET cancellation base rates 0.1–0.9% (SOF 5.9%, SKY ~0.02%).
- **Category coverage is a non-issue**: `disruptions.categories` matches 99–100% of bus
  situations' onset texts (shared templates again).
- **Status mix varies by publisher**: KOL/SOF ≈ 90% `other` (request-stop notices),
  MOR/RUT lean `forsinket` (delay echoes), FIN/TRO/GCO lean `innstilt`.
- **The extraction fix earns its keep**: RUT/KOL refs only exist in
  `affected_journeys` because of the 2026-07-03 `vehicleJourneyRefs` extraction fix.

## Eval (temporal holdout, onset ≥ 2026-04-01; thr = 0.65)

The NSB parity comparison (prod family recomputed on the same pipeline):

| metric | family | n | base | AUC | prec@.65 | rec@.65 |
|---|---|---|---|---|---|---|
| cancel | prod | 262,647 | .103 | **.9603** | .933 | .587 |
| cancel | pooled_all | 262,647 | .103 | .9553 | .929 | .526 |
| impact_arr | prod | 250,697 | .187 | **.9304** | .912 | .589 |
| impact_arr | pooled_all | 250,697 | .187 | .9261 | .909 | .552 |
| impact_dep | prod | 245,176 | .189 | **.9234** | .911 | .567 |
| impact_dep | pooled_all | 245,176 | .189 | .9195 | .906 | .536 |

The rail-only pooled build sat within ±0.0004 of prod on all three — this one loses
0.004–0.005 AUC and ~6 recall points. **Gate: FAILED** (deliberately strict: any real
NSB regression blocks promotion of a shared family).

Per-operator results of the pooled_all family (the bus deliverable):

| operator | cancel n / base / AUC / prec / rec | impact_arr AUC / prec / rec | impact_dep AUC / prec / rec |
|---|---|---|---|
| RUT | 104,101 / .13 / .977 / .93 / .77 | .916 / .92 / .80 | .915 / .90 / .81 |
| KOL | 79,260 / .31 / .879 / .90 / .42 | .838 / .89 / .39 | .841 / .89 / .34 |
| MOR | 76,722 / .04 / .993 / .96 / .69 | .912 / .85 / .71 | .910 / .84 / .71 |
| SOF | 43,176 / .38 / .968 / .92 / .80 | .953 / .92 / .78 | .943 / .92 / .78 |
| TRO | 16,798 / .59 / .969 / .94 / .96 | .947 / .94 / .91 | .943 / .93 / .91 |
| SKY | 16,405 / .31 / .929 / .91 / .53 | .906 / .91 / .56 | .890 / .90 / .59 |
| NOR | 12,383 / .15 / .923 / .81 / .35 | .898 / .89 / .55 | .904 / .88 / .58 |
| FIN | 7,812 / .14 / .997 / .94 / .89 | .935 / .90 / .80 | .938 / .91 / .82 |
| GOA | 5,420 / .51 / .984 / .96 / .91 | .964 / .96 / .89 | .961 / .96 / .89 |
| ATB | 3,270 / .37 / .982 / .96 / .87 | .970 / .96 / .81 | .961 / .96 / .85 |
| OST | 1,616 / .25 / .926 / .84 / .09 | .923 / .82 / .09 | .808 / .79 / .10 |
| GCO | 60 / .70 / .90 / .95 / .93 | (58 rows) | (45 rows) |

**Calibration** (operator-aware cancel platt, mean calibrated p vs realized): NSB
.1065/.1034, RUT .1355/.1309, KOL .3236/.3106, MOR .0427/.0400, SOF .3912/.3799,
TRO .6022/.5860, SKY .3174/.3053, NOR .1567/.1500, FIN .1540/.1440, GOA .5138/.5052,
ATB .3783/.3664, OST .2676/.2525 — every operator within ~1.5 pt, mild uniform overshoot
(the usual fit-on-holdout optimism).

### Reading the results

- **The NSB regression is dilution, not contamination.** Features stay leak-free and
  per-operator (fingerprint-verified); what changed is that NSB fell from ~98% to ~36%
  of training rows, so tree capacity now goes to bus patterns. Losses concentrate in
  recall at the fixed threshold (fewer alerts) rather than precision (alerts stay
  trustworthy).
- **Why most bus operators read "easy" (like GOA):** per-departure notices often
  announce their outcome ("Avgangen er innstilt", "ca. 15 min forsinket"), so much of
  the AUC is structured parsing of confirmations plus propagation to the journey's
  downstream calls — valuable, but not long-horizon prediction. The lead-time question
  is untested for bus.
- **KOL is the hard one** (AUC .84–.88, recall ~.4): 93% of its situations are
  status=`other` request-stop notices whose outcome is genuinely mixed (31% cancel
  base) — the message text doesn't give the answer away, so the model has to actually
  predict.
- **OST's recall collapse** (.09 at prec .82) is a threshold artifact on a small,
  drifted operator — its AUC is fine (.92); a per-operator threshold or full
  per-operator platt (not just intercept) is the knob.
- **FIN shows the platt architecture working**: train cancel base .42 → eval .14, yet
  calibration holds (.154 vs .144) and AUC is .997.

### Caveats

- GCO-codespace eval slice is 60 rows — ignore it. OST (1.6k) and ATB (3.3k) are small.
- FIN and TRO have strong train→eval base-rate drift (FIN cancel .42 train → .14 eval;
  TRO .38 → .59): small-operator quarters differ, so threshold metrics are noisy.
- The platts are fit on the same holdout they're evaluated on (same caveat as prod and
  the rail pooled build; both families identically affected).
- Jan 2026 GOA producer gap still under-counts that month's labels.

## Where this leaves the architecture

The two builds together answer the pooling question with a size condition:

- **Pooling helps when the newcomer is small** (GOA at 2% of rows: free for NSB, +3 AUC
  for GOA vs zero-shot).
- **Pooling costs the incumbent when the newcomer dominates** (bus at 63%: −0.005 AUC,
  −6 recall pts for NSB).

So the serving shape should be **two families**: the rail family (prod today; `_pooled`
NSB+GOA when promoted) and a bus family — for which these `_pooled_all` models qualify
as-is. If a leaner bus family is ever wanted, retrain on the bus rows only; nothing in
this eval suggests bus needs the rail rows.

## Not done — gates before any serving change

Same list as the rail pooled build (per-ask-horizon platts, NSB/VYG-hard-coded serving
path, lead-time analysis, promotion decision), plus:

- live serving for bus would need anshar TTL scans per codespace (last_recorded is a
  daily batch — training-only);
- the `/disruptions` API and its alert consumers are rail-scoped by product intent;
  whether bus alerts are wanted at all is a product question, not a modeling one;
- KOL (and OST) would need either a per-operator threshold or a full per-operator platt
  before their alerts are useful at meaningful recall.

## Repro

```sh
bq query --nouse_legacy_sql < data/queries/create_bus_calls.sql                    # ~$0.57
bq query --nouse_legacy_sql < data/queries/create_training_calls_pooled_all.sql    # ~$0.39
# train: run each statement as its own job (see file header)
# calibrate: each statement its own job
bq query --nouse_legacy_sql < data/queries/eval_pooled_all_models.sql
```
