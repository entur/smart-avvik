---
paths:
  - "disruptions/api/varsel*"
  - "disruptions/api/test_varsel*"
  - "disruptions/api/{impact,alternatives,llm_message,journey_status,alert_policy,notification_wording,gen_vapid}.py"
  - "disruptions/api/test_{impact,alternatives,llm_message,journey_status,alert_policy,personvern_texts}.py"
  - "disruptions/api/{predict_calls,predict_calls_bus,rail_calls_planned_today,bus_calls_planned_today,rail_calls_today,rail_calls_removed_today}.sql"
  - "data/queries/replay_removed_trains.sh"
  - "demo/avviksvarsel/**"
  - "scripts/{make_device_report,bench_matcher,bench_cycle}.py"
---

# /varsel — Smarte reisevarsler

Moved verbatim from the root `CLAUDE.md` on 2026-09-23, where it was a bullet under
"Deployment". Where the text says "above", "below", "see the … section" or "Conventions", it
means the root `CLAUDE.md` or a sibling file in `.claude/rules/`.

- The **Smarte reisevarsler** PWA at `/varsel` (register a journey → Web Push when a live disruption
  is predicted to hit it; rail + the trained bus operators, per-operator alert thresholds) lives in
  `demo/avviksvarsel/` (React + Vite + Entur Linje). **Renamed on 2026-09-16 from «Avviksvarsel»**,
  which was the traveller-facing name only: the rename moved the displayed strings (masthead, tab
  title, both manifests, the push fallback title, `/varsel/personvern/`, and the places
  `/rushvarsel`, `/historikk` and `/meldinger` name the product to a reader) and nothing else. The
  directory `demo/avviksvarsel/`, the `/varsel` and `/varsel-api` paths, every `varsel_*`
  identifier, the `varsel_decisions` tables and the `disruptions/api` docstrings keep the old word — so
  "Avviksvarsel" surviving in this file or in the code is the internal name, not a missed rename.
  Its
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
  **Since 2026-09-09 that group can also contain a webhook subscription** (KTD3, see the client
  channel's own section below): a webhook recipient is another registration in the same group, so one
  match and one wording fan out to both channels. It is a **view merged at match time, never a
  materialization** — `by_journey_key` stays browser-only because it is the BROWSER grouping, which
  is what says whether a definition has a browser side at all, and `_journeys`, `_notified` and the
  GCS state blob never carry a client id. It was also what `_record_decisions` iterated until
  2026-09-13; since U16 that function walks `_recipient_view` — every definition pass 1a matched,
  with a channel and two counts — and `by_journey_key` is one of its two inputs.
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
  **A PARENT ref expands, it is never aliased** (2026-09-23): Entur's search returns the multimodal
  parent for a station (Lysaker 58856, Oslo S 59872), the app's favourites store it, and a parent
  carries no calls, so it matched nothing and read `in_timetable_today: false`. `place_refs(parent)`
  is now every served child, busiest first (the alternative lookup runs on the first pair only), and
  `canonical_stop(parent)` stays the parent — 154 of dev's 822 served parents hold more than one
  named stop, and every one-child rule dropped a served side (Vy at Nationaltheatret or Oslo lufthavn,
  RUT beside them, the 46 Vy calls at «Oslo S Trelastgata»). The cost is identity: a parent route and
  its child-ref twin are two `journey_key`s, so two matches, two wordings and two decision rows for
  one train. Aliasing the 668 single-group parents would unify those at no coverage cost; not done.
  The parent's registry name reaches the prompt through `main._varsel_stop_names`.
  **The client de-duplicates too, and it must use the SAME key** (2026-09-03): the picker's resolver
  (`demo/avviksvarsel/src/components/stopResolver.ts`) folds ranked matches defensively, and it did
  so on `place_id` alone — so «Nationaltheatret stasjon» (rail, 591 calls) lost to «Nationaltheatret»
  (bus/tram/metro, 1887 calls) under their shared NSR parent, and the picker offered the station as
  bus only; Ski stasjon and Oslo lufthavn stasjon were hidden the same way (three of the 161 places
  prd serves more than one row for). Different names under one parent are separate rows on purpose
  (Oslo S and Jernbanetorget), so the client's key is now (place, name) like the server's, and
  `test/stopResolver.test.mjs` pins it.
  **A registration nothing can ever alert on says so** (2026-08-04): `POST /varsel-api/journeys`
  asks whether any planned trip today runs between the two places (`main._varsel_connectivity` over
  `{rail,bus}_calls_planned_today`, every ref of both places, MIN/MAX per side so a circular route is
  handled). varsel only alerts on a **direct** trip, so a pair no single trip connects yields no alert
  however bad the day gets — and the old symptom was the reassuring "ingen aktive avvik akkurat nå".
  It **never blocks**: the verdict rides on `Journey.warning`, shown on the card in place of that
  line, with a sharper text naming both stops when the pair is served *the other way* (a likely
  swap). Weekend-only and seasonal routes are why a refusal would be wrong, and why the poller
  re-asks every 6 h (`varsel.recheck_service`) and clears the caveat when service appears; a match
  also suppresses it. `None` from the provider means "could not establish" — an outage, a timeout, a
  rate limit — and never becomes a warning. Answers are cached 10 min.
  **That rate limit is TWO numbers since 2026-09-09**, keyed per caller in
  `alternatives._fetch_calls`' shape: `main._CONNECTIVITY_MAX_PER_MIN` (30) for this public
  unauthenticated path, and a second, smaller one for the poller. One shared list was `alternatives`'
  own 2026-08-21 lesson repeated a module over — a client may issue 120 create/replace requests a
  minute, and every one it spent came out of a budget that exists as abuse control for a traveller's
  registration. What a traveller loses when it binds is not an error: the caveat is simply not
  established, so the journey registers without it. The *cache* stays shared, because an answer about
  a stop pair is an answer whoever asked.
  **Since U10 (2026-09-13) the second number is `_POLLER_CONNECTIVITY_MAX_PER_MIN` (4), not
  `_NOTIFICATION_API_CONNECTIVITY_MAX_PER_MIN` (15)**, because the notification API's create and
  replace stopped asking at all — see the webhook section's own paragraph. What is left on that tally
  is the two servability sweeps, each issuing ONE BigQuery job per ~296 s cycle however many records
  are pending, so the worst-case draw from this seam goes 30 + 15 → 30 + 4 per minute. `varsel.
  recheck_service` moved onto it in the same change: it had been spending the traveller-facing 30/min
  by default, which is the same mistake one layer further over. Whose budget a batch spends is bound
  into the provider in `main.lifespan` rather than passed per call — both callers are poller sweeps,
  and `varsel` does not own the vocabulary.
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
  columns. `disruptions/api/varsel_connectivity.sql` asks in the same shape — **four groupings, not
  two**, because the reverse direction boards at the *to*-place and alights at the *from*-place — so
  the caveat above and the alerting cannot disagree; measured against dev's timetable it drops the
  Oslo S → Drammen trip set from 173 journeys to 161, all twelve of them F4. It was an inline string
  in `main.py` and therefore unvalidated until U10 moved it into a file registered in
  `validate_varsel_sql.sh`; nothing in this repo executes SQL, so a query that is not a file is a
  query no dry run can reach. Cost: ~0.68 GB more per planned rebuild
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
  (rail) / **120 min** (bus) because each is an ~8 GB / ~12 GB ET partition scan (the rail one reads yesterday's partition too since 2026-09-21, ~22 GB by mid-afternoon: a train cancelled the DAY BEFORE, and every evening train ET had not touched since publishing it, was otherwise absent — `rail_calls_planned_today.sql` says why), so a cancellation
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
  about the train** («Tog L1 07:18 er innstilt», `varsel._notification_title`; since 2026-09-24
  «Tog RE10 Hamar 07:18» — the destination rides beside the line, from the journey-status spine's
  terminus call via `journey_status.destination`, as `destinasjon` in the message input and
  `destination` on the Alert, TrainStatus, the journey-alerts read's `train`, the webhook payload
  (`payload.destination`) and the alternative's legs (JP front text); null for
  bus and before the spine loads, and the label is then unchanged) and the **body
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
  **A journey never needed notifications to be on, and the page now says so** (2026-09-04). The
  device report (`scripts/make_device_report.py`, which renders every device with its signup and
  journeys — from the prd state blob then, from the STORE since 2026-09-13, because the blob writes
  every non-pushable device as an «expired» tombstone stamped at the export instant and so destroys
  the very split measured here; it also shows `last_seen_at` and the days left on each
  registration's inactivity horizon, which the blob cannot carry) found 79 of prd's 186 registered
  journeys on 41 device ids with no push subscription — and the logs over the days most of them registered showed
  **zero** expired subscriptions and no relevant «Skru av», so they are overwhelmingly devices that
  registered a journey and never turned notifications on (an iPhone opening the link in Safari
  without installing is the likely bulk). Such a journey is not dead: `list_journeys` runs the
  matcher live regardless of the device record, so the page works as a dashboard for it, and the
  poller's first pass matches it too — only the delivery loop skips it. Four things changed, and
  the journeys are deliberately kept. (1) `App.tsx` shows a warning on the journey list when
  journeys exist and `VarselStatus` reports `DeliveryStatus` `action-needed`, linking to the card
  (`#varsel-status`), which knows the right instruction per device; `unknown` while the browser
  subscription is being looked up and `unavailable` where the traveller can do nothing suppress it.
  (2) **Silent repair**: permission `granted` with no browser subscription used to ask for a second
  tap; the reconcile effect now calls `push.subscribePush` itself — gated on
  `components/pushOptOut.ts`, because «Skru av» leaves the permission at `granted` too and the
  repair would otherwise undo it at the next visit. (3) **`sw.js` handles `pushsubscriptionchange`**:
  the page stores device id, brand and the VAPID key as bytes in IndexedDB (`src/pushIdentity.ts`;
  the worker keeps its own copy of the four store names, parity-pinned by
  `test/pushIdentity.test.mjs`), the worker re-subscribes and POSTs the same upsert; «Skru av» and
  «Slett alt» clear the record so the worker never resurrects an ended registration. Browser
  support for the event is uneven and it cannot be triggered on purpose, so it is a net under the
  page-open repair, not a replacement. (4) **`varsel._device_tombstones`** in the state blob:
  `_drop_device` (`expired`) and `delete_device` (`turned_off`) record reason, time, the original
  `created_ts` and the journeys left behind — written only when journeys remain, cleared by
  `upsert_device` (which restores the original signup so a lapse is not a new device), by the
  last journey's deletion and by «Slett alt», which keeps no marker. Rows before that date have
  no tombstone and read as "never turned on" in the report, so the split is meaningful only from
  2026-09-04.
  **Each journey card carries an «I dag» strip since 2026-09-22** (plan
  `docs/plans/2026-09-22-001-feat-journey-status-strip-plan.md`): one circle per Vy train in today's
  window, filled for a fact (the alighting call recorded, or a declared cancellation whose time has
  passed), a ring for a forecast and grey where nothing can be said. It is `Journey.today` on
  `GET /varsel-api/journeys` and the same body on the public `GET /notification-api/v1/journey-status`,
  both from the leaf module `disruptions/api/journey_status.py`, which imports `impact` and
  `alert_policy` only, and gets `varsel.place_refs` by injection. **Three in-memory sources, one
  precedence**: the day's planned timetable (`rail_calls_planned_today`, loaded by
  `main.refresh_planned_spine` in phase `rail_planned_cache` after each hourly rebuild) decides WHICH
  trains exist. A declared cancellation wins, then a recorded alighting call (the spine's own,
  overlaid by `rail_calls_today`, loaded by `main.refresh_rail_actuals` in phase `rail_actuals_cache`
  every 15 min — that table's first live reader; a recorded row outranks a stale cancellation, which
  is how a reinstated train shows), then the prediction cache read per train through
  `impact._lookup_journey_calls` and PAIRED as the matcher pairs a boarding and an alighting call.
  Since 2026-10-22 that means a situation's own alighting row, or else the worst row any situation
  scored there; see the webhook rules' destination paragraph. A figure scored at one end only (the
  other is not in the cache at all) grades yellow or green at most, with no probability, because
  no push can fire on it. `test_the_train_set_agrees_with_the_
  push_matchers_eligibility` holds the restated eligibility rule to `_trip_cells`' own. Two things
  are easy to get backwards. The per-train lookup is
  deliberately NOT `_trip_cells`, which keeps one train per line per situation, so enumerating
  through the matcher would show one circle where one SX message names three trains. And red for a
  forecast is `alert_policy.gate`, the push's own bar, while a no-SX figure more than 120 min out
  is `unknown` rather than green, because that layer collapses to the base rate past ~2 h. A trip is
  cancelled when EITHER end is. Confirmed trips are graded by the DEPARTURE delay at the alighting
  call (a terminus by the last recorded call before it): an arrival-delay column was measured at
  +11 % on the planned rebuild and +58 % on the 15-min actuals scan and left out. Nothing is cached
  and nothing touches BigQuery on a request. The public read spends `journey-alerts`' limiter
  buckets and logs `notification_status`. Vy rail only, today only. **A green forecast reads «ventet
  i rute»** (2026-10-23, `dagensReiseModel.stateText`): the words follow the circle's colour, so a
  typical delay under 5 min is never quoted — a DISPLAY rule only, the served p50 stays the honest
  number. Its price is in the comment beside it (SX trains called on time: 20 % arrive ≥ 5 late).
  **Trains Vy took OUT of the plan are cancellations too** (2026-09-23, plan
  `docs/plans/2026-09-23-001-feat-removed-trains-plan.md`). From 2026-09-19 Vy has been removing
  trains from the day's timetable instead of cancelling them — 188 standing trains on 2026-09-23
  against 5 proper cancellations — and such a train never enters SIRI-ET, so nothing above could see
  it. `disruptions/api/rail_calls_removed_today.sql` is the strike reports' rule: a train number in
  GTFS on ≥ 3 of the 4 previous same weekdays, absent from today's GTFS **and** from ET (either of a
  through train's two numbers, RE20's `382-110`), not stood in for by a new train at its origin
  within 15 min; its calls are its last run's, moved by whole Oslo days (DST). Four things are easy
  to get wrong. The restricted-line rule looks at **trains only** — bus-for-train journeys filed
  under a train line (`BUS-2279_...` under L1) carry their own activities and alone took L1, R12
  and R13 out on the first run — and it can only see a line that RUNS today, so F4 and RX11 are
  also listed by hand (a whole-day F4 closure would otherwise come out bookable everywhere, the
  2026-09-01 incident); persist the detected set before the digest ships. `bus_for_train` is a NUMBER match (`ServiceJourney:BUS-<n>_`), 66 of
  66 on the 2026-09-01 Gjøvikbanen closure. The GTFS partition for a day exists days AHEAD, so the
  missing-partition guard rarely fires, and the build instead FAILS (`ERROR()`) when more than half
  the standing trains read as removed; the poller then rebuilds the table EMPTY, since the one the
  refusal kept was anti-joined against an older ET. Two quiet guards come before it (2026-09-23):
  no rows on a holiday in `disruptions.togvarsel_calendar` or a date it does not cover (Ascension
  Day read 396 removals, under the refusal), and none when more than 25 % of the day's train numbers
  are new against the standing plan, which is a new timetable rather than a cut (cuts ran 2-8 %
  new, Easter and the summer plan's edges 48-66 %). What they cannot tell from a cut is a seasonal
  reduction that keeps its numbers (22-25 June 2026, 57-131 trains), nor a real closure inside a
  timetable swap (the L2 closure of 20 August is hidden). And planned engineering closures ARE
  removals (L2 week of 2026-08-17, R31/RE30, Bergen L4): they truly do not run, and they are included by
  choice (2026-09-23), the morning digest to say when Vy has set up a bus. Built hourly after the planned rebuild (`_phase("rail_removed_calls")`, ~0.3 GB,
  a refusal waits for the next planned rebuild, any other failure is retried next tick), behind `VARSEL_PLAN_REMOVED` (on in
  dev, and in prd since 2026-09-24; off builds the table EMPTY, which is the rollback), created empty at startup
  when absent because `predict_calls.sql` reads it. `data/queries/replay_removed_trains.sh FIRST
  LAST` is the same file over past days (it swaps only the ET CTE for `last_recorded`); on
  2026-09-14..18 it found zero removed trains on registered journeys and 29/67/84/108/159 on the
  cut days 19-23 Sept, against the reports' 39/67/~86/~107/147 — the 19th's gap is R22 trains the
  reports counted that ran in ET. **Two readers, and the model is not one** (KTD1): the relay in
  `predict_calls.sql` (`KIX:PlanRemoved:<jkey>`, «Tatt ut av dagens rutetabell», 1.0 and no figures)
  and the strip's spine (one UNION query, `TrainStatus.removed_from_plan` / `bus_for_train`, labelled
  «tatt ut av rutetabellen» rather than «innstilt» — Vy's own channels show no cancellation).
  **Never pushed per train, never worded by the poller, never a webhook event**
  (`notification_wording.is_plan_removed`, checked in `_pending_pushes`, the write queue's card
  fallback, loop 2's collect and `_observations_for_key`): one push per train was 601 pushes to 92
  registrations on 2026-09-23, and 35 of them would have spent the daily cap on it before 06:00.
  **They get ONE morning digest instead** (U4, 2026-09-23): from 05:00 Oslo (`_digest_due`, on the
  cycle's own clock like `oslo_today`), each browser registration with removed trains still to
  depart in today's window, not all already notified, gets «N tog er tatt ut av rutetabellen i dag»
  (`notification_wording.removed_digest`, deterministic, six named then «og K til», a sentence naming
  the trains Vy has set up a bus for). Its set is the STRIP's (`_removed_trains` →
  `journey_status.status_for`), never the prediction cache's, which at 05:00 reaches only ~17:00; and
  only registrations whose from-place a removed train calls at are asked (`journey_status.
  removed_today`). It is queued in loop 2 ahead of the per-train alerts, so it takes its slot of the
  four first; its tag is `<journey_id>|plan-removed|<oslo day>` (the `<journey_id>|` prefix is what
  the blob ledger finds tags by), so a later digest the same day — a grown set — names every train
  still to come, replaces the first on the lock screen and costs no slot. The tag is recorded as
  departing at the next Oslo midnight, not with its last train: the blob prunes a tag 90 min after
  its departure, so a short morning digest's would be gone before a grown one came to replace it,
  and that one would be charged a slot. A send marks every covered
  situation in ONE write (`mark_notified(jid, *situations)`; per-train transactions were ~40 serial
  Firestore round trips per registration on a cut day), a failure marks nothing, and the push log
  says `push_kind="digest"`. Two traps it had to avoid: the blob's `prune` sweeps marks whose
  situation is not in the prediction cache, so it never sweeps a `KIX:PlanRemoved:` mark dated
  today (`…_<Oslo day>`), whatever the cache says — or a 17:30 train's mark is swept and the digest
  re-sent every cycle. It first kept them by adding the spine's removed trains to `live`, which
  failed the same way whenever the spine was unloaded or a refused build rebuilt the table empty for
  an hour: the marks went, and the digest was re-sent when the trains came back. Earlier days' marks
  go as before, and the store prunes at write time and never met it. The other: the covered trains'
  decision rows record `pushed` with the digest's text as `message`
  (`digest_messages`), since the /historikk card quotes the first pushed row — but only the trains
  already inside the cache have a row that cycle, so one entering it later is recorded unpushed.
  `/impact` and the pull read show them, and the pull read still words its own four soonest as it
  words any cancellation (KTD6; the model then says «innstilt»). The journey card does NOT since
  2026-09-25 (`varsel._card_alerts`): the «I dag» strip already shows each one, and on a cut day a
  box per removed train buried the real disruptions. One cost is not closed: a removed train is
  alert-worthy in `/impact`, so it competes for `impact._MAX_ALTERNATIVE_LOOKUPS` (3, soonest first)
  with the real cancellations beside it. Its decision rows carry `et_cancelled` but are NOT
  `relayed`: since U5 (2026-09-23) `/historikk` and `/meldinger` segment them as `removed`, tested
  ahead of the flag, and the nightly labeller judges them by train number (see
  `.claude/rules/historikk.md`).
  Install steps for phones are in `demo/avviksvarsel/README.md`. Env vars beyond the base deploy:
  - `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` (raw base64url) + `VAPID_SUBJECT` (`mailto:`) — the
    Web Push keypair. When unset, push is disabled gracefully (`/varsel-api/config` reports
    `push_enabled=false`); the journey list still works. Regenerate with
    `uv run --with pywebpush python disruptions/api/gen_vapid.py`.
    **The keypair no longer gates the writing pass** (KTD4, 2026-09-08). `check_and_notify` used to
    skip loop 1c entirely when no keypair was loaded, on the reasoning that writing prose nobody can
    receive is spend with no reader — the reasoning is right and the test was wrong, because it read
    "no keypair" as "no reader" and so a webhook-only deployment, and tst which has no keypair, wrote
    nothing at all. The condition is now **at least one deliverable recipient on any channel**:
    browser push enabled, or a webhook subscription with a pending delivery for that train. The other
    half of the same move is the expensive one — a browser recipient is **not** pending when
    `push_enabled()` is false, because `_pending_pushes` derives pending-ness from `_notified` and
    `_notified` only grows on a successful browser send, so without that clause a keypair-less
    deployment reports every alert-worthy train as pending for every browser registration on every
    cycle and spends the whole write budget re-wording trains it can never deliver. A journey with no
    deliverable channel still writes nothing and still records `message_status='push_disabled'`, so
    that value keeps its meaning for browser subjects.
  - `VARSEL_STATE_BUCKET` — a GCS bucket holding `varsel_state.json` (devices/journeys/notified)
    so registrations survive redeploys (the container fs is ephemeral). The runtime SA needs
    `roles/storage.objectAdmin` on it. Without the bucket, state is local-only (lost on redeploy).
    **Behind a seam since 2026-09-13** (U13 of the 100k plan, first half): `varsel` no longer
    indexes a state dict anywhere — every read and write goes through
    `varsel_state.RegistrationStore`, whose one implementation today is `BlobRegistrations`, the
    same five dicts and the same blob moved across unchanged. The point is the second
    implementation, U12's Firestore collections, and the shape of the protocol is what decides how
    reviewable that one is: it is the CALLER's question, never either backend's primitives, so
    `erase_device(device_id)` is a method while "pop the device, pop its journeys, pop their marks,
    and put them all back if the write failed" is not — the blob does that under one lock, the
    store as a paged transaction, and a seam naming the steps would be the blob wearing a
    protocol's clothes. Three things a later change has to keep. **`save()` is the one asymmetry**
    and it is published rather than hidden: the blob BATCHES, so its mutators change memory and
    `save()` is the write that makes them durable, while the store has already written each
    mutation as its own document and answers True with nothing to flush — so "mutate, then save" is
    one correct sequence on both, and it is what lets the request path stop writing a blob at the
    cutover without a call site moving. **Reads hand out COPIES**, which is why the two in-place
    edits `varsel` used to make to stored records now have methods of their own
    (`set_canonical_stops`, `stamp_service_check`): the blob can hand out a live record and the
    store cannot, so an in-place edit would work on one backend and vanish on the other, silently
    and only in production. And **`apply_state` mutates in place rather than rebinding**, because
    `varsel` aliases the blob's objects for one release so the existing suites read exactly the
    state they always read — a rebind orphans those aliases, and then the module serves pre-load
    state while every test reading through the alias asserts happily against the dict the load
    discarded. `main` passes `registrations=` explicitly (a seam whose only caller is its own
    default is one nobody has to keep honest) and reads no backend flag: which backend is the
    cutover's decision, and `VARSEL_STATE_BACKEND` arrives with the implementation it would select.
    The togvarsel round markers are deliberately **off** the protocol — a rush round marker is not
    a registration, and putting it there would make the store grow a document nothing reads.
    `disruptions/api/test_varsel_state.py` is the seam's own suite (39 checks), and what it exists
    for is the three properties above plus a signature-by-signature comparison of BOTH
    implementations against the protocol: a `Protocol` nothing checks is a comment, and a method
    renamed on one side type-checks in the editor and fails on the poller's thread hours later.
    **The store backend arrived the same day** (`varsel_state.StoreRegistrations` over U12's
    collections, selected by `VARSEL_STATE_BACKEND`; unset and `gcs` are the blob, `store` is
    Firestore). Which read goes where is the design and it is R15 plus one: the REQUEST path reads
    the store, because a traveller who has just created a journey must see it in the next
    `list_journeys` and the mirror is at best as fresh as this cycle's sync — and empty before the
    poller has seeded it; the CYCLE reads the mirror, because at a hundred thousand registrations a
    per-cycle read of the collection IS the cycle. `get_journey` answers both, preferring the mirror
    and falling back to the store while it is not ready, and that is exact in both cases only
    because every commit this process makes is applied to the mirror from its own result.
    **"This process" is the LEADER since the poller lease (2026-10-13,
    `docs/operations/poller-lease.md`).** Every pod in dev and prd serves `/varsel`, so a
    registration, delete or «Slett alt» can land on a pod whose commits the leader's mirror only
    learns at its next sync. Four rules follow, each silent if taken the other way. (1) The mirror is
    the CYCLE's and the leader's alone: reset and seeded synchronously on acquisition (the push marks
    do not stamp the watermark, so a sync would re-offer what an interim leader pushed), reset on
    loss, never seeded on a follower. (2) Request paths read the STORE on every pod, the leader
    included, through `stored_journey` (the delete route's ownership check) and the lists — through
    the mirror, a journey registered on another pod would refuse its own delete on the leader. (3)
    Before each send the leader makes ONE store read (`StoreRegistrations.may_send`, outside
    `_state_lock`) and refuses a journey that is gone or a push whose situations are all already
    marked — and deliberately NOT a train tag marked under a DIFFERENT situation: that is a
    replacement the stale mirror did not recognise (an ET cancellation confirming the interim
    leader's forecast), and refusing it would withhold the confirmation for up to six hours, until
    the mirror re-seeds; sending costs the «Bekreftet:» prefix and one slot of the day's cap. The
    blob backend's `may_send` is always True (one process, tst). (4) A follower publishes the stop
    picker's aliases (`refresh_varsel_stops(heal_registrations=False)`) but never rewrites a stored
    registration onto a canonical stop; that write is the leader's cycle. The wording and
    alternative caches (`_messages`, `_message_meta`, `_alternatives`) are the leader's pass's, and
    every pod serves them since 2026-10-24 (the translation store plan's U7): the leader writes
    what changed after each pass to Firestore's `varsel_card_cache` (`flush_card_writes`, found
    by DIFFING the caches against what was last written, so a mutation site added later cannot be
    missed; a dropped entry is a tombstone), and every pod seeds and syncs it
    (`follow_card_caches`) as it does the translations. A follower never runs the pass or
    `_reconcile_caches`, it only applies; a new leader starts from the stored entries. The
    wording key carries the prediction snapshot, so a new leader's forced first rebuild still
    re-words every alerting train; what the store saves it is the Journey Planner lookups behind
    the alternatives. Per pod, not shared: the failure budgets, the decision ledger,
    and the feedback snapshot cache — the snapshots themselves are `feedback_contexts` documents
    under this backend, bound to the device and deleted in its erase transaction. And
    **`/rushvarsel-api/outlook` reads the device's journeys from the store on every call** (it read
    process memory before), so the rush panel at the top of `/varsel` costs one store query per
    poll and can trigger the weekly activity renewal write, as the journey list already does —
    watch it if the PWA's poll rate rises. **The two
    global ceilings moved off the protocol**: `device_count()`/`journey_count()` are gone and
    `put_device`/`create_journey` raise `varsel_state.CapacityExceeded` instead, because a count the
    caller compares against a constant is two things the store cannot do — the ceiling there is the
    CLIENT's quota, enforced inside the create transaction where U7 put it, and a read-then-compare
    outside it is a race the blob only got away with because it held a lock across both halves. The
    quotas belong to a **reserved `browser` client**, which is not one more onboarded integration:
    the traveller behind those documents is a member of the public, so
    `scripts/manage_notification_client.py` refuses to create it and nothing may issue a credential
    for it — its `clients` document exists only to carry `max_devices` and
    `max_active_subscriptions`, which were `varsel._MAX_DEVICES` and `varsel._MAX_JOURNEYS`.
    `main._registration_store` **fails the lifespan** when `store` is asked for without the database
    or without that account, which is `webhook_worker`'s fail-loud-at-startup rule and matters for
    one number: `quotas_for` answers an unknown client with `DEFAULT_QUOTAS`, whose
    `max_active_subscriptions` is 500, so a deployment that took the default would refuse the 501st
    traveller with «Tjenesten er full» and nothing would say why. Two more things the store half
    needed and U12 had not left behind: the servability answer's **reverse-only qualifier**
    (`servable_reverse`, without which every such traveller silently gets the blunter caveat — which
    reads as the check working), and a **canonicalization body** (`canonicalize_stops`), its own
    bounded write rather than `replace_subscription`, whose version bump means the client changed the
    route and whose semantics would retire the servability answer this rewrite has no business
    touching; it recomputes the definition digest, which covers both stop refs. And the delivery
    loop's collect pass was **re-ordered**: which registrations have an alert-worthy train at all is
    decided first and from memory, and only then is one multi-get issued for their devices — device
    first was one store round trip per registration per cycle, serial, on the poller's thread, for a
    question almost every registration answers "no" to. **A per-journey mirror read is KEYED**
    (`SubscriptionMirror.entry`), which is the same lesson one layer along: `recipients` copies the
    whole entry list and tests liveness on every element — right for the pass, which wants all of
    them, and the wrong shape for `get_journey` (three calls per push) and `push_ledger` (one per
    candidate), where at a hundred thousand entries a thousand pushes is ~4 000 full walks and the
    backend that exists to scale is slower per cycle than the blob it replaces. A suite at n=2 can
    see none of that, so what pins it is a test that makes `recipients` RAISE and asserts every
    per-journey read still answers. **The togvarsel round markers are OFF the protocol and durable under both
    backends**, and the distinction is the point: a rush round marker is not a registration, so it
    has no business on a seam whose every method is a question about one — but "off the protocol"
    never meant "not persisted". U13 shipped them persisted under `gcs` only, which U14 settled
    (2026-09-13) in favour of ONE Firestore document, `notification_store.ROUNDS` — a collection of
    its own, one document of fixed id, no TTL (there is nothing to age out and a TTL could only
    delete the live copy). **The shape that looks simpler is the trap**: keeping the blob as their
    home under `store` means writing through `save_state()`, which serialises the blob GLOBALS —
    empty under this backend — so a marker write would replace the rollback artefact with
    `{"devices": {}, "journeys": {}, …}`; writing only the one key instead is a read-modify-write of
    the whole GCS object racing the six-hourly export, with no generation precondition on either
    side. `varsel_state.configure_rounds` names the home, set by `main` from the same
    `VARSEL_STATE_BACKEND` the registrations follow — they must agree, because a rollback to the
    blob-reading binary has to find the markers where that binary looks. The document's body is
    `{date, areas: {<area>: {<issue>: marker}}}` and **the area level is nested before it carries
    any weight**: Rushvarsel is one geography today (`togvarsel_oslo_stations`, 39 stations) and is
    planned to become several rush areas, so nested from the start that split is a data change here
    rather than a migration, and a build that knows one area reads its own key and ignores the
    rest — which is what makes a rollback ACROSS the split safe. The in-memory map and the injected
    accessor pair stay per-issue; widening them is the split's own work. The markers are
    deliberately NOT part of the cutover import: they are keyed by Oslo date and one filed under an
    earlier date already reads as absent, so by the first midnight after any cutover there would be
    nothing left to have migrated — what a mid-day cutover costs is instead that rounds still inside
    their timeliness window are decided again, which is `togvarsel_notify`'s documented crash window
    and something a restart can already produce. The export writes the LIVE markers into the blob
    rather than carrying the old object's copy through, or the artefact would fossilise the day of
    the cutover and a rollback would read it as the current day's dedup.
    **The cutover is a ONE-SHOT startup import** (`varsel_state.import_blob_into_store`, U14):
    inside `lifespan` before the port binds, so `/varsel` never serves against a half-imported
    store, and every document goes through the same functions the request path uses
    (`create_subscription`, `upsert_device`, `mark_notified`, `tombstone_device`) rather than
    through hand-built documents — a second builder would be a second definition of what a
    registration is, and it would drift the first time `_new_subscription_doc` gained a field. What
    makes it one-shot is a marker document in the new `migrations` collection, read BEFORE the blob
    is opened and written only after the invariants hold, and **that collection deliberately has no
    TTL**: the import creates by document id and never deletes, it runs at every startup and dev
    restarts on every push, so an expired marker would put back every registration a traveller had
    erased since. Re-importing after a rollback is an operator deleting that document, never
    something a restart does. Three failure shapes, each deliberate: an unreadable blob raises with
    the marker unwritten (`read_blob_text` is STRICT where `load_state` is tolerant — "could not
    reach GCS" must never be read as "there was nothing to import"); a blob that does not exist
    marks the migration complete (a fresh environment, and leaving it pending would import a
    restored backup at the next restart); and an invariant that does not hold raises `ImportFailed`
    out of the lifespan with the marker unwritten, so the release rolls back to the binary still
    reading the blob — intact, because the import creates and never deletes. The invariants are
    read back OUT of the store rather than counted on the way in, through `device_journeys`, which
    carries four of them at once: its query filters `client_id` and `channel` and its liveness test
    covers `status` and the horizon. **Two instants, two reasons**: `created_at` is the blob's own
    `created_ts` (the signup cohort the device report groups on, and what `device_journeys` orders
    by), `last_seen_at` is the import instant — the blob records no per-journey last-seen, and
    under the inactivity horizon that is a fresh 180 days rather than a cliff. `create_subscription`
    and `upsert_device` gained a `created_at` override for it, deliberately NOT on
    `SubscriptionSpec`: the spec is what a CLIENT submits, and a caller-chosen creation instant
    there would let a client move the webhook channel's lifetime horizon by submitting a date.
    **The blob is the rollback artefact and `varsel_state_export` is what keeps it one** — the last
    phase of the poller's cycle, at most six-hourly, rewriting the blob from the store. Under this
    backend `save()` is a no-op, so without it the blob freezes at the cutover while still holding
    the journeys of anybody who has pressed «Slett alt» since, and a rollback resurrects them; the
    export's read applies liveness, so an erasure reaches the artefact within one interval and six
    hours is therefore the rollback loss WINDOW. On the poller's tick rather than a CronJob,
    because a second workload would resolve the bucket and the database again per environment and
    the bucket names differ. It writes the blob backend's own five keys through `_journey_record`
    and `_device_record` so `apply_state` can load it verbatim, and carries any UNKNOWN top-level
    key through from the existing blob — the store has no home for one a future build wrote, so a
    document rebuilt from it alone would delete that feature's state from the only place it exists.
    `togvarsel_rounds` is the exception and is written LIVE (`varsel_state.all_rounds`) rather than
    carried: the markers have their own document in the store now, so copying the old object's
    would fossilise the day of the cutover and a rollback would read it as the current day's dedup. Every device
    is exported, including one with no journeys left (`all_devices` pages `device_listing` rather
    than multi-getting the ids the journeys name), because that record is an endpoint, a brand and
    the signup instant `_upsert_device_body` restores.
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
    `VERTEX_CLAUDE_MODEL` / `VERTEX_GEMINI_MODEL` / `VERTEX_GEMINI_FALLBACK_MODEL` /
    `VERTEX_GEMINI_CANCELLATION_FALLBACK_MODEL` — the push text is written by an LLM on Vertex AI
    (`disruptions/api/llm_message.py`) from the enriched `/impact` payload. No API keys: both
    backends use the runtime SA's ADC (`roles/aiplatform.user`, already granted for `AI.CLASSIFY`).
    Every Helm environment pins `VARSEL_LLM_PROVIDER=gemini`; `auto` remains supported by the code
    but is not deployed. Check `jsonPayload.provider` on `event="llm_message"` when diagnosing calls.
    **Every such record carries `ms`, `journey_digest` and `situation_number` since 2026-09-19** —
    on the failures as well as on `ok`, which is where they had been alone. A `504
    DEADLINE_EXCEEDED` from Vertex CAN be this process's own deadline expiring (google-genai turns
    `_TIMEOUT_S` into an `X-Server-Timeout` header) and `499 CANCELLED` is the client half of it, so
    neither means the model is down; Vertex's `model_invocation_latencies` counts the `200`s only
    and can say nothing about either, which is what `ms` exists for. **The first reading through
    that field says it is not always ours** (2026-09-20): the six 504s behind that morning's rule-8
    firing all returned at **5.27-5.29 s**, well inside the 8 s header, against successes of
    1.1-2.8 s in the same batch — so read `ms` before assuming the deadline, and note prd did not
    carry the field until its next deploy.
    **Three in-call attempts since 2026-09-20** (`llm_message._RETRY_OPTIONS`, `attempts: 3` with
    each wait pinned at 1 s by `initial_delay == max_delay` — google-genai reads every retry field
    as `x or <default>`, so a falsy one silently restores the SDK's own, and `jitter: 0` is
    therefore not available): Gemini on Vertex is stateless and served at scale, so a later attempt
    is very likely a different server. Before it there were **none** — `retry_args(None)` is the
    SDK's `stop_after_attempt(1)`, so the tenacity frames in a traceback were a wrapper doing one
    attempt — and the only recovery was the next poller cycle
    (`varsel._MAX_TRANSIENT_ATTEMPTS`), which is too late twice over: the push for that train has
    already gone out on the deterministic template and `_notified` means the better wording never
    reaches that traveller, while the `result="error"` record trips the Grafana rule however well
    the next cycle recovers.
    **`_TIMEOUT_S` came DOWN to 6 s in the same change, and the two only make sense together.** It
    is **per attempt**, so three attempts multiply it: at 8 s the worst case is 26 s of a 30 s pass
    and the pass would admit almost nothing. 6 s clips about one call in a thousand (dev's 81
    successes that day topped out under 6 s; across both projects' 1 776 Gemini calls p99 was 5.0 s
    and the slowest 6.3 s) — and clipping stopped meaning losing a wording, because the retry now
    covers that tail. Neither constant is a fix for the 504s themselves: those returned at ~5.3 s,
    inside the old header.
    What it costs is the reservation every caller on a deadline makes —
    `llm_message._WORST_CASE_CALL_S` is 20 s (3 × 6 s + 2 × 1 s), restated in `varsel` and
    `meldinger` and pinned against it by both suites, which moves the write pass's admission window
    from 22 s to 10 s of its 30 s. **20 s is the ceiling, not a round number**: above it 16 workers
    at ~2.2 s stop fitting `_MAX_LLM_CALLS_PER_CYCLE` (64) into the window and the clock starts
    binding ahead of the count, so a fourth attempt or a slower model needs that arithmetic redone
    rather than the constant nudged. `/meldinger`'s 4-wide replay pass drops from ~40 to ~18 calls
    per request, i.e. a full 200-row page takes ~11 «Hent flere» rounds rather than ~5, each
    re-serving the cached wordings for free; `MELDINGER_REQUEST_DEADLINE_S` is the lever if that
    becomes tiresome. An `ok` record with `ms >= 7000` is a retry that saved a wording, and that is
    how to tell whether this was worth it.
    **The second attempt goes to another model since 2026-10-23** (`VERTEX_GEMINI_FALLBACK_MODEL`,
    default `gemini-3.1-flash-lite`; empty turns it off). That afternoon Vertex EU served 3.8 slowly
    for hours: a one-word prompt still answered, the wording prompt did not finish inside the ~7.4 s
    server cut, and nearly every wording in dev and prd went to the template. Probed with the real
    prompt, 3.8, 3.7 and 3.5 Flash timed out 15 of 15 while 3.1 Flash-Lite answered 15 of 15 in
    1-5 s. `_gemini_write` now makes the attempts itself (the wording's client is built with
    `sdk_retry=False`; `situation_english` keeps the SDK's), so the count, the 1 s wait and
    `_WORST_CASE_CALL_S` did not move -- and note the count is TWO attempts at 9.5 s since
    2026-09-21, not the three at 6 s described above. Three things are easy to get backwards. **A
    predicted cancellation never reaches Flash-Lite** (`_attempt_models`, keyed on
    `innstilling_prosent`): replayed over 100 prd inputs, Flash-Lite quoted that percentage in none
    of 32 and wrote 3 as certain, a miss `_is_grounded` cannot see. Its second attempt goes to
    `VERTEX_GEMINI_CANCELLATION_FALLBACK_MODEL` instead (default `gemini-3.5-flash`, since
    2026-10-26; empty sends it back to 3.8): when 3.8 went down again that day, every wording still
    failing was a predicted cancellation, and 3.5 Flash served all 64 probes and quoted the
    percentage in 33 of 33 replayed. It is not the fallback for everything because it was down
    with 3.8 on 2026-10-23, when Flash-Lite alone answered. **Flash-Lite needs
    `thinking_level: "minimal"`** (`_THINKING_LEVELS`): at "low" it hit `_MAX_TOKENS` on 35 of the
    100. **Only a 404/403 on `_GEMINI_MODEL` latches the backend**; the fallback's own is one failed
    attempt. `llm_model` on the decision row and `model` on the `ok` record name the model that
    wrote the text (`_model_of` reads `_replied_by`), and a retry logs `event="llm_message_retry"`
    with `model`, `next_model` and `error`. A prompt change now has to be replayed on all three models.
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
    credentials). **The winner is the arrival closest to the disrupted train's *aimed* arrival, a
    late minute counting double** (`_LATE_WEIGHT`, 2026-10-08): cost = minutes early + 2 × minutes
    late, among trains not yet departed. Until then any in-time train won however early, and the
    R14 14:04 was offered the SJ 11:46 (144 min early) over the 14:31 (34 min late). **Both Journey
    Planner queries run on every lookup** — `arriveBy` the deadline, and depart-after the train's
    own departure floored to the 30-min cache quantum — so a later train is always in the pool; the
    second is skipped only when the first got no reply, and a pool missing either side reports
    `answered = false`. A late winner is normal, so `minutes_saved` is signed and
    `arrives_before_original` says whether they still make it. A candidate already declared
    cancelled is never offered, and neither is one whose `p_cancel` is at or over the operator's
    alert gate (`likely_cancelled`, 2026-10-13) — **the original included**, so "stay put" is never
    the verdict on a train we expect to be cancelled. Below the gate `p_cancel` is overconfident
    (Vy, 1 Sep-12 Oct 2026: predicted 0.30-0.65, cancelled 2 % without an SX message, 9-14 % with
    one), so no lower bar. Every scored train is ranked on `predicted_arrival_time` (aimed + our
    p50, never earlier than JP's) like the original already was; `risky` only publishes `at_risk`.
    Every alerted Vy train in that period had either `p_cancel` over the gate or a p50 of ≥ 3 min,
    so an alerted original never ranked as on time — but ≥ 3 was the p50's FLOOR, not a finding,
    and the floor went on 2026-10-23 (`predict_calls.sql`, `arr_delay_p50_stack_min`). In prd's
    cache that day the lowest p50 of an alerted train under the cancel gate was 1 min. 0 is
    possible when `p_cancel` sits just under the gate, and that train then ranks on its delay
    alone, which is the reading the gate already gives a `p_cancel` below it.
    The **hard** exclusion (`affected_refs`) is the subject train
    plus cells that are cancelled or over the alert gate — never every cell on the trip: since nosx
    scoring every running train has a cell, and until 2026-10-07 that excluded every in-window
    rival, so the offer fell back to a train *before* the window (R22 17:00 removed, 16:00/16:29
    running, offered the 13:00). The traveller's **own train competes** under the same rules: when
    it wins, `alternative_journey` is null and `original_is_best` is true — distinct from a null
    with `original_is_best` false, which means nothing could be established.
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
  **The matcher scans a PRUNED scope, not the whole cache** (2026-09-12, U2 of the 100k plan).
  `for_trip` used to hand `impact._trip_cells` the entire prediction snapshot — ~1 100 situations
  over ~20 000 scored calls at the midnight peak — and `_trip_cells` rebuilt its two per-situation
  groupings over all of it on **every** scan: once per definition, per window, per stop-ref pair. So
  a cache sized by the *network* was re-walked for a question about two stops, and the cycle's cost
  grew with registrations although its input does not. Measured on a synthetic late-evening cache,
  pruning is **15.7x** cheaper per definition (0.52 ms against 8.2 ms), which is what brings 20 000
  definitions inside R1's budget. **Both of those figures are the MATCH and the wording passes
  only**, and until 2026-09-12 the harness could produce no other: it stubbed `decisions.record` and
  configured no client, so `_record_decisions` returned at its first line and the decision-row build
  was outside every number either benchmark printed. It is inside them now, which moves the
  end-to-end figure and not the speedup's meaning: at 10 000 definitions and the same cache, 0.604 ms
  pruned against 8.164 ms unpruned is **13.5x** end to end, while the matcher alone — the two
  arms less the ~0.083 ms the rows cost in each — is the 15.5x that 15.7x was. Quote 15.7x for what
  the INDEX buys and 0.60-0.65 ms for what a definition COSTS, and read a `decision_rows` of zero on
  a bench row as "this figure went back to being a matcher figure". `main.refresh_call_predictions_cache` now builds a fourth index in
  the same O(calls) ingest loop, `_call_pred_by_stop_ref = {stop_place: [(situationNumber,
  CallPred)]}`, and `impact` keeps a `_Generation` derived from it, identified by **(snapshot object,
  refreshed_at)** so the poller's swap-and-restamp under `_call_pred_lock` invalidates it on both
  counts.
  **The scan is pruned, the matcher is not forked**, and that is the load-bearing choice: `_trip_cells`
  stays the one implementation and is simply handed fewer situations, so byte-identity holds by
  construction rather than by inspection — dicts preserve insertion order, the pruned scope is
  rebuilt in the snapshot's own order, and a dropped situation emits nothing, so the cell list comes
  out element for element the same. A second "pair half" reimplementing eligibility is the
  arrangement this repo has already watched drift apart twice. **Both prune predicates are
  deliberately SUPERSETS** and the two omissions each have a reason. `catchable_after` is *not* in
  the prune: it comes from the caller's clock, so a memo keyed on it would never be shared between
  the poller, the journey list and the pull read, and one ignoring it would show a departed train as
  catchable for up to a cycle — left out, the cut stays exactly where `_trip_cells` already applies
  it, in candidate selection *before* the per-line rollup, which is the ordering trap that docstring
  warns about twice. And `stop_order > bcall.stop_order` is not in the alighting predicate, because
  which boarding candidate wins is a per-call question. A superset can only leave a situation in that
  emits nothing; it can never take out one that would have emitted.
  Three things a later change has to keep. The posting list carries the situation **key** rather than
  reading `CallPred.situation_number` back off the call: production always makes the two agree and
  three suites rely on their being able to differ, which is exactly the assumption worth not encoding
  twice. A suite that refills a snapshot **in place** under an unchanged `refreshed_at` must call
  `impact.invalidate_generation()` — the generation cannot see a mutation it has no way to observe.
  And the guarantee is pinned by a differential test
  (`test_the_pruned_scan_answers_exactly_what_the_full_scan_answers`) that compares `for_trip`
  against the unpruned scan over randomized snapshots x query shapes; it is an **oracle, not a
  regression test**, so it is green by design and its worth is its falsifications — six are recorded
  in its docstring, including one deliberately GREEN case (dropping the operator from the alighting group
  key *loosens* the prune, so the answer is unchanged), which is what proves the superset argument is
  the thing doing the work.
  **The pass beats the heartbeat per chunk** (same unit). `varsel_notify` is one `_phase` and
  `/health`'s staleness rule means "no STEP has finished in `HEALTH_STALE_AFTER_S`" (600 s), so a
  pass that merely got *slow* — a Journey Planner outage holding all 16 match workers at their
  timeout, or a bad morning at 100 000 subscriptions — read as a wedged poller, and the restart that
  follows comes up with both planned-call tables cold. `varsel.configure(beat=...)` takes `main._beat`
  as a callable (`varsel` may import neither `main` nor the tracing wiring) and `_drain` beats every
  `_HEARTBEAT_EVERY` (64) results in passes 1a and 1c. It beats in **submission** order, since that
  is what `ThreadPoolExecutor.map` yields, so a single definition that never returns still blocks —
  which is correct, because a pass wedged on one definition *is* the state `/health` exists to catch.
  **`_match` now passes `cycle_now`** to `match_journey`, so one instant governs the windows, the
  catchability cut and `decided_at` across a pass; it was defaulting to each worker's own wall clock.

  **The poller's two blocking passes run CONCURRENTLY** (2026-08-21), which is what makes a pilot
  ten times the size affordable. `check_and_notify`'s loop 1 is now three passes: **1a** matches
  every journey across `varsel._MATCH_WORKERS` (16) — carrying the Journey Planner lookups with it,
  since those live inside `match_journey`, and, since 2026-09-09, matching webhook subscriptions in
  the same pass as browser registrations grouped on `journey_key`; **1b** reconciles caches and picks what to write for,
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
  stop offering a replacement train. The wait is **poller and pull only** — never the request path
  (an inbound request holding an anyio slot to wait on another request's fetch is how a slow
  upstream becomes a queue; the pull path runs on its own executor, which is why it may wait) —
  and bounded by the fetch's own timeout, and `_release` fires on the fetch's **failure** path too — during a JP
  outage the success path never runs. The write budget (`varsel._WriteBudget`) **reserves** its
  worst case under a lock rather than checking a clock, so the deadline stays a ceiling on when the
  pass *ends*, exactly as it was single-threaded. And the write queue is now sorted **soonest
  departure first, across journeys**: it used to be walked in `_journeys` insertion order, which
  nobody noticed while ~44 registrations rarely reached a cap of 8, but at ten times that the cap
  binds on every busy morning and would hand the whole budget to whichever travellers registered
  earliest, every cycle, forever.
  **One wording and one alternative per (PLACE PAIR, TRAIN), not per definition** (KTD9, 2026-09-12).
  What a notification says is a function of the trip's two ends and of the disrupted train — never of
  the window or weekday set one commuter registered (`llm_message.build_message_input` reads only
  `from_stop`/`to_stop` off the record) — so keying the wording caches on `journey_key` made a
  corridor with five hundred registrations cost five hundred Vertex calls per disrupted train and let
  five hundred travellers be shown five hundred different sentences about one departure. The key is
  `notification_wording.wording_key` — `(pair, situation, departure, cancelled, tomorrow, alternative
  identity, prompt version)`, the shape `notification_pull` already used — and it and
  `alternative_identity` live in that **leaf module** so `varsel` never imports `notification_pull`
  (a plan stop condition, pinned by a source-inspection test both ways).
  **It is TWO key shapes, and collapsing them to one breaks something specific each way.**
  `varsel._message_key` (the wording) carries all seven elements; `varsel._subject_key` — `(pair,
  situation, departure)` — carries `_alternatives`, `_message_attempts` and `_message_errors`. The
  alternative cannot be keyed on its own identity, because a lookup is skipped on most cycles (the
  impact gate, the 6 h horizon, `impact._MAX_ALTERNATIVE_LOOKUPS`, a Journey Planner outage) and
  `_reconcile_caches` deliberately keeps the cached answer through that silence — a read that must
  therefore not name the answer it is looking for. And the two retry budgets must not reset when a
  replacement train shifts by a minute: `_forget_message` says so in as many words, and that is the
  ~720-calls-per-situation hole `_MAX_MESSAGE_ATTEMPTS` closed. `_message_key` therefore reads the
  EFFECTIVE alternative out of `_alternatives` itself rather than off the alert; keying on the raw
  field would make every silent cycle a miss and re-word every alerting train.
  `varsel._decision_key` is the third and is the OLD shape, kept for everything genuinely about one
  registration: `pushed_keys` (two definitions on one corridor differ — different windows, and
  `_notified` is per registration), `_decision_ledger` (KTD15) and the pass-local `message_inputs`
  each row records. **`_MAX_MESSAGES_PER_JOURNEY` (3) is a PAIR's cap now**, spent soonest-departure
  first within the pair, and the queue is still sorted soonest-first across pairs — spending it in
  descending subscription count was considered and refused (a busy train three hours out must not
  beat a quiet one leaving in five minutes), and the comment at the sort records that as an open
  product question rather than a settled one.
  **Which wording POLICY runs is now a question about the pair** (R16): browser as soon as ANY
  browser registration on the corridor contributed the train, `notification_wording.WEBHOOK`
  otherwise. One flag on the queue entry decides three things together — the policy, whether the
  poller's own `llm_message` latch is consulted, and whether the call claims the browser allowance or
  only the clock — because the new key names no definition, and answered per definition a
  client-only route's origin, destination and departure time would reach the prompt/reply DEBUG
  record dev writes. Two tests drive the REAL `write_wording` with only the model call stubbed and
  assert the absence and the presence of that record; the wholesale stub every other webhook test
  uses cannot see it. Two consequences to expect rather than discover: a train written for because a
  SIBLING definition needed it records `ok`/`ok_cached` where it recorded `not_selected`, and the LLM
  audit record is one per (pair, train) carrying the digest of the definition that caused the call,
  so the `journey_digest` join resolves for that one and correctly for none of the others.
  **The Journey Planner fetch was ALREADY shared per pair** and this unit pins it rather than adding
  a memo: `alternatives._cache` keys on (from, to, arrive-by, anchor) with a 10-minute TTL and
  cross-thread single-flight, and the in-time anchor is the train's own aimed arrival, so five
  definitions on one pair spend one lookup (`test_five_definitions_on_one_pair_issue_one_journey_
  planner_lookup`, which drives the real `fetch_candidates` — a stub sits above the cache and would
  count five). What pair-keying ADDS is that a lookup any definition paid for now answers the whole
  corridor through `_alternatives`. What is **not** done: `impact._MAX_ALTERNATIVE_LOOKUPS` is still
  a per-`/impact`-call budget, so with different windows the three trains it covers still differ per
  definition. Making it per (pair, train) per cycle needs a cycle-stable instant in `impact`, which
  does not exist — `for_trip` samples its own `now` per call — so the seam is `main._rebuild_all`
  calling something like `impact.begin_lookup_cycle()`, and `main.py` was outside this unit.
- **SJ Norge trains are served zero-shot by the rail family** (KIX-39, 2026-10-15; measurements in
  `docs/sjn/LEARNINGS.md`). SJN's ET arrives through Bane NOR (`dataSource = 'BNR'`) with lineRef
  `SJN:Line:*` and the legacy ref `<train number>:<date>`, so an SJN jkey is `49:2026-10-15` and its
  operator, read from the lineRef, is `SJN`. `rail_calls_planned_today.sql` takes those lineRefs, and
  the no-SX path, the ET relay and the «I dag» strip serve them unchanged.
  **SJ's own SX are served since KIX-42 (2026-10-24)**: `disruptions_today.sql` reads
  `participantRef IN ('NSB', 'SJN')` and drops a situation whose text carries an unfilled `{…}`
  template field; `predict.sql` (duration model, `/disruptions`) stays NSB; `predict_calls.sql`'s
  `aff` bridges `SJN:DatedServiceJourney:<nr>_…[_M]_<yy-mm-dd>` to `<nr>:<date>` and is DISTINCT,
  because SJ names one train under two refs (the `_M` variant), which also made `num_journeys` count
  SJN trains once rather than twice. Flagged SJN calls alert at `_OPERATOR_ALERT_THRESHOLD["SJN"]`
  = 0.75, not 0.65 (`data/queries/eval_sjn_flagged.sql`): at 0.65 «innstilt» was right 77 % of the
  time, mostly on part-cancelled trains.
  **SJ trains taken out of the timetable ride the removed-trains relay since KIX-43**:
  `rail_calls_removed_today.sql` carries an `operator` through every CTE, and its guards are one row
  per operator, so a Vy timetable change cannot silence SJN and an SJN refusal is quiet instead of
  an `ERROR()` that would keep Vy's previous table too. SJN's F6 and F7 close boarding mid-journey
  and are excluded like Vy's F4 and RX11; SJN's GTFS has no bus trips, so `bus_for_train` is
  Vy-only. The labeller's removed branch reads SJN's train number from `<nr>:<date>` and joins on
  operator; re-register `varsel-label-daily` in both environments after merging.
  Four places keep it from leaking or missing, each easy to undo by accident:
  `predict_calls.sql`'s `netpre` stays Vy-only (training's network is Vy; without the filter 1 110
  of 8 694 Vy rows moved on one snapshot, pinned in `test_impact.py`); Rushvarsel's provisional
  outcomes filter `VYG:%` on both planned-table reads; `alternatives.cache_key` rewrites JP's
  `SJN:DatedServiceJourney:49_OSL-TND_26-10-15` to the ET key, without which a late SJ train is
  offered as its own alternative; and `label_varsel_decisions.sql` reads SJN from `BNR`
  (re-register the scheduled query after changing it). `rail_calls_today` stays Vy-only, so SJN's
  «I dag» facts come from the planned table's recorded calls, up to the planned TTL old.
