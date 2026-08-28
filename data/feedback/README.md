# Review varsel feedback

This directory contains the internal, JSON-only review export. PostHog holds the isolated
response; BigQuery holds the exact alert snapshot. The opaque `context_id` is the only join key.
The output is sensitive because it combines free text with route-bearing alert context. Never
commit it, serve it from the API, paste it into tickets, or keep it longer than 30 days.

## Run a bounded export

Use a PostHog **personal API key with read-only Query API access**, supplied through the
environment (never an argument). Use developer ADC for BigQuery. Both date arguments are UTC and
`--to-date` is exclusive; windows are required and limited to 31 days. PostHog keeps events for
**30 days** (the project's data-retention setting), so run the export at least fortnightly:
anything older is gone from PostHog, and its BigQuery context expires at 45 days.

```sh
export POSTHOG_PERSONAL_API_KEY="$(approved-secret-manager-or-cli-command)"
uv run python data/feedback/export_varsel_feedback.py \
  --project ent-kixavvik-dev \
  --environment dev \
  --from-date 2026-08-25 \
  --to-date 2026-08-27 \
  --posthog-project-id PROJECT_ID \
  --output /private/tmp/varsel-feedback-2026-08-25.json
unset POSTHOG_PERSONAL_API_KEY
```

The tool talks only to `https://eu.posthog.com/api/projects/{project_id}/query/`, queries the
single `varsel_alert_feedback` event with server-built time/environment predicates, and refuses
more than 10,000 events. It then parameterizes the distinct context IDs and the same date bounds
against `<project>.disruptions.varsel_feedback_contexts`. It selects the greatest complete
`revision` per `(environment, context_id, response_id)`; `event_id` deterministically breaks an
invalid equal-revision tie. Missing, malformed, out-of-horizon, identical-duplicate, and
conflicting-duplicate contexts appear under `issues`; ambiguous rows are never enriched.

The output is created mode `0600`. The tool refuses stdout, symlinks, missing parent directories,
and existing files. Delete exports within 30 days using the team's approved secure-deletion
procedure; ordinary local filesystem deletion may not erase snapshots or backups.

## Setup and release gate

Capture is deliberately disabled by `VARSEL_FEEDBACK_ENABLED=false`. Configure each environment
separately and promote dev → tst → prd only after every item below is recorded for that exact
PostHog EU project:

1. Create `POSTHOG_PROJECT_TOKEN` and a separate 32-byte-or-longer
   `VARSEL_FEEDBACK_RECEIPT_SECRET` in Google Secret Manager, then add both names to that
   environment's Helm `secrets.varsel` mapping before enabling capture. They are deliberately not
   referenced while absent, so the disabled-code rollout cannot break an existing deployment.
   Values never belong in git. Rotate either secret after suspected disclosure and on the platform team's normal rotation
   cadence. The receipt key rotation ends updates to already-issued receipts but does not affect
   alert delivery.
2. Confirm `POSTHOG_HOST=https://eu.i.posthog.com`, EU project residency, IP capture disabled,
   autocapture/session replay disabled, the GeoIP transformation removed, no person profiles, and
   30-day event retention (the project data-retention setting). Inspect the
   **raw captured event**, not only a transformed view: it may contain only the documented event
   fields, PostHog ingestion metadata, and no user-agent, geo, URL, route, prompt, session, or
   browser-library properties. `$ip` is the one exception and must read exactly **`0.0.0.0`** rather
   than be absent — an absent one means the capture code lost its override and PostHog has stamped a
   real address, which the project's own IP setting was measured not to prevent (see the IP entry
   under the measurements below). Check `person_mode`, which reads `propertyless` when no profile
   was created.
3. Assign runtime write access only to the kix-avvik workload identity: BigQuery Job User plus the
   minimum dataset/table append/create permissions needed for `varsel_feedback_contexts`. There is
   no public context-read route. The owning access group is `team-ki-eksperimentering`; developers
   and product managers receive BigQuery Data Viewer/Job User and PostHog project read access only
   through that approved group/process. Revoke PostHog membership, personal API keys, BigQuery IAM,
   and cached exports at offboarding.
