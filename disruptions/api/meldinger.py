"""/meldinger-api — replay a window of past alert decisions through today's prompt.

/historikk answers "what did we send, and was it right". This surface answers "what would today's
prompt say about the same disruptions": it reads the ``message_input`` document already stored on
every decision row and re-runs the current prompt over it. Nearly the same filters, the same stats
and the same cards; a different window axis, and a wording that is generated rather than quoted. The
one filter with no /historikk twin is ``sx_summary`` -- one exact SIRI-SX summary out of the window's
30 most common -- which belongs here because the question is what the prompt makes of a given
disruption text.

**The window axis is the whole difference.** /historikk windows on ``call_date`` — the operating day
of the train. This page windows on ``departure_time`` — the aimed departure of the boarding call —
as a single day plus a from/to time of day, all Europe/Oslo. The unit of this page is a MESSAGE, and
a message belongs to a departure: "what would we have sent out for the trains leaving between 07:00
and 09:00, worded by today's prompt".

It windowed on ``decided_at`` until 2026-08-22, which selected CYCLES instead. That axis could not
answer the question — a departure has one send moment, while a cycle window has to pick one by a
rule the reader cannot see — and it cost a whole class of caveats: a subject's cycles straddled the
window, so every per-cycle figure was window-scoped and ``first_in_window`` had to exist to stop
"Varsel kl." claiming a first opportunity that was merely the earliest cycle the reader selected.
On this axis a subject is wholly in or wholly out, so every figure is whole-subject and means what
/historikk's means. What was given up is real and is written into the SQL header: only a
``decided_at`` window reconstructs "what the app was saying at 07:15" as one coherent snapshot, and
only it let the reader sweep lead time by moving the window.

The consequence for cost is that ``varsel_decisions`` is ``PARTITION BY DATE(decided_at)``, which
the window predicate no longer touches — so the pruning bound is now DERIVED (every cycle that could
have decided about a departure in the window) and a too-narrow one drops cycles from the aggregates
silently, while reporting the loss as a cheaper query. See ``meldinger_decisions.sql``.

**Both times are quantized to 15-minute steps and the range is half-open ``[from, to)``.** That is
not cosmetic: the endpoint is public and unauthenticated and every parameter lands in the response
cache key, so two free-form second-resolution times are on the order of a billion distinct keys per
day, each one a cache entry and a BigQuery query. ``from_time`` rounds down and ``to_time`` rounds
up, so a narrowed range never drops a minute the caller asked for, and the whole-day default runs
00:00 to the FOLLOWING midnight — 23:59:59.999999 is not on a 15-minute grid, and quantizing it down
would silently drop the last quarter-hour of every default request.

**Five routes.** ``config``, ``filters``, ``decisions`` and ``summary`` are BigQuery only; ``messages``
is the one that calls Vertex AI. They are separate endpoints deliberately: a Vertex outage empties
the wordings and leaves every card, filter and statistic on the page intact, where one endpoint would
503 the whole thing. The two lists are aligned POSITION BY POSITION — nothing served identifies a
decision uniquely, since the real grain includes the redacted ``journey_digest`` — which is why
``meldinger_messages.sql`` repeats ``meldinger_decisions.sql``'s WHERE and ORDER BY verbatim.

**``/messages`` regenerates; it never renders what was stored.** It reads back the ``message_input``
document recorded on the chosen cycle, hands it to today's prompt, and returns the result beside the
deterministic wording built from the same document. The chosen cycle is the one a push ACTUALLY went
out on (``pushed``), which ``_record_decisions`` writes after delivery — so it already carries
varsel's four delivery filters (the operator threshold, the not-yet-departed cut,
``one_alert_per_train``'s collapse, and the ``_notified`` dedup keyed on journey_id and situation).
It is looked up rather than re-derived because a re-derivation cannot see ``_notified`` (which
persists across cycles and redeploys), cannot see a subscription that was ``gone`` so the push
landed a cycle later, and cannot see which situation won a collapse in a cycle it is not
simulating. It asks :func:`llm_message.generation_enabled`
and never ``delivery_enabled`` — whether a *traveller* is sent model prose is a different question,
settled per deployment, and consulting it here would blank this page in prd, which is the one
deployment that generates without delivering. Bounded four ways (see the constants): a per-request
cap in backend CALLS, hourly and daily budgets tallied against replay's own allowance, a wall-clock
deadline that reserves the worst case rather than measuring elapsed time, and a process-lifetime
cache keyed on the replayed row plus the prompt version. It runs against its OWN backend latch
(:func:`llm_message.new_replay_state`), so a replay burst that drives both backends into a
configuration error cannot strip model wordings from real pushes for the next half hour.

Decision rows name the registered origin and destination. The query projects their refs only long
enough for the server to resolve National Stop Register names; the public model still forbids the
refs, ``journey_digest`` and ``message_input``. The last embeds origin, destination and departure
time in one blob and remains server-only.

Reads only. No import of main (the package rule): main injects the BigQuery client via
:func:`configure`. It DOES import :mod:`historikk`, for the constants the two surfaces must not let
drift — the served codespaces, the verdict vocabulary and the lookback bound — and it reads
``historikk_stop_places.sql`` as-is rather than cloning it. The rest of /historikk's logic is forked
rather than shared, which the plan accepts explicitly: a fix to one side must be mirrored to the
other.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

# The decision log's own `channel` vocabulary (U16), UNDER AN ALIAS: the route function below
# is itself named `decisions` and would rebind a bare module name (main.py's `impact_engine`
# trap, one module over).
import decisions as decision_log
import historikk
import llm_message
from deployment import environment_label
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_DECISIONS_QUERY = (Path(__file__).parent / "meldinger_decisions.sql").read_text()
_SUMMARY_QUERY = (Path(__file__).parent / "meldinger_summary.sql").read_text()
_FILTERS_QUERY = (Path(__file__).parent / "meldinger_filters.sql").read_text()
# The query behind /meldinger-api/messages, and the only one of the four that names
# `message_input`. Its rows are read server-side and never projected: what leaves the process is the
# wording generated FROM the input. Loaded at import for the same reason as the other three -- a
# missing .sql becomes an import error that stops the whole app at startup rather than a 500 on the
# first page load.
_MESSAGES_QUERY = (Path(__file__).parent / "meldinger_messages.sql").read_text()
# /historikk's file, deliberately SHARED rather than cloned: it takes only @refs, has no window of
# its own, and fails open to bare refs. Two copies would be two things to keep in step for no gain.
_STOP_PLACES_QUERY = (Path(__file__).parent / "historikk_stop_places.sql").read_text()

# The decision log only changes once a poller cycle, so a short cache keeps a page refresh from
# costing a BigQuery query. Capped because the key carries caller-chosen values on a public endpoint
# and this pod never restarts. 100 rather than /historikk's 500: each entry can hold up to `limit`
# hydrated rows, the pod's memory limit is the binding constraint (1228 Mi in dev, forceReplicas 1,
# uvicorn binds only after lifespan's eager loads, so an OOMKill is zero capacity for a cold start),
# and this surface adds a second cache of its own for wordings.
_CACHE_TTL_S = 120
_CACHE_MAX_ENTRIES = 100
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, Any]] = {}

_OSLO = ZoneInfo("Europe/Oslo")

# The grid both ends of the range snap to. See the module docstring for why quantizing at all is the
# bound that makes a public endpoint keyed on two times affordable.
_STEP_MINUTES = 15
_MINUTES_PER_DAY = 24 * 60

_MAX_LIMIT = historikk._MAX_LIMIT

# A ceiling on what one request may bill, on the job config rather than in a comment. Nothing else in
# this repo sets one, and KTD3's cost argument is a pruning claim no test can check: the suites are
# hermetic and no .sql here is ever executed by a test.
#
# Sized from measurement (2026-08-21): the WHOLE decision log is 181.6 MB / 161 391 rows in prd and
# 135 MB / 111 980 rows in dev, and a correctly pruned three-partition read of it is a few MB. So be
# honest about what this does and does not do — at ~11x the entire unpruned log it will NOT catch a
# pruning regression on today's table. What it does catch is a query that starts reading something
# big: the ET tables a mistyped join could reach are ~10 TiB for a two-year scan, three orders of
# magnitude above this. It is a blast-radius bound on a public endpoint, not a pruning detector.
# Revisit the number when the decision log approaches it — at that point it becomes both. It grew
# fast: measured again on 2026-09-11 prd's log is 1.53 GB, so the same 2 GiB is now ~1.3x the whole
# table rather than 11x.
#
# ONE CONSTANT for both pages since 2026-09-12 (R12, U4 step 4), imported rather than restated, the
# way `_MAX_LIMIT` already is. The two ask the same question of the same table through the same
# grouping, so a cap that differed between them would be an accident rather than a decision — and
# the derivation that is owed (four times the measured pruned, thinned 14-day scan) has to land in
# ONE place or the next reader has to guess which copy is authoritative. The derivation and the
# three conditions it waits on are written out on `historikk._MAX_BYTES_BILLED`.
_MAX_BYTES_BILLED = historikk._MAX_BYTES_BILLED
# And the one test of what a refusal by that cap looks like, shared for the reason the cap
# itself is: this page runs queries of the same shape over the same table, so a second copy
# would be a second answer to one question.
_bytes_cap_refusal = historikk._bytes_cap_refusal

# ---- The replay bounds -------------------------------------------------------------------------
# Four of them, every one a number rather than an adjective, because /messages is the only path in
# this process that makes a blocking network call to Vertex AI from a public unauthenticated
# request. What is actually being bounded is not page responsiveness: it is the per-project Vertex
# quota the POLLER shares. A replay burst that eats it makes a real push wording 429, and a 429 is a
# *transient* failure — `write_message_detailed` returns status="error" immediately without trying
# the other backend — so a traveller silently receives the template and nothing on that side says so.
#
# 1. Per request, counted in backend calls. Every deployed environment pins Gemini, so one replayed
#    subject makes at most one call. Keep this assumption aligned with the Helm environment values.
_MAX_CALLS_PER_REQUEST = 1000
# 2. Across requests, with enough burst allowance to regenerate a full page after a deploy and a
#    second ceiling that bounds sustained public traffic. Cached rows cost neither allowance.
_MAX_CALLS_PER_HOUR = 1000
_MAX_CALLS_PER_DAY = 5000
_HOUR_WINDOW_S = 60.0 * 60
_DAY_WINDOW_S = 24.0 * 60 * 60

# How many backend calls one attempt is charged for. Helm pins VARSEL_LLM_PROVIDER=gemini in dev,
# tst, and prd, and a named provider never falls through to another backend.
_CALLS_PER_ATTEMPT = 1
# A replay can recover from one momentary transport/service failure inside the same request. Every
# attempt rechecks both the deadline and the spend ledger, so retrying cannot bypass any replay bound.
_MAX_BACKEND_ATTEMPTS = 2

# 3. The wall clock, RESERVED rather than measured. `monotonic() + _WORST_CASE_CALL_S >= deadline`
#    stops a new call being started; checking elapsed time instead would bound when the last call
#    *starts*, not when the pass ends. The check runs inside the worker as its task starts, never at
#    submit time — submitted all at once, every task queues before the deadline is consulted and the
#    bound guarantees nothing.
#
#    Every environment pins one provider, so one subject can consume only the backend's 8 s timeout.
_REQUEST_DEADLINE_S = float(os.getenv("MELDINGER_REQUEST_DEADLINE_S", "30"))
_WORST_CASE_CALL_S = 8.0
# Pool width. `varsel`'s pools are the shape, not the number — they run 16/16/32
# (`_MATCH_WORKERS`/`_LLM_WORKERS`/`_PUSH_WORKERS`) on the poller thread, on a cadence nobody can
# trigger; this one is reachable from a public unauthenticated endpoint, so it is deliberately the
# narrowest of them. The deadline, not the spend ceiling, limits how much work one pool can finish.
_REPLAY_WORKERS = 4
# ...and the ceiling on how many of those pools can exist at once. `_REPLAY_WORKERS` bounds ONE
# request; without this, N concurrent requests mean N pools, N BigQuery jobs and 4N threads, because
# `messages` is `async def` and therefore deliberately outside the anyio worker pool that is the only
# other thing bounding concurrency in this process. That escape is the point of the `async` — it
# stops one replay holding a slot traveller-facing registration needs — but it also means nothing
# else counts these requests, so the bound has to be stated here.
#
# This remains deliberately independent of the spend ceilings. Raising the hourly allowance must
# not turn a public endpoint into an unbounded number of pools, BigQuery jobs, or request threads.
_MAX_CONCURRENT_REPLAYS = 2

# 4. The wording cache, separate from the BigQuery response cache above and keyed differently: the
#    subject key INCLUDING the replayed decided_at, plus llm_message.PROMPT_VERSION. Both halves are
#    load-bearing. The prompt version is what makes a prompt edit invalidate the page rather than
#    show yesterday's answer under today's brief; the replayed timestamp is what makes narrowing the
#    window — which picks a different cycle — correctly miss rather than serve the wider window's
#    wording under a different time.
#
# Successful and payload-determined results live until eviction or restart. A deployment restarts
# the single pod, while `PROMPT_VERSION` explicitly invalidates prompt edits as well. 10 000 entries
# makes repeated exploration cheap while retaining a hard memory bound.
_WORDING_MAX_ENTRIES = 10_000
# A failure that nothing about the input caused is cached far more briefly than an answer: a
# transient Vertex error must not stick a red badge on a card for half an hour after Vertex
# recovers. Cached at all, and not simply left uncached, because during an outage every refresh
# would otherwise re-spend the whole budget on the same first subjects and never reach the rest.
_WORDING_ERROR_TTL_S = 60.0
_wording_lock = threading.Lock()
_wordings: OrderedDict[tuple, tuple[float, _Wording]] = OrderedDict()
_wording_inflight: dict[tuple, threading.Event] = {}

# The hourly/daily tally, keyed BY CALLER in the shape of `alternatives._fetch_calls`. Replay spends
# only its own key, so its ceiling is its own number. Be honest about what this does here: the poller
# does not call this module at all, so its slot stays empty by construction — the keying is what
# makes that a structural property rather than a fact somebody has to keep true, and what stops a
# later caller from silently drawing replay's allowance down. The coupling it defends against is one
# layer out, at Vertex, where the two really do share a quota.
_REPLAY_PATH = "replay"
_POLLER_PATH = "poller"
_budget_lock = threading.Lock()


class _CallLedger(NamedTuple):
    hour: deque[float]
    day: deque[float]


_replay_calls: dict[str, _CallLedger] = {
    _REPLAY_PATH: _CallLedger(deque(), deque()),
    _POLLER_PATH: _CallLedger(deque(), deque()),
}

_bq_client: Any | None = None
# varsel._deterministic_body, injected. See :func:`configure`.
_deterministic_writer: Callable[[dict], str] | None = None
_warned_no_writer = False


def configure(
    *, bq_client: Any, deterministic_writer: Callable[[dict], str] | None = None
) -> None:
    """Wire in the BigQuery client and the template writer. Called once from main.lifespan.

    ``deterministic_writer`` is ``varsel._deterministic_body`` — a pure function of the stored
    ``message_input`` — passed in the way :mod:`historikk`, :mod:`impact` and :mod:`reisevalg` all
    take their dependencies. Importing :mod:`varsel` directly would pull a 147 KB module carrying the
    poller's mutable state onto a public request path for the sake of one pure function.

    Optional and fail-soft: without it every card simply has no template arm to compare the
    regenerated wording against, which is a poorer page rather than a broken one. It is the arm, not
    the answer.
    """
    global _bq_client, _deterministic_writer
    _bq_client = bq_client
    if deterministic_writer is not None:
        _deterministic_writer = deterministic_writer


class Decision(BaseModel):
    """One alert decision replayed: what we concluded, when it could have been said, what happened.

    /historikk's :class:`~historikk.Decision` field for field, with three differences.

    **Added**: ``would_send_at``, ``send_state``, ``other_pushed_on_train`` and ``prompt_version`` —
    the replayed cycle, whether a message went out for this subject at all and why not when it did
    not, whether a sibling situation about the same train pushed, and which prompt wrote the row's
    own wording.

    **Removed**: ``message`` and ``message_source``. The stored wording is deliberately not on this
    card (R12). Showing the pushed text beside one regenerated from a possibly different cycle would
    reintroduce exactly the mismatch the 2026-08-21 fix removed from /historikk, where 41 % of cards
    quoted text nobody received.

    **Whole-subject since 2026-08-22.** On the old ``decided_at`` axis the per-cycle aggregates below
    were computed over the window's slice of a subject and every one of them carried a WINDOW-SCOPED
    caveat, which is how ``ever_alerted: false`` could sit beside ``verdict: 'hit'`` and be correct.
    The departure axis puts a subject wholly in or wholly out, so these now describe the whole
    subject and mean what /historikk's mean.

    The card names the registered route, while its internal stop refs, ``journey_digest`` and the
    writer's input (``message_input``) remain absent from the public response.
    ``extra="forbid"`` is the guard that keeps that true — the SQL and this model must agree column
    for column, so a future SELECT that re-adds one fails loudly here.
    """

    model_config = ConfigDict(extra="forbid")

    situation_number: str | None = Field(description="SIRI-SX situationNumber.")
    summary: str | None = Field(description="Short Norwegian summary of the situation.")
    description: str | None = Field(description="The operator's fuller explanation.")
    operator: str | None = Field(description="Ref codespace: VYG, GOA, RUT, ...")
    line: str | None = Field(description="Line ref of the matched train.")
    mode: str | None = Field(description="Vehicle noun: 'Tog' or 'Buss'.")
    from_name: str = Field(
        description="Registered journey origin, resolved to an NSR name when possible."
    )
    to_name: str = Field(
        description="Registered journey destination, resolved to an NSR name when possible."
    )
    call_date: date | None = Field(description="Operating day of the boarding call (Europe/Oslo).")
    departure_time: datetime | None = Field(
        description="Aimed departure from the boarding stop (UTC).",
    )

    n_decisions: int | None = Field(
        description="Refresh cycles that scored this train, over its whole life.",
    )
    first_decided_at: datetime | None = Field(
        description="First time it was scored (UTC).",
    )
    last_decided_at: datetime | None = Field(
        description="Last time it was scored (UTC).",
    )
    ever_alerted: bool | None = Field(
        description="Whether any cycle crossed the alert threshold. Crossing it is not the same as "
        "a traveller being told: see pushed_any and send_state, which are narrower.",
    )
    first_alert_at: datetime | None = Field(
        description="When we first crossed the threshold (UTC); null if we never did.",
    )
    alert_lead_min: int | None = Field(
        description="Departure minus first_alert_at, in minutes. Agrees with the nightly job's "
        "figure by construction now that both describe the whole subject, but still computed here "
        "so an unlabelled subject — nearly all of a same-day window — also gets a lead time.",
    )
    max_impact_probability: float | None = Field(
        description="Highest alerting score reached.",
    )
    final_impact_probability: float | None = Field(
        description="Score at the last cycle.",
    )
    final_cancel_probability: float | None = Field(
        description="Cancellation probability at that cycle.",
    )
    final_delay_p50_min: int | None = Field(
        description="Median predicted departure delay (min) at that cycle.",
    )
    final_delay_p90_min: int | None = Field(
        description="Bad-case predicted departure delay (min) at that cycle.",
    )
    alert_threshold: float | None = Field(description="The operator's threshold applied here.")
    et_cancelled: bool | None = Field(
        description="SIRI-ET had already declared this boarding call cancelled when the decision "
        "was made, so the probabilities beside it are 1.0 by construction: we relayed a fact rather "
        "than predicted one. Taken from the nightly job where it has run, so it agrees with the "
        "segment /summary counted this decision in. Null for decisions taken before 2026-08-05.",
    )
    pushed_any: bool | None = Field(
        description="Whether a notification was ISSUED for this subject on any channel: a Web Push "
        "varsel's four delivery filters let through, or — since 2026-09-13 — a webhook event that "
        "was minted. The recorded output of those filters, never a re-derivation of them.",
    )
    n_devices: int | None = Field(
        description="How many /varsel registrations shared this journey definition — the BROWSER "
        "side only. The dedup key is per journey_id, so this card stands for that many real "
        "pushes, not one.",
    )
    channel: str | None = Field(
        default=None,
        description="Which channel(s) watched this definition: 'browser', 'webhook' or 'both'. "
        "Null for decisions taken before 2026-09-13, when webhook routes were not recorded.",
    )
    n_subscriptions: int | None = Field(
        default=None,
        description="How many webhook subscriptions shared this definition. A count and nothing "
        "more — no client, subscription or destination identity is recorded in the decision log.",
    )
    other_pushed_on_train: int | None = Field(
        description="How many OTHER situations about this same registration and train delivered a "
        "push. Non-zero beside pushed_any=false is one_alert_per_train's collapse: the traveller "
        "was told, under another situation number. Non-zero beside pushed_any=true is the open "
        "_notified gap — the dedup key is per situation while the collapse is per cycle, so a "
        "winner that flips between cycles pushes twice for one train.",
    )

    would_send_at: datetime | None = Field(
        description="When the message went out (UTC): the decided_at of the FIRST cycle that "
        "actually delivered a push AND recorded a writer's input. First, because _notified makes "
        "the first delivery the only one for that situation's life. Null means there is nothing to "
        "replay, and send_state says which of the several reasons applies.",
    )
    send_state: str | None = Field(
        description="Why this subject does or does not carry a message. 'sendt' — a push went out "
        "and its cycle is replayable. 'sendt_uten_input' — delivered, but that cycle recorded no "
        "message_input, so there is nothing to hand the prompt. 'under_terskel' — never crossed the "
        "operator's threshold. 'samlet' — alert-worthy but another situation about the same train "
        "pushed instead (one_alert_per_train). 'push_av' — this deployment has no VAPID keypair and "
        "pushes nothing. 'ikke_sendt' — alert-worthy, and nothing was delivered for this train.",
    )
    prompt_version: str | None = Field(
        description="SHA-256[:8] of the system prompt in force when the replayed cycle was written. "
        "Compare with llm_message.PROMPT_VERSION to see whether the row predates the current "
        "prompt. It does NOT separate a prompt change from sampling variance: each wording is a "
        "single draw from a non-deterministic decoder.",
    )

    actual_cancelled: bool | None = Field(description="ET declared the boarding call cancelled.")
    actual_dep_delay_min: int | None = Field(description="Realized departure delay (min).")
    actual_trouble: bool | None = Field(
        description="Realized: cancelled OR >= 10 min late. Null means UNKNOWN, not 'no trouble'.",
    )
    verdict: str | None = Field(
        description="'hit' | 'false_alarm' | 'miss' | 'correct_reject'; null when unresolved. Comes "
        "WHOLE from the nightly job — it judges the subject's entire life, not the window.",
    )
    outcome_state: str = Field(
        description="'avklart' (labelled), 'venter' (too recent to label) or 'ukjent' (labelled, "
        "but no realized call was found — permanent). A same-day window is almost entirely 'venter'.",
    )


class WindowFields(BaseModel):
    """The resolved window, echoed on every data response so a client never has to infer it.

    Four fields for one range, because two of them are what a picker needs and two are what a
    contract needs. ``from_time``/``to_time`` are the QUANTIZED times of day, which is what the page
    puts back into its 15-minute-step inputs; ``window_start``/``window_end`` are the exact half-open
    instants, so nothing about timezones or DST has to be re-derived downstream.

    ``to_time`` of ``00:00`` means the FOLLOWING midnight — the whole-day default. It cannot mean
    anything else: a literal 00:00 end would have to be at or before the start, which is rejected.
    """

    day: date = Field(description="The Europe/Oslo calendar day the trains depart on.")
    from_time: time = Field(description="Quantized start of the range, Europe/Oslo (inclusive).")
    to_time: time = Field(
        description="Quantized end of the range, Europe/Oslo (EXCLUSIVE). 00:00 means the "
        "following midnight, i.e. the rest of the day.",
    )
    window_start: datetime = Field(description="Exact first instant of the range (inclusive, UTC).")
    window_end: datetime = Field(description="Exact end of the range (EXCLUSIVE, UTC).")


class DecisionsResponse(WindowFields):
    """Decisions about the departures inside the window, earliest departure first."""

    count: int = Field(description="Number of decisions returned.")
    decisions: list[Decision] = Field(description="Matching decisions, newest departure first.")


class ReplayedMessage(BaseModel):
    """What today's prompt writes for one replayed decision — and why, when it wrote nothing.

    Aligned POSITION BY POSITION with :class:`DecisionsResponse`'s list: the same window, the same
    filters, the same ORDER BY and the same LIMIT, resolved by a query that repeats the other's
    predicate verbatim. The four identifying fields below are echoed so a client can *assert* that
    alignment rather than assume it; they are all fields ``/decisions`` already serves.

    PRIVACY: the stored ``message_input`` — origin, destination and departure time in one blob — is
    read server-side and never leaves the process. What leaves is the wording generated FROM it.
    ``extra="forbid"`` for the same reason as :class:`Decision`: it makes a future field addition
    fail here rather than quietly widen what an unauthenticated public host serves.
    """

    model_config = ConfigDict(extra="forbid")

    situation_number: str | None = Field(description="SIRI-SX situationNumber, for alignment.")
    line: str | None = Field(description="Line ref of the matched train, for alignment.")
    mode: str | None = Field(description="Vehicle noun: 'Tog' or 'Buss'.")
    departure_time: datetime | None = Field(description="Aimed departure (UTC), for alignment.")
    would_send_at: datetime | None = Field(
        description="decided_at of the replayed cycle (UTC) — when this wording could have been "
        "sent. Null when nothing in the window was replayable.",
    )

    status: str = Field(
        description="Why this entry has the content it has. 'ok' — the model answered. 'no_input' — "
        "no alert-worthy cycle in the window recorded a writer's input, so there is nothing to "
        "replay (R7): not 'no message', not an error, and no generation was attempted. "
        "'unreadable_input' — a recorded input that would not parse. 'disabled' — no backend could "
        "be ASKED (VARSEL_LLM_PROVIDER=off, no project, or every backend latched off); it never "
        "means 'this deployment sends the template'. 'unavailable' / 'error' / 'rejected' / "
        "'ungrounded' come straight off llm_message.MessageResult. 'capped', 'rate_limited' and "
        "'deadline' are this request's own bounds — the subject was not attempted, and a later "
        "request will pick it up, because the cache is consulted before the budget.",
    )
    message: str | None = Field(
        description="What today's prompt wrote, or null on every status but 'ok'.",
    )
    deterministic_message: str | None = Field(
        description="The template wording built from the SAME stored input — the comparison arm, "
        "which is the point of the page. Present whenever an input was replayable, including when "
        "generation was bounded out, and independent of whether this deployment delivers model "
        "prose to travellers.",
    )
    discarded: str | None = Field(
        description="The reply that was thrown away on 'ungrounded' and 'rejected' — a model did "
        "answer and the answer was refused. Those two are exactly what a prompt edit produces, so a "
        "status badge with no text would hide the thing being iterated on. Never delivered, never "
        "stored on a decision row.",
    )
    provider: str | None = Field(description="'claude' or 'gemini' — which backend answered.")
    model: str | None = Field(description="The model id behind that backend.")

    prompt_version: str | None = Field(
        description="SHA-256[:8] of the prompt in force when the ORIGINAL wording was written; null "
        "when nothing was replayable.",
    )
    current_prompt_version: str = Field(
        description="SHA-256[:8] of the prompt that wrote `message`. Differs from prompt_version "
        "exactly when the row predates the current brief. It does NOT separate a prompt change from "
        "sampling variance: each wording is a single draw from a non-deterministic decoder (R9).",
    )


class MessagesResponse(WindowFields):
    """Regenerated wordings for the same subjects, in the same order, as ``/decisions``."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(description="Number of subjects returned; matches /decisions' count.")
    messages: list[ReplayedMessage] = Field(
        description="One entry per subject, positionally aligned with /decisions' list.",
    )


class ConfigResponse(BaseModel):
    """What the page needs to describe itself, independent of the decision log."""

    environment: str | None = Field(
        description='Non-prod environment name ("dev", "tst") for the page to show in its title; '
        "null in prd.",
    )


class PlaceOption(BaseModel):
    """One entry in the from/to dropdowns: one physical PLACE, not one stop ref.

    A station's rail and bus sides are separate NSR refs sharing a ``parentRef``, and a registration
    lands on whichever one it was made against — so the two are offered as a single row here. ``ref``
    is the row's identity and ``n_refs`` says how many refs it stands for; the server expands it back
    to all of them when filtering, which is the part that keeps the merge from hiding half the
    place's decisions behind the row.
    """

    ref: str = Field(
        description="The value to send back as from_place/to_place. The busiest of the refs this "
        "place covers, so it is one the decision log really carries — never a synthetic id.",
    )
    name: str | None = Field(
        description="National Stop Register name, or null when the registry could not be read or "
        "does not hold the ref. Clients fall back to showing the ref.",
    )
    n_refs: int = Field(
        default=1,
        description="How many stop refs this one row stands for. Above 1 means co-located refs "
        "were merged (a station's rail and bus sides); filtering by it matches all of them.",
    )


class SxSummaryOption(BaseModel):
    """One entry in the SIRI-SX dropdown: one thing a reader can pick, not one stored string.

    :class:`PlaceOption` for summaries, and for the same reason. A station's rail and bus sides are
    separate refs standing for one place; here, several stored summaries can stand for one ANSWER.
    ``value`` is the option's identity and ``n_values`` says how many stored texts it covers; the
    server expands it back to all of them when filtering, which is the part that keeps the merge from
    hiding half the population behind the row named after it.

    Only ``no_sx`` merges today. Those rows are the ones no operator published anything about, where
    ``predict_calls.sql`` writes a placeholder summary of its own — and that placeholder's wording has
    already changed once (2026-08-20), leaving two texts meaning one thing. They are one option.
    """

    model_config = ConfigDict(extra="forbid")

    value: str = Field(
        description="What to send back as sx_summary. The busiest of the stored summaries this "
        "option covers, ties broken on the text itself — so it is one the decision log really "
        "carries, and it cannot flip between two refreshes of the same window and invalidate a "
        "selection the page is holding.",
    )
    n_values: int = Field(
        default=1,
        description="How many stored summary texts this one option stands for. Above 1 means they "
        "were merged; filtering by it matches all of them.",
    )
    no_sx: bool = Field(
        default=False,
        description="True when every row behind this option is one NO operator published a "
        "SIRI-SX message about (situationNumber `KIX:NoSituation:`), so the summary is a "
        "placeholder this codebase wrote rather than operator prose. The page labels such an option "
        "'Ingen' — the wording is deliberately the client's, since this is a fact about the data.",
    )


class FilterOptions(WindowFields):
    """What the page's dropdowns can be set to, over one window.

    Its own endpoint rather than something the client derives from ``/decisions``: that response is
    capped and ordered by departure, so options taken from it would quietly omit anything that only
    ran early in the window — and, once a filter were applied, would shrink to the selected value.

    This is also the API's allow-list. A value not in these arrays is rejected by ``/decisions`` and
    ``/summary`` rather than passed through, so a caller cannot mint an unbounded number of cache
    entries — and BigQuery queries — out of arbitrary strings. It follows that the page must fetch
    these BEFORE the data on every window change, and clear a selection the narrowed window no longer
    contains, or a window change would flash an error the user never caused.

    PRIVACY: ``from_places``/``to_places`` are the registered journeys' own endpoints, published here
    as a *vocabulary* and never served per decision. The concession — that a pair can be chosen and
    the matching departures read off — is inherited from /historikk's 2026-08-21 reversal, not newly
    made here.
    """

    operators: list[str] = Field(description="Codespaces occurring on a decision in the window.")
    lines: list[str] = Field(
        description="Line refs occurring in the window, e.g. 'VYG:Line:R11'. Full refs, not the "
        "short code the page shows: the ref is what the decision log stores and what the filter "
        "compares against, and two codespaces can share a short code.",
    )
    from_places: list[PlaceOption] = Field(description="Registered boarding places, by name.")
    to_places: list[PlaceOption] = Field(description="Registered alighting places, by name.")
    channels: list[str] = Field(
        default_factory=list,
        description="The channel values a decision can carry: 'browser', 'webhook', 'both'. A "
        "FIXED vocabulary, unlike every list above it: three values is already a bounded "
        "response-cache key, and a channel absent from this window is a true answer rather than a "
        "value to hide.",
    )
    # No default: the one construction site must pass it, or an empty dropdown would look exactly
    # like a window whose decisions carry no summary at all.
    sx_summaries: list[SxSummaryOption] = Field(
        description="The SIRI-SX options for the window, MOST COMMON FIRST and capped at 30. Order "
        "is the answer to 'which disruption texts dominated this window', so it is served as the "
        "query produced it and must not be re-sorted. The cap counts OPTIONS rather than stored "
        "texts, which is why the merge happens in SQL. The only TRUNCATED vocabulary here: a "
        "summary outside the top 30 appears on cards the page shows and is a 400 if filtered on, "
        "which is the price of allow-listing free-form text on a public endpoint.",
    )


class Segment(BaseModel):
    """One slice of the window, split on where the decision's information came from.

    Same four verdicts, same arithmetic, over a subset of the same rows. There are three:
    `predicted` (an SX message existed and the models forecast the outcome), `relayed` (ET had
    already declared it) and `no_sx` (nobody had published anything; the models read real-time data
    alone). Every labelled verdict lands in exactly one, or in `n_unsegmented`.
    """

    n_hit: int = Field(description="Alerted, and trouble came.")
    n_false_alarm: int = Field(description="Alerted, no trouble.")
    n_miss: int = Field(description="Did not alert, trouble came.")
    n_correct_reject: int = Field(description="Did not alert, no trouble.")
    precision: float | None = Field(
        description="hit / (hit + false_alarm) within this segment. Null when it alerted nothing.",
    )
    recall: float | None = Field(
        description="hit / (hit + miss) within this segment, with the same caveat as the blended "
        "figure: among SX-flagged calls on registered journeys only.",
    )
    median_lead_min_on_hits: int | None = Field(
        default=None,
        description="Median minutes between the first alert and the AIMED departure, over this "
        "segment's hit TRAINS — not its hit rows. Comes whole from the nightly job, like the "
        "verdict, so it is the subject's real first alert — which the cards' lead now also is, "
        "show; that is what keeps it comparable with /historikk's. Null when the segment has no "
        "hits.",
    )
    n_hit_with_30min_notice: int = Field(
        default=0,
        description="Hit trains in this segment alerted 30+ min before the aimed departure. "
        "Per train, like the median beside it — read it against n_hit_trains, not n_hit.",
    )
    n_hit_trains: int = Field(
        default=0,
        description="Distinct trains behind this segment's hits, collapsed on varsel's own "
        "one_alert_per_train key. The denominator for the two lead-time figures.",
    )


class Summary(WindowFields):
    """Alerting performance over the window — coverage first, then counts, then rates.

    **Coverage is first here, and it is not a diagnostic.** Verdicts arrive from the 04:00 UTC
    labelling job the following night, so on this page's default view — today, all day — nearly every
    subject is still ``venter`` and the rates below are computed over a handful of rows.
    /historikk avoids this by defaulting to 14 days on ``call_date``; this page cannot, because the
    question it answers is about a stretch of a specific day. ``n_subjects`` and ``n_labelled`` are
    what let it say so instead of showing confident-looking numbers.

    The headline figures are otherwise **blended**: every labelled decision, whether the app forecast
    the trouble or relayed a cancellation SIRI-ET had already declared. That is the honest answer to
    "can a traveller trust what this app tells them". It is not the answer to "did the models see it
    coming", because a declared cancellation is served at probability 1.0 by construction. The
    segments are the same window split on exactly that, and are the reason the blended figure can be
    quoted at all — on its own it moves with the MIX of the populations.

    Narrowed by the POPULATION filters only (day, time range, operator, line, from_place, to_place,
    sx_summary). ``verdict``, ``alerted_only`` and ``resolved_only`` narrow by OUTCOME and are
    deliberately not accepted: filtering to hits would make precision read 100 %.
    """

    n_subjects: int = Field(
        description="Every decision subject in the window — the population the card list shows. The "
        "denominator the rates below must be read against; n_labelled + n_unknown + n_pending.",
    )
    n_labelled: int = Field(
        description="How many of those carry a verdict. On a same-day window this is a few per "
        "cent, and the rates describe only that slice.",
    )
    n_hit: int = Field(description="Alerted, and trouble came.")
    n_false_alarm: int = Field(description="Alerted, no trouble.")
    n_miss: int = Field(description="Did not alert, trouble came.")
    n_correct_reject: int = Field(description="Did not alert, no trouble.")
    n_unknown: int = Field(
        description="Labelled but unresolvable — no realized call found. Never counted as a "
        "correct rejection.",
    )
    n_pending: int = Field(description="Too recent to have been labelled yet.")
    precision: float | None = Field(
        description="hit / (hit + false_alarm). Null when nothing was alerted.",
    )
    recall: float | None = Field(
        description="hit / (hit + miss) among SX-flagged calls on registered journeys — NOT true "
        "recall over all disruptions, since the models never score an unflagged call. Null when "
        "no trouble was observed.",
    )
    median_lead_min_on_hits: int | None = Field(
        description="Median minutes of warning on the TRAINS we got right. Whole-subject, from the "
        "nightly job. Since 2026-08-22 the cards' alert_lead_min agrees with it by construction.",
    )
    n_hit_with_30min_notice: int = Field(
        description="Trains alerted 30+ min before departure. Per train, like the median beside "
        "it — its denominator is n_hit_trains, never n_hit.",
    )
    n_hit_trains: int = Field(
        default=0,
        description="Distinct trains behind the hits, collapsed on varsel's one_alert_per_train key.",
    )

    predicted: Segment = Field(
        description="Decisions where ET had NOT declared the cancellation when we decided — the "
        "models' own skill. Tune a threshold on this, not on the blended figure.",
    )
    relayed: Segment = Field(
        description="Decisions where ET had ALREADY declared it: the app repeated a published fact. "
        "Real value to the traveller, no evidence about the models.",
    )
    no_sx: Segment = Field(
        default_factory=lambda: Segment(
            n_hit=0, n_false_alarm=0, n_miss=0, n_correct_reject=0,
            precision=None, recall=None, median_lead_min_on_hits=None,
        ),
        description="Decisions about a train NO SX message named at all, scored from real-time "
        "data alone. Strictly less information than either segment above and its own calibration.",
    )
    n_unsegmented: int = Field(
        description="Labelled decisions taken before the flag existed (2026-08-05), in no segment.",
    )


class _Window(NamedTuple):
    """A resolved, quantized, half-open window: one Oslo day and a range of instants inside it."""

    day: date
    from_minutes: int
    to_minutes: int
    from_ts: datetime
    to_ts: datetime

    @property
    def cache_key(self) -> tuple:
        """What identifies this window in the response cache.

        The QUANTIZED minutes, never the caller's raw times — that is what makes two requests inside
        the same 15-minute step share one entry and one BigQuery query, which is the bound the
        quantization exists for.
        """
        return (self.day, self.from_minutes, self.to_minutes)

    def fields(self) -> dict[str, Any]:
        """The window half of every response body."""
        return {
            "day": self.day,
            "from_time": _as_time(self.from_minutes),
            "to_time": _as_time(self.to_minutes),
            "window_start": self.from_ts,
            "window_end": self.to_ts,
        }


def _as_time(minutes: int) -> time:
    """Minutes-from-midnight as a time of day; the following midnight renders as 00:00.

    Unambiguous in a response because a literal 00:00 end is impossible: it would have to be at or
    before the start, which :func:`_resolve_window` rejects.
    """
    return time(hour=(minutes // 60) % 24, minute=minutes % 60)


def _oslo_instant(day: date, minutes: int) -> datetime:
    """The instant `minutes` after midnight on `day`, Europe/Oslo — 1440 being the next midnight.

    Wall-clock arithmetic on an aware datetime, which is what makes the whole-day default correct on
    the two days a year that are 23 or 25 hours long: midnight + 1440 min is the NEXT midnight, not
    "24 hours later".

    The DST edges are defined rather than special-cased. A time inside the spring-forward gap
    (02:00-03:00) resolves through the pre-transition offset, so 02:15 becomes 03:15 local; an
    ambiguous fall-back time takes the first (summer-time) occurrence. Both are one-off quarter-hours
    a year on a tuning surface, and both are stable rather than erroring.
    """
    return datetime.combine(day, time(0, 0), tzinfo=_OSLO) + timedelta(minutes=minutes)


def _floor_to_step(t: time) -> int:
    """Minutes from midnight, rounded DOWN to the 15-minute grid."""
    return (t.hour * 60 + t.minute) // _STEP_MINUTES * _STEP_MINUTES


def _ceil_to_step(t: time) -> int:
    """Minutes from midnight, rounded UP to the 15-minute grid; may be 1440 (the next midnight).

    Seconds count: 14:00:01 rounds up to 14:15, because the caller asked for a moment after 14:00
    and the range is half-open. A time already exactly on the grid stays where it is — snapping it
    forward would widen every boundary-aligned request by a quarter-hour for nothing.
    """
    total = t.hour * 60 + t.minute + (1 if (t.second or t.microsecond) else 0)
    return -(-total // _STEP_MINUTES) * _STEP_MINUTES


def _resolve_window(
    day: date | None, from_time: time | None, to_time: time | None
) -> _Window:
    """Resolve and validate the requested day and time range.

    Bounded in BOTH dimensions, and both bounds exist for the same reason: every value here lands in
    the response-cache key on a public unauthenticated endpoint, where an unbounded key space is an
    unbounded number of cache entries and BigQuery queries.

    * The day defaults to today in Europe/Oslo, and may reach ONE DAY AHEAD. That is a consequence
      of the departure axis and not a loosening: the poller scores about 12 h forward, so at 23:00
      there are already real decisions about a 00:30 departure tomorrow, and rejecting tomorrow
      would hide them. Two days ahead cannot hold a decision, so it can only be key-space and is
      refused, as is a day further back than ``historikk._MAX_WINDOW_DAYS`` — the same lookback
      /historikk bounds its window by.
    * The times default to the whole day, 00:00 to the FOLLOWING midnight, and are quantized to
      15-minute steps: from down, to up, range half-open.

    ``to_time`` at or before ``from_time`` is a 400 rather than an empty page. Equal endpoints are
    rejected too, not only inverted ones: after quantization an equal pair is an empty range exactly
    when it lands on a step boundary and a non-empty one otherwise, and a rule whose behaviour
    depends on where the clock hands happen to be is worse than a refusal.

    That refusal is checked on the RESOLVED minutes, not on the raw times, and the difference is a
    real hole rather than a tidier spelling. The raw comparison can only fire when both times were
    supplied, so an explicit ``to_time=00:00`` with ``from_time`` omitted walked straight past it:
    the default start is minute 0 and midnight ceils to minute 0, giving the half-open ``[0, 0)``
    that this docstring promises is impossible — a 200 with a permanently empty window, on the one
    endpoint whose whole job is to describe a window. Quantization preserves order for every range
    that was already valid (``from < to`` implies ``floor(from) < ceil(to)``), so moving the check
    after it refuses exactly the ranges the raw form meant to refuse, plus the one it could not see.
    """
    today = datetime.now(_OSLO).date()
    day = day or today
    if day > today + timedelta(days=1):
        raise HTTPException(
            status_code=400, detail="day must not be more than one day in the future."
        )
    if (today - day).days > historikk._MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"day is too far back; keep it within {historikk._MAX_WINDOW_DAYS} days.",
        )
    from_minutes = _floor_to_step(from_time) if from_time is not None else 0
    to_minutes = _ceil_to_step(to_time) if to_time is not None else _MINUTES_PER_DAY
    if to_minutes <= from_minutes:
        # Named, because the likeliest way to reach this is a caller who read the page's own
        # sentence -- "00:00 in «Til kl.» means the rest of the day" -- and sent it literally. The
        # page implements that by OMITTING the parameter (api.ts), which is the only way to express
        # the following midnight; a bare "to_time must be later than from_time" would send someone
        # who supplied no from_time at all looking for the wrong mistake.
        raise HTTPException(
            status_code=400,
            detail=(
                "to_time must be later than from_time; omit to_time for the rest of the day "
                "(a literal 00:00 is the START of the day, never the end)."
            ),
        )

    return _Window(
        day=day,
        from_minutes=from_minutes,
        to_minutes=to_minutes,
        from_ts=_oslo_instant(day, from_minutes),
        to_ts=_oslo_instant(day, to_minutes),
    )


def _checked_channel(channel: str | None) -> str | None:
    """Reject a channel outside `decisions.CHANNELS` -- /historikk's copy, and its reasoning.

    The value lands in the response-cache key, so the vocabulary has to be bounded; it is checked
    against the CONSTANT rather than against the window, because "no webhook route in this window"
    must be an empty page rather than a 400.
    """
    if channel is not None and channel not in decision_log.CHANNELS:
        raise HTTPException(
            status_code=400, detail=f"channel must be one of {', '.join(decision_log.CHANNELS)}."
        )
    return channel


def _checked_operator(operator: str | None) -> str | None:
    """Reject a codespace this stack does not serve.

    Not cosmetic validation: the value becomes part of the response-cache key, so accepting arbitrary
    strings on a public endpoint lets a caller mint one cache entry and one BigQuery query per
    distinct value. The vocabulary is /historikk's, imported rather than copied so the two surfaces
    cannot come to disagree about which operators exist.
    """
    if operator is not None and operator not in historikk._OPERATORS:
        raise HTTPException(
            status_code=400,
            detail=f"operator must be one of {', '.join(sorted(historikk._OPERATORS))}.",
        )
    return operator


def _checked_choice(value: str | None, field: str, allowed: list[str]) -> str | None:
    """Reject a filter value no decision in the window carries.

    Same job as :func:`_checked_operator`, and for the same reason, but the vocabulary comes from the
    data rather than a constant, so it is read from :func:`_options` (itself cached).

    A 400 rather than an empty result on purpose: "line R99 was not decided on in this window" and
    "it was, and nothing matched the rest of your filters" are different answers, and a client that
    cannot tell them apart will show the second when it means the first.
    """
    if value is not None and value not in allowed:
        raise HTTPException(status_code=400, detail=f"Unknown {field}: not present in this window.")
    return value


def _place_registry(refs: list[str]) -> dict[str, tuple[str | None, str]]:
    """StopPlace ref -> (name, place_id), read from the National Stop Register.

    Read from the registry rather than from main's picker cache, which is what /historikk did first
    and which failed twice over: that cache holds only stops in TODAY's timetable, and it collapses
    co-located refs and drops the absorbed one entirely — so Asker stasjon's rail side came back
    nameless because its bus side had won the collapse. The registry names every ref that has ever
    existed and is the thing that knows the two are one place.

    Fails open to an empty mapping: no registry means no names and no grouping, which is a dropdown
    of bare refs rather than a page that will not load.
    """
    if not refs:
        return {}
    from google.cloud import bigquery

    params = [bigquery.ArrayQueryParameter("refs", "STRING", sorted(refs))]
    try:
        rows = _query(_STOP_PLACES_QUERY, params, ("stop_places", tuple(sorted(refs))))
    except HTTPException:
        logger.warning("Stop registry unreadable; filter options will show bare refs", exc_info=True)
        return {}
    out: dict[str, tuple[str | None, str]] = {}
    for r in rows:
        row = dict(r)
        name = row.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else None
        out[row["id"]] = (name, row.get("place_id") or row["id"])
    return out


def _place_groups(counted: list[dict], registry: dict[str, tuple[str | None, str]]) -> list[dict]:
    """Collapse `[{ref, n}]` into one entry per physical place, busiest ref first within each.

    The grouping rule is main.refresh_varsel_stops', deliberately copied rather than approximated:
    same NSR parent place AND the same name means one physical stop registered more than once, while
    a parent alone does not (one parent legitimately holds Oslo S and Jernbanetorget). An **unnamed**
    ref is never merged either — its name cannot be shown to tell it from its neighbour, so folding
    it in would hide a ref behind a row nobody can read.

    The registry is fetched by the CALLER and handed in, rather than looked up here. This function
    is called twice per window — once for the from-dropdown, once for the to-dropdown — and looking
    the refs up itself meant two sequential BigQuery jobs against the same registry table whose cache
    entries could never share, because the from-refs and the to-refs are different sets even where
    they overlap (a station that is both a common boarding and a common alighting place). One job
    per window is not merely cheaper: :func:`_options` runs inside ``_message_rows`` on
    ``/messages``, AFTER the request deadline clock has started, so a second round trip is taken
    straight out of the budget that rations the replay's Vertex calls. Grouping is unaffected — the
    lookup is per ref (``historikk_stop_places.sql`` is ``WHERE id IN UNNEST(@refs) GROUP BY id``),
    so a union answers each ref exactly as a narrower ask did, and the extra keys belong to refs this
    call never looks up.

    Returns dicts of {ref, name, refs}: `ref` is the busiest member (ties on the ref itself, so the
    nomination cannot flip between refreshes and invalidate the page's selection under it), and
    `refs` is every member — what the query actually filters on.
    """
    groups: dict[tuple, list[dict]] = {}
    for entry in counted:
        name, place_id = registry.get(entry["ref"], (None, entry["ref"]))
        key = (place_id, name.casefold()) if name else (entry["ref"], "")
        groups.setdefault(key, []).append({**entry, "name": name})

    out = []
    for members in groups.values():
        members.sort(key=lambda m: (-m["n"], m["ref"]))
        primary = members[0]
        out.append(
            {
                "ref": primary["ref"],
                # From the primary, so the label and the value describe the same stop. Members can
                # only differ in case here -- the grouping key is the casefolded name.
                "name": primary["name"],
                "refs": sorted(m["ref"] for m in members),
            }
        )
    return out


def _options(window: _Window) -> dict[str, Any]:
    """The filter vocabulary for the window: two lists of strings and two lists of place groups.

    Keyed on the window alone, so every filter combination on the page shares one cache entry and one
    BigQuery query — which is what makes it affordable to run this on the validation path of
    ``/decisions`` and ``/summary`` as well as for the dropdowns themselves.
    """
    from google.cloud import bigquery

    params = [
        bigquery.ScalarQueryParameter("day", "DATE", window.day),
        bigquery.ScalarQueryParameter("from_ts", "TIMESTAMP", window.from_ts),
        bigquery.ScalarQueryParameter("to_ts", "TIMESTAMP", window.to_ts),
    ]
    rows = _query(_FILTERS_QUERY, params, ("filters", *window.cache_key))
    row = dict(rows[0]) if rows else {}

    def counted(field: str) -> list[dict]:
        return [
            {"ref": r["ref"], "n": r.get("n") or 0}
            for r in (row.get(field) or [])
            if (r or {}).get("ref")
        ]

    from_counted = counted("from_places")
    to_counted = counted("to_places")
    # ONE registry lookup covering both dropdowns. Asked per dropdown it was two sequential jobs
    # against the same table that could never share a cache entry, and this runs on the deadline
    # clock of /messages -- see _place_groups for why the union is the same answer.
    registry = _place_registry(
        sorted({c["ref"] for c in from_counted} | {c["ref"] for c in to_counted})
    )

    return {
        "operators": [v for v in (row.get("operators") or []) if v],
        "lines": [v for v in (row.get("lines") or []) if v],
        "from_places": _place_groups(from_counted, registry),
        "to_places": _place_groups(to_counted, registry),
        # Left in the order the query produced -- most common first, capped at 30. Sorting it here
        # would throw away the only thing that makes a list of free-form operator prose readable,
        # and would do it silently, since an alphabetical dropdown looks perfectly deliberate.
        #
        # Already GROUPED by the query, unlike the places above, which are grouped here: the cap has
        # to count options rather than stored texts, and only the query can cap after grouping.
        "sx_summaries": [
            {
                "value": g["value"],
                "members": [m for m in (g.get("members") or []) if m] or [g["value"]],
                "no_sx": bool(g.get("no_sx")),
            }
            for g in (dict(r) for r in (row.get("sx_summaries") or []))
            if g.get("value")
        ],
    }


def _place_refs(groups: list[dict], value: str | None, field: str) -> list[str]:
    """Expand a chosen place into EVERY ref standing for it; `[]` when nothing is chosen.

    The expansion is the whole point of grouping, not a detail of it. The dropdown offers one row per
    physical place and nominates one of its refs as the value; filtering on that ref alone would
    return only the registrations made against that side of the station and silently drop the rest —
    659 of Asker stasjon's 842 decisions on /historikk, measured on dev 2026-08-21. Empty list means
    no filter, since BigQuery has no NULL array parameter.
    """
    if value is None:
        return []
    for group in groups:
        if group["ref"] == value:
            return group["refs"]
    raise HTTPException(status_code=400, detail=f"Unknown {field}: not present in this window.")


class _Narrowing(NamedTuple):
    """The three expanded array parameters the population filters resolve to.

    A NamedTuple rather than a bare 3-tuple because all three are ``list[str]``: unpacked
    positionally, swapping two of them type-checks, runs, and quietly filters on the wrong column.
    """

    from_refs: list[str]
    to_refs: list[str]
    sx_summaries: list[str]


def _sx_summary_values(groups: list[dict], value: str | None) -> list[str]:
    """Expand a chosen SIRI-SX option into EVERY stored summary it stands for; `[]` when unset.

    :func:`_place_refs` for summaries, and the expansion is the whole point of grouping rather than a
    detail of it. The dropdown offers one option per answer and nominates one of its stored texts as
    the value; filtering on that text alone would return one slice and silently drop the rest — which
    is exactly what matching a single stop ref did to Asker stasjon's decisions on /historikk. Today
    the only merged option is the no-SX one, whose placeholder wording changed on 2026-08-20: filter
    on the nominated text alone and every row written before that afternoon disappears.

    Empty list means no filter, since BigQuery has no NULL array parameter.
    """
    if value is None:
        return []
    for group in groups:
        if group["value"] == value:
            return group["members"]
    raise HTTPException(status_code=400, detail="Unknown sx_summary: not present in this window.")


def _checked_data_filters(
    window: _Window,
    line: str | None,
    from_place: str | None,
    to_place: str | None,
    sx_summary: str | None,
) -> _Narrowing:
    """Validate the four data-driven filters and expand them, spending a query only when one of them
    is actually set. Returns the three arrays the queries take.

    Named for the filters rather than for the return value since ``sx_summary`` joined them. All
    four are allow-listed for the same reason — the value lands in the response-cache key on a
    public unauthenticated endpoint — and three of them expand, because an option can stand for more
    stored values than the one it is named after. /historikk still has this as ``_checked_places``,
    which is the shape it kept when the fork was made.
    """
    if line is None and from_place is None and to_place is None and sx_summary is None:
        return _Narrowing([], [], [])
    opts = _options(window)
    _checked_choice(line, "line", opts["lines"])
    return _Narrowing(
        _place_refs(opts["from_places"], from_place, "from_place"),
        _place_refs(opts["to_places"], to_place, "to_place"),
        # Free-form operator prose, so the allow-list is doing more work here than for a line ref:
        # an unchecked value would mint one cache entry and one BigQuery query per distinct string,
        # and there is no shape to reject a bad one by. _sx_summary_values raises the same 400 the
        # place expansion does when the value is not in the window's vocabulary.
        _sx_summary_values(opts["sx_summaries"], sx_summary),
    )


def _base_query_params(
    window: _Window,
    operator: str | None,
    line: str | None,
    from_refs: list[str],
    to_refs: list[str],
    sx_summaries: list[str],
    channel: str | None = None,
) -> list:
    """The opening parameters every decision-log query here takes, built once.

    ``/messages``, ``/decisions`` and ``/summary`` all narrow the same POPULATION the same way — the
    window, then the five filters that say which cycles, which trains, whose registered journeys and
    which published disruption — and each used to spell the parameters out again. Three verbatim
    copies of one list is three chances for them to drift, and a drift here does not fail: the
    queries name their parameters, so an omission surfaces as a query that quietly ignores a filter
    the caller set.

    Returns a fresh list each call, so a caller can append its own parameters to it without reaching
    into a shared one.
    """
    from google.cloud import bigquery

    return [
        bigquery.ScalarQueryParameter("day", "DATE", window.day),
        bigquery.ScalarQueryParameter("from_ts", "TIMESTAMP", window.from_ts),
        bigquery.ScalarQueryParameter("to_ts", "TIMESTAMP", window.to_ts),
        bigquery.ScalarQueryParameter("operator", "STRING", operator),
        bigquery.ScalarQueryParameter("line", "STRING", line),
        bigquery.ScalarQueryParameter("channel", "STRING", channel),
        bigquery.ArrayQueryParameter("from_place_refs", "STRING", from_refs),
        bigquery.ArrayQueryParameter("to_place_refs", "STRING", to_refs),
        bigquery.ArrayQueryParameter("sx_summaries", "STRING", sx_summaries),
    ]


def _list_cache_key(
    prefix: str,
    window: _Window,
    operator: str | None,
    line: str | None,
    from_place: str | None,
    to_place: str | None,
    sx_summary: str | None,
    verdict: str | None,
    alerted_only: bool,
    pushed_only: bool,
    resolved_only: bool,
    sx_only: bool,
    limit: int,
    channel: str | None = None,
) -> tuple:
    """The cache key for the two row-listing endpoints, which differ only in the leading prefix.

    Every query parameter is in it — the day, BOTH quantized times, and every filter. The key is a
    positional tuple, so a parameter added to the query and forgotten here fails silently: for two
    minutes, whichever request landed first decides what both callers see, and on ``/messages`` that
    would also decide which subjects get wordings. Built in one place so there is one such tuple to
    keep in step with the parameter list above rather than two.
    """
    return (
        prefix, *window.cache_key, operator, line, from_place, to_place, sx_summary, channel,
        verdict, alerted_only, pushed_only, resolved_only, sx_only, limit,
    )


def _query(sql: str, params: list, cache_key: tuple):
    """Run a parameterised query behind the TTL cache. Rows are returned as a list."""
    if _bq_client is None:
        raise HTTPException(status_code=503, detail="Decision log unavailable.")
    now = monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    from google.cloud import bigquery

    # maximum_bytes_billed is the backstop for a cost claim nothing else can check; see the constant.
    job_config = bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=_MAX_BYTES_BILLED
    )
    try:
        rows = list(_bq_client.query(sql, job_config=job_config).result())
    except Exception as exc:
        if _bytes_cap_refusal(exc):
            # `historikk`'s branch and its reasoning, one page over. This one is the likelier
            # of the two to be reached by a WIDE day rather than by a long period: the window here
            # is a single day, so what grows the scan is the population.
            logger.warning("Decision-log query refused by the bytes cap", exc_info=True)
            raise HTTPException(
                status_code=503,
                detail=(
                    "This day reads more of the decision log than the query cap allows. "
                    "Narrow the time range or the filters."
                ),
            ) from exc
        # Most likely cause on a fresh environment: the tables do not exist yet because no decision
        # has been recorded. Say so rather than surfacing a raw BigQuery traceback.
        logger.warning("Decision-log query failed", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Could not read the decision log; it may not have been written to yet.",
        ) from exc
    with _cache_lock:
        # Drop expired entries, then cap. Without this the dict only ever grows: the key carries
        # caller-chosen days, times and filters, and this pod does not restart.
        for key in [k for k, (ts, _) in _cache.items() if now - ts >= _CACHE_TTL_S]:
            _cache.pop(key, None)
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            for key in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX_ENTRIES // 5]:
                _cache.pop(key, None)
        _cache[cache_key] = (now, rows)
    return rows


# ---- Replay ------------------------------------------------------------------------------------
class _Wording(NamedTuple):
    """One replayed wording and its provenance — what the cache holds and the response renders.

    Deliberately NOT a :class:`ReplayedMessage`: the identifying half of that model comes from the
    row and is the same whatever the wording turned out to be, so caching it would mean caching a
    copy of the subject beside every wording, and a cache entry that could disagree with the row it
    was keyed on.
    """

    message: str | None
    deterministic: str | None
    status: str
    discarded: str | None
    provider: str | None
    model: str | None


_replay_state_lock = threading.Lock()
_replay_state: Any | None = None

# Admission control for /messages -- see _MAX_CONCURRENT_REPLAYS. Module-level rather than
# per-request for the obvious reason (a per-request semaphore bounds nothing) and safe to build at
# import on 3.10+, where a Semaphore binds to the running loop on first use rather than at
# construction. This process runs one loop.
_replay_slots = asyncio.Semaphore(_MAX_CONCURRENT_REPLAYS)


def _replay_latch() -> Any:
    """This surface's own backend latch, built on first use and kept for the process.

    Its own, and that is the whole of R15: `llm_message`'s module-global holder is what
    `varsel.check_and_notify` writes push wordings against, and a replay burst that drives both
    backends into a configuration error would otherwise strip model wordings from every real push for
    the next `_LATCH_TTL` — silently, with nothing on the traveller-facing side reporting it. The
    holder is seeded ONE-WAY from the global at construction, so replay does not re-pay for a 404 the
    poller has already discovered, and a failure discovered here never travels back.

    Built lazily rather than at import so the seeding reads the latch as it is when the page is first
    used, not as it was before the app finished starting.
    """
    global _replay_state
    with _replay_state_lock:
        if _replay_state is None:
            _replay_state = llm_message.new_replay_state()
        return _replay_state


def _wording_key(row: dict) -> tuple:
    """What identifies a wording: the subject, the cycle replayed, and the prompt that wrote it.

    The subject key is the decision log's own grain and includes `journey_digest` — which is exactly
    why this key never leaves the process. `would_send_at` is in it because narrowing the window
    picks a *different* cycle, and serving the wider window's wording under the narrower one's time
    is the mismatch the 2026-08-21 fix removed from /historikk one layer down.
    """
    return (
        row.get("journey_digest"),
        row.get("operator"),
        row.get("jkey"),
        row.get("call_date"),
        row.get("stop_order"),
        row.get("situation_number"),
        row.get("would_send_at"),
        llm_message.PROMPT_VERSION,
    )


def _cached_wording(key: tuple) -> _Wording | None:
    """A wording for this exact row and prompt, retained until restart unless transient."""
    now = monotonic()
    with _wording_lock:
        hit = _wordings.get(key)
        if hit is None:
            return None
        if now >= hit[0]:
            _wordings.pop(key, None)
            return None
        _wordings.move_to_end(key)
        return hit[1]


def _remember_wording(key: tuple, wording: _Wording) -> None:
    """Store stable results until restart and transient failures briefly.

    The ordered mapping provides O(1) LRU eviction. Transient entries expire lazily on lookup or are
    evicted with the least-recently-used stable entries when the hard capacity is reached.
    """
    stable = wording.status in ("ok", "rejected", "ungrounded")
    now = monotonic()
    with _wording_lock:
        _wordings.pop(key, None)
        while len(_wordings) >= _WORDING_MAX_ENTRIES:
            _wordings.popitem(last=False)
        _wordings[key] = (float("inf") if stable else now + _WORDING_ERROR_TTL_S, wording)


class _Budget:
    """One request's share of the request, hourly, and daily call ceilings.

    Both are claimed in the same operation and neither is decremented when the other refuses, so a
    request that runs out of shared allowance does not also burn its per-request cap — it would
    otherwise report `capped` for subjects a later request could have answered immediately.
    """

    def __init__(self, path: str = _REPLAY_PATH) -> None:
        self.path = path
        self.remaining = _MAX_CALLS_PER_REQUEST
        self.lock = threading.Lock()
        self.logged_cap = False
        self.logged_rate = False

    def reserve(self, cost: int, now: float | None = None) -> str | None:
        """Claim `cost` backend calls from both ceilings, or name the one that refused.

        Returns None when the call may proceed, else the status to report for this subject. Takes
        this request's lock and then the module's; nothing takes them in the other order.
        """
        with self.lock:
            if self.remaining < cost:
                if not self.logged_cap:
                    self.logged_cap = True
                    logger.info(
                        "Replay hit its per-request cap of %d backend calls; the remaining "
                        "subjects are reported as capped and a later request will fill them",
                        _MAX_CALLS_PER_REQUEST,
                        extra={"fields": {"event": "meldinger_bounded", "reason": "capped"}},
                    )
                return "capped"
            with _budget_lock:
                # Sample production timestamps while holding the ledger lock so concurrent workers
                # cannot append them out of order. Tests may inject an explicit monotonic value.
                now = monotonic() if now is None else now
                ledger = _replay_calls.setdefault(self.path, _CallLedger(deque(), deque()))
                while ledger.hour and now - ledger.hour[0] >= _HOUR_WINDOW_S:
                    ledger.hour.popleft()
                while ledger.day and now - ledger.day[0] >= _DAY_WINDOW_S:
                    ledger.day.popleft()
                refused_window = None
                if len(ledger.hour) + cost > _MAX_CALLS_PER_HOUR:
                    refused_window = "hour"
                elif len(ledger.day) + cost > _MAX_CALLS_PER_DAY:
                    refused_window = "day"
                if refused_window is not None:
                    if not self.logged_rate:
                        self.logged_rate = True
                        logger.warning(
                            "Replay rate-limited on the %s path (%d calls/hour, %d calls/day); "
                            "regenerating nothing further in this request",
                            self.path,
                            _MAX_CALLS_PER_HOUR,
                            _MAX_CALLS_PER_DAY,
                            extra={
                                "fields": {
                                    "event": "meldinger_bounded",
                                    "reason": "rate_limited",
                                    "path": self.path,
                                    "window": refused_window,
                                }
                            },
                        )
                    return "rate_limited"
                ledger.hour.extend([now] * cost)
                ledger.day.extend([now] * cost)
            self.remaining -= cost
            return None


def _deterministic(payload: dict) -> str | None:
    """The template wording for this stored input, or None when it could not be produced.

    Fail-soft in both directions. An uninjected writer says so once and costs the comparison arm, not
    the page; an exception from the writer costs one card's arm. Neither is worth a 500 on a surface
    whose job is to show what the model wrote.
    """
    global _warned_no_writer
    writer = _deterministic_writer
    if writer is None:
        if not _warned_no_writer:
            _warned_no_writer = True
            logger.warning(
                "No deterministic writer was injected; /meldinger-api/messages will show the "
                "regenerated wording without its template arm",
            )
        return None
    try:
        return writer(payload)
    except Exception:
        logger.warning("Could not build the template wording for a replayed input", exc_info=True)
        return None


def _replay_one(row: dict, *, budget: _Budget, deadline: float, generating: bool) -> _Wording:
    """Regenerate one subject's wording, or say why it was not regenerated. Runs on the pool.

    The order of the guards is the contract. A subject with no stored input is R7's state and never
    reaches the budget; a deployment that cannot ask a backend reports `disabled` rather than a
    bound it never hit; the deadline is consulted before the reservation, so a request that is out
    of time does not also spend the allowance a later one would use.
    """
    raw = row.get("message_input")
    if raw is None:
        # R7. No alert-worthy cycle in the window recorded a writer's input, so there is nothing to
        # replay. Its own state, never an empty wording, never an attempt, never a charge.
        return _Wording(None, None, "no_input", None, None, None)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "A recorded message_input would not parse; reporting it rather than failing the page",
            extra={"fields": {"event": "meldinger_bounded", "reason": "unreadable_input"}},
        )
        return _Wording(None, None, "unreadable_input", None, None, None)

    # Built for every replayable subject, including the ones that are about to be bounded out: it is
    # the arm the regenerated wording is read against, it costs no network, and a card that showed a
    # bound with no text at all would be strictly less useful than one that showed the template.
    deterministic = _deterministic(payload)
    if not generating:
        return _Wording(None, deterministic, "disabled", None, None, None)

    result = None
    for attempt in range(_MAX_BACKEND_ATTEMPTS):
        now = monotonic()
        if now + _WORST_CASE_CALL_S >= deadline:
            if result is None:
                return _Wording(None, deterministic, "deadline", None, None, None)
            break
        refusal = budget.reserve(_CALLS_PER_ATTEMPT)
        if refusal is not None:
            if result is None:
                return _Wording(None, deterministic, refusal, None, None, None)
            break

        result = llm_message.write_message_detailed(
            payload,
            # Deliberately EMPTY rather than the row's journey_digest. `write_message_detailed` puts
            # `key_digest(journey_key)` on its log record, and the digest is already that function's
            # output — passing it would hash an already-hashed value and mint a second identifier that
            # looks joinable with the live event="llm_message" trail and is not. The unhashed digest
            # goes on this module's own record below instead, which is what keeps a replay record
            # joinable with the decision row and with the live trail.
            journey_key="",
            situation_number=row.get("situation_number") or "",
            # Its own latch (R15) and its own client cache; `replay=True` marks the audit record and
            # suppresses the prompt/reply DEBUG record, which dev has switched on and which would
            # otherwise publish a traveller's origin, destination and departure time at page-load
            # volume.
            state=_replay_latch(),
            replay=True,
        )
        if result.status != "error":
            break
        if attempt + 1 < _MAX_BACKEND_ATTEMPTS:
            logger.info(
                "Retrying a transient replay backend failure",
                extra={
                    "fields": {
                        "event": "meldinger_replay_retry",
                        "replay": True,
                        "journey_digest": row.get("journey_digest"),
                        "situation_number": row.get("situation_number"),
                        "provider": result.provider,
                        "attempt": attempt + 1,
                    }
                },
            )

    assert result is not None
    wording = _Wording(
        message=result.text,
        deterministic=deterministic,
        status=result.status,
        discarded=result.discarded,
        provider=result.provider,
        model=result.model,
    )
    _remember_wording(_wording_key(row), wording)
    # The join key, unhashed, on this surface's own event. Not event="llm_message": that record
    # already exists for this call, carries the provider and the `replay` marker, and is what
    # operators' existing queries count — emitting a second one under the same name would double
    # every such count. This one adds what that record cannot carry.
    logger.info(
        "Replayed %s under prompt %s: %s",
        row.get("situation_number"),
        llm_message.PROMPT_VERSION,
        result.status,
        extra={
            "fields": {
                "event": "meldinger_replay",
                "replay": True,
                "journey_digest": row.get("journey_digest"),
                "situation_number": row.get("situation_number"),
                "result": result.status,
                "provider": result.provider,
                "model": result.model,
                "prompt_version": llm_message.PROMPT_VERSION,
                "row_prompt_version": row.get("prompt_version"),
            }
        },
    )
    return wording


def _replay_one_cached(row: dict, *, budget: _Budget, deadline: float, generating: bool) -> _Wording:
    """Generate one cache key once, even when both admitted requests miss concurrently."""
    key = _wording_key(row)
    hit = _cached_wording(key)
    if hit is not None:
        return hit
    with _wording_lock:
        # Close the gap between the optimistic lookup and claiming ownership: another request may
        # have completed and removed its flight while this worker was waiting for the lock.
        cached = _wordings.get(key)
        if cached is not None and monotonic() < cached[0]:
            _wordings.move_to_end(key)
            return cached[1]
        if cached is not None:
            _wordings.pop(key, None)
        event = _wording_inflight.get(key)
        owner = event is None
        if owner:
            event = threading.Event()
            _wording_inflight[key] = event
    assert event is not None
    if not owner:
        event.wait(max(0.0, deadline - monotonic()))
        hit = _cached_wording(key)
        if hit is not None:
            return hit
        return _replay_one(row, budget=budget, deadline=deadline, generating=generating)
    try:
        return _replay_one(row, budget=budget, deadline=deadline, generating=generating)
    finally:
        with _wording_lock:
            _wording_inflight.pop(key, None)
            event.set()


def _message_rows(
    window: _Window,
    *,
    operator: str | None,
    line: str | None,
    from_place: str | None,
    to_place: str | None,
    sx_summary: str | None,
    channel: str | None,
    verdict: str | None,
    alerted_only: bool,
    pushed_only: bool,
    resolved_only: bool,
    sx_only: bool,
    limit: int,
) -> list[dict]:
    """The replay inputs for the window, behind the same cache and allow-list as /decisions.

    Blocking: BigQuery, and the allow-list lookup that may itself be a query. Called on the pool, so
    the event loop never waits on a socket.

    The filters are spelled out as keyword-only parameters rather than taken as ``**filters`` and
    read back by string key, so this signature is the same declaration ``decisions()`` already
    carries. A misspelled or forgotten name is then a ``TypeError`` at the call — which is where the
    only caller is — instead of a ``KeyError`` raised on a worker thread partway through, after the
    allow-list lookup has already run and with nothing left to answer the request but a 500.
    """
    from google.cloud import bigquery

    narrowed = _checked_data_filters(window, line, from_place, to_place, sx_summary)
    channel = _checked_channel(channel)
    params = _base_query_params(window, operator, line, *narrowed, channel=channel) + [
        bigquery.ScalarQueryParameter("verdict", "STRING", verdict),
        bigquery.ScalarQueryParameter("alerted_only", "BOOL", alerted_only),
        bigquery.ScalarQueryParameter("pushed_only", "BOOL", pushed_only),
        bigquery.ScalarQueryParameter("resolved_only", "BOOL", resolved_only),
        bigquery.ScalarQueryParameter("sx_only", "BOOL", sx_only),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    # Same key shape as /decisions', with its own prefix; see _list_cache_key for what rides on it.
    key = _list_cache_key(
        "messages", window, operator, line, from_place, to_place, sx_summary,
        verdict, alerted_only, pushed_only, resolved_only, sx_only, limit, channel,
    )
    return [dict(r) for r in _query(_MESSAGES_QUERY, params, key)]


def _replayed_message(row: dict, wording: _Wording) -> ReplayedMessage:
    """One response entry: the row's identity, and what came of replaying it.

    Note what is NOT copied across from the row — `journey_digest`, and the `message_input` the
    wording was built from. The model is `extra="forbid"`, so adding either here fails loudly.
    """
    return ReplayedMessage(
        situation_number=row.get("situation_number"),
        line=row.get("line"),
        mode=row.get("mode"),
        departure_time=row.get("departure_time"),
        would_send_at=row.get("would_send_at"),
        status=wording.status,
        message=wording.message,
        deterministic_message=wording.deterministic,
        discarded=wording.discarded,
        provider=wording.provider,
        model=wording.model,
        prompt_version=row.get("prompt_version"),
        current_prompt_version=llm_message.PROMPT_VERSION,
    )


async def _replay(
    rows: list[dict],
    pool: ThreadPoolExecutor,
    loop: asyncio.AbstractEventLoop,
    deadline: float,
) -> list[ReplayedMessage]:
    """Walk the WHOLE subject list, serving cached wordings free and spending the budget on misses.

    The cache is consulted before the budget, so page reloads spend no allowance on wordings this
    process has already generated.

    `generation_enabled` is asked ONCE per request rather than per subject, and asked of this
    surface's own latch. Asked per subject it would flip mid-pass — a configuration error latches a
    backend off — and half the page would read `disabled` while the other half read `error` for the
    same cause.
    """
    generating = llm_message.generation_enabled(_replay_latch())
    budget = _Budget()
    out: list[_Wording | None] = [None] * len(rows)
    pending: list[int] = []
    for i, row in enumerate(rows):
        if row.get("message_input") is None:
            out[i] = _Wording(None, None, "no_input", None, None, None)
            continue
        hit = _cached_wording(_wording_key(row))
        if hit is not None:
            out[i] = hit
            continue
        pending.append(i)

    if pending:
        futures = [
            loop.run_in_executor(
                pool,
                partial(
                    _replay_one_cached,
                    rows[i],
                    budget=budget,
                    deadline=deadline,
                    generating=generating,
                ),
            )
            for i in pending
        ]
        for i, wording in zip(pending, await asyncio.gather(*futures), strict=True):
            out[i] = wording

    return [
        _replayed_message(row, wording)
        for row, wording in zip(rows, out, strict=True)
        if wording is not None
    ]


router = APIRouter(prefix="/meldinger-api", tags=["meldinger"])


@router.get("/config", summary="Which deployment is serving this page")
def config() -> ConfigResponse:
    """The environment name the page appends to its title, or null in prd.

    Its own endpoint rather than a field on ``/summary`` deliberately: both data endpoints 503 when
    the decision log cannot be read, which is exactly when knowing whether you are looking at dev or
    prd matters most. This one touches no BigQuery.
    """
    return ConfigResponse(environment=environment_label())


@router.get(
    "/filters",
    summary="The values the page's dropdowns can be set to",
    response_description="Operators, lines and registered places departing inside the window.",
)
def filters(
    day: date | None = Query(
        default=None, description="Oslo day of the DEPARTURES (default: today; tomorrow allowed)."
    ),
    from_time: time | None = Query(
        default=None,
        description="Start of the time range, Europe/Oslo (default: 00:00). Rounded DOWN to a "
        "15-minute step.",
    ),
    to_time: time | None = Query(
        default=None,
        description="End of the time range, Europe/Oslo, EXCLUSIVE (default: the following "
        "midnight). Rounded UP to a 15-minute step.",
    ),
) -> FilterOptions:
    """Every operator, line, boarding place and alighting place departing inside the window.

    Scoped to the same window as the data, not to the whole log, so the dropdowns offer exactly what
    the page can show. The consequence is that a selection can go stale when the window narrows —
    which is the client's job to reconcile, and the reason the data endpoints answer 400 rather than
    an empty list for a value that is not here.

    Deliberately *not* narrowed by the other filters: options derived from the current selection
    shrink to it, and there is then no way back to the rest of the window.
    """
    window = _resolve_window(day, from_time, to_time)
    opts = _options(window)

    def places(groups: list[dict]) -> list[PlaceOption]:
        # Sorted by what the reader sees. The SQL sorts by ref, which puts "NSR:StopPlace:337" next
        # to "NSR:StopPlace:338" and their names nowhere near each other.
        #
        # Unnamed refs go LAST rather than under their own text: sorting the two kinds together
        # interleaves them, which reads as a list that lost its ordering. Behind the named ones they
        # read as what they are — the tail the registry could not place.
        options = [PlaceOption(ref=g["ref"], name=g["name"], n_refs=len(g["refs"])) for g in groups]
        return sorted(options, key=lambda o: (o.name is None, (o.name or o.ref).casefold()))

    return FilterOptions(
        **window.fields(),
        operators=opts["operators"],
        lines=opts["lines"],
        from_places=places(opts["from_places"]),
        to_places=places(opts["to_places"]),
        # Not sorted at all: the query already ordered these by how often each option occurs, which
        # is the only ordering that makes free-form prose readable in a dropdown. `n_values` is the
        # count of stored texts behind the option, the same thing `n_refs` is for a place.
        sx_summaries=[
            SxSummaryOption(value=g["value"], n_values=len(g["members"]), no_sx=g["no_sx"])
            for g in opts["sx_summaries"]
        ],
    )


@router.get(
    "/decisions",
    summary="Alert decisions taken in a day-and-time window, with their realized outcome",
    response_description="Decisions about departures inside the window, earliest first.",
)
def decisions(
    day: date | None = Query(
        default=None, description="Oslo day of the DEPARTURES (default: today; tomorrow allowed)."
    ),
    from_time: time | None = Query(
        default=None, description="Start of the range, Europe/Oslo (default 00:00; rounded down)."
    ),
    to_time: time | None = Query(
        default=None,
        description="End of the range, Europe/Oslo, EXCLUSIVE (default the following midnight; "
        "rounded up).",
    ),
    operator: str | None = Query(default=None, description="Filter to one codespace, e.g. 'VYG'."),
    line: str | None = Query(
        default=None,
        description="Filter to one line REF, e.g. 'VYG:Line:R11' — not the short code the page "
        "shows, since two codespaces can share one. Values come from /meldinger-api/filters.",
    ),
    from_place: str | None = Query(
        default=None,
        description="Filter to registrations boarding at this StopPlace ref. Values come from "
        "/meldinger-api/filters, which is also the allow-list this is checked against.",
    ),
    to_place: str | None = Query(
        default=None, description="Filter to registrations alighting at this StopPlace ref."
    ),
    channel: str | None = Query(
        default=None,
        description="Filter to decisions watched on one channel: 'browser' (a /varsel "
        "registration), 'webhook' (a client subscription) or 'both'. A POPULATION filter, so it "
        "reaches /summary too. Decisions taken before 2026-09-13 carry no channel and are excluded "
        "by any value here.",
    ),
    sx_summary: str | None = Query(
        default=None,
        description="Filter to decisions carrying exactly this SIRI-SX summary. Values come from "
        "/meldinger-api/filters, which offers the window's 30 most common and is also the "
        "allow-list this is checked against — a summary outside that cap is a 400, not an empty "
        "page. Distinct from `sx_only`, which is about whether an operator published anything at "
        "all rather than about which text.",
    ),
    verdict: str | None = Query(
        default=None,
        description="Filter to 'hit', 'false_alarm', 'miss' or 'correct_reject'. Excludes "
        "unresolved decisions, which have no verdict to match — nearly all of a same-day window.",
    ),
    alerted_only: bool = Query(
        default=False,
        description="Only decisions that crossed the operator's threshold. Leave off to see the "
        "decisions we did NOT alert on — the ones a miss is made of.",
    ),
    pushed_only: bool = Query(
        default=False,
        description="Only decisions a traveller was actually PUSHED about — strictly narrower than "
        "alerted_only, because an alert-worthy subject can still reach nobody: another situation "
        "about the same train won one_alert_per_train's collapse, or _notified had already "
        "delivered for that situation on an earlier cycle.",
    ),
    resolved_only: bool = Query(
        default=False,
        description="Only decisions whose realized outcome is known (outcome_state 'avklart').",
    ),
    sx_only: bool = Query(
        default=False,
        description="Only decisions about a call an operator published a SIRI-SX message for — "
        "i.e. drop the rows this codebase minted a situation number for (`KIX:`).",
    ),
    limit: int = Query(default=200, ge=1, le=_MAX_LIMIT, description="Maximum rows to return."),
) -> DecisionsResponse:
    """Every alert decision about a departure inside the window, whether or not it produced a push.

    Non-alerting decisions are included by default on purpose: they are the only evidence a miss
    leaves behind, and a log filtered to what we did send can only ever measure precision. Pass
    ``pushed_only`` for the narrower list — the messages a traveller actually received — and note it
    is narrower than ``alerted_only`` rather than the same cut spelled differently: an alert-worthy
    subject reaches nobody when another situation about its train wins ``one_alert_per_train``'s
    collapse, or when ``_notified`` already delivered for that situation on an earlier cycle.

    On this page the sub-threshold rows matter for a second reason — such a cycle records a
    ``message_input`` too, but it is never the replayed one, so a subject that never crossed the
    gate has no replayable cycle at all. ``send_state`` names which of those states each card is in.
    """
    from google.cloud import bigquery

    window = _resolve_window(day, from_time, to_time)
    operator = _checked_operator(operator)
    channel = _checked_channel(channel)
    narrowed = _checked_data_filters(window, line, from_place, to_place, sx_summary)
    if verdict is not None and verdict not in historikk._VERDICTS:
        raise HTTPException(
            status_code=400, detail=f"verdict must be one of {', '.join(historikk._VERDICTS)}."
        )
    params = _base_query_params(window, operator, line, *narrowed, channel=channel) + [
        bigquery.ScalarQueryParameter("verdict", "STRING", verdict),
        bigquery.ScalarQueryParameter("alerted_only", "BOOL", alerted_only),
        bigquery.ScalarQueryParameter("pushed_only", "BOOL", pushed_only),
        bigquery.ScalarQueryParameter("resolved_only", "BOOL", resolved_only),
        bigquery.ScalarQueryParameter("sx_only", "BOOL", sx_only),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    # Every query parameter is in the key -- the day, BOTH quantized times, and every filter; see
    # _list_cache_key for why it is positional and what a forgotten parameter costs.
    key = _list_cache_key(
        "decisions", window, operator, line, from_place, to_place, sx_summary,
        verdict, alerted_only, pushed_only, resolved_only, sx_only, limit, channel,
    )
    rows = _query(_DECISIONS_QUERY, params, key)
    raw_rows = [dict(r) for r in rows]
    refs = sorted(
        {
            row[field]
            for row in raw_rows
            for field in ("from_stop", "to_stop")
            if row.get(field)
        }
    )
    registry = _place_registry(refs)
    decisions_out = []
    for row in raw_rows:
        from_ref = row.pop("from_stop", None)
        to_ref = row.pop("to_stop", None)
        row["from_name"] = registry.get(from_ref, (None, from_ref))[0] or from_ref or "Ukjent"
        row["to_name"] = registry.get(to_ref, (None, to_ref))[0] or to_ref or "Ukjent"
        decisions_out.append(Decision(**row))
    return DecisionsResponse(
        **window.fields(),
        count=len(rows),
        decisions=decisions_out,
    )


@router.get(
    "/summary",
    summary="Alerting performance over the departures in a window",
    response_description="Verdict counts, precision/recall, and how much of the window is resolved.",
)
def summary(
    day: date | None = Query(
        default=None, description="Oslo day of the DEPARTURES (default: today; tomorrow allowed)."
    ),
    from_time: time | None = Query(
        default=None, description="Start of the range, Europe/Oslo (default 00:00; rounded down)."
    ),
    to_time: time | None = Query(
        default=None,
        description="End of the range, Europe/Oslo, EXCLUSIVE (default the following midnight; "
        "rounded up).",
    ),
    operator: str | None = Query(default=None, description="Filter to one codespace, e.g. 'VYG'."),
    line: str | None = Query(default=None, description="Filter to one line ref, e.g. 'VYG:Line:R11'."),
    from_place: str | None = Query(
        default=None, description="Filter to registrations boarding at this StopPlace ref."
    ),
    to_place: str | None = Query(
        default=None, description="Filter to registrations alighting at this StopPlace ref."
    ),
    channel: str | None = Query(
        default=None,
        description="Filter to decisions watched on one channel: 'browser' (a /varsel "
        "registration), 'webhook' (a client subscription) or 'both'. A POPULATION filter, so it "
        "reaches /summary too. Decisions taken before 2026-09-13 carry no channel and are excluded "
        "by any value here.",
    ),
    sx_summary: str | None = Query(
        default=None,
        description="Filter to decisions carrying exactly this SIRI-SX summary. Accepted here, "
        "unlike the outcome filters, because it narrows the POPULATION: it says which decisions "
        "exist, not which of them to show.",
    ),
) -> Summary:
    """Counts and rates over the window, with the unresolved share reported alongside.

    `n_subjects`, `n_labelled`, `n_unknown` and `n_pending` are part of the contract, not
    diagnostics. On this page they carry more weight than on /historikk: the default window is
    today, verdicts arrive from the 04:00 UTC job the following night, so nearly everything is
    `venter` and a precision computed over a handful of rows is a different claim from one computed
    over almost all of them. A caller cannot tell which it has without them.

    The filters it accepts are exactly the ones that narrow the POPULATION — which cycles, which
    trains, on whose registered journeys, about which published disruption — so these rates describe
    the same slice the list beside them shows. `verdict`, `alerted_only`, `resolved_only` and
    `sx_only` narrow by OUTCOME instead and are deliberately not accepted here at all: filtering to
    hits would make precision read 100 %.
    """
    window = _resolve_window(day, from_time, to_time)
    operator = _checked_operator(operator)
    channel = _checked_channel(channel)
    narrowed = _checked_data_filters(window, line, from_place, to_place, sx_summary)
    # Only the population filters, so no extras on top of the shared block -- and for the same reason
    # the key carries only those: /summary deliberately accepts none of the outcome filters.
    params = _base_query_params(window, operator, line, *narrowed, channel=channel)
    rows = _query(
        _SUMMARY_QUERY,
        params,
        ("summary", *window.cache_key, operator, line, from_place, to_place, sx_summary, channel),
    )
    row = dict(rows[0]) if rows else {}

    def segment(suffix: str) -> Segment:
        """One split of the window. Counts default to 0; rates stay None — a rate over nothing is
        not zero, and rendering it as 0 % would read as "we got everything wrong"."""
        return Segment(
            n_hit=row.get(f"n_hit_{suffix}") or 0,
            n_false_alarm=row.get(f"n_false_alarm_{suffix}") or 0,
            n_miss=row.get(f"n_miss_{suffix}") or 0,
            n_correct_reject=row.get(f"n_correct_reject_{suffix}") or 0,
            precision=row.get(f"precision_{suffix}"),
            recall=row.get(f"recall_{suffix}"),
            median_lead_min_on_hits=row.get(f"median_lead_min_on_hits_{suffix}"),
            n_hit_with_30min_notice=row.get(f"n_hit_with_30min_notice_{suffix}") or 0,
            n_hit_trains=row.get(f"n_hit_trains_{suffix}") or 0,
        )

    return Summary(
        **window.fields(),
        n_subjects=row.get("n_subjects") or 0,
        n_labelled=row.get("n_labelled") or 0,
        n_hit=row.get("n_hit") or 0,
        n_false_alarm=row.get("n_false_alarm") or 0,
        n_miss=row.get("n_miss") or 0,
        n_correct_reject=row.get("n_correct_reject") or 0,
        n_unknown=row.get("n_unknown") or 0,
        n_pending=row.get("n_pending") or 0,
        precision=row.get("precision"),
        recall=row.get("recall"),
        median_lead_min_on_hits=row.get("median_lead_min_on_hits"),
        n_hit_with_30min_notice=row.get("n_hit_with_30min_notice") or 0,
        n_hit_trains=row.get("n_hit_trains") or 0,
        predicted=segment("predicted"),
        relayed=segment("relayed"),
        no_sx=segment("no_sx"),
        n_unsegmented=row.get("n_unsegmented") or 0,
    )


@router.get(
    "/messages",
    summary="What we would have sent out for the departures in a window",
    response_description="One regenerated wording per subject, aligned with /decisions' list.",
)
async def messages(
    day: date | None = Query(
        default=None, description="Oslo day of the DEPARTURES (default: today; tomorrow allowed)."
    ),
    from_time: time | None = Query(
        default=None, description="Start of the range, Europe/Oslo (default 00:00; rounded down)."
    ),
    to_time: time | None = Query(
        default=None,
        description="End of the range, Europe/Oslo, EXCLUSIVE (default the following midnight; "
        "rounded up).",
    ),
    operator: str | None = Query(default=None, description="Filter to one codespace, e.g. 'VYG'."),
    line: str | None = Query(
        default=None, description="Filter to one line REF, from /meldinger-api/filters."
    ),
    from_place: str | None = Query(
        default=None, description="Filter to registrations boarding at this StopPlace ref."
    ),
    to_place: str | None = Query(
        default=None, description="Filter to registrations alighting at this StopPlace ref."
    ),
    channel: str | None = Query(
        default=None,
        description="Filter to decisions watched on one channel: 'browser' (a /varsel "
        "registration), 'webhook' (a client subscription) or 'both'. A POPULATION filter, so it "
        "reaches /summary too. Decisions taken before 2026-09-13 carry no channel and are excluded "
        "by any value here.",
    ),
    sx_summary: str | None = Query(
        default=None,
        description="Filter to decisions carrying exactly this SIRI-SX summary, from "
        "/meldinger-api/filters. Pass the SAME value as to /decisions: the two lists are aligned by "
        "position, and a filter set on one of them only would pair every card with another's "
        "wording.",
    ),
    verdict: str | None = Query(
        default=None,
        description="Filter to 'hit', 'false_alarm', 'miss' or 'correct_reject'.",
    ),
    alerted_only: bool = Query(
        default=False, description="Only decisions that crossed the operator's threshold."
    ),
    pushed_only: bool = Query(
        default=False, description="Only decisions a traveller was actually PUSHED about."
    ),
    resolved_only: bool = Query(
        default=False, description="Only decisions whose realized outcome is known."
    ),
    sx_only: bool = Query(
        default=False,
        description="Only decisions about a call an operator published a SIRI-SX message for.",
    ),
    limit: int = Query(
        default=200,
        ge=1,
        le=_MAX_LIMIT,
        description="Maximum subjects to return. Pass the SAME value as to /decisions: the two "
        "lists are aligned by position, and a different limit truncates one of them.",
    ),
) -> MessagesResponse:
    """Replay each subject's stored writer input through today's prompt.

    Takes the same window and the same allow-listed filters as ``/decisions`` and NEVER a
    caller-supplied row key: what may be regenerated is decided by a query this module wrote, over a
    vocabulary ``/filters`` published, so a caller cannot name an arbitrary decision — nor mint an
    unbounded number of cache entries and Vertex calls out of arbitrary strings.

    Asks :func:`llm_message.generation_enabled` and never ``delivery_enabled`` or
    ``varsel._deliverable_message``. The two switches are not interchangeable: generation says
    whether a wording can be WRITTEN, delivery only what a traveller RECEIVES. prd runs generation on
    and delivery off — precisely the combination that makes its real journeys comparable — so a page
    that consulted the delivery gate would be blank exactly where the data is worth looking at.

    **Declared ``async def``, which is not a style choice.** Every other route in this process is a
    plain ``def`` and is therefore served on the shared anyio worker threadpool — ``/impact``, the
    ``/varsel-api`` device and journey writes, the ``/historikk`` routes. A handler that can hold one
    of those slots for its full 30 s deadline would starve traveller-facing registration from a
    public unauthenticated endpoint, and with no ingress cutoff nothing reclaims the slot. Declared
    ``async``, this coroutine only awaits: both the BigQuery read and the generation fan-out run on
    this request's own pool.

    Always 200 (or the same 400s and 503s ``/decisions`` raises). A bound that binds, a backend that
    fails and a subject with nothing to replay are all reported per entry, because a page that lost
    every card to one subject's problem would be useless for the one thing it exists to do.
    """
    deadline = monotonic() + _REQUEST_DEADLINE_S
    window = _resolve_window(day, from_time, to_time)
    operator = _checked_operator(operator)
    if verdict is not None and verdict not in historikk._VERDICTS:
        raise HTTPException(
            status_code=400, detail=f"verdict must be one of {', '.join(historikk._VERDICTS)}."
        )

    # Admission control BEFORE anything is allocated -- see `_MAX_CONCURRENT_REPLAYS`. Checking
    # `locked()` first is what makes the refusal immediate rather than a queue: the semaphore alone
    # would make a third caller WAIT behind a 30 s deadline it cannot see, on a public
    # unauthenticated endpoint. The check and the acquire are not one atomic step, so a rare third
    # request can still slip in and wait briefly; the semaphore is what keeps the real ceiling, and
    # the check is what keeps the common answer honest.
    if _replay_slots.locked():
        raise HTTPException(
            status_code=503,
            detail="Too many replays running; try again shortly.",
        )
    async with _replay_slots:
        return await _messages(
            window, operator, line, from_place, to_place, sx_summary, channel, verdict,
            alerted_only, pushed_only, resolved_only, sx_only, limit, deadline,
        )


async def _messages(
    window: _Window,
    operator: str | None,
    line: str | None,
    from_place: str | None,
    to_place: str | None,
    sx_summary: str | None,
    channel: str | None,
    verdict: str | None,
    alerted_only: bool,
    pushed_only: bool,
    resolved_only: bool,
    sx_only: bool,
    limit: int,
    deadline: float,
) -> MessagesResponse:
    """The body of :func:`messages`, run while holding a replay slot.

    Split out only so the slot is released by one `async with` around the whole of it, rather than
    by a `try/finally` wrapped around a long body where a later edit could return past the release.
    """
    loop = asyncio.get_running_loop()
    # One pool per request, the shape `varsel.check_and_notify`'s delivery loop uses. Per request
    # rather than module-wide so its threads cannot outlive the work: this pod runs for weeks.
    with ThreadPoolExecutor(
        max_workers=_REPLAY_WORKERS, thread_name_prefix="meldinger-replay"
    ) as pool:
        rows = await loop.run_in_executor(
            pool,
            partial(
                _message_rows,
                window,
                operator=operator,
                line=line,
                from_place=from_place,
                to_place=to_place,
                sx_summary=sx_summary,
                channel=channel,
                verdict=verdict,
                alerted_only=alerted_only,
                pushed_only=pushed_only,
                resolved_only=resolved_only,
                sx_only=sx_only,
                limit=limit,
            ),
        )
        replayed = await _replay(rows, pool, loop, deadline)
    return MessagesResponse(**window.fields(), count=len(replayed), messages=replayed)
