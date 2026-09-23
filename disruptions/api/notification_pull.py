"""The journey-keyed public read: `GET /notification-api/v1/journey-alerts` (plan 2026-09-10-001).

The Entur app's favourites panel asks "what would /varsel be warning about on this trip right now?"
for a definition nobody registered, with no credential, and gets back the alert-worthy trains ahead
in the webhook event's own payload shape. It is the notification pass's selection reused by
extraction, not a second matcher: `varsel.match_journey` finds the situations, `varsel.
alert_worthy_trains` is the per-train collapse followed by the push gate in the pass's own order,
and every entry's `payload` is the dict `varsel._webhook_payload` builds for a webhook event, worded
by `varsel.notification_wording_for` -- so a favourite and a webhook subscription about the same
train read the same facts and the same Norwegian.

Its own router (KTD1), with NO router-level dependency: `notification_api.router` carries
`_authenticated` and `_throttled` at router level, which is exactly why a public read cannot live on
it. It shares that router's prefix so the contract stays one API, and it is deliberately NOT behind
`sso.PROTECTED_PREFIXES` (a machine caller redirected into a Google login screen gets HTML where it
expected JSON; `test_sso` pins the tuple exactly).

Module-level `import varsel` -- the `main._service_verdict` precedent for a module that reaches
another's underscored helpers -- and therefore deliberately OFF the import-forbidden tuple in
`test_notification_contract`: that tuple keeps an EVENT from reaching the VAPID keypair and the
device registry, and a read delivers nothing.

What this unit does not do, by design (KTD13): no servability check, no decision row, no Firestore.
And it never WRITES anything the poller owns (R10): not `varsel._messages`, not `_alternatives`, not
`_notified` -- the one accessor it reads the poller's caches through is
`varsel.cached_wording_and_alternative`, which is read-only by contract.

**Enrichment (U4) is fetched on request, under the read's own allowances, behind `wait`** (R4, R5).
The figures -- which trains, which numbers -- are the cheap half and are served at once; the model's
wording and the alternative journey are the expensive half, and each read submits ONE background
task per definition per Oslo day that runs the matcher with Journey Planner lookups on the pull fetch
path and words the soonest `MAX_WORDINGS_PER_READ` trains through `notification_wording.PULL`. The
request waits for at most `wait` seconds and then assembles whatever is ready: an entry that is not
is served with the template and no alternative, its two statuses say why, and `complete` -- every
entry's two statuses terminal -- decides the caching header (R9). The task keeps running after the
request returns, so the next read finds its answer. Three caches carry it, and the answer is
assembled PER REQUEST from them, never cached whole (KTD3): the figures per definition per cycle,
the wording per train for the situation's life (`WORDING_TTL_S`), the alternative outcome per train
for the Journey Planner cache's life (`alternatives._TTL`). A transient outcome is a short marker
(`FAILURE_TTL_S`) that is resubmitted on expiry; a terminal one is not.

A REGISTERED definition -- one a browser or a webhook subscription holds, read by its five fields
verbatim -- serves the poller's own wording for a train the pass already worded, byte for byte the
event's (KTD6, R3), and spends no pull wording on it. The entry says whether its text is the model's
or the template, never whether the definition is registered.

Bounds this module owns, each a number so it can be checked, and each published in
`docs/operations/notification-webhooks.md`'s constants table:

* **Its own limiter** (KTD11, R7): a global per-minute ceiling and a per-STOP-PAIR ceiling in a
  bounded map, independent of the credentialed tiers. Per pair rather than per definition, because
  a definition is free to mint -- three hundred periods on one pair are one bucket.
* **A figures cache per definition per poller cycle** (KTD3, R8): keyed on `(journey_key,
  refreshed_at, Oslo day)` with single-flight per key, so a warm read within a cycle runs no matcher.
* **A cold cache is a 503, never an empty list** (KTD8, R6): a cache the poller has not loaded has no
  situations in it, and "no disruptions" off it would report an empty process as a calm day.
* **The enrichment pool** (KTD5): `WORKERS` threads, a queue of `QUEUE_MAX` bounded BY THIS MODULE
  (a `ThreadPoolExecutor` queues without limit), `WORDING_WORKERS` wide inside a task, the wording
  cap per read, the waiter ceiling, and the two cache lifetimes.

R11 binds every log record here: no stop ref, no name, no departure time, no prose, and nothing
derived from the definition beyond `alternatives.pair_digest`, the salted per-process digest, under
the field name that module already logs it as. The process's HTTP access log redacts this path's
query string too (`logging_setup._AccessLogRedaction`), because the refs ride in it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from zoneinfo import ZoneInfo

import alert_policy
import alternatives
import decisions
import impact as impact_engine
import journey_status
import notification_api
import notification_auth
import notification_events
import notification_wording
import varsel
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

#: Reads per minute across every caller. Raised 300 -> 3 000 by U15 (2026-09-13), which is R6's
#: 50 per second stated per minute. It was `notification_auth.GLOBAL_PRELOOKUP_PER_MIN`'s number
#: and is deliberately no longer tied to it: that one bounds a Firestore lookup and this one bounds
#: INTERPRETER TIME, which is the binding resource here and is shared with the poller (KTD17).
#:
#: **What this ceiling is worth depends on the hit rate, and both halves are published** in the
#: operations document's capacity table. A HIT is assembly only. A MISS is one matcher run on the
#: request pool -- pruned per place pair since U2, with no Journey Planner lookup on this phase.
#:
#: **Measured 2026-09-13** (`bench_matcher.py --pull-read`, 20 000 definitions): a miss **0.32 ms**,
#: a hit **0.017 ms**, and ~**3 000 misses per second** sustainable beside the pass. So 50/s fits
#: with about sixty times the headroom even if EVERY read misses -- and it is U2's index that did
#: that, not warming: the same miss measured 15 ms p50 in dev on 2026-09-10, which is where the old
#: 300/min came from. Warming (:func:`warm_figures`) turns a REGISTERED definition's reads into
#: hits for the whole cycle; a favourite nobody registered is still a miss on the first read of
#: each cycle, however high this number is.
#:
#: What is still owed is the same question asked of a real process -- 50 rps against dev during
#: pass 1a -- because the figures above are one process over a synthetic cache and say nothing
#: about anyio or the event loop. Until then read this as a bound on ABUSE: raising it past what
#: the measured per-miss time supports would not buy throughput, it would buy a queue in front of
#: one GIL.
MAX_PER_MIN = 3_000
#: Reads per minute per canonical stop pair. What a favourites panel polling every minute on a phone
#: needs is one; thirty is room for a client's whole fleet behind one NAT without being room for
#: a burst that minted three hundred definitions on one corridor.
PER_PAIR_PER_MIN = 30
#: How many pair buckets the limiter remembers before dropping the insertion-oldest
#: (`feedback._claim`'s shape). A pair bucket is a few floats; ten thousand is ~a day of distinct
#: pairs at the global ceiling.
LIMITER_KEYS_MAX = 10_000
#: The longest a caller may ask the read to wait for enrichment. It bounds the REQUEST and never the
#: task (KTD5): a waiter that times out leaves the task running for the next read. Also the bound on
#: a single-flight waiter below, so this module adds no second clock of its own.
MAX_WAIT_S = 20
#: Entries served when the caller names no `limit`, and the most it may ask for. A panel shows a
#: handful; the cap is what stops a caller turning one read into the whole cycle's worth of trains.
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
#: Figures cached per poller cycle. One entry per definition READ this cycle plus one per
#: definition the pass WARMED (:func:`warm_figures`), which is what raised it 512 -> 20 000 in U15:
#: at 512 the warm pass would evict its own oldest entries before a single read arrived, and the
#: registered definitions that survived would be whichever the pass happened to match last.
#:
#: The bound is memory, not correctness -- eviction costs a matcher run, never a wrong answer. An
#: entry is one `_Figures` per definition, and almost every one of them is EMPTY: an ordinary
#: evening cycle has 0-1 alert-worthy trains across ~85 matched definitions, so the cost is the
#: dict slot. What is not free is the bad-morning shape, where ~13 % of definitions are
#: alert-worthy. **Measured 2026-09-13** (`bench_matcher.py --pull-read`, which reports it): a
#: warmed cache of 19 300 alert-worthy entries retains **373 MB**, about **19 KB per entry**,
#: against the pod's 2 048 MiB and the 350-435 MiB prd runs at. That synthetic shape is ~1 entry
#: per definition, far above anything measured -- a bad morning at 100 000 definitions is ~13 000
#: entries, ~250 MB -- so the bound holds, but **the number to watch is ENTRIES, not definitions**.
#:
#: The lever is this bound and NOT the entry's shape. Most of those 19 KB is `winner.impact`, which
#: `_message_input` reads, so it cannot be stripped; and `contributing` is not the cost it looks
#: like -- measured over 540 entries, 539 of them have a `contributing` that IS the winner object,
#: because one situation per train is the ordinary case.
#:
#: Past this many registered definitions the warm pass evicts insertion-oldest and the read falls
#: back to computing, which is the documented degradation rather than a failure. `_listed_trains`
#: is bounded by this same number, so past it the third forget rule also stops firing for whichever
#: definitions were evicted -- their stale wordings then age out on `WORDING_TTL_S` instead of going
#: when the train leaves the list. Both are one signal: this bound is under the population.
FIGURES_CACHE_MAX = 20_000
#: Trains worded per read, spent soonest-first (KTD7). Four is a panel's worth and one Vertex call
#: each; the rest carry the template under `not_worded`. That verdict is POSITIONAL -- derived at
#: read time from the entry's place in the definition's own soonest-first order, never written to
#: the wording cache (2026-09-10): persisted under KTD3's key, which carries no definition term, a
#: wide definition's fifth train made every narrower definition on the pair whose FIRST train it
#: was read a terminal record, submit no task and serve the template as a complete, publicly
#: cacheable answer for `WORDING_TTL_S`. So it is re-evaluated on every read, and the same train
#: is worded once the trains ahead of it have departed or a narrower read puts it under the cap.
MAX_WORDINGS_PER_READ = 4
#: The enrichment pool's width (KTD5). `/meldinger`'s number, the repo's narrowest, because this is
#: reachable from a public unauthenticated endpoint on the process that also runs the poller.
WORKERS = 4
#: Tasks that may wait for a worker. Bounded HERE, because `ThreadPoolExecutor` queues without
#: limit: past `WORKERS + QUEUE_MAX` a new definition's read is answered `capped` at once, with its
#: figures, rather than queued behind a burst it would never see the end of.
QUEUE_MAX = 16
#: Wordings written concurrently inside one task, each a ~2 s Vertex call.
WORDING_WORKERS = 4
#: Requests that may be parked on a task's Future at once. Past it a read behaves as `wait=0`
#: (200, never 503): the answer is the figures and the statuses, and the task still runs.
MAX_CONCURRENT_WAITERS = 64
#: Wording records kept, and the bound on the alternative-outcome map beside it. An entry is a
#: short Norwegian text and a status; `/meldinger` sizes its own wording cache the same.
WORDING_CACHE_MAX = 10_000
#: The OUTER bound on a wording's life: the situation's own (12 h from onset), the same lifetime
#: `varsel._messages` has. The figures refresh forgets it earlier when its train drops off the list.
#:
#: Since 2026-09-22 it is rarely what retires an entry. `notification_wording.wording_key` carries
#: the prediction snapshot the alert was scored against, so an entry is reachable only for the cycle
#: that wrote it -- what this bounds is how long an unreachable one occupies a slot before
#: `WORDING_CACHE_MAX`'s LRU or the third forget rule takes it. Left at 12 h because the cache is
#: bounded by COUNT and the orphans are short strings; lowering it would evict nothing that matters
#: sooner and would need its own measurement.
WORDING_TTL_S = 43_200
#: How long a transient outcome -- `error`, `rate_limited`, `unanswered`, a queue-full `capped` --
#: stands before the next read resubmits the task. Sixty seconds is `/meldinger`'s number for the
#: same class of failure: long enough that a burst of reads during an outage does not re-spend the
#: allowance on the same first trains every request, short enough that a red status does not stick
#: after Vertex or the Journey Planner recovers.
FAILURE_TTL_S = 60

#: The operator codes the read can say anything about: the bus operators with a tuned threshold in
#: `alert_policy._OPERATOR_ALERT_THRESHOLD`, plus `VYG` -- the rail family, which runs on the
#: DEFAULT threshold and is therefore deliberately absent from that table. Served in `coverage` so a
#: client can tell "no alert" from "not an operator we score".
SERVED_OPERATORS: tuple[str, ...] = tuple(sorted({*alert_policy._OPERATOR_ALERT_THRESHOLD, "VYG"}))

#: `NSR:StopPlace:<digits>` -- the shape `notification_api.SubscriptionIn` publishes for a stop ref,
#: tested BEFORE canonicalization (KTD2): a collapsed ref is still an NSR ref, and an alias map
#: must not be able to launder an arbitrary string into one.
_STOP_REF_RE = re.compile(r"^NSR:StopPlace:\d+$")

_ROUTE = "/journey-alerts"
_STATUS_ROUTE = "/journey-status"
_CACHE_CONTROL_COMPLETE = "public, max-age=60"
_CACHE_CONTROL_INCOMPLETE = "no-store"

# ---- The status vocabularies (R5) ---------------------------------------------------------------
# `wording.status`: `ok` (source `llm`), `rejected`/`ungrounded` (the model answered and the answer
# was unusable for THIS input -- payload-determined, so terminal for the wording's life, source
# `deterministic`), `disabled` (no backend may be asked, or the channel does not deliver prose --
# terminal), `not_worded` (past the per-read cap in THIS read's soonest-first order -- terminal for
# this read, so `complete` may be true, but never cached: it is derived from the entry's position by
# `_entry_state` on every read and re-evaluated as the list moves, see `MAX_WORDINGS_PER_READ`);
# `pending` (a task is running), `capped` (the queue refused the task), `rate_limited` (the PULL
# allowance) and `error` (a transient backend failure) expire after `FAILURE_TTL_S` and are retried.
_TERMINAL_WORDING = frozenset({"ok", "rejected", "ungrounded", "disabled", "not_worded"})
# `alternative.status`: `decisions._alternative_status`'s four (`offered`, `suppressed`,
# `none_found`, `not_worth_lookup`) plus `not_looked_up` (past the matcher's lookup cap) are
# terminal; `pending`, `capped` and `unanswered` (the Journey Planner did not reply -- throttled, an
# outage, the cooldown) are transient.
_TERMINAL_ALTERNATIVE = frozenset({"offered", "suppressed", "none_found", "not_worth_lookup", "not_looked_up"})
_PENDING = "pending"
_CAPPED = "capped"
_NOT_WORDED = "not_worded"

_refreshed_at_provider: Callable[[], datetime | None] | None = None


def configure(*, refreshed_at: Callable[[], datetime | None]) -> None:
    """Wire in the freshness provider (`main._varsel_refreshed_at`), once, at startup.

    The only injection this module takes. The stop cache the `coverage` block reads comes off the
    provider `varsel.configure` already holds (:func:`_stop_rows`), rather than through a second
    parameter here: the two must describe the same picker rows, and one seam cannot drift from
    itself.
    """
    global _refreshed_at_provider
    _refreshed_at_provider = refreshed_at


def _wall_clock() -> datetime:
    return datetime.now(UTC)


#: The clock every step of one request reads ONCE -- windows, the matcher's `catchable_after`, the
#: message input's «i morgen», the assembly cut, the Oslo-day cache key and `as_of` -- so no two of
#: them can straddle an instant. Rebound by the test suite to freeze it.
_now: Callable[[], datetime] = _wall_clock


def _utc(value: datetime) -> datetime:
    """Every instant the answer carries is a UTC instant, whatever zone its source was built in --
    `windows` already are (`varsel.journey_windows` converts), and a mixed answer is a client bug
    waiting to happen."""
    return value.astimezone(UTC)


def _stop_rows() -> list[dict]:
    """The poller's stop cache, through the provider `varsel` holds. Empty when unwired."""
    provider = varsel._stops_provider
    return provider() if provider is not None else []


# ---- Response models ---------------------------------------------------------------------------
# `extra="forbid"` on every one, as on `notification_api`'s: on the response side it is the drift
# guard against the published contract (U5), not input validation.


class JourneyOut(BaseModel):
    """The canonical definition the answer is about -- refs after canonicalization, defaults filled."""

    model_config = ConfigDict(extra="forbid")
    from_stop: str
    to_stop: str
    begin: str
    end: str
    days: list[int]


class WindowOut(BaseModel):
    """One boarding window asked about, as UTC instants."""

    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime


class CoverageOut(BaseModel):
    """What the read can and cannot say anything about.

    `operators` is :data:`SERVED_OPERATORS`. The two timetable flags say whether each stop appears
    in TODAY's planned timetable as the poller's stop cache knows it -- checked over every ref of
    the place (`varsel.place_refs`), so a collapsed ref is not read as absent -- and fail OPEN: an
    empty cache reads True for both, because "we do not know" must not be reported as "not served".
    """

    model_config = ConfigDict(extra="forbid")
    operators: list[str]
    from_stop_in_timetable_today: bool
    to_stop_in_timetable_today: bool


class TrainOut(BaseModel):
    """The train's identity, in `notification_store.TrainRef`'s shape (the webhook's own)."""

    model_config = ConfigDict(extra="forbid")
    operator: str
    line: str
    journey_ref: str
    call_date: str
    stop_place: str
    stop_order: int


class WordingOut(BaseModel):
    """Where `payload.notification`'s text came from: `llm` or `deterministic`, and why."""

    model_config = ConfigDict(extra="forbid")
    source: str
    status: str


class AlternativeOut(BaseModel):
    """What became of the alternative-journey lookup behind `payload.alternative_journey`."""

    model_config = ConfigDict(extra="forbid")
    status: str


class JourneyAlertOut(BaseModel):
    """One alert-worthy train ahead."""

    model_config = ConfigDict(extra="forbid")
    severity: int = Field(
        description="1-based rank among this answer's `n_alerts` trains in worst-first order "
        "(`varsel._alert_rank`: severity, then situation number), so a client can re-sort. A "
        "truncated answer lists the soonest trains and may therefore lack some ranks."
    )
    phase: str = Field(
        description="`confirmed` when ET has declared the train cancelled, or when a Vy train that "
        "runs on this weekday is missing from today's timetable (`KIX:PlanRemoved:`), else "
        "`predicted`."
    )
    train: TrainOut
    aimed_departure_time: datetime
    expected_departure_time: datetime = Field(
        description="Aimed departure plus the typical delay predicted for it -- the figure the "
        "matcher keeps a late train on. Equal to the aimed time for a declared cancellation."
    )
    situations: list[str] = Field(
        description="The contributing situation numbers -- `payload.situations[*].situation_number`, "
        "repeated here so a client can join without opening the payload."
    )
    payload: dict = Field(description="Exactly the webhook event's payload for this train.")
    wording: WordingOut
    alternative: AlternativeOut


class JourneyAlertsResponse(BaseModel):
    """The answer: the trains a notification would go out for on this definition, right now."""

    model_config = ConfigDict(extra="forbid")
    as_of: datetime
    refreshed_at: datetime = Field(description="When the prediction cache the figures come from was loaded.")
    complete: bool = Field(
        description="Whether every listed entry's wording and alternative reached a terminal status. "
        "False while any is pending or transiently refused; the answer is then `no-store`."
    )
    n_alerts: int = Field(description="Alert-worthy trains ahead, before `limit` was applied.")
    truncated: bool
    journey: JourneyOut
    windows: list[WindowOut]
    coverage: CoverageOut
    alerts: list[JourneyAlertOut]


class JourneyStatusResponse(journey_status.JourneyStatus):
    """Every train in the definition's window today, graded (plan 2026-09-22-001) -- the body
    `/varsel`'s «I dag» strip renders, plus the echoed definition and the coverage block.
    `coverage.operators` is `journey_status.OPERATORS`, not this read's `SERVED_OPERATORS`: the
    timetable behind the strip is Vy rail only."""

    model_config = ConfigDict(extra="forbid")
    journey: JourneyOut
    coverage: CoverageOut


# ---- Validation --------------------------------------------------------------------------------


def _definition(
    from_stop: str, to_stop: str, begin: str | None, end: str | None, days: list[int] | None, now: datetime
) -> dict:
    """The five-key definition dict (KTD2), or a 422.

    The rules are `/varsel`'s own, and they are REUSED rather than restated: after the shape test
    (this module's, and before canonicalization), the dict goes through
    `varsel._parse_webhook_recipient`, which canonicalizes both refs through `varsel.canonical_stop`,
    refuses equal refs AFTER that, checks HH:MM and `begin < end`, and bounds `days` -- with the
    messages `notification_api` already pins word for word against `JourneyIn`'s
    (`test_the_window_and_weekday_verdicts_are_varsels_own_word_for_word`). A second copy of four
    rules here is what would drift. The recipient id it needs is a fixed label; only `.journey` is
    read.

    No 422 message echoes the caller's value: the response is public and the refusal is logged
    nowhere with it, so an unparseable ref stays unrecorded end to end (R11).
    """
    for ref in (from_stop, to_stop):
        if not _STOP_REF_RE.match(ref):
            raise HTTPException(status_code=422, detail="from_stop and to_stop must be NSR:StopPlace:<digits>")
    if (begin is None) != (end is None):
        raise HTTPException(status_code=422, detail="begin and end must be given together")
    local_now = now.astimezone(_OSLO)
    if days is None:
        days = sorted({local_now.weekday(), (local_now.weekday() + 1) % 7})
    try:
        recipient = varsel._parse_webhook_recipient(
            {
                "recipient_id": "pull",
                "from_stop": from_stop,
                "to_stop": to_stop,
                "begin": begin if begin is not None else "00:00",
                "end": end if end is not None else "23:59",
                "days": days,
            }
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return recipient.journey


# ---- The limiter (KTD11) -----------------------------------------------------------------------

_limiter_lock = threading.Lock()
_global_bucket: deque[float] = deque()
_pair_buckets: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()


def _claim(pair: tuple[str, str], now_s: float) -> bool:
    """Spend one global slot and one slot on the pair's bucket, or refuse.

    Global FIRST, and a per-pair refusal leaves the global slot SPENT. The global ceiling is about
    the process's total work, and a refused read still did its parse and took this lock; un-claiming
    it would also make a client hammering one pair invisible in the global count, which is the count
    an operator sizes the ceiling from. The cost of that choice is bounded and stated: a pair-bound
    client can burn at most its own thirty of the three hundred per minute.
    """
    with _limiter_lock:
        if not notification_auth.claim_window(_global_bucket, MAX_PER_MIN, now_s):
            return False
        bucket = _pair_buckets.get(pair)
        if bucket is None:
            bucket = _pair_buckets[pair] = deque()
            while len(_pair_buckets) > LIMITER_KEYS_MAX:
                _pair_buckets.popitem(last=False)
        return notification_auth.claim_window(bucket, PER_PAIR_PER_MIN, now_s)


# ---- The figures cache (KTD3, phase 1) ---------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    """One alert-worthy train's figures, built once per definition per cycle.

    Everything a request needs to assemble its answer without touching the matcher: the winner, the
    UNCOLLAPSED alerts about the same train (the payload's contributing situations -- the wording is
    the winner's, the list is what a client joins on), the train's two identities (the webhook's
    `TrainRef` shape for the answer, `varsel._train_key`'s tuple for the per-train caches), and the
    expected departure the per-request cut re-applies -- `catchable_after` is match time, and a
    cached figure can outlive a departure by a cycle. No payload: it is rebuilt per request from the
    winner plus whatever wording and alternative the caches hold by then (KTD3).
    """

    winner: varsel.Alert
    contributing: tuple[varsel.Alert, ...]
    train: TrainOut
    train_key: tuple
    expected_departure: datetime
    situations: tuple[str, ...]


@dataclass(frozen=True)
class _Figures:
    entries: tuple[_Entry, ...]


_figures_lock = threading.Lock()
_figures: OrderedDict[tuple, _Figures] = OrderedDict()
_figures_inflight: dict[tuple, threading.Event] = {}
#: The `refreshed_at` every cached entry belongs to. When the poller's load moves, every entry of
#: the older generation is dropped at once rather than aged out: a figure from the previous cycle is
#: exactly what a cycle-keyed cache exists not to serve.
_figures_generation: datetime | None = None


def _cache_key(journey_key: str, refreshed_at: datetime, now: datetime) -> tuple:
    return (journey_key, refreshed_at, now.astimezone(_OSLO).date().isoformat())


def _enter_generation(refreshed_at: datetime) -> bool:
    """Make `refreshed_at` the live generation if it is newer, and answer whether it IS the live
    one. Caller holds :data:`_figures_lock`.

    Extracted from :func:`_cached_figures` in U15, for a reason that is silent if it is missed:
    :func:`_remember_figures` refuses to store under a generation marker that does not match, and
    the marker was only ever SET here, by a read. The warm pass runs BEFORE any read of a fresh
    process, so without this being reachable from the warm path every warmed figure would be
    discarded on the way in and every read would still miss -- a warm pass that spends the pass's
    time and buys nothing, with no error anywhere.

    False means an OLDER load: a caller that read `refreshed_at` just before the poller moved. It
    misses, and `_remember_figures` refuses its result; it must not wipe the newer generation's
    entries and regress the marker, which would cost one extra matcher run per such straddle.
    """
    global _figures_generation
    if _figures_generation is None or refreshed_at > _figures_generation:
        # A NEWER load: every entry of the older generation goes at once (KTD3).
        _figures.clear()
        _figures_generation = refreshed_at
        return True
    return refreshed_at == _figures_generation


def _cached_figures(key: tuple, refreshed_at: datetime) -> _Figures | None:
    with _figures_lock:
        if not _enter_generation(refreshed_at):
            return None
        hit = _figures.get(key)
        if hit is not None:
            _figures.move_to_end(key)
        return hit


def _remember_figures(key: tuple, refreshed_at: datetime, figures: _Figures) -> None:
    with _figures_lock:
        if _figures_generation != refreshed_at:
            return  # the poller moved while we computed; a stale generation is not worth keeping
        _figures[key] = figures
        _figures.move_to_end(key)
        while len(_figures) > FIGURES_CACHE_MAX:
            _figures.popitem(last=False)


def _release(lock: threading.Lock, inflight: dict[tuple, threading.Event], key: tuple, event: threading.Event) -> None:
    """Give up ownership of a single-flight key: drop it from the in-flight map and wake the waiters.
    One helper for the figures and the wordings, because the shape is the same and the order is
    the part that matters -- the map entry goes before the event fires, or a waiter could re-find
    the owner's own event and park on it again."""
    with lock:
        inflight.pop(key, None)
    event.set()


def _figures_for(definition: dict, key: tuple, refreshed_at: datetime, now: datetime) -> _Figures:
    """A miss: run the matcher once per key, with every other caller for the key waiting on it.

    Blocking (the matcher runs here), so it is called through `run_in_threadpool`. Single-flight per
    key in `meldinger._wording_inflight`'s shape: the owner computes and publishes, a waiter blocks
    on the owner's event for at most :data:`MAX_WAIT_S` and then re-reads the cache, computing itself
    only if the owner failed or the wait elapsed -- a second matcher run is a cost, never a wrong
    answer. The event is set in `finally` so an owner that raised releases its waiters, who then
    raise on their own rather than block for the full wait.
    """
    with _figures_lock:
        event = _figures_inflight.get(key)
        owner = event is None
        if owner:
            event = threading.Event()
            _figures_inflight[key] = event
    assert event is not None
    if not owner:
        event.wait(MAX_WAIT_S)
        hit = _cached_figures(key, refreshed_at)
        if hit is not None:
            return hit
        return _compute_figures(definition, now, journey_key=key[0])
    try:
        figures = _compute_figures(definition, now, journey_key=key[0])
        _remember_figures(key, refreshed_at, figures)
        return figures
    finally:
        _release(_figures_lock, _figures_inflight, key, event)


def _compute_figures(definition: dict, now: datetime, *, journey_key: str) -> _Figures:
    """The matcher and the selection -- what a request assembles from.

    Selection is `varsel.alert_worthy_trains` over `varsel.match_journey`'s alerts: the per-train
    collapse first, then the push gate, in the pass's own order (KTD7). A train `notification_events.
    train_ref_of` cannot name stably is skipped exactly as `varsel._observations_for_key` skips it, and
    is not counted anywhere: `n_alerts` is what the answer lists. The per-train build -- message
    input, wording, payload -- moved to assembly (:func:`_assembled_entry`) with U4, because the
    wording and the alternative it is built from arrive after the figures do.

    Ends with the poller's third forget rule (KTD3): a train this definition listed last time and
    does not list now has dipped under the gate (or departed), and its wording and alternative
    outcome go with it, so a train that re-crosses the gate is re-worded rather than served the
    text written for its earlier reading.
    """
    return _figures_from_alerts(definition, varsel.match_journey(definition, now), now, journey_key=journey_key)


def _figures_from_alerts(
    definition: dict, alerts: list[varsel.Alert], now: datetime, *, journey_key: str
) -> _Figures:
    """The selection and the per-train build, over alerts SOMEBODY ELSE ran the matcher for.

    One builder for the read's miss and for the pass's warm (:func:`warm_figures`), because two
    would be two definitions of what an entry is -- and the first field one of them gained would
    make a warmed definition's answer differ from the same definition's answer on a cold cycle,
    which no test that stubs the matcher can see.

    The pass's alerts are `with_alternative=True` and the read's are not. That difference is
    deliberately NOT normalised here: an entry carries the winner, and every reader of an entry
    goes through :func:`_enriched`, which decides the alternative from the caches and silences the
    one fallback that could otherwise read the pass's (see its docstring).
    """
    contributing: dict[tuple, list[varsel.Alert]] = {}
    for alert in alerts:
        train = varsel._train_key(alert)
        if train is not None:
            contributing.setdefault(train, []).append(alert)
    entries: list[_Entry] = []
    for winner in varsel.alert_worthy_trains(alerts):
        ref = notification_events.train_ref_of(
            operator=winner.operator,
            line=winner.line,
            journey_ref=winner.jkey,
            stop_place=winner.stop_place,
            stop_order=winner.stop_order,
            departure_time=winner.departure_time,
        )
        expected = varsel._expected_departure_of(winner)
        train_key = varsel._train_key(winner)
        if ref is None or expected is None or train_key is None:
            continue
        about = tuple(contributing.get(train_key, [winner]))
        entries.append(
            _Entry(
                winner=winner,
                contributing=about,
                train=TrainOut(
                    operator=ref.operator,
                    line=ref.line,
                    journey_ref=ref.journey_ref,
                    call_date=ref.call_date,
                    stop_place=ref.stop_place,
                    stop_order=ref.stop_order,
                ),
                train_key=train_key,
                expected_departure=expected,
                situations=tuple(s["situation_number"] for s in varsel._webhook_situations(list(about))),
            )
        )
    _forget_unlisted(journey_key, (definition["from_stop"], definition["to_stop"]), entries)
    return _Figures(entries=tuple(entries))


def warm_figures(journey_key: str, definition: dict, alerts: list[varsel.Alert], now: datetime) -> None:
    """Publish the pass's own match as this cycle's figures for one registered definition (U15).

    Wired by `main` into `varsel.configure(figures_warmer=...)` and called from pass 1a, which is
    the only place the alerts already exist. It is an INJECTED callable rather than an import
    because `varsel` may not depend on this module -- a plan stop condition, pinned both ways by
    `test_varsel_api.test_varsel_does_not_import_the_pull_read`.

    What it buys is bounded and worth stating plainly: a favourite that IS a registered definition
    is a figures hit for the whole cycle, for every caller, at no matcher cost. A favourite nobody
    registered is untouched by this -- the app's favourites are mostly not registrations -- so the
    warm pass raises the hit rate rather than removing the miss, and `MAX_PER_MIN` is sized against
    the miss that remains.

    Three properties it must keep. It reads `_refreshed_at_provider` ITSELF rather than taking the
    generation from the pass: `main._rebuild_all` swaps the prediction cache and calls
    `check_and_notify` on the one poller thread, so the value is stable across the pass, and taking
    it here keeps the seam's signature free of this module's vocabulary. It SKIPS a key already
    cached this generation, because a read that beat the pass to it already computed the same
    figures and already applied the forget rule, and a second application is not free. And it
    raises nothing that matters: the caller wraps it, but the cheaper guarantee is that everything
    here is either a dict operation or the same build a read does.
    """
    provider = _refreshed_at_provider
    refreshed_at = provider() if provider is not None else None
    if refreshed_at is None:
        return
    key = _cache_key(journey_key, refreshed_at, now)
    if _cached_figures(key, refreshed_at) is not None:
        return
    _remember_figures(key, refreshed_at, _figures_from_alerts(definition, alerts, now, journey_key=journey_key))


def _message_input(definition: dict, alert: varsel.Alert, now: datetime) -> dict:
    """`varsel._message_input`'s build with the read's clock, and its fallback on failure.

    The clock matters: `i_morgen` -- and so `payload.call.departs_tomorrow` -- is decided against
    `now`, which is the same instant the windows were built from. The fallback is `varsel`'s own
    (`_message_input_from_alert`), which carries no `reise`, so the route line then names the refs.

    Reads the alternative OFF THE ALERT (`alert.alternative_journey`), which is why every caller
    hands it an enriched copy (:func:`_enriched`) rather than the cached winner -- the 2026-08-05
    rule: the response copy the alert carries may say one thing and the field the wording is built
    from another, and the Alert is the field.
    """
    try:
        import llm_message

        return llm_message.build_message_input(definition, alert.impact, alert, now=now)
    except Exception:
        logger.debug("Could not build a message input for a pull read; wording from the alert", exc_info=True)
        return varsel._message_input_from_alert(alert)


#: `llm_message.PROMPT_VERSION`, through the leaf module both surfaces key on (KTD9). Kept as a
#: module-level name because the log record and the cache key both read it here.
_prompt_version = notification_wording.prompt_version


# ---- The enrichment caches (KTD3, phase 2) -----------------------------------------------------


@dataclass(frozen=True)
class _WordingRecord:
    """One train's wording outcome: the model's text (the wording alone, never the route line --
    `notification_wording_for` puts that around it at assembly, as it does for a push) or None for
    the template, its status, and the train it is about, which the figures-refresh eviction needs
    because the cache key (KTD3) carries no train term."""

    text: str | None
    status: str
    train_key: tuple


@dataclass(frozen=True)
class _AlternativeRecord:
    """One train's alternative outcome: the status in the vocabulary above and, for `offered`, the
    journey itself -- raw, with `varsel._still_catchable` applied by the reader against ITS clock."""

    status: str
    journey: alternatives.AlternativeJourney | None


_enrichment_lock = threading.Lock()
#: wording key -> (expires at, record). The key is KTD3's: `(pair, situation_number, departure,
#: cancelled, i_morgen, alternative identity, PROMPT_VERSION)`, so a wording written while a train
#: was merely at risk is never served once ET has declared it cancelled, one written at 23:00 for
#: «i morgen» is never served for the same train read after midnight, and a changed alternative --
#: or a prompt edit -- re-words rather than reuses.
_wordings: OrderedDict[tuple, tuple[float, _WordingRecord]] = OrderedDict()
#: (pair, train key) -> (expires at, record).
_alternative_outcomes: OrderedDict[tuple, tuple[float, _AlternativeRecord]] = OrderedDict()
#: The per-train single-flight map for wordings, in `meldinger._wording_inflight`'s shape: two
#: definitions sharing a train (a morning and an evening period on one pair, a wide period and a
#: narrow one) word it once between them.
_wording_inflight: dict[tuple, threading.Event] = {}
#: journey_key -> the train keys its figures listed at the last compute, for the third forget rule.
_listed_trains: OrderedDict[str, frozenset] = OrderedDict()
#: (pair, train key) -> the `_wordings` keys written for that train. The third forget rule pops by
#: TRAIN, and `_wordings` is keyed by KTD9's wording key, which does not begin with one -- so
#: without this the rule is a scan of the whole wording cache. That was affordable while it ran on
#: a read MISS, a handful a minute; U15 runs it once per registered definition per cycle, and at
#: `WORDING_CACHE_MAX` entries a scan is ~1 ms, so a few thousand of them are seconds of the one
#: interpreter the pass needs (KTD17).
#:
#: DERIVED and self-healing, deliberately not a mirror of the cache: an entry left behind by LRU
#: eviction or by expiry is harmless, because the rule pops keys OUT of `_wordings` and popping an
#: absent key is a no-op. That is what keeps maintenance to two sites -- written beside the
#: wording, dropped with the train -- rather than the four a mirror would need, each of them a
#: silent leak if missed. It is bounded by REBUILDING from `_wordings` rather than by evicting:
#: evicting the index alone would retire the forget rule for whichever train went, which is a wrong
#: answer, while a rebuild is one pass over a bounded cache. It fires at MOST once per write and in
#: practice far less -- the trigger is the index outgrowing the cache, which needs stale entries to
#: have accumulated, and `notification_wording.PULL`'s allowance bounds writes to 30 a minute.
_wording_keys_by_train: dict[tuple, set[tuple]] = {}

# ---- The enrichment pool (KTD5) ----------------------------------------------------------------

_tasks_lock = threading.Lock()
_executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="pull-enrich")
#: (journey_key, Oslo day) -> the task's Future. A FINISHED future stays until the next submission
#: for its key replaces it, so a caller (and a test) can join a task that completed before it
#: looked; only unfinished ones count toward the queue bound.
_inflight: dict[tuple, Future] = {}
#: Advanced by every `reset_state`. A task captures it at submit and every cache write it makes is
#: refused unless it still matches, so a task from one test cannot write into the next test's cache
#: -- and a task from before a reset cannot write into the caches the reset emptied.
_epoch = 0
_waiters = 0


def _inflight_key(journey_key: str, now: datetime) -> tuple:
    return (journey_key, now.astimezone(_OSLO).date().isoformat())


def _alternative_key(pair: tuple[str, str], train_key: tuple) -> tuple:
    return (pair, train_key)


def _wording_key(pair: tuple[str, str], winner: varsel.Alert, alt, message_input: dict) -> tuple:
    """KTD9's key, built by `notification_wording` so the poller and this read cannot drift apart.

    Kept as a thin local name rather than inlining the call at four sites: this module's caches,
    its eviction sweep and its tests all speak of "the wording key", and the `i_morgen` term is
    read off the input HERE while the poller derives it from its own clock — same answer, two
    callers that hold different things.
    """
    return notification_wording.wording_key(
        pair, winner, alt, tomorrow=bool(message_input.get("i_morgen"))
    )


def _cached(cache: OrderedDict, key: tuple, now_s: float):
    """A live record, or None -- an expired one is dropped on the way out, like `meldinger`'s."""
    with _enrichment_lock:
        hit = cache.get(key)
        if hit is None:
            return None
        if now_s >= hit[0]:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return hit[1]


def _remember(cache: OrderedDict, key: tuple, record, ttl_s: float, epoch: int) -> None:
    """Store under the LRU bound, unless the world has been reset since the writer was submitted."""
    with _enrichment_lock:
        if epoch != _epoch:
            return
        cache.pop(key, None)
        while len(cache) >= WORDING_CACHE_MAX:
            cache.popitem(last=False)
        cache[key] = (monotonic() + ttl_s, record)


def _rebuild_wording_index() -> None:
    """Rebuild :data:`_wording_keys_by_train` from `_wordings`, dropping what eviction and expiry
    left behind. Caller holds :data:`_enrichment_lock`."""
    _wording_keys_by_train.clear()
    for cached_key, (_, cached) in _wordings.items():
        _wording_keys_by_train.setdefault((cached_key[0], cached.train_key), set()).add(cached_key)


def _remember_wording(key: tuple, record: _WordingRecord, epoch: int) -> None:
    ttl = WORDING_TTL_S if record.status in _TERMINAL_WORDING else FAILURE_TTL_S
    _remember(_wordings, key, record, ttl, epoch)
    with _enrichment_lock:
        # After the store and under the same condition it applied: a write refused for a stale
        # epoch, or one the LRU bound evicted on the way in, must not leave the index naming a key
        # the cache does not hold -- harmless for the forget rule, which pops, but it would grow.
        if epoch != _epoch or key not in _wordings:
            return
        _wording_keys_by_train.setdefault((key[0], record.train_key), set()).add(key)
        if len(_wording_keys_by_train) > WORDING_CACHE_MAX:
            _rebuild_wording_index()


def _remember_alternative(key: tuple, record: _AlternativeRecord, epoch: int) -> None:
    ttl = alternatives._TTL.total_seconds() if record.status in _TERMINAL_ALTERNATIVE else FAILURE_TTL_S
    _remember(_alternative_outcomes, key, record, ttl, epoch)


def _cached_alternative(pair: tuple[str, str], train_key: tuple, now: datetime) -> _AlternativeRecord | None:
    """The train's live outcome. An `offered` journey that can no longer be caught is not an
    answer any more -- `_reconcile_caches` drops the poller's the same way -- so it is forgotten
    here and the next read looks again."""
    record = _cached(_alternative_outcomes, _alternative_key(pair, train_key), monotonic())
    if record is not None and record.status == "offered" and not varsel._still_catchable(record.journey, now):
        with _enrichment_lock:
            _alternative_outcomes.pop(_alternative_key(pair, train_key), None)
        return None
    return record


def _forget_unlisted(journey_key: str, pair: tuple[str, str], entries: list[_Entry]) -> None:
    """The third forget rule: a train this definition listed last time and not now loses its
    wording and its alternative outcome, so a re-crossing is re-worded."""
    current = frozenset(e.train_key for e in entries)
    with _enrichment_lock:
        previous = _listed_trains.get(journey_key)
        _listed_trains[journey_key] = current
        _listed_trains.move_to_end(journey_key)
        while len(_listed_trains) > FIGURES_CACHE_MAX:
            _listed_trains.popitem(last=False)
        gone = (previous or frozenset()) - current
        if not gone:
            return
        for train_key in gone:
            _alternative_outcomes.pop(_alternative_key(pair, train_key), None)
            for wording_key in _wording_keys_by_train.pop((pair, train_key), ()):
                _wordings.pop(wording_key, None)


@dataclass(frozen=True)
class _EntryState:
    """What the caches say about one entry right now: the enriched winner, the input the payload and
    the wording are built from, the two statuses, and the wording's key (for the writer)."""

    winner: varsel.Alert
    message_input: dict
    text: str | None
    wording_source: str
    wording_status: str
    alternative_status: str
    wording_key: tuple | None

    @property
    def pending(self) -> bool:
        return self.wording_status == _PENDING or self.alternative_status == _PENDING

    @property
    def terminal(self) -> bool:
        return self.wording_status in _TERMINAL_WORDING and self.alternative_status in _TERMINAL_ALTERNATIVE


def _enriched(winner: varsel.Alert, alt) -> varsel.Alert:
    """The winner with the alternative ON THE ALERT, which is where both the message-input builder
    and `varsel._webhook_payload` read it (2026-08-05) -- and with the RESPONSE copy's own
    alternative cleared, which is the other half of that same rule.

    `llm_message.build_message_input` reads `alert.alternative_journey` **or**, failing that,
    `impact.alternative_journey` -- the worst train's answer, kept as a fallback for callers that
    have only the response. On this path the Alert's field is authoritative by construction: it is
    what the caches said about THIS train, and `None` means no alternative was established for it.

    Left alone the fallback would reach past that. Every alert the poller's pass produces carries a
    response-level alternative (`with_alternative=True`), and since U15 a registered definition's
    figures are WARMED from exactly those alerts -- so a warmed winner would put an
    `alternativ_reise` into the message input that the same definition's answer on a cold cycle
    does not have, moving the wording key and the payload with it, in production and never in a
    suite that stubs the matcher. A no-op for a winner this module computed itself, which is every
    winner before U15, and the copy is only made when there is something to clear.
    """
    impact = winner.impact
    update: dict = {"alternative_journey": alt}
    if impact is not None and getattr(impact, "alternative_journey", None) is not None:
        update["impact"] = impact.model_copy(update={"alternative_journey": None})
    return winner.model_copy(update=update)


def _entry_state(
    definition: dict,
    journey_key: str,
    pair: tuple[str, str],
    entry: _Entry,
    now: datetime,
    *,
    enabled: bool,
    position: int,
) -> _EntryState:
    """Resolve one entry against the poller's caches and then the read's own.

    The poller first (KTD6): for a definition the pass holds, `varsel.cached_wording_and_alternative`
    is the registered wording -- read raw through the webhook channel's gate, refused for a declared
    cancellation, never written -- and when it has text the entry is the poller's entirely: its
    wording (source `llm`), its alternative, and `offered` or `not_looked_up` for the status, because
    the read spent no lookup on that train. `_alternatives` is absent for suppressed, none-found and
    never-looked-up alike, so `not_looked_up` is the honest terminal reading of an absence there,
    and it keeps `complete` computable. The read's own outcome record is deliberately ignored for
    such an entry: a wording and an alternative from two different lookups would be advice from two
    different moments.

    Otherwise the read's own caches: the alternative outcome by train, then -- with the alternative
    on the alert and the message input built from it -- the wording by KTD3's key. A live record
    wins whatever the position. Absent, the verdict is `position` -- the entry's index in the
    definition's alive, soonest-first order (`_alive`), which every caller passes for the same
    list the task spends its cap in: at or past `MAX_WORDINGS_PER_READ` the entry is `not_worded`
    for this read, with its key still set so a later task words it once it moves under the cap;
    under it, `pending`, which is what tells the route a task is owed.
    """
    # Keyed on the PAIR, not the definition (KTD9, 2026-09-12) -- the same shape this module's own
    # caches use, and the whole reason the poller's entry is findable at all: the pass may have
    # worded this train for a NEIGHBOUR's registration of the corridor, with a different window.
    text, poller_alt = varsel.cached_wording_and_alternative(pair, entry.winner, enabled=enabled)
    if text:
        alt = poller_alt if varsel._still_catchable(poller_alt, now) else None
        winner = _enriched(entry.winner, alt)
        return _EntryState(
            winner=winner,
            message_input=_message_input(definition, winner, now),
            text=text,
            wording_source="llm",
            wording_status="ok",
            alternative_status="offered" if alt is not None else "not_looked_up",
            wording_key=None,
        )
    record = _cached_alternative(pair, entry.train_key, now)
    alt = record.journey if record is not None and record.status == "offered" else None
    winner = _enriched(entry.winner, alt)
    message_input = _message_input(definition, winner, now)
    key = _wording_key(pair, winner, alt, message_input)
    wording = _cached(_wordings, key, monotonic())
    if wording is None:
        text, source = None, "deterministic"
        status = _NOT_WORDED if position >= MAX_WORDINGS_PER_READ else _PENDING
    else:
        text, source, status = wording.text, ("llm" if wording.text else "deterministic"), wording.status
    return _EntryState(
        winner=winner,
        message_input=message_input,
        text=text,
        wording_source=source,
        wording_status=status,
        alternative_status=record.status if record is not None else _PENDING,
        wording_key=key,
    )


def _alive(entries: tuple[_Entry, ...], now: datetime) -> list[_Entry]:
    """The entries in the answer's order: the departure cut, then soonest expected departure first
    with `varsel._alert_rank` -- higher is worse -- as the tie-break (two stable sorts, the
    tie-break first). The same order the task spends its caps in (KTD7)."""
    alive = [e for e in entries if not e.expected_departure < now]
    alive.sort(key=lambda e: varsel._alert_rank(e.winner), reverse=True)
    alive.sort(key=lambda e: e.expected_departure)
    return alive


def _webhook_delivery_enabled() -> bool:
    """The webhook channel's wording gate, read ONCE per task or per request, as the pass reads it,
    and failing to the template exactly as `varsel._webhook_delivery_enabled` does."""
    try:
        return bool(notification_wording.webhook_delivery_enabled())
    except Exception:
        logger.warning(
            "Could not read the webhook wording-delivery switch; using the deterministic wording",
            exc_info=True,
            extra={"fields": {"event": "notification_wording", "result": "error", "surface": "pull"}},
        )
        return False


def _alternative_record(alert: varsel.Alert | None, position: int) -> _AlternativeRecord:
    """The task's reading of one train's lookup, in the answer's vocabulary.

    `answered` FIRST: a throttled or failed Journey Planner lookup leaves the same absence a
    completed-and-empty one does, and read through `decisions._alternative_status` alone it would
    be cached as `none_found` -- terminal -- for the whole Journey Planner cache life. Then that
    function's four values, which are the decision log's own. `not_looked_up` is the KTD7 reading
    of `not_worth_lookup` at a position past `impact._MAX_ALTERNATIVE_LOOKUPS` in the soonest-first
    order: an approximation of the matcher's verdict rather than the verdict itself, since the
    matcher's cap is spent per `/impact` call over eligible situations by aimed departure, and both
    readings are terminal and worded without an alternative either way.
    """
    if alert is None:
        # The task's own match did not list the train the figures did. Transient by construction:
        # the two matched at the same instant, so this is the collapse landing differently.
        return _AlternativeRecord("unanswered", None)
    if not bool(getattr(alert.impact, "answered", True)):
        return _AlternativeRecord("unanswered", None)
    status = decisions._alternative_status(alert)
    if status == "not_worth_lookup" and position >= impact_engine._MAX_ALTERNATIVE_LOOKUPS:
        status = "not_looked_up"
    return _AlternativeRecord(status, alert.alternative_journey if status == "offered" else None)


def _wording_outcome(wording: notification_wording.Wording) -> tuple[str | None, str]:
    """`write_wording`'s answer, in this module's vocabulary: the model's text only for source
    `llm`, and its statuses folded to the six the contract names -- `not_delivered` and
    `unavailable` are both `disabled` (no model may be asked here), `rejected`/`ungrounded` stay
    what they are, `rate_limited` and `capped` stay what they are, and anything else the backend
    reported is a transient `error`."""
    if wording.source == "llm" and wording.status == "ok":
        return wording.body, "ok"
    status = wording.status
    if status in ("not_delivered", "disabled", "unavailable"):
        return None, "disabled"
    if status in ("rejected", "ungrounded", "rate_limited", "capped"):
        return None, status
    return None, "error"


def _enrich(
    definition: dict,
    journey_key: str,
    pair: tuple[str, str],
    now: datetime,
    entries: tuple[_Entry, ...],
    epoch: int,
    digest: str,
) -> None:
    """The background task: one matcher run with lookups, then the wordings (KTD4, KTD5).

    Runs on the module's own pool, never on the request. Every write goes through `_remember`,
    which refuses it once `epoch` has moved. Nothing here raises past this frame: a failure of the
    matcher marks every unresolved entry transient (`unanswered` / `error`) so the read reports it
    and the next read after `FAILURE_TTL_S` resubmits, a wording failure is one entry's, and
    anything else is logged by TYPE and leaves the entries `pending` on a finished task, which the
    next read resubmits.
    """
    try:
        _enrich_body(definition, journey_key, pair, now, entries, epoch, digest)
    except Exception as exc:
        logger.debug("Pull enrichment traceback", exc_info=True)
        logger.warning(
            "A pull enrichment task failed: %s",
            type(exc).__name__,
            extra={"fields": {"event": "notification_pull", "result": "enrich_failed", "pair_digest": digest}},
        )


def _enrich_body(
    definition: dict,
    journey_key: str,
    pair: tuple[str, str],
    now: datetime,
    entries: tuple[_Entry, ...],
    epoch: int,
    digest: str,
) -> None:
    enabled = _webhook_delivery_enabled()
    ordered = _alive(entries, now)
    try:
        alerts = varsel.match_journey(definition, now, with_alternative=True, path=alternatives._PULL_PATH)
    except Exception as exc:
        logger.debug("Matcher traceback for a pull enrichment", exc_info=True)
        logger.warning(
            "The matcher failed on a pull enrichment: %s",
            type(exc).__name__,
            extra={"fields": {"event": "notification_pull", "result": "enrich_failed", "pair_digest": digest}},
        )
        _mark_unresolved(
            definition, journey_key, pair, ordered, now,
            alternative_status="unanswered", wording_status="error", enabled=enabled, epoch=epoch,
        )
        return
    by_train = {}
    for alert in varsel.alert_worthy_trains(alerts):
        train_key = varsel._train_key(alert)
        if train_key is not None:
            by_train.setdefault(train_key, alert)
    not_looked_up = 0
    for position, entry in enumerate(ordered):
        if _cached_alternative(pair, entry.train_key, now) is not None:
            continue  # a live record -- terminal, or a marker not yet expired -- stands
        record = _alternative_record(by_train.get(entry.train_key), position)
        not_looked_up += record.status == "not_looked_up"
        _remember_alternative(_alternative_key(pair, entry.train_key), record, epoch)
    if not_looked_up:
        _log_bounded("alternative_lookups", not_looked_up, digest)

    to_word: list[tuple[int, _Entry]] = []
    beyond = 0
    for position, entry in enumerate(ordered):
        state = _entry_state(definition, journey_key, pair, entry, now, enabled=enabled, position=position)
        if state.wording_status == _NOT_WORDED:
            # Past the cap in this task's order and nothing cached for it: counted for the bounded
            # record and written NOWHERE (2026-09-10) -- the verdict is the position, and the next
            # read derives it again from wherever the train sits then.
            beyond += 1
            continue
        if state.wording_status != _PENDING or state.wording_key is None:
            continue  # the poller's, already worded, or a marker that still stands
        to_word.append((position, entry))
    if beyond:
        _log_bounded("wordings_per_read", beyond, digest)
    if not to_word:
        return
    rate_limited = 0
    with ThreadPoolExecutor(max_workers=WORDING_WORKERS, thread_name_prefix="pull-word") as pool:
        for status in pool.map(
            lambda item: _word_one(definition, journey_key, pair, item[1], now, enabled, epoch, digest, position=item[0]),
            to_word,
        ):
            rate_limited += status == "rate_limited"
    if rate_limited:
        _log_bounded("wording_allowance", rate_limited, digest)


def _word_one(
    definition: dict,
    journey_key: str,
    pair: tuple[str, str],
    entry: _Entry,
    now: datetime,
    enabled: bool,
    epoch: int,
    digest: str,
    *,
    position: int,
) -> str | None:
    """One train's wording through `notification_wording.PULL`, single-flight per wording key.

    The state is re-read here rather than carried in, because the alternative record was written
    moments ago on this task and the key depends on it (`position` rides along for the same
    re-read; it is under the cap by construction). `subject_id` is the pair digest (KTD4) --
    an opaque per-process label, never the pattern -- and the message input is the one built after
    the alternative, so the model and the template word the same document (2026-08-05). No
    `budget`: the per-read cap was spent by the caller before this was reached, and `write_wording`'s
    own `PassBudget` is the poller pass's shape, not this one's.
    """
    state = _entry_state(definition, journey_key, pair, entry, now, enabled=enabled, position=position)
    key = state.wording_key
    if state.wording_status != _PENDING or key is None:
        return state.wording_status
    with _enrichment_lock:
        # Close the gap between the optimistic read above and claiming ownership, as
        # `meldinger._replay_one_cached` does: another task for a definition sharing this train may
        # have written the record AND released its in-flight event since, and taking ownership
        # then would word the train a second time and charge the allowance twice.
        cached = _wordings.get(key)
        if cached is not None and monotonic() < cached[0]:
            _wordings.move_to_end(key)
            return cached[1].status
        event = _wording_inflight.get(key)
        owner = event is None
        if owner:
            event = threading.Event()
            _wording_inflight[key] = event
    assert event is not None
    if not owner:
        event.wait(MAX_WAIT_S)
        return None
    try:
        try:
            wording = notification_wording.write_wording(
                notification_wording.Route(definition["from_stop"], definition["to_stop"]),
                notification_wording.DepartureWordingFacts(situation_number=entry.winner.situation_number),
                policy=notification_wording.PULL,
                subject_id=digest,
                now=now,
                message_input=state.message_input,
            )
            text, status = _wording_outcome(wording)
        except Exception:
            logger.warning(
                "Pull wording step raised; using the deterministic wording",
                exc_info=True,
                extra={"fields": {"event": "notification_wording", "result": "error", "surface": "pull"}},
            )
            text, status = None, "error"
        _remember_wording(key, _WordingRecord(text, status, entry.train_key), epoch)
        return status
    finally:
        _release(_enrichment_lock, _wording_inflight, key, event)


def _submit(
    definition: dict,
    journey_key: str,
    pair: tuple[str, str],
    now: datetime,
    entries: tuple[_Entry, ...],
    digest: str,
) -> Future | None:
    """The task for this definition and day: the one running, a new one, or None when the queue is
    full. Bounded BY THIS MODULE at `WORKERS + QUEUE_MAX` unfinished tasks (KTD5)."""
    key = _inflight_key(journey_key, now)
    with _tasks_lock:
        existing = _inflight.get(key)
        if existing is not None and not existing.done():
            return existing
        if sum(not f.done() for f in _inflight.values()) >= WORKERS + QUEUE_MAX:
            return None
        if len(_inflight) > FIGURES_CACHE_MAX:
            for stale in [k for k, f in _inflight.items() if f.done()]:
                _inflight.pop(stale, None)
        future = _executor.submit(_enrich, definition, journey_key, pair, now, entries, _epoch, digest)
        _inflight[key] = future
        return future


def _mark_unresolved(
    definition: dict,
    journey_key: str,
    pair: tuple[str, str],
    entries: list[_Entry],
    now: datetime,
    *,
    alternative_status: str,
    wording_status: str,
    enabled: bool,
    epoch: int,
) -> None:
    """Write a transient marker on every entry still owed something, and on nothing else.

    "Owed" is `_entry_state`'s own verdict, for both halves: an entry the poller owns (its wording
    is cached in `varsel`) reads terminal there and gets no marker, even though the read's own
    alternative cache has never heard of its train -- a marker written on that absence would sit
    in the cache and be served as this read's outcome the moment the poller forgot the train.
    An entry past the wording cap in this order reads `not_worded` rather than `pending` and gets
    no wording marker either: it was owed no wording by this task, and the next read re-derives
    its position. `entries` is an alive prefix -- `_alive` itself, or the listed cut of it -- so
    the index is the position.
    """
    for position, entry in enumerate(entries):
        state = _entry_state(definition, journey_key, pair, entry, now, enabled=enabled, position=position)
        if state.alternative_status == _PENDING:
            _remember_alternative(
                _alternative_key(pair, entry.train_key), _AlternativeRecord(alternative_status, None), epoch
            )
        if state.wording_status == _PENDING and state.wording_key is not None:
            _remember_wording(state.wording_key, _WordingRecord(None, wording_status, entry.train_key), epoch)


def _mark_capped(definition: dict, journey_key: str, pair: tuple[str, str], entries: list[_Entry], now: datetime) -> None:
    """A refused submission: every entry still owed something is marked `capped`, transient, so
    the reads that follow within `FAILURE_TTL_S` report it without each trying to submit again."""
    _mark_unresolved(
        definition, journey_key, pair, entries, now,
        alternative_status=_CAPPED, wording_status=_CAPPED, enabled=_webhook_delivery_enabled(), epoch=_epoch,
    )


def _claim_waiter() -> bool:
    global _waiters
    with _tasks_lock:
        if _waiters >= MAX_CONCURRENT_WAITERS:
            return False
        _waiters += 1
        return True


def _release_waiter() -> None:
    global _waiters
    with _tasks_lock:
        _waiters = max(0, _waiters - 1)


async def _wait_for_enrichment(future: Future, wait: int, digest: str) -> bool:
    """Park on the task from a waiter slot, or not at all. Returns whether a wait happened.

    No slot means the read behaves as `wait=0` (KTD9): the answer is the figures and the statuses,
    the task still runs, and a bounded record says the slots bound rather than a 503.
    """
    if wait <= 0:
        return False
    if not _claim_waiter():
        _log_bounded("waiters", 1, digest)
        return False
    try:
        await _await_task(future, wait)
    finally:
        _release_waiter()
    return True


async def _await_task(future: Future, wait: int) -> None:
    """Park on the task for at most `wait` seconds without pinning a thread and without being able
    to cancel it (KTD9): `wrap_future` turns the pool's Future into an awaitable, `shield` keeps a
    timeout from cancelling the task underneath, and the task's own exception is its own -- it has
    already recorded what the read should say."""
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=min(wait, MAX_WAIT_S))
    except TimeoutError:
        return
    except Exception:
        logger.debug("A pull enrichment task failed; the read reports what it recorded", exc_info=True)


# ---- Assembly ----------------------------------------------------------------------------------


_coverage_lock = threading.Lock()
#: The stop places today's timetable holds, and the stop-cache LIST the set was built from. Built
#: once per stop-cache refresh rather than per request (U15): the set is over the poller's whole
#: stop cache, and at `MAX_PER_MIN` that is thousands of rebuilds a minute of a list that changes
#: at most daily. `None` means the provider is unwired or answered nothing, which is the fail-open
#: reading -- the same one this had before there was a memo -- and is deliberately distinct from an
#: empty set.
#:
#: **Keyed on the ROWS and not on the prediction-cache generation**, which is what U15's plan
#: proposed. Coverage is a function of the STOP cache, and the two are refreshed on different TTLs:
#: keyed on the generation the set would rebuild every cycle for a list that had not moved, AND
#: still report a newly-served stop place as absent until the next cycle. Identity does strictly
#: less work and is strictly fresher.
#:
#: The rows are held by a STRONG reference so `is` is sound -- an id compared against a freed
#: object's is a wrong answer, not a miss. `main._varsel_stops` returns the cache list itself and
#: `refresh_varsel_stops` REBINDS it rather than mutating in place, which is what makes identity
#: mean "the same refresh"; a provider that rebuilt the list per call would simply always miss.
_coverage_present: frozenset | None = None
_coverage_rows: list | None = None


def _present_stop_places() -> frozenset | None:
    """The memo. The BUILD is outside the lock on purpose: two callers racing a refresh build the
    same set twice and the second overwrites the first with an equal value, which is cheaper than
    holding a module lock across a pass over the whole stop cache while `MAX_PER_MIN` requests
    queue behind it."""
    global _coverage_present, _coverage_rows
    rows = _stop_rows()
    with _coverage_lock:
        if rows is _coverage_rows:
            return _coverage_present
    present = frozenset(row.get("stop_place") for row in rows) if rows else None
    with _coverage_lock:
        _coverage_present = present
        _coverage_rows = rows
    return present


def _coverage(definition: dict) -> CoverageOut:
    present = _present_stop_places()
    if present is None:
        return CoverageOut(
            operators=list(SERVED_OPERATORS), from_stop_in_timetable_today=True, to_stop_in_timetable_today=True
        )

    def known(ref: str) -> bool:
        return any(r in present for r in varsel.place_refs(ref))

    return CoverageOut(
        operators=list(SERVED_OPERATORS),
        from_stop_in_timetable_today=known(definition["from_stop"]),
        to_stop_in_timetable_today=known(definition["to_stop"]),
    )


def _assembled_entry(definition: dict, entry: _Entry, state: _EntryState, rank: int) -> JourneyAlertOut:
    """One answer entry from the figures plus what the caches hold: the message input built after
    the alternative, the wording through `varsel.notification_wording_for` (the route line around
    the text, the template when there is none), the payload through `varsel._webhook_payload` with
    the uncollapsed alerts about the train -- `varsel._observations_for_key`'s build, restated for a
    definition nobody registered, with registry-resolved names in `reise` and never a typed one."""
    resolved = state.message_input.get("reise") or {}
    from_name = resolved.get("fra") or definition["from_stop"]
    to_name = resolved.get("til") or definition["to_stop"]
    title, body = varsel.notification_wording_for(state.message_input, from_name, to_name, state.text)
    payload = varsel._webhook_payload(
        state.winner, list(entry.contributing), message_input=state.message_input, title=title, body=body
    )
    return JourneyAlertOut(
        severity=rank,
        phase="confirmed" if entry.winner.cancelled else "predicted",
        train=entry.train,
        aimed_departure_time=_utc(entry.winner.departure_time),
        expected_departure_time=_utc(entry.expected_departure),
        situations=list(entry.situations),
        payload=payload,
        wording=WordingOut(source=state.wording_source, status=state.wording_status),
        alternative=AlternativeOut(status=state.alternative_status),
    )


def _listed_states(
    definition: dict, journey_key: str, pair: tuple[str, str], listed: list[_Entry], now: datetime, *, enabled: bool
) -> list[_EntryState]:
    """What the caches say about each listed entry, resolved once per request -- and again only
    after a wait, which is the one thing between the two reads that can move them. `listed` is a
    prefix of the alive order, so its index is the position the wording cap is spent by."""
    return [
        _entry_state(definition, journey_key, pair, entry, now, enabled=enabled, position=position)
        for position, entry in enumerate(listed)
    ]


def _assemble(
    definition: dict, alive: list[_Entry], states: list[_EntryState], limit: int
) -> tuple[list[JourneyAlertOut], int, bool]:
    """The answer's list from the figures and their resolved states: the rank, the cut, `complete`.

    `alive` is the departure cut already applied against THIS request's clock (`_alive`), because
    `catchable_after` is match time and a cached figure can outlive a departure by a cycle.
    `severity` is the 1-based position in the worst-first order over every train that survived the
    cut, assigned before `limit` so a truncated answer's ranks still mean the same thing.
    `complete` is over the LISTED entries: it describes the answer a cache would hold.
    """
    by_rank = sorted(alive, key=lambda e: varsel._alert_rank(e.winner), reverse=True)
    rank = {id(e): position for position, e in enumerate(by_rank, start=1)}
    out = [
        _assembled_entry(definition, entry, state, rank[id(entry)])
        for entry, state in zip(alive[:limit], states, strict=True)
    ]
    return out, len(alive), all(state.terminal for state in states)


def _log(
    result: str,
    *,
    cache: str | None,
    n_alerts: int | None,
    wait_s: int,
    started: float,
    pair_digest: str | None,
    level: int = logging.INFO,
    message: str = "journey-alert pull read",
) -> None:
    """The one record per request (R11): these seven fields and nothing else.

    A field that does not apply (`cache` on a refused read, `n_alerts` on a failed one) is null
    rather than absent, so the record's shape is one shape. Written as `extra={"fields": {...}}`
    with a LITERAL dict, because the contract suite's AST walk sees only literals -- a field added
    here must be seen by it.
    """
    logger.log(
        level,
        message,
        extra={
            "fields": {
                "event": "notification_pull",
                "result": result,
                "cache": cache,
                "n_alerts": n_alerts,
                "wait_s": wait_s,
                "ms": round((monotonic() - started) * 1000, 1),
                "pair_digest": pair_digest,
            }
        },
    )


def _log_bounded(reason: str, n: int, pair_digest: str) -> None:
    """A bound binding on the enrichment (R11): which one, how many entries it touched, the pair's
    digest -- and nothing identifying. `reason` is one of `queue_full`, `wordings_per_read`,
    `alternative_lookups`, `wording_allowance`, `waiters`."""
    logger.info(
        "journey-alert pull enrichment bounded: %s (%d)",
        reason,
        n,
        extra={
            "fields": {
                "event": "notification_pull_bounded",
                "reason": reason,
                "n": n,
                "pair_digest": pair_digest,
            }
        },
    )


def _retry_later(detail: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=detail,
        headers={"Retry-After": str(notification_auth.RETRY_AFTER_S)},
    )


# ---- The route ---------------------------------------------------------------------------------

TAG = "notification-journey-alerts"

router = APIRouter(prefix=notification_api.PREFIX, tags=[TAG])


# `async def`, like `meldinger.messages` and for the same reason (KTD9): the wait on enrichment must
# not hold one of the anyio pool's slots, which every plain-`def` route in this process -- `/impact`,
# the `/varsel-api` writes -- shares. The one blocking step here, the matcher on a figures miss, runs
# through `run_in_threadpool`; the enrichment runs on this module's own pool and is only ever AWAITED
# here.
@router.get(
    _ROUTE,
    summary="The alert-worthy trains ahead on a journey definition, right now",
    response_model=JourneyAlertsResponse,
    responses={
        422: {"description": "A malformed definition, period, weekday list, wait or limit."},
        429: {"description": "The read's own limiter, global or per stop pair. Retry-After is 60 s."},
        503: {"description": "The prediction cache is not loaded yet, or the matcher failed. Retry-After."},
    },
)
async def journey_alerts(
    response: Response,
    from_stop: str = Query(description="Boarding stop, `NSR:StopPlace:<digits>`."),
    to_stop: str = Query(description="Alighting stop, `NSR:StopPlace:<digits>`."),
    begin: str | None = Query(default=None, description='Period start "HH:MM" (Europe/Oslo). Both or neither.'),
    end: str | None = Query(default=None, description='Period end "HH:MM" (Europe/Oslo), after `begin`.'),
    days: list[int] | None = Query(
        default=None,
        description="Weekdays to ask about, 0=Monday .. 6=Sunday, as a REPEATED parameter "
        "(`days=0&days=1`). Default: today's and tomorrow's weekday.",
    ),
    wait: int = Query(
        default=MAX_WAIT_S,
        ge=0,
        le=MAX_WAIT_S,
        description="Seconds to wait for the wording and the alternative journey. 0 answers at once "
        "with whatever is ready; the work continues in the background either way.",
    ),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Entries to return."),
) -> JourneyAlertsResponse:
    """The alert-worthy trains ahead on this journey definition, with no registration.

    Each entry carries the predicted delay and the chance of cancellation for one train (for a
    train ET has declared cancelled the chance is 1.0 and the delay figures are null), plus
    `payload`, which has the same shape as a webhook event's payload. The wording in
    `payload.notification` is what the event would have contained if it were generated now.

    `days` is a repeated query parameter only -- `days=0&days=1` -- and strictly integers 0..6: a
    comma-separated list is a 422, as is any other shape. No period means the whole day
    (`00:00`-`23:59`); no days means today's and tomorrow's Oslo weekday.
    """
    started = monotonic()
    now = _now()
    definition = _definition(from_stop, to_stop, begin, end, days, now)
    pair = (definition["from_stop"], definition["to_stop"])
    digest = alternatives.pair_digest(*pair)
    key = varsel.journey_key(definition)
    windows = varsel.journey_windows(definition, now)

    if not _claim(pair, monotonic()):
        _log("rate_limited", cache=None, n_alerts=None, wait_s=wait, started=started, pair_digest=digest)
        raise HTTPException(
            status_code=429,
            detail="rate_limited",
            headers={"Retry-After": str(notification_auth.RETRY_AFTER_S)},
        )

    refreshed_at = _refreshed_at_provider() if _refreshed_at_provider is not None else None
    if refreshed_at is None:
        _log("not_loaded", cache=None, n_alerts=None, wait_s=wait, started=started, pair_digest=digest)
        raise _retry_later("predictions_not_loaded")

    cache_key = _cache_key(key, refreshed_at, now)
    figures = _cached_figures(cache_key, refreshed_at)
    cache = "hit"
    if figures is None:
        cache = "miss"
        try:
            figures = await run_in_threadpool(_figures_for, definition, cache_key, refreshed_at, now)
        except Exception as exc:
            # The exception's TYPE only, in the message, at WARNING: its text can name a ref (a
            # KeyError on a stop-place lookup carries it verbatim), and this record is routine.
            # The traceback goes to DEBUG, which no environment ships to Cloud Logging by default.
            logger.debug("Matcher traceback for a pull read", exc_info=True)
            _log(
                "matcher_failed",
                cache=cache,
                n_alerts=None,
                wait_s=wait,
                started=started,
                pair_digest=digest,
                level=logging.WARNING,
                message=f"The matcher failed on a pull read: {type(exc).__name__}",
            )
            raise _retry_later("matcher_failed") from None

    # The enrichment (KTD5, KTD9). Owed when any listed entry has nothing terminal or standing in
    # the caches; submitted once per definition per day, joined by every later read; waited on for
    # `wait` seconds from a bounded number of waiter slots, and without one the read is a `wait=0`
    # read rather than a refusal.
    enabled = _webhook_delivery_enabled()
    alive = _alive(figures.entries, now)
    listed = alive[:limit]
    states = _listed_states(definition, key, pair, listed, now, enabled=enabled)
    if any(state.pending for state in states):
        future = _submit(definition, key, pair, now, figures.entries, digest)
        if future is None:
            _log_bounded("queue_full", len(listed), digest)
            _mark_capped(definition, key, pair, listed, now)
            states = _listed_states(definition, key, pair, listed, now, enabled=enabled)
        elif await _wait_for_enrichment(future, wait, digest):
            states = _listed_states(definition, key, pair, listed, now, enabled=enabled)

    alerts, n_alerts, complete = _assemble(definition, alive, states, limit)
    response.headers["Cache-Control"] = _CACHE_CONTROL_COMPLETE if complete else _CACHE_CONTROL_INCOMPLETE
    _log("ok", cache=cache, n_alerts=n_alerts, wait_s=wait, started=started, pair_digest=digest)
    return JourneyAlertsResponse(
        as_of=_utc(now),
        refreshed_at=_utc(refreshed_at),
        complete=complete,
        n_alerts=n_alerts,
        truncated=n_alerts > len(alerts),
        journey=JourneyOut(**definition),
        windows=[WindowOut(start=s, end=e) for s, e in windows],
        coverage=_coverage(definition),
        alerts=alerts,
    )


def _log_status(result: str, *, n_trains: int | None, started: float, pair_digest: str) -> None:
    """One `notification_status` record per journey-status read (R13), under this module's R11: a
    count, a duration and the pair's salted digest, and nothing identifying. A LITERAL dict, like
    `_log`'s, so the contract suite's AST walk sees every field."""
    logger.info(
        "journey-status read: %s",
        result,
        extra={
            "fields": {
                "event": "notification_status",
                "result": result,
                "n_trains": n_trains,
                "ms": round((monotonic() - started) * 1000, 1),
                "pair_digest": pair_digest,
            }
        },
    )


# `async def` like the alerts read, although nothing here waits: the answer is a walk over memory the
# poller already holds (`journey_status`), so it neither needs the anyio pool nor blocks the loop for
# longer than a dict lookup per train. It spends the SAME limiter buckets as `journey-alerts` -- a
# client reading both for one favourite spends the per-pair thirty twice as fast, which the
# operations document states.
@router.get(
    _STATUS_ROUTE,
    summary="Every train in a journey definition's window today, graded",
    response_model=JourneyStatusResponse,
    responses={
        422: {"description": "A malformed definition, period or weekday list."},
        429: {"description": "The read's own limiter, shared with journey-alerts. Retry-After is 60 s."},
        503: {"description": "The day's timetable is not loaded yet, or grading failed. Retry-After."},
    },
)
async def journey_status_read(
    response: Response,
    from_stop: str = Query(description="Boarding stop, `NSR:StopPlace:<digits>`."),
    to_stop: str = Query(description="Alighting stop, `NSR:StopPlace:<digits>`."),
    begin: str | None = Query(default=None, description='Period start "HH:MM" (Europe/Oslo). Both or neither.'),
    end: str | None = Query(default=None, description='Period end "HH:MM" (Europe/Oslo), after `begin`.'),
    days: list[int] | None = Query(
        default=None,
        description="Weekdays the definition travels on, 0=Monday .. 6=Sunday, as a REPEATED "
        "parameter. Default: today's and tomorrow's weekday.",
    ),
) -> JourneyStatusResponse:
    """Every Vy train in today's window on this definition, each graded: `confirmed` where the
    outcome is known, `predicted` where the model has a figure, `unknown` otherwise.

    Built per request from memory: the day's planned timetable (rebuilt hourly), the prediction
    cache (every poller cycle) and today's recorded calls. `as_of` says how old each is. A day not
    in `days` answers an empty list with `reason: not_a_travel_day`; a timetable not loaded yet is
    503, never an empty list.
    """
    started = monotonic()
    now = _now()
    definition = _definition(from_stop, to_stop, begin, end, days, now)
    pair = (definition["from_stop"], definition["to_stop"])
    digest = alternatives.pair_digest(*pair)
    if not _claim(pair, monotonic()):
        _log_status("rate_limited", n_trains=None, started=started, pair_digest=digest)
        raise HTTPException(
            status_code=429,
            detail="rate_limited",
            headers={"Retry-After": str(notification_auth.RETRY_AFTER_S)},
        )
    try:
        status = journey_status.status_for(definition, now)
    except Exception as exc:
        # The embed guards the same call per journey; here a raise would be an unguarded 500 with a
        # traceback and no record. The exception's TYPE only, as `journey_alerts` logs its matcher:
        # its text can name a ref.
        logger.debug("journey-status traceback for a public read", exc_info=True)
        logger.warning("The journey-status grading failed on a public read: %s", type(exc).__name__)
        _log_status("status_failed", n_trains=None, started=started, pair_digest=digest)
        raise _retry_later("status_failed") from None
    if status is None:
        _log_status("not_loaded", n_trains=None, started=started, pair_digest=digest)
        raise _retry_later("plan_not_loaded")
    coverage = _coverage(definition).model_copy(update={"operators": list(journey_status.OPERATORS)})
    response.headers["Cache-Control"] = _CACHE_CONTROL_COMPLETE
    _log_status("ok", n_trains=len(status.trains), started=started, pair_digest=digest)
    return JourneyStatusResponse(**dict(status), journey=JourneyOut(**definition), coverage=coverage)


def shutdown() -> None:
    """Stop the enrichment pool without waiting: called from `main.lifespan` on exit. Queued tasks
    are cancelled; a running one finishes on its own and its writes go nowhere anybody reads."""
    _executor.shutdown(wait=False, cancel_futures=True)


def reset_state() -> None:
    """Forget the limiter, the figures cache, the single-flight events, the two enrichment caches
    and the in-flight map; replace the executor and advance the epoch. Test-only.

    One seam, like `notification_wording.reset_state`: a leaked bucket reads as the limiter firing
    for no reason, a leaked figure reads as the matcher not running, and a leaked wording reads as
    the model not being asked -- each exactly the claim some test makes. The epoch is what makes a
    task still running from the previous test unable to write into this one's caches (KTD5); the
    executor is replaced so its queue is dropped with it.
    """
    global _figures_generation, _executor, _epoch, _waiters, _coverage_present, _coverage_rows
    with _limiter_lock:
        _global_bucket.clear()
        _pair_buckets.clear()
    with _figures_lock:
        _figures.clear()
        _figures_inflight.clear()
        _figures_generation = None
    with _coverage_lock:
        _coverage_present = None
        _coverage_rows = None
    with _tasks_lock:
        _epoch += 1
        old = _executor
        _executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="pull-enrich")
        _inflight.clear()
        _waiters = 0
    old.shutdown(wait=False, cancel_futures=True)
    with _enrichment_lock:
        _wordings.clear()
        _wording_keys_by_train.clear()
        _alternative_outcomes.clear()
        _wording_inflight.clear()
        _listed_trains.clear()
