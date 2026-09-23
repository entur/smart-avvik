# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`kix-avvik` is an experiment from Entur's Team KI-eksperimentering aimed at proactive disruption handling (Norwegian: "proaktiv avvikshåndtering"). It predicts the duration of public-transport disruptions from SIRI-SX situation messages using BigQuery ML.

## Tooling

- Use uv for Python operations
- Use bq for bigquery operations. For instance, use bq show --schema --format=prettyjson ent-anshar-prd:realtime.siri_realtime_et_production

## Important BigQuery tables

- ent-anshar-prd.realtime.siri_realtime_sx_production: SIRI-SX (Situation Exchange), textual incident and disruption messages
- ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded: SIRI-ET (Estimated Timetable), real-time 
  updates to the planned timetable. Refreshed daily. The last message of a journey, providing a "trip summary". 
  The most-used fields, with one row per stop in a journey. Orefer this for SIRI-ET data.
- ent-anshar-prd.realtime.siri_realtime_et_production: The full SIRI-ET table, large and therefore expensive to query

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
  **Two notification phases exist since 2026-09-08**, `notification_webhooks` and — since 2026-09-09 —
  `notification_servability` after it, and both are present only when the webhook channel's flag is on:
  with it off the keys are *absent* from `poller_cycle.phases`, not zero. `notification_webhooks` runs
  after `varsel_notify`, i.e. after `check_and_notify` has returned, so a raise inside it cannot
  unwind the decision-log write or the state save at all. Two of its worst cases are **ceilings**
  rather than timings and the distinction is worth keeping: a destination hanging forever costs
  `_DELIVERY_WAIT_S` (20.0 s) and a hung store costs `_EMIT_WAIT_S` + `_DELIVERY_WAIT_S`, which
  **U9 moved from 28.0 s to 110.0 s** (2026-09-13) when it widened the emit pass — see the webhook
  section's own paragraph on it. The timings beside them: a hanging destination ~5.0 s (one attempt
  at the 5 s per-attempt ceiling — since claims are sharded per subscription, 30 pending on one
  subscription are one shard and the first failure skips the rest), a healthy backlog of 200 pending
  in 7 ms, emit of 200 subjects in 14 ms **one-wide, which is what that figure was taken at**.
  `notification_servability` is the six-hourly re-ask of a stale or never-established servability
  answer (`varsel.recheck_service`'s webhook copy, which cannot reach these because it walks
  `_journeys`). Its own number is likewise a **ceiling**, and since U10 (2026-09-13) the whole phase
  is inside it: `_DEADLINE_S` (55 s) = one BigQuery question at `_WORST_CASE_CHECK_S` (20 s) plus one
  write pass at `_WRITE_WAIT_S` (30 s) plus the load. It was 30 s **plus one `set_servability`**,
  because the budget reserved the question and not the store write that followed it — a residual
  that was unbounded in principle, since a transactional commit carries no client-side deadline.
  Typical is still ~0: nothing is due on a healthy list. It is
  its own phase rather than folded into `notification_webhooks` for a reason that has outlived its
  original wording: that phase's measured `ms` was the unit KTD6's reversal trigger was stated in,
  and adding this reservation to its ceiling would have fired the trigger on a healthy cycle.
  **KTD6's trigger is no longer 30 s of wall clock** — U9's widened pass exceeds that by design, so
  the operations document restates it as emit's own share being exceeded (`capped` above zero on
  `notification_emit`, or `overlap_refused`), with half the cycle period as the wall-clock bound
  that still means something. Note the period quoted above is **stale**: measured again on
  2026-09-08 it is ~296 s, against which the 110 s ceiling is ~37 % of one cycle and the 28 s one
  was ~9.5 %.
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
- **The internal surfaces are behind Entur SSO** (`disruptions/api/sso.py`; `/historikk`,
  `/meldinger`, `/rushhistorikk` and `/demo`, pages and APIs both — `sso.PROTECTED_PREFIXES` is the
  list, pinned exactly by `test_sso.py`; everything traveller-facing stays public). OIDC
  authorization-code flow against **Auth0's partner tenant** (`partner.<dev|staging|>.entur.org`,
  Entra-federated through the `entur-waad` connection), which is the official route per Team
  Sikkerhet's Golden Path — it replaced a Google-OIDC gate on 2026-09-10, whose OAuth client needed
  `roles/oauthconfig.editor` in our own project, a role the platform's assignable-roles list does
  not carry (tf-gcp-apps PR #724's answer). **The access rule is the organisation claim, never the
  tenant**: that tenant logs in every partner operator's staff too, so `_claims_problem` requires
  `https://entur.io/organisationID == 1` (a JSON number set by the tenant's own action, on the
  id_token only when `profile` is in scope — drop `profile` from `_SCOPE` and every login is refused
  as `no_organisation`). The verifier is PyJWT against the tenant's JWKS, RS256/PS256 only. Per
  environment: `SSO_REQUIRED`, `SSO_AUTH0_DOMAIN`, `SSO_AUTH0_CLIENT_ID` in the configmap and
  `SSO_AUTH0_CLIENT_SECRET` + `SSO_SESSION_SECRET` in Secret Manager as the `sso` ExternalSecret
  group — its own group, because one unresolvable name fails a whole group and `varsel`'s carries
  the VAPID keys. **The Auth0 application is a dashboard resource the tenant owners create by
  hand** on a Slack request (`#talk-sikkerhet` / `#work-tilgangsstyring`, one Regular Web
  Application per tenant environment, callback `https://<host>/auth/callback`, logout
  `https://<host>/`, only `entur-waad` enabled) — a new protected PATH costs nothing, a new HOST
  is another request. Fail-closed: required but missing any of the four serves the gated paths as
  503; rollback is the environment's flag back to `"false"`. The Google route's parked Terraform
  (`oauth_iam.tf`, `var.oauth_config_editors`) was retired the same day, in the PR that also
  un-released the Firestore rules (#143; see the Firestore paragraph in the webhook section for
  the plan-identity gap behind it). Sessions are a 12 h signed cookie
  with no server-side store; `/auth/logout` also ends the Auth0 session (`/v2/logout`, never
  `federated`, which would sign the person out of Microsoft 365). Local runs use
  `http://localhost:8000/auth/callback`, registered on the dev application only.
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
- **Each surface's notes live in `.claude/rules/`, not here** (moved 2026-09-23: this file had
  reached 3 100 lines, all of it loaded into every session). Each rule file has a `paths:` list and
  Claude Code loads it when a matching file is read with the Read tool — **not** when the file is
  read through the shell (`cat`, `sed`, `head`; checked 2026-09-23), and never for work that opens
  no file (logs, BigQuery, a prd question). So **read the file named here before working on that
  surface**, whatever triggered the work:
  - `.claude/rules/varsel.md` — `/varsel` (Smarte reisevarsler): matching through `/impact`, the
    stop picker, boarding/alighting, ET cancellations, alternatives, the «I dag» strip,
    `?brand=none`, the registration store, the poller's match and write passes, and `/varsel`'s env
    vars — VAPID, `VARSEL_STATE_*`, and the LLM switches (`VARSEL_LLM_PROVIDER=off` is the egress
    kill switch).
  - `.claude/rules/notification-webhooks.md` — the webhook channel `/notification-api/v1`: the
    Firestore store and its mirrors, emit, signing, the delivery worker, the public journey-alerts
    read, and their Terraform and Helm.
  - `.claude/rules/historikk.md` — `/historikk` and the decision log (`varsel_decisions`, thinning,
    the nightly labelling scheduled query, the wording-failure budgets).
  - `.claude/rules/meldinger.md` — `/meldinger`, the prompt replay, and its Vertex bounds.
  - `.claude/rules/rushvarsel.md` — Rushvarsel (`/togmelding`), its push rounds, the
    `SHOW_RUSHVARSEL` gate, and `/rushhistorikk`.

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
- All commit messages, PRs (including titles) and code comments should be written in English. Documentation can
  be either English or Norwegian, depending on the intended audience.