# kix-avvik — project learnings

_Team KI-eksperimentering · proaktiv avvikshåndtering · 2026-06_

This is the top-level synthesis across every workstream in this prototype. It is
written to answer one question for a reader who was not in the room: **given what we
now know, should this work continue, pivot, or stop?** Each section below rolls up
the three deep-dive write-ups and the project memory; read those for the numbers,
the SQL, and the gotchas behind each claim:

- [`predict-go-delayed-cancelled/LEARNINGS.md`](predict-go-delayed-cancelled/LEARNINGS.md) — the original duration task and the pivot to a per-trip outcome classifier.
- [`predict-duration/LEARNINGS.md`](predict-duration/LEARNINGS.md) — the reframed duration work (reopen / impact labels from SIRI-ET, intervals, ET & weather features, live re-prediction).
- [`huskehjulet/LEARNINGS.md`](huskehjulet/LEARNINGS.md) — E05 "disruption memory": does recurrence actually exist in the archive?

---

## The big picture in five sentences

1. The original task — **predict a disruption's duration in minutes from the SIRI-SX message** — does **not** work, because the only label SX offers (the declared validity window) is noise: it correlates **0.01** with what actually happens on the track.
2. The fix that unlocked everything was to stop trusting SX text as ground truth and **derive labels from SIRI-ET** (the real-time estimated timetable), which is what passengers actually experience.
3. With ET labels, two reframings *do* work and are shippable: a **per-trip GO/DELAYED/CANCELLED classifier** (a passenger "wait / take alternative / WFH" recommender) and a **time-to-reopen / time-to-impact estimator presented as calibrated intervals** (not point numbers).
4. Almost every fancy feature we hoped would help — geography, weather, watching ET delays accumulate during the disruption — turns out to be **"informative in isolation, redundant in the model"**, because by the time SX is posted the operator has already priced the severity into the `category` and the declared window.
5. The honest product shape is **modest models wrapped in calibrated uncertainty plus auditable recurrence memory**, not a precise oracle — and the binding constraint on shipping is now **organisational (a data-access grant), not modelling**.

---

## What we tried

Roughly in chronological order, across the three workstreams:

**Workstream 1 — original duration regression → per-trip outcome classifier**
1. Reviewed and bug-fixed the pre-existing `categories` / `disruptions` / `training` pipeline.
2. Trained boosted-tree **duration-in-minutes regressors** on the SX declared validity window, with multi-hot geography (top-N stations + OD corridors), benchmarked against naive baselines.
3. When that stalled, **re-labelled from SIRI-ET** — measuring *observed* outcomes instead of declared ones.
4. **Pivoted to per-trip outcome classification** (GO / DELAYED / CANCELLED for an individual planned boarding): v1 conditional on SX-flagged journeys, v2 unconditional over all traffic at affected stops (needed an NSR Quay→StopPlace bridge).
5. **Calibrated** (Platt) and built a **two-tier action policy**; tried a v2b window-tightening (12h→3h).

**Workstream 2 — duration, reframed honestly (the bulk of the work)**
6. Built a **leakage-safe, first-SX-version feature table** (`disruptions_initial`) so nothing reads the outcome at prediction time.
7. Defined **two ET-derived labels on one population**: `reopen_minutes` ("trains running again") and `impact_minutes` ("traffic impacted"), and trained dual boosted-tree regressors on `LN(label)`.
8. Built **empirical quantile intervals** (P50/P80/P90), later **per-category**.
9. Explored a **two-part / hurdle impact model** (tail classifier × tail-size).
10. Fed **pre-onset SIRI-ET state** (leakage-safe `knowable_delay`) back in as features for the impact model.
11. Built a **re-prediction-during-the-disruption** model ("impacted for ~Y *more* min"), with and without watching ET as it accumulates.
12. Added **weather features from frost.met.no** (155M met.no observations, areal aggregation StopPlace→nearest stations).
13. Built a **category × line × hour impact lookup** (`category_line_hour_impact`) for alerting, with baseline + lift and back-off.

**Workstream 3 — huskehjulet (E05 recurrence memory)**
14. **Recurrence census** over the archive (incidents per stop place × category).
15. **Precedent backtest**: predict each incident as the median of earlier similar incidents ("sist gang tok det X min"), scored against the trained models.
16. **Weather coupling** (GHCN-D daily, then frost) — do incident *types* cluster in weather bands?

**Productionization**
17. Shipped the **`/disruptions` FastAPI service on Cloud Run** (single-instance poller + in-process cache), serving live disruptions with category alerts and impact predictions.

---

## What did NOT work, and why

- **Duration-in-minutes regression from SX (the original task).** Every variant plateaued at ~240 min MAE / ~63 min median AE / 66% MAPE; geography barely moved it and corridors *hurt*. **Root cause is the label, not the features:** the SX declared validity window is largely defaulted and correlates **0.01** with ET-observed reality. This is the single most important negative result in the project.
- **Observed duration from the flagged journeys' time span.** Dead end — 94% of NSB situations flag only one journey, so the "duration" collapsed to one train's scheduled route length.
- **The two-part / hurdle impact model.** A respectable tail classifier (AUC 0.77), but no blend or decision rule beat the single impact regressor on the bulk — the decomposition just compounds three models' errors. Kept the single model.
- **Geography as a predictor.** Multi-hot stops/corridors, NSR region, and the huskehjul (stop+category) precedent all earn little-to-nothing over a category-only baseline — even on the infrastructure clusters where place "should" matter. Location is mostly already captured by category and line.
- **Watching SIRI-ET delays accumulate *during* a live disruption.** The re-prediction reframe is a big win (below), but the win comes from the **clock + the reopen event**, not from ET texture — `elapsed_min` + `minutes_since_reopen` substitute for the ET-so-far features almost completely. ET's only niche is the post-reopening congestion tail (~3%).
- **Weather (frost.met.no) as a duration feature.** The raw signal is real (snow/wind ≈ 2–2.5× median impact) but **redundant**: by the time SX is posted, severe weather is already priced into `category`, the declared window, and elevated pre-onset ET. Weather ranks 19th+ in feature importance. Its only (marginal, within standard error) niche is long-disruption *risk*.
- **The reopening label as a proxy for passenger impact.** Only 12–18% of cancellations/delays fall inside the onset→reopening window. "Toget kjører igjen" means *track clear*, not *service restored* — impact lands mostly after reopening. (This is what forced the separate `impact_minutes` label.)
- **Class regrouping to improve cancellation ranking.** Collapsing/splitting the 3-class outcome model doesn't help — cancellation separability is ~0.79 AUC regardless. Delays are the genuinely hard class; the lever is downstream calibration, not class layout.
- **The E05 storyboard's flagship physics.** The constructed-data story claimed signalfeil + "fuktig luft" as the weather-coupled type; the archive shows it's **sporvekselfeil + deep cold/snow** (2.5× frequency at < −5 °C), and weather affects *frequency, not duration*.

---

## What worked, and how well

- **Re-labelling from SIRI-ET.** The foundational unlock. One reusable `rail_calls` scan (the 26 TB ET table read once into a small table, 100% Quay→StopPlace mapped) made every downstream iteration cheap, and ET is the only trustworthy source of ground truth.
- **Per-trip outcome classification.** Genuinely learnable: ROC-AUC **0.81 (v1) / 0.78 (v2)**. After Platt calibration, a two-tier policy gives a shippable **"take alternative / WFH" tier that fires on 0.62% of trips and is 91% correct**, plus an "expect delays" tier. The strong features are the intuitive ones (`minutes_since_onset`, `lineRef`, `stop_order`); severity and planned carry ~zero signal.
- **Category mapping via AI.CLASSIFY.** Excellent — 99.8% of the slice joins to a category from ~894 distinct free-text summaries. The high-cardinality-text problem is solved. **Category is the single strongest predictor of impact** (trouble rate 8%→57% across categories).
- **Reopen model.** Usefully accurate on the bulk (94% of cases ≤2h): MAE ≈ **9.7 min**, median error ≈ **4.6 min**.
- **Quantile calibration — the headline win for impact.** Coverage lands on target and generalizes train→eval; per-category offsets separate fast causes (materiell/teknisk reopen P90 ≈ 24 min) from slow infra ones (infrastruktur/strøm ≈ 459 min). A calibrated **range** is the right comms artifact, given the impact point estimate is intrinsically noisy.
- **Pre-onset ET features for the impact model.** Clean, leakage-safe ~10–13% median improvement; 5 of the top 11 features become ET-derived. (Helps impact only — leave reopen alone.)
- **Re-prediction for live disruptions.** Refreshing the estimate as the clock runs beats projecting the static onset number forward by **−18% MAE / −29% median**, and the gap grows with elapsed time. The pragmatic production choice (`impact_remaining_noet`) needs no ET join at predict time — just the clock + onset features + the tracked reopen event.
- **Huskehjulet (E05) recurrence is real and the memory story holds.** 60 localized clusters with ≥4 incidents (e.g. sporveksel at Lillestrøm/Kløfta 29×, signal at Oslo S/Gardermoen 51×). **Precedent retrieval ("sist gang tok det X min") ties the trained models** on held-out rows while being fully auditable — arguably the right product shape, not a consolation. And the weather coupling, corrected, is a usable *preparedness* signal ("−8 °C i morgen → ~2.5× flere sporvekselfeil").
- **Category × line × hour alerting lookup.** A cheap (~cents, no ET scan) materialized table giving conditional impact (cancel/trouble rate, delay p50/p90) with unconditional baseline and lift, wired into the live API.
- **The `/disruptions` API.** Live on Cloud Run, serving ~180 disruptions with per-item category alerts and impact predictions.

