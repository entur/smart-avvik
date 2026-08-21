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
node stage builds the `/demo`, `/varsel` and `/historikk` front-ends (from
`demo/reisedemo` + `demo/avviksvarsel` + `demo/varselhistorikk`, which is why the context is the
repo root, not `disruptions/api`), and its python stage assembles the API. CI builds and deploys it via `.github/workflows/{ci,cd}.yml`:

```sh
git push origin main                         # builds the image (ci.yml) and deploys to dev (cd.yml)
gh workflow run cd.yml -f environment=prd    # promote to prd (tst/prd are dispatch-only)
```

- **Single always-on instance** (`deployment.forceReplicas: 1` in the Helm chart): the app runs
  one background poller thread and an in-process cache, so more replicas mean duplicate pollers
  and unshared caches.
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
  `disruptions/api/{demo,varsel,historikk}_static/` (all gitignored). `demo/*/build.sh` do the same locally for dev. **`/eval` is the exception**:
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
    `VARSEL_LLM_PROVIDER=auto` tries **Claude Sonnet 5** and latches to **Gemini** for the process
    lifetime if the project cannot serve Anthropic publisher models — those need Vertex **Model
    Garden enablement on top of the IAM role**, so check `jsonPayload.provider` on
    `event="llm_message"` rather than assuming which one answered. Every failure path (unset,
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
    was *written*, whether or not it was sent. **prd runs the two switches apart**:
    `VARSEL_LLM_PROVIDER=auto` (it writes a wording for every alert-worthy decision) with
    `VARSEL_LLM_ENABLED=false` (travellers still get the template). That is the combination the
    split exists for — it is what makes prd's real journeys comparable at all. Know what it sends:
    the prompt carries the traveller's origin, destination and departure time to a `global`,
    non-region-bound endpoint, which is *not* what the `AI.CLASSIFY` precedent covers
    (operator-authored SX text); decided with that in view on 2026-08-07, and
    `VARSEL_LLM_PROVIDER=off` stops the egress if that changes. Enabling delivery in prd is a
    separate one-value deploy. Prompts and replies are logged under
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
    `capped` / `attempts_exhausted` / `rejected` / `ungrounded` / `error` — because a cap, an
    exhausted retry and a refusal all store the same deterministic text. Two of those values changed
    meaning on 2026-08-07 and the schema descriptions carry the new reading: `llm_disabled` now means
    no backend could be **asked** (`VARSEL_LLM_PROVIDER=off`, no project, everything latched), never
    "this deployment sends the template"; and `attempts_exhausted` means no backend was reachable for
    three cycles rather than "the model was asked and failed" — `_write_message` returns `None`
    without calling anything on several paths and the caller spends an attempt on any `None`
    (measured in prd 2026-08-07, where generation had never run: 753 rows `llm_disabled` and 693
    `attempts_exhausted` across the same 11 subjects).
    **Both wordings are recorded, and `message` is the one that was *delivered*** (2026-08-07):
    `message_llm` and `message_deterministic` sit beside `message`/`message_source`, so the two
    writers can be compared on the same decision — which is impossible while a flag decides that only
    one of them exists. The sent wording is chosen on `delivery_enabled()`, never on "an LLM wording
    exists": `written or deterministic` would file a template push as `llm` on the `/historikk` card
    *and* permanently in `varsel_decision_outcomes`, and would leave "written but withheld"
    (`message_source='deterministic' AND message_llm IS NOT NULL`) matching no row at all — the one
    population prd's rollout is judged on. `message_deterministic` is written on **every** decision,
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
    now be chosen and the matching departures read off. What survives is the **per-row** redaction —
    no decision carries `from_stop`, `to_stop` or `journey_digest`, `Decision` is still
    `extra="forbid"`, and a listing still does not pair an origin with a destination. Stronger
    ground than that means access control on `/historikk`, not a shorter SELECT list.
  - Privacy, in two layers. The *table* is keyed on the journey **definition**
    (`journey_digest`, the same `llm_message.key_digest` used in the LLM audit log, so the two
    join) plus a device count — no `device_id`, no `journey_id`, no time window or weekday pattern.
    The *API* redacts further: `journey_digest`, `from_stop` and `to_stop` are **not** served,
    because `/historikk-api` is unauthenticated on a public host and those three next to
    `departure_time` and `n_devices` would publish "somebody commutes A→B at 07:15". `Decision` is
    `extra="forbid"` so a future SELECT that re-adds a redacted column fails loudly instead of
    quietly publishing it. The page identifies a decision by its **train**, not by whose trip it was.
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
    `bq cp -f` overwrites. **Both environments record pairs, and they differ in what was sent.**
    dev has both switches on, so its rows carry a written wording, a template wording, and the
    written one delivered (`message_source='llm'`). prd generates but does not deliver
    (`VARSEL_LLM_PROVIDER=auto`, `VARSEL_LLM_ENABLED=false`), so its rows carry both wordings with
    the template delivered — `message_source='deterministic' AND message_llm IS NOT NULL`, the
    "written but withheld" population the prd rollout decision is judged on. prd's pairs are the
    ones that matter: real travellers, real journeys.

- The **Reisevalg** endpoint `GET /reisevalg-api/recommendation` (car vs train for the
  Lier/Drammen → Oslo S corridor; feasibility prototype — see `docs/bil-avvik/LEARNINGS.md`)
  lives in `disruptions/api/reisevalg.py`. Road signal: SVV Trafikkdata volumes gated to
  UNKNOWN (measured precision 0.60 < 0.8 bar) + a DATEX II stub behind optional
  `SVV_DATEX_USER`/`SVV_DATEX_PASSWORD` (unset = provider unavailable; when provisioning,
  use Secret Manager like the VAPID keys). Its `disruptions.svv_baselines` lookup loads in
  the poller/lifespan and is deliberately NOT in `copy_runtime_to_prd.sh` — prd serves the
  road side as UNKNOWN until a road provider is validated. Rebuild baselines/backtest with
  `data/queries/reisevalg_*.sql`; raw volume backfill via `data/svv/download_volumes.py`.

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