---
title: Shared Translation Store - Plan
type: feat
date: 2026-10-22
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Shared Translation Store - Plan

## Goal Capsule

- **Objective:** Every API pod serves the same English and nynorsk for a situation, and the same
  `/varsel` card wording and alternative journey for a train, from the moment the leader has them.
  A deploy or lease handoff no longer starts any of them from zero.
- **Authority:** This plan, then the repo's conventions (`CLAUDE.md`, `.claude/rules/*.md`), then the
  implementer's judgment. Decisions marked `session-settled` are not re-opened; D1-D6 were decided
  by the user on 2026-10-22 (see Decisions).
- **Execution profile:** Code in `disruptions/api/` (`situation_english.py`, `notification_store.py`,
  `main.py`, and for U7 `varsel.py` and `alternatives.py`), Terraform in `terraform/firestore.tf`, `firestore.indexes.json`, tests, documents. dev
  on merge; prd's Terraform by dispatch **before** prd's `cd.yml` dispatch (the order the
  zero-downtime rollout slipped on 2026-10-20).
- **Stop conditions:**
  - Do not call Firestore on a request path: reads serve from memory only.
  - Do not let a follower call Gemini or the Journey Planner.
  - Do not let a follower run the alerting pass or `varsel._reconcile_caches`: its card caches are a
    read-only copy of the leader's.
  - Do not let a store failure fail a lease acquisition, a follower tick or the lifespan.
  - Do not change the webhook wire contract.
  - Do not put translation text in any index.
  - Do not run Firestore or Vertex from a hermetic test.
- **Tail ownership:** This repository owns every unit. The prd Terraform dispatch is the user's.

---

## Product Contract

### Summary

Machine translations (Gemini's English and nynorsk of an operator's Norwegian situation text) move
from one pod's memory into a Firestore collection, `situation_translations`, in the existing
`notifications` database. The lease holder writes each translation once. Every pod keeps an
in-memory copy that it seeds at startup and on acquisition and syncs on its follower tick. Requests
read memory as they do today.

### Problem Frame

`situation_english` has three sources (module docstring). Two of them are on the prediction tables
every pod loads, so they are already the same on every pod:
- the operator's own English (Vy, Ruter);
- the English and nynorsk that `predict_calls*.sql` writes for `KIX:` rows.

The third, Gemini's translation, exists only in the lease holder's `_translated` cache:
`main.refresh_call_predictions_cache` passes `translate=False` on a follower (KTD12 of the
zero-downtime plan). It covers **every operator situation's nynorsk** (no operator publishes
nynorsk) and the English of GCO, MOR, NOR, FIN and TRO.

prd runs two pods behind one Service since step B (2026-10-22). Two consequences follow:

1. **Flip-flop.** A client asking journey-alerts or journey-status twice for the same situation
   gets `summary_nn` from the leader and `null` from the follower, depending on which pod answered.
   `/varsel`, the public reads and the decision log all read these fields.
2. **Cold restarts, now more often.** The cache is process memory, so every deploy, eviction and
   handoff empties it, the leader's included. The module's own sizing note says a restart finds
   about 1 100 live situations untranslated and takes about half an hour to drain them at
   `MAX_TRANSLATIONS_PER_UPDATE` = 100 per cycle. A webhook event minted inside that window carries
   null translations for good, because a changed field mints nothing (the phase rule).

### Requirements

- **R1** A translation the leader has written is served by every pod within one follower tick
  (60 s) plus the sync's own duration.
- **R2** A pod that starts, or acquires the lease, serves every stored, unexpired translation
  before its first cycle. It does not wait for Gemini.
- **R3** Gemini is asked at most once per distinct (text, prompt version, model) across all pods
  and restarts, apart from the documented race in KTD4.
- **R4** Two pods never serve different machine wording for the same text and version once both
  have synced.
- **R5** A Firestore outage degrades to today's behaviour (the leader translates into memory,
  followers serve null) and never blocks serving, the cycle or the lease.
- **R6** Documents expire, and translation text is never indexed.
- **R7** Each pod reports its translation coverage, so a regression or a stalled sync is visible
  (KTD7).
- **R8** (D2) A follower's `/varsel` card shows the wording and the alternative journey the leader
  holds for that (place pair, train), within one follower tick of the leader's change, through the
  same delivery gate (`_deliverable_message`) and catchability check (`_still_catchable`) as the
  leader's.
- **R9** (D2) A wording or alternative the leader DROPS (`_forget_message`, the supersede and
  catchability pops in `_reconcile_caches`, the stale sweep) disappears from every follower within
  one tick.
