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
The wording is always the template here (`wording.status = "not_asked"`) and no alternative journey
is looked up (`alternative.status = "not_looked_up"`); U4 adds both behind the `wait` budget, which
this unit validates and logs but does not otherwise use.

Three bounds this module owns, each a number so it can be checked, and each published in
`docs/operations/notification-webhooks.md`'s constants table:

* **Its own limiter** (KTD11, R7): a global per-minute ceiling and a per-STOP-PAIR ceiling in a
  bounded map, independent of the credentialed tiers. Per pair rather than per definition, because
  a definition is free to mint -- three hundred periods on one pair are one bucket.
* **A figures cache per definition per poller cycle** (KTD3, R8): keyed on `(journey_key,
  refreshed_at, Oslo day)` with single-flight per key, so a warm read within a cycle runs no matcher.
* **A cold cache is a 503, never an empty list** (KTD8, R6): a cache the poller has not loaded has no
  situations in it, and "no disruptions" off it would report an empty process as a calm day.

R11 binds every log record here: no stop ref, no name, no departure time, no prose, and nothing
derived from the definition beyond `alternatives.pair_digest`, the salted per-process digest, under
the field name that module already logs it as. The process's HTTP access log redacts this path's
query string too (`logging_setup._AccessLogRedaction`), because the refs ride in it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from zoneinfo import ZoneInfo

import alert_policy
import alternatives
import notification_api
import notification_auth
import notification_events
import varsel
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

