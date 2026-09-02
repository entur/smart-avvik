# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`kix-avvik` is an experiment from Entur's Team KI-eksperimentering aimed at proactive disruption handling (Norwegian: "proaktiv avvikshåndtering"). It predicts the duration of public-transport disruptions from SIRI-SX situation messages using BigQuery ML.

## Tooling

- Use uv for Python operations
- Use bq for bigquery operations. For instance, use bq show --schema --format=prettyjson ent-anshar-prd:realtime.siri_realtime_et_production

## Important BigQuery tables

- ent-anshar-prd:realtime.siri_realtime_sx_production: SIRI-SX (Situation Exchange), textual incident and disruption messages
- ent-anshar-prd:realtime.siri_realtime_et_production: SIRI-ET (Estimated Timetable), real-time updates to the planned timetable

## Deployment

The `/disruptions` API (`disruptions/api/main.py`) runs on **Kubernetes** (Entur GKE golden
path; hosts `kix-avvik.dev.entur.io` / `.staging.entur.io` (tst) / `.entur.io` (prd); see the
Kubernetes memory note). It is built from the multi-stage `Dockerfile` at the **repo root** — its
node stage builds the `/demo`, `/varsel`, `/historikk`, `/meldinger`, `/togmelding` and
`/rushhistorikk` front-ends (from `demo/reisedemo` + `demo/avviksvarsel` +
`demo/varselhistorikk` + `demo/meldinger` + `demo/togmelding` + `demo/rushhistorikk`, which is
why the context is the repo root, not `disruptions/api`), and its python stage assembles the API. CI builds and deploys it via `.github/workflows/{ci,cd}.yml`:

```sh
git push origin main                         # builds the image (ci.yml) and deploys to dev (cd.yml)
gh workflow run cd.yml -f environment=prd    # promote to prd (tst/prd are dispatch-only)
```

- **Single always-on instance** (`deployment.forceReplicas: 1` in the Helm chart): the app runs
  one background poller thread and an in-process cache, so more replicas mean duplicate pollers
  and unshared caches.
- **What the poller cycle costs is now logged, not inferred** (2026-08-21). Every step of
  `main._rebuild_all` is wrapped in `main._phase`, which emits `event="poller_phase"` with `phase`
  and `ms`, and the cycle ends with one `event="poller_cycle"` carrying the total and a `phases`
  breakdown. Before this there was no instrumentation at all and "where does the cycle go?" meant
  subtracting timestamps of unrelated INFO lines that happen to sit at step boundaries — which
  reports any step that logs nothing as free. Both are logged from `finally` and neither swallows:
  a step that fails *slowly* is the most interesting reading there is, and the rebuild's whole
  isolation model is per-step `try/except`, so a context manager that ate the exception would
  quietly turn "bus predictions failed, serve NSB-only" into a success
  (`test_poller_refresh.py` pins this). Baseline measured in prd that evening, ~44 registered
  journeys: **cycle 50-63 s**, of which `predict_calls_*` ~32-37 s, `disruptions_sql` ~9 s,
  `varsel_stops` ~6 s and `varsel_notify` **1.3-1.7 s**. The cycle *period* is ~177 s (p50) and is
  set by how often the upstream SIRI-SX ingestion timestamp moves, not by this work — so there is
  ~120 s of headroom, and BigQuery owns ~97 % of what is spent. The corollary matters when sizing
  anything user-facing: **the expensive part of a cycle does not scale with registrations**, and
  what does — `varsel_notify` — is 3 % of it.
- Public, read-only service; the SA needs SIRI read access (granted via the team group).
- **Who may read the `disruptions` dataset is Terraform, not a console click** (`terraform/`, added
  2026-08-13 — the app had no Terraform before that, only the platform-provisioned state bucket
  `ent-gcs-tfa-kixavvik`). Add the group to `disruptions_readers` in `terraform/env/<env>.tfvars`;
  `.github/workflows/terraform.yml` plans on the PR and applies to dev on merge (tst/prd are
  dispatch-only, as in `cd.yml`). Grant to a **group**, never a person — reading a group's
  membership needs permissions an ordinary developer does not have, so a per-person list rots
  silently. Two traps, both spelled out in `terraform/bigquery_iam.tf`: the resource must stay
  `google_bigquery_dataset_iam_member` (the `_iam_binding`/`_iam_policy` forms are authoritative and
  would silently drop `application@ent-kixavvik-prd`, which `copy_runtime_to_prd.sh` reads through),
  and the dataset itself is **not** a Terraform resource — the app creates it at startup and CTASes
  into it, so `dataset_id` stays a literal string.
- **Front-ends are built into the image**, not committed: the Dockerfile's node stage runs each
  app's `npm ci && npm run build`, and Vite writes into
  `disruptions/api/{demo,varsel,historikk,meldinger,togmelding,rushhistorikk}_static/` (all gitignored). `demo/*/build.sh` do the same locally for dev. **`/eval` is the exception**:
  its marimo snapshots run BigQuery under a developer's ADC, so they can't be built in the
  image/CI — they are rendered offline by `disruptions/api/export_eval.sh` and **committed** under
  `disruptions/api/eval_static/`, shipped as-is. Refresh them by running that script, then commit
  and push.
  **A deploy only reaches a browser that re-asks for `index.html`** (2026-08-13): Starlette's
  `StaticFiles` sends `ETag`/`Last-Modified` and no `Cache-Control`, and a response with neither
  `Cache-Control` nor `Expires` is *heuristically* cacheable — the browser invents a lifetime and
  may not revalidate at all. Harmless for a hashed asset, fatal for `index.html`, which keeps its
  name across every deploy while the bundle its `<script src>` names does not. Measured on prd
  2026-08-11, minutes after a deploy: the server served `index-BA3KKIi9.js` while a 40-minute-old
  `index.html` still ran `index-N5X3j20f.js` — the pre-fix bundle, carrying the bug that deploy had
  just fixed. It reads exactly like a failed deploy, so it costs an hour in CI and Helm before
  anyone thinks to check what the browser actually loaded; **when a fix looks absent from a healthy
  deployment, compare the `<script src>` against what the server serves before debugging anything
  else.** All four mounts therefore use `main._VersionedStaticFiles`, never `StaticFiles`:
  `assets/` (Vite writes only content-hashed names there) is `immutable` for a year, everything
  else — `index.html`, the PWA's manifest/service worker/icons, `/eval`'s notebooks — is
  `no-cache`, which means "ask first", not "do not store". Two things this got wrong first and
  `test_static_cache.py` caught: the header must be set on the response `file_response` **returns**
  (it decides "not modified" internally, and a 304 without `Cache-Control` re-enters heuristic
  caching on the very request meant to prevent it), and the mount root must be **resolved** —
  Starlette passes a realpath while `directory` stays as given, so comparing them unresolved
  matched nothing and sent every asset down the `no-cache` branch. That second one does not
  reproduce in the container, where `/app` has no symlinks. Note the fix cannot repair caches that
  already exist: it protects the *next* deploy, not the last one.
- The partner demo UI at `/demo` (predictions vs realized outcomes for a chosen trip) lives in
  `demo/reisedemo/` (React + Vite). Its data is served by the `/demo-api` endpoints
  from static tables built by `data/queries/create_demo_predictions.sql` — re-run that query
  after model/calibration retrains. `data/queries/refresh_demo_recent.sh` extends the tables
  with the last ~25 days straight from SIRI-ET/SX (run before a partner demo; ~$2).
  Non-prod deployments label the masthead and tab title (`Reiseprediksjon under avvik (dev)`) from
  `GET /demo-api/config` — a partner shown dev numbers as if they were prd is the failure it
  prevents. All three front-ends do this from the same `deployment.environment_label()`, each
  through its own `*-api/config` endpoint, kept off the data endpoints because those can fail while
  the page shell still renders. `disruptions/api/test_demo_api.py` covers the `/demo` one.
