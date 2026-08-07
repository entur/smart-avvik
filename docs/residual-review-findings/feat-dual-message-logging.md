# Residual Review Findings — `feat/dual-message-logging`

Source: `ce-code-review mode:agent` over `feat/dual-message-logging` vs `main`
(base `b733395`), 2026-08-07. Nine reviewers: correctness, security, adversarial,
data-migration, reliability, api-contract, testing, project-standards,
maintainability. Applied fixes landed in `d12493f`; the items below were
deliberately **not** applied and are recorded here as the durable sink.

Plan: `docs/plans/2026-08-07-001-feat-dual-message-logging-plan.md`

## Not applied — behaviour

- **P3 · advisory · `disruptions/api/varsel.py` (`_journey_model`)** — *The card can
  quote a wording the push never carried, one cycle later.* Delivery is gated on
  the flag, but `_notified` dedups per `(journey_id, situation_number)`: if cycle
  N is capped or fails and pushes the template, a wording written in cycle N+1 is
  shown on the card and never pushed. The documented guarantee is "the in-app list
  says exactly what the push said". Both fixes are real work — weaken the stated
  guarantee, or record the body actually pushed per `message_key` and have
  `_journey_model` prefer it — so this is a design decision, not a patch.
  *(adversarial)*

- **Residual · prd Cloud Logging gains model prose at INFO.** `write_message_detailed`
  logs the written message with a hashed `journey_digest`; prd produces those
  records for the first time, and the prose names both ends of a trip. Not filed as
  a finding because `_log_push` already writes `Avvik: {from} → {to}` beside
  `device_id` in the same prd log, so no new identifier is introduced — but the
  deferred `/historikk` projection defers on exactly this ground and does not cover
  the log path. *(security, correctness)*

- **Residual · streaming-buffer window after `ADD COLUMN`.** Rows carrying the new
  columns can be rejected for minutes after `ensure_table` adds them, logged as
  "N of M alert decisions rejected by BigQuery" — byte-identical to the line
  `CLAUDE.md` documents as the symptom of a *forgotten* ALTER. Fails open; an
  operator cannot tell the two apart from the log alone. *(adversarial)*

## Not applied — measurement and cost

- **P3 · `data/queries/message_wording_comparison.sql` (query 2)** — the side-by-side
  joins `varsel_decision_groups`, which aggregates the whole table, so only the
  `paired` CTE carries the 30-day predicate the header prices. Harmless at current
  volumes and consistent with how `/historikk` reads the view, but the stated cost
  is wrong if the log grows. *(adversarial)*

- **Residual · the best-effort DDL block still has 7 `ALTER COLUMN` statements**
  against BigQuery's 5-per-10 s table-metadata cap. `d12493f` reordered it
  newest-first (so a truncation drops re-applications, not the descriptions that
  have yet to land) and gave it its own retry, which bounds the damage — it does not
  remove it. **The real fix is pruning:** a statement there exists only to carry
  reworded text to an existing table, so once it has reached dev *and* prd it is
  dead weight. Verify with `bq show --schema` in both projects, then delete.
  *(correctness, adversarial, data-migration, reliability — four reviewers)*

- **Residual · `varsel.py` is 2381 lines** and keeps absorbing matching, push,
  delivery gating, decision recording and the API surface. Nothing in this diff
  crosses a threshold; there is no decomposition plan. *(maintainability)*

## Not applied — test coverage

- **`_deliverable_message`'s fail-open branches are unexercised** (an
  `import llm_message` failure, or `delivery_enabled()` raising). Its own docstring
  notes it now sits on the delivery path where an ImportError would cost the
  notification itself. Its failure mode is also indistinguishable from correct
  delivery-off behaviour, which is what makes it worth pinning.
  *(maintainability, testing, correctness — three reviewers)*

- **`test_the_ddl_splits_structure_from_descriptions` asserts marker membership, not
  uniqueness.** `str.partition` splits on the *first* occurrence, so a second
  `-- BEST-EFFORT SECTION` would silently move the boundary and drop structural
  statements into the forgiven half. `assert _ALL_DDL_SQL.count(_BEST_EFFORT_MARKER) == 1`
  closes it. *(correctness)*

- **Nothing asserts the best-effort half ever succeeds** — only that its failure is
  forgiven. The failure mode is invisible in the suite by construction. *(correctness)*

- **No test covers generation ON + delivery ON for a non-worst alert-worthy
  situation** (`message_status = 'not_selected'` with a wording cached under the
  sibling key). *(adversarial)*

- **`message_wording_comparison.sql` is executed by nothing.** Both statements were
  dry-run against `ent-kixavvik-dev` by hand during this branch; CI has no GCP
  credentials, so that leaves no repo artefact. *(correctness, api-contract)*

- **No frontend test for the null-`message` rendering path**, which is now prd's
  ordinary state rather than a transient one. *(api-contract)*

## Coverage note

The independent cross-model adversarial pass was **not run**. `CROSS_MODEL_PEERS`
is unset and this checkout carries no `.compound-engineering` config, so nothing
opts this private repository into sending its diff to a third-party provider; the
in-process `adversarial-reviewer` ran as the documented fallback. The lens is
covered — what is absent is independent-model corroboration and the whole-document
sweep. Enable by setting an allowlist if that trade is wrong.