#: Reads per minute across every caller. The same number as `notification_auth.
#: GLOBAL_PRELOOKUP_PER_MIN`, chosen for the same reason: it is what the process comfortably serves
#: from in-memory caches, and a read that misses its figures cache costs one matcher run on the
#: request pool -- milliseconds against the poller's in-process snapshot, with no Journey Planner
#: lookup on this phase.
MAX_PER_MIN = 300
#: Reads per minute per canonical stop pair. What a favourites panel polling every minute on a phone
#: needs is one; thirty is room for a client's whole fleet behind one NAT without being room for
#: a burst that minted three hundred definitions on one corridor.
PER_PAIR_PER_MIN = 30
#: How many pair buckets the limiter remembers before dropping the insertion-oldest
#: (`feedback._claim`'s shape). A pair bucket is a few floats; ten thousand is ~a day of distinct
#: pairs at the global ceiling.
LIMITER_KEYS_MAX = 10_000
#: The longest a caller may ask the read to wait for enrichment (U4). Validated and logged here;
#: otherwise unused in this unit. Also the bound on a single-flight waiter below, so this module
#: adds no second clock of its own.
MAX_WAIT_S = 20
#: Entries served when the caller names no `limit`, and the most it may ask for. A panel shows a
#: handful; the cap is what stops a caller turning one read into the whole cycle's worth of trains.
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
#: Figures cached per poller cycle. One entry per definition read this cycle; at ~300 reads a
#: minute and a ~5 min cycle the working set is well under this, and an entry is a few KB.
FIGURES_CACHE_MAX = 512

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
_CACHE_CONTROL = "public, max-age=60"
#: `wording.status` and `alternative.status` in this unit: nothing was asked and nothing was looked
#: up. U4 replaces them with the enrichment's own vocabulary.
_WORDING_NOT_ASKED = "not_asked"
_ALTERNATIVE_NOT_LOOKED_UP = "not_looked_up"

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
    """Whether `payload.alternative_journey` was looked up at all."""

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
    phase: str = Field(description="`confirmed` when ET has declared the train cancelled, else `predicted`.")
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
    complete: bool = Field(description="Whether every entry's enrichment finished (always true in this unit).")
    n_alerts: int = Field(description="Alert-worthy trains ahead, before `limit` was applied.")
    truncated: bool
    journey: JourneyOut
    windows: list[WindowOut]
    coverage: CoverageOut
    alerts: list[JourneyAlertOut]


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

    Everything a request needs to assemble its answer without touching the matcher: the winner and
    the payload built from it, the train's identity, and the expected departure the per-request cut
    re-applies -- `catchable_after` is match time, and a cached figure can outlive a departure by a
    cycle.
    """

    winner: varsel.Alert
    train: TrainOut
    expected_departure: datetime
    situations: tuple[str, ...]
    payload: dict


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


def _cached_figures(key: tuple, refreshed_at: datetime) -> _Figures | None:
    global _figures_generation
    with _figures_lock:
        if _figures_generation is None or refreshed_at > _figures_generation:
            # A NEWER load: every entry of the older generation goes at once (KTD3).
            _figures.clear()
            _figures_generation = refreshed_at
            return None
        if refreshed_at != _figures_generation:
            # An OLDER load -- a caller that read `refreshed_at` just before the poller moved. It
            # misses, and `_remember_figures` refuses its result; it must not wipe the newer
            # generation's entries and regress the marker, which would cost one extra matcher run
            # per such straddle.
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
        return _compute_figures(definition, now)
    try:
        figures = _compute_figures(definition, now)
        _remember_figures(key, refreshed_at, figures)
        return figures
    finally:
        with _figures_lock:
            _figures_inflight.pop(key, None)
        event.set()


def _compute_figures(definition: dict, now: datetime) -> _Figures:
    """The matcher, the selection and the per-train build -- what a request assembles from.

    Selection is `varsel.alert_worthy_trains` over `varsel.match_journey`'s alerts: the per-train
    collapse first, then the push gate, in the pass's own order (KTD7). The build per train is
    `varsel._subjects_for_key`'s, restated for a definition nobody registered: the message input
    from `llm_message.build_message_input` (registry-resolved place names in `reise`, never a typed
    name -- there is none here), the template wording through `varsel.notification_wording_for`
    with no model text, and the payload through `varsel._webhook_payload` with the UNCOLLAPSED
    alerts about that train as its contributing situations. A train `notification_events.
    train_ref_of` cannot name stably is skipped exactly as that loop skips it, and is not counted
    anywhere: `n_alerts` is what the answer lists.
    """
    alerts = varsel.match_journey(definition, now)
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
        if ref is None or expected is None:
            continue
        message_input = _message_input(definition, winner, now)
        resolved = message_input.get("reise") or {}
        from_name = resolved.get("fra") or definition["from_stop"]
        to_name = resolved.get("til") or definition["to_stop"]
        title, body = varsel.notification_wording_for(message_input, from_name, to_name, None)
        payload = varsel._webhook_payload(
            winner,
            contributing.get(varsel._train_key(winner), [winner]),
            message_input=message_input,
            title=title,
            body=body,
        )
        entries.append(
            _Entry(
                winner=winner,
                train=TrainOut(
                    operator=ref.operator,
                    line=ref.line,
                    journey_ref=ref.journey_ref,
                    call_date=ref.call_date,
                    stop_place=ref.stop_place,
                    stop_order=ref.stop_order,
                ),
                expected_departure=expected,
                situations=tuple(s["situation_number"] for s in payload["situations"]),
                payload=payload,
            )
        )
    return _Figures(entries=tuple(entries))


def _message_input(definition: dict, alert: varsel.Alert, now: datetime) -> dict:
    """`varsel._message_input`'s build with the read's clock, and its fallback on failure.

    The clock matters: `i_morgen` -- and so `payload.call.departs_tomorrow` -- is decided against
    `now`, which is the same instant the windows were built from. The fallback is `varsel`'s own
    (`_message_input_from_alert`), which carries no `reise`, so the route line then names the refs.
    """
    try:
        import llm_message

        return llm_message.build_message_input(definition, alert.impact, alert, now=now)
    except Exception:
        logger.debug("Could not build a message input for a pull read; wording from the alert", exc_info=True)
        return varsel._message_input_from_alert(alert)


# ---- Assembly ----------------------------------------------------------------------------------


def _coverage(definition: dict) -> CoverageOut:
    rows = _stop_rows()
    if not rows:
        return CoverageOut(
            operators=list(SERVED_OPERATORS), from_stop_in_timetable_today=True, to_stop_in_timetable_today=True
        )
    present = {row.get("stop_place") for row in rows}

    def known(ref: str) -> bool:
        return any(r in present for r in varsel.place_refs(ref))

    return CoverageOut(
        operators=list(SERVED_OPERATORS),
        from_stop_in_timetable_today=known(definition["from_stop"]),
        to_stop_in_timetable_today=known(definition["to_stop"]),
    )


def _assemble(entries: tuple[_Entry, ...], now: datetime, limit: int) -> tuple[list[JourneyAlertOut], int]:
    """The answer's list from the cached figures: the departure cut, the order, the rank, the cut.

    The expected-departure cut is the matcher's own comparison (`impact.for_trip` drops a boarding
    whose expected departure is `< catchable_after`), re-applied against THIS request's clock. Order
    is soonest expected departure first with `varsel._alert_rank` -- higher is worse -- as the
    tie-break; two stable sorts, the tie-break first. `severity` is the 1-based position in the
    worst-first order over every train that survived the cut, assigned before `limit` so a truncated
    answer's ranks still mean the same thing.
    """
    alive = [e for e in entries if not e.expected_departure < now]
    alive.sort(key=lambda e: varsel._alert_rank(e.winner), reverse=True)
    rank = {id(e): position for position, e in enumerate(alive, start=1)}
    alive.sort(key=lambda e: e.expected_departure)
    out = [
        JourneyAlertOut(
            severity=rank[id(e)],
            phase="confirmed" if e.winner.cancelled else "predicted",
            train=e.train,
            aimed_departure_time=_utc(e.winner.departure_time),
            expected_departure_time=_utc(e.expected_departure),
            situations=list(e.situations),
            payload=e.payload,
            wording=WordingOut(source="deterministic", status=_WORDING_NOT_ASKED),
            alternative=AlternativeOut(status=_ALTERNATIVE_NOT_LOOKED_UP),
        )
        for e in alive[:limit]
    ]
    return out, len(alive)


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


def _retry_later(detail: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=detail,
        headers={"Retry-After": str(notification_auth.RETRY_AFTER_S)},
    )


# ---- The route ---------------------------------------------------------------------------------

TAG = "notification-journey-alerts"

router = APIRouter(prefix=notification_api.PREFIX, tags=[TAG])


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
    wait: int = Query(default=MAX_WAIT_S, ge=0, le=MAX_WAIT_S, description="Seconds to wait for enrichment."),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Entries to return."),
) -> JourneyAlertsResponse:
    """What `/varsel` would be warning about on this definition, with no registration.

    `days` is a repeated query parameter only -- `days=0&days=1` -- and strictly integers 0..6: a
    comma-separated list is a 422, as is any other shape. No period means the whole day
    (`00:00`-`23:59`); no days means today's and tomorrow's Oslo weekday, which is every window the
    prediction cache can carry.

    `async def`, like `meldinger.messages` and for the same reason (KTD9): U4's wait on enrichment
    must not hold one of the anyio pool's slots, which every plain-`def` route in this process --
    `/impact`, the `/varsel-api` writes -- shares. The one blocking step here, the matcher on a
    cache miss, runs through `run_in_threadpool`.
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

    alerts, n_alerts = _assemble(figures.entries, now, limit)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    _log("ok", cache=cache, n_alerts=n_alerts, wait_s=wait, started=started, pair_digest=digest)
    return JourneyAlertsResponse(
        as_of=_utc(now),
        refreshed_at=_utc(refreshed_at),
        complete=True,
        n_alerts=n_alerts,
        truncated=n_alerts > len(alerts),
        journey=JourneyOut(**definition),
        windows=[WindowOut(start=s, end=e) for s, e in windows],
        coverage=_coverage(definition),
        alerts=alerts,
    )


def reset_state() -> None:
    """Forget the limiter, the figures cache and the single-flight events. Test-only.

    One seam, like `notification_wording.reset_state`: a leaked bucket reads as the limiter firing
    for no reason, and a leaked figure reads as the matcher not running, which is exactly the claim
    the warm-read test makes. U4 extends this with the enrichment's own state.
    """
    global _figures_generation
    with _limiter_lock:
        _global_bucket.clear()
        _pair_buckets.clear()
    with _figures_lock:
        _figures.clear()
        _figures_inflight.clear()
        _figures_generation = None