- The **Avviksvarsel** PWA at `/varsel` (register a journey → Web Push when a live disruption is
  predicted to hit it; rail + the trained bus operators, per-operator alert thresholds) lives in
  `demo/avviksvarsel/` (React + Vite + Entur Linje). Its
  API is `/varsel-api` (see `disruptions/api/varsel.py`), which runs in the poller's refresh cycle.
  **It does not match on its own — it calls `/impact`** through an injected in-process provider
  (`main._varsel_impact`, structurally typed as `varsel.ImpactProvider`), so there is one matcher and
  one severity order behind both surfaces; varsel keeps only its own policy on top (drop trains that
  can no longer be caught, plus the per-operator alert gate). The matcher itself lives in
  `disruptions/api/impact.py` — extracted from `main.py` so `varsel` can *type* that contract rather
  than hold it as `Callable[..., Any]`; it broke silently once and took all push alerting with it
  while the test suite stayed green. `main` owns the prediction cache and injects a reader via
  `impact.configure()`. Note `main.py` imports it as `impact_engine`: the `/impact` route function is
  itself named `impact`, and a bare `impact.` inside that function would resolve to the route.
  Work is shared per journey *definition* (`varsel.journey_key`): two
  devices watching the same trip drive one `/impact` call and one written message and see identical
  text, while push dedup stays keyed on `journey_id` so each device is still notified once.
  **`to_stop` narrows `/impact`**: only trains that actually reach it are returned, so a caller
  passing it sees fewer situations than one that does not.
  **One picker row per physical place** (2026-08-03): `/varsel-api/stops` collapses stop refs that
  the National Stop Register groups under the same `parentRef` *and* that share a name — a station's
  rail and bus sides, a ferry berth registered twice. Before that the picker offered two identical
  rows ("Nyvoll ferjekai" 57120 / 57070) and picking the wrong one made `to_stop` narrowing drop
  every situation, so the journey card said "ingen aktive avvik akkurat nå" forever. Three parts,
  and all three are needed: the row keeps the busiest ref, `varsel.create_journey` **canonicalizes**
  submitted refs (a stale client can still name a collapsed one), and `match_journey` asks `/impact`
  about **every** ref of the place (`varsel.place_refs`) because each feed records its calls on its
  own ref — matching only the canonical one would just move the silent miss down a layer. Names that
  belong to several *different* places (~1 300 of them: "Sentrum", "Kirken") get a county suffix from
  `topographicPlaceRef`, `KVE:` codespace only. The registry read fails open: no registry means no
  grouping and no counties, exactly as before it existed. It is readable from both environments (the
  dataset grants `allAuthenticatedUsers` READER) and costs ~3 MB per refresh.
  **A registration nothing can ever alert on says so** (2026-08-04): `POST /varsel-api/journeys`
  asks whether any planned trip today runs between the two places (`main._varsel_connectivity` over
  `{rail,bus}_calls_planned_today`, every ref of both places, MIN/MAX per side so a circular route is
  handled). varsel only alerts on a **direct** trip, so a pair no single trip connects yields no alert
  however bad the day gets — and the old symptom was the reassuring "ingen aktive avvik akkurat nå".
  It **never blocks**: the verdict rides on `Journey.warning`, shown on the card in place of that
  line, with a sharper text naming both stops when the pair is served *the other way* (a likely
  swap). Weekend-only and seasonal routes are why a refusal would be wrong, and why the poller
  re-asks every 6 h (`varsel.recheck_service`) and clears the caveat when service appears; a match
  also suppresses it. `None` from the provider means "could not establish" — an outage, a timeout, the
  30/min rate limit that exists because the endpoint is unauthenticated — and never becomes a
  warning. Answers are cached 10 min.
  **Calling at a stop is not the same as being usable from it** (2026-09-01): a traveller wrote in
  after an Oslo S → Drammen registration was pushed «Tog F4 16:23 er innstilt» — the 16:23 is the
  Bergen train, which calls Drammen with **alighting closed**. Vy sells the local stretch of every
  long-distance run as boarding-only (`arrivalBoardingActivity = NO_ALIGHTING` at Sandvika, Asker,
  Drammen and Hokksund; alighting opens at Hønefoss), and the matcher knew only stop_order, so it
  offered that train to five registered journeys (Oslo S → Drammen ×3, → Asker, → Sandvika) and
  pushed the one cancellation to all eight of their devices — not one of whom could have been
  aboard. The two SIRI activities now ride from the planned sources
  (`boarding_allowed` / `alighting_allowed` in `{rail,bus}_calls_planned_today.sql`) through
  `predict_calls*.sql` onto `impact.CallPred`, and `impact._trip_cells` requires both: board at
  `from_stop`, alight at `to_stop`. Not fjerntog-only — measured the same day on Vy alone, 56
  mid-journey NO_ALIGHTING calls (F4, RX11) and 417 mid-journey NO_BOARDING ones (F4, R31, RE30),
  and on the served bus operators 0–6.3 % of mid-journey calls carry one. **The reading is fail-open
  and lives in ONE place per source table**: most operators leave both fields NULL on most calls
  (RUT, KOL, TRO, SKY, OST), so "not stated" must mean permission — read as prohibition it would
  silently retire whole operators — and `PASS_THRU` bars both sides. Every default is
  permissive for the same reason, including `CallPred`'s and `_call_pred_from_row`'s `getattr`, which
  is what carries the app through the cycle after a deploy where the serving table predates the
  columns. `main._VARSEL_CONNECTIVITY_QUERY` asks in the same shape — **four CTEs, not two**, because
  the reverse direction boards at the *to*-place and alights at the *from*-place — so the caveat above
  and the alerting cannot disagree; measured against dev's timetable it drops the Oslo S → Drammen
  trip set from 173 journeys to 161, all twelve of them F4. Cost: ~0.68 GB more per planned rebuild
  on each side (+3.5 %). Nothing in `alternatives.py` needed the rule — the Journey Planner plans real
  trips and never offers a leg nobody may board.
  **What ET already knows is a fact, not a forecast** (2026-08-05): when a call carries
  `et_declared_cancelled`, `impact._cancelled_prediction` serves `cancel_probability` /
  `impact_probability` of **1.0**, `cancelled=True` and **null delay figures** (every one of them
  meant "if it runs"), and every wording states it — `Innstilt` in `varsel._deterministic_body` and
  on the card, `innstilt: true` in the message input with the probability and delay keys *removed*
  so the model cannot hedge a certainty. `_call_severity` restates the same rule (it is the matcher's
  hot loop and cannot afford to build a `StopPrediction`), so `select="worst"` lands on the train
  that is certainly gone. Two symptoms, one cause: Vy cancelled RE10 80323 Drammen–Skøyen and the
  card offered "91 % sjanse for innstilling" for a 15:03 from Asker that entur.no no longer listed —
  and the *first* cycle scored that same known-cancelled call at 0.57, under the gate, so nobody was
  told at all. `decisions.et_cancelled` marks such rows so the log can be **segmented, not
  filtered**: the app relayed a known fact instead of forecasting one, which makes the row a real
  hit for the traveller and a gimme for the models, and one precision figure cannot mean both.
  *Service* quality — was the traveller told the truth? — includes them, and should: relaying what
  ET already knows is most of the value, since nobody carries the ET feed on their phone, and in the
  reported case it was ~40 min of usable warning. *Model* skill — did we see it coming? — excludes
  them. Distrust either quoted alone: the **mix** of the two populations moves the headline with
  nothing about the app having changed. `/historikk` shows both (see its section below). Also worth watching
  `et_cancelled AND verdict='false_alarm'` — a *reinstated* train now gets a flat "Innstilt" where
  the old wording hedged at 87 %, so a confidently wrong message replaces a soft one; the labelling
  job scores it correctly, but if reinstatements turn out to be common the certainty should go back
  to hedging for departures still some way off. Note `/journey` still serves the model's probability
  for a known-cancelled call — same defect, separate contract, not yet fixed.
  **A cancellation ET has declared needs no SX message** (2026-08-19): `predict_calls.sql` and
  `predict_calls_bus.sql` now UNION in one row per declared-cancelled call that **no situation
  named**, consulting no model — `impact._cancelled_prediction` was going to replace every score
  with the certainty anyway. Until then the whole stack was gated on an operator having published
  SX about the train: measured in dev on 2026-08-19, 90 declared-cancelled rail calls, 70 named by
  a situation and **20 not**, and on the bus side 3 602 uncovered calls across 706 journeys. The
  train was gone, our own cache could see it, and the card said "ingen aktive avvik akkurat nå".
  Three things the shape gets right that are each easy to get backwards. The anti-join is **per
  call, not per journey**: a situation reaches 12 h from ITS onset, so one journey can have some
  calls named and others not, and a duplicate row would put two boxes about one train on the card
  (`varsel.one_alert_per_train` would collapse them, but leaning on it would be wrong — it
  deliberately does not reach the decision log, where every situation keeps its row). The synthetic
  `situationNumber` is **one per journey** (`KIX:ETCancellation:<jkey>`, plus operator and call_date
  on the bus side, where jkeys are date-scoped by neither), because that is the grain /impact
  renders a card at, `impact._alternatives_by_situation` spends a lookup at, varsel dedups pushes at
  and `varsel_decisions` CLUSTERs at — and it is stable across cycles, which is what keeps the
  traveller to one push per cancelled train. And the row carries only **1.0 and NULL**, never a
  model number: every delay figure meant "if it runs". The `KIX:` prefix keeps it clear of the
  publisher namespaces (`ENT:`, `NSB:`), at the cost that `GET /situation/{id}` has nothing to
  render for one — the honest answer, since there is no SX message behind it.
  **What bounds the relay is `_PLANNED_TTL`, not the poller cadence.** It reads
  `rail_calls_planned_today` / `bus_calls_planned_today`, each rebuilt at most once per **60 min**
  (rail) / **120 min** (bus) because each is an ~8 GB / ~12 GB ET partition scan, so a cancellation
  declared just after a rebuild waits that long to reach anybody. Tightening the rail TTL is the one
  lever and it costs linearly — ~190 GB/day per environment at 60 min, ~$1.2, doubling at 30 min.
  Expect the `relayed` segment on `/historikk` to grow sharply against `predicted`: that is the
  point of the change, and exactly why that headline is segmented rather than quoted blended.
  **An observation is in the past** (2026-08-05): a declared cancellation is routinely published for
  calls the train has not reached — announcing it ahead of time is the point — so `impact._is_observed`
  admits one only once its own time has passed, while a `recorded` call is an observation by
  construction. Without that cut `_latest_observation`, which takes the *furthest-along* observed
  call, chose the most distant announcement and the card read "Sist observert: Lysaker stasjon,
  innstilt (kl 15:15)" at 14:28 — then the model repeated it, the prompt having told it the timestamp
  is in the past. `_impact_response` takes `now` once and passes it down, so `as_of` and the cut
  cannot straddle two instants.
  **One card and one push per train, not per SX message** (2026-08-05): operators publish several
  situations about one disruption — Vy sent both "Ta neste tog" and "Innstilt Drammen–Skøyen" for
  the same cancelled 15:03 — and each scores the same call independently, so the card showed two
  boxes quoting **91 % and 87 % for one train**. `varsel.one_alert_per_train` collapses on the
  boarding call (`(operator, jkey, stop_place, stop_order)`, falling back to `(operator, line,
  departure_time)`; an alert identifying no train is never merged) and is applied in
  `_journey_model` and in `check_and_notify`'s delivery loop — **not** in `match_journey`, which
  also feeds the decision log, where every situation must keep its row. The survivor is the most
  severe by `_alert_rank`, which is `match_journey`'s own order: severity, then `situation_number`.
  That tie-break is load-bearing now rather than cosmetic — a declared cancellation scores exactly
  (1.0, 0, 1.0), so all its situations tie, and an unstable winner would flip both the card's
  wording and which situation the written message is cached under on every 60 s refresh.
  **The advice is about the traveller's trip, not the SX message's geography** (2026-08-05): asked
  about an Asker→Lillestrøm registration, the model answered "Du bør ta neste tog mellom Drammen og
  Skøyen" — the cancelled *segment*, straight out of the situation text. A prompt rule now scopes
  the advice to `reise.fra`/`reise.til` and says place names inside `situasjoner` are where on the
  line the disruption applies, not the traveller's route.
  **The 2026-09-01 design review landed on 2026-09-02** (`docs/design-review/2026-09-01/`, 48
  findings; the answers to its two open questions were: 47 — on blur with typed text and no pick,
  adopt the first suggestion; 48 — an evening push about tomorrow's train is by design and must
  say «i morgen»). What it changed that a later reader has to know: the **push title is the fact
  about the train** («Tog L1 07:18 er innstilt», `varsel._notification_title`) and the **body
  leads with the route** on its own line, then the wording — the wording itself is unchanged, so
  `/historikk`'s `message` and every wording comparison are untouched, but anything reading a push
  body byte-for-byte now strips the first line (`_wording()` in the test suites). Pushes carry a
  **`tag` per train per registration** (`_notification_tag`, keyed like `one_alert_per_train`),
  so a declared cancellation REPLACES the earlier forecast about the same train on the lock screen
  and is titled «Bekreftet:»; `_pushed_trains` is the in-memory record behind that prefix (a
  restart loses only the prefix). **At most `_MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY` (4) alert
  pushes per registration per Oslo day**, spent soonest-departure-first; a replacement is exempt,
  a capped alert is still recorded (`pushed=False`) and re-offered next cycle, and the suppression
  is logged once per cycle as `event="push_capped"`. The message input gained `i_morgen` (exactly
  tomorrow, Oslo calendar) and `alternativ_reise.minutter_senere` (from the new
  `AlternativeJourney.minutes_after_original`, anchored on the PLANNED arrival like
  `arrives_before_original`), both writers say them, and the prompt asks for the card's delay
  register («typisk 3–9 min forsinket»). The card now renders `alternative_journey` as its own
  box, hides the detail line beside a written message (mirrored in the feedback snapshot's
  `detail_text`), falls back to «Vurdert kl HH:MM» when nothing has been observed, and defers a
  delete behind an 8 s undo. The privacy texts moved to `CONSENT_TEXT_VERSION = 2026-09-02.1`
  (T1 is one line, T5 is in the reader's order with links and a dated «Sist oppdatert»); the level
  vocabulary is `demo/*/src/levels.ts`, a guarded duplicate like `personvern.ts`
  (`test_the_level_vocabulary_is_one_copy_per_app_and_the_copies_agree`), yellow now reads «moderat
  fare for forstyrrelser». Rushvarsel's `known_deviations` are aggregated per (planned, class,
  summary) with `n_situations`, its `skill_note` quotes rates rather than multipliers, and the
  card heading is «Morgenrushet 07:00–09:00 — gult nivå» (mirrored in `detail_text`). The picker's
  `region` is now the **municipality** from NSR's `topographic_places_all_versions` (latest version
  per id, joined on `topographicPlaceRef.ref`; 99.6 % of stop places resolve, the county prefix is
  the fallback for the rest), so «Sandvika (Troms)» ×6 reads Dyrøy, Harstad, Tromsø ….
  **`?brand=none` renders the app unbranded** (2026-08-19), for showing the concept to an audience
  that should not read it as an Entur product; no parameter — or an unrecognised one — renders
  Entur exactly as before. A brand is four things, and leaving any one out still reads as Entur:
  colour, **typeface** (Nationale is Entur's proprietary face, bundled by `@entur/typography` and
  set on `:root`), the `<Logo/>`, and the PWA/notification identity. The last one is why this is
  not a CSS-only change: the page reskins itself from the query parameter, but a NOTIFICATION is
  drawn by the OS from what the server sends, so `brand` rides on the device record
  (`varsel.DeviceIn.brand` → `_devices[id]["brand"]` → `_notification_payload`'s `icon`/`url` →
  `sw.js`). Records written before this have no `brand` key and mean `entur`, which is what
  `normalize_brand` exists for — an unknown slug indexed straight into `_BRAND_ASSETS` would
  KeyError on the poller thread and take the whole cycle's alerting with it. Push **wording** is
  deliberately brand-independent: it is what `/historikk` records, and text that varied by variant
  would split every wording comparison along a second axis.
  **Unbranded is now about the four brand carriers, NOT about never saying "Entur"** (2026-08-31).
  The privacy texts made the old invariant false and it had to give: GDPR art. 13(1)(a) obliges the
  page to name its **controller**, so the unbranded variant states «Entur AS» in the privacy line
  under the intro (T2), in the sender footer (T7) and throughout `/varsel/personvern/`, where the
  whole point of the page is saying who holds the data. Naming a company in body copy is not
  branding it: what still holds unchanged is every one of the four carriers — colour, typeface,
  the `<Logo/>`, and the PWA/notification identity — so `?brand=none` still does not look, read or
  install as an Entur product, which is what the variant was for. There is no way to have both:
  an unattributed privacy notice is not a lesser brand, it is an unlawful one. The privacy page
  itself renders unbranded via a **second Vite entry**, and two duplications keep it that way (Vite
  shares nothing between entries) — `personvern/index.html`'s inline brand script and
  `personvern-page.tsx`'s `@entur/*` import block, both byte-for-byte parity-guarded in
  `disruptions/api/test_personvern_texts.py`. The import block is the one that bites: diverge it
  and Rollup moves a Linje sheet after `brands/none.css`, un-reskinning **the whole app**, with
  nothing red anywhere.
  **The colour sheet is generated, and has to be** (`demo/avviksvarsel/scripts/make_brand_css.py`
  → the committed `src/brands/none.generated.css`): Linje's components do **not** read the design
  tokens at runtime — each package's `dist/styles.css` defines its own `--components-<pkg>-*` with
  literal hex (~3 100 across the packages `/varsel` imports) and reads those, plus ~120
  declarations that skip variables entirely, 31 of them the focus ring — so overriding
  `--lavender-90` reskins nothing. Colours are moved in **OKLCh** at constant perceptual lightness,
  which is what keeps a reskin from quietly breaking the contrast Entur tuned the ramps for. Two
  traps, both of which shipped and were caught in the browser rather than by a test, and both
  written up in the script: non-root rules must be scoped inside **`:where()`** (zero specificity)
  or the uniform +1 class flattens Linje's own hierarchy — `.eds-h1{color}` then beat
  `.eds-contrast .eds-h1{color:#fff}` and painted the masthead heading the same near-black as the
  band behind it; and Linje writes **CSS fallback pairs** (`color:#181c56;
  color:var(--primary-text-color)`), so emitting only the changed half leaves the legacy literal
  last and resolves to a value no browser was ever meant to use. And one class of Entur colour the
  generator **cannot** see at all, because it is not in a stylesheet: `@entur/icons` bakes the
  transport marks' colour into the component MARKUP (`<svg class="eds-icon__train"
  color="#00367f"><path fill="#00367F">`), so they leaked through into the stop picker, where every
  row carries one — neutralised in the hand-written `src/brands/none.css` via `svg.eds-icon[color]`,
  which matches exactly the icons that hardcode a brand colour. If a component ever looks
  un-reskinned, check its markup before assuming the generator missed a rule. Re-run the script
  after upgrading Linje — `--check` fails on a stale sheet. Adding an operator brand is an entry in `src/brand.ts`
  plus a `Brand` in that script; note `none` is a *removal* needing no assets, while `vy`/`goa`
  would need their artwork and permission to serve it, and that token overrides carry colour and
  logo but not design language.
  Install steps for phones are in `demo/avviksvarsel/README.md`. Env vars beyond the base deploy:
  - `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` (raw base64url) + `VAPID_SUBJECT` (`mailto:`) — the
    Web Push keypair. When unset, push is disabled gracefully (`/varsel-api/config` reports
    `push_enabled=false`); the journey list still works. Regenerate with
    `uv run --with pywebpush python disruptions/api/gen_vapid.py`.
  - `VARSEL_STATE_BUCKET` — a GCS bucket holding `varsel_state.json` (devices/journeys/notified)
    so registrations survive redeploys (the container fs is ephemeral). The runtime SA needs
    `roles/storage.objectAdmin` on it. Without the bucket, state is local-only (lost on redeploy).
  - Non-prod deployments label themselves in the PWA header and tab title —
    `Avviksvarsel (dev)` — and in the **push title** (`[dev] Oslo S → Lillehammer`, prefixed
    because phones truncate on the right), because dev and prd installs are otherwise identical on
    a phone and in the notification tray (the manifest name and icons are baked into the one image
    every environment shares). The tag goes in the title only, never the body, so the wording
    `/historikk` records is the same text in every environment.
    `/varsel-api/config` serves the name. The one derivation lives in
    `disruptions/api/deployment.py` (`environment_label()`), shared with `/historikk`: it reads the
    `GOOGLE_CLOUD_PROJECT` suffix, so no cosmetic per-environment variable has to be remembered —
    that one is already mandatory everywhere, since the unqualified `disruptions.*` refs resolve
    through it. `KIX_ENVIRONMENT` overrides it locally. `prd` — and an unreadable project — stay
    unlabelled: a stray `(dev)` on the traveller-facing host is worse than none on a deployment we
    control. Note the `/varsel-api` tests pin `KIX_ENVIRONMENT=prd` at import, or a developer with
    `GOOGLE_CLOUD_PROJECT=ent-kixavvik-dev` exported fails the push-title assertions.
  - `VARSEL_LLM_ENABLED` / `VARSEL_LLM_PROVIDER` / `VERTEX_LOCATION` /
    `VERTEX_CLAUDE_MODEL` / `VERTEX_GEMINI_MODEL` — the push text is written by an LLM on Vertex AI
    (`disruptions/api/llm_message.py`) from the enriched `/impact` payload. No API keys: both
    backends use the runtime SA's ADC (`roles/aiplatform.user`, already granted for `AI.CLASSIFY`).
    Every Helm environment pins `VARSEL_LLM_PROVIDER=gemini`; `auto` remains supported by the code
    but is not deployed. Check `jsonPayload.provider` on `event="llm_message"` when diagnosing calls.
    Every failure path (unset,
    unavailable model, timeout, empty or over-long reply) falls back to the previous deterministic
    wording, so a push is never lost to an LLM problem.
    **Writing a wording and sending it are two switches** (2026-08-07): `VARSEL_LLM_PROVIDER=off`
    stops a model being *asked*; `VARSEL_LLM_ENABLED=false` still asks and records the answer but
    *sends* the deterministic wording. `llm_message.generation_enabled()` and `delivery_enabled()`
    are the two gates — the old conflated `llm_enabled()` is gone. Delivery is enforced in exactly
    one place, `varsel._deliverable_message`, which the push body and the in-app journey card both
    read through; the card is the one that would otherwise silently start showing prose the push did
    not carry. `_messages` has two other readers (`check_and_notify`'s cache probe,
    `_record_decisions`' fill of `message_llm`) that stay ungated on purpose: the log records what
    was *written*, whether or not it was sent. **prd ran the two switches apart from 2026-08-07 to
    2026-08-23, and now runs both on** — `VARSEL_LLM_PROVIDER=gemini` with
    `VARSEL_LLM_ENABLED=true`, so a real traveller's push carries the model's prose. The apart
    period is what the decision rests on: it is the only way prd's real journeys were ever
    comparable, and it produced the "written but withheld" pairs
    (`message_source='deterministic' AND message_llm IS NOT NULL`) the rollout was judged on —
    measured 2026-08-23 over the trailing 30 days in prd, 590 same-input pairs (234 `predicted`,
    338 `relayed`, 18 `no_sx`), none delivered. **That population now stops growing in prd**: from
    this deploy its rows carry `message_source='llm'`, so the side-by-side
    (`data/queries/message_wording_comparison.sql`) is a historic window there plus whatever dev
    keeps writing, and what replaces it is `/historikk`'s verdicts over wordings travellers actually
    received. Reverting is one value, and *which* value depends on what is wrong: bad **text** →
    `VARSEL_LLM_ENABLED=false`, which returns travellers to the template while the model keeps
    writing and recording, so the next attempt has evidence; **egress** must stop →
    `VARSEL_LLM_PROVIDER=off`, which is the kill switch and the other one is not. Know what it
    sends — and note delivery does not change that: the prompt carries the traveller's origin,
    destination and departure time to Gemini's EU multi-region endpoint, which is *not* what the
    `AI.CLASSIFY` precedent covers (operator-authored SX text); decided with that in view on
    2026-08-07, when *generation* was enabled. Prompts and replies are logged under
    `jsonPayload.event="llm_message"`.
    **There are three location shapes, not two** (2026-08-07): `global`, a single region, and the
    **EU multi-region** endpoint (`aiplatform.eu.rep.googleapis.com`, `location="eu"`) — and only the
    third both serves the model and confines processing, which is why `VERTEX_GEMINI_LOCATION` is
    **`eu` in every environment and in the code default**. Vertex documents multi-region endpoints as
    keeping ML processing "within a specific jurisdictional boundary" and `global` as the opposite:
    "you can't control or know which region your ML processing requests are sent to". It also anchors
    Google's abuse-monitoring retention, which holds a logged prompt "in the same region or
    multi-region selected by the customer" — at `global`, that clause binds to nothing. A single
    region is **not** the fallback it looks like: measured 2026-08-07 in both projects,
    `gemini-3.5-flash` 404s at `europe-west1`, `europe-north1` and `europe-west4` alike (only
    `gemini-2.5-flash*` serve there), so pinning a region costs a model generation while `eu` costs
    nothing — measured from inside the dev cluster, median 1.51 s vs 1.38 s at `global`. Private
    Google Access is **not** in that path, which had to be checked in-cluster rather than from a
    laptop because the Vertex docs warn PGA breaks multi-region endpoints. `VERTEX_LOCATION` still
    defaults to `global` and still governs Claude, which is not served in these projects at all; an
    explicit `VERTEX_LOCATION` moves both backends, so only the final fallback is per-model. What
    this does **not** settle: whether a commute pattern is "Fortrolig" under Entur's classification,
    for which "Google Gemini on Cloud" is approved for neither — that is a DPIA question, and
    `VARSEL_LLM_PROVIDER=off` remains the egress kill switch. The backend latch expires
    after 30 min (`llm_message._LATCH_TTL`), so enabling Model Garden takes effect without a
    redeploy. The prompt is **not** logged by default (it carries the traveller's origin,
    destination and departure time); records carry a hashed `journey_digest`, and
    `VARSEL_LLM_LOG_PROMPTS=true` puts the full prompt/reply at DEBUG.
    **The Gemini default is `gemini-3.7-flash`, and moving it moves the thinking parameter with it**
    (2026-08-20). Two things a model bump here has to re-check, because neither errors. Availability
    is per model, not per family: 3.7-flash 404s at `europe-west1` and `europe-north1` exactly as
    3.5-flash did, so `eu` stays the only endpoint that serves it *and* confines processing. And the
    thinking cap is family-specific — `thinking_level` (`minimal`/`low`/`medium`/`high`) is the 3.x
    parameter, `thinking_budget` the 2.5 one, and they must not be mixed. `thinking_budget: 0`, which
    turned thinking off on 3.5-flash, is **silently ignored** by 3.7-flash: measured at `eu`, 92-385
    thinking tokens per call with it set, drawn from `max_output_tokens` and therefore from the room
    the answer needs. `thinking_level: "low"` is what the code sends now — 0 thinking tokens and
    ~2.2 s per call on a real payload, against ~3.3 s for the ignored budget, and no truncation.
    Not `"minimal"`: 3.7-flash rejects it with `400 Thinking level is unsupported`, and a 400 is
    deliberately never latched, so it would fail every call on every cycle rather than once.
  - `/impact` also returns `alternative_journey` when `to_stop` is given — the **best remaining way
    there**, not a faster one (`disruptions/api/alternatives.py`, TTL-cached, fail-open, no
    credentials). Two tiers, and the second is why "faster" is the wrong word: prefer the **latest**
    departure that still arrives before the disrupted train's *aimed* arrival (the traveller's own
    deadline) among trains not yet departed; when nothing gets there in time, fall back to the
    **earliest arrival** among those still catchable — which for a cancelled train legitimately
    lands *later* than the timetable promised, so `minutes_saved` is signed and
    `arrives_before_original` is what says whether they still make it. A candidate already declared
    cancelled is never offered; one that would itself trip the alert gate is a soft preference
    (`at_risk` says so out loud when it wins anyway). The traveller's **own train competes** under
    the same rules: when it wins, `alternative_journey` is null and `original_is_best` is true —
    distinct from a null with `original_is_best` false, which means nothing could be established.
    Both the fallback query and the reconciliation of the original's arrival are decided on **what
    the selection could use**, never on what the query returned (2026-08-06: a reply full of
    departures past the traveller's window answered "yes, something came back" and suppressed the
    only tier that could still help somebody whose train was cancelled).
    **The lookup runs once per disrupted TRAIN** (`impact._alternatives_by_situation`, 2026-08-12).
    The deadline that decides the winner is that train's own aimed arrival, so the answer is a claim
    about one train and about nothing else — it was one lookup per journey *window* until then, and
    a window is not a train: in prd one registered journey's 00:31 cycle alerted on thirteen
    cancelled L1 departures between 05:59 and 11:59, and all thirteen wordings offered the 05:45,
    which was the correct answer for the first of them. Three things bound the cost, and all three
    are needed, because a lookup is a blocking ~3 s call on the ingestion thread: the impact gate
    (now per situation), the 6 h horizon (`alternatives.within_lookahead`, now applied to the train
    being advised rather than only to the window's worst — this is the cut that makes the 11:59 case
    answer "nothing" instead of "the 05:45"), and `_MAX_ALTERNATIVE_LOOKUPS` (3, spent on the
    soonest departures, logged when it binds). Several situations about one train share one lookup.
    Still **once per stop ref**, on the canonical one (`match_journey`'s `pair_index == 0`): which
    ref of a place the question names does not change the answer, so `match_journey` back-fills onto
    an alert whose worst reading came from a *collapsed* ref — but **only when it is about the same
    train**, and then the whole outcome transfers (journey, `original_is_best`, `alternative_subject`)
    rather than just the journey. Such an alert therefore carries an alternative its own `/impact`
    response does not, and anything reading it must take it off the **Alert**, never off
    `alert.impact` (`llm_message.build_message_input`, fixed 2026-08-05: reading the response
    recorded those decisions as `has_alternative = true` with `alt_lines`, beside a `message_input`
    with no `alternativ_reise` — the advice withheld from the model and the template both). The two
    now agree by construction — `_collect_alerts` projects each situation's own outcome onto the
    response copy its alert carries — but the Alert is still the field the wording is built from.
    Expect `alt_status = 'not_worth_lookup'` to grow against `offered` at long lead times: that is
    the horizon finally reaching the trains that used to inherit a nearer train's answer.
  **The poller's two blocking passes run CONCURRENTLY** (2026-08-21), which is what makes a pilot
  ten times the size affordable. `check_and_notify`'s loop 1 is now three passes: **1a** matches
  every journey across `varsel._MATCH_WORKERS` (16) — carrying the Journey Planner lookups with it,
  since those live inside `match_journey`; **1b** reconciles caches and picks what to write for,
  sequentially, because it only mutates module state and blocks on nothing; **1c** writes the
  wordings across `varsel._LLM_WORKERS` (16). Both ceilings moved with the widths and only make
  sense together — `_MAX_LLM_CALLS_PER_CYCLE` 8 → 64 and `alternatives._POLLER_FETCH_MAX_PER_MIN`
  30 → 90 (the poller now has its own, split from the public `_FETCH_MAX_PER_MIN`, which is an
  abuse-control number and was setting the alerting budget only by accident). **Raising a ceiling
  without the width just makes the poller block longer**, which is what the old cap of 8 was quietly
  protecting against: 8 calls × ~2.2 s is ~18 s of a 30 s budget, so the count and the clock were
  one limit wearing two names, and raising the count alone would have moved the binding constraint
  to the clock one capped call at a time. Three things this rests on. Concurrency reached
  `alternatives._KeyedTTLCache`'s **single-flight** for the first time and it had to learn to
  **wait** rather than shrug: a second caller for a key already in flight used to be served "stale"
  — for a cold cache, `None` — which was harmless while the poller was sequential (A's fetch had
  finished before B asked) and becomes the common case once journeys sharing a stop pair are matched
  at the same instant. Without `wait_for_refresh` every journey but the first is told there is no
  alternative, having spent no fetch and learned nothing; nothing errors, the cards just quietly
  stop offering a replacement train. The wait is **poller-only** (an inbound request holding an
  anyio slot to wait on another request's fetch is how a slow upstream becomes a queue) and bounded
  by the fetch's own timeout, and `_release` fires on the fetch's **failure** path too — during a JP
  outage the success path never runs. The write budget (`varsel._WriteBudget`) **reserves** its
  worst case under a lock rather than checking a clock, so the deadline stays a ceiling on when the
  pass *ends*, exactly as it was single-threaded. And the write queue is now sorted **soonest
  departure first, across journeys**: it used to be walked in `_journeys` insertion order, which
  nobody noticed while ~44 registrations rarely reached a cap of 8, but at ten times that the cap
  binds on every busy morning and would hand the whole budget to whichever travellers registered
  earliest, every cycle, forever.

- The **Varselhistorikk** page at `/historikk` (`demo/varselhistorikk/`, React + Vite + Entur
  Linje) is the feedback loop for alerting: every decision Avviksvarsel took about a registered
  journey, the message that was sent, and — once the journey has run — a hit / false alarm / miss /
  correct-reject verdict. Its API is `/historikk-api` (`disruptions/api/historikk.py`).
  - Non-prod deployments label the heading and tab title (`Varselhistorikk (dev)`) from
    `GET /historikk-api/config`, sharing `deployment.environment_label()` with `/varsel`. It is its
    own endpoint, not a field on `/summary`, because both data endpoints 503 when the decision log
    cannot be read — precisely when "is this dev or prd?" matters — and it touches no BigQuery.
  - **Every decision is logged, not every push.** `varsel.check_and_notify` appends one row per
    (journey definition x situation x departure) per refresh cycle to `disruptions.varsel_decisions`
    via `disruptions/api/decisions.py`, *including* matches that stayed below the alert threshold.
    Without those, a `miss` leaves no trace and only precision is measurable. Repeats across cycles
    are intentional — they are the lead-time series. Writing fails open: a BigQuery problem never
    blocks a push.
  - **The row records the writer's input, not just its output** (2026-08-05). `message_input` is the
    exact JSON `llm_message.build_message_input` produced, serialized by the shared
    `llm_message.message_input_json` so it is byte-identical to the prompt — written for **every**
    decision, including the sub-threshold ones no message is written for (~83 % of rows, and the
    ones a miss is made of). **One document, three consumers**: the model is prompted with it,
    `varsel._deterministic_body` words the template fallback from it, and the row stores it — the
    same object, built once per decision in `check_and_notify`'s first loop, not rebuilt per
    consumer. It was `llm_payload` until 2026-08-05, when the recorded copy was a *second* build
    made after the message had already been written; a figure the wording needs therefore belongs in
    `build_message_input` (that is why `innstilling_prosent` is there, carried only when
    cancellation leads — quoting a low p_cancel is the 2026-07-27 regression). The rename migration
    in `create_varsel_decisions.sql` is guarded by `INFORMATION_SCHEMA`, **not** by
    `RENAME COLUMN IF EXISTS`: measured 2026-08-05, that form fails on the *second* run
    ("Column already exists"), which in a file re-run at every startup means the decision log dies
    on the next deploy. Flat alongside it: the observation the prompt tells the model to quote
    (`obs_*`) and the alternative offered (`has_alternative`, `alt_*` — a *replacement*, not
    necessarily a faster one), so "how often do we offer one?" is a GROUP BY. `alt_status` is what
    gives that rate a denominator: `offered` / `none_found` / `suppressed` (the selection ran and
    kept the traveller on their own train) / `not_worth_lookup` (none was issued), written on
    **every** decision including the ~83 % that are sub-threshold, so the 2026-08-06 selection
    change — which moves the numerator sharply — cannot be mistaken for a change in behaviour.
    `alt_arrives_before_original`, `alt_at_risk` and `alt_scored` qualify the offer, and
    `alt_operator` / `alt_jkey` / `alt_stop_order` are the recommendation's own ET join key, so
    "take this instead" can eventually be labelled the way every other decision here already is. `message_status` says why the stored wording has the source it does —
    `ok` / `ok_cached` / `not_alertworthy` / `not_selected` / `push_disabled` / `llm_disabled` /
    `capped` / `attempts_exhausted` / `transient_exhausted` / `rejected` / `ungrounded` / `error` —
    because a cap, an
    exhausted retry and a refusal all store the same deterministic text. `llm_disabled` changed
    meaning on 2026-08-07 and the schema description carries the new reading: it means
    no backend could be **asked** (`VARSEL_LLM_PROVIDER=off`, no project, everything latched), never
    "this deployment sends the template".
    **A failed write is charged to one of two budgets, and picking the wrong one retired writable
    payloads** (2026-08-22). Every `None` used to charge `_message_attempts` — including the several
    `_write_message` paths that ask no backend at all, and every transient Vertex failure — so three
    bad minutes retired a subject's wording for the situation's whole 12 h life. It was not a corner
    case: measured over the 14 days to 2026-08-21, the statuses preceding a first
    `attempts_exhausted` in prd were **290 `error` and zero `rejected`/`ungrounded`**, while **149 of
    the 270 subjects that hit an error (55 %) went on to succeed** on a later cycle — the budget for
    hopeless payloads was in practice spent entirely on the one class of failure it was never meant
    to bound, and 99 subjects were retired at three. Now three outcomes, three treatments:
    `_PAYLOAD_FAILURES` (`rejected`, `ungrounded` — the model answered and the answer was unusable
    for this input) charge `_message_attempts`, cap 3; `_TRANSIENT_FAILURES` (`error`) charge the
    separate `_message_errors`, cap `_MAX_TRANSIENT_ATTEMPTS` = 12, sized above the worst run
    observed (p50 2, max 9) and giving `transient_exhausted` on the row; and **a status naming
    neither means no backend was asked, so it charges nothing** — that third bucket is the whole
    defect, and it must not fall through to a default. A success clears both. `_write_message`
    reports which via an `outcome` out-parameter, the same shape and for the same reason as
    `alternatives.fetch_candidates` (25 suites stub it wholesale, so the `str | None` return has to
    survive). Two traps a similar change will hit again: a new module-level cache needs adding to
    **both** `check_and_notify`'s stale sweep and the test suite's `_reset_state` (a leaked
    half-spent budget reads exactly like the give-up bug), and a reworded column description is
    capped by BigQuery at **1024 characters** — `validate_varsel_sql.sh` is the only thing that
    catches it, since nothing in this repo executes SQL. Note a
    pre-2026-08-22 `attempts_exhausted` should be read as transient unless proven otherwise, so no
    rate spanning that date means anything.
    **Both wordings are recorded, and `message` is the one that was *delivered*** (2026-08-07):
    `message_llm` and `message_deterministic` sit beside `message`/`message_source`, so the two
    writers can be compared on the same decision — which is impossible while a flag decides that only
    one of them exists. The sent wording is chosen on `delivery_enabled()`, never on "an LLM wording
    exists": `written or deterministic` would file a template push as `llm` on the `/historikk` card
    *and* permanently in `varsel_decision_outcomes`, and would leave "written but withheld"
    (`message_source='deterministic' AND message_llm IS NOT NULL`) matching no row at all — the
    population prd's rollout was judged on, and which prd stopped adding to on 2026-08-23 when
    delivery was turned on. `message_deterministic` is written on **every** decision,
    so its presence is what dates a row as post-change; `message_llm` cannot, since NULL there also
    means the column did not exist yet (the `n_unsegmented` third-bucket rule, one column over). The
    side-by-side is `data/queries/message_wording_comparison.sql`; filter `message_status = 'ok'` for
    a same-input pair, because under `ok_cached` the model's text came from an earlier cycle's input
    than the template's did. `llm_provider`/`llm_model` outlive the Cloud Logging retention that used to
    be the only record, and `prompt_version` (SHA-256[:8] of the system prompt, derived so nobody
    has to remember to bump it) keeps rows written under different briefs comparable.
    `message_input` carries origin, destination and departure time together — the trio
    `/historikk-api` redacts — so it must never enter the API's SELECT; `Decision` is
    `extra="forbid"`, which fails loudly if it does.
  - Both tables and the `varsel_decision_groups` view are created by the app at startup
    (`create_varsel_decisions.sql`, `CREATE TABLE IF NOT EXISTS` — **never** `CREATE OR REPLACE`;
    their contents cannot be rebuilt). Because `IF NOT EXISTS` does nothing to an existing table,
    a new column also needs an `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in that same file — without
    it the write path just logs "N of M rows rejected by BigQuery" every cycle.
    **That file is two halves and `ensure_table` runs them as separate jobs** (2026-08-07): structure
    above the `-- BEST-EFFORT SECTION` marker decides readiness, the `ALTER COLUMN … SET OPTIONS`
    description refresh below it is best-effort and a failure there is logged and forgiven. BigQuery
    caps table-metadata updates at **5 per 10 s per table** and that block only ever grows — every
    reworded description adds a permanent statement. At seven of them it reliably returned "Exceeded
    rate limits: too many table update operations for this table", and since `ensure_table` treats
    any failure as "table not ready", one whole-file job meant a *cosmetic description edit* silently
    stopped the decision log recording anything at all. Prune statements whose text has demonstrably
    reached both dev and prd; a fresh environment reads its descriptions from the `CREATE` bodies.
    Note
    `validate_varsel_sql.sh` checks that file **statement by statement**: a dry run of a
    multi-statement script validates only the first one (`numChildJobs: 1`), so a broken ALTER
    would otherwise pass the validator and fail at startup. The outcomes table is created there too, not by the nightly
    job: `/historikk` queries it on every request and would 503 until the first nightly run
    otherwise. The view holds the grouping shared by the nightly job and the API, so the two cannot
    drift apart.
  - Outcomes come from `data/queries/label_varsel_decisions.sql` (scheduled query
    `varsel-label-daily`, 04:00 UTC, ~$0.01/run measured; **registered in both dev and prd** —
    register it per environment with the `bq mk --transfer_config` recipe in
    `label_varsel_decisions.sh`, which needs `--service_account_name` or it blocks on an
    interactive OAuth prompt. Each config holds a **copy** of the SQL, so a schema change is one
    ALTER and *two* `bq update --transfer_config` calls, in the same operation), joining
    `last_recorded` on
    `(operator, jkey, call_date, stop_order)`. It must **not** remap `dataSource='VYG'` to `'NSB'`
    the way `extend_demo_recent_daily.sql` does — varsel records the codespace from the line ref
    (`VYG`), so remapping would silently join zero rail rows. Backfill by hand with
    `data/queries/label_varsel_decisions.sh [project]`.
  - **An unmatched join is `NULL`, never "no trouble".** `last_recorded`'s rail slice runs 10-17 %
    thin, so some calls never resolve; counting them as correct rejections would manufacture
    precision. The API keeps `venter` (too recent to label) distinct from `ukjent` (labelled, no
    call found) for the same reason.
  - Recall on this page is recall **among SX-flagged calls on registered journeys** — the models
    never score a call no SX message flagged — and the UI says so next to the number.
  - **The headline precision is blended, and the page shows what it is made of** (2026-08-05).
    `/summary` keeps its existing top-level figures over *every* labelled decision — that is the
    honest answer to "could a traveller trust what we sent", and relaying an ET-declared
    cancellation is most of the app's value rather than a cheat — and adds `predicted` / `relayed`
    segments plus `n_unsegmented` beside them. Segment, never filter: quoted alone the blended
    number moves with the **mix**, so it can swing with nothing about the app having changed.
    `predicted` is what a threshold should be tuned on; a `miss` inside `relayed` is not a model
    failure at all but a plumbing one (the feed told us and we said nothing — the 0.57 case).
    Three things this rests on, all of them easy to get backwards:
    - The per-subject flag lives in `varsel_decision_groups`, resolved **at the cycle the verdict
      judges** — the first alerting cycle where we alerted, `LOGICAL_OR` where we did not. A plain
      `LOGICAL_OR` would be exactly backwards: a cancellation is routinely declared *after* we
      alerted (beating ET is the point), so it would file the stack's best wins as gimmes.
    - `NULL` is a **third bucket** (`n_unsegmented`), not a default to `predicted`. Rows written
      before 2026-08-05 carry no flag, and 83 of dev's 83 labelled rows are in it today, so the
      split describes only a slice of the window until the log turns over. Defaulting them would
      bury exactly the rows worth finding.
    - Adding a column to `varsel_decision_outcomes` **breaks the registered scheduled query** until
      it is re-registered: `label_varsel_decisions.sql`'s INSERT was positional, and the transfer
      config holds a *copy* of the SQL. Measured 2026-08-05 — the old text dry-ran as "Inserted row
      has wrong column count; Has 35, expected 36". The INSERT now names its columns, and new
      columns still go **last** in the CREATE so a fresh environment and an ALTERed one agree on
      order. Applying the DDL and updating the config are one operation, not two.
  - **The from/to filters are a deliberate narrowing of the privacy claim** (2026-08-21). The page
    has five dropdowns now — Utfall, Operatør, **Linje, Fra, Til** — populated by
    `GET /historikk-api/filters` (`historikk_filters.sql`) over the whole window, never from the
    rendered cards: that list is capped at 200 and ordered by departure, so options derived from it
    would drop a line that only ran early in the window and would shrink to the current selection
    once one was applied. Three things about it are load-bearing. `/filters` is also the API's
    **allow-list** — an unknown value is a 400, not an empty page, because the value lands in the
    response-cache key on a public unauthenticated endpoint and arbitrary strings would mint one
    cache entry and one BigQuery query apiece (`_checked_operator`'s reasoning, with the vocabulary
    read from the data); the page therefore fetches the options **before** the data on every filter
    change and clears a selection the narrowed window no longer contains, or a window change would
    flash an error the user never caused. **A from/to option is a PLACE, not a ref**, and getting that
    wrong shipped a real bug on 2026-08-21: names and grouping came from `main._varsel_stops`, which
    holds only *today's* timetable and deliberately collapses co-located refs keeping the busiest —
    so Asker stasjon's bus side (`NSR:StopPlace:3247`, 267 calls) won the collapse and its rail side
    (`NSR:StopPlace:418`, 264) came back nameless and was offered as a raw ref *beside its own name*.
    Naming it alone would have been worse: two identical "Asker stasjon" rows, which is exactly the
    2026-08-03 picker bug. The fix is `varsel`'s own three parts one layer up. Names and parents come
    from the **National Stop Register** directly (`historikk_stop_places.sql`, ~2 MB, fails open to
    bare refs — never from the picker cache); refs sharing a parent **and** a name are merged into one
    row on `refresh_varsel_stops`' exact rule (parent alone would merge Oslo S with Jernbanetorget;
    an unnamed ref is never merged); and the row **filters on every ref it covers**
    (`from_place_refs` / `to_place_refs` arrays, `historikk._place_refs`) — matching only the
    nominated ref hid 659 of Asker's 842 decisions behind the row named after it, which is
    `varsel.place_refs`' lesson verbatim. **The emptiness test must be
    `IFNULL(ARRAY_LENGTH(@refs), 0) = 0`**: an empty array from the Python client serializes as
    `{'arrayValues': []}` and BigQuery reads `ARRAY_LENGTH` of that as **NULL**, so the bare form is
    NULL on every unfiltered request and the ANDed predicate drops every row — the whole page went to
    zeros. A dry run validates it and `bq --parameter=refs:ARRAY<STRING>:[]` returns 0 (the CLI
    encodes empties differently), so neither cheap check catches it and nothing in this repo executes
    SQL; `test_the_no_filter_array_test_is_null_safe` greps for the bare form instead. And **Linje, Fra and Til reach
    `/summary` too**, exactly as `operator` already did, because they narrow the *population* — the
    rates must describe the slice the list beside them shows; `verdict`/`alerted_only`/
    `resolved_only` narrow by *outcome* and still must not, since filtering to hits makes precision
    read 100 %. What the change concedes is real and is written into the file headers rather than
    left implicit: `from_stop`/`to_stop` were withheld entirely before this, and a from/to pair can
    now be chosen and the matching departures read off.
  - **The per-row redaction of the route is GONE (2026-08-22), and that was the last of it.** The
    cards name a journey: `historikk_decisions.sql` selects `from_stop`/`to_stop`, `historikk.
    _with_place_names` pops them and serves resolved NSR `from_name`/`to_name`, and the card prints
    the pair — in the header, or as the drawn push's title where one is drawn. So "a listing does
    not pair an origin with a destination" no longer holds, and the two paragraphs above it in
    `historikk_decisions.sql` are history rather than a live guarantee. Three things make it smaller
    than the deletion looks, and one of them is the whole load: the 2026-08-21 filter change had
    already made the pair *choosable*, so what goes is the work of narrowing rather than the
    possibility; `/meldinger` already published exactly this, decided the same way; and **prd's
    registrations are test users rather than members of the public**, so there is no real traveller
    behind a k=1 pair. Team decision. The condition to re-check is therefore not a threshold but a
    fact — **if `/varsel` ever takes registrations from the public, this publishes real commutes** —
    and the fix then is access control on `/historikk`, not a shorter SELECT list, because the same
    exposure sits in `/meldinger`'s generated prose, which no projection controls. What still
    holds: `journey_digest` is absent from both surfaces (it is the join key to the LLM audit log,
    and no page needs it), the raw stop **refs** never leave the process, and `Decision` is still
    `extra="forbid"` — which is what fails loudly if a SELECT re-adds the digest or a future change
    forgets the pop. `test_historikk.test_the_refs_are_selected_but_only_resolved_names_are_served`
    pins both halves, and pins the *selection* too: resolve names without selecting the refs and
    every card quietly reads "Ukjent → Ukjent", which looks like a registry outage rather than a
    missing column.
  - Privacy, in two layers. The *table* is keyed on the journey **definition**
    (`journey_digest`, the same `llm_message.key_digest` used in the LLM audit log, so the two
    join) plus a device count — no `device_id`, no `journey_id`, no time window or weekday pattern.
    The *API* redacts further — though much less than it did, see the 2026-08-22 note above.
    `journey_digest` and the raw `from_stop`/`to_stop` refs are **not** served; the route itself is,
    by resolved NSR name. Until 2026-08-22 the route was withheld too, on the grounds that
    `/historikk-api` is unauthenticated on a public host and origin plus destination next to
    `departure_time` and `n_devices` publishes "somebody commutes A→B at 07:15" — which it does, and
    which is now accepted because the registrations behind it are test users. `Decision` is
    `extra="forbid"` so a SELECT that re-adds the digest, or a change that forgets to pop the refs,
    fails loudly instead of quietly publishing it. The page identifies a decision by its **train**
    and by the **journey it was watched for** — no longer by the train alone.
  - **The card quotes the wording that was PUSHED, not the last one produced** (2026-08-21).
    `varsel_decision_groups` took `ARRAY_AGG(message ORDER BY decided_at DESC LIMIT 1)`, and that is
    a different row: a wording lives in `varsel._messages` only while its situation stays above the
    alert gate, so `_reconcile_caches` calls `_forget_message` the tick a subject eases off and every
    later cycle records the template again. Measured on dev over 1 790 alerted subjects — 52 cards
    quoted the template under "Skrevet av modellen" though the push carried the model's prose, 85
    quoted the reverse, and 603 more quoted the right *kind* of wording from the wrong cycle
    (different percentages, a different replacement train): **41 % of the page was text nobody
    received**. The view now prefers the first `pushed` row and falls back to the last wording
    produced only when nothing was ever pushed — which is the state the card already labels "ikke
    levert". Two things it gets right that are easy to lose: `message` and `message_source` come from
    **one** `STRUCT` aggregate, because two aggregates "picking the same row" is a promise the
    ORDER BY cannot keep and its failure is a card showing one cycle's text under another cycle's
    attribution; and both halves skip rows whose `message` is NULL, so a pushed row predating that
    column falls through instead of blanking the card. `test_the_quoted_wording_is_the_one_that_was_
    pushed` pins both. The fix does **not** reach rows the nightly job already resolved —
    `varsel_decision_outcomes` holds a frozen copy and only unresolved rows are re-labelled — so
    `data/queries/backfill_outcome_pushed_message.sql` re-copies the two columns; **run it once per
    environment** (done in dev 2026-08-21, 738 of 3 888 rows corrected; prd still owes it).
  - **A wording per TRAIN, and the budget follows the push** (2026-08-21). The writing pass served
    `next(a for a in alerts if a.alertworthy)` — one train per journey — while loop 2 delivers per
    train (`one_alert_per_train`), so a journey with several disrupted departures pushed the model's
    prose for its worst train and the template for every other one: **604 of dev's 844 pushes over
    14 days, 83 % of everything that reached a lock screen**, filed as `message_status =
    'not_selected'`. `check_and_notify` now writes for `varsel._pending_pushes` — the collapsed
    trains a push is **still coming for** — capped by `_MAX_MESSAGES_PER_JOURNEY` (3) on top of the
    existing per-cycle budget. Not "every alert-worthy train", and the measurement is why: per
    journey-cycle on dev, alert-worthy trains run p50 3 / p90 12 / max 22, while *pushes* run p90 1
    / max 18 and 3 610 of 4 099 cycles push nothing at all — the trains are alert-worthy again every
    tick, but `_notified` means each reaches a phone once, so writing for all of them would spend a
    bounded budget re-wording trains nobody will be told about again and starve the one that is
    about to be. "Still coming" is per **registration**, because `_notified` is: a train the first
    device already heard about is still pending for the second. With nothing pending the journey's
    worst train is still written for — same one call as before — because the in-app card reads the
    same wordings and would otherwise decay to the template as soon as `_reconcile_caches` forgot
    one. `not_selected` survives with a much narrower meaning (a collapsed duplicate situation, a
    train with no push pending that was not the worst, or one past the cap), so a rate computed
    across the change is measuring two different things.
  - `venter` vs `ukjent` tracks the labelling job's 10-day retry window, on both surfaces: an
    unresolved row inside the window is still being retried (`venter`); past it, the attempts have
    given up (`ukjent`). Keep `historikk_decisions.sql` and `historikk_summary.sql` in step — the
    page shows the per-row state next to the count.
  - Neither table is in `copy_runtime_to_prd.sh`: each environment records its own decisions and
    `bq cp -f` overwrites. **Both environments record both wordings, and since 2026-08-23 both
    deliver the model's.** Every row carries a written wording and a template wording; which one
    reached the phone is `message_source`, and it is `llm` in dev and — from that date — in prd too.
    The distinction to keep is now a *date*, not an environment: prd rows before 2026-08-23 are the
    "written but withheld" population (`message_source='deterministic' AND message_llm IS NOT
    NULL`) the rollout was judged on, and a rate computed across that boundary is measuring two
    different deployments. prd's rows still matter most for the same reason they did: real
    travellers, real journeys.