4. Obtain privacy-owner approval for free-text purpose/lawful basis, the “do not enter personal
   information” notice, access list, 30-day PostHog and 45-day BigQuery retention, 30-day export
   cleanup, and
   anonymous subject-rights limitations. Record whether PostHog supports event deletion for these
   personless events; if it does not, an explicit retention-only procedure must be approved.
5. In dev, submit a synthetic response, wait the measured consistency delay, retrieve it through
   the official project Query API, prove the bounded query returns all rows at its limit, join the
   exact context, and delete the response plus an exclusively referenced context. A shared context
   remains until no retained response refers to it. Repeat the approved procedure in tst and prd.
6. Verify public-endpoint global/per-context rates, the 2,000/day hard capture budget, bounded logs,
   p95 submit latency below 3 seconds, and the kill switch. Then set
   `VARSEL_FEEDBACK_ENABLED=true` only in the environment being promoted.

Rollback is `VARSEL_FEEDBACK_ENABLED=false` followed by a deployment. It stops new capture only;
alert display and delivery stay healthy, while existing feedback remains reviewable until expiry.
PostHog outages or context-write failures must never be worked around by logging feedback bodies.

### Step 6, the half that needs no deployment (2026-08-28)

Driven against the real modules, not asserted — `_claim`, `parse` and `enabled` were called directly
and the counts read back. 15 checks, all passing:

- **Rate limits bind at exactly the documented numbers**: 12 accepted per context per minute out of
  50 offered (`_PER_CONTEXT_PER_MIN`), 60 out of 200 offered across *different* contexts
  (`_GLOBAL_PER_MIN`) — the second is what proves the global cap is not merely the per-context one.
- **The 2 000/day budget outranks a fresh minute**: at the cap a claim is refused even with both
  minute windows empty, and the last slot below it is honoured.
- **Two independent length caps**: a body over `_MAX_BODY` (2 KiB) raises `body_too_large` before any
  parsing, and a 501-character comment is refused by the model even inside a legal body.
- **The kill switch, and three things that also kill it**: `VARSEL_FEEDBACK_ENABLED=false`, a
  **trailing slash** on `POSTHOG_HOST`, a non-EU host, and a receipt secret under 32 bytes. The
  trailing slash is worth knowing — the host compare is an exact string match, so
  `https://eu.i.posthog.com/` disables capture silently rather than working.
- **No log line carries the traveller's free text.** Verified on the two paths that actually log,
  because an early return proves nothing: a failed BigQuery context write (67 characters logged) and
  a raising PostHog capture (**1 004 characters, a full `exc_info` traceback**). A canary string in
  the comment appears in neither, nor does the `context_id`.

Still needing a deployment: **p95 submit latency** against the 3 s bar. Server-side submit measured
1.07-1.29 s over three local runs (dominated by the BigQuery context write plus the PostHog POST),
but that is a laptop against dev, not a p95 through the ingress under concurrency.

## Official API assumptions to verify

