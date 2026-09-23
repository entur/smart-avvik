---
paths:
  - "disruptions/api/{notification,webhook}_*"
  - "disruptions/api/test_{notification,webhook}_*"
  - "disruptions/api/{varsel,varsel_state,test_varsel,test_varsel_api,test_varsel_state,test_varsel_import}.py"
  - "contracts/notifications/**"
  - "integrations/**"
  - "docs/integrations/**"
  - "docs/operations/notification-webhooks.md"
  - "firebase.json"
  - "firestore.rules"
  - "firestore.indexes.json"
  - "terraform/firestore.tf"
  - "helm/kix-avvik/env/worker-values-*"
  - "helm/kix-avvik/test/worker_render.sh"
  - "scripts/{manage_notification_client,loadgen_receiver,loadgen_subscriptions}.py"
---

# Webhook channel — /notification-api/v1

Moved verbatim from the root `CLAUDE.md` on 2026-09-23, where it was a bullet under
"Deployment". Where the text says "above", "below", "see the … section" or "Conventions", it
means the root `CLAUDE.md` or a sibling file in `.claude/rules/`.

- **No event for a train Vy took out of the plan** (`KIX:PlanRemoved:`, 2026-09-23, plan
  `docs/plans/2026-09-23-001-feat-removed-trains-plan.md`, R5): `varsel._observations_for_key` skips
  it before a subject exists, so it is never minted and never withdrawn — per-train `confirmed`
  events would spend a subscription's cap at 03:00 on a cut day. The public journey-alerts read lists
  it as `confirmed` (it walks `alert_worthy_trains`, not the webhook subjects), and the contract says
  both. It never WORDS one either (`notification_pull._entry_state`: `disabled`, and `_positioned`
  gives it no place under `MAX_WORDINGS_PER_READ`) — until 2026-09-23 it did, once per cycle per train
  because the snapshot is in the wording key, and a cut day's removed trains took the cap from the
  forecasts behind them; a dev read waited out its whole `wait` on it and fired the p99 alert. A digest event would be a contract change; nobody has asked for one.
