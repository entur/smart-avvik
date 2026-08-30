# Residual Review Findings

Branch `feat/togvarsel-push-notifications` — togvarsel push notifications (plan: `docs/plans/2026-08-30-001-feat-togvarsel-push-notifications-plan.md`). Both actionable code-review findings were applied on the branch (`fix(review): apply review findings`); everything below is report-only residue from the multi-agent review (6 local reviewers + an independent cross-model adversarial pass, requested GPT-5.6-luna at xhigh via the codex CLI, serving identity unverified) and the headless plan review. No tracker tickets were filed — nothing here is actionable without a product or scale signal first.

## Report-only advisories (code review)

- P1 (advisory, cross-model) `disruptions/api/main.py` — on an SX-ingestion tick, `_rebuild_all`'s push fan-out runs before `togvarsel_notify.evaluate()`; a mass of hanging push endpoints could delay a round past its deadline. Requires unestablished scale (~44 journeys today; fan-out bounded at ceil(devices/workers)×10 s); the timeliness gate suppressing a late round is the designed honest response. Watch the measured `poller_cycle` numbers as registrations grow.
- P1 (advisory, cross-model) `disruptions/api/varsel.py` — `store_togvarsel_rounds` calls `_save_state()` (synchronous GCS upload) under `_state_lock`; a stalled upload would block the poller and stateful HTTP routes. Pre-existing mechanism (SX drops and registrations already save under the same lock); the new code adds a call site only.
- P2 (advisory, cross-model) — a stale `gone` for a subscription replaced mid-flight can drop the replacement (device_id-keyed drop in the shared helper). Identical semantics in the pre-existing SX path.
- P2 (advisory, cross-model) — a journey deleted between the tick's snapshot and delivery (seconds window) can still receive that round's push once.
- P3 (advisory, cross-model) — the dev-only test trigger has no rate limit; same posture as the existing prd-wide `/varsel-api/test`.
- P2 (anchor 50, reliability) `disruptions/api/togvarsel_notify.py` `station_set()` — the BigQuery `.result()` has no explicit timeout, unlike the `timeout=60` convention on the `togvarsel_issued` writes; it runs on the shared poller thread. One-line hardening if the poller ever stalls there.
- Validator-rejected as designed behavior: a fresh device whose push returns `gone` is kept but not retried within the round — plan KTD8's documented trade-off, pinned by test.

## Doc-review decisions skipped with reasons (plan review)

- Level-only significance gate (cross-model suggestion: extend to scope/reasons) — skipped: KTD1 owns the decision with research grounding; reasons recompute from live features every issue, so a reasons-sensitive predicate would defeat the change gate.
- Both-endpoints matching (cross-model suggestion: route intersection with the station set) — skipped: a recorded plan assumption matching the model's area-wide grain; revisit with the deferred corridor-level work.

## FYI observations (plan review)

- No signal is contracted to adjudicate the deferred red-only threshold decision (watch unsubscribe/gone trends after dev/tst enablement).
- No outcome metric ties pushes to improved commute decisions.
- The per-journey error log has no stated field-redaction boundary.

## Testing gaps noted (not blocking)

- `KIX_ENVIRONMENT=dev` on a prd project would open the test trigger (operator-misconfiguration only); no test pins the inverse of the gate override.
- The trigger's Pydantic validation floor (`device_id` min length → 422) is unpinned.
- `varsel.journeys_snapshot()` is never exercised by a test as the wired `journeys` seam (fakes substitute it).
- No flag-parity pin between `main._togvarsel_notify_enabled` and `llm_message._TRUTHY` (mitigated: the fix made main reuse `_TRUTHY` directly).