- The **Meldinger** page at `/meldinger` (`demo/meldinger/`, React + Vite + Entur Linje) is
  the prompt-iteration loop: the alerts that actually went out for a window of DEPARTURES, replayed
  through **today's** prompt from the `message_input` already stored on every decision row.
  `/historikk` answers "what did we send, and was it right"; this one answers "what would we send out
  for these departures now", and nothing is sent to anybody. Its API is `/meldinger-api`
  (`disruptions/api/meldinger.py`) over `meldinger_{decisions,summary,filters,messages}.sql`.
  `config`, `filters`, `decisions` and `summary` are BigQuery only; `messages` is the one that calls
  Vertex — separate endpoints deliberately, so a Vertex outage empties the wordings and leaves every
  card, filter and statistic on the page intact, where one endpoint would 503 the whole thing. The
  two lists are aligned **position by position** — nothing served identifies a decision uniquely,
  since the real grain includes the redacted `journey_digest` — which is why
  `meldinger_messages.sql` repeats the decisions query's WHERE and ORDER BY verbatim, full subject
  key and all. /historikk's logic is **forked, not shared**: only `historikk_stop_places.sql` (it
  takes `@refs` and has no window of its own) and historikk's operator/verdict/lookback constants
  are, so a fix to one side must be mirrored to the other — the 2026-08-21 pushed-wording fix is
  exactly the shape of change that has to be.
  - **It was `/tilbakeblikk` until 2026-08-23**, and the rename went all the way down: the route,
    the API prefix, `demo/meldinger/`, `disruptions/api/meldinger*.{py,sql}`, `meldinger_static/`,
    the Vite `MELDINGER_` env prefix and the `TILBAKEBLIKK_ENABLED` flag (deleted — see below). The
    one thing that does not travel with a source rename is **Cloud Logging**: the structured events
    are now `meldinger_replay`, `meldinger_replay_retry` and `meldinger_bounded`, so a saved query
    or dashboard on the `tilbakeblikk_*` names silently returns nothing after the deploy rather
    than erroring, and rows either side of the rename need both names to be counted together. The
    `.tb-` CSS prefix in `demo/meldinger/src/` was deliberately left alone — it is internal, and
    renaming 200 class names risks a silent styling break for no reader's benefit. The plan document
    `docs/plans/2026-08-21-001-feat-tilbakeblikk-message-replay-plan.md` keeps its old name as the
    dated record of what was decided at the time.
  - **The window is `departure_time`** (changed from `decided_at` on 2026-08-22) — one Oslo day plus
    a from/to time of day, both ends quantized to 15-minute steps as a half-open `[from, to)`. The
    unit of this page is a MESSAGE, and a message belongs to a departure. The old axis selected
    *cycles*, which could not answer "what would we have sent for this departure" — a departure has
    one send moment, while a cycle window has to pick one by a rule the reader cannot see — and it
    cost a whole class of caveats: a subject's cycles straddled the window, so every per-cycle figure
    was window-scoped and `first_in_window` existed to stop "Varsel kl." claiming a first opportunity
    that was merely the earliest cycle the reader selected. On this axis a subject is **wholly in or
    wholly out**, so every figure is whole-subject and means what /historikk's means; `first_in_window`
    and the `vindu` badges are gone. **Tomorrow is a legal day** and was not before: the poller scores
    ~12 h forward, so at 23:00 there are real decisions about a 00:30 departure tomorrow. Quantizing
    is not cosmetic: every parameter lands in the response-cache key on a public unauthenticated
    endpoint, and two free-form second-resolution times are on the order of a billion keys a day,
    each one a cache entry and a BigQuery query of its own. `from` rounds down and `to` rounds up so
    a narrowed range never drops a minute the caller asked for, and the whole-day default runs 00:00
    to the **following** midnight — 23:59:59.999999 is not on the grid and quantizing it down would
    silently drop the last quarter-hour of every default request. A day more than one ahead, a day
    past `historikk._MAX_WINDOW_DAYS`, and a `to_time` at or before `from_time` are all 400s rather
    than empty pages, for the same key-space reason. What the change gives up is real: only a
    `decided_at` window reconstructs "what the app was saying at 07:15" as one coherent snapshot, and
    only it let a reader sweep lead time by moving the window.
  - **Both date bounds are now DERIVED, and both fail silently and cheaply.** `varsel_decisions` is
    `PARTITION BY DATE(decided_at)`, which the window predicate no longer touches — so the pruning
    predicate is a derived cover (`@day ± 1 day`) for "every cycle that could have decided about a
    departure in the window". **The trap got sharper, not softer**: under the old axis a too-narrow
    span only mislabelled `first_in_window`, and now it drops cycles out of the aggregates —
    `n_decisions` undercounts, `first_alert_at` moves later, and a push whose cycle fell outside
    reads as "never sent". Measured in dev 2026-08-22 over 117 448 rows (14 days):
    `DATE(departure Oslo) − DATE(decided)` is **0 or 1**, lead runs **−54 min to 710 min**, and
    `departure_time` is never NULL. Two things there are worth keeping, because the obvious version
    of each is wrong. A decision is **not** always before its departure — `catchable_after=now` drops
    a train that can no longer be *caught*, which a delayed train still can be — and that negative
    tail is the only thing that could ever put a cycle on `@day + 1`; at −54 min it cannot, so the
    `@day + 1` reach is margin against a looser cut or a lead past about −70 min, not decoration.
    And 710 min is the ~12 h horizon, which is what bounds `@day − 1`: **re-measure before widening
    it**, because at 24 h the bound stops covering the span. The outcomes join stays bounded
    separately on `call_date`, because that table is partitioned on it and a `departure_time`
    predicate prunes nothing there; it stays `± 1 day` even though the same measurement found
    `call_date = DATE(departure_time, 'Europe/Oslo')` for **all** 117 448 rows — fourteen days of
    dev, whose registrations number about three, is thin evidence for a claim about after-midnight
    operating days across every operator, so tighten it only against a prd measurement. Both failures
    are invisible — the subjects simply read as unresolved or under-counted, states the page renders
    perfectly happily — and the byte counts `validate_varsel_sql.sh` prints would report the loss as
    a **cheaper** query, i.e. as success. **Cost is not a correctness check here.** The outcomes bound
    also lives inside the `ON` and never the `WHERE`: in the `WHERE` it turns the LEFT JOIN into an
    inner one and drops every unlabelled subject, which on a same-day window is nearly all of them.
  - **The replayed cycle is the one a push actually went out on, looked up and never re-derived.**
    `pushed` is written by `varsel._record_decisions` *after* delivery, so it already carries all four
    delivery filters: the operator threshold, the not-yet-departed cut (`catchable_after=now`),
    `one_alert_per_train`'s collapse of several SX messages about one departure, and the `_notified`
    dedup keyed on `(journey_id, situation_number)` and persisted in the GCS state blob. A
    re-derivation would be strictly worse — it cannot see `_notified` across cycles and redeploys,
    cannot see a `gone` subscription that pushed a cycle later, and cannot see which situation won a
    collapse in a cycle it is not simulating — so it would produce a plausible send time that is not
    the send time. It is the **first** such cycle, because `_notified` makes the first delivery the
    only one for that situation's life. Picking `alertworthy` instead, which this did until
    2026-08-22, names a cycle no message went out on: most alert-worthy cycles are repeats
    `_notified` suppressed, and a collapse loser is alert-worthy for its whole life while reaching
    nobody. `send_state` on each card names which of six states a subject is in — `sendt`,
    `sendt_uten_input`, `under_terskel`, `samlet` (a sibling situation about the same train pushed
    instead), `push_av` (no VAPID keypair in this deployment) and `ikke_sendt` — and
    `other_pushed_on_train` is what distinguishes the middle two. That column also makes the page a
    **detector for the open `_notified` gap**: the dedup key is per situation while the collapse is
    per cycle, so a winner that flips between cycles pushes twice for one train, which shows up as
    two `sendt` subjects on one train. `pushed_only` is the filter for "what a traveller actually
    received"; it is strictly narrower than `alerted_only`, and both exist so the difference is
    visible. Note the log's grain is the journey **definition**, so one card stands for `n_devices`
    real pushes rather than one.
  - **Generation is the gate, delivery is not.** `/messages` asks `llm_message.generation_enabled()`
    and never `delivery_enabled()` or `varsel._deliverable_message` — the two switches are not
    interchangeable (generation says whether a wording can be *written*, delivery only what a
    *traveller* receives), and prd was exactly the deployment that generated without delivering
    (`VARSEL_LLM_PROVIDER=gemini`, `VARSEL_LLM_ENABLED=false`) until 2026-08-23. Delivery is on there
    now, which does **not** retire the rule: `VARSEL_LLM_ENABLED=false` is the documented revert for
    bad text, so the generate-without-deliver state is one deploy away at any time, and a surface
    that consulted the delivery gate would go **blank for a reason having nothing to do with the
    feature** — blank precisely where the data is worth looking at, and precisely when a prompt is
    being fixed.
    `test_the_delivery_gate_is_never_consulted` pins it. `generation_enabled` is asked **once per
    request**, not per subject: asked per subject it would flip mid-pass when a configuration error
    latched a backend off, and half the page would read `disabled` while the other half read `error`
    for one cause.
  - **Four bounds on regeneration, every one a number so it can be checked.** What they protect is
    not page responsiveness — it is the per-project Vertex quota the **poller** shares: a replay
    burst that eats it makes a real push wording 429, and a 429 is a *transient* failure that returns
    `status="error"` without trying the other backend, so a traveller silently gets the template and
    nothing on that side says so. **That shared draw grew 8x on 2026-08-21**:
    `varsel._MAX_LLM_CALLS_PER_CYCLE` went 8 -> 64 and the writing pass went 16-wide
    (`varsel._LLM_WORKERS`), so the poller can now hold 16 concurrent Vertex calls and sustain
    ~22/min against the same project where it used to manage ~3. `/meldinger` is now enabled in
    prd, so replay and poller demand can overlap at Vertex even though their in-process ledgers are
    separate. The replay ceilings below are a cost and abuse bound, not a reserved Vertex quota.
    (1) **1 000 backend CALLS per request.** Dev, tst, and prd all pin
    `VARSEL_LLM_PROVIDER=gemini`, so a subject can make at most one backend call and
    `_CALLS_PER_SUBJECT = 1`. The endpoint returns at most 200 subjects, so a full page fits in one
    request without consuming half the quota on a fallback that deployments never use.
    (2) **1 000 calls per trailing hour and 5 000 per trailing day**, tallied **by caller** in the shape of
    `alternatives._fetch_calls`' `{_REQUEST_PATH, _POLLER_PATH}` keying. Be honest about what that
    does here: the poller does not call this module at all, so its slot is empty by construction —
    the keying is what makes that structural rather than a fact somebody has to keep true, and what
    stops a later caller silently drawing replay's allowance down. The coupling it defends against is
    one layer out, at Vertex.
    (3) **A 30 s wall-clock deadline that RESERVES the worst case** rather than measuring elapsed
    time: `monotonic() + 8 s >= deadline` stops a new call being started, the shape
    `varsel._WORST_CASE_CALL_S` uses. Checking elapsed time would bound when the last call *starts*,
    not when the pass ends, and
    the check must run **inside the worker as its task starts**, never at submit time, or every task
    queues before the deadline is consulted and the bound guarantees nothing. Every environment
    pins one provider, so the reachable worst case is Gemini's `_TIMEOUT_S` (8 s). Pool width 4 --
    the shape `varsel`'s three pools use, and deliberately the
    narrowest of them (`_MATCH_WORKERS`/`_LLM_WORKERS` 16 each, `_PUSH_WORKERS` 32), because those run
    on the poller thread on a cadence nobody triggers while this one is reachable from a public
    unauthenticated endpoint -- so the cap costs three rounds, ~7 s typically.
    (4) **A wording cache keyed on subject + replayed `decided_at` + `llm_message.PROMPT_VERSION`**,
    10 000 entries, process-lifetime for stable results, and only 60 s for a transient failure so a
    red badge does not stick after Vertex recovers. Which failures get which is the same
    cut `varsel` makes with `_message_attempts` vs `_message_errors` (2026-08-22), arrived at
    independently and expressed here as a cache lifetime rather than a counter: `rejected` and
    `ungrounded` remain until eviction or restart because they are payload-determined -- the same stored `message_input` under
    the same `PROMPT_VERSION` will be refused again, and re-asking spends a Vertex call to learn
    nothing -- while `error` says only that the backend was unreachable a minute ago. Do not unify
    the two lifetimes on the grounds that both are failures. Both halves of the key are
    load-bearing: the prompt version is what makes an edit invalidate the page rather than show
    yesterday's answer under today's brief, and the replayed timestamp is what keeps an entry tied to
    the cycle it was written from. On the `decided_at` axis it did more than that — narrowing the
    window picked a *different* cycle for the same subject, and the timestamp is what made that
    correctly miss. On the departure axis narrowing only removes subjects, so the timestamp is now
    belt-and-braces rather than the thing preventing a wrong hit; keep it, since `pushed` can name a
    second cycle when the `_notified` winner flips. The cache is consulted **before** the budget, which is what makes
    repeated browsing cheap: a cache hit consumes no replay allowance and does not call Vertex.
    That is also what makes the page's **«Hent flere»** button work (2026-08-23): a bounded
    subject is never cached — `capped`/`deadline`/`rate_limited` return before `_remember_wording` —
    so a second `/messages` request for the same filters re-serves every wording already written for
    free and spends its allowance further down the list. A full 200-row page therefore fills in
    ~3 rounds rather than in one, and the button asks for the filters that produced the cards on
    screen, never the current control values. A page **reload** would do the same thing, which is
    what the notice used to advise; it just re-runs the window's BigQuery queries to arrive at the
    same cards.
    A bound that binds is reported per entry and the request still returns 200 — `capped`,
    `rate_limited` and `deadline` beside `no_input`, `unreadable_input`, `disabled` and the statuses
    off `MessageResult` — and `ungrounded`/`rejected` carry the **discarded reply**, since those two
    are what a prompt edit most plausibly produces and a badge with no text defeats the loop. On top
    of all four, `maximum_bytes_billed` is set to 2 GiB on every job: at ~11x the whole decision log
    (181.6 MB in prd, 135 MB in dev, measured 2026-08-21) it will **not** catch a pruning regression —
    it is a blast-radius bound against a mistyped join reaching a ~10 TiB ET table.
  - **The latch is isolated, and that is not optional.** `llm_message`'s `_active`, `_active_at`,
    `_latched_off` and the lock guarding them were four module globals; they now live in a
    `_LatchState` holder, the module keeps one global instance so **every existing caller is
    unchanged**, and replay takes its own via `new_replay_state()`. Without it, a replay burst that
    drove both backends into a `_ConfigurationError` would make `generation_enabled()` read False for
    the whole `_LATCH_TTL` (30 min) inside `varsel.check_and_notify`: every real push silently falls
    back to the template *and* the poller spends a message attempt per subject on each `None` —
    silently, on the side nobody watches. **The lock moves with the state**; left behind, a replay
    pool would hold separate latches and still contend on the one mutex with the poller thread, and
    the isolation would read stronger than it is. **Seeding is one-way**: a new holder copies
    `latched_off` from the global at construction (so it does not re-pay for a 404 the poller already
    discovered — `active` pins only on success, so a cold holder would re-walk claude-then-gemini
    every TTL window) and never writes anything back. The replay holder also caches one client per
    backend, which the live path does not need and a request-path fan-out does. Replay traffic is
    marked with a `replay` **field** on the existing `event="llm_message"` record rather than a new
    event name — a new name would quietly drop it out of every query that already exists, including
    the ones that would notice a burst — and it suppresses the prompt/reply DEBUG record regardless
    of `VARSEL_LLM_LOG_PROMPTS`, which dev has on and which would otherwise write a traveller's
    origin, destination and departure time to Cloud Logging at page-load volume. A second record,
    `event="meldinger_replay"`, carries the unhashed `journey_digest` so a replay joins both the
    decision row and the live trail. And the `/messages` route is **`async def`**, which is not a
    style choice: every other route in this process is a plain `def` and shares the anyio worker pool
    — `/impact`, the `/varsel-api` device and journey writes, the `/historikk` routes — so a handler
    that can hold one of those slots for its full 30 s deadline would starve traveller-facing
    registration from a public unauthenticated endpoint, with no ingress cutoff to reclaim it.
  - **The privacy split is structural, not a test.** `meldinger_decisions.sql` carries
    `message_input` only as a **null test** (`message_input IS NOT NULL AS has_input`) — no CTE in it
    holds the text — while `meldinger_messages.sql` names it and only the server ever reads those
    rows; what leaves the process is the wording generated *from* the input, never the input. A query
    that does not name a column cannot leak it however the model above it drifts, which is why the
    split is worth a duplicated query. The rest is /historikk's redaction verbatim: no decision row
    carries `journey_digest`, `from_stop` or `to_stop`, both `Decision` and `ReplayedMessage` are
    `extra="forbid"`, and the redacted columns are used as JOIN, WHERE and ORDER BY terms only
    (ordering by a redacted column is not projecting it). `/filters` publishes place refs as the
    dropdown vocabulary exactly as /historikk's does — the redaction is per **row**, not per
    response, or the Fra/Til filters could not exist at all — and it is the API's allow-list, so an
    unknown value is a 400 rather than one more cache entry and one more BigQuery query.
  - **The surface is served unconditionally, in every environment.** It was gated on a
    `TILBAKEBLIKK_ENABLED` env var (one switch over both the router and the static mount, failing
    closed) from the day it shipped until the **2026-08-23** rename to `/meldinger`, which removed
    the flag: prd had been `"true"` since 2026-08-22 and dev/tst since the start, so the switch was
    a chart value nobody varied any more. The bundle was always built into every image regardless;
    only the variable moved, which is why removing it changed nothing that was deployed.
    **Q1 is answered, and the answer rests on one fact rather than on the measurement.** What the
    surface publishes is real: the prompt instructs the model to advise on the traveller's own route,
    so the prose routinely names **both ends of a trip**, `trafficType: public` is set once in the
    chart and inherited by every environment, and measured on 2026-08-22 over 14 days of prd
    decisions there are **49 journey definitions across 31 from/to pairs, 21 of which (68 %) isolate
    exactly one journey** — best k anywhere is 5. So a Fra/Til selection in the page's own dropdowns
    identifies a single registered journey two times out of three, and no column-level redaction can
    close that, because the exposure is in the **generated sentence** and not in a column. It is
    accepted because **prd's registrations are test users, not members of the public** (team
    decision, 2026-08-22): there is no real traveller behind a k=1 pair, and the corridors themselves
    carry tens of thousands of people a day, so naming one as disrupted discloses nothing about
    anybody. **Re-run that measurement if prd ever takes registrations from the public** — at k=1 on
    two thirds of pairs it becomes a genuine disclosure, and the fix is then access control in front
    of `/meldinger` (a chart change, since `trafficType` is set once), not a shorter SELECT list, and
    not the deleted flag either: withholding the whole surface from prd would answer the prompt
    question on dev's thin data. A page built on *hypothetical* journeys rather than registrations
    would retire the question outright. Note the volume argument for prd is weaker than it looks:
    measured the same day, prd's pushed subjects run only ~22 % above dev's over six days (408 vs
    334), because a subject is (journey × situation × train) and dev's few registrations sit on busy
    corridors. What prd actually adds is **authenticity** — real registrations and, since
    2026-08-23, wordings a traveller actually received; before that date its rows are the
    written-but-withheld pairs (`message_source='deterministic' AND message_llm IS NOT NULL`) the
    rollout decision was judged on.
  - **The card's figures are whole-subject**, and were not before 2026-08-22. On the `decided_at`
    axis the per-cycle aggregates (`n_decisions`, `ever_alerted`, `first_alert_at`, `alert_lead_min`,
    the four `final_*`, `max_impact_probability`, `pushed_any`) described the window's *slice* of a
    subject while the verdict came whole from the nightly job, so `ever_alerted: false` beside
    `verdict: 'hit'` was correct rather than a bug — it alerted outside the window the reader chose.
    That needed a `WINDOW-SCOPED` note on every field description and a `vindu` badge on every
    figure, plus `first_in_window` to stop "Varsel kl." claiming a first opportunity the window
    could not support. The departure axis puts a subject wholly in or wholly out, so all of it is
    gone and `alert_lead_min` now agrees with the nightly job's by construction rather than by a
    recomputation kept from disagreeing with it. It is still computed here rather than copied, so an
    unlabelled subject — nearly all of a same-day window — also gets a lead time.
    `/summary`'s `median_lead_min_on_hits` was always whole-subject,
    from the nightly job — which is what keeps it comparable with /historikk's.
  - **What a replay measures, and what it does not.** It measures a **prompt** change, not a
    `build_message_input` change: the stored document was built by the builder of that day and is
    replayed byte-identically (`message_input_json(json.loads(s)) == s`), so a figure a newer builder
    would add is simply not there — a newer prompt that asks the model to quote one gets a reply
    quoting figures absent from the input, which the grounding check refuses as `ungrounded`, and the
    status is shown rather than hidden. And each wording is a **single draw from a non-deterministic
    decoder**,
    cached per row and prompt version — so a difference between two cards, or between two prompt
    versions, is confounded with sampling variance, and reloading and seeing the same text is the
    cache rather than evidence that the model is predictable. The page says both, in prose at the
    top and not in a tooltip.
  - `data/queries/validate_varsel_sql.sh` covers all four new files (the parameter sets differ per
    query — fourteen for the two row queries against /historikk's eleven, eight for the summary,
    three for the filters — and one omitted makes `bq` refuse to parse and the check reports FAILED,
    so the file goes **unvalidated**; `test_meldinger.py` greps the script for every `@param` as
    the cheap half of that guard). A clean dry run still proves neither of the two things that
    matter most: the CLI encodes an empty ARRAY parameter differently from the Python client, so the
    `IFNULL(ARRAY_LENGTH(@refs), 0) = 0` rule dry-runs identically either way, and **both** date
    bounds above fail silently and *cheaply* — the derived `DATE(decided_at)` cover as well as the
    `call_date` one.
  - **A same-day window is almost entirely `venter`**, because verdicts arrive from the 04:00 UTC
    labelling job the following night and /historikk avoids this by defaulting to 14 days on
    `call_date` — which this page cannot do, since its question is about a stretch of a specific day.
    So precision, recall and lead on the default view describe a handful of rows, and `/summary`
    states that as counts (`n_subjects`, `n_labelled`, `n_pending`, `n_unknown`) *before* the rates
    rather than showing confident-looking numbers. The blended-plus-segments rule is /historikk's
    unchanged: `predicted` / `relayed` / `no_sx` beside `n_unsegmented`. `/summary` takes the
    **population** filters only (day, time range, operator, line, SIRI-SX summary, from/to place);
    `verdict`, `alerted_only` and `resolved_only` narrow by outcome and never reach it — filtering
    to hits would make precision read 100 % — and `sx_only` is withheld from it on the same
    grounds.

