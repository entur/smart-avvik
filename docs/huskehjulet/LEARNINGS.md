# Huskehjulet (E05) — does disruption memory exist in real data?

_Team KI-eksperimentering · avvikslæring over tid · 2026-06_

## Goal

E05 «Avvikslæring over tid (Huskehjulet)» (docs/kix-docs/ki-eksperimenter/
prototype-kandidater.md) pitches pattern memory for driftssentraler: «Signalfeil
ved Skøyen for fjerde gang på seks måneder … Sist gang tok det 3 timer.» The spec
plans a scrollytelling demo on **constructed** data («Hendelsene er konstruert.
Mønsterdeteksjon er konseptuelt»).

This experiment tested whether the premise holds in the real SIRI-SX archive,
using the assets built by the duration work (`disruptions_initial`, ~7.9k acute
NSB stoppages over 2 years with onset, category, affected stops, and measured
reopen/impact durations). Three hypotheses:

1. **Recurrence is real** — (stop place, category) pairs with repeated incidents exist.
2. **Memory predicts** — «median of earlier similar incidents» is competitive with
   the trained BQML duration models, while being explainable.
3. **Weather couples** — incident types cluster in weather bands (the storyboard's
   sporveksel-kondens claim).

## What we tried

1. **Recurrence census** (`huskehjul_census.sql`): explode `affected_stops`, join
   `categories` + NSR names, count incidents per (stop place, category).
2. **Precedent backtest** (`huskehjul_backtest.sql`): leave-one-out, time-ordered —
   predict each incident's reopen/impact as the median of strictly *earlier*
   incidents sharing a stop and category; score on the models' held-out eval rows
   against `dur_init_reopen`/`dur_init_impact`, category median, and global median.
3. **Weather coupling** (`huskehjul_weather.sql`): GHCN-D daily weather (Oslo
   Blindern) × localized Oslo-area incidents; incident rate ratios per temperature
   band, precipitation, and snow cover, per category.

## What did NOT work, and why

- **Raw recurrence counts are smear.** A line-wide situation is counted at every
  station it touches, so Oslo-area hubs showed 500–2,300 «incidents» each, all
  generic («Toget står», «Delvis stengt»), and not one cluster was
  majority-localized. Everything below filters to **localized incidents
  (≤ 3 affected stops)** — that single filter is what makes the census meaningful.
- **Place does not improve point accuracy.** The (stop + category) precedent never
  beat the category-only median, not even on the infrastructure clusters where
  place was expected to matter (reopen MAE 125.0 vs 127.8, n=87). Consistent with
  the duration work's finding that geography earns little.
- **The storyboard's two flagship details are unsupported.** Signalfeil shows *no*
  weather coupling (RR 0.8–1.6, no pattern), and sporvekselfeil skews **dry**
  (wet-day RR 0.78) — the «fuktig luft»-fellesnevner is not visible at daily
  granularity. Deep cold + snow is the real signature (see below).
- **Weather does not stretch durations.** Sporveksel medians are slightly *shorter*
  in deep cold (59 min at < −5 °C vs 84–86 mild). The weather effect is on
  frequency, not duration.

## What worked, and how well

- **H1 confirmed: recurrence clusters are real.** 60 localized clusters with ≥ 4
  incidents, in two families:
  - *materiell/teknisk* («Toget står … teknisk feil med toget»): frequent
    everywhere, fast and predictable — median reopen 7–11 min regardless of place
    (Oslo S 173×, Ski 118×, Lillestrøm 87×).
  - *infrastruktur/{sporveksel, signal, spor}* at specific hubs — the huskehjul
    gold: sporveksel at Lillestrøm/Kløfta **29×** (median reopen 48 min, p80 185),
    Oslo S **25×** (82 / 179), Drammen/Gulskogen **21×** (76 / 212), Ski **16×**;
    signal at Oslo S/Gardermoen–Kløfta **51×**. Long, heavy-tailed, place-bound.
- **H2 confirmed (the useful half): precedent retrieval ties the ML models.** On
  321 identical held-out rows (localized, ≥ 3 cluster priors):

  | target | model | precedent (place+cat) | category median | global median |
  |---|---|---|---|---|
  | reopen MAE | **48.4** | 50.3 | 50.3 | 54.2 |
  | impact MAE | **68.9** | 74.6 | 71.9 | 76.8 |

  Differences of 2–5 min at n≈100–300 are noise. «Sist gang tok det X min» loses
  nothing measurable against the boosted tree — and it can show its work. Since no
  method beats simple medians by much, retrieval + interval is arguably the right
  product shape, not a consolation prize.
- **H3 confirmed, with a corrected story.** 712 days Oslo Blindern × Oslo-area
  incidents, rate ratio vs the category's own base rate:

  | category | tmin < −5 °C | snow on ground | wet days |
  |---|---|---|---|
  | infrastruktur/sporveksel | **2.51** | 1.64 | 0.78 |
  | materiell/teknisk | 1.70 | 2.20 | 0.93 |
  | drift/personale | 1.57 | 2.67 | 1.09 |
  | infrastruktur/signal | 0.91 | 1.32 | 0.89 |

  Switch failures are 2.5× as frequent in deep cold — read against materiell's
  1.70 generic winter effect, the cold-specificity is real. The right huskehjul
  message is **preparedness, not duration**: «−8 °C i morgen → forvent ~2.5×
  flere sporvekselfeil», not «denne feilen vil vare lenger».

## Implications for the E05 demo

- **Drop the constructed data.** The real archive is strictly more compelling:
  21 actual sporvekselfeil at Gulskogen–Drammen beats a fictional sequence, and
  directly demonstrates R13's «denne dataen strømmer i dag og forsvinner».
- **Fix the storyboard physics:** sporveksel (not signal) is the weather-coupled
  type, and the fellesnevner is deep cold + snow (not humid air).
- **The honest AI claim:** memory (retrieval over the archive) matches a trained
  model and is auditable. The demo's «mønster identifisert»-moment can show the
  actual prior incidents with their real durations.

## Data-quality / method caveats

- Population is the **7.8 % reopening slice** (acute unplanned NSB stoppages) —
  the right slice for a driftssentral demo, but recurrence counts understate
  reality since most situations never emit a reopening message.
- `category` is 20 coarse AI.CLASSIFY classes; within-cluster matching on summary
  text similarity was not tried.
- Weather is **daily, one regional station** (Blindern for a 40 km radius);
  GHCN-D has **no humidity element**, so the RH/kondens claim is untested, not
  refuted. Incidents (not incident-days) are counted, so cascade days inflate
  rate ratios; season confounds anything winter-correlated.

### Technical gotchas (cost retries)

- `bq query "$(...)"`: the query string must not START with an SQL comment —
  bq's flag parser eats a leading `--`.
- `IS NOT DISTINCT FROM` in join predicates blew BigQuery's on-demand CPU/bytes
  ratio (21k CPU-s on 50 MB scanned); `IFNULL()`-equality joins run in seconds.
  Expanding per-row medians via time self-join did the same — train-split
  constants are the cheap, established baseline anyway.
- `bigquery-public-data.ghcn_d` is **US region**; project + NSR tables are EU —
  unjoinable in one query. Fix: extract the small weather pivot in a US query,
  `bq load` into an EU table (`disruptions.hh_weather_blindern`).

## Artifacts

SQL (`data/queries/`, results documented in each header):
`huskehjul_census.sql`, `huskehjul_backtest.sql`, `huskehjul_weather.sql`.

Tables (`ent-kixavvik-dev.disruptions`): `hh_backtest_base` (localized situations
with leave-one-out place precedents), `hh_weather_blindern` (Blindern daily
weather 2024-06 → 2026-05, from GHCN-D).

## Recommended next steps

- **MET Frost (frost.met.no)** for hourly obs incl. RH: test the kondens claim
  properly and match weather to the incident *hour*; also enables a
  forecast-driven «sporveksel-beredskap i morgen»-signal.
- **Per-cluster prediction intervals** («de siste 5 gangene: 48–185 min») instead
  of point estimates — combine with the calibrated quantile machinery from the
  duration work.
- **Build the E05 demo on the real clusters** (Gulskogen–Drammen or
  Lillestrøm/Kløfta sporveksel as the running example), with the corrected
  weather narrative.
- Count **incident-days** and/or add a season control to firm up the rate ratios;
  try summary-text similarity for within-cluster precedent selection.
