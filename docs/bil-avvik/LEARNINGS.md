# Reisevalg-varsel (bil → kollektivt): feasibility findings

Investigation of the concept in `Entur, fra bil til kollektivt.pdf` ("Proaktivt
reisevalg-varsel"): when E18 is congested and the parallel trains run normally, nudge the
commuter — "i dag lønner det seg å ta toget, du sparer ca. 20 min". Corridor: Lier/Drammen →
Oslo S. Everything below was measured 2026-07-11 against real data (sources and verification
dates at the end); the plan is `docs/plans/2026-07-11-001-feat-reisevalg-varsel-feasibility-plan.md`.

## Executive verdict

**Partly possible today, fully possible only with better road data — and the honest version of
the concept is a rarer, more modest nudge than the pitch.**

- **Possible now:** the whole serving pipeline is built and live-ready (`/reisevalg-api/
  recommendation` on the existing kix-avvik API): rail-side realtime + predictive check,
  five honest states, park-and-ride static info, Norwegian copy. The rail leg and the car
  free-flow baseline work from open data today.
- **Possible with DATEX access or a bus-probe provider:** live queue *detection* and the
  "du sparer ca. N min" *number*. Open hourly traffic volumes cannot carry a live nudge —
  measured precision 0.60 against a 0.8 honesty bar, and the data is published too late
  (rush-onset congestion becomes visible ~10:30, after the commute window). The endpoint
  therefore ships with the volume signal gated to `UNKNOWN` — it never fires the nudge.
- **Not possible (from data anyone holds today):** knowing *who* drives (PDF question 01),
  rail capacity headroom (question 03), and real-time parking availability. And on this
  corridor the opportunity itself is small: even with a *perfect* road detector, "road bad ∧
  rail fine" occurs on ~2% of clean weekday mornings (~4–7 per year), because two thirds of
  genuinely bad road mornings have a degraded railway too (winter weather hits both modes).

Per claim in the PDF's mock notification:

| Mock claim | Supportable from open data today? |
|---|---|
| "Det bygger seg kø på E18" | **No (live).** Hourly volumes: precision 0.60 < 0.8 bar, published 134–194 min late. **Yes with** DATEX travel times (registration-gated, no archive) or a SIRI-ET bus-probe provider (in-house, minutes-fresh — strongest candidate). |
| "Togene Lier–Drammen–Oslo S går som normalt" | **Yes (live)** — JP realtime departure board + kix-avvik per-call predictions. But it is only *true* on 52–58% of clean weekday mornings at morning granularity — the weaker premise leg of the pitch. |
| "Du sparer ca. 20 min" | **No.** Requires a measured congested drive time. Break-even analysis: the car must lose ≥ ~7.5 min to queues before the train wins in expectation (≥ ~22 min if you just missed one). The number is claimable the day a travel-time source exists; the endpoint already computes it then. |
| "Ledig plass på Lier innfartsparkering" | **No.** Real-time occupancy lives only in Bane NOR's proprietary app backend (verified — no open feed). Static capacity only; the API field says exactly that. |

---

## The four questions from the PDF

### 02 — "Er kødata presise og fremoverskuende nok til å stole på?" (answered empirically)

**Open kødata: no. Registration-gated or in-house kødata: promising. Nothing is
forward-looking; everything is a nowcast.**

- **What exists openly** is SVV Trafikkdata (GraphQL, no auth): hourly volumes per direction,
  2+ years of history, dense E18 coverage (we ingested 10 points Lier→Oslo, 340,504 hourly
  rows, 2024-06 → 2026-05, `disruptions.svv_hourly_volumes`). No speeds, nothing sub-hourly.
- **Volume is a weak queue proxy.** Congestion shows as volume *deficit* (throughput
  collapse — visible in routine rush: Holmen's toward-Oslo volume at 08:00 drops below its
  06:00 value). We built per point × direction × weekday × hour baselines
  (`disruptions.svv_baselines`) and tested 8 deficit variants against a ground-truth set of
  24 congested mornings (bus-probe run-time inflation ∪ SX incident mornings — the ≥15-morning
  evidence bar was met only by the union of two individually weak channels):

  | flag (deficit rule) | flagged | confirmed | precision | recall |
  |---|---|---|---|---|
  | d15 (vol < 0.85×median, ≥50% points, 2 h persistence) | 10 | 6 | **0.60** | 0.25 |
  | d25 (same at 0.75×) | 5 | 2 | 0.40 | 0.08 |
  | p10 rule | 26 | 9 | 0.35 | 0.38 |
  | rush volume < 85% of median (hindsight) | 9 | 4 | 0.44 | 0.17 |

  No variant approaches the 0.8 precision bar the plan set for a consumer-facing nudge. False
  positives are calendar-missed demand dips (New-Year tail, pre-fellesferie Friday).
  corr(rush-volume ratio, bus run-time inflation) = −0.15 — right sign, weak.
- **Timing is structurally fatal regardless of precision.** Measured current-hour publication
  lag: 134–194 min. Simulated live detection with a 150-min lag: of 5 flagged mornings, **0
  were detectable by 07:00 or 08:00**; 3 by 09:00 (all pre-dawn-starting events); rush-onset
  (06–07 h) congestion becomes visible ~10:30 — after the window. The hourly feed can explain
  yesterday; it cannot power a morning nudge.
- **What would work:**
  - **SVV DATEX II `GetTravelTimeData`** — measured travel times on E18 segments, ~5-min
    cadence. Registration-gated (org details + fixed IP; request prepared on day 1, see
    `datex-access-request.md`), and **no historical archive exists** — archiving must start
    the day access lands, which is why the request was filed before the verdict.
  - **Entur's own buses as probe vehicles** (the investigation's most useful discovery):
    RUT line 250 (IKEA Slependen → Vika) and 160 (Sandvika → Vika) drive the corridor with
    21k weekday-morning runs in 2 years of SIRI-ET history (`last_recorded`, ~$0.24 to scan).
    Realized run-time inflation is a direct, minutes-fresh travel-time signal with the same
    semantics DATEX would give — backtestable, already in-house, no registration. Caveat: bus
    lanes on parts of the stretch damp their exposure to car queues. This is the recommended
    next live road provider (`RoadObservation` interface is ready for it).