---

## How the scope narrowed from the original task

The original ask — *"predict the duration of a disruption from its SIRI-SX message"* — narrowed substantially. Each step is defensible, but the cumulative effect is large and worth stating plainly:

| dimension | original | where we ended |
|---|---|---|
| **target** | one disruption **duration in minutes** | per-trip **3-class outcome** *and* split **reopen / impact** times as **intervals** |
| **label source** | SX declared validity window | **SIRI-ET observed reality** (SX validity is noise) |
| **population** | all SIRI-SX situations | **NSB rail only**, and for duration the **7.8% "reopening" slice** (~7.9k acute unplanned stoppages over 2y); planned works and never-reopening disruptions excluded |
| **decision unit** | the situation | individual planned boarding (journey × stop) / a single incident |
| **geography** | quay-level, expected to matter | **StopPlace** granularity, and geography earns little |
| **output** | a precise number | a **calibrated range** or an **auditable precedent**, because point precision isn't there |

The throughline: we kept the *intent* (help ops/passengers act before a disruption resolves) but abandoned the literal framing (a single trustworthy minutes number), because the data does not support it.

---

## Data-quality & missing-data issues we found

These are the durable facts about the source data — they will constrain any future work on the same tables:

- **SX declared validity window is unusable as a label** — defaulted, tightly banded, and **corr 0.01 with ET reality**. Any real label must come from ET. *(This is the finding most likely to bite a newcomer who tries the obvious thing.)*
- **The reopening signal is sparse:** only **7.8%** of NSB situations ever emit a machine-readable reopening summary — the hard ceiling on duration-label coverage.
- **SX versioning is messy:** `version` is **NULL ~73%** of the time with ~4.3 duplicate rows per situation — order/dedup by `versionedAtTime` / `ingestionTime`, **never** `version`. ~26% of labelled situations were lost to NULL/malformed timestamps.
- **NSB sparsely populates `affects`:** `affected_lines` **0%** (no line/region feature at all), `affected_journeys` ~71%, `affected_stops` ~54%. `planned` is **100% NULL**.
- **Identifier mismatch:** ET is keyed by `NSR:Quay`, SX `affected_stops` by `NSR:StopPlace` — no in-project bridge; unblocked via the National Stop Register.
- **ET physical layout:** 26 TB / 5.4B rows, day-partitioned, **no clustering** — operator/stop filters don't prune bytes, which dictated the "scan once into `rail_calls`" architecture.
- **The ET impact label under-counts:** at ≥15-min threshold, 47% of the slice shows zero measured impact (sparse rural departures, bus-replacement stops absent from ET, sub-threshold holds). The threshold / gap / cap knobs trade this under-count against busy-hub noise.
- **Timing mismatch:** impact both **precedes onset** (40% of situations already show impact before SX is posted) and **lingers after reopening** — neither label fully captures the passenger experience.
- **NULL-cancellation gotcha:** SIRI sets `call_cancelled` only when cancelled (NULL otherwise); naive `AVG(CAST(... AS INT64))` silently drops on-time trips and inflates rates — `COALESCE(... , FALSE)` everywhere.
- **Junk values:** ~0.44% negative durations; placeholder end-times reaching ~year 9999 — must be bounded.
- **Weather is coarse for the recurrence story:** GHCN-D is daily, one regional station, no humidity element (the "kondens" claim is untested, not refuted). Frost.met.no fixes the granularity but, as found, weather is redundant for duration anyway.
- **Free text is high-cardinality** (~894 distinct summaries) — unusable raw; the AI.CLASSIFY category lookup is mandatory, and category joins must be NULL-safe (IFNULL-sentinel, not `=`, not `IS NOT DISTINCT FROM` which blew the CPU limit).

---

## Cross-cutting lessons (true in every workstream)

1. **Trust ET, not SX text, for ground truth.** SX tells you a disruption was *declared*; only ET tells you what *happened*.
2. **"Informative in isolation, redundant in the model"** is the recurring shape — geography, weather, and ET-during all show a real raw signal that the model already gets through `category` + declared window + pre-onset ET. Stop adding leading indicators of things the model already sees.
3. **The clock is the strongest live feature.** For an ongoing disruption, elapsed time and the reopen event dominate every fancier signal.
4. **Calibrate, then surface uncertainty.** Raw boosted-tree probabilities are over-confident; calibrated probabilities and empirical quantile intervals are what's trustworthy enough to show a user.
5. **Honest modesty beats false precision.** Most methods barely beat naive medians, so the right products are *ranges* and *auditable precedents* ("sist gang tok det X min"), not a single confident number.
6. **One expensive scan, many cheap iterations.** The `rail_calls` design (read 26 TB once) is the reason this exploration stayed within budget.