- The **webhook channel** (`/notification-api/v1`, `disruptions/api/notification_*.py` +
  `webhook_*.py` + `notification_wording.py`, 2026-09-08) sends an external client's BFF the alerts
  `/varsel` already sends its own browsers, as signed HTTPS webhooks. **Renamed on 2026-09-09 from the
  `partner_*` vocabulary**, before anything was applied or integrated: the credentialed caller is a
  **client** (the Entur app is one, an external operator another — "partner" is Entur's word for the
  latter only), the channel is **webhook**, the shared machinery is **notification**, and the Firestore
  database is `notifications` with unprefixed collections. `/varsel` stays as the test-bed for
  experimental features ahead of this API, and the decision log covers both channels since
  2026-09-13 (U16, KTD4) — the precondition being `/historikk` behind authentication, which it has
  been since 2026-09-10 (see the KTD10 paragraph below for the grain, which did not move). The three documents are
  `docs/integrations/notification-api.md` (the client contract), `docs/integrations/firebase-bff.md`
  (the two vendored TypeScript packages under `integrations/`) and
  `docs/operations/notification-webhooks.md` (the operating contract, which carries the
  clause-by-clause retention check and the enablement conditions). **It is on in dev and prd since 2026-09-10
  with no client onboarded, and off in tst**, which still needs the two Secret Manager secrets and
  the Terraform apply; the operations document is the sequence.
  **It is `/varsel`'s logic reused by extraction, not a second alerting stack.** Which trains alert,
  which alerts clear the bar, the per-train collapse, the Norwegian wording and the daily cap are
  `/varsel`'s, unchanged: a webhook subscription is another registration whose channel is a webhook,
  grouped under `journey_key`, so a client and a browser holding the same definition see the same
  trains and cost **one match and one wording between them** — asserted by call count, because that
  parity is the whole premise and the thing most likely to drift silently. `alert_policy.gate` is the
  one statement of the threshold and the comparison, so the two surfaces cannot come to disagree
  about what a threshold means; `is_material` — the origin branch's widening of that gate by a
  declared cancellation anywhere on the ridden segment — is **refused by name** and its absence is
  pinned by a grep, because a widening only ever adds subjects and would therefore be silent.
  **Notification DELIVERY state is Firestore's and never `varsel_decisions`'** (KTD10), and since
  2026-09-13 the decision itself is in both. That table is written for every match including
  sub-threshold ones and `/historikk` and `/meldinger` publish it — including resolved origin and
  destination names since 2026-08-22, accepted then because prd's registrations are the team's test
  users. Client-app travellers are not (`/meldinger` measured 21 of 31 stop pairs isolating exactly
  one journey), which is why a webhook route stayed out of it entirely while those pages were
  **public**. They are behind Entur SSO in every environment since 2026-09-10 (#142), and at the
  population the 100k plan sizes for "a webhook-only route is unscored" would have meant the product
  is unscored — so **U16 (2026-09-13, KTD4) records every definition pass 1a matches**, at the grain
  both channels already share. What did NOT move is that grain: a row is one journey DEFINITION's
  view of one situation on one train, and the client side contributes `channel` ('browser',
  'webhook', 'both') and an `n_subscriptions` COUNT beside `n_devices`. No subscription, client,
  destination or external reference reaches the decision log, the outcomes table, the groups view or
  either page — `test_notification_contract` greps the three files for exactly those names, and each
  page suite greps its own SQL as WHOLE WORDS, which is what tells `n_subscriptions` (a count) from
  `subscriptions` (the collection).
  **Three readings changed with it and each is silent if taken the other way.** `pushed` means "a
  notification was ISSUED on any channel" — for the client the event MINT, not its delivery, which
  lags by up to two cycles and would read as never-notified for a subject retired in between — so a
  `both` row can say `pushed` with no Web Push behind it. `push_disabled` is a BROWSER answer (no
  VAPID keypair) and is never given to a definition with no browser side, or the value would quietly
  start covering two unlike states. And which LLM delivery switch chose the recorded wording follows
  the row's channel (`VARSEL_LLM_ENABLED` for the push, `NOTIFICATION_WEBHOOK_LLM_ENABLED` for the
  event), so a webhook-only row does not quote prose no client received and a shared row keeps the
  browser's. **The mint row is an EXTRA row, written after emit**: emit runs in its own phase after
  `check_and_notify` has returned (KTD6), so a cycle's mints cannot reach the rows it already wrote —
  `varsel.record_minted_decisions` copies the thinning ledger's own row with `pushed` flipped rather
  than rebuilding one, and skips a subject the browser already pushed this cycle so the view's "first
  pushed row" wording pick stays decidable. **What the copy cannot carry is the five wording columns,
  and they are restated** — on a thinned cycle the ledger's row came through `decisions.rescored`,
  which refreshes the prediction figures but carries `message_input`, `message_deterministic` and
  `message_status` from the last row actually WRITTEN, up to `_DECISION_THINNING_INTERVAL_S` ago. The
  browser channel never meets that (`pushed` is one of the thinning rule's keep-reasons, so a pushed
  row there is always a fresh `build_row`), and `/meldinger` replays the FIRST pushed row's
  `message_input` under that row's own `decided_at` — so a straight copy had it wording "what we
  would send now" from a quarter-hour-old document beside figures that are not. Two rules the
  restatement follows, both silent if taken the other way: `message_llm` falls back to the pass's
  answer where this channel read no cached wording, because `_webhook_wording` answers None when
  `NOTIFICATION_WEBHOOK_LLM_ENABLED` is off while a model wording sits in the cache — which is the
  "written but withheld" population, not an absence; and `ok` is demoted to `ok_cached` exactly where the restated
  `message_input` DIFFERS from the one the ledger was carrying, never merely because a model wording
  is present — `ok` claims a same-input pair (`message_wording_comparison.sql` filters on exactly
  that), and most mints are the `predicted` transition, which is itself a thinning keep-reason, so
  the pass wrote that row fresh on the same cycle and its `ok` is true. Demoting those would retire
  the client channel from that population almost entirely. Anything else that reads `_decision_ledger`'s stored
  row has the same trap. The groups view AGGREGATES `channel` onto the subject and
  never groups on it: grouping would split a shared definition into two subjects and two verdicts.
  **The operator step is not optional and its order is fixed**: two columns on
  `varsel_decision_outcomes` break the registered scheduled query until it is re-registered in BOTH
  environments, so deploy first (the startup `ADD COLUMN IF NOT EXISTS` creates them), then
  `bq update --transfer_config`. What is still gated is the product decision, not the code: enabling
  webhook scoring in prd for a real client waits on the DPO answer to finding 46, and prd is inert by
  POPULATION today (no webhook client) rather than by a switch — there deliberately is no flag.
  Nothing anywhere is a claim about any operator but **Vy rail** (21 308 of 21 331 replayed subjects
  were VYG).
  **The poller matches against an in-memory MIRROR, not a per-cycle read** (U6, 2026-09-12, KTD5).
  `varsel._load_webhook_recipients` used to pull every active subscription from Firestore on every
  cycle, capped at 2 000 documents in two places that had to be edited together. At 100 000 that read
  IS the cycle (~$9-18/day in document reads alone) and the cap truncates **silently** — the load
  returns a page and the subscriptions past it are simply not matched. `notification_store` now holds
  a `SubscriptionMirror`: one full paged read at startup, then per cycle a query for the documents
  whose server-stamped `mirror_updated_at` moved. A subscription nothing writes to is read once, ever.
  **Which bodies stamp is the mechanism**, and both mistakes are silent. `_touch_body` DOES (the plan's
  own list omitted it): it moves `expires_at` through `_horizons`, the mirror tests liveness against
  its own copy, and the idempotent re-PUT is the write a client makes most often — unstamped, the
  mirror drops a live subscription the client has just said it still wants. `_sent_body` does NOT: it
  writes the `delivered` marks, so stamping it would make every delivery re-read its own subscription
  and the busiest documents would sync every cycle, which is the whole economy of the design. The cost
  of that omission is stated rather than hidden — the mirror's `delivered` set can lag, so the pass can
  WORD a train already delivered; it cannot duplicate one, because `_emit_body` reads the real subject
  document inside its transaction — and the standing-subject mirror is what closes it. The set is
  declared in `MIRROR_STAMPING_BODIES` and pinned off the source both ways, because what has to hold is
  a property of the SET: a body added next year is the case no behavioural test over today's five sees.
  **Server-stamped, never `now`.** Every body already takes `now` for `updated_at` and reusing it is the
  obvious thing to do: it would break the mirror quietly, because the watermark is compared against
  values Firestore wrote, so a pod whose clock runs a few seconds fast advances past documents the
  store stamped later and never syncs them again. The suite's fake resolves the sentinel against a
  clock deliberately 37 s ahead of the one the bodies get, so an implementation that wrote `now` fails.
  **Three properties the pass depends on.** A failed sync RAISES and keeps the previous mirror (an
  empty one reads to every downstream reader as "every client unsubscribed"). Liveness is applied on
  the READ, because nothing writes a document when its inactivity horizon passes at 03:00, so no sync
  can carry that news. And the 120 s overlap exists because Firestore commits are not ordered by the
  instant a transaction started — a transaction that began before the last sync can commit with an
  EARLIER stamp than a document that sync already saw, and a strict `> watermark` steps over it
  forever; the overlap is safe only because entries are applied by document id, so re-applying is a
  no-op. **`MIRROR_MAX_AGE_S` (6 h) is a CORRECTNESS bound, not a freshness one**: a revocation arrives
  as a tombstone whose own TTL deletes it after roughly a day, so a sync broken for longer loses the
  removal for good and the poller keeps alerting a revoked recipient — past the bound the mirror
  re-seeds instead of syncing. `kix_mirror_age_seconds` is the series an alert selects on (rule 2a).
  Two Firestore traps recorded here because both are silent: an `order_by` **excludes documents that
  lack the field** exactly as an equality does, so the seed orders by `created_at` and never by the
  watermark — ordering by it would skip every document written before it existed, which on the deploy
  that introduces it is all of them; and the seed runs **on the poller thread**, not in the lifespan,
  because the app serves nothing until the lifespan yields and a 100 000-document read there would
  hold the port closed and fail the readiness probe. It beats the heartbeat between pages, or
  `/health`'s 600 s staleness rule restarts the pod mid-read, forever. The sync is its own phase
  (`notification_mirror`) placed BEFORE `varsel_notify`: its own because a slow store now costs the
  mirror its freshness rather than the pass its cycle, and before because a mirror synced after the
  pass would serve every cycle the population of the one before.
  **The standing-subject mirror is the second half and is not optional.** The emit pass asks "does this
  subject stand, and in which phase?" and answered it with one `client_alerts` query per client,
  bounded at `MAX_SUBJECTS_PER_READ` — past which the answer is INCOMPLETE, and incomplete means a
  store transaction per subject, because treating a truncated read as complete skips a withdrawal that
  was owed and a `confirmed` mint for a train ET has cancelled. At one client with 100 000
  subscriptions that fallback is the steady state. `SubjectMirror` answers it completely from memory,
  **kept current by the poller's own commits rather than a watermark**: subject documents have exactly
  one writer (KTD6), so every change passes through this process. It is indexed BY SUBSCRIPTION as
  well as by alert id, because the pass asks per subscription and a flat map would make each of
  100 000 questions a scan.
  **U6 BUILT it and U9 WIRED it, and for one deploy it was neither read nor written** — the class,
  its seed and its tests shipped with no caller at all, which is worth knowing because the sentence
  above described the intent rather than the state. Since U9 (2026-09-13): the emit pass reads it in
  place of `client_alerts`, and `notification_store._mirror_subject` is the one place a commit
  reaches it, called from `commit_event`, `record_below_threshold` and `clear_below_threshold` and
  from nothing else. **Which bodies mirror is declared as a SET**, `SUBJECT_MIRRORING_BODIES` beside
  `SUBJECT_WRITERS_OUTSIDE_THE_MIRROR`, pinned off the source both ways for `MIRROR_STAMPING_BODIES`'
  reason: what has to hold is a property of the set, and a body added next year is the case no
  behavioural test over today's four can see. That pin is also the answer to "should the standing set
  re-seed periodically, like the subscription mirror's `MIRROR_MAX_AGE_S`?" — **no**: the
  subscription mirror's bound exists because a revocation arrives as a tombstone whose own TTL
  deletes it, which is a timing race; this one's failure mode is a forgotten writer, and a clock
  cannot catch that while the set-pin does. The apply is on the RESULT of a transaction and never
  from inside a body — a body may run more than once, so an apply from inside records a state a
  later attempt overwrote — and an emulator-backed test drives the three commits through the real
  client, because the hermetic fake runs each body exactly once and so cannot see that ordering.
  `_sent_body` is the declared exemption: `delivered_revision`, `delivered_at` and the pending
  pointer are not in the standing set, and mirroring them would put the busiest documents in the
  hottest loop for no reader. **Re-check that exemption when U11 moves delivery to its own
  Deployment** — those three fields are exactly what a worker there writes, and the moment one of
  them enters the standing set this process stops being their only writer. `sync_mirrors` also
  prunes expired subjects, which correctness never needed (both readers test expiry) and memory
  does: a subject document lives `HISTORY_DAYS` (7), so an unpruned mirror holds every train every
  subscription was ever warned about for a week.
  **The SUBSCRIPTION mirror learns from this process's own commits too since 2026-09-13** (U13's
  store half), and that supersedes a constraint U12 recorded rather than merely adding to it. U12's
  note at `MIRROR_STAMPING_BODIES` said the cycle cannot read `notified` or `pushed_trains` from the
  mirror, because the two mark bodies deliberately do not stamp the watermark field — one push on a
  corridor with a hundred thousand registrations would be a hundred thousand documents through the
  watermark query. That is right about a WATERMARK and wrong about the mirror: a `/varsel`
  registration has exactly one writer (the request path and the poller are one process, one replica
  under Recreate, and the delivery worker writes webhook deliveries only), so every commit is
  applied to the mirror from its own RESULT — `SubjectMirror`'s arrangement, one collection along —
  which makes the copy exact rather than approximate and costs no read. The superseded reading
  would have cost a document read per registration per cycle, which at this population IS the
  cycle. `SUBSCRIPTION_MIRRORING_BODIES` declares the seven bodies that apply and
  `SUBSCRIPTION_WRITERS_OUTSIDE_THE_MIRROR` the five that deliberately do not (the webhook delivery
  half, which runs in the worker, plus the cleanup that only ever follows a tombstone the mirror has
  already dropped); both are pinned off the source in both directions, and a second test walks each
  declared body back to the public function that runs it — a declaration with no call reads as
  covered and is the same silent failure as an undeclared body. **The race the local apply creates
  is real and is guarded**: a sync that read a page before a request thread deleted a journey would
  apply its stale copy after the delete, leaving the journey live in the mirror for a cycle and
  waving a push through the delivery loop's own re-check. `SubscriptionMirror._overtaken` skips any
  entry this process applied after the run's read began — conservatively, against the whole run's
  start rather than the page's — and the count is published as `overtaken` on the mirror's log
  record. What it costs is one cycle of staleness for a FOREIGN write to a locally-touched
  document, which for this collection means a webhook subscription the API wrote in the same
  process and has therefore already applied. **The erasure drops by id** rather than by document,
  because `_erase_device_body` returns ids and not documents: the stamp is what makes a restarted
  process agree, the drop is what makes this one agree now. `MirrorEntry` grew the six fields the
  browser cycle reads (`device_id`, the two typed names and the three push ledgers), measured at
  **87.7 MB → 158.8 MB per 100 000 entries** against the pod's 1 024 MiB — the delta is the marks
  themselves, not the slots, so what bounds it is `_prune_marks` at write time.
  **Both 2 000 ceilings are gone**, with `active_subscriptions`, the `load_active_subscriptions` spec,
  its composite index and the `truncated` flag that reported it — an index paid for on every write with
  no reader is what the bidirectional index test exists to refuse. Note U5 added that index one PR
  earlier and U6 removes it: the destroy is free (nothing queries it), but it is worth knowing the
  plan's own sequencing produced it.

  **Every per-client ceiling is the CLIENT's, and the create count is an aggregation** (U7,
  2026-09-13). `_create_body` counted a client's live subscriptions by READING them inside the
  create transaction, up to the ceiling — bounded, paid on a create, which a commuter does once, and
  the stated reason `QUOTA_BOUNDS` capped `max_active_subscriptions` at 10 000. At 200 000 "bounded"
  stops meaning "affordable". It is now a `count()` over the `expires_at > now` inequality the
  `subscription_count` spec declares, and that inequality needed **no new index**: the query already
  ordered by `expires_at DESCENDING`, and Firestore's rule is that a range filter's field must lead
  the ordering, so `index_fields` is unchanged and the unit's terraform plan is empty — the
  operations document predicted an index and was wrong. Two properties to know. The count is a
  **superset of live, never a subset**: `expires_at` is the minimum of R5's two horizons, so every
  live row is inside it, while a row whose stored value drifted ahead of a horizon is counted
  although `subscription_is_live` would reject it — so it reads high, never low, and high REFUSES a
  create, which is the direction a ceiling must fail in. And there is deliberately **no stream
  fallback**, unlike `_count_destination_deliveries`, which keeps one because it is the same answer
  at a worse price: at this ceiling a silent fallback is a 200 000-document read under pessimistic
  locks on every create, so a `FailedPrecondition` nobody can miss is the better failure. One thing
  moved with the bound that is not the ceiling — `list_subscriptions` bounded its liveness-skipping
  scan by the client's ceiling too, so the raise would have turned a `?limit=1` request over a
  collection full of undeleted rows into a 200 000-document read; that scan is capped separately at
  `MAX_SUBSCRIPTION_LIST_SCAN` (10 000, the old bound), so the listing's worst case did not move.
  Three more quotas joined `Quotas`: `max_pending_events` and `deliveries_per_minute` (the two
  per-destination constants, which stay in the published-numbers table as DEFAULTS — the plan said
  to remove the rows, and keeping them keeps the only guard that notices a constant moving) and
  `concurrent_deliveries`, **declared and deliberately unread until U8**, with a test asserting it
  is still unread so it cannot become decoration. All three are resolved by the CALLER and never by
  the store: `notification_store` must not import `notification_auth`, and `notification_events`
  pins its import set exactly, so `commit_event` takes `max_pending`, `pending_deliveries` takes a
  `per_destination_limit` resolver, `emit` takes a `pending_quota` resolver, and `main` and
  `webhook_delivery` — which already import both — do the lookups through the new
  `notification_auth.quotas_for`, off the same cached account read `authenticate` pays for and
  memoised once per client per pass. The delivery counter stays per DESTINATION while the ceiling
  comes from the client, so one bad endpoint cannot eat its sibling's allowance.
  **A retryable failure now backs the destination off DURABLY**, beside the pass-local skip rather
  than instead of it. The skip is what stops N pending events against one hung endpoint costing N
  read timeouts on the poller's thread and it cannot outlive the pass — so it could not honour a
  receiver that says *how long*: a 429 with `Retry-After: 45` was answered by attempting again on
  the next cycle, which may be sooner. `back_off_destination` writes `retry_not_before` and a
  `consecutive_failures` count: the receiver's delay when it named one, otherwise `BACKOFF_BASE_S`
  (30 s) doubled per consecutive failure, both capped at `RETRY_AFTER_CAP_S` (300 s) — an event's
  validity is an hour, so a longer backoff would expire the backlog it protects. Four things about
  it, each silent if taken the other way. It is **not a pause**: `deliverable` keeps meaning "in
  service" because the notification API reports it to a client, and the delivery path asks
  `ready_to_dial(now)` instead. Only a **retryable** failure earns one — a 400 is about the event we
  sent, and a 410 keeps R12's terminal pause, which no clock lifts (the two are pinned together, or
  a pause that expired on a timer would resume delivering to an endpoint that said it is gone; the
  tests assert `webhook_sender.classify_status` itself rather than the stub that stands in for it).
  A named `Retry-After` still advances the failure count, so a receiver that asks for 45 s three
  times and then fails bare is backed off at the fourth step of the curve — intended, since the
  count means consecutive failures, but it makes the curve describe recent history rather than only
  unasked-for failures. It is
  written **once per destination per pass**, because `_Skips.drop` runs AFTER the failure write and
  every worker already in flight fails before it lands — unclaimed, a four-wide pool against a merely
  slow receiver doubles four times and hits the cap on its first bad cycle. And it is **cleared on
  the first success that has something to clear**, tested against the pass's cached record, so a
  healthy endpoint carrying twenty thousand POSTs writes to its destination document zero times.
  `backed_off` is its own counter on `webhook_delivery_round`, never folded into `skipped`: "a
  sibling attempt took it out" and "we are waiting exactly as asked" are different operator answers.

  **A claim takes a LEASE, and the outbound client pools connections** (U8, 2026-09-13, KTD14).
  `claimed_at` was written by `_claim_body` and read by nothing — harmless with one writer, and the
  whole problem with U11's worker: two workers reading the same pending page both claim, both POST,
  and the traveller is notified twice, which the receiver's own `event_id` inbox drops and which is
  exactly why it would never have been noticed from our side. A claim now takes `DELIVERY_LEASE_S`
  (20 s) and mints a token. **The number is arithmetic, not judgement**: after the claim commits an
  attempt still reads the event (one Firestore call at 4 s), POSTs it (5 s, 2 s of that the connect)
  and writes the terminal state (4 s) — 13 s worst case, so the plan's "~15 s" leaves two seconds
  for signing and scheduling. The token is minted OUTSIDE the transaction for the reason every clock
  here is read outside one: a body may run more than once, and one minted inside would differ
  between attempts, so the retry would write a lease its own caller could not prove it holds.
  `_sent_body` and `_failed_body` compare it, and a mismatch is a THIRD outcome (`lease_lost`) and
  not a flavour of `already_resolved` — the delivery is not resolved, it is somebody else's. The
  failure half is the worse one: unguarded, a lapsed holder returning would set a re-claimed
  delivery back to `pending` and release the live claim's cap slot. Leased rows are filtered out of
  `pending_deliveries` **in Python**, never in the query: a second inequality would need its own
  index *and* would exclude every document lacking the field, which on the deploy that introduces it
  is the whole backlog. `leased` and `lease_lost` are unreachable while there is one writer and say
  so — they report on U11. `RESERVATION_TTL_S`'s rationale was rewritten and its number was not: its
  upper half was "shorter than the pass period", which stops holding when delivery has no period, so
  what bounds a leaked reservation is now the lease and the re-claim behind it.
  **The pool is the other half.** Twenty thousand POSTs to one receiver were twenty thousand TLS
  handshakes, inside a 5 s per-attempt ceiling — so the CONNECTION, not the request, was the cost of
  a bad morning. `webhook_sender` keeps per-host connections with four bounds: one validated
  resolution per 60 s (KTD9 one layer along — trusting the first answer forever is safe against a
  rebind and useless against a client moving their endpoint, while re-resolving per request pays a
  DNS round trip inside the ceiling; a refusal drops the idle set, an open connection finishes,
  and a resolution that merely MOVES also drops idle so the client's own failover takes effect);
  idle connections kept 60 s, with a peer-closed pooled connection costing exactly one fresh retry
  rather than a delivery; in-flight requests per host from the client's `concurrent_deliveries`,
  which is what stops a wide worker opening a connection per thread against one receiver; and a
  response that was not drained is never put back, since the leftovers would become the next
  request's reply. The transport drives `http.client` directly now — `urllib`'s
  `AbstractHTTPHandler.do_open` opens and closes a connection per call, which is the cost being
  removed — and everything the opener was chosen for holds by construction rather than by absence
  (a 3xx is a response this module classifies, nothing reads `https_proxy`, no cookie jar exists).
  **The socket deadline is armed per REQUEST, not per connect**, which is the bug pooling
  introduces and does not look like one: a reused connection otherwise carries the first request's
  deadline, already in the past, `read_timeout` floors it at 50 ms, and every later request times
  out looking exactly like a receiver that went slow. One thing the plan asked for was deliberately
  NOT done — see the paragraph below.
  **`_emit_body` keeps its transactional subscription read**, against the plan's step 2 ("the
  mirror holds the subscription"). The read feeds exactly one thing, `_subscription_obsolete`, and
  four of its five checks are redundant with `_claim_body`'s own re-read at dispatch — but the
  fifth, the VERSION compare, is not: `_claim_body` deliberately does not check version (a version
  compare there would cancel a still-pending alert on every amendment, which its comment spells
  out), so emit is the only place R13's version rule is enforced. The mirror is at best as fresh as
  the sync at the start of this cycle, which is the same snapshot the pass matched against — so
  reading it here would answer "has this subscription changed since the match?" with the match's own
  data, and an amendment landing mid-cycle would emit a v1-shaped notification for a v2
  subscription. The saving is one document read per minted transition; the cost is the only
  enforcement point of a rule the plan does not otherwise replace. Revisit if the mirror ever syncs
  inside the pass.

  **Emit is ONE WIDENED PASS, and the deadline had to move with the width** (U9, 2026-09-13, KTD2).
  One transaction at a time against a ~200 ms round trip is about **five emits a second** however
  many are waiting, so a bad morning's lifecycle transitions could not be emitted inside one cycle
  at any population worth the name. Three things moved together and any one alone changes nothing.
  The **width** is `_EMIT_WORKERS` (64), derived as the target rate times the measured transaction
  latency (250/s × ~250 ms) rather than chosen; the pass decides SERIALLY on the coordinator — the
  sort, the train identity, the standing set, the skip rule, the ceiling, all microseconds and no
  lock because there is one of it — and submits only the transaction. A whole observation as one
  task would make a 100 000-recipient corridor a single task and defeat the pool; deciding inside
  the workers would put the standing set behind a lock all sixty-four take. The **deadline** went
  6 s → 80 s with the wait at 90 s, because at 6 s the pass stops after ~4 s of claims whether it is
  one-wide or sixty-four-wide; `_MAX_EMITS_PER_CYCLE` is that deadline times R2's 250/s = 20 000,
  and a test refuses the two drifting apart. And the **count**: `commit_event` ran a pending-delivery
  aggregation per mint, which was most of that ~250 ms and would be sixty-four concurrent
  aggregations against one collection — `_PendingLedger` counts each destination once per pass,
  charges on the coordinator and REFUNDS in the worker whatever did not leave a new pending delivery
  behind. The refund is the load-bearing half: a superseding mint is net zero (`_mint` flips the old
  delivery to `superseded` in the same transaction), so a counter that only counted up would refuse
  at HALF the ceiling on exactly the morning where many mints are a confirmation. A **withdrawal**
  now passes the same ledger, where only a first `QuotaExceeded` stopped one before.
  **What makes the ceiling exact is no longer the shape of the loop.** KTD6's "one writer" is now
  sixty-four threads of one pass, so exactness rests on that lock plus `emit`'s refusal to start a
  second pass while the first is draining (`overlap_refused`) — the module lock is taken without
  blocking and released by `_run`'s own `finally`, never by the thread that gave up waiting, and the
  future collection is bounded for the same reason: a transactional commit carries no client-side
  deadline, so waiting on a hung one would hold the lock and refuse every later cycle forever.
  `abandoned` on the record is how many were walked away from; read it beside `capped`, which is a
  pass simply larger than its budget when `abandoned` is zero and a slow store when it is not.
  **Two things above the loop cost a whole cycle if unguarded**, and both were inside the
  per-subject `try` before this unit: the quota resolver and the ledger's first-sight aggregation.
  Unguarded on the coordinator, one 4 s deadline hit reports the entire cycle as a timeout and
  discards the counts of every transaction already committed. Guarded per destination now.
  **`varsel` hands over one observation per (definition, train) with its recipients**, not one
  `WebhookSubject` per (recipient, train): the payload was always shared by reference and the
  dataclasses around it were not, so a 100 000-subscription corridor with AE3's thirteen departures
  built 1.3 million of them in one burst on the ingestion thread. The fan-out moved into the emit
  loop, after the standing set has said whether a store call can happen at all — which for the ~83 %
  of matched alerts that are sub-threshold with nothing standing is never. `_sort_key` orders TRAINS
  now and the subscription id has left it: urgency is a property of the departure.
  **No per-revision log line survives**: `commit_event`'s INFO record per mint would be 20 000 lines
  a cycle, so what a reader gets is the one `notification_emit` summary, and the events themselves
  are Firestore documents rather than log lines. `EmitReport.minted` is U16's seam, on the returned
  value and never a module global.
  **Servability is ONE BigQuery job per cycle, and no write path asks it** (U10, 2026-09-13, R5/R6).
  A job was the unit of work: a create and a replace each asked inline, and
  `notification_servability` re-asked FOUR subscriptions a cycle — ~1 200 records a day, against a
  population meant to reach 100 000, so a first pass would never have finished. Both sweeps now
  derive the distinct stop PAIRS from the records they are about to write and put all of them to one
  job (`disruptions/api/varsel_connectivity.sql`, `main._varsel_connectivity_batch`), fanning each
  answer back out — so a corridor with a hundred thousand commuters is one place in one query, and
  `main._varsel_connectivity` is a batch of one rather than a second implementation. The bound moved
  from questions to WRITES: `_MAX_WRITES_PER_CYCLE` (2 000) across `_WRITE_WORKERS` (32), spent in
  `_priority` order, collected under a bounded wait because a transactional commit carries no
  client-side deadline. `varsel.recheck_service` had no per-cycle bound at all and now has the same
  shape.
  **The writes stay TRANSACTIONS, and the plan's batched-write step was refused.** Firestore has
  exactly two preconditions, `exists` and `last_update_time`, and neither can say "and the route has
  not changed" — which is the only thing a servability answer is about, and what
  `set_servability`'s `expected_version` already enforces inside a transaction. `last_update_time`
  read off the subscription mirror is as old as the last sync and `_sent_body` deliberately does not
  stamp, so the busiest subscriptions would fail the precondition every cycle with nothing in the
  logs to say why; a `WriteBatch` is also atomic, so "the rest of the batch lands" is not what it
  would do. Pinned by an AST check over the module, not a grep — the prose explaining the refusal
  would otherwise match as though it were the rule being broken.
  **`servable` is null on a create and a replace response**, always, and this is the one
  wire-VISIBLE change of the 100k work so far — the operations document's "none of it touches the
  wire contract" line was wrong and now says so. The schema does not move (`boolean | null`,
  optional, in both the JSON schema and the generated TypeScript); what moves is that a client
  rendering the caveat from a write response renders nothing until it re-reads. A fresh bulk load of
  100 000 settles in ~50 cycles, about four hours, and both contract documents say so.
  **One fix in there is not about scale**: `varsel.recheck_service` saved its state only when a
  verdict CHANGED, so the stamp that takes a still-unservable journey out of the due set for six
  hours lived in memory until some other writer happened to persist — and a restart re-asked them
  all. It saves whenever anything was stamped.
  **The measured equivalence, recorded because nothing in CI executes SQL**: against dev on
  2026-09-13, a batch of one answers exactly what the single-pair query answered over six pairs,
  including several refs a side, a pair served only in reverse, a pair whose two ends are one place,
  and a ref no table holds. One job over six pairs billed 20 MB. A `bq --parameter` dry run accepts
  the `ARRAY<STRUCT<... ARRAY<STRING> ...>>` shape, so the file is in `validate_varsel_sql.sh` — but
  a dry run does not evaluate parameters, so it proves the shape and the column types and nothing
  about the join.
  **The store can hold a traveller's registration, and the shape dispatches on `channel`** (U12,
  2026-09-13, KTD12). Nothing reads any of it yet — U13 is the wiring — and it is one PR ahead of
  that on purpose, because U12 ADDS an index and a TTL config while U13 switches queries: the
  2026-09-12 measurement is that adding an equality to an existing query is a destroy-and-create
  with a **~7-minute** guarded outage on that read, and add-only has none.
  **Validation refuses the other channel's field in both directions.** `destination_id` is required
  on `webhook` and refused on `web_push`; `device_id` is the mirror image. Refusing matters as much
  as requiring: a `web_push` document carrying a destination id would be offered to the delivery
  pass by any future query that forgets its channel filter, which is a traveller's commute POSTed
  to a client's callback. The **web_push**-only fields are **absent** on a webhook document rather
  than written empty — a Firestore equality does not match a document that LACKS a field, so an
  absent `device_id` is what makes it impossible for the `device_journeys` query to return a
  client's subscription, as a property of the data rather than of whoever writes the query next.
  **That holds in one direction only, and the asymmetry is the code's rather than the design's**
  (found on prd after the 2026-09-13 cutover, where all 275 imported registrations carry it):
  `_channel_fields` writes the web_push set conditionally, while `destination_id` is written by
  `_new_subscription_doc` itself, so a `web_push` document has it as `""`. The half that matters
  still holds — validation refuses a NON-EMPTY `destination_id` here, so no such document can name
  a real destination, and U5's `channel` equality is a genuine filter — but the defence-in-depth
  claimed above is not there for that one field: an empty string is matchable by an `order_by` or a
  range, where an absent field is invisible. Do not "fix" it by moving one line:
  `_subscription_from_doc` and `_delivery_subscription_from_doc` index `doc["destination_id"]`
  directly, so omitting it KeyErrors every web_push read in production until both learn `.get`.
  Note dev's pre-U12 webhook documents lack `device_id` because the field did not exist when they
  were written, which is what made the stronger claim look true from the data.
  **`web_push` has an INACTIVITY horizon and no lifetime one — 180 days since the traveller was
  last here, and U14 inverted that from U12's shape on the product owner's decision (2026-09-13).**
  U12 gave this channel the lifetime horizon alone, reasoning that the 60-day inactivity one is
  about a client's forgotten integration while a traveller not opening the PWA for two months is
  what a working alert service looks like. Both halves are right and the conclusion was backwards:
  what it left was a clock anchored on `created_at` that NOTHING could reset — the client never
  touches, `_replace_body` keeps `created_at`, and the device upsert writes the device document
  alone — so a commuter opening the app every morning still lost the journey on day 181. Silently:
  an expired registration stops matching, drops out of `device_journeys` and is TTL-deleted within
  ~24 h, while the device record stays healthy because its own TTL is restamped on every upsert.
  A storage-limitation rule is for ageing out what nobody uses, which is what an inactivity horizon
  says and a creation-anchored one does not. **`notification_store.note_web_push_activity` is what
  moves the anchor**, called from `StoreRegistrations.device_journeys` — the journey list, because
  that is the one request every app open makes *and* the one the traveller who never turned
  notifications on still makes (79 of prd's 186 journeys sat on such devices when the device report
  last counted); hooking it to the device upsert instead would have expired exactly that population
  while it was using the page daily. It renews at most once per `WEB_PUSH_RENEW_AFTER_DAYS` (7) per
  journey, so the PWA's 60 s poll costs nothing and the effective expiry is 173–180 days after last
  use; the device document rides on the same event through `renew_device`, so «a device and its
  registrations go at the same time» is finally true. It is a read that WRITES, deliberately not a
  protocol method: the seam is a list of questions the caller asks, and "the traveller was here" is
  this backend's own storage-limitation rule, which the blob has no concept of. The field is
  OMITTED rather than written far ahead, because a field that lies about its meaning is worse than
  an absent one, and `subscription_is_live` dispatches through `_horizon_fields` — via `_channel_of`
  since U14, which is the one reader of the stored channel and is in the liveness-map field scan for
  that reason. The fail-closed property survives and it is worth saying which part: a document with
  no `channel` reads as `webhook` (U5's convention) and still needs both, and a `web_push` document
  missing its INACTIVITY horizon is still dead — so a document written under U12's rule reads as
  expired rather than as immortal, which is the direction a horizon change has to fail in. No
  environment held one when the inversion was made (nothing selected the store backend until dev's
  cutover later the same day, and `varsel_state` is the only writer of the channel), which is what
  made it a free choice rather than a migration. **What it
  still costs is a privacy-text change, not a silent one: «Reiser du har registrert lagres til du
  sletter dem» is false for a traveller who abandons the app for 180 days.** T5's «Hvor lenge» is
  rewritten and `CONSENT_TEXT_VERSION` is `2026-09-13.1` (#189, held open to merge WITH prd's
  cutover). The version moved although T1-T3 did not, because `personvern-page.tsx` renders «Sist
  oppdatert» from it — a T5-only edit that changes what we DO must bump it or the page dates the
  new text to the day the old one was written. The published wording is «rundt et halvt år» rather
  than «180 dager» because the anchor is refreshed at most weekly, so real deletion lands 173-180
  days after the last visit and a stated 180 would be a promise beaten by up to a week;
  `test_personvern_texts.py` fails if the constant leaves 180. Two call sites had to learn the channel with it —
  `_replace_body` and `_touch_body` both called `_horizons` with its `webhook` default, unreachable
  while no web_push document existed and the primary path now.
  Two traps this had to not fall into, both of them silent. `device_id` shares `destination_id`'s
  slot in the **definition digest** through an `or`, because exactly one is non-empty by
  construction — a slot of its own would add a separator, change every stored hash and turn every
  client's idempotent re-POST into a Conflict on the deploy that introduced the field. And
  `MirrorEntry.liveness` is a hand-built map handed to `subscription_is_live`, so it had to gain
  `channel` too: without it every `web_push` entry reads as a webhook one, demands the horizon it
  does not have, and **the mirror holds the entry, reports it in its size, and serves it to
  nobody**. A structural test now pins that map against every field the predicate reads.
  **«Slett alt» TOMBSTONES the journeys and hard-deletes the device, and the reason is the mirror.**
  The obvious erasure is `transaction.delete` on every document, and it is wrong for a reason
  nothing about the transaction reveals: a deleted document matches no query, `mirror_sync`'s
  watermark query included, so the poller's in-memory mirror would keep the traveller's journeys
  for up to `MIRROR_MAX_AGE_S` (6 h) and keep matching, wording and writing decision rows about
  somebody who asked to be forgotten. A tombstone travels the path the mirror already has, so
  `_erase_device_body` is in `MIRROR_STAMPING_BODIES`. What survives is a husk — id, status, a
  7-day TTL — because the route, the window, the typed names, the device id and the three push
  ledgers are cleared in the SAME write, which is **stricter than a client's revocation** (that one
  redacts only once cleanup completes): a client is administering its own integration, a traveller
  is exercising erasure, and "within 24 hours" is not what that answer should be. The pass is
  bounded at `MAX_ERASE_WRITES_PER_TRANSACTION` (400, under Firestore's 500 writes) and the caller
  LOOPS, rebuilding the query each pass — the tombstone clears `device_id`, which is what drops a
  journey out of the query and therefore what ends the loop — with the device document deleted in
  the pass that drains the last journey, so a retry after a partial one still finds the rest.
  **Both reads come first**: Firestore raises `ReadAfterWriteError` on a read below a write in a
  transaction, so reading the device under the tombstone loop raised on every erasure that had
  anything to erase. The emulator caught that; the hermetic fake records op order but does not
  enforce the client's rule, so the check is now explicit.
  **`_mark_notified_body` and `_record_push_body` deliberately do NOT stamp the mirror**, for
  `_sent_body`'s reason one channel over: one push on a corridor with a hundred thousand
  registrations would be a hundred thousand documents through the watermark query. **The
  consequence is a constraint on U13 and is written at the declaration so U13 finds it — the cycle
  cannot read `notified` or `pushed_trains` from the mirror**, because a stale mark re-offers a
  train already pushed; those reads go to the store, per push. Two smaller changes ride with them:
  `daily_pushes` and `pushed_trains` become DURABLE where they were in process memory, so a restart
  no longer hands every registration a fresh four and a confirmation after one still says
  «Bekreftet:».
  What U12 deliberately did **not** build: a `create_web_push_journey` body. `create_subscription`
  already is one, and a second create would mean a second ceiling check, a second idempotence rule
  and a second place that stamps the mirror — three chances to disagree with a function that does
  all of it — over a `channel` value the spec carries anyway. A journey is deleted by
  `delete_subscription` for the same reason. `max_devices` joins `Quotas` **declared and unread**
  until U13, in `concurrent_deliveries`' own pattern, with a test that asserts it is still unread so
  it cannot become decoration.
  **Delivery left the poller and became its own workload** (U11, 2026-09-13, KTD18). It was a step
  of `main._rebuild_all`'s `notification_webhooks` phase, which put it behind ~30 s of BigQuery on a
  loop whose period is how often SIRI-SX moves — ~296 s — while a pending event is valid for an
  hour: at a few hundred subscriptions fine, at a hundred thousand a backlog that cannot drain in
  the ~20 s the phase allows it, expiring undelivered while the poller scans ET partitions.
  `disruptions/api/webhook_worker.py` is a `python -m` entrypoint that loops `deliver_pending`,
  sleeps three seconds on an empty queue and **not at all** when a round found work (the read is
  bounded per round, so sleeping would cap the drain at one page per sleep). It fails **loudly at
  startup** on a missing database or signing root — exit non-zero, so `helm --wait` rolls the
  release back — and **never at runtime**: a store error sleeps twenty seconds and the loop
  continues, because a worker that exits on a Firestore blip is a CrashLoopBackOff whose backoff is
  the outage. Its `/health` is `main`'s staleness rule (`HEALTH_STALE_AFTER_S`, the same variable)
  on port **8081**, minus the dead-thread branch it cannot need — the loop IS the main thread.
  SIGTERM sets an Event read BETWEEN rounds, never inside one, and
  `terminationGracePeriodSeconds` is 75 s so a round finishes; below that, SIGKILL lands mid-round
  and U8's lease is what makes it safe (the claims return when the lease lapses, at the cost of a
  re-send).
  **The common chart renders exactly ONE Deployment per release** — `containers` is a list of
  containers in one POD, and the changelog's "multiple deployments" is the `releaseName` override —
  so this is a second RELEASE, `kix-avvik-worker`, of the same chart and the same image, with
  `helm/kix-avvik/env/worker-values-kub-ent-{dev,tst,prd}.yaml` and its own `cd.yml` job. Checked
  against the vendored chart before designing around it, and the same read answered two smaller
  questions: the chart creates **no ServiceAccount at all** (the platform provisions `application`
  in the namespace), so there is no second SA and `.entur/application.yaml` is untouched; and every
  probe port derives from `service.internalPort`, so setting that to 8081 **with the Service off**
  is what puts the probes on the worker's own port. tst's file sets `deployment.enabled: false` and
  renders no workload at all — that project holds neither notification secret and the worker refuses
  to start without the signing root, so a file rendering a guaranteed CrashLoopBackOff would make
  every tst dispatch red for a feature tst does not have. The worker's secret group is
  `WEBHOOK_SIGNING_ROOT` **alone**: delivery signs but never verifies a credential, so it gets no
  pepper, no VAPID keys and no SSO secret. `helm lint` cannot see any of that — every override that
  makes this file a worker's is NEGATIVE, and a negative that stops holding renders valid YAML —
  so `helm/kix-avvik/test/worker_render.sh` renders all three environments, asserts both directions
  and gates the image build; it FAILS rather than skips when helm is absent.
  **Which process delivers is `NOTIFICATION_DELIVERY_WORKER_ENABLED`, read by `main` and in the
  OPPOSITE direction to every other flag here**: on means this process does less, so unset — the
  code default — keeps the poller delivering. That polarity is the rollout. The worker release lands
  first with the poller still delivering; **both deliver for a window**, which is deliberate (U8's
  lease arbitrates, and `leased`/`lease_lost` on `webhook_delivery_round` become reachable for the
  first time — seeing them is the evidence the lease works, and what it costs is that each reader
  applies the per-destination ceiling to its own read, so a destination can briefly see twice its
  `deliveries_per_minute`); only then does a values-only deploy turn the poller's half off. The
  other order leaves NOBODY delivering if the worker never becomes Ready, because gha-helm's
  rollback reaches only the release it deployed. **Emit and the servability sweep do not move**:
  emit is KTD6's one writer of a subject's phase and revision, and the sweep is a BigQuery question
  the worker has no client for.
  **KTD6's "one writer" has to be read more precisely from here on**: one writer of a subject's
  PHASE and REVISION, still this process, with delivery-side fields written elsewhere. The U6 note
  to re-check the `_sent_body` mirror exemption at U11 was acted on and it **stands** —
  `delivered_revision`, `delivered_at` and `pending_delivery_id` are none of them in the standing
  set or in `SUBJECT_MIRRORING_BODIES`, so the `SubjectMirror` is not made stale by a write it
  cannot see; a field moved into the standing set later would break that, which is what
  `SUBJECT_WRITERS_OUTSIDE_THE_MIRROR` is pinned for. The pending-event ledger's figure goes stale
  **HIGH** inside a pass for the same reason (the worker resolves deliveries this process does not
  hear about), so it refuses earlier than it needs to — the conservative direction; what would break
  its exactness is a second EMITTER.
  **`deliveries_per_minute` means per minute since U11, and it did not quite before.** The
  per-destination ceiling is applied inside `pending_deliveries`' read; under the poller that was
  once per ~296 s cycle, so 50 per read was about ten a minute — under the name, which is why
  nothing said otherwise. The worker's rounds are continuous while there is work, so the same
  ceiling per read would be ~600/min at one receiver: twelve times the published number, whose
  first visible effect would be the receiver's own 429 and U7's backoff, i.e. a self-inflicted
  outage that reads as a misbehaving client. `webhook_delivery` keeps a rolling per-destination
  minute tally (`_DESTINATION_WINDOW_S`) and subtracts it from the ceiling before the read admits a
  row. Charged on the page the pass is ABOUT TO ATTEMPT, after the stale sweep — a sweep that hits
  its bounded wait empties the shard list and nothing is dialled, so a charge beside the read would
  spend a destination's whole minute for zero POSTs and keep refusing it for a minute after the
  store recovered. It still over-counts by whatever the pass skips mid-flight, which is the safe
  direction. It is a no-op for a poller-only deployment (a cycle
  is longer than the window), and it is PER PROCESS, so N replicas are N times the rate exactly as
  the drain formula's `× replicas` says. Like every module budget here it joins the suite's reset
  (`webhook_delivery.reset_destination_window`), or a leaked tally reads exactly like a real
  refusal.
  Two new series: `kix_worker_last_tick_timestamp_seconds` (liveness, set on every round including
  the empty ones, **absent from the API pods** — so an alert on it must count no data as firing, or
  a worker scaled to zero reads as healthy) and `kix_delivery_backlog_age_seconds` (how long ago the
  oldest **deliverable** pending delivery was minted; a leased row, a backed-off row and a row past
  a destination's ceiling are all absent from it, so one unwell receiver cannot drive it). The age
  comes off `created_at`, which `_mint` already wrote and nothing read back; a delivery minted
  before U11 carries none and contributes nothing, so the gauge climbs from zero rather than
  reporting the whole backlog as infinitely old on the deploy that introduces it. The drain formula,
  the rollback and how to scale replicas are in the operations document's "The delivery worker"
  section; scaling is `deployment.forceReplicas` **by hand**, because the `kix_*` series are in
  Thanos rather than in Google Managed Prometheus and common's HPA on custom metrics cannot see
  them.
  **Six never-queried fields are exempt from Firestore's automatic single-field indexes** — `payload`
  and `situations` on `events`, `situations` on `deliveries`, and `delivered`, `daily_deliveries`
  and `daily_reservations` on `subscriptions`. Every field is indexed ascending and descending
  automatically, and every element of an array separately, so each was paid for on every write by a
  query that does not exist. A map's exemption covers its subfields, which is what makes one
  resource per map enough (the keys are alert ids and Oslo dates, so a per-key resource could not be
  written). The precondition is the SPEC TABLE and a test checks it against `QUERY_SPECS` in both
  directions, because both failures are silent: `retry_not_before` exempted makes `pending_deliveries`
  return nothing at all, and an exempted `expires_at` stops TTL finding documents. Note `index_config {}`
  now means opposite things in two resources in `terraform/firestore.tf` — absent on the TTL field
  ("leave the automatic indexes alone") and present on these six ("remove them") — and the two
  comments say so to each other.

  **The R16 prompt-record claim has two halves and only one is closed**, published rather than
  asserted away. A **webhook-only** definition — one absent from `by_journey_key`, which stays
  browser-only for exactly this — is written for through `notification_wording.WEBHOOK`, which
  suppresses the prompt/reply DEBUG record **in code** regardless of `VARSEL_LLM_LOG_PROMPTS` (dev has
  it on) and drops `message` from the INFO record. A **shared** definition stays on the browser policy
  unavoidably: one wording between the channels means one call and it is the browser's, and that
  record would exist with no webhook channel at all. So R16 is about webhook-only routes plus every
  other client field; it is **not** "no client route ever appears in the prompt log". Accepted as
  it stands on 2026-09-09: dashboard authentication, offered as a reason to drop R16 altogether, does
  not reach this half — the record is in Cloud Logging, which project IAM gates — and the code behind
  it is one field (`caller`) on the webhook wording policy.
  **Decide in the pass, deliver in the pass, after the browser cycle is already durable** (KTD6).
  Emit and deliver are their own `main._rebuild_all` phase (`notification_webhooks`) placed after the
  `varsel_notify` phase and fed by a `PassOutcome` that `check_and_notify` returns — deliberately
  stronger than "after `_record_decisions` and `_save_state`", since running after the function has
  *returned* means a raise cannot unwind either write at all. A return value rather than a
  module-level snapshot, because a leaked half-cycle snapshot reads exactly like the give-up bug the
  stale sweep exists to prevent. The read-only load step necessarily precedes pass 1a and reports
  `event="varsel_webhook_load"`. Every bound is a number: one attempt per pending event per cycle
  with the next cycle as the retry; a **4-wide** pool (`/meldinger`'s width, the repo's narrowest, not
  `varsel`'s 16/16/32); 5 s per attempt and 2 s connect; 4 s per Firestore call, under `varsel`'s 5 s
  wait so a hung call surfaces as the store's own retryable `DeadlineExceeded` rather than as the
  caller's timeout with a thread still running; and deadlines that **reserve** the worst case rather
  than check a clock. The per-destination skip is **pass-local**, the documented exception to the
  stale-sweep rule: swept, one failure would pause a destination permanently, which is
  indistinguishable from the intended 410 pause.
  **The store's own rules, each with the failure that motivated it.** Four collections, all **top
  level** carrying a subscription id — a document delete does not remove subcollections and a TTL
  field does not cascade into them, so subcollections would be orphaned by the deletion path and the
  delivery query would keep returning them. **TTL is a retention floor and never an access rule**:
  Firestore deletes within roughly 24 h and expired documents stay queryable until it does, so every
  read path tests `expires_at` itself and a *missing* one reads as expired. **A transaction body may
  run more than once**, so every `_*_body` takes its clock and its revision seed as arguments, reads
  before it writes, returns rather than appending to an outer list, and logs nothing — the suite runs
  each body twice against one fake transaction with a clock that raises and a logger that raises on
  attribute access. `alert_id`'s grain is **(subscription, train)**, not per train: a per-train id
  would make the supplied receiver's "drop a revision not strictly newer" rule suppress the *second*
  commuter's notification about the same train. **A revision is minted only on a lifecycle
  transition** — a changed probability, delay figure or wording mints nothing at any size, because
  minting on a changed figure would turn every cycle into a fresh revision and a second notification
  about one train. Monotonicity is structural: a UTC instant captured once outside the transaction,
  clamped inside to `max(stored + 1, derived)` — an Oslo-formatted revision goes backwards for an hour
  at every autumn DST fall-back and the receiver then silently discards every event for those
  subjects, a confirmed cancellation included.
  **The daily cap is `/varsel`'s, at `/varsel`'s grain**, four per subscription per Oslo day, spent
  soonest-departure-first and spent **on success** — a dead destination must not burn a subscription's
  four. Two things about it are published because they are not what the requirement's wording implies.
  `cap_exempt` is decided **at mint, not at send**, so a `confirmed` minted before its `predicted` was
  ever delivered is *not* exempt: it is the first notification about that train. And the exemption is
  **generalised beyond "a confirmation"** to any replacement of a *delivered* revision, which includes
  a **withdrawal** — a retraction the cap suppressed leaves the traveller holding a warning we have
  withdrawn, the friskmelding argument. That widening is **an open product-owner question**, flagged
  in the operations document rather than presented as settled.
  **The cap, the active-subscription ceiling and the per-minute rate are per-client DEFAULTS since
  2026-09-09** (`notification_auth.Quotas`, three fields on the `clients` document seeded from the
  constants, bounded by `QUOTA_BOUNDS` at 1 000 / 10 000 / 10 000, set with the operator tool's
  `set-quotas` and read on every request through the cached account lookup, so they ride on the
  `Principal` for free). The cap is copied onto each subscription document (`daily_cap`) at create,
  replace and touch — the idempotent re-PUT included — which is where `cap_remaining` reads it, so a
  changed cap reaches a subscription at the client's next write and a document without the field
  reads as four. The bound on the subscription ceiling is not arbitrary: `_create_body` counts a
  client's live subscriptions by READING them inside the create transaction, so a ceiling above
  10 000 needs the `count()` aggregation named in the operations document's "Scaling beyond the
  pilot" section, which lists what has to move before 100 000 commuters (matcher per place pair,
  mirror instead of full read, emit and delivery as their own Deployment, batched servability, the
  aggregation) and states that none of it touches the wire contract. Every subscription document
  also carries `channel` (`webhook`; `web_push` is reserved for `/varsel`'s move).
  **Since 2026-09-12 that field is a QUERY key, not just a label** (U5 of the 100k plan, the
  precondition for U12-U14 moving `/varsel`'s own registrations into the same collection). Two of
  the three subscription queries gained a `channel` equality — `load_active_subscriptions` (the
  poller's per-cycle read) and `subscription_count` (the client's create-time ceiling) — and the
  listing deliberately did not, because a client lists what it created and until U13 everything it
  can create is a webhook. Both are per-channel *questions*: a page bounded by a ceiling would
  otherwise let a `web_push` majority truncate the webhook recipients out of the load, and a
  traveller's own registration would spend a client's webhook ceiling on a collection they do not
  own. The equality is **in the query and not after the fact** for the first of those reasons, and
  `varsel._parse_webhook_recipient` restates it as a second, independent refusal (`UnroutableChannel`,
  counted as `unroutable` on `varsel_webhook_load` and deliberately NOT as `skipped` — a declined
  channel is not an unreadable document, and from U12 a warning naming tens of thousands of healthy
  ones is how a real warning stops being read). Two statements is the right number because U6
  replaces that query with a mirror filtering in Python: "the load path forgot the filter" then has a
  private commute arriving at a client's webhook as its symptom. Three traps. A Firestore equality
  **does not match a document that LACKS the field**, so the filter silently retires pre-field
  documents — checked rather than assumed before shipping, 18 of 18 in dev carry it and prd holds
  none — and both the store's reader and the parser read a missing or empty value as `webhook`, which
  is what every document written before the field meant. Adding an equality **replaces** the index
  rather than adding one beside it (Firestore serves a query from an index whose equality fields
  match and whose first ordering is the range field), so the terraform plan shows a destroy and a
  create per changed query, and between that apply and the pod rollout the running binary's own load
  query has no index — guarded, logged, and at a pilot with one dev smoke client and no prd client
  it costs nothing, but it is not nothing at scale. **Measured on the dev apply, 2026-09-12: the
  window was 7 minutes**, not seconds — the destroys completed in 3-4 s and each create took
  **7m01s**, because Firestore builds an index over the existing collection before it is usable. Two
  poller cycles logged `varsel_webhook_load` `result="error"` inside it (18:51:17, 18:53:11), the
  browser half of the pass was untouched (the cycle completed in 52.7 s), and the load recovered by
  itself at 18:58:32 — `ok`, all 18 recipients, `unroutable: 0`. So the shape to expect from any
  later query change here is **~7 minutes of a guarded, logged outage on that one read**, and the
  order is fixed by the workflows rather than chosen: `terraform.yml` and `cd.yml` both fire on the
  merge, so the index is destroyed while the OLD binary is still serving. If a future change cannot
  afford that window, the way out is two merges — add the new index in one, switch the query in the
  next — not a faster apply. And `SubscriptionSpec.channel` is validated
  against `CHANNELS` at create: unvalidated, a typo writes a document that no read path's equality
  matches — a subscription that exists, is billed for, and is never loaded or counted by anything.
  **Four semantics moved on 2026-09-10, on the full review's findings, and each is the kind a later
  reader would otherwise assume the other way.** (1) The delivered marks on a subscription document
  are keyed on the **`alert_id`** — one per delivered (subscription, train) — not on situation
  numbers: the store mints per train, so `varsel._pending_pushes` asks per train for a webhook
  recipient (`_webhook_pending`, through `notification_events.alert_id_for`, the same derivation the
  emit step's `train_ref` uses), while the browser ledger `_notified` stays per situation because a
  browser is pushed once per situation. Per-situation marks refused the wording pass to a second
  departure under a delivered situation whose event still went out. No migration: the channel is off
  everywhere. (2) `valid_until`'s departure term is the **expected** departure (aimed plus predicted
  typical delay, `WebhookSubject.expected_departure_time`), never earlier than `DEPARTURE_GRACE_S`
  (120 s) past the emit instant (a term less than that far ahead, already past included, is floored
  to it) — the matcher keeps a late train on exactly that figure, and the aimed-time clamp minted
  events that were stale before their first attempt. (3) The create-time
  ceiling counts through its own `subscription_count` query ordered `expires_at DESC` (a seventh
  composite index): through the listing's `created_at ASC` the page filled with expired rows first
  and under-counted. (4) The webhook event's route line always carries the stop **registry's** names,
  never a browser registration's typed `from_name`/`to_name` — unauthenticated free text that reached
  a client's notification through a shared definition; `test_varsel_api` now wires the registry
  provider exactly as `main` does. Beside those: `pending_deliveries` pages past a throttled
  destination up to `MAX_DELIVERY_SCAN_PER_CYCLE`, the served-subjects bound and the list ceiling
  follow the client's quota (`subjects.maxItems` is `QUOTA_BOUNDS["daily_cap"]`'s 1 000), the
  servability re-check reuses the pass's read of `subscriptions` when under `_LOAD_REUSE_S` (600 s)
  old, and the delivery attempt's destination lookup goes through `_store_step` like every other read.
  **Signing is Standard Webhooks, fresh per attempt, per destination** (KTD7). HMAC-SHA256 over
  `webhook-id.webhook-timestamp.<raw body>` with stdlib `hmac` and no new dependency; the body bytes
  are stored once and signed unmodified, because re-serializing signs a document the receiver did not
  get. **The `webhook-id` is the delivery id and is what gets persisted; never persist the timestamp
  or the signature** — a retry reusing the first attempt's timestamp lands outside the receiver's
  five-minute replay window, where a *correct* receiver drops our own retry as an attack. Each
  destination's secret is HMAC of a root over length-framed `destination_id || generation`, so the
  store holds no signing material and one client's leaked receiver secret forges nothing for another;
  both live generations ride in one header, each revocable alone.
  **The outbound client uses a POSITIVE address test, not a deny-list** (KTD9), and that is not
  belt-and-braces: implemented first as the bare `is_global and not is_multicast`, the NAT64 form
  `64:ff9b::a9fe:a9fe` was **accepted** — global, non-private, non-multicast, `ipv4_mapped` None, and
  in none of the named ranges — while the address it carries is the metadata service. So the same test
  is re-run on whatever a prefix embeds (`::ffff:0:0/96`, `2002::/16`, `64:ff9b::/96`), judging the
  reachable host rather than the notation. The host is resolved **once**, every returned address in
  both families is validated, and the connection dials a validated literal while still sending the
  original SNI and Host — validate-then-request-by-hostname is a time-of-check race, which is how
  CVE-2026-27129 and SVIXSEC-2026-0001 both happened this year. An IPv4-mapped address is judged by
  what it *carries* rather than refused outright; do not "fix" that into a blanket refusal.
  **The API mounts unconditionally**, independent of `NOTIFICATION_WEBHOOKS_ENABLED`, because gating
  onboarding on the flag that needs onboarding is circular — availability is store plus pepper, read
  per call, so tst answers 503 today. The consequence was put to the product owner and decided on
  2026-09-09 — this is a public API:
  **`/notification-api/v1` appears in the public `/docs`**, since `docs_url` is FastAPI's default, and
  unlike Rushvarsel nothing here asks for the product to be unmentioned. The prefix is deliberately
  **not** in `sso.PROTECTED_PREFIXES`: that gate answers an unconfigured deployment with 503 and an
  unauthenticated browser with a 302, and a machine caller following a redirect into an Entra login
  screen gets HTML where it expected JSON. `/test-events` writes **nothing at all**, because the
  store's pending-event ceiling is exact rather than best-effort *only because the emit path has one
  writer* — one poller thread, no route invoking the pass, one replica under Recreate — and a route
  calling `commit_event` would falsify every quota check in that module.
  **What a reader sees in the logs**: `varsel_webhook_load`, `notification_store_load`, `notification_emit`,
  `webhook_delivery_round`, `webhook_delivery`, `notification_quota`,
  `notification_subscription`, `notification_cleanup`, `notification_auth`, `notification_admin`, `notification_test_event`,
  `notification_servability`, `notification_alert_state`, `webhook_delivery_refused`, `webhook_worker`,
  `webhook_delivery_response_truncated`, `notification_auth_config` / `webhook_signing_config`,
  `notification_wording`, and `poller_phase` with `phase="notification_webhooks"` **or
  `"notification_servability"`** — two phases, not one. `docs/operations/notification-webhooks.md` carries
  the table of what each field means and is grep-pinned against the code by
  `test_notification_contract.py::test_the_operations_document_names_every_notification_log_event`; **nothing
  pins this copy of the list**, so treat that table as the authority and this sentence as a summary
  that can lag it.
  No **`notification_*` or `webhook_*`** log line carries an origin, a destination, a departure time, a full callback
  URL, a credential secret or any prose. **Three** things are carried and named rather than glossed,
  all inside that namespace: `subscription_id` embeds the client's own reference,
  `webhook_delivery_refused.reason` can carry a refused destination's hostname and its resolved
  addresses, and `notification_admin.actor` is a named human.
  **There was a FOURTH, deliberately outside that namespace, and U10 closed it** (2026-09-13). A
  webhook subscription's origin and destination reached Cloud Logging under `/varsel`'s event name:
  `main._varsel_connectivity` wrote both stop refs into the *message* of its success and failure
  records (`event="varsel_service_check"`), and `varsel._service_verdict`'s own error path did the
  same — three carriers, all `/varsel`'s rather than this channel's, which is exactly why the claim
  above was true as stated and would have misled anybody who stopped there. They carry
  `alternatives.pair_digest` now, salted per process because an unsalted SHA-256 over two public
  enumerable refs is a reversible pseudonym; a BATCH writes one summary record with counts and no
  digest at all. The fix was `/varsel`'s log line rather than this channel's, as the old paragraph
  predicted it would have to be. App logs live in the **cluster** project
  (`ent-kub-{dev,prd}`, namespace `kix-avvik`), not the app project.
  **`NOTIFICATION_WEBHOOK_LLM_ENABLED` is read at IMPORT** and therefore needs a restart, unlike the
  other three flags; it defaults off and is set in **dev and prd** (both since 2026-09-10, for the
  public read below, not for the channel — `NOTIFICATION_WEBHOOKS_ENABLED` went on in both later that day,
  so an event in either can carry it), so in tst the channel would emit and deliver the deterministic template until
  somebody adds it. Expect one divergence during the rollout window: with `VARSEL_LLM_ENABLED=true`
  beside it false, the push carries the model's prose while the event carries the template, so the
  two channels are byte-identical only where both switches agree.
  **The journey-keyed public read** (`GET /notification-api/v1/journey-alerts`,
  `disruptions/api/notification_pull.py`, plan 2026-09-10-001) is the same selection and the same
  payload assembly with **no registration and no credential**: the Entur app's favourites panel
  asks for a saved route — five fields, `from_stop`/`to_stop` canonicalized, optional Oslo
  `begin`/`end`, optional repeated `days` — and gets one entry per alert-worthy train ahead,
  soonest expected departure first with a worst-first `severity` rank, each carrying the webhook
  event's own `payload`. Its own `APIRouter` on the shared prefix with **no router-level
  dependency** (`notification_api.router` carries `_authenticated` and `_throttled` at router
  level, which is exactly why a public route cannot live on it), `async def` like `/meldinger`,
  deliberately **not** in `sso.PROTECTED_PREFIXES`, and published in the same `openapi.json` with
  operation-level `security: []` and a fourth tag. **Two phases, three caches, one assembly per
  request**: the figures (`match_journey` → `alert_worthy_trains`) are cached per
  `(journey_key, refreshed_at, Oslo day)` with single-flight and evicted when the poller's load
  moves, and the enrichment — one background task per definition per day on the module's own
  `WORKERS`-wide pool that re-runs the matcher with Journey Planner lookups and words the soonest
  `MAX_WORDINGS_PER_READ` trains — fills a per-train wording cache (`WORDING_TTL_S`, keyed on pair,
  situation, departure, `cancelled`, `i_morgen`, alternative identity and `PROMPT_VERSION`) and a
  per-train alternative cache (`alternatives._TTL`), while the request waits at most `wait`
  (≤ `MAX_WAIT_S`) seconds and assembles whatever is ready, with `complete` deciding
  `Cache-Control` (`public, max-age=60` / `no-store`). **Its own limiter and allowances, never the
  poller's**: `MAX_PER_MIN` global and `PER_PAIR_PER_MIN` per canonical pair, wordings through
  `notification_wording.PULL` (its own `WORDINGS_PER_MIN/HOUR/DAY` ledger and its own latch holder)
  and Journey Planner fetches on `alternatives._PULL_PATH` (`_PULL_FETCH_MAX_PER_MIN`) — a read
  burst that moves `spent("browser")` or `spent("webhook")` is a bug.
  **Since U15 (2026-09-13) the PASS warms the figures, and the two ceilings moved with it**:
  `MAX_PER_MIN` 300 → 3 000 (R6's 50/s stated per minute) and `FIGURES_CACHE_MAX` 512 → 20 000, the
  second because at 512 the warm pass evicts its own oldest entries before a read arrives. Pass 1a
  hands each definition's alerts to `notification_pull.warm_figures` through a callable `main`
  injects as `varsel.configure(figures_warmer=...)` — injected and not imported because
  `notification_pull` imports `varsel`, a plan stop condition pinned both ways — so a favourite that
  IS a registered definition, on EITHER channel, is a figures hit for every caller for the whole
  cycle. Absent the callable, pass 1a is byte-for-byte what it was, which is what every suite but
  two asserts by never noticing it. **The ceiling is not a throughput and the document says so**:
  warming raises the HIT RATE and does not make a miss cheaper — U2's index did that. Measured
  2026-09-13 with `scripts/bench_matcher.py --pull-read` at 20 000 definitions: a miss **0.32 ms**
  (it was 15 ms p50 in dev before U2, which is where 300/min came from), a hit **0.017 ms**, warming a
  few per cent of the pass, and ~**3 000** misses per second sustainable against R6's 50. The 50 rps run
  against a real dev process is still owed. Three smaller things this unit had to get right, each
  silent if taken the other way. **`_enriched` now also clears `impact.alternative_journey`**:
  `llm_message.build_message_input` falls back to it, and every alert the pass produces carries one
  (`with_alternative=True`), so a warmed winner would otherwise put an `alternativ_reise` into the
  message input — and so into the wording key and the payload — that the same definition's answer on
  a cold cycle does not have. **The third forget rule pops by a `(pair, train)` index** rather than
  scanning the whole wording cache, because that scan ran on a read miss (a handful a minute) and
  now runs once per registered definition per cycle; the index is derived and REBUILT rather than
  evicted, since evicting it alone would silently retire the rule. And **`coverage`'s stop-place set
  is memoised on the identity of the stop-cache list**, not on the prediction generation the plan
  proposed: coverage is a function of the stop cache and the two refresh on different TTLs, so
  identity is both cheaper and strictly fresher. **It never writes poller
  state** (`_messages`, `_alternatives`, `_notified`, `varsel_decisions`, Firestore): the one
  accessor it reads through is `varsel.cached_wording_and_alternative`, read-only by contract. A
  cold prediction cache is **503 `predictions_not_loaded`, never an empty list**. **Byte-identity
  has one boundary**: where the five fields are verbatim a registration's and the pass has worded
  the train, the entry serves the poller's wording and `payload.notification` equals the event's
  byte for byte; the figures are live where the event's were its mint snapshot, the alternative can
  differ in the one case the operations document publishes, and everywhere else the wording is the
  read's own draw or the template — and the entry never says whether the definition is
  registered. **Log redaction is R11**: `notification_pull` / `notification_pull_bounded` carry no
  ref, name, departure time or prose, only `pair_digest`, the prompt record is suppressed in code,
  and `logging_setup._AccessLogRedaction` strips the query string from the access log for this
  path. **A definition nobody registered is unscored** — no decision row (KTD10), the fifth KTD12
  limitation — and an empty list means no alert-worthy train among the trains the service scores,
  never all-clear. **The bad-text revert is now two switches** beside `/varsel`'s: `VARSEL_LLM_ENABLED=false`
  returns the *push* to the template and reaches neither the read nor the event; those follow
  `NOTIFICATION_WEBHOOK_LLM_ENABLED`, so `false` **plus a restart** is their revert (`disabled` on
  every entry, template served, correct and complete); `VARSEL_LLM_PROVIDER=off` stops every
  surface. Three accepted exposures are published in both documents rather than closed: a first
  read's completeness reveals that a matching registration exists, the ceiling and allowances are
  shared with every anonymous caller, and the definition rides in the query string outside the
  process's own redaction.
  **Infrastructure is this repo's own Terraform** (`terraform/firestore.tf`, the directory's first
  non-IAM resource): the API, one named Firestore database per environment in `europe-west1`
  (immutable), seven composite indexes materialised with `for_each` keyed on each index's **content**
  rather than its array position, the four TTL field configs, and a prd-only backup schedule (five
  weeks, a chosen value: Firestore allows 14 weeks on both recurrences — checked 2026-09-09 — and
  five is what PITR's seven days does not already cover). **The deny-all `firestore.rules` is
  deliberately NOT released by Terraform** (2026-09-10). It was, for one day — `google_firebaserules_ruleset`
  + `_release`, named `cloud.firestore/<database>` per firebase-tools' own release code, and the
  2026-09-09 apply accepted that name from a project Firebase has never touched, so that question is
  answered — but refreshing the ruleset on every later plan needs `firebaserules.rulesets.get`, and
  **the pull-request plan runs as a different identity than the push apply**:
  `gh-kix-avvik-f849-ci@ent-github-shr` (`roles/viewer` + the org's `githubCiTerraformPlanReader`)
  lacks it, `…-dev@` (`roles/firebase.admin`) has it. So #139's plans and apply passed (a resource not
  yet in state needs no get) and every Terraform PR after it failed its plan — #142 had to drop its
  Terraform change to merge. Rules govern client-SDK access only, no Firebase app exists in these
  projects, and a database with no release denies every client anyway, so the guard guarded nothing.
  Two `removed { destroy = false }` blocks forget the pair (dev's live release stays deny-all,
  unmanaged; tst/prd never held it) — and a `removed` block is what made that plannable: Terraform
  does not refresh a resource it is forgetting (verified on #143's plan), whereas deleting the
  blocks outright plans a destroy, which refreshes first. Keep that in mind for the next resource a
  plan cannot read. Re-add them, with the Team Plattform ask for the plan-reader permission, the
  day a Firebase app is registered here. `test_notification_store` pins the absence. The
  runtime `roles/datastore.user` grant is the one part that **cannot** go there — a
  `google_project_iam_member` runs under a `projectIamAdmin` conditioned by `only_assignable_roles`,
  which carries no `roles/datastore.*` (the same condition refused the Google-OAuth
  `roles/oauthconfig.editor` grant `terraform/oauth_iam.tf` carried — parked 2026-08-31, retired
  2026-09-10 with the move to Auth0; its header had read the 403 as an org-level deny policy, which
  refuses with the same wording); it goes in `.entur/application.yaml`, whose filter drops a
  non-matching role **silently**, so the binding is verified after `entur apply` rather than assumed.
  Plan-time index drift rests on one config line that is present: `firestore.indexes.json` is in
  **both** of `terraform.yml`'s `paths:` lists (and `firestore.rules` deliberately is not, since
  nothing in `terraform/` consumes it), without which a PR adding a query and its index would
  pass the static test, merge, apply nothing and land a runtime `FailedPrecondition`.
  **The two TypeScript packages ship from here and are vendored, not published** (KTD11). Declared
  consumption shape: compiled CommonJS plus `.d.ts` on Node 24, and `package.json` deliberately
  declares **no `type` field** — with none, Node's syntax detection loads a `.ts` source as ESM (what
  `node --test` needs) while treating `dist/*.js` as CommonJS (what the artifact needs); an explicit
  `"commonjs"`, which `npm install` adds if allowed, kills the `.ts` path and `"module"` kills the CJS
  emit. Both were observed and a test pins the omission. Two answers only the TypeScript side knows:
  `firebase-functions@7.3.2`'s `retry` is reachable **only** through the options-object overload of
  `onDocumentCreated`, and the `document: string` overload every quickstart shows produces
  `retry === false` — not an absent field — so it deploys, runs, and silently drops events on the
  first store blip; and the shipped `firestore.rules` is **necessary and not sufficient**, because
  Firestore evaluates rules as a **union** and a deny cannot override an allow, so a client merging
  the four blocks into a ruleset carrying a permissive catch-all still has an open database.
  **The two emulator suites cannot be started at the same instant**: this repo's root `firebase.json`
  binds Firestore on 8571 with the hub on its default 4400, and the client package pins 8572 / 9151 /
  4401 and its own demo project id, which makes the *sequential* case safe (CI's shape) but not two
  simultaneous starts — the contention is inside `firebase-tools`' own hub startup.
