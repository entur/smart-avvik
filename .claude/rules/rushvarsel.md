---
paths:
  - "disruptions/api/{togvarsel,togmelding,rushhistorikk}*"
  - "disruptions/api/test_{togvarsel,rushhistorikk}*"
  - "disruptions/api/deployment.py"
  - "data/queries/togvarsel/**"
  - "data/queries/{backfill_togvarsel_rounds_from_log.sql,validate_varsel_sql.sh}"
  - "demo/{togmelding,rushhistorikk}/**"
  - "docs/{togvarsel,rushvarsel}/**"
---

# Rushvarsel — /rushvarsel, its push rounds, /rushhistorikk

Moved verbatim from the root `CLAUDE.md` on 2026-09-23, where it was a bullet under
"Deployment". Where the text says "above", "below", "see the … section" or "Conventions", it
means the root `CLAUDE.md` or a sibling file in `.claude/rules/`.

- **Rushvarsel** at `/rushvarsel` (`/rushvarsel-api/today`, `disruptions/api/togvarsel.py`) is
  the rush-hour forecast from `docs/togvarsel/` (§8 of the findings as revised by §14k; the
  implementation is `docs/togvarsel/03-implementasjon.md`). Four weekday issues (06:30, 07:00 for
  the AM rush; 12:00, 14:00 for the PM rush), each a tuned `LOGISTIC_REG` probability on the
  product bar (cancelled OR ≥ 10 min) mapped to green/yellow/red at fixed cuts (0.10/0.30), with
  rule-based reasons and a deterministic "kjente avvik" list. **A reason names only a feature
  that RAISES the probability** (2026-09-03): «større avvik annonsert (buss for tog / stengt
  strekning)» read `sx_big`, which is *negative* in all four live models (−0.36 at 06:30), and
  «manglende togsett meldt» read `sx_mt_0630`, weight +0.09 and no lift over 343 training
  mornings (20.5 % vs 17.3 % bad) — both dropped from the reasons, both still rows of «kjente
  avvik», which is where `02-findings` placed them ("as a known fact, not as a probability").
  **The plan against the standing timetable is a label term and a feature since 2026-09-21**
  (`data/queries/togvarsel/03b_plan.sql` → `togvarsel_plan_cut`; docs/rushvarsel/02–04): Vy
  sometimes takes trains out instead of cancelling them — pulled from ET with no `Cancellation`,
  or dropped from the day's GTFS plan the evening before — and the product owner ruled the
  day-before cut a bad rush. The labels count those planned Oslo calls as cancelled (and read an
  `estimated` time as no observation), `cut_*`/`cut_*_new` are in all four models, linear, and
  `togvarsel_features_today.sql` computes them live from GTFS (~0.4 GB; the day's partition is
  written ~02:06, and the table REQUIRES a partition filter spelled out as literals — a subquery
  does not count). **Serving must carry the columns before the models are promoted**, or
  `ML.PREDICT` fails the whole scoring job; the outcome passes behind /rushhistorikk still grade on
  the old rule and are the next step.
  `togvarsel.refresh()` runs on
  every poll tick (wall-clock — an issue is due at its cutoff whether or not SX moved) and
  computes due issues (one rebuild of `togvarsel_features_today` + one `ML.PREDICT` per issue —
  a handful of BQ jobs per weekday; failures back off per issue and give up after 8 tries/day);
  weekends/holidays get no forecast, and issued forecasts land in `disruptions.togvarsel_issued`
  (MERGE, first write per (d, issue) wins; `late` marks back-fills — per-environment, not copied
  to prd). **Since 2026-09-03 the row also carries `scope` and `reasons`** — the rule-based
  reason sentences exactly as the card and push served them. Until then nothing durable held
  them: they lived in process memory, the feature row they read is `CREATE OR REPLACE`d every
  rebuild, and the only copy was the feedback snapshot, written only when a traveller pressed
  the widget. Rows before that date are NULL in both; the columns reach existing tables via an
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS` behind the same readiness latch as the CREATE. Retrain monthly with `data/queries/togvarsel/retrain.sh` (≈ $1.5; trains candidates,
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
  of `demo/avviksvarsel/src/styles.css`, and `GET /rushvarsel-api/config` labels non-prod in the
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
  link texts on /varsel. Nothing identifying moved — not the modules, `demo/togmelding/`, the
  `disruptions.togvarsel_*` tables and models,
  `TOGVARSEL_NOTIFY_ENABLED`, the `togvarsel_*` log events, or the two data discriminators
  (`display_source='togvarsel'`, `KIX:Togvarsel:<date>:<issue>`) — that last pair for the
  meldinger-rename reason: a new name drops the rows out of every export query that already
  exists. **The paths are the exception: on 2026-10-22 the page moved from `/togmelding` to
  `/rushvarsel` and the API from `/togmelding-api` to `/rushvarsel-api`.** The page path is baked
  into push payloads sitting on lock screens, so `/togmelding` (and anything under it, query kept)
  answers a 301 to `/rushvarsel/` — a route on `togvarsel.router`, so the master gate 404s it with
  the rest of the product. The API has no alias: nothing outside our own two bundles calls it and no
  push names it, so the cost is one deploy's worth of open tabs — an old /varsel bundle polls the
  dead path and shows no rush panel (its ordinary empty state), an old /togmelding bundle shows its
  fetch error, both until reloaded. The HTTP metrics `path` label counts `rushvarsel`,
  `rushvarsel-api` and the redirect's `togmelding`. The page intro no longer opens with «Rushtidsvarsel for lokaltogene …» — the phrase the
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
  too), rate limits, receipts and the data/feedback exporter/reviewer are all varsel's. Context
  rows are discriminated the documented way — `display_source='togvarsel'`,
  `situation_number = KIX:Togvarsel:<date>:<issue>` — never a new column. **The PostHog event is
  its own since 2026-09-29, `rush_alert_feedback`** (`feedback.RUSH_EVENT`), so PostHog's UI and
  dashboards can tell the two pages apart; `feedback._event` picks it from that `display_source`
  on the first submission and the signed receipt carries it into every edit. The exporter queries
  both names, and `test_feedback.py` pins that — a name it did not query would drop one page's
  feedback out of every review, the meldinger-rename lesson. The snapshot quotes the page's literal rendering
  (`togvarsel._LEVEL_TEXT` mirrors the page's `LEVEL_TEXT`, JS-round parity on the percentage) and
  its key set is pinned against the contexts DDL in `test_togvarsel.py` — a key the table lacks
  fails the streamed insert invisibly (the 2026-08-27 mode). Page-side, widget state lives outside
  the DOM keyed by context_id and `load()` skips re-rendering an unchanged payload: fingerprint
  dedup keeps the context ids — and therefore the payload — byte-stable between forecast changes,
  which is what lets a half-typed comment survive the 5-minute poll.

- **Togvarsel push rounds** (`disruptions/api/togvarsel_notify.py`, 2026-08-30) deliver
  Rushvarsel's prognoses as /varsel Web Pushes. Each of the four weekday issues is one
  notification *round*: the baselines (06:30, 12:00) push iff yellow or red (red only under
  `TOGVARSEL_NOTIFY_RED_ONLY`, below); the follow-ups
  (07:00, 14:00) push iff the level *differs* from what their baseline pushed — a level
  transition, never a probability delta (the paired models answer different questions with
  different calibration, so raw p is not comparable across them) — and a green follow-up after a
  pushed warning is the one green that pushes: the friskmelding.
  **Red-only is a flag since 2026-09-28, ON in dev and prd** (`TOGVARSEL_NOTIFY_RED_ONLY`,
  `deployment.rushvarsel_red_only`, allow-list parse, unset = the launch rule;
  `togvarsel_notify.push_levels`). A push then says red or green, never yellow: a yellow baseline
  is suppressed as `yellow_baseline`, a yellow follow-up is `no_change` whether a red stands or
  nothing does, and `/varsel-api/config`'s `togvarsel_red_only` words the «Nytt: Rushvarsel»
  notice. It was first a code change, live from 2026-09-02 ~23:45 Oslo to 2026-09-03 (PR #130),
  reverted the next morning at the product owner's request. `docs/togvarsel/04-presisjon-2026-09-02.md`
  is the measurement that motivated it and it still stands: a yellow forecast (p 0.10–0.30) is
  calibrated at 15–23 % bad days and is ~70 % of pushes, so under this rule /rushhistorikk's
  Presisjon reads ~25 % on every window by construction, while red-only would read ~38 % at
  0.46 pushes a day, warning 43 % of bad rushes instead of 91 %. In its one live weekday it
  changed exactly one decision per environment: the 2026-09-03 07:00 round (red pushed at 06:30,
  yellow at 07:00) was recorded `no_change` where the launch rule pushes «Oppdatert rushvarsel».
  `rushhistorikk.reconstruct_rounds` replays the LIVE policy, so a reconstruction follows the flag
  as it is set NOW, past days included — the gap 2026-09-08 to 2026-09-28, when the master gate
  recorded nothing, reads as red-only. Recorded rounds keep the rule they were decided under.
  Wording is deterministic
  Norwegian from the issue's own fields (no LLM), titled `Rushvarsel`/`Oppdatert
  rushvarsel`/`Friskmelding` so a red morning's SX push and forecast push stay tellable apart;
  entur-brand pushes click through to `/rushvarsel`. Delivery is gated by
  `TOGVARSEL_NOTIFY_ENABLED` (allow-list parse; **unset = off**; helm base off, dev on since
  launch, prd on since 2026-08-30, tst off), and while off nothing is evaluated and no state
  written, so an enable-restart inside a window still delivers.
  **`SHOW_RUSHVARSEL` is the master gate over the whole product** (`deployment.show_rushvarsel`,
  allow-list parse, **unset = off**; base helm value `"false"`). It was off everywhere from
  2026-09-08 (not ready to be seen) and is ON in dev and prd since 2026-09-28, with red-only;
  tst inherits the base. While it is off: no rush-forecast push is sent (`main.
  _togvarsel_notify_enabled` ANDs the two flags, and that one function is the seam for all THREE
  ways rushvarsel reaches a traveller, so it also empties `/varsel-api/config`'s
  `togvarsel_notify` and with it the PWA's «Nytt: Rushvarsel» notice and its `RushOutlook` panel);
  `/rushvarsel` is never mounted and all of `/rushvarsel-api` 404s. That last one is closed
  TWICE, and the two halves close different things: `main` skips `include_router` for both
  routers, which is what keeps the endpoint names out of the OpenAPI schema behind FastAPI's
  default `/docs` — a listing is itself a mention — while each module's own router-level
  `deployment.require_rushvarsel` dependency is what holds the gate PER REQUEST wherever the
  router is mounted, including the two test suites' own apps and the dev-only
  `POST /rushvarsel-api/test-notify`, which is deliberately not wired to the `enabled` seam and
  would otherwise still send a real push. Three things the gate deliberately does not do. It does **not replace**
  `TOGVARSEL_NOTIFY_ENABLED` — dev's and prd's `"true"` stay exactly where they were, so turning
  the product back on is this one value per environment and nothing else to re-derive. It does
  **not** stop the forecast: `togvarsel.refresh()` and `togvarsel_outcomes.refresh()` keep issuing
  and resolving on the poller tick, because /rushhistorikk is how the model earns its way back on,
  and the BigQuery cost is unchanged. And it is read at call time everywhere except the static
  mount, which is import-time by necessity — a `Mount` answers the bare `/rushvarsel` with a 307
  to the trailing slash before any per-request check could 404 it, so the page is gated by not
  being mounted at all. One consequence to expect on /rushhistorikk: with no round recorded,
  every issue is RECONSTRUCTED, so the «Mangler runde» chip (`n_reconstructed_after_start`) fires
  on every day the gate is closed — that is its documented "the gate was closed" cause, not a lost
  write.
  Turning it on in an environment
  needs more than the flag: the 39-station geography `disruptions.togvarsel_oslo_stations`
  (fetched once per Oslo day, DEFINES matching, never duplicated in Python) exists on prd only
  after `copy_runtime_to_prd.sh` — and existing subscribers opted into train-specific alerts, so
  the /varsel page announces the new push class ("Nytt: rushvarsel", shown only where /config
  reports `togvarsel_notify: true`, which follows this same flag via a provider injected in
  `varsel.configure`). State is one primitive: round markers
  `{date: {issue: {level, pushed}}}` in the varsel state blob — dedup (a marked round never
  re-fires, suppressions included) and the follow-up baseline in one; markers commit in memory
  before fan-out and persist after, so a crash mid-round inside the timeliness window can
  duplicate that one round — accepted. Only FINISHED rounds' markers are persisted: a round the
  poller lease fenced (asked before the fan-out and before every send, `RoundFenced`) saves no
  marker and ends the pass, so the next leader re-pushes it — duplicates for devices already
  sent, never a lost round (KTD8 of the zero-downtime plan). Timeliness is strict Oslo wall-clock at evaluation time
  (0630 < 07:00, 0700 < 09:00, 1200 < 14:00, 1400 < 18:00; a late back-fill is recorded as
  suppressed and never revisited), and matching is additionally bounded by evaluation time: a
  journey whose registered window has already passed never gets the push, friskmelding included.
  Fatigue is the known cost: yellow-or-red fires on ≈56 % of weekday mornings (measured level
  bins), the alert-noise shape this repo has rejected before — shipped as requested, mitigated
  (the change gate caps a rush at two pushes, reasons ride in the body, delivery started dev-only;
  red-only is the flag above, and per-user levels are the remaining documented follow-up). Audit is structured logs (one
  `togvarsel_notify_round` event per round with decision and sent/error/gone counts) +
  `disruptions.togvarsel_issued` + the persisted markers — deliberately **no `varsel_decisions`
  rows** in v1: the decision is a deterministic function of durable inputs, and adding columns to
  the decisions/outcomes tables is a documented trap (a new `varsel_decision_outcomes` column
  breaks the registered scheduled query — see Conventions below); if rows come later, discriminate on
  a `KIX:Togvarsel:<date>:<issue>` situationNumber prefix, never a new column.
  **The levels also sit at the top of `/varsel`** (2026-09-01):
  `GET /rushvarsel-api/outlook?device_id=…` (`togvarsel_notify.rush_outlook`) answers with the
  rushes THIS device is notified about that are not yet over, and `demo/avviksvarsel`'s
  `RushOutlook` card renders them with a link to `/rushvarsel` — nothing at all when the list is
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
  both gates, and so does `main._rush_hydrate`, which warms the cache on every pod (zero-downtime
  U4) rather than on the leader alone. Since U4 the device's journeys come from the registration
  STORE (`varsel.device_journeys`, the follower's mirror is never seeded), so the route is a plain
  `def` like `test-notify`, and the forecasts every pod serves are hydrated from
  `togvarsel_issued` (`togvarsel.load_issued`; `refresh` stays the leader's). The two writes in `station_set` publish `refs` before `date` so the lock-free
  read cannot see a date whose refs have not landed. Dev-only test
  trigger: `POST /rushvarsel-api/test-notify` with `{"device_id": ...}` (same shape as
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
    `push_round`, which `POST /rushvarsel-api/test-notify` reaches directly on the public dev host
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
    **Both passes grade on the 2026-09-21 label rule** (docs/rushvarsel/04-cuts): the planned
    Oslo-area calls of trains that never ran — absent from ET on a line that ran, or cut from the
    day's plan against the standing timetable and not already cut the previous weekday — count as
    cancelled on both sides, and the settled pass also reads an `estimated` time as no observation
    and a silent journey's tail as cancelled (the provisional pass cannot: no snapshot time on the
    planned table, its fifth named skew). The plan terms are computed INLINE from GTFS for the
    span, not read from `togvarsel_plan_cut` (rebuilt monthly), which is why the settled query
    reads ~2.4 GB per 14-day chunk and `_MAX_BYTES_BILLED` is 6 GiB. **A settled row is never
    rewritten**, so a rule change leaves history graded on the old rule until
    `data/queries/togvarsel/regrade_outcomes.py` re-opens a range and re-settles it through the
    module's own path — run per environment, after the code carrying the rule is what you want.
  - **The authoritative pass's latch is keyed on the UTC DATE, and mixing the two calendars once
    cost the whole page.** Its gate is a UTC hour (04:00) because the batch it waits for was measured at
    a UTC hour (~03:04, 2026-09-01; it is really ~05:03 Oslo, so ~04:03 UTC and after this gate in
    winter), while everything else in `togvarsel_outcomes` is scoped to the **Oslo** day because a
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
  - **Its SSO prefixes gate it since 2026-09-10**, like its siblings'. `sso.PROTECTED_PREFIXES`
    carries both `/rushhistorikk` and `/rushhistorikk-api` (matching is per path segment, so one
    does not claim the other), and every environment file sets `SSO_REQUIRED: "true"` with its
    Auth0 partner-tenant client (see the SSO section under Deployment). Until then the base
    `"false"` held everywhere and the page shipped publicly readable on all three hosts —
    `trafficType: public` is set once and inherited, so the gate in-process is the only one there
    is. The prefixes land in the same commit as the router, so no deploy ever serves the API with
    no entry. `test_sso.py` pins the tuple EXACTLY (eight entries now) — extend it, never weaken
    it to a containment check.
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
  - **Two views under the stats table judge the MODEL, not the push rules** (2026-09-28; they
    replaced the weekly Presisjon/Dekning trend chart). «Når vi sa grønt, gult, rødt»
    (`calibration`) bins every scored issue by forecast level and counts the rushes that turned
    yellow or red, beside the mean p quoted and §14a's long-run rate. It exists because a red
    forecast means "about a coin flip", and the matrix's red/red cell was read as the model's hit
    rate. «Ble du varslet – og hva kostet det?» (`coverage`) counts RUSHES by their scored baseline
    issue (06:30, 12:00): a bad rush is warned first (a warning push at the baseline), late (only at
    the follow-up) or missed, and a calm one is warned or quiet. It exists because Dekning folds the
    owed friskmeldinger in (the 2026-09-25 audit: 9 of 9 bad rushes warned, yet Dekning 33 %).
    "Warned" is a warning push as the round decided it (`sent`), not the forecast level: under
    red-only a yellow nobody was sent is no warning, and the yellow pushes recorded before the flag
    still are.