### 01 — "Vet vi hvem som faktisk kjører bil på strekningen i dag?"

Not answerable from any data Entur or SVV holds (volumes count vehicles, not identities).
Would require app-side opt-in (commute profile / home-work registration in the Entur app) or
panel data. The prototype sidesteps it: the endpoint answers "is the train the better choice
now" for whoever asks.

### 03 — "Tåler toget økt kapasitet hvis dyttet faktisk virker?"

Not answerable from this repo's data (no occupancy). Requires Bane NOR / Jernbanedirektoratet
capacity and load data (see r4 catalog). Worth noting the scale: ~4–7 nudge mornings/year ×
modest conversion is unlikely to strain capacity — but that is an argument, not a measurement.

### 04 — "Hvordan måler vi at en bilreise faktisk ble konvertert?"

Design sketch only (declared non-goal): nudge-shown → journey-registered/ticket-activated
within the window as the app-side funnel; parking occupancy deltas at Lier as the physical
check (requires the Bane NOR data that is not open); a holdout group for causality.

---

## What the backtest says about the concept itself

380 usable weekday mornings (2024-12-15 → 2026-05-31 — `rail_calls` has no usable passenger
journeys before the 2024-12-15 timetable change, a surprise that cut the nominal 2-year window
to 17.5 months), of which 299 clean (non-holiday, non-closure); 23 closure mornings cluster in
the 2025 summer engineering works.

- **Rail-normal rate: 52% (strict variant) / 58% (loose)** — "togene går som normalt" at
  morning granularity holds only about half the time. Cancellations touch 7.7% of clean
  mornings; the rest of the degradation is arrival lateness at Oslo S.
- **Opportunity rate (road bad ∧ rail normal): 0.7–2.3% of clean mornings** depending on
  detector — 2 mornings at the backtested volume thresholds, 6–7 with a *perfect* detector.
- **Conflict dominates: 63–75% of bad-road mornings had degraded rail too** (e.g. the
  2025-01-06..08 snow days) — mornings where the nudge must stay silent, and where a naive
  "road is bad → take the train" push would have been wrong exactly when it looked most
  tempting.
- **Break-even queue delay ≈ 7.5 min** (30-min effective headway at Lier, ~28.5 min realized
  run to Oslo S, 36 min free-flow drive): with expected wait, the car must lose ≥ ~7 min for
  the train to win; ≥ ~22 min for a just-missed train. Bus-probe p90-inflation mornings clear
  the expected-wait bar. "Du sparer ca. 20 min" is a *severe-morning* number, not a typical
  one.
- Reading note (R3): all rates above use the *anomaly* reading of "congested" (worse than the
  typical morning). Under the routine-rush reading, the road is "congested" every weekday
  morning by construction and the nudge rate equals the rail-normal rate (52–58%) — a
  different product (a daily mode-comparison, not an event nudge). The PDF's "i dag"-framing
  implies the anomaly reading; its authors should confirm.

---

## What was built (all live on the branch)

- **`GET /reisevalg-api/recommendation`** (`disruptions/api/reisevalg.py`, wired in
  `main.py`): five states (`TRAIN_RECOMMENDED` / `NO_ADVANTAGE` / `TRAIN_DEGRADED` /
  `OUTSIDE_WINDOW` / `UNKNOWN`), machine-readable reasons, Norwegian copy, per-source
  freshness (`observed_at` ≠ `fetched_at`), `state_since`/`valid_until`, next-departures list
  with predicted cancellation probabilities, static-only parking field. Rail gate is
  arrival-side at Oslo S over the JP departure board (`estimatedCalls` — includes cancelled
  trips; the `trip` query silently drops them) plus the kix-avvik per-call prediction snapshot
  (demote-only). Road providers behind a frozen `RoadObservation` interface: Trafikkdata
  volumes (live, gated), DATEX stub (activates when credentials exist). 35 hermetic tests.
