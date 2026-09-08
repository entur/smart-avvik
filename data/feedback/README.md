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
POSTHOG_PERSONAL_API_KEY="$(approved-secret-manager-or-cli-command)" \
uv run python data/feedback/export_varsel_feedback.py \
  --project ent-kixavvik-dev \
  --environment dev \
  --from-date 2026-08-25 \
  --to-date 2026-08-27 \
  --posthog-project-id 259080 \
  --output /private/tmp/varsel-feedback-2026-08-25.json
```

**The key is a one-command prefix, not an `export`, and that is the safer of the two forms** — it
was `export` plus a trailing `unset` until 2026-08-29. A variable assignment preceding a simple
command is placed in that command's environment and nowhere else, so the key never enters the
interactive shell: with `export` it lives in the shell's memory, and in the environment of every
process spawned from it, until the `unset` runs — and the `unset` is exactly what does not run when
the export succeeds and the command then fails, is interrupted, or is simply walked away from.
Verified 2026-08-29 that both links hold for this command specifically, since neither is pure shell
semantics: `uv run` passes the variable through to the child Python, and the assignment survives the
`\` continuation that follows it. What does not change is the "never an argument" rule the value
still obeys — argv is readable by every user on the machine via `ps`, an environment is not — nor
that shell history records the `$(...)` substitution rather than the value either way, so that half
rests on the secret command, not on this choice. The transience is guaranteed for an **external**
command only; `VAR=x some_shell_function` can leak the assignment into the shell, which is not this
case but is the trap if the recipe is ever wrapped in a helper function.

`--posthog-project-id` is **259080** for `Kix - dev` — which serves the dev *and* tst environments, so
`--environment` is what separates them — and **259077** for `Kix - prod`. `--project` is the GCP
project holding the context table, so it always tracks `--environment`: `ent-kixavvik-{dev,tst,prd}`.

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

## Read it in a notebook

`review_feedback.py` beside this file is a marimo app that runs the same export **in memory** and
renders it: the gate's synthetic residue filtered out by default, a selectable list, and per-response
drill-down to the exact alert — displayed wording, observation, prediction, policy, delivery and the
`message_input` the wording was written from.

```sh
POSTHOG_PERSONAL_API_KEY="$(approved-secret-manager-or-cli-command)" \
uv run marimo edit data/feedback/review_feedback.py
```

It opens on **prd** over the **last 30 days including today** and fetches on load: there is no
window to set and no button to press. Thirty because the feedback is sparse — six responses in the
trailing week of prd on 2026-09-08 — and because it is the whole of PostHog's 30-day event
retention, so the default view is every response that still exists and a wider one would only ask
for days the project has already deleted. It also sits one day under the exporter's own 31-day cap,
and comfortably inside the 45-day context retention, which is why `expired_context` should stay
absent from `issues` at this width. That replaced a two-day dev default behind a **Run export**
button on 2026-09-08 — the pre-click state is `mo.stop`, which renders as "this cell wasn't run
because an ancestor was stopped" on every cell below it, so the page a reviewer met most often was
one that looked broken. The three controls remain as the override, and **Refresh** asks the same
window again, which is how feedback ingested since the page loaded appears (`fetch_posthog` sets
`force_blocking`, so a repeat is a real query rather than PostHog's cached answer). Two things the
button had been quietly doing had to be replaced rather than dropped: the window bounds are now
returned from the `target` cell and stop the fetch, because a half-edited window reaches the network
on the keystroke once nothing stands between them; and `fetch_contexts` returns `[]` without a
BigQuery call when the window holds no events, which is what makes firing on every dropdown change
cheap.

It calls the exporter's own `collapse_events`, `fetch_contexts` and `enrich` by loading this
directory's script by path, rather than reimplementing them — a divergent copy would be worse than no
notebook, since it would look like the export without being it. It skips only `write_json`, because
that function refuses to overwrite and a notebook that wrote on every run would fail on its second;
saving is opt-in in the last cell, through that same guarded function, and writes the **unfiltered**
document — filtering is a reading convenience, and an evidence file that quietly omits rows is not
evidence. Table download buttons are disabled throughout for the same reason the exporter sets
`0600`: a widget-dropped CSV in `~/Downloads` has none of that protection.

Run `uv run marimo check data/feedback/review_feedback.py` after editing it, not just `ruff`. The
two catch different things: marimo's checker found a save cell whose `try`/`except` branches each
ended in their own `mo.md(...)`, which renders **nothing** — marimo only displays an *unnested*
trailing expression, so the button would have looked inert whether it wrote the file or refused to.
Ruff and the interpreter are both happy with that code. Note also that current marimo closes every
cell with a `return`, including display-only ones, and re-adds them on save; `pyproject.toml` exempts
this file from `PLR1711` for that reason rather than fighting the formatter. Neither checker
covers the defect a hand-edit most plausibly leaves behind: a cell signature naming a variable no
cell produces. `marimo check` passed on a copy with exactly that, so **run the notebook** after
editing it — `python data/feedback/review_feedback.py` executes every cell — rather than trusting
either linter to have exercised the dataflow.

**It must never move to `eval/`.** `disruptions/api/export_eval.sh` globs `eval/*.py`, renders every
notebook there to static HTML, and `eval_static/` is committed and served publicly at `/eval` — which
for this page would publish travellers' free text and their routes. That is the whole reason it lives
here, and it is recorded in `pyproject.toml` beside the B018 exemption the marimo cell idiom needs.

The synthetic filter is an editable case-insensitive regex, `^\s*(syntetisk|synthetic)` by default,
and what it removed is rendered underneath what it kept rather than hidden behind a toggle. Both
choices are deliberate: the prefix is a **convention in free text** that the gate's submissions
happened to follow — nothing enforces it and no column records it — so a pattern that tried to be
forgiving about spelling would silently discard real feedback, and a filter on free text can only be
trusted if what it dropped stays as visible as what it kept. A response with no comment at all
(`comment: null`, an ordinary thumbs-up) is never matched by it.

## Setup and release gate

Capture is off until `VARSEL_FEEDBACK_ENABLED=true`, and the gate below is **per environment**:
dev since 2026-08-28, prd since 2026-08-31, tst still off. Configure each environment separately and
promote only after every item below is recorded for that exact PostHog EU project — an approval for
one environment is not evidence for another, because the subjects differ:

1. Create `POSTHOG_PROJECT_TOKEN` — a **`phc_` project write token**, which `_token()` requires by
   shape, for the reason in the measurement section below — and a separate 32-byte-or-longer
   `VARSEL_FEEDBACK_RECEIPT_SECRET` in Google Secret Manager, then add both names to that
   environment's Helm `secrets.varsel` mapping before enabling capture. They are deliberately not
   referenced while absent, so the disabled-code rollout cannot break an existing deployment.
   Values never belong in git. Rotate either secret after suspected disclosure and on the platform team's normal rotation
   cadence. The receipt key rotation ends updates to already-issued receipts but does not affect
   alert delivery. Done for all three environments on 2026-08-28 — see the tst/prd pre-wiring
   section below for what each one still needs.
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
   and the kill switch — none of those four needs a deployment, and all four must pass first. Then
   set `VARSEL_FEEDBACK_ENABLED=true` only in the environment being promoted, and measure **p95
   submit latency below 3 seconds against that deployment**. That order is forced rather than
   careless: `submit()` returns `{"status": "disabled"}` before it touches BigQuery or PostHog — the
   two calls that are very nearly all of the cost — so a latency figure taken with capture off
   measures the short-circuit and nothing else. Minting is gated the same way, so there is also no
   real `context_id` to submit against until the flag is on. The kill switch is verified *before* the
   enable precisely so that it is what a failed latency reading is rolled back with.

Rollback is `VARSEL_FEEDBACK_ENABLED=false` followed by a deployment. It stops new capture only;
alert display and delivery stay healthy, while existing feedback remains reviewable until expiry.
PostHog outages or context-write failures must never be worked around by logging feedback bodies.

### Step 3, verified (2026-08-28) — with three deviations from what the step claims

Read out of the live IAM, not out of the plan. The **principle holds**: nothing here is granted to a
person-shaped list, access arrives through `sg-dig-team-ki-eksperimentering@entur.no` bound at the
`team-ki-eksperimentering` folder and inherited by both projects. But three specifics are wider than
this file said, and a reviewer comparing the two would rightly not trust either until it is written
down.

**Runtime write access.** `application@ent-kixavvik-{dev,prd}` holds `roles/bigquery.dataEditor` +
`roles/bigquery.jobUser` at *project* level, which reaches the dataset through its `projectWriters`
ACL entry. That satisfies create + `insertAll` for `varsel_feedback_contexts`, and is proved in
practice rather than on paper: the same SA already writes `varsel_decisions` through the identical
`ensure_table` + `insert_rows_json` path. **Deviation 1: `dataEditor` is project-wide, not scoped to
this table**, so "the minimum permissions needed" above is aspirational. Narrowing it is a separate
piece of work — the whole app runs on that grant.

**No public context-read route** — structural, not a policy. `feedback_contexts.resolve` has exactly
one caller (`feedback.submit`, `revision == 1`) and is used as an existence test (`is None`); no
value from it reaches a response. `mint` and `persist` only write. So there is nothing to restrict.

**Reader access. Deviation 2, and the one to read twice:** this file said developers and product
managers get "BigQuery Data Viewer/Job User". They actually hold **`roles/bigquery.admin`** (plus
`aiplatform.admin` and `entur.devOps`) via the team group at the folder. `bigquery.admin` includes
delete. So the real access is far wider than documented, and any statement to a privacy owner must
use the true figure. Terraform's `disruptions_readers` is a *separate* grant and currently holds only
`group:sg-dig-team-sd@entur.no` (Team SD) — it is not how the owning team gets in.

**Deviation 3: a person holds OWNER on the dataset** in *both* environments —
`thomas.oldervoll@entur.org`, almost certainly because BigQuery grants the creator OWNER and the app
creates the dataset at startup under whoever ran it first. It contradicts the "grant to a group, never
a person" rule in `CLAUDE.md`, and it is the one deviation with a cheap fix.

**prd, as read on 2026-08-28 — since overtaken, except for one line.** The two capture secrets did
not exist in Secret Manager then; they landed on 2026-08-28/29 and capture is on there since
2026-08-31 (see the prd approval below). `varsel_feedback_contexts` still **has not been created** at
the time of writing — the app creates it on first persist, so it appears after the enabling deploy,
not before, which is also why nothing had to be created by hand. What is unchanged and still worth
fixing: the prd dataset ACL carries no team group at all, only `projectWriters`/`projectReaders`
plus that personal OWNER.

### Step 4, approved for dev (2026-08-28)

Approval covers **dev only**, and this step is per environment — tst and prd each need their own,
because each has different subjects. The artifact is
`docs/personvern/2026-08-28-varsel-tilbakemelding-personvernvurdering.md`: purpose and lawful basis,
the free-text notice as it actually reads in the PWA (`Ikke skriv personopplysninger.`), the true
access list — `roles/bigquery.admin` through the team group, not the Data Viewer/Job User this file
claimed before the step-3 read-out — 30-day PostHog and 45-day BigQuery retention, 30-day export
cleanup, and the subject-rights limitation.

**The retention-only procedure is the part that needed approving, and it is approved.** PostHog's
deletion path is per *person*; these events have no person, so there is nothing to delete against
and no per-event route exists. A response therefore ages out at 30 days rather than being erasable
on request, and its BigQuery context at 45. Evidence under the measurements below.

Two things a dev approval does **not** settle, recorded so nobody reads more into it than it says:

- **Punkt 9b of the assessment was still open on that date**: whether the origin/destination/
  departure-time trio `/varsel` already sends to Gemini is "Fortrolig" under Entur's information
  classification, and whether KI-løypa applies to it. That is not a feedback question — the trio
  predates this feature and reaches Vertex whether or not capture is on — which is why it did not
  block dev. It was **answered with the prd approval on 2026-08-31**; see that section below.
- **Dev's subjects are the team.** The approval rests on that, so it is not evidence for an
  environment with real travellers behind it. If `/varsel` ever takes public registrations this
  re-opens, alongside the k=1 exposure recorded for `/meldinger` in `CLAUDE.md`.

Reference for the approval record (ticket, email or meeting note): _to be filled in by the owner._

### Step 4, approved for prd (2026-08-31) — and it closed punkt 9b too

prd's approval is its own, as this step requires, and it rests on what the dev one could not speak
for: the subjects here are real travellers rather than the team. It covers the same artifact,
`docs/personvern/2026-08-28-varsel-tilbakemelding-personvernvurdering.md` — purpose and lawful basis
for the free text, the notice as it reads in the PWA (`Ikke skriv personopplysninger.`), the access
list, 30-day PostHog and 45-day BigQuery retention, 30-day export cleanup, the **expiry-only**
deletion procedure and the subject-rights limitation that follows from it.

**Punkt 9b was answered, which is the part worth reading twice.** The assessment attached it
deliberately, on the grounds that approving the feedback feature while the larger intrusion on the
same surface stayed open would approve the smaller of two. So it is not a footnote that this approval
settled it:

- The origin/destination/departure-time trio is classified **«Intern», not «Fortrolig»**. That is one
  answer for the whole of `/varsel`, as the assessment asked for — it covers both the context row
  this feature writes and the Gemini prompt that has carried the same trio since 2026-08-07. The
  Gemini generation running in prd therefore stands unchanged, and no re-assessment is triggered by
  classification.
- **KI-løypa does not apply** to an in-house service that calls an already-approved platform; the
  løype covers procurement of AI tooling, so no KI-vurderingsskjema is owed for `/varsel`.

Neither answer changes any code, and neither retires a kill switch: `VARSEL_LLM_PROVIDER=off` still
stops the Vertex egress and `VARSEL_FEEDBACK_ENABLED=false` still stops capture, each one value and
one deployment.

**What this approval still does not settle.** `/varsel`'s registrations are internal — 22 devices
across 82 journeys in prd on 2026-08-30 — and the approval is for that population. If `/varsel` ever
takes registrations from the public, this re-opens together with the k=1 from/to exposure recorded
for `/meldinger` in `CLAUDE.md`, and the fix there is access control rather than a shorter SELECT.

**Three gate items were outstanding for prd and could not be done first**, because each needs
capture on. **All three were completed on 2026-08-31** — step 2 once a real event finally reached
`Kix - prod` (see the incident section below), steps 5 and 6 in the section after it. They were: step 5's synthetic round-trip against the prd PostHog project (submit, wait the measured
consistency delay, retrieve through the Query API, join the exact context, delete the response and an
exclusively referenced context), step 6's **p95 submit latency against the prd deployment** — dev's
0.429 s is not transferable evidence, and a figure taken with the flag off measures the
`{"status": "disabled"}` short-circuit — and step 2's raw-event inspection for `Kix - prod` itself
(`$ip` exactly `0.0.0.0`, `person_mode` `propertyless`, no `$geoip_*`, no user-agent, no URL), which
needs one real event in that project. Rollback is the flag, as always, and it is what a failure of
any of the three is corrected with.

Reference for the approval record (ticket, email or meeting note): _to be filled in by the owner._

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

The one thing left needing a deployment — p95 submit latency — is measured below.

### Step 6, complete in dev (2026-08-28): p95 0.429 s against a 3 s bar

Capture went live in dev at 16:53 UTC and was measured through the public ingress against a **real
disruption** (Vy L1 Asker-Lillestrom cancelled all evening for staff shortage), not a contrived one.

| population | what it exercises | n | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| **full** | ingress + BigQuery insert + PostHog POST | 55 | 0.230 s | **0.429 s** | 1.352 s |
| repeat | ingress + PostHog POST (context already persisted) | 10 | 0.185 s | 0.398 s | 0.398 s |
| floor | ingress + `resolve`, refused before any I/O | 40 | 0.113 s | 0.172 s | 0.203 s |

**The three are separate because `persist` is idempotent, and a naive load test reports the middle
row as the bar.** After a context's first successful write it returns early on the in-process
`_persisted` set, so every later submit on that context skips BigQuery entirely. The `full` row is
therefore one sample per *distinct* context — a real first tap — which is why it needed 55 distinct
alert renderings rather than 55 requests. Confirmed in the data rather than assumed: the 55 full
submits wrote exactly 55 rows and the 10 repeats wrote none.

The `floor` row is worth keeping for the next environment. An unknown `context_id` is refused
*before* `_claim`, so it costs no rate budget and no daily budget and can be measured freely; it
isolates the fixed cost of the public path at ~0.11-0.20 s. A burst of 8 concurrent floor requests
gave p95 0.176 s — no degradation — but note they return before `_inflight`, which refuses rather
than queues at 4, so that burst does not exercise the semaphore.

**Deployed is ~5x faster than the laptop figure, and the old number must not be quoted as if it
were this one**: 1.07-1.29 s locally against a 0.230 s p50 here. BigQuery and PostHog EU are both
far closer from inside GCP than from a developer's machine.

Two honest limits. n=55 puts the p95 at the 53rd value, which is coarse — tolerable only because it
lands 7x under the bar. And the code's own timeouts (`insert_rows_json(timeout=5)` plus
`urlopen(timeout=3)`) *permit* a single submit above 3 s; the bar is a p95, and the observed max was
1.352 s.

**Also verified against the deployment, none of it previously exercised outside unit tests:**

- **The receipt model.** `revision 1` returned a receipt, `revision 2` carrying it was accepted,
  and both `revision 3` with a tampered signature and `revision 2` with no receipt returned
  `invalid_receipt`. The HMAC works against the deployed secret.
- **The JSON-column serialization fix, for the first time in a deployment.** All rows were accepted
  and `policy` / `prediction` / `journey_context` are queryable through `JSON_VALUE`. Before that
  fix every context write failed invisibly, and only a real write proves it.
- **No free text reaches the logs.** A canary in the comment returned **0 hits** in `ent-kub-dev`
  over the window, against a control query on the same resource that did return entries. The first
  attempt searched `ent-kixavvik-dev`, which holds only BigQuery audit logs — its empty result meant
  nothing, and a log check whose control is not run is not a check. The only warnings in the window
  were unrelated: one transient Gemini fallback, a `google_genai` AFC notice, and the known
  `varsel_decisions` description-refresh warning.

**What was measured is not what a traveller will mostly see.** 49 of the 55 contexts carried
`display_source='summary'` with an 11-character `displayed_text` (`Mulig avvik`) — 44 of them were
no-SX scored alerts, and a **device-less** registration has no pending pushes, so the poller wrote a
wording only for each journey's worst train (the documented fallback), 6 in all. A real install with
a push subscription gets a written wording far more often. Latency is unaffected — same path, payloads
differing by tens of bytes — but the mix must not be read as a statement about card quality.

**Step 2's raw-event inspection, re-done on the real path (2026-08-28).** The gate's earlier
inspection used events sent from a laptop; this one is a real tap on a real phone, which is the path
that matters. Every required property holds: `$ip` reads exactly **`0.0.0.0`**, `person_mode` reads
**`propertyless`**, `$process_person_profile` is `false`, `team_id` is the intended EU project, and
`elements_chain` is empty. Nothing undocumented is present — no user-agent, no `$geoip_*` (so the
GeoIP transformation really is removed, which `$ip: 0.0.0.0` alone would not have proved), no URL,
route, session, prompt or `$lib` browser-library property. The event carried `revision: 2`, so the
**client-side receipt round-trip works from the PWA** as well as from the synthetic test above: the
rating was submitted first and the comment added afterwards under the receipt, writing no second
context row. Its `context_id` is the one genuine row named below.

**Residue in dev that cannot be cleaned up.** 67 synthetic PostHog events and 56 synthetic context
rows, every one carrying a comment beginning `SYNTETISK`. PostHog deletion is per person and these
have none, so they age out at 30 days rather than being removable: **exclude them by that prefix**
when reviewing dev feedback. The BigQuery rows are deletable once the streaming buffer clears. One
row is *not* synthetic — a submit at 16:59:27 on `Oslo S -> Sandvika`, a journey this measurement
never registered.

### Steps 1 and 3, pre-wired for tst and prd (2026-08-28)

Step 1 is done for all three environments and step 3 needs nothing further, so **promotion is a flag
plus the approval, not an infrastructure project.** That is how prd was promoted on 2026-08-31 —
one value, once the approval existed. What that leaves per environment:

| | dev | tst | prd |
| --- | --- | --- | --- |
| `POSTHOG_PROJECT_TOKEN` in Secret Manager | yes | yes | yes |
| `VARSEL_FEEDBACK_RECEIPT_SECRET` in Secret Manager | yes | yes | yes |
| both names in Helm `secrets.varsel` | yes | yes | yes |
| `disruptions` dataset exists (EU) | yes | yes | yes |
| step 4 approval | yes (2026-08-28) | **no** | yes (2026-08-31) |
| `VARSEL_FEEDBACK_ENABLED` | `true` | `false` | `true` |

**Order, and why it is not negotiable.** Every secret was created in its project *before* its name
was added to Helm. Each namespace's `SecretStore` reads its own `ent-kixavvik-<env>` with
`secretVersionSelectionPolicy: LatestOrFail`, and the rendered `ExternalSecret` is all-or-nothing —
one unresolvable name fails the whole object, and the container takes the resulting `kix-avvik-varsel`
Secret through a single `envFrom.secretRef`. In prd that would have taken `VAPID_PRIVATE_KEY` and
`VAPID_PUBLIC_KEY` down with it, **silently disabling push for real travellers over a feature that is
switched off**. `roles/secretmanager.secretAccessor` was already granted project-wide to
`application@ent-kixavvik-<env>` in all three, so no per-secret IAM was needed.

**tst deliberately shares dev's PostHog project ("Kix - dev").** The token value is dev's byte for
byte (verified by hash, never displayed). What separates the two inside that project is the
`environment` event property — `deployment.environment_label() or "prd"`, so `"dev"`, `"tst"`, `"prd"`
— which is also salted into the per-response `distinct_id`, so no person can span environments. So an
export of tst is `--environment tst` with **dev's** `--posthog-project-id`, and tst's events age out
on dev's 30-day retention clock. The exporter already takes both as arguments and its `ENVIRONMENTS`
tuple already lists all three; nothing there needed changing.

**The three receipt keys are distinct** (fresh 64-byte values, verified by hash). Sharing one would
let a receipt minted in dev authorize a `revision > 1` update against a real traveller's response in
prd, which is the single thing that HMAC exists to prevent.

**prd's real token landed on 2026-08-29**, replacing the labelled placeholder the secret was parked
with. Verified without printing the value: version 2 enabled and current, `phc_` prefix, and
**byte-distinct from both dev's and tst's** — that last check is not ceremony, since dev's token is
the one closest to hand and pasting it here would have routed real travellers' feedback into the dev
project with nothing anywhere reporting an error. Version 1 was `disabled` so a rollback could not
restore the placeholder, and the `state=placeholder` label is gone.

**That verification also claimed "48 characters, no trailing newline", and it was wrong** — the
payload was 49 bytes ending `0x0a`, and version 2 is now disabled too. What went wrong and how to
check it properly is the incident below; the short version is that every recipe in this file used
`tr -d '\n'`, which deletes the byte being looked for.

The placeholder was worth its short life. A secret that is absent fails the whole `ExternalSecret`
and takes the VAPID keys with it; a secret holding a placeholder keeps the Helm reference safe from
the moment it lands and leaves only a value for a human to supply. It was safe to sit there because
`_token()` rejects it — `enabled()` reads False, `/varsel-api/config` reports capture off, and no
context row is written — which is the guard described next, and the exact placeholder string is
pinned by `test_a_token_that_is_not_a_project_write_key_fails_closed`.

**The PostHog project ids**, needed for `--posthog-project-id` on an export: **259080** for
`Kix - dev` (which serves both the dev *and* tst environments — separate them with `--environment`)
and **259077** for `Kix - prod`. Not secrets, and recorded here so an export does not start with a
console visit.

### Steps 5 and 6, complete in prd (2026-08-31)

Both were run against the deployment through the public ingress, the same way dev's were, and only
after the token defect above was fixed — a latency figure taken while every capture was being
dropped would have measured a POST that did no useful work.

**Step 6: p95 0.403 s against a 3 s bar.**

| population | what it exercises | n | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| **full** | ingress + BigQuery insert + PostHog POST | 4 | 0.391 s | **0.403 s** | 0.403 s |
| repeat | ingress + PostHog POST (context already persisted) | 8 | 0.162 s | 0.167 s | 0.167 s |
| floor | ingress + `resolve`, refused before any I/O | 40 | 0.098 s | 0.111 s | 0.114 s |
| floor, 8 concurrent | the same, issued at once | 8 | 0.107 s | 0.111 s | 0.111 s |

prd is marginally faster than dev on every population, which is what a shared platform predicts;
the three are kept separate for dev's reason, that `persist` is idempotent and a naive load test
reports the `repeat` row as the bar.

**`full` is n=4, and that is a real limit rather than an impatience.** A first tap needs a *distinct*
context, one context is one rendering, and `mint`'s fingerprint dedup deliberately returns the same
id for an unchanged rendering — so prd offered exactly four that day, the Togmeldingen issue cards.
Dev's 55 came from a live disruption across registered journeys. At n=4 the p95 is arithmetically the
maximum, so read the row as "four first taps, 0.225-0.403 s", not as a distribution. It clears the
bar by 7.4x, which is why this was not worth manufacturing more traffic in prd for: every extra
sample is an unremovable synthetic response in a store that holds real travellers' feedback.

**Step 5, clause by clause.** Four synthetic responses submitted, all `saved`. The export returned
**14 responses with `issue_count: 0`** — every response resolved its exact context, none missing,
malformed, out-of-horizon, duplicated or conflicting. The count reconciles and is worth stating,
because it is the collapse working: **15 submissions produced 14 responses**, the fifteenth being a
`revision 2` carrying its receipt, which updates its response rather than adding one. The receipt
model was exercised against prd's own key in all three directions — `revision 2` with the receipt
accepted, a tampered signature and a missing receipt both `invalid_receipt`. Those two negatives cost
nothing: they are refused before `_claim`, so they consume no rate budget and write no event.

**Deletion, and the half of it that does not exist.** One exclusively referenced synthetic context
was deleted (1 row affected, 10 -> 9); the context carrying nine responses was left in place and
verified still present, which is the shared-context rule; the two contexts behind genuine responses
were verified untouched, having been identified first — context ids are fingerprint-stable, so a
synthetic submission on the same rendering as a real one would share its context, and deleting it
would have orphaned somebody's real feedback. **The response itself cannot be deleted**: it is
personless, PostHog's deletion path is per person, and the retention-only procedure approved in step
4 is what covers it. Note the consequence, which is by design rather than a fault: the next export
reports `issue_count: 1`, `kind: "missing_context"`, for the response whose context is gone — an
orphan surfaces as a listed issue instead of silently.

**One sub-item is carried from dev, not re-proven here.** "The bounded query returns all rows at its
limit" cannot be exercised against ~15 events with a 10 000-event cap. It is a property of the
exporter, covered by `test_export_varsel_feedback.py` and demonstrated in dev; claiming it as
re-verified in prd would be a fiction.

**Residue in prd**: 13 synthetic responses, every comment prefixed `SYNTETISK`, unremovable for the
same reason as dev's and ageing out at 30 days. Exclude them by that prefix when reviewing prd
feedback — the reviewer notebook's default filter already does.

### The detector fired, and it took eight hours (2026-08-31)

The failure this file predicted — "an export returning **zero responses beside a growing
`varsel_feedback_contexts`**" — happened on the day prd capture was enabled, and it is worth reading
as a case study in why the shape guard is not enough.

**What was wrong: one byte.** prd's `POSTHOG_PROJECT_TOKEN` payload was **49 bytes ending `0x0a`**
against dev's clean 48. The token itself was correct. `phc_...\n` went out as the `api_key`, PostHog
EU answered `200 {"status":"Ok"}` exactly as it does for any non-empty key, and every submission was
dropped at ingestion while `submit()` returned `saved`, issued a receipt, and wrote the
route-bearing context row to BigQuery. Eight submissions between 13:00 and 21:20 CEST were lost;
nothing is recoverable, because PostHog never accepted them under a key it recognised.

**Why three verifications missed it.** `_token()` checks a `phc_` prefix and a 32-byte minimum, and
a trailing newline only makes both *more* true. Worse, the hash comparisons this file and the values
comments prescribe were all written `| tr -d '\n' | shasum -a 256` — which deletes precisely the
defect, so the stored value hashed identically to the token in PostHog's UI on 2026-08-29 and again
on 2026-08-31. **A check that normalises the input cannot detect a defect in the input.**

**The verification that works**, and the one to use after any rotation, in any environment:

```sh
gcloud secrets versions access latest --secret=POSTHOG_PROJECT_TOKEN \
  --project=ent-kixavvik-<env> | wc -c            # 48, not 49
gcloud secrets versions access latest --secret=POSTHOG_PROJECT_TOKEN \
  --project=ent-kixavvik-<env> | tail -c1 | xxd -p  # not 0a
```

Write the value with `printf '%s'`, never `echo`, and never from a file. `feedback._env` now strips
surrounding whitespace from the token, the receipt key and the host before any guard reads them, and
logs `event="varsel_feedback_config"` once per variable when it has to — so the same provisioning
artefact is now a startup WARNING rather than eight silent hours. Three checks in `test_feedback.py`
pin it, including that padding cannot smuggle a short receipt key past the 32-byte minimum.

**Two operational traps found in the same hour, both of which cost a round trip.** A corrected secret
reaches the process only after *both* of these:

- the **ExternalSecret re-syncs** — `refreshInterval: 1h` on `kix-avvik-varsel`, so either wait or
  `kubectl annotate externalsecret kix-avvik-varsel force-sync=$(date +%s) --overwrite`;
- the **pod restarts**, because the container takes the Secret through `envFrom` and reads its
  environment once at start. A pod whose Secret was fixed underneath it keeps serving the old value
  with nothing to show for it.

And pass `--context` explicitly. Both fix commands were run against the wrong cluster (kubectl's
active context was dev), which restarted dev, left prd untouched, and looked exactly like the fix
having failed — prd's Secret was correct by then only because its own hourly refresh had landed.

**Capture verified end to end at 21:27 CEST**, which also closed step 2 of the gate for
`Kix - prod`: the raw event carries `$ip` exactly `0.0.0.0`, `person_mode` `propertyless`, and no
`$geoip_*` or user-agent properties.

### A wrong PostHog token cannot be detected after the fact (measured 2026-08-28)

`POST https://eu.i.posthog.com/i/v0/e/` is **fire-and-forget**. Measured with four keys: a
`phc_`-shaped random token, a well-formed-but-wrong token, and the prd placeholder string all
returned **HTTP 200 `{"status":"Ok"}`**; only an **empty** key returned `401 API key is not valid:
empty`. The token is validated later, asynchronously, at ingestion. So `_capture`'s
`response.status >= 300` check cannot distinguish a wrong token from a right one, and a bad one would
discard every traveller's feedback **in complete silence** while `submit()` still returned `saved`,
still issued a receipt, and still wrote the route-bearing context row to BigQuery. This is not
hypothetical maintenance work: a rotation that pastes a personal API key (`phx_`) where the project
token belongs would do exactly that, in dev today.

Two consequences, both now in place:

- **`_token()` checks the shape** — a `phc_` prefix and at least 32 characters — so a placeholder or
  a wrong *kind* of key fails **closed** rather than silent: `enabled()` reads False, nothing is
  captured, nothing is persisted, and `/varsel-api/config` says capture is off, which is a
  one-request diagnosis. Do not relax this to make a demo work.
- **A well-formed token for the wrong project is still undetectable in-band**, and the only detector
  is on this side: an export returning **zero responses beside a growing
  `varsel_feedback_contexts`**. Check that pairing after any token rotation, in any environment.

Secret Manager refuses an empty payload (`INVALID_ARGUMENT: Secret Payload cannot be empty`), so
parking the prd secret as an empty string — which would also have failed closed — was not an option;
the shape guard is what makes the descriptive placeholder safe.

**Every test of this feature was unrun in CI, in two different ways.** CI executes each suite as
`python <suite>.py` rather than under pytest, so a file with no `if __name__ == "__main__"` runner
imports cleanly, collects nothing and exits 0.

- `test_feedback.py` (now 10 checks) and `test_feedback_contexts.py` (3) had no runner. Thirteen
  checks over the whole capture path — including the personless-payload and receipt-binding
  assertions this gate leans on — had never once run. `test_feedback.py` also needed a
  `setenv`-capable `_MonkeyPatch` shim, which the `test_varsel_api.py` original lacked.
- `test_export_varsel_feedback.py` (14) was unrun **twice over**: no runner, *and* outside the CI
  loop's `working-directory: disruptions/api`, so the glob never saw it. It guards the export of
  travellers' free text, which is the most sensitive path in this repo. It now has a runner (with a
  `tempfile`-backed `tmp_path` shim) and a CI step of its own.

The loop globs `test_*.py` deliberately, so that adding a suite cannot be forgotten — but a glob
catches the *file*, not a missing runner or a directory it never enters. **A suite that prints no
"N checks passed" line is a suite that did not run**; that line is the only evidence CI produces.

**Step 3 needs no change for tst or prd.** The app SA holds `roles/bigquery.{dataEditor,jobUser}`
project-wide in all three, which is what lets `feedback_contexts.ensure_table()` create the table on
first persist — it does not exist in prd or tst yet and does not need to be created by hand. Reviewer
read access is inherited from an ancestor rather than granted on the project (no human or group has a
project-level BigQuery binding in any of the three), and it was verified by querying prd's
`varsel_decisions` from a laptop. `terraform/env/{tst,prd}.tfvars` therefore stay `disruptions_readers
= []`: that list grants *other* teams, and the 2026-08-13 note there still applies unchanged.

**One correction to a claim in CLAUDE.md.** Nothing in this repo creates the `disruptions` dataset —
there is no `create_dataset` call and no `CREATE SCHEMA` statement; the startup CTASes create *tables*
into a dataset that must already exist. tst had none, so `ensure_table()` would have failed there with
"Dataset not found" and every submit would have returned `downstream`. `ent-kixavvik-tst:disruptions`
was created in **EU**, matching dev and prd. It is empty and stays empty: tst has no models or runtime
tables, so its poller produces no alerts and there is nothing there to rate. tst's wiring exists to
make promotion a flag and to exercise the plumbing, not because tst can evaluate the feature.

**What stays unverified until a flag actually moves:** egress from the tst and prd clusters to
`eu.i.posthog.com`. It cannot be probed from outside and the image is distroless, so there is no shell
to `kubectl exec` into. The evidence is indirect but good: every `NetworkPolicy` in both namespaces is
`policyTypes: ["Ingress"]` only, and both deployments already reach `api.entur.io` and Vertex AI.

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
