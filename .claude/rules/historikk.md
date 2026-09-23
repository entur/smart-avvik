---
paths:
  - "disruptions/api/historikk*"
  - "disruptions/api/{test_historikk,decisions,test_decisions,test_decisions_freshness}.py"
  - "disruptions/api/{varsel,test_varsel,test_varsel_api}.py"
  - "disruptions/api/create_varsel_decisions.sql"
  - "data/queries/{label_varsel_decisions.sql,label_varsel_decisions.sh,backfill_outcome_pushed_message.sql,message_wording_comparison.sql,validate_varsel_sql.sh}"
  - "demo/varselhistorikk/**"
---

# /historikk — Varselhistorikk and the decision log

Moved verbatim from the root `CLAUDE.md` on 2026-09-23, where it was a bullet under
"Deployment". Where the text says "above", "below", "see the … section" or "Conventions", it
means the root `CLAUDE.md` or a sibling file in `.claude/rules/`.

- The **Varselhistorikk** page at `/historikk` (`demo/varselhistorikk/`, React + Vite + Entur
  Linje) is the feedback loop for alerting: every decision Avviksvarsel took about a registered
  journey, the message that was sent, and — once the journey has run — a hit / false alarm / miss /
  correct-reject verdict. Its API is `/historikk-api` (`disruptions/api/historikk.py`).
  - Non-prod deployments label the heading and tab title (`Varselhistorikk (dev)`) from
    `GET /historikk-api/config`, sharing `deployment.environment_label()` with `/varsel`. It is its
    own endpoint, not a field on `/summary`, because both data endpoints 503 when the decision log
    cannot be read — precisely when "is this dev or prd?" matters — and it touches no BigQuery.
  - **Every decision is logged, not every push.** `varsel.check_and_notify` appends rows keyed on
    (journey definition x situation x departure) to `disruptions.varsel_decisions`
    via `disruptions/api/decisions.py`, *including* matches that stayed below the alert threshold.
    Without those, a `miss` leaves no trace and only precision is measurable.
    **Both channels since 2026-09-13** (U16, KTD4): a row now carries `channel` ('browser',
    'webhook' or 'both') and `n_subscriptions` beside `n_devices`, and both pages have a Kanal
    filter — a POPULATION filter, so it reaches `/summary` like operator, line and the two places
    and unlike `verdict`/`alerted_only`/`resolved_only`. The vocabulary is a FIXED three-value set
    rather than one read from the window: three values is a bounded response-cache key, and "no
    client route in this window" has to be an empty page rather than a 400, which is also why it is
    the one selection the page never reconciles when the window moves. A named channel EXCLUDES
    rows whose own channel is NULL — written before that date — rather than folding them into
    'browser', which is `n_unsegmented`'s third-bucket rule one column over. Read `pushed_any` with
    it: it means a notification was ISSUED on either channel, so a 'both' subject can be true with
    no Web Push behind it, and the client half is the event MINT rather than its delivery. The
    grain did not move and no client identity is anywhere in the table — see the webhook section's
    KTD10 paragraph. Repeats across cycles
    are intentional — they are the lead-time series. Writing fails open: a BigQuery problem never
    blocks a push.
    **Not one row per cycle since 2026-09-12** (KTD15 of the 100k plan): it was from the day the
    table existed, and what stopped holding is the arithmetic, not the reasoning — measured in prd
    2026-09-11, ~400 rows per definition per day with 88-95 % of them sub-threshold no-SX subjects
    re-scored every ~295 s with nothing having moved, which at 35 000-80 000 definitions is
    14-33 GB a **day** and a 14-day `/historikk` window of 70-150 GB per request. `varsel.
    _thinning_reason` now writes a standing subject at most once per 15 min, **plus** every row a
    figure the pages compute is made of: the first sighting, every alert-worthy/sub-threshold
    transition, every new maximum impact probability, every push, and a **closing row** carrying the
    subject's last observation — written from the stale sweep when the situation ends and from the
    match when a train leaves it uncatchable, and dated at the cycle it *describes*, so `final_*`
    and `last_decided_at` mean what they always did. `first_alert_at`, `max_impact_probability`,
    `et_cancelled`'s first-alerting-cycle pick and the quoted wording are therefore unchanged **by
    construction**; `n_decisions` changes meaning (rows written, not cycles scored) and its
    description says so, so **no per-cycle rate may be computed across that date**. The ledger is
    its own module structure, not a field on `_message_meta` — `_forget_message` drops that the tick
    a subject eases below the gate, which is exactly when the rule needs the prior class — and it
    joins the stale sweep and `test_varsel_api._reset_state` like every other module budget.
    **Measured ratio, replayed over prd's own rows** (two days to 2026-09-10, 168 436 rows /
    6 462 subjects): **2.63x**, 38.1 % of rows kept — 6 462 first sightings, 13 502 new maxima,
    40 881 interval rows, 3 153 closing rows, 83 transitions, 44 pushes. So 15 min alone is **not**
    enough at 100 000, which is what gates the deferred per-day summary table; a longer interval is
    not the answer either (30 min buys 3.76x, 60 min 4.96x, and past an hour the interval starts
    deciding which cycle a reader may see).
    `decisions.record` also **chunks** its insert under 10 000 rows and 5 MB per request (half the
    streaming API's documented 50 000 / 10 MB): one refused request used to lose a whole cycle's
    decisions, and at 100 000 a cycle is 30 000+ rows. A failed chunk costs its own rows only, and
    every branch of the write — including the exception path, which did not until then — carries
    `event="varsel_decision_write"` with its chunk index.
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
    **With that ALTER in place the very same line can still appear for ~10 minutes after a deploy,
    and telling the two apart is the whole trap** (2026-09-13, prd, U16's `channel` /
    `n_subscriptions`). `insertAll` is served by several backends that refresh their cached table
    schema independently, so for a few minutes after an `ADD COLUMN` a request can land on one that
    has not. Measured: the startup ALTER ran at 18:29:32, the new pod's **first** write at 18:30:39
    carried both new columns and was accepted, as were 18:32 and 18:34 — then 18:36:06 rejected 9 of
    9 rows with `no such field: channel`, and 18:38:56 onwards was clean. So it is not a cold cache
    at startup, and a deployment cannot dodge it by sitting out a cycle before its first write; dev,
    deployed ninety minutes earlier from the same commit, never saw it at all. **The discriminator is
    whether it STOPS** — a missing ALTER fires every cycle forever, this fires once or twice inside
    ten minutes of a deploy and goes quiet — so read a couple of hours of
    `jsonPayload.event="varsel_decision_write"` before touching DDL that is probably already correct.
    Note that field is `jsonPayload.event` and **not** `jsonPayload.fields.event`, which matches
    nothing here and so reads as "no errors". Nothing is lost on either cause:
    `varsel._record_decisions` rolls the thinning ledger back on any SHORT write and re-offers the
    whole cycle — "duplicate rather than drop", which is what `result="partial"` beside
    `result="rolled_back"` on one cycle *is*.
    **That file is two halves and `ensure_table` runs them as separate jobs** (2026-08-07): structure
    above the `-- BEST-EFFORT SECTION` marker decides readiness, the `ALTER COLUMN … SET OPTIONS`
    description refresh below it is best-effort and a failure there is logged and forgiven. BigQuery
    caps table-metadata updates at **5 per 10 s per table** and that block only ever grows — every
    reworded description adds a permanent statement. At seven of them it reliably returned "Exceeded
    rate limits: too many table update operations for this table", and since `ensure_table` treats
    any failure as "table not ready", one whole-file job meant a *cosmetic description edit* silently
    stopped the decision log recording anything at all. Prune statements whose text has demonstrably
    reached both dev and prd; a fresh environment reads its descriptions from the `CREATE` bodies.
    **That prune was finally done on 2026-09-12**, verified with `bq show --schema` in both projects
    rather than assumed: all eight statements carried text byte-identical to what dev and prd already
    held *and* to the `CREATE`/`ADD COLUMN` body beside them, so the block could never complete
    inside the cap and was spending three statements per startup failing. It is now three on
    `varsel_decisions` and one on `varsel_decision_outcomes` — **prune again before adding a fifth to
    either.** The block also carries the one thing there that is not a description: the
    `partition_expiration_days=365` `SET OPTIONS` that gives the log a year's retention (the `CREATE`
    body covers a fresh environment). It sits below the marker deliberately — a retention rule that
    lands a poller tick late costs nothing, while a metadata rate limit above the marker is the
    2026-08-07 outage — and **it deletes data** once it lands.
    **The grouping body is a TABLE FUNCTION since 2026-09-12**,
    `disruptions.varsel_decision_groups_between(d_start DATE, d_end DATE)`, with
    `varsel_decision_groups` kept as a **windowless view over it**. A BigQuery view takes no
    parameters, and the table is `PARTITION BY DATE(decided_at)` while every reader bounds
    `call_date`, so through the view each `/historikk` request scanned the whole log — affordable at
    1.5 GB and not at the volume R12 sizes for. The three `/historikk` queries now call the function
    with a **derived cover** of their `call_date` window ± 1 day (a decision is made from 720 min
    before its departure to 54 min after, and `call_date` is the Oslo date of that departure), which
    fails **silently and cheaply** if it is ever too narrow — re-measure before widening the 12 h
    horizon. The view keeps its **exact column set and GROUP BY**, because `label_varsel_decisions.
    sql` does `SELECT *` over that name and the registered scheduled query holds a copy in each
    environment. Six readers, not four: the three `/historikk` queries, the nightly job,
    `backfill_outcome_pushed_message.sql` and `message_wording_comparison.sql`. **The function must
    exist in a project before `validate_varsel_sql.sh` can dry-run anything that names it** (the app
    creates it at startup), exactly like the two togvarsel tables. And `historikk._MAX_BYTES_BILLED`
    is now the **one** cap for both pages — `meldinger` imports it — with the derivation (four times
    the measured pruned, thinned 14-day scan) written out there. **Half that measurement exists
    since 2026-09-13 and the value still did not move**, which is now a decision rather than a gap:
    prd's thinned 14-day scan is ~190 MB, so the derived cap would be ~760 MB — *below* the
    placeholder — and adopting it would refuse the 180-day window sooner, which is the failure the
    constant exists to prevent. The half still owed is the dev harness at 10 000 definitions.
    Note
    `validate_varsel_sql.sh` checks that file **statement by statement**: a dry run of a
    multi-statement script validates only the first one (`numChildJobs: 1`), so a broken ALTER
    would otherwise pass the validator and fail at startup. The outcomes table is created there too, not by the nightly
    job: `/historikk` queries it on every request and would 503 until the first nightly run
    otherwise. The view holds the grouping shared by the nightly job and the API, so the two cannot
    drift apart.
  - Outcomes come from `data/queries/label_varsel_decisions.sql` (scheduled query
    `varsel-label-daily`, 04:00 UTC, ~$0.015/run measured (2.30 GB, 2026-09-23); **registered in both dev and prd** —
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
  - **Removed trains are labelled by train number, and only where the line ran** (2026-09-23, U5 /
    KTD9 of `docs/plans/2026-09-23-001-feat-removed-trains-plan.md`). A `KIX:PlanRemoved:` subject
    has none of the existing join keys: its jkey is `RemovedJourney:<n>_<operating date>`, its
    `stop_order` is GTFS `stop_sequence` and its `stop_place` the boarding parent, so the
    `(operator, jkey, stop_order)` join matched nothing and every such row stayed NULL.
    `label_varsel_decisions.sql` sends them (on the `situation_number` prefix, the one the `removed`
    segment uses) to a branch of their own. It looks the number up in `last_recorded` on the jkey's
    operating date, under either of a through train's two numbers as `rail_calls_removed_today.sql`
    matches them, and finds the boarding call by line code, number and parent stop place (the quay
    through `quays_last_version`; when two ET journeys share that key, an ExtraJourney beside
    the train, the one whose aimed departure is nearest the subject's is used). TRUE when ET ran the line's trains that day and has no journey
    with that number; TRUE/FALSE on the boarding call's cancellation or ≥ 10 min delay when the
    train ran after all; NULL otherwise, and above all when ET did not carry the line. Lines compare
    on the code (`last_recorded` sometimes files `L1` bare), and "the line ran" counts
    `vehicleMode = 'rail'` only, so a line carried by replacement buses alone labels NULL, not a
    hit. Replayed over 2026-09-19..22 (one synthetic subject per removed train at its first call):
    94/94, 127/127, 149/151 and 131/131 verdicts; the two NULLs are RX20, absent from ET that day.
    The replay can only yield TRUE or NULL, because its trains were built by excluding every number
    `last_recorded` held that day, so the FALSE and delay arms were proven on 942 trains that did
    run (830 FALSE, 68 cancelled, 41 late, 6 NULL with no recorded departure). **One asymmetry the
    rule creates, and the guard that closes it:** an unmatched row in the old branch stays NULL and
    is retried for ten nights, but an "absent" TRUE is resolved and so frozen by the NOT EXISTS
    guard, which would make a re-added train whose ET rows land after the 04:00 UTC run a permanent
    hit (0-37 VYG rail rows a day arrived after that hour on 19-22 Sept, apparently tail calls of
    trains already present), so the absent arm decides only once the operating date is two days
    old: the first night leaves it NULL and retries it, the second decides. The
    columns did not change, so there is no ALTER; the two registered copies still need
    `bq update --transfer_config` in the same sitting.
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
  - **Four segments since 2026-09-23: `predicted`, `relayed`, `no_sx` and `removed`** (plan
    2026-09-23-001, KTD10). `removed` is a train Vy took out of the day's timetable without
    publishing a cancellation (`KIX:PlanRemoved:<jkey>`, relayed by `predict_calls.sql`'s `et_only`
    branch). Its rows carry `et_cancelled = TRUE`, because that is what makes
    `impact._cancelled_prediction` serve the certainty, but ET declared nothing. So `relayed`, which
    means "the feed had already said so", is the wrong home for them. The discriminator is the
    prefix, as with `no_sx`, and **it is tested first, ahead of `et_cancelled IS NULL` as well as
    `IS TRUE`**: tested after the flag, every removed row lands in `relayed` with no error and the
    segment stays empty. Ahead of NULL moves no row, because a removed row cannot predate the flag:
    the prefix is from 2026-09-23 and the column from 2026-08-05 (dev on 2026-09-23: 1 355 removed
    rows, all TRUE). `n_unsegmented` now counts `provenance IS NULL`, not `et_cancelled IS NULL`, so
    the four segments and it still partition the labelled rows by construction. The CASE is spelled
    twice in each of `historikk_summary.sql`, `meldinger_summary.sql` and
    `message_wording_comparison.sql` (the rates, and the per-train lead time). All six copies must
    move together. `test_a_removed_train_is_resolved_before_the_et_cancelled_flag` in both suites
    pins the order. The blended figures still count these rows, since they are real decisions. On
    the card, a removed row gets the «Tatt ut av rutetabellen» tag, and the «Allerede meldt i
    SIRI-ET» tag is **withheld** although its flag is true, because that tag's claim is the one
    thing a removed train is not. The strip shows the fourth row only once it has rows, like
    `no_sx`. Two things this does not do. It leaves `historikk_decisions.sql` alone apart from a
    comment: `sx_only` already cuts on the whole `KIX:` namespace. And it does not collapse the
    removed summary into its own `/meldinger` SIRI-SX option, because it is one fixed text («Tatt ut
    av dagens rutetabell»), so it is one option already, the way the ET relay's texts always have
    been. Group it in `meldinger_filters.sql` if that wording ever changes. Read `pushed_any` with
    care on these rows: they reach the traveller in the morning digest rather than a per-train push,
    and a train that entered the prediction cache after the digest went out can show
    `pushed_any = false` («ikke levert») although the digest covered it. That is a known limitation
    of the digest's bookkeeping, not of the segment.
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
    exposure sits in `/meldinger`'s generated prose, which no projection controls. **That access
    control exists since 2026-09-10** (Entur SSO on both pages and both APIs, every environment —
    see the SSO bullet under Deployment), so the condition is answered ahead of time; the redaction
    below stays as it is because the gate is one env var from off. What still
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
    `/historikk-api` was (until 2026-09-10) unauthenticated on a public host and origin plus destination next to
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