- **Honesty machinery that actually bit:** the R5 precision gate is real code — because the
  backtest measured 0.60 < 0.8, a volume-only "congested" returns
  `UNKNOWN / VOLUME_BELOW_PRECISION_BAR` ("Mulig kø på E18, men datagrunnlaget er ikke presist
  nok til å anbefale togbytte") instead of a nudge, live, today.
- **Data assets:** `disruptions.svv_hourly_volumes` (340,504 rows), `svv_points`,
  `svv_baselines` (serving schema, `built_at`-gated), `reisevalg_rail_mornings`,
  `reisevalg_road_mornings`, `reisevalg_backtest`, `reisevalg_bus_*` (probe tables);
  `data/svv/download_volumes.py` (re-runnable backfill), `data/queries/reisevalg_*.sql`
  (re-runnable analysis).

### Captured endpoint responses

Real endpoint, real decision code; road/rail inputs injected for the in-window examples (the
investigation ran at night — live in-window captures require a normal-traffic week, and the
build window falls in fellesferie/summer-works season; conclusions about live behavior need at
least one post-holiday week of observation).

```text
(real, live, 01:18 Friday)   OUTSIDE_WINDOW / OUTSIDE_MORNING_WINDOW
  "Utenfor morgenrushet — anbefalingen gjelder hverdager 05:30–09:30."

(volume congested, rail normal — the SHIPPED default)
  UNKNOWN / VOLUME_BELOW_PRECISION_BAR
  "Mulig kø på E18, men datagrunnlaget er ikke presist nok til å anbefale togbytte."

(synthetic DATEX 58-min drive, rail normal — target behavior once travel-time data exists)
  TRAIN_RECOMMENDED / ROAD_CONGESTED_RAIL_OK, saved_min: 19
  "Bedre å ta toget i dag — kø på E18, togene går som normalt."

(next departure cancelled)
  TRAIN_DEGRADED / RAIL_CANCELLATIONS
  "Ikke regn med toget nå — innstillinger på strekningen mot Oslo S."
```

---

## Named constants (backtest evidence → live literals)

| Constant (reisevalg.py) | Value | Evidence |
|---|---|---|
| `CONGESTION_VOLUME_DEFICIT` | 0.15 | best of 8 variants: precision 0.60, 10 flagged / 6 confirmed (d25 scored 0.40) |
| `CONGESTION_POINT_FRAC` | 0.5 | winning d15 variant definition |
| `PERSISTENCE_N` | 2 | winning variant; N=1 adds demand-dip noise |
| `VOLUME_PRECISION_BAR` / `VOLUME_PRECISION_MEASURED` | 0.8 / **0.60** | gate closed → volume never nudges (`VOLUME_NUDGE_ENABLED = False`) |
| `VOLUME_MAX_AGE_MIN` | 250 | measured publication lag 134–194 min (night); keeps observations visible, gate keeps them from nudging |
| `RAIL_NORMAL_MAX_ARR_DELAY_MIN` | 5 | arrival-side predicate at Oslo S; same family as the backtest classification |
| `MIN_SAVED_MIN` | 10 | break-even ≈ 7.5 min at expected wait; 10 keeps the claim above noise |
| `MORNING_WINDOW` | Mon–Fri 05:30–09:30 | PDF use case (backtest used 06:00–09:30 — residual mismatch noted) |
| `BASELINES_MAX_AGE_DAYS` | 180 | drift check found ≤4.1% y/y (no roadwork drift), but the re-derivation loop is enforced, not remembered |

Definition-parity residual: the backtest classifies whole mornings from `rail_calls` actuals;
the live gate judges the next 3 departures from JP expected times + predictions — same
predicate family (arrival at Oslo S, ≤5 min, cancellations), different granularity and
instrument. Backtest window 06:00–09:30 vs live 05:30–09:30.

## API access map (verified 2026-07-10/11)

| Source | Access | Verdict |
|---|---|---|
| SVV Trafikkdata GraphQL | open, no key, User-Agent | hourly volumes only; 2y history; publication lag 134–194 min |
| SVV DATEX II GetTravelTimeData / GetSituation | registration (org, fixed IP), free, NLOD | 5-min travel times, E18 covered; **no archive** — request prepared (`datex-access-request.md`); fixed-IP question vs GKE egress NAT unresolved |
| Entur JP v3 (`ET-Client-Name`) | open | departure board incl. cancellations + `datedServiceJourney` refs (verified live); car routing Lier→Oslo S 36 min/40.9 km |
| kix-avvik per-call predictions | in-process | joins JP refs exactly (`VYG:DatedServiceJourney:…`) |
| SIRI-ET `last_recorded` (bus probe) | in-house BQ | RUT 250/160 corridor probes work; Brakar/NOR lack stop names (negative finding — but `stopPointRef` is quay-level for ALL operators in `last_recorded`, so an NSR `quays_last_version` join could still rescue them; the finding is about name-addressability in the U2 pass, not a proven dead end) |
| Park-and-ride occupancy | **not open** | Bane NOR app backend only; NSR/NeTEx has static capacity |

## Cost ledger

BigQuery: **$0.29** (46.4 GB billed, dominated by 24 monthly `last_recorded` extractions;
every query dry-run ≤ 1.6 GB). SVV/JP API calls: free (~1,710 polite requests). Total ≪ the
$100 cap; the $10-per-operation ask-first bar was never approached.

## Next steps (in value order)

1. **Submit the prepared DATEX request** (day-1 artifact exists; an Entur-side action) and
   start archiving travel times the day access lands — the archive is unbuildable
   retroactively. Wire credentials via Secret Manager + ExternalSecret (the VAPID pattern),
   never plaintext Helm values.
2. **Build the bus-probe live provider** (RUT 250/160 realized run times via SIRI-ET) behind
   the existing `RoadObservation` interface, and backtest it against the same ground truth —
   it may make DATEX optional. Constraint: the live provider must read from an anshar/SIRI-ET
   real-time scan (the `rail_calls_planned_today.sql` pattern) — `last_recorded` is a daily
   batch table, training-only (established repo learning); it cannot feed a live signal.
3. Observe the live endpoint through at least one normal-traffic week (post-fellesferie,
   post-summer-works) before drawing live-behavior conclusions; verify cluster egress to
   `trafikkdata-api.atlas.vegvesen.no` on the first dev deploy.
4. prd stays road-side-`UNKNOWN` by design: `svv_baselines` is deliberately **not** in
   `copy_runtime_to_prd.sh` until the verdict on a validated road provider is positive.
5. If the PDF's authors intend the *routine-rush* reading (daily mode comparison rather than
   event nudge), that is a different — and much more often applicable — product; the same
   endpoint can serve it with a config change. Worth an explicit product decision.