The exporter follows PostHog's official [Query API endpoint](https://posthog.com/docs/api/query)
and uses a Bearer personal API key, while runtime capture uses PostHog's documented
[server-side event endpoint](https://posthog.com/docs/product-analytics/capture-events) with
`$process_person_profile=false`. PostHog documents EU Cloud privacy controls in its
[GDPR guidance](https://posthog.com/docs/privacy/gdpr-compliance). Project tier, key scopes,
retention, deletion support, Query API result limits, and consistency timing can change; capture
must remain disabled until those facts are tested in the intended Entur project and the evidence is
linked from the operational change record.

The reviewer credential is intentionally absent from Kubernetes. Runtime capture project tokens
are not reviewer credentials and cannot be used by this tool.

### Measured against the Entur dev project (2026-08-28, project 259080)

Verified by running the exporter, not by reading docs — the first two are defects this found:

- **HogQL is not ClickHouse.** `toInt64(...)` is refused with a 400 and the whole query fails;
  HogQL exposes `toInt(...)`. Nothing offline validates the dialect, so only a real run proves it.
- **PostHog type-infers a date-ish property as `DateTime64(6, 'Europe/Oslo')`** — the *project*
  timezone — and the raw stored string cannot be recovered. An unwrapped `properties.context_date`
  therefore returns `2026-08-27T00:00:00+02:00` for a stored `2026-08-27`, which the exporter
  rejected as one `malformed_event` while dropping every response. All date-ish columns are now
  projected through explicit casts; changing one needs a real run to re-verify.
- **The completeness guard is sound.** `SELECT number FROM numbers(N+5000) LIMIT N` returned exactly
  10 001 rows at N=10 001 and 20 000 at N=20 000, so PostHog does not silently cap below
  `MAX_EVENTS + 1` and the guard fires instead of truncating a busy window in silence.
- **A refused query now carries the API's reason.** urllib hides the body behind
  `HTTP Error 400: Bad Request`; the body is the entire diagnosis.
- **Ingestion consistency delay: ~50 s.** A synthetic response submitted through `feedback.submit`
  became readable through the Query API after 50.5 s (not visible at 48.2 s). Server-side submit
  latency was 1.07-1.29 s over three runs, against step 6's 3 s bar — a single-shot local figure,
  not a p95 through the ingress. Re-measure per environment; this is dev.
- **PostHog stamps `$ip` from the socket, and only a valid value in the payload displaces it.**
  Five probes: an absent, `null` or `""` `$ip` all recorded the caller's real address — null and
  empty are read as "not supplied" — while `"0.0.0.0"` survived intact, as did an
  `X-Forwarded-For: 0.0.0.0` header (not used: trusting PostHog's proxy handling is a weaker
  contract than occupying the property). The project's **"discard client IP data" setting did not
  suppress it**, which is why `feedback._capture` now sends `"$ip": "0.0.0.0"`: a privacy guarantee
  resting on a console toggle is one click from silently ending. Note what the field would otherwise
  hold — capture is server-side, so it is this pod's egress address, Entur infrastructure rather than
  the traveller, whose browser never contacts PostHog. The exposure it actually closes is a
  **developer** running the synthetic round-trip below from a laptop and writing their own
  residential address into a store with 30-day retention and no per-event deletion. That happened
  during this gate: the four synthetic responses of 2026-08-27/28 carry a real developer address and
  cannot be deleted, only aged out. Run the round-trip from inside the cluster, or accept and record
  it as was done here.
- **Deletion is retention-only, and this is the evidence step 4 asks for.** Measured on the four
  synthetic responses: `persons` holds **0 rows** for the whole project, `person_distinct_ids` has no
  row for any of their `distinct_id`s, and every event carries
  `$process_person_profile: false`. Each event does show a derived `person_id`, but
  `person.created_at` reads `1970-01-01`, i.e. a placeholder rather than a stored profile. PostHog's
  deletion path is per **person**, so for these events there is nothing to delete against and no
  per-event route exists. The BigQuery half works and was exercised: all five synthetic context rows
  were deleted, the table back to 0. Note DML is refused while rows sit in the streaming buffer
  ("would affect rows in the streaming buffer") — ~10-30 min here, documented as up to 90 — so a
  deletion request cannot be serviced immediately after capture.
- **The Query API caches a result per query text**, and that interacts badly with the delay above.
  A repeat of an identical query returns `is_cached: true`, so the first measurement of the delay
  read as "never readable in 300 s": the poll's own first empty answer was cached and re-served to
  every retry. The exporter therefore sends `refresh: "force_blocking"`. Without it a reviewer who
  re-runs a window after more feedback arrived gets the earlier, shorter answer — with no indication
  that anything was withheld.
