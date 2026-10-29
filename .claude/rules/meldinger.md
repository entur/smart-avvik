---
paths:
  - "disruptions/api/meldinger*"
  - "disruptions/api/{test_meldinger,llm_message,test_llm_message}.py"
  - "disruptions/api/historikk_stop_places.sql"
  - "data/queries/validate_varsel_sql.sh"
  - "demo/meldinger/**"
---

# /meldinger — prompt replay

Moved verbatim from the root `CLAUDE.md` on 2026-09-23, where it was a bullet under
"Deployment". Where the text says "above", "below", "see the … section" or "Conventions", it
means the root `CLAUDE.md` or a sibling file in `.claude/rules/`.

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
    (4) **A wording cache keyed on subject + replayed `decided_at` + the row's prompt version**
    (`llm_message.prompt_version_for` of its stored input since 2026-10-28, see the last bullet),
    10 000 entries, process-lifetime for stable results, and only 60 s for a transient failure so a
    red badge does not stick after Vertex recovers. Which failures get which is the same
    cut `varsel` makes with `_message_attempts` vs `_message_errors` (2026-08-22), arrived at
    independently and expressed here as a cache lifetime rather than a counter: `rejected` and
    `ungrounded` remain until eviction or restart because they are payload-determined -- the same stored `message_input` under
    the same prompt version will be refused again, and re-asking spends a Vertex call to learn
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
    ~3 rounds rather than in one — nearer 11 since 2026-09-20, when two in-call Gemini retries took
    the reserved worst case per call from 8 s to 20 s and with it this pass's admission window from
    ~22 s to ~10 s of the 30 — and the button asks for the filters that produced the cards on
    screen, never the current control values. A page **reload** would do the same thing, which is
    what the notice used to advise; it just re-runs the window's BigQuery queries to arrive at the
    same cards.
    A bound that binds is reported per entry and the request still returns 200 — `capped`,
    `rate_limited` and `deadline` beside `no_input`, `unreadable_input`, `disabled` and the statuses
    off `MessageResult` — and `ungrounded`/`rejected` carry the **discarded reply**, since those two
    are what a prompt edit most plausibly produces and a badge with no text defeats the loop. On top
    of all four, `maximum_bytes_billed` is set to 2 GiB on every job — and since 2026-09-12 so is
    `/historikk`'s, from the one shared constant, where it had no cap at all. The RATIO has moved and
    the conclusion has not: 2 GiB was ~11x the whole decision log when it was set (181.6 MB in prd,
    135 MB in dev, 2026-08-21) and is ~1.3x it now (1.53 GB in prd, 2026-09-11), so it still will
    **not** catch a pruning regression — it is a blast-radius bound against a mistyped join reaching a
    ~10 TiB ET table. At ~1.3x it is approaching the point where it becomes both, which is what the
    derived cap under R12 of the 100k plan is for.
    **Measured against prd on 2026-09-13, and what matters is not the ratio but the DEFINITION
    COUNT the pages survive to**: post-thinning the log runs 153.7 rows per definition per day, and
    at 515 B per scanned row `/historikk`'s 14-day default reaches 2 GiB at **~1 700 definitions**
    and its 180-day maximum at **~150** — prd's registration count today, which does not bind yet
    only because the log does not reach back that far. `/meldinger`'s one-day window survives to
    ~5 700. So the FEEDBACK LOOP is the first thing to break on the way to R1's population, at
    roughly 2 % of it: 35 000–80 000 definitions want 41–94 GiB for a 14-day window, 20–47x the cap.
    No interval closes that — KTD15 measured 3.76x at 30 min and 4.96x at 60 against 15 min's
    2.63x, so an hour buys ~3 200 definitions — which is what makes the deferred per-day summary
    table required rather than merely desirable. The arithmetic is on the constant. **The refusal is
    loud but it used to lie**: exceeding the cap raised into the same handler as a missing table and
    answered 503 "it may not have been written to yet" — *empty* when the cause is *too full*. Since
    2026-09-13 `historikk._bytes_cap_refusal` tells the two apart and both pages say which it was.
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
    of `/meldinger` — **in place since 2026-09-10** as the in-process Entur-SSO gate (`sso.py`), not
    the `trafficType` chart change once imagined here, which would have locked the team out too —
    not a shorter SELECT list, and
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
  - **A same-day window is almost entirely `venter`**, because verdicts arrive from the 05:30 Oslo
    labelling job the following morning and /historikk avoids this by defaulting to 14 days on
    `call_date` — which this page cannot do, since its question is about a stretch of a specific day.
    So precision, recall and lead on the default view describe a handful of rows, and `/summary`
    states that as counts (`n_subjects`, `n_labelled`, `n_pending`, `n_unknown`) *before* the rates
    rather than showing confident-looking numbers. The blended-plus-segments rule is /historikk's
    unchanged: `predicted` / `relayed` / `no_sx` / `removed` beside `n_unsegmented`, with `removed`
    resolved first. See `.claude/rules/historikk.md` for why it goes ahead of the `et_cancelled` flag
    (2026-09-23). `/summary` takes the
    **population** filters only (day, time range, operator, line, SIRI-SX summary, from/to place);
    `verdict`, `alerted_only` and `resolved_only` narrow by outcome and never reach it — filtering
    to hits would make precision read 100 % — and `sx_only` is withheld from it on the same
    grounds.
  - **A departure with a change replays under the TRIP brief** (2026-10-28, KTD12 of
    `docs/plans/2026-10-24-001-feat-multi-leg-journeys-plan.md`). A stored input carrying
    `reise_med_bytte` (`llm_message.TRIP_KEY`) is worded under `_TRIP_SYSTEM_PROMPT`, versioned
    `TRIP_PROMPT_VERSION`; every other input keeps the direct brief and `PROMPT_VERSION`, byte for byte.
    `llm_message.prompt_version_for(input)` is the ONE selector — this page's cache key and comparison
    (`meldinger._prompt_version`), the decision row's `prompt_version` and the wording keys all read
    it — so an edit to one brief invalidates only its own rows, and reading `PROMPT_VERSION` directly
    anywhere would file trip rows under the wrong brief with no error. **Owed before prd**: the trip
    brief has not been replayed. It must be, on all three models a wording can come from — 3.8 Flash
    first, then `_attempt_models`' fallback, 3.1 Flash-Lite or, for a predicted cancellation, 3.5 Flash
    — and Flash-Lite most closely, the model already measured weakest on quoting figures. Three
    nynorsk strings also await the owner's review (Q4): «mistar overgangen», «ankomst ukjend» and the
    nynorsk pair in `notification_wording.TRIP_SITUATION_TEXTS`. `/summary` gains a fifth row, «Med
    bytte» (`transfer`), resolved ahead of every other provenance as on `/historikk` (see
    `.claude/rules/historikk.md`).