---

## Production / deployment state (as of 2026-06)

- **Live service:** `kix-avvik-disruptions` on Cloud Run (`ent-kixavvik-dev` / europe-west1), public, single-instance, CPU-always-on (background poller + in-process cache). Serves `/disruptions` (with per-item category × hour alerts and impact predictions), `/situation`, `/alert_profile`, `/last_ingestion_time`.
- **⚠ The one real blocker is organisational, not technical.** The runtime service account has **no read access to Anshar's SIRI prod tables**; that access flows through a centrally-managed team group the SA isn't in, and Thomas can't grant it himself. **Pending action:** grant `application@ent-kixavvik-dev.iam.gserviceaccount.com` → `roles/bigquery.dataViewer` on `ent-anshar-prd:realtime` (via the EP "Data warehouse access" portal, or ask team-ror / team-dataplattform). The poller self-recovers within a minute once access lands — no redeploy needed.
- **⚠ Security debt:** to demo before the grant, the service currently authenticates as **Thomas's personal ADC** (refresh token in Secret Manager) in a *public* service. This must be torn down the moment the SA grant lands (cleanup steps are in the `cloud-run-deployment` memory). Treat this as a liability, not a solution.
- **Refresh chain** for the NSB lookup tables is cheap (~cents) and documented: `update_rail_calls` → `create_disruptions` → classify gap pairs → rebuild `trip_outcomes` + `category_line_hour_impact`. `disruptions` (SX), not `rail_calls` (ET), is usually the binding freshness constraint.

---

## Should we continue, pivot, or stop?

**What is genuinely shippable today** (modelling is done; these are products, not experiments):
- The **per-trip "wait / alternative / WFH" recommender** (Tier-1: 0.62% of trips, 91% correct) — the clearest passenger-facing win.
- The **reopen estimate + calibrated intervals** ("trains expected to run again in ~X min, up to Z").
- The **live-impact re-prediction** (`impact_remaining_noet`) + per-category intervals.
- The **category × line × hour alerting lookup** for driftssentral / operator alerting.
- The **E05 "huskehjul" demo built on real recurrence clusters** (Gulskogen–Drammen or Lillestrøm/Kløfta sporveksel) with the corrected cold-weather preparedness narrative — strictly more compelling than the constructed-data storyboard.

**Arguments to CONTINUE (turn prototype into product):**
- The hard, expensive groundwork is done: the ET re-labelling architecture, the Quay→StopPlace bridge, calibration, intervals, and a deployed service all exist and are reusable.
- There are validated, demoable artifacts for both audiences (passengers *and* driftssentral), each with honest, defensible accuracy claims.
- The remaining blocker is a **one-line IAM grant**, not a research problem.

**Arguments to be CAUTIOUS / consider a narrower PIVOT:**
- Point-prediction accuracy is fundamentally limited and we've now confirmed the obvious levers (geography, weather, ET-during) **don't** add orthogonal signal — there is little modelling headroom left to chase.
- Coverage is intrinsically narrow: **NSB rail only**, and duration models only cover the **7.8% reopening slice**. Generalising to bus/other operators or to non-reopening disruptions is unproven and may hit the same label-sparsity wall.
- The strongest results (per-trip classifier, recurrence retrieval, intervals) are all **modest-but-honest** — good enough to *advise*, not to *automate*. Frame any product as decision support, never as a precise SLA.

**Recommended posture:** the modelling exploration has reached **diminishing returns** — further feature work is low-value, and that question is well-settled. The value now is in **productionising the two or three honest wins** (per-trip recommender, calibrated reopen/impact intervals, the huskehjul recurrence demo) rather than chasing accuracy. The first concrete step is unblocking the **SIRI data-access grant** and retiring the personal-ADC stopgap; until that lands, nothing ships for real. If product appetite is low, the cleanest **stop** point is here, with the three deep-dive write-ups + this synthesis as the durable record — the negative results (SX label is noise; geography/weather/ET-during are redundant) are themselves the most valuable, reusable output for anyone who revisits disruption prediction at Entur.

**Do NOT re-run** (settled, with evidence): SX-validity-window duration regression; geography as a point-accuracy lever; weather as a duration feature; ET-during for live re-prediction; trip-outcome class regrouping for cancellation ranking. See the deep-dives before reopening any of these.