- **R10** (D2) A new leader starts from the stored wordings and alternatives (user, 2026-10-22). Its
  first cycle's reconcile drops whatever is stale, as it would for its own entries.

### Acceptance Examples

- **AE1** With prd at two pods after U3 has deployed: 20 journey-alerts reads of one definition
  carrying a GCO situation all return the same non-null `summary_nn`.
- **AE2** Deleting the leader pod: the successor's first `poller_cycle` logs
  `situation_english.submitted` at or near 0, not about 100.
- **AE3** Firestore unreachable in a hermetic test: the leader still translates into memory, the
  follower tick returns normally, and acquisition succeeds.
- **AE4** A changed `_PROMPT` gives a new version key. The new text is requested once, and the old
  documents expire unread.

### Scope Boundaries

**In scope:**
- the machine-translation cache (`situation_english._translated`);
- the coverage signal;
- the `/varsel` card's wording and alternative caches (`varsel._messages`, `_message_meta`,
  `_alternatives`), D2.

**Out of scope, and why** (each also listed under Decisions):
- a shared store for the journey-alerts read's own wording (D1);
- the null translations on events minted before a translation lands (D5);
- a nynorsk review (D6, closed).

### Success Criteria

- In prd, the U1 flip-flop rate after U3 is zero within one tick of a translation landing.
- Gemini translation calls after a deploy drop from about 100 per cycle to near zero (the
  `situation_english` log's `submitted`).
- A `/varsel` card served by the follower carries the same wording and alternative as the leader's
  within one tick, and the first cycle after a handoff makes no Journey Planner fetch or wording call
  for a (pair, train) the store already holds.

---

## Planning Contract

### Key Technical Decisions

- **KTD1, session-settled: Firestore, in the existing `notifications` database.** Rejected:
  - *Followers translate too* (`translate=True` everywhere): Gemini cost × replicas, still cold
    after every restart, and two pods produce DIFFERENT wording for one text, so R4 fails by
    construction.
  - *Translate in BigQuery during the rebuild* (`ML.GENERATE_TEXT` into a table `follow_last_build`
    loads): every pod would be identical, but it adds model latency and cost to every cycle, and an
    incremental "translate only what is new" is awkward in SQL.
  - *Memorystore:* a new resource for a few megabytes.

  Firestore already holds the lease, the registrations and the feedback contexts, has the
  server-stamped watermark sync pattern (`SubscriptionMirror`), and its TTL is Terraform's.
- **KTD2: memory stays the read path.** `situation_english.english`/`nynorsk` keep reading `_lock`ed
  dicts. The store is written from the translation pool thread, and read only by seed and sync on
  the poller thread or in the lifespan. Never on a request.
- **KTD3: the key is versioned.** Document id = `sha256(json([TRANSLATION_VERSION, summary,
  description]))`, where `TRANSLATION_VERSION` = the first 8 hex of
  `sha256(_PROMPT + _SCHEMA + llm_message._GEMINI_MODEL)`. The in-memory key is the same digest, so
  the two cannot disagree. A prompt, schema or model change re-translates instead of serving words
  the current prompt would not produce (AE4). The id derives from public operator text only.
- **KTD4: first writer wins, and everyone adopts it.**
  - The write is `create()`, which fails if the document exists.
  - On `AlreadyExists`, the writer re-reads the document and puts the STORED translation into its
    own memory instead of its own.
  - So across a handoff where two leaders briefly translate the same text, every pod converges on
    one wording (R4).
  - The cost is one Gemini call wasted in that race. That is the documented exception to R3.
- **KTD5: the sync is the mirror's pattern, at its smallest.**
  - **Watermark:** a server-stamped `updated_at` (`SERVER_TIMESTAMP`, never `now`, for the reason
    the subscription mirror gives).
  - **Query:** `updated_at > watermark - overlap`, ordered by `updated_at`, paged.
  - **Overlap:** 120 s, the subscription mirror's `MIRROR_SYNC_OVERLAP_S`, for the same
    commit-ordering reason.
  - **Application is by id**, so re-reading is a no-op.
  - **Seed:** the newest `CACHE_MAX` (10 000) by `updated_at` descending.
  - **Indexes:** none needed. Firestore's automatic single-field index on `updated_at` serves both
    directions, so `firestore.indexes.json` gains no composite index. Both queries are still
    declared as `QuerySpec`s, so "every query is a spec" stays true and the bidirectional index test
    covers them.
- **KTD6: expiry by TTL, 30 days from creation, no renewal.**
  - Situations live hours to days. A recurring operator phrase («Ta neste tog») re-translates about
    monthly, which costs one call.
  - Renew-on-use would add a write per use per text. **Decide D4.**
  - `situation_translations` joins the TTL collection list in `terraform/firestore.tf`.
  - The four text fields (`summary_en`, `description_en`, `summary_nn`, `description_nn`, held in
    one map `translations` so a single exemption covers them) join the index exemptions, with the
    matching `firestore.indexes.json` `fieldOverrides`. The existing test checks Terraform against
    the JSON and the specs in both directions.
- **KTD7: coverage is logged per pod, and is not yet alerted.** On each `update`, and on a
  follower's sync, `situation_english` logs `event="situation_english_coverage"` with:
  - the live situations needing a machine translation;
  - how many of them have English and nynorsk;
  - `role`.

  It is a log field rather than a `kix_*` gauge, because the rules that would read it do not exist
  yet. **Decide D3.**
- **KTD8: the store handle is injected.** `situation_english.configure(store=...)` from `main`, set
  only under `main._varsel_store` (the store backend, never `notification_store.enabled()`, which
  is true in tst where the database does not exist). The same pattern as `feedback_contexts`.
  `situation_english` never imports `notification_store`. tst therefore stays memory-only, with its
  single pod.

- **KTD9 (D2): a second collection, `varsel_card_cache`, beside `situation_translations`.**
  - One document per cache entry. The id is a hash of `kind` plus the entry's key: `kind` is
    `message` (keyed by `_message_key`, the wording key) or `alternative` (keyed by
    `_subject_key`).
  - Fields:
    - the key itself, serialized (the pair, situation, departure and the wording key's other
      elements), so a pod can rebuild the tuple;
    - `value`: the message text plus `_message_meta` for a `message`, or the serialized
      `AlternativeJourney` for an `alternative`;
    - `tombstone`;
    - `updated_at` (server-stamped);
    - `expires_at`.
  - Separate from translations because the lifecycle differs: entries are replaced and dropped
    every few cycles, not created once.
- **KTD10: deletes are tombstones** (the mirror lesson). A deleted document matches no query, so the
  watermark sync would never tell a follower that an alternative was superseded, and the follower
  would keep advising it. The leader writes `tombstone: true` with a fresh `updated_at`; a sync
  that reads one pops the entry. TTL: `expires_at` = 24 h after the write, which covers the 12 h
  situation window and lets tombstones age out.
- **KTD11: the leader writes on change, from the places that already change the caches.**
  - The write-through sits beside every assignment to and pop from `_messages`, `_message_meta`
    and `_alternatives`: the wording pass, `_reconcile_caches`, `_forget_message` and the stale
    sweep.
  - Writes are queued and flushed after `check_and_notify` returns, on the poller thread, in one
    bounded batch per cycle. They never happen inside `_state_lock` and never on the push path.
  - A failed flush is logged and dropped. The next change rewrites the entry, and a follower serves
    a stale entry until then, which the catchability check bounds.
  - Volume measured on prd 2026-10-21 (single pod): 1 018 wordings and 654 Journey Planner fetches,
    so at most about 2 000 writes and tombstones a day.
- **KTD12: what the copy deliberately does NOT carry.**
  - The failure budgets (`_message_attempts`, `_message_errors`) and the decision-log ledger stay
    per-leader. A new leader starts those afresh, which re-allows at most three attempts per
    hopeless payload per handoff.
  - `_message_meta` IS carried, because a follower's card render writes a feedback context whose
    snapshot needs `message_input`, provider, model, prompt version and status.
  - `message_input` names stop refs and a departure time. It joins the registrations in the same
    database, under the 24 h TTL.
  - The `varsel.py` comment "In-memory only: a redeploy re-writes messages rather than restoring
    stale advice" is rewritten: a new leader restores and its first reconcile drops what is stale
    (R10). Its first `_record_decisions` then reports `ok_cached` for a restored wording, which is
    what that status means.

### High-Level Technical Design

```
leader, translation pool       Firestore situation_translations/{digest}        every pod, poller thread
_translate_one ──create()──▶   { translations: {en:{s,d}, nn:{s,d}},  ◀──seed──  lifespan, _on_lease_acquired
  AlreadyExists → read, adopt    version, model, created_at,          ◀──sync──  follower tick (watermark)
  └─▶ _translated[digest]        updated_at (server), expires_at }                 └─▶ _translated[digest]
                                                                    requests: english()/nynorsk() → memory only
```

### Risks

| Risk | Mitigation |
|---|---|
| A slow store holds up the poller thread on acquisition or a tick | Bounded per call (`_CALL_DEADLINE_S`) and in pages; the seed beats between pages like `sync_mirrors`; any failure is logged and skipped (R5, AE3) |
| A seed of 10 000 documents on every acquisition | ~10 000 reads (~$0.006) per deploy or handoff; acceptable, revisit only if handoffs become frequent |
| Watermark trap: documents with no `updated_at` | The collection is new; every writer stamps it; the seed orders by it, which is safe for that reason only (the mirror's own warning) |
| Memory | Unchanged: the same `CACHE_MAX` LRU, now filled sooner |

---

## Implementation Units

### U1. Measure in prd first (no code)

Two pods have been live in prd since 2026-10-22. Before building:

1. **Flip-flop rate.** Read journey-alerts and journey-status about 20 times each for three
   definitions that carry operator situations, at least one of them non-Vy. Count the answers with
   `summary_nn` null against non-null, per situation. Expectation: about half null for every
   operator situation.
2. **Volume.** Over the last 7 days of `event="situation_english"` in `ent-kub-prd`:
   - the `submitted` total per day, which sizes the write rate;
   - the `missing` peak, which sizes the seed.
3. **Journey-alerts wording divergence,** as input to D1. For the same reads, compare
   `payload.notification` between answers for one train, and count how often two answers word the
   same train differently.

Record the numbers in this plan's Measurements section. If (1) shows no flip-flop, stop and find out
why before building.

### U2. The store

`notification_store.py`:
- `SITUATION_TRANSLATIONS` in `COLLECTIONS`;
- `write_situation_translation(digest, translations, version, model)`, a `create()`, returning the
  stored translations on `AlreadyExists` (KTD4);
- `seed_situation_translations(limit)` and `sync_situation_translations(watermark)`, both paged and
  bounded;
- two `QuerySpec`s (KTD5);
- `expires_at` = creation + 30 days.

`terraform/firestore.tf`: the collection joins the TTL list, and the `translations` map joins the
exemptions (KTD6). `firestore.indexes.json`: the matching `fieldOverrides` entry.

**Tests:**
- in `test_notification_store.py` (hermetic fake): create, AlreadyExists and adopt, seed order, the
  sync overlap re-application as a no-op, and expiry read as absent;
- the bidirectional spec, index and exemption tests extended;
- one emulator test of create-if-absent through the real client.

### U3. Wiring

`situation_english.py`:
- `configure(store=...)`;
- `_translate_one` writes after a successful call and keeps the adopted result;
- `seed()` and `sync()` merge store entries into `_translated` under `_lock` (LRU order by
  `updated_at`);
- the key becomes the KTD3 digest everywhere.

`main.py`:
- a seed in the lifespan's follower load (best-effort, logged, never raises);
- a seed in `_on_lease_acquired` (wrapped so a store failure is logged and does NOT fail acquisition,
  unlike the mirror seed);
- a sync in `follow_last_build`'s follower tick;
- the injection under `_varsel_store`.

`translate=False` stays on followers.

**Tests:**
- `test_situation_english.py`: a follower serves a translation the leader wrote after one sync
  (R1); a seeded pod serves before any cycle (R2); two writers converge (R4); a raising store
  degrades to memory (R5, AE3); a prompt change misses the old key (AE4).
- `test_poller_lease.py`: acquisition succeeds with a raising translation store.

### U7. `/varsel` card caches on every pod (D2)

After U3 has deployed and its sync has run a day without issue, so U2's store pattern is proven.

`notification_store.py`:
- `VARSEL_CARD_CACHE`;
- `write_card_entries(entries)`, a batched `set` of up to 500 per commit;
- `seed_card_entries()` and `sync_card_entries(watermark)` with the KTD5 overlap;
- QuerySpecs;
- `expires_at` = write + 24 h.

`terraform/firestore.tf`: TTL list and an exemption for `value`; the matching
`firestore.indexes.json` entry.

`alternatives.py`: `AlternativeJourney` round-trips through `model_dump(mode="json")` and
`model_validate`. Pin that with a test; nothing persists it today.

`varsel.py`:
- `_card_writes`, a queue filled beside every cache mutation (KTD11);
- `flush_card_writes()`, called by `main` after the pass;
- `apply_card_entries(entries)`, which sets or pops under `_state_lock` and rebuilds the key
  tuples;
- the injected store handle (KTD8's pattern).

`main.py`:
- the flush after `varsel_notify` (its own `_phase`, so its cost is visible);
- a seed in the lifespan and in `_on_lease_acquired` (best-effort, as U3);
- a sync on the follower tick.

**Tests:**
- every mutation site enqueues: a structural test over the module, the `MIRROR_STAMPING_BODIES`
  set-pin idea;
- a follower that applies the leader's entries renders the same card (R8);
- a tombstone pops (R9);
- a seeded new leader's first cycle reconciles a superseded alternative away and records
  `ok_cached` for a kept wording (R10);
- the store raising changes nothing in the leader's pass, pushes or decision rows;
- `test_varsel_api`'s card tests run once as leader and once as a follower fed from the store.

### U4. Coverage signal

KTD7's log record from `update` and from sync. Test that the counts are right for a mix of provided,
machine-translated and missing texts.

### U5. Documents

- `.claude/rules/notification-webhooks.md`: the nynorsk paragraph's "A follower never translates …
  persisting the translations where every pod reads is the fix" becomes the description of this
  store.
- `docs/operations/poller-lease.md`: the translation line in "leader-only" moves to "every pod
  reads, the leader writes".
- `docs/operations/notification-webhooks.md`: the log-event table gains
  `situation_english_coverage`; `test_notification_contract` greps for it if the event is in that
  namespace.

### U6. Rollout and measurement

1. Merge. dev's Terraform applies on the push, then dev deploys. Confirm the TTL config. dev runs
   one pod, so R1/R4 cannot be seen there; check AE2 with a graceful delete instead.
2. **prd:** first `gh workflow run terraform.yml -f environment=prd` and confirm "3 added" or the
   equivalent; only then the `cd.yml` dispatch.
3. Repeat U1(1) in prd and expect zero flip-flop. Check AE2 on prd's next handoff.
4. U7 ships in its own PR after U3 has run a day in prd, with the same Terraform-first order. Verify
   it by loading one registered journey's `/varsel` card about 20 times: every answer must carry the
   same wording and alternative.
5. Record the before and after numbers below.

---

## Decisions (decided by the user, 2026-10-22)

- **D1. Journey-alerts wording per pod:** decided on U1(3)'s number. The divergence measured is
  mostly the per-generation template window at `wait=0`, not the pods, so there is **no shared
  store for the pull read's own wording**. U7 still gives a follower the leader's wording for
  registered definitions, through `cached_wording_and_alternative`.
- **D2. The follower's `/varsel` card:** **share the leader's wording and alternatives (U7)**, and a
  new leader **starts from the stored entries** (R10). Rejected:
  - followers computing their own: Journey Planner and Gemini per pod, and two pods choosing
    different alternatives or wordings for one train;
  - routing `/varsel` reads to the leader: no sticky routing, and it would couple serving to the
    leader's health.
- **D3. Coverage:** a log only (KTD7), no gauge or alert.
- **D4. Translation expiry:** 30 days from creation, no renewal.
- **D5. Events minted before a translation lands:** accepted; they keep null.
- **D6. Nynorsk quality:** closed. The hand-written and machine nynorsk were reviewed by the user,
  who knows nynorsk.

## Measurements

_U6 (after) still owed._

**U1, prd, 2026-10-22 ~09:20 UTC** (two pods since step B at 09:00, `a2e3fd7`). 40 journey-alerts
reads at `wait=0` over four Oslo S / Lillestrøm / Drammen pairs, then 16 reads of one pair:

- **Flip-flop confirmed, for machine nynorsk only.**
  - Each of 10 Vy situations had `summary_nn` in only 4-6 of 10 answers (one incident: 14 of 27),
    which is the leader/follower split. The answers that carried it all said
    `nn_machine_translated: true`.
  - The `KIX:ETCancellation` row carried nynorsk in 10 of 10 (SQL-written, on the table every pod
    loads).
  - Operator English was present in every answer for every situation (`en_machine_translated:
    false`).
  - No non-Vy situation was in these pairs, so machine English was not observed. It follows the
    same path as machine nynorsk.
- **Journey-alerts wording also differs between answers (D1), and mostly for a different reason.**
  - Across the 40 reads, 6 of 8 trains had two different `payload.notification` texts.
  - The 16-read sequence shows why: answers alternate between two `refreshed_at` generations
    (09:22:07 and 09:24:16, then 09:24:28), and a pod on a NEW generation answers
    `wording.source: deterministic` (the template, "Mulig avvik … Innstilt") until its own
    enrichment has worded the train. A pod on the old generation still has the LLM wording.
  - So at `wait=0` the prose flips with each table generation on whichever pod reloads, not only
    between pods. A client using the default `wait` would mostly get the LLM text.
  - The two pods also serve different `refreshed_at` for up to one follower tick after each
    rebuild, which is KTD6's consistent-stamps design working as specified (a follower loads on its
    next tick).
  - Input for D1: a shared wording store would remove the between-pods part, not the
    per-generation template window.