- The **Reisevalg** endpoint `GET /reisevalg-api/recommendation` (car vs train for the
  Lier/Drammen → Oslo S corridor; feasibility prototype — see `docs/bil-avvik/LEARNINGS.md`)
  lives in `disruptions/api/reisevalg.py`. Road signal: SVV Trafikkdata volumes gated to
  UNKNOWN (measured precision 0.60 < 0.8 bar) + a DATEX II stub behind optional
  `SVV_DATEX_USER`/`SVV_DATEX_PASSWORD` (unset = provider unavailable; when provisioning,
  use Secret Manager like the VAPID keys). Its `disruptions.svv_baselines` lookup loads in
  the poller/lifespan and is deliberately NOT in `copy_runtime_to_prd.sh` — prd serves the
  road side as UNKNOWN until a road provider is validated. Rebuild baselines/backtest with
  `data/queries/reisevalg_*.sql`; raw volume backfill via `data/svv/download_volumes.py`.

- **Rushvarsel** at `/togmelding` (`/togmelding-api/today`, `disruptions/api/togvarsel.py`) is
  the rush-hour forecast from `docs/togvarsel/` (§8 of the findings as revised by §14k; the
  implementation is `docs/togvarsel/03-implementasjon.md`). Four weekday issues (06:30, 07:00 for
  the AM rush; 12:00, 14:00 for the PM rush), each a tuned `LOGISTIC_REG` probability on the
  product bar (cancelled OR ≥ 10 min) mapped to green/yellow/red at fixed cuts (0.10/0.30), with
  rule-based reasons and a deterministic "kjente avvik" list. `togvarsel.refresh()` runs on
  every poll tick (wall-clock — an issue is due at its cutoff whether or not SX moved) and
  computes due issues (one rebuild of `togvarsel_features_today` + one `ML.PREDICT` per issue —
  a handful of BQ jobs per weekday; failures back off per issue and give up after 8 tries/day);
  weekends/holidays get no forecast, and issued forecasts land in `disruptions.togvarsel_issued`
  (MERGE, first write per (d, issue) wins; `late` marks back-fills — per-environment, not copied
  to prd). Retrain monthly with `data/queries/togvarsel/retrain.sh` (≈ $1.5; trains candidates,
  promotes all four `togvarsel_m_*` only on full success), then `copy_runtime_to_prd.sh` copies
  models + tables to prd. Holidays/low season live in ONE place: `disruptions.togvarsel_calendar`
  (`data/queries/togvarsel/00_calendar.sql`) — extend yearly; serving ERRORs loudly past its
  horizon and retrain.sh refuses under 60 days of headroom.
  **The page is a Vite bundle on Entur Linje** (`demo/togmelding/`, React + Vite + Linje), built
  into the image by the root Dockerfile's node stage like the other four front-ends, with
  `disruptions/api/togmelding_static/` now GENERATED and gitignored. It was one committed
  hand-written HTML file (the `eval_static` pattern, no build step) until 2026-09-01, when it moved
  onto Linje to match `/varsel` — a traveller usually arrives here by tapping a rush-forecast push
  from that PWA, and two surfaces of one product should not look like two products. So the header
  band, the card language and the feedback widget are `/varsel`'s, `src/styles.css` is a near-copy
  of `demo/avviksvarsel/src/styles.css`, and `GET /togmelding-api/config` labels non-prod in the
  heading and tab title exactly as `/historikk` and `/meldinger` do (its own endpoint, not a field
  on `/today`, because `/today` is legitimately empty on a weekend or a failed rebuild and that is
  when "dev or prd?" matters most). Three things the move has to keep right. The level colours are
  Linje **validation tokens** (`--stroke-success` / `--fill-success-muted` and the warning/negative
  pair), never literal hex, and the badge keeps DARK ink on a LIGHT tint at all three levels —
  Linje's canary is too light for white text and too light to darken without leaving the palette,
  so a filled swatch cannot be done consistently. The level accent is an **inset box-shadow**, not
  a thick `border-left`, which against the card's radius leaves a mitred wedge at both corners.
  And the widget's half-typed comment survives the 5-minute poll because the component is **keyed
  on `feedback_context_id`** — the vanilla page kept that state outside the DOM by hand; React's
  key does the same job only as long as the server keeps the id byte-stable, which its fingerprint
  dedup does. There is deliberately no `?brand=none` variant, unlike `/varsel`: the unbranded
  install's pushes never link here.
  **The product is called «Rushvarsel» and the code is called togvarsel/togmelding** (renamed
  from «Togmeldingen» on 2026-09-01). The rename is deliberately DISPLAY-ONLY: the page heading,
  `<title>`, meta description, the two no-forecast sentences, the published disclaimer and the two
  link texts on /varsel. Nothing identifying moved — not the `/togmelding` route (already baked into push
  payloads sitting on lock screens, where a moved path is a dead tap), not `/togmelding-api`, the
  modules, `demo/togmelding/`, the `disruptions.togvarsel_*` tables and models,
  `TOGVARSEL_NOTIFY_ENABLED`, the `togvarsel_*` log events, or the two data discriminators
  (`display_source='togvarsel'`, `KIX:Togvarsel:<date>:<issue>`) — that last pair for the
  meldinger-rename reason: a new name drops the rows out of every export query that already
  exists. The page intro no longer opens with «Rushtidsvarsel for lokaltogene …» — the phrase the
  name was drawn from, so keeping it would have the intro restate the heading. One thing the name
  buys for free: the push titles already said «Rushvarsel»/«Oppdatert rushvarsel»
  (`togvarsel_notify.compose_message`), so the notifications and the product now carry the same
  word instead of two — and the /varsel notice announcing the push class reads «Nytt: Rushvarsel»,
  capitalised, because that string went from a common noun to a proper name.
  **The published privacy texts stay DUPLICATED, and the reason changed while the arrangement did
  not** (2026-09-01, plan 2026-08-31-001 KTD8): the experiment disclaimer and the T7 sender footer
  live in `demo/togmelding/src/personvern.ts`, a copy of `demo/avviksvarsel/src/personvern.ts`.
  Before the move the page could not import that module because it had no build step; now it cannot
  because the image's node stage copies **only `demo/togmelding/`**, so a relative import into
  `../avviksvarsel/src/` resolves on a laptop and fails in CI — the worst shape of breakage.
  `disruptions/api/test_personvern_texts.py` now resolves the constants out of BOTH modules and
  compares them, which is tighter than the old markup search (a text present but never rendered
  used to pass) — so it also asserts each constant is actually **referenced** by the app, and the
  bracket guard moved from a slice of markup, which JSX has no equivalent of, onto the resolved
  strings. `TOGMELDING_PERSONVERN_LINK_TEXT` was added to the shared vocabulary in the same pass:
  the vanilla footer shipped that string un-guarded.
  **Each issue card carries the /varsel feedback widget** (2026-08-30): `togmelding_today` mints
  one `feedback_contexts` capability per issued forecast — on the request path only, never in
  `refresh()`/`build_melding()`, and on a COPY of the cached Issue, so `issued_today()`'s notify
  seam stays a pure forecast — and the page posts to the shared `POST /varsel-api/feedback`, so
  the gate (`feedback.enabled()` — dev since 2026-08-28 and prd since 2026-08-31, tst off; the
  approval behind it is per environment because the subjects are, and prd's also classified the
  origin/destination/departure-time trio as «Intern», which closed punkt 9b for the Gemini prompt
  too), rate limits, receipts, PostHog event and the
  data/feedback exporter/reviewer are all varsel's, unchanged. Rows are discriminated the
  documented way — `display_source='togvarsel'`, `situation_number = KIX:Togvarsel:<date>:<issue>`
  — never a new column or event name (a new event would drop them out of every existing export
  query, the meldinger-rename lesson). The snapshot quotes the page's literal rendering
  (`togvarsel._LEVEL_TEXT` mirrors the page's `LEVEL_TEXT`, JS-round parity on the percentage) and
  its key set is pinned against the contexts DDL in `test_togvarsel.py` — a key the table lacks
  fails the streamed insert invisibly (the 2026-08-27 mode). Page-side, widget state lives outside
  the DOM keyed by context_id and `load()` skips re-rendering an unchanged payload: fingerprint
  dedup keeps the context ids — and therefore the payload — byte-stable between forecast changes,
  which is what lets a half-typed comment survive the 5-minute poll.

- **Togvarsel push rounds** (`disruptions/api/togvarsel_notify.py`, 2026-08-30) deliver
  Rushvarsel's prognoses as /varsel Web Pushes. Each of the four weekday issues is one
  notification *round*: the baselines (06:30, 12:00) push iff yellow or red; the follow-ups
  (07:00, 14:00) push iff the level *differs* from what their baseline pushed — a level
  transition, never a probability delta (the paired models answer different questions with
  different calibration, so raw p is not comparable across them) — and a green follow-up after a
  pushed warning is the one green that pushes: the friskmelding. Wording is deterministic
  Norwegian from the issue's own fields (no LLM), titled `Rushvarsel`/`Oppdatert
  rushvarsel`/`Friskmelding` so a red morning's SX push and forecast push stay tellable apart;
  entur-brand pushes click through to `/togmelding`. Delivery is gated by
  `TOGVARSEL_NOTIFY_ENABLED` (allow-list parse; **unset = off**; helm base off, dev on since
  launch, prd on since 2026-08-30, tst off), and while off nothing is evaluated and no state
  written, so an enable-restart inside a window still delivers. Turning it on in an environment
  needs more than the flag: the 39-station geography `disruptions.togvarsel_oslo_stations`
  (fetched once per Oslo day, DEFINES matching, never duplicated in Python) exists on prd only
  after `copy_runtime_to_prd.sh` — and existing subscribers opted into train-specific alerts, so
  the /varsel page announces the new push class ("Nytt: rushvarsel", shown only where /config
  reports `togvarsel_notify: true`, which follows this same flag via a provider injected in
  `varsel.configure`). State is one primitive: round markers
  `{date: {issue: {level, pushed}}}` in the varsel state blob — dedup (a marked round never
  re-fires, suppressions included) and the follow-up baseline in one; markers commit in memory
  before fan-out and persist after, so a crash mid-round inside the timeliness window can
  duplicate that one round — accepted. Timeliness is strict Oslo wall-clock at evaluation time
  (0630 < 07:00, 0700 < 09:00, 1200 < 14:00, 1400 < 18:00; a late back-fill is recorded as
  suppressed and never revisited), and matching is additionally bounded by evaluation time: a
  journey whose registered window has already passed never gets the push, friskmelding included.
  Fatigue is the known cost: yellow-or-red fires on ≈56 % of weekday mornings (measured level
  bins), the alert-noise shape this repo has rejected before — shipped as requested, mitigated
  (the change gate caps a rush at two pushes, reasons ride in the body, delivery starts dev-only;
  red-only/per-user levels are the documented follow-up). Audit is structured logs (one
  `togvarsel_notify_round` event per round with decision and sent/error/gone counts) +
  `disruptions.togvarsel_issued` + the persisted markers — deliberately **no `varsel_decisions`
  rows** in v1: the decision is a deterministic function of durable inputs, and adding columns to
  the decisions/outcomes tables is a documented trap (a new `varsel_decision_outcomes` column
  breaks the registered scheduled query — see Conventions below); if rows come later, discriminate on
  a `KIX:Togvarsel:<date>:<issue>` situationNumber prefix, never a new column.
  **The levels also sit at the top of `/varsel`** (2026-09-01):
  `GET /togmelding-api/outlook?device_id=…` (`togvarsel_notify.rush_outlook`) answers with the
  rushes THIS device is notified about that are not yet over, and `demo/avviksvarsel`'s
  `RushOutlook` card renders them with a link to `/togmelding` — nothing at all when the list is
  empty, which is the ordinary state. It is the push policy read-only, in the same module, so
  there is one definition of "a relevant journey": the same three matching tests, the same
  supersession (the newest issued forecast per rush, so the two surfaces cannot quote different
  numbers for one rush), gated on the delivery flag. Two things are deliberately different from a
  round. The window is the **whole rush**, not the part still ahead of `now` — `match_journeys`
  clamps to evaluation time because a push about a window the traveller has left is noise, while a
  status panel going blank at 08:00 with the trains still late is the opposite of useful — so what
  bounds the panel is the rush's end (09:00 / 18:00), never the registration's. And the request
  path reads the poller's station cache through `cached_station_set` and **never queries**:
  `station_set` deliberately has no failure cooldown, which is right on a poller tick and one
  billable scan per page view on a public unauthenticated endpoint. A cold cache therefore yields
  no panel — and that doubles as the VAPID gate, since `_evaluate` reaches the fetch only past
  both gates. The route is `async def` (every input is already in-process) where `test-notify` is
  a plain `def`, and the two writes in `station_set` publish `refs` before `date` so the lock-free
  read cannot see a date whose refs have not landed. Dev-only test
  trigger: `POST /togmelding-api/test-notify` with `{"device_id": ...}` (same shape as
  `/varsel-api/test`) sends one synthetic yellow round — body marked as testvarsel — through the
  real composition + delivery path without touching markers; it 404s wherever
  `deployment.environment_label()` is not exactly `"dev"` (tst, prd, unset and unrecognized labels
  all fail closed). Tests: `disruptions/api/test_togvarsel_notify.py` (hermetic; standalone and
  under pytest).


- The **Rushhistorikk** page at `/rushhistorikk` (`demo/rushhistorikk/`, React + Vite + Entur
  Linje) is the feedback loop for **Rushvarsel** — what `/historikk` is to `/varsel`. One row per
  DAY: the four forecast issues (06:30, 07:00, 12:00, 14:00), what was communicated for each, and
  what the two rushes actually did. Its API is `/rushhistorikk-api`
  (`disruptions/api/rushhistorikk.py`), one query (`rushhistorikk_days.sql`) per request.
  - **Three records join to answer it, and two of them are new.** `togvarsel_issued` had no reader
    anywhere in the repo before this; `disruptions.togvarsel_rounds` records every notification
    round as it was DECIDED (pushes and suppressions alike, plus the reason) and
    `disruptions.togvarsel_outcomes` records what the rush actually did. Neither is in
    `copy_runtime_to_prd.sh`: each environment resolves the forecasts it issued itself, and
    `bq cp -f` overwrites.
  - **Nothing about the notification was durable before.** The round markers in the `/varsel` state
    blob are the runtime primitive, but `varsel.store_togvarsel_rounds` replaces the whole map with
    a SINGLE DATE — the first write after midnight destroys yesterday's — and the
    `togvarsel_notify_round` log event carries no Oslo date and has no BigQuery sink. So no round
    HISTORY can be backfilled as a record. What the page does instead, since 2026-09-02, is
    **reconstruct** the rounds nobody recorded from the forecast log — next bullet — so it fills
    back to the first issued forecast rather than starting at the deploy. The three days before the
    round table existed (31 Aug – 2 Sep 2026) were **backfilled from the `togvarsel_notify_round`
    log events** in both dev and prd on 2026-09-02
    (`data/queries/backfill_togvarsel_rounds_from_log.sql`, 10 and 11 rows) — a copy of the app's
    own record, not a replay, and it agreed with the reconstruction on every round, so no verdict
    moved; only «Varsler sendt» did. Those events live in the GKE **cluster** project
    (`ent-kub-{dev,prd}`, namespace `kix-avvik`), not the app project — `gcloud logging read
    --project=ent-kixavvik-prd` returns nothing for them, silently — and expire after 30 days.
  - **A recorded round is never re-derived, and a round nobody recorded is reconstructed and
    labelled** (the second half since 2026-09-02). `decide_issue` is a pure function of (level,
    marker state, clock), so replaying it looks like a substitute for the record and is not: it
    cannot see a round the gate closed before evaluating (which writes no markers at all), cannot
    see the `untimely` verdict on a back-filled round, and cannot see how many devices matched. Same
    rule as `/meldinger`'s `pushed` — so where a `togvarsel_rounds` row exists it always wins. But
    the page exists to judge the MODEL, and for that the forecast plus the policy is the whole
    input, so `rushhistorikk.reconstruct_rounds` replays `togvarsel_notify.decide_issue` for every
    issue with NO row — at the forecast's own `computed_at`, baseline before follow-up, with the
    day's recorded rounds seeding the marker state — and grades the result like a recorded round.
    It calls the policy rather than restating it (a test greps the source for policy tokens), flags
    every such issue (`reconstructed`), counts them beside the rates (`n_reconstructed`, and
    `n_reconstructed_after_start` as the lost-write detector), and keeps a would-have-been push out
    of `varsler_sendt` (it goes in `varsler_rekonstruert`), because that column says "sent". The
    product owner asked for this on 2026-09-02 ("the main purpose of this dashboard is to judge
    the quality of the model"), having earlier chosen to score only real rounds; without it the
    page read `ikke vurdert` for every issue before the round log and, in tst, forever.
    Three things the write gets right: `baseline_level` rides on the `Decision` (reading it off the
    BASELINE's row would let one swallowed best-effort write invert a verdict — a 06:30 red that
    pushed and was right reads as a miss if only its own MERGE failed), so `_effective_baseline` is
    resolved BEFORE the deadline check; the recording loop sits at the very END of `_evaluate`,
    because the two sites that persist markers are inside the `pushes` loop and in the `not pushes`
    branch and NEITHER reaches a suppression on a mixed pass; and it is never called from
    `push_round`, which `POST /togmelding-api/test-notify` reaches directly on the public dev host
    with a caller-supplied `device_id` — on a first-write-wins MERGE that write would take the day's
    row for good.
  - **The fasit is two passes, because no single source serves both timeliness and the models'
    semantics.** `last_recorded` is a once-daily batch (measured 2026-09-01: every partition shares
    one `last_modified_time` of ~03:04 UTC, and the current day's held 115 k rows against 1.81 M for
    the previous full day), so it cannot answer for the morning rush at 09:20 — and `01_calls.sql`
    codifies that by stopping at `CURRENT_DATE() - 1`. So: a **provisional** pass at 09:20 and 18:20
    Oslo from `rail_calls_today` + `rail_calls_planned_today` (3.4 MB, tables the poller already
    builds), and an **authoritative** pass after 04:00 UTC from `last_recorded` (32.5 MB/day,
    ~$0.0002) that settles the row. Both ride the poller's wall-clock tick beside
    `togvarsel.refresh()` rather than a transfer config — four configs each holding its own COPY of
    the SQL, on a fixed UTC time that slides an hour against Oslo twice a year, none of which could
    self-heal a missed run. `:20` and not `:05` because both provisional sources are TTL-gated off
    the SX rebuild (`_RAIL_CALLS_TTL` 15 min, `_PLANNED_TTL` 60 min), so an earlier read stops
    before the window it measures. The provisional pass's **four skews are named in its file
    header** (departure delay where the label prefers arrival; `recordedCalls` coverage;
    TTL staleness; extra journeys it cannot exclude) — that is why its value is marked and excluded
    from every rate, and why `provisional_trouble10`/`_level`/`_at` are **never overwritten** by the
    settled pass: the overnight disagreement rate is the measurement that decides whether the pass
    earns its keep, and destroying it would repeat the marker store's own defect.
  - **The authoritative pass's latch is keyed on the UTC DATE, and mixing the two calendars once
    cost the whole page.** Its gate is a UTC hour (04:00) because the batch it waits for rewrites at
    a UTC hour, while everything else in `togvarsel_outcomes` is scoped to the **Oslo** day because a
    rush is an Oslo-clock thing. Held as a boolean inside the Oslo-day state those two shared one
    dict, and the failure was not a near miss: at Oslo midnight — 22:00 UTC in CEST, 23:00 in CET —
    the Oslo rollover cleared the flag while `hour >= 4` was trivially true, so the pass ran about
    five hours **before** the batch it depends on, found nothing, and settled every day as
    `unresolvable` — permanently, and with nothing logged as an error. Two calendars now live in two
    variables: `_settled_last_utc` sits outside `_state`, which is also why it needs resetting in the
    suite's own `_reset` (a leaked latch makes half the suite pass by accident). Both offsets are
    pinned, because a fix that handled only the summer one would look right for half the year.
  - **The settled work set has two reaches, and the SELECT is chunked** (2026-09-02). Inside the
    10-day retry window, any issued day not fully settled; beyond it, out to `_BACKFILL_DAYS` (60),
    only issued days with NO outcome row at all — the pre-feature backlog each environment has
    once, and any day a long outage skipped — because a day with rows there was settled or marked
    unresolvable, and re-reading either re-runs an immutable query to the same answer. This is what
    lets the page initialise from the forecast log: without it every issue older than ten days
    read `ukjent`. The range SELECT is cut into spans of `_SETTLED_CHUNK_DAYS` (14) CALENDAR days,
    one job each, because at the measured 32.5 MB/day the whole horizon in one job sits at
    `_MAX_BYTES_BILLED` and the first pass in a fresh environment would fail every retry and settle
    nothing — a failure that logs as a bytes error, not as "nothing ever settles". By calendar span
    and not by count, since the work set holds weekdays only.
  - **A day NO scope resolved for is left for the retry window; a day missing ONE scope settles.**
    That pair is the discriminator between the two causes of an empty read, and only one is final.
    With a sibling scope resolved the batch has demonstrably landed, so "no resolved call in this
    window" is an answer about the data and `unresolvable` is right — `last_recorded` is immutable,
    and retrying for ten days re-runs one query to reach the same NULL. With nothing at all the
    likeliest cause is that the batch has not landed for that day yet, and because
    `resolve_outcome_state` reads `unresolvable` **before** the window, three rows written there
    burn the day for good on the strength of one early read. Left alone the day stays in the work
    set, reads `venter`, is retried daily, and turns `ukjent` at day 11 by itself. Same shape one
    layer up: a provisional scope the query answered nothing for stays **due**, or one empty read
    retires it for the rest of the Oslo day and the page waits for the overnight pass with nothing
    retrying. And the two provisional windows hold **separate** give-up budgets, or a morning
    outage silently forfeits an afternoon check on a day that was healthy by 18:20.
  - **ROUNDING IS PER SCOPE, and getting it uniform breaks the identity the page rests on.**
    `03_day_windows.sql` emits `ROUND(SAFE_DIVIDE(...), 4)`, which `04_days.sql` carries into
    `am_t10`/`pm_t10`; `08_features_cat.sql` emits a **raw** `SAFE_DIVIDE` for `am_t10_rest0700` —
    and it is the only file that defines the 07:00-09:00 window at all, so "reproduces
    `03_day_windows.sql`" could never have covered the 0700 issue. Worked example: 204 troubled of
    2041 resolved is 0.0999510, which rounds to the 0.10 bar (label TRUE, level must be yellow) and
    truncates below it (label TRUE, level would read green).
  - **The realized level is green below the bar, yellow to twice the bar, red at or above — and only
    the green boundary follows from the models.** Nothing in the repo defined a realized
    green/yellow/red: the 0.10/0.30 cuts are on the PROBABILITY while the truth is the binary
    `trouble10 >= bar`. What keeps this from minting a second definition of "a bad rush" is that a
    non-green level coincides EXACTLY with the training label being true — the green boundary *is*
    the bar (per-scope: 0.10 for `am`/`amrest`, 0.15 for `pm`). Twice-the-bar is a display convention
    chosen for class balance: measured over 345 non-holiday weekdays it gives 82.0/11.6/6.4 % for the
    morning and 82.9/12.8/4.3 % for the afternoon. Two consequences the page carries rather than
    buries — the matrix diagonal compares two differently-scaled categoricals (forecast red on 13 %
    of days against realized red on 6.4 %), so a well-calibrated red lands in `traff nesten` more
    often than in `traff`, and §14a's expected bad-day rate per level (6/21/55 %) prints beside the
    matrix as the reference.
  - **Each issue is scored against ITS OWN trained window.** `10_train_models.sql` gives 0630 the
    label `y_am10` over 06:30-09:00 and 0700 `y_amrest10` over 07:00-09:00; 1200 and 1400 share
    `y_pm10` over 15:00-18:00. Giving 0700 the whole morning would hand it thirty minutes that had
    already happened when it was issued. Three realized figures are resolved per day (`am`,
    `amrest`, `pm`), the verdict picks the issue's own, and the row shows `amrest` beside `am` so the
    two morning figures cannot silently disagree — **only when they differ** (2026-09-02, product
    owner: two pills on every row read as clutter). Measured over 343 eligible weekdays the 07–09
    level differs from the whole-morning one on 19 (5.5 %): 10 green mornings with a bad 07–09, 8
    yellow with a red 07–09, 1 the other way — so the pill appears about once a month, exactly where
    a 07:00 verdict would otherwise contradict the visible morning level.
  - **The verdict grades what was COMMUNICATED, not what the issue forecast** — a fourteen-branch
    table in `rushhistorikk_verdicts.py`, pure functions, no I/O. A follow-up that confirms its baseline
    sends nothing, yet the traveller holds a warning at the BASELINE's level, and that is what gets
    graded: 12:00 pushes yellow, the 14:00 round is suppressed `untimely` with a red forecast, the
    rush is yellow, and grading the red would give `traff nesten` where the yellow the traveller
    actually holds was exactly right. Without the rule at all, 0700 and 1400 would read `bom` on
    every warned day. `sent` records the POLICY decision, not delivery: `push_round` can fan out to
    zero matched devices.
  - **A standing warning is a yellow or red `baseline_level`, never a truthy one** (2026-09-02).
    `decide_issue` records a `baseline_level` on EVERY follow-up — `"green"` when the baseline
    pushed nothing — and the day table tested `issue.baseline_level &&`, so the ordinary calm weekday
    showed «Sto ved lag» on both follow-up cells with a tooltip claiming a warning at green level
    still stood. The verdict was never wrong (`rushhistorikk_verdicts.standing()` already tests
    yellow/red); the marker was. The day table now has its own `standing()` mirroring it, a grep in
    `test_rushhistorikk` refuses the truthiness form, and every `untimely` round now carries «For
    sent» — a baseline round used to show nothing, leaving a cell excluded from Presisjon with no
    hint of why. The old render stub never showed the defect because it modelled a non-pushing
    baseline as null, unlike the code: a stub payload that does not come through the real writer
    hides exactly the class of bug a render exists to catch.
  - **Three populations, because the figures are not over the same rows.** `scored` (record present,
    day eligible, outcome settled and resolvable, forecast not late, round not untimely) is
    Presisjon's and the matrix's; `delivered` is the same with late and untimely INCLUDED and is
    Dekning's; `recorded` is every issue, and is what `Varsler sendt` counts pushes across — a push
    on a day that never settled still reached a phone. Beside that count the page prints «N
    venter» (`varsler_venter`, 2026-09-02): the pushes among them on days not yet settled, in the
    count and not yet in the matrix — the reason a same-day view's count exceeds its matrix, and
    the first question the page got asked in prd. That split is what keeps a **late**
    forecast out of Presisjon (it saw post-cutoff reality — `03-implementasjon.md` says such
    forecasts "skal utelates fra etterprøving av treffsikkerhet") while keeping it in Dekning, since
    a rush nobody was warned about is a coverage miss whatever contaminated the forecast; dropping it
    from both halves of that ratio would hide exactly the misses. **`late` and `untimely` are
    independent**, and the shortcut that a late forecast never pushed is FALSE: `late` is
    `now > due + 10 min` while `untimely` is `now >= deadline`, and the gap is 30 min for 0630, 120
    for 0700 and 1200, and 240 for 1400. (The all-clear exclusion that lived here until the second
    2026-09-02 redefinition is gone with the warning-in-force reading; the total row still counts
    rushes, for a different reason — see the precision-and-recall bullet below.)
  - **The outcomes pivot is `LOGICAL_OR(scope = 'x' AND settled)`, never `LOGICAL_AND` over an `IF`
    whose else-branch is TRUE.** That second form reports a scope as settled when the day has no row
    for it at all — the else-branch answers TRUE for every *other* scope's row and the AND passes —
    so both afternoon issues read `ukjent`, a settled verdict, instead of `venter` every weekday
    between 12:00 and 18:20, which is exactly the window a reader looks at. Verified against
    BigQuery. `eligible` is the deliberate opposite: bare `LOGICAL_AND(eligible)`, because BigQuery
    skips NULLs and returns NULL when every input is NULL, which is the "no scope has answered yet"
    state `api.ts` tests for. Nothing in this repo executes SQL and the vacuous form dry-runs clean,
    so what guards it is a **grep** of the uncommented text.
  - **Three kinds of "we do not know", kept apart:** `venter` (still being retried), `ukjent` (can no
    longer be established, or the day failed the training chain's day-level eligibility test —
    `am_n > 300 AND pm_n > 300`, which has no `amrest` term, so eligibility is a property of the DAY)
    and `ikke vurdert` (no round was recorded AND none could be reconstructed — the forecast row
    has no `computed_at`, or the policy refused it; rare since 2026-09-02). Folding any two together
    manufactures skill out of missing data. The reconstructions are split on the earliest day a
    round was ever recorded (`first_recorded_day`) and served as `n_reconstructed_after_start`
    beside `n_reconstructed`, with its own chip («Mangler runde»): before that day is the expected
    backlog, after it means the gate was closed or a best-effort write was lost, and without the
    split a write regression is indistinguishable from the backlog during exactly the weeks the
    backlog is large.
    **Strictly after**, because that value is a DAY — on the deploy day itself the issues that fired
    before the restart legitimately have no round, and reading those as lost writes would put one
    false positive per environment permanently on the page.
  - **Varslingstid is measured from each issue's own `NOTIFY_SPECS.scope_start_h`** — 6.5 / 7.0 /
    15.0 / 15.0, identical to each model's label window, so there is no second source to choose
    between. Positive means the push preceded the rush, so 1200 leads by ~180 min and 1400 by ~60
    while 0630 and 0700 land ~1 min INTO their rush. That is a product finding worth seeing rather
    than a number to massage. **The column is no longer on the page** (2026-09-02, product owner):
    it went together with the row's scored count («8 avklarte, kvalifiserte utstedelser») and the
    «(N varsler / situasjoner / rush)» denominators beside the two rates, leaving Tidspunkt,
    Presisjon, Dekning, Varsler sendt and the Treffmatrise. The API still serves `varslingstid_min`,
    `varslingstid_n` and the per-issue `lead_min`, computed exactly as above, so the figure is one
    front-end change away from returning. The question that preceded the drop is worth keeping:
    the 06:30 row read **-1 min** where the reader expected ~+29, because the 06:30 forecast is
    about 06:30–09:00 (its model's window, and the scope the card names) while the product's
    heading says «Morgenrushet 07:00–09:00» — two defensible reference points, and the figure
    follows the first.
  - **The five subscriber-derived columns are never served.** `togvarsel_rounds` holds
    `matched_journeys`, `matched_devices`, `n_sent`, `n_error`, `n_gone` — day-grained counts over a
    subscriber population numbering a handful — and `rushhistorikk_days.sql` does not name them,
    `meldinger_decisions.sql`'s rule that a query which cannot name a column cannot leak it. The
    test greps the UNCOMMENTED SQL, because the file names all five in its header explaining why it
    never selects them. Otherwise nothing here is redacted, and that claim is easy to over-read: a
    rush forecast is one product-wide prognosis over a fixed 39-station geography, with no journey,
    route, device or departure behind a row — the one way this surface is simpler than its two
    siblings.
  - **Its SSO prefixes gate nothing today**, like its siblings'. `sso.PROTECTED_PREFIXES` carries
    both `/rushhistorikk` and `/rushhistorikk-api` (matching is per path segment, so one does not
    claim the other), but `SSO_REQUIRED` is `"false"` in `helm/kix-avvik/values.yaml`, overridden in
    no environment file, and no environment wires the OAuth client — so `sso.required()` returns
    False and the middleware short-circuits. With `trafficType: public` set once and inherited, the
    page ships publicly readable on all three hosts. The prefixes land in the same commit as the
    router, so no deploy ever serves the API with no entry. `test_sso.py` pins the tuple EXACTLY
    (eight entries now) — extend it, never weaken it to a containment check.
  - The window is a four-value allow-list (7 / 14 / 30 / 365, "all time" being a bounded year), not
    a range check: the value lands in the response-cache key on a public endpoint, so arbitrary
    integers would mint one cache entry and one BigQuery query apiece. Four values, four reachable
    keys.
  - Both new SQL files plus `rushhistorikk_days.sql` are registered in
    `data/queries/validate_varsel_sql.sh` — the first togvarsel SQL that script covers; the twelve
    files under `data/queries/togvarsel/` and `togvarsel_features_today.sql` are still unvalidated.
    **The prerequisite is not optional:** both new tables must EXIST in the project, because a dry
    run resolves table references and both are created from Python at startup, so a validation run
    against an environment the app has never started in reports FAILED and the files go unvalidated.
  - **`traff` means the right CLASS was communicated, silence included** (2026-09-02). A green
    forecast on a green rush is a hit. Until then it was its own verdict, `riktig stille` — and since
    green is 82 % of days, and the verdict sat outside Presisjon's numerator while its rows sat
    inside the denominator, «Presisjon (samlet)» read 0 % on a good month by construction, the plan's
    first Open Question. The product owner's answer folded the verdict into `traff` (table rows 11
    and 13), dropped «Presisjon (varslet)», renamed «Presisjon (samlet)» to plain «Presisjon», and
    replaced «Avgjørelser» — four per weekday whether or not anything was sent — with **«Varsler
    sendt»**: pushes issued in the window, friskmeldinger included, one per round, over the RECORDED
    population so a push on an unsettled or ineligible day still counts. That is the
    notification-VOLUME figure the second Open Question asked for. `riktig_stille` is gone from
    `VERDICTS`, `format.ts` and the badge variants alike; `test_rushhistorikk` checks the
    front-end's label KEYS against the module's, not only that each module label is present, so a
    chip nothing can ever show cannot linger.
  - **Presisjon and Dekning are precision and recall over NOTIFICATIONS** (later on 2026-09-02, the
    product owner's second redefinition of the day — the first made Presisjon `traff` over every
    scored issue, silence included, which is an accuracy figure and not the question the table is
    read for). **Presisjon**: of the pushes that went out on scored issues, the share that were
    *justified* — a yellow or red push on a rush that turned yellow or red, whichever of the two
    (`traff` and `traff_nesten` alike), or a friskmelding on a rush that turned green
    (`rushhistorikk_verdicts.justified`). **Dekning**: of the *situations* where a push was owed,
    the share that got a justified one — `due` names a situation as an issue whose rush turned
    yellow or red with no warning standing (a warning was owed) or turned green with one standing (a
    friskmelding was owed). Four consequences worth knowing before reading the table. **Silence
    enters neither rate**: a green forecast on a green day is `traff` in the day table and nothing
    here, so a calm month reads an en dash (`presisjon_den` 0), not 100 % and not 0 %. **A
    confirming follow-up is in neither rate** — nothing sent, nothing owed. The old warning-in-force
    reading kept a correct friskmelding out of Dekning because it retracts rather than warns; now a
    correct friskmelding covers the friskmelding that was owed, and a wrong one on a rush that
    stayed bad is a Presisjon failure only (no situation existed — the traveller already held the
    right warning). **Dekning's total row counts RUSHES, its slot rows ISSUES** (product owner,
    same day): a rush missed at 06:30 and caught at 07:00 reads 100 % on the total row — the
    traveller was told, late, and how late is a Varslingstid question (the API's per-rush figure
    takes the LAST push for exactly this reason; the page no longer shows the column) — while the
    06:30 row keeps the miss and the 07:00 row the hit. So the
    total is NOT the slot rows added up, and a rush missed at both issues is one miss there, two on
    the slot rows. The old code also counted rushes on that row, to avoid crediting one baseline
    push twice through its confirming follow-up; that motive is gone (the follow-up is no
    situation), the aggregation stays for the new one. **Dekning's numerator asks for a JUSTIFIED
    push, not any push**, for one edge: yellow standing, the follow-up escalates to red, the rush
    turns green — a friskmelding was owed and an escalation went out. Populations are unchanged:
    Presisjon over
    `scored` (a late forecast's push stays out — it saw post-cutoff reality), Dekning over
    `delivered` (a late push that reached the traveller covers, an untimely round that owed one and
    sent nothing is a miss). The verdicts, the day table and the matrix did not move; only what the
    two rates count did. Presisjon is therefore even less the matrix's diagonal than before: the
    matrix is over every scored issue by FORECAST level (R16), the rate over the pushes among them.
    API fields: `presisjon`, `presisjon_den` (new), `dekning`, `dekning_den`. The page shows the
    two percentages alone since the same day's third request; the denominators are served, not
    shown.
  - **The Treffmatrise paints LEVELS, not verdicts** (2026-09-02). Each cell is split on the
    diagonal — lower-left half the level we communicated (the row), upper-right half the level the
    rush became (the column), one colour on the diagonal — with the count on top, and the text axis
    labels are gone: the header's «meldt ↓ / ble →» and a legend line under the table carry the
    orientation, and each half sits nearest the axis that names it. The old hit/near/far tints
    borrowed the level hues, so a «far» miss painted red read as a red rush. The colours are Linje's
    `--fill-*-tint` step, one step stronger than the level chips' `--fill-*-muted`, because two
    muted fills do not separate across a diagonal at 24 px; a zero cell is faded rather than greyed
    so the grid keeps its structure without labels. The full rationale is at `.rh-matrix` in
    `demo/rushhistorikk/src/styles.css`.

## Conventions

- All SQL targets BigQuery Standard SQL; legacy SQL is not used.
- Text fields are filtered to Norwegian (`language IS NULL OR UPPER(language) = 'NO'`) — preserve this when adding queries against SX data.
- The two-year partition window (`_PARTITIONTIME >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR)`) is the standard lookback for training queries.
- `disruptions.training_calls` has held **two populations** since 2026-08-19: the (situation x
  call) rows it always had, and one row per rail call **no SX message named** (5.9 M of them, 86 %
  of the table), told apart by `has_situation`. Anything that fits, evaluates or reports on that
  table must say which population it means. `WHERE is_eval = TRUE` used to *mean* "the flagged
  holdout" and no longer does, so a Platt fit that omits `AND has_situation` re-centres production
  alerting onto a base rate 47x lower — no error, no schema change, the alerts just stop firing.
  The rationale, the filler values and their measurements are in the headers of
  `create_training_calls.sql`, `train_call_models.sql` and the two calibration files.
  **Serving carries `has_situation` before the models expect it**, never the other way round:
  `ML.PREDICT` ignores an input column the model does not know, but a model whose contract names a
  column its input lacks fails the whole scoring job, which on `predict_calls.sql` means the
  poller stops producing predictions.
- **`predict_calls.sql` has three branches and they partition the timetable** (2026-08-20):
  `base` (a situation named the call), `nosx` (none did — scored, `has_situation = FALSE`) and
  `et_only` (none did and ET has declared it cancelled — relayed, no model). No call is in two, and
  the cancelled ones are deliberately not scored because `impact._cancelled_prediction` would
  replace every figure with the certainty anyway. The flagged half's calibration is untouched: it
  still reads the same live `ML.WEIGHTS` blocks, while the no-SX half joins the materialized
  `nosx_platt_weights` (45 rows, `gen_nosx_platt_weights.py`). **That table is a snapshot** — a
  Platt refit that does not rebuild it serves last quarter's calibration, silently.
- **A no-SX alert is cut HIGHER than a published one — 0.85, not 0.65** (`alert_policy`), which is
  the opposite of what a 12x-lower base rate suggests. Without a message the only signal is the last
  hour of observation (`own_prev_delay`, `line_now_*`), so the probability decays with the ask
  horizon far faster: measured out of sample, precision at 0.65 is 0.844 for a call inside 45 min,
  0.712 one bucket out, and beyond ~2 h the layer collapses to the base rate and fires nothing at
  all at any threshold. 0.85 holds the near buckets to the bar the flagged family clears at 0.65.
  The second reason is not statistical: this alert reaches the traveller with **no explanation**,
  because there is no operator text to attach — `ingen_melding` in the message input, with an empty
  `situasjoner`, so neither writer can quote the placeholder summary the SQL writes as though an
  operator had said it. Both writers **stated** that absence until 2026-08-20 and now simply do not
  mention it: the caveat ("Ingen melding fra operatøren – varselet bygger på sanntidsdata", and the
  model's own sentence) ran longer than the warning it qualified, so the template heads the body
  with **`Mulig avvik`** and the prompt asks for the disruption alone — while still forbidding an
  invented cause, which is the part that was actually load-bearing. Changed in both writers at once,
  and in `predict_calls.sql`'s `summary` beside them: the card falls back to that summary when no
  wording has been written, and a wording comparison whose two arms disagree about whether to
  mention the absence measures the disagreement rather than the writers.
  `/historikk` segments these as a third provenance beside `predicted`/`relayed`, discriminated on
  the `KIX:NoSituation:` prefix rather than a new column — **adding a column to
  `varsel_decision_outcomes` breaks the registered scheduled query** until it is re-registered.
- Consider cost before running BigQuery operations. Ask for permission before running anything more expensive than $10.
- In docs/kix-docs, references to R1, R2 etc refers to the documents docs/kix-docs/docs/Context/r1-entur-rolle-og-mandat.md, docs/kix-docs/docs/Context/r2-entur-teknisk-deep-dive.md etc.
- All commit messages, PRs (including titles) and code comments should be written in English. Documentation can
  be either English or Norwegian, depending on the intended audience.