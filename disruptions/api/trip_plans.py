"""Journey Planner trip plans: one canonical stop pair's trips with a change, for one fetch range.

Direct trains come from `impact._trip_cells`, as they always have. This module asks the Journey
Planner (JP) for the planned structure of a pair's day and keeps what the matcher cannot see on its
own: the departures that reach the destination only with a change (:class:`trips.PlanTrip`), plus
the plan's direct patterns (:class:`trips.DirectDeparture`), which KTD3 and KTD20 read and which
are never subjects themselves. Our own predictions decide whether a trip is late; JP supplies only
the structure (KTD1).

:func:`fetch_range` pages through one range and parses it. :func:`refresh` is the leader's poller
phase (`trip_plans`): it fetches the ranges of registered definitions that are due and merges each
`complete` answer into the pair's :class:`trips.DayPlan` (:func:`merge`), through the store's
transaction and into this pod's memory. Every pod reads the store's plans into memory with
:func:`follow`, and every request reads memory only, through :func:`read_plan` (KTD2).

A public read that finds no plan for its range starts :func:`fetch_for_read` (U8, KTD1): a
background fetch on the `plans_pull` path into this pod's own bounded cache
(:data:`PUBLIC_PLANS_MAX`), which :func:`read_plan` consults after the leader's plans and which is
never stored and never refreshed by the leader.

:func:`refresh_realtime` (U4) asks JP, per pod and per tick, for the real-time state of the legs
our predictions do not score, and :func:`realtime_for` is the reader main injects into `trips`
(KTD8).

Verified live against JP v3, 2026-10-27
---------------------------------------
Hand-built GraphQL is checked against the real endpoint before a parser is written for it, as
`alternatives._TRIP_QUERY` was. Recorded replies from that session are the fixtures in
`test_trip_plans.py`.

* **The planned structure is `ignoreRealtimeUpdates: true`.** A/B on a real cancellation:
  VYG:ServiceJourney:2868-SKI_660212-R (L2 Ski -> Skøyen, 2026-10-27, cancelled at every call),
  asked Kolbotn (NSR:StopPlace:572) -> Skøyen (NSR:StopPlace:152) at 21:05:

  | arguments                           | the 21:10 departure                                  |
  |-------------------------------------|------------------------------------------------------|
  | defaults                            | absent                                               |
  | `includeRealtimeCancellations: true`| present, `cancellation: true` on both calls          |
  | `ignoreRealtimeUpdates: true`       | present, `cancellation: false`, `realtime: false`    |

  Adding either cancellation flag beside `ignoreRealtimeUpdates` changes nothing. So the plan keeps
  a cancelled departure (a realtime-routed answer hides exactly the trips we must judge) but carries
  NO real-time state: cancellation comes from the prediction cache (ET) and U4's per-leg real-time
  fetch, never from the plan. `realtime` is therefore not selected (it reads false on every leg),
  and the call-level `cancellation` is parsed only because it is cheap and would carry a planned
  (NeTEx) cancellation if JP ever flagged one.
* **`timetableView` stays unset** (its default behaves as `true`). `timetableView: false` cut Lier
  -> Oslo S at 07:00 from 4 patterns to 1, dropping the 07:17 trip with a change.
* **`searchWindow` stays unset**, for the reason `alternatives._TRIP_QUERY` records: clamping it
  shrinks the answer. `numTripPatterns` is left at JP's default, 12 a page.
* **Paging.** The reply carries `nextPageCursor` and `metadata { nextDateTime }`; the cursor goes
  back as `pageCursor` with the rest of the request unchanged. Adjacent pages can repeat the
  boundary pattern (Drøbak torg -> Asker: page 1's last and page 2's first both 06:57), so patterns
  are de-duplicated.
* **Density**, travel date 2026-10-28 from 06:00, each page at most 12 patterns:
  Nationaltheatret (NSR:StopPlace:58404) -> Oslo S (337) ~36-40 min of departures per page, the
  densest measured; Oslo S -> Lillestrøm (451) 100-180 min; Drøbak torg (5576) -> Asker (59616)
  ~50 min, 15-44 KB a page. That sets :data:`_PAGE_MINUTES` and :data:`_MAX_PAGES`.
* **Stop order.** JP's `stopPositionInPattern` counts from 0; ET's `stop_order` (dev
  `rail_calls_planned_today`) is that + 1 for VYG, checked on R13 1606 (Lier, position 15, order
  16, NSR:Quay:712) and L2 2868 (Oslo S, position 13, order 14, NSR:Quay:553). Not verified for
  buses. :class:`trips.PlanLeg` stores JP's position as sent, and U3 resolves a call on stop place,
  aimed time and service date, never on position alone.
* **Identity.** Every transit leg carries `serviceJourney.id`, `datedServiceJourney.id` and
  `serviceDate`. The rail cache keys on the dated ref (`DatedServiceJourney:1606_DAL-DRM_26-10-28`),
  the bus (RUT) cache on the plain `ServiceJourney:<hash>`, so a leg keeps both, through
  `alternatives.cache_key` (the SJN rewrite holds) as `alternatives._to_candidate` collects them.
* **Siblings (KTD19) do come back on some pairs.** Lier -> Oslo S returns none. Drøbak torg ->
  Asker does: the 07:07 bus 500 continues two ways (bus 81 to Lysaker and the R12, or on foot from
  Oslo bussterminal to the R12 at Oslo S, both arriving 08:29), and the 07:27 continues to the 08:49
  RE10 and the 09:01 L1. So KTD19's cushion engages on some pairs.
* **JP answers in CHILD stop places** (Lier 426 under parent 58835, Oslo S 337 under 59872). A
  registered stop may be a parent, so the caller passes the from-place's refs (`varsel.place_refs`)
  as `from_places`, and a pattern boarding anywhere else is not the pair's.

Real-time per leg (U4), verified live 2026-10-27 23:25
------------------------------------------------------
The recorded reply is `RECORDED_REALTIME` in `test_trip_plans.py`.

* **One request asks up to :data:`_REALTIME_BATCH` legs, as GraphQL aliases** over
  `datedServiceJourney(id: "<JP's full id>")`, selecting each call's stop place, `realtime`,
  `cancellation` and aimed and expected times (:data:`_REALTIME_LEG`, the verified document's
  selection). The id is JP's own, kept on the leg as `PlanLeg.dated_ref`: `cache_key` rewrites an
  SJN ref, so it cannot be rebuilt from `refs`.
* **`estimatedCalls` takes no arguments on `DatedServiceJourney`** (introspected), so every leg
  answers its whole journey: ~7 KB a leg, 27 calls for the RUT 500 bus, 17 for the VYG R12 507.
* **An unknown id comes back `null` under its alias, with no `errors`**, so one stale ref does not
  fail its batch, and null means JP has no such journey.
* **`realtime: false` means JP copied the aimed times into the expected ones.** The RUT bus for the
  next morning read false on every call, the VYG train true. Such a leg is unknown, never on time
  (KTD8).

Fails open like `alternatives.fetch_candidates`: every failure is a result, never an exception. A
malformed pattern is a failure of its whole page, never a pattern quietly skipped: only a `complete`
fetch is merged, and one that had skipped every pattern would merge as a pair with no trips. Every
fetch logs one `event="trip_plans_fetch"` record labelled by `alternatives.pair_digest`, never
by a stop, line, journey or time (R17).
"""

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import ceil
from time import monotonic, perf_counter
from typing import Any, Literal
from zoneinfo import ZoneInfo

import alternatives
import trips
from trips import CoveredRange, DayPlan, DirectDeparture, PlanLeg, PlanTrip

logger = logging.getLogger(__name__)

OSLO = ZoneInfo("Europe/Oslo")

# The modes block is `alternatives._TRIP_QUERY`'s, for its reasons (no air; foot access, egress
# and direct). The leg selection is that query's endpoints plus what identity needs (KTD5): quays,
# stop positions and `serviceDate`. No expected times and no `realtime`: under
# `ignoreRealtimeUpdates` both only echo the plan. `estimatedCalls` and
# `intermediateEstimatedCalls` are forbidden here as there: the two endpoint calls are all a leg
# needs, and a full call list is two orders of magnitude more payload per page.
_PLAN_QUERY = """
{
  trip(
    from: {place: "%(from_stop)s"}
    to: {place: "%(to_stop)s"}
    dateTime: "%(date_time)s"
    ignoreRealtimeUpdates: true%(page_cursor)s
    modes: {
      accessMode: foot
      egressMode: foot
      directMode: foot
      transportModes: [
        {transportMode: rail}
        {transportMode: bus}
        {transportMode: coach}
        {transportMode: tram}
        {transportMode: metro}
        {transportMode: water}
      ]
    }
  ) {
    nextPageCursor
    metadata { nextDateTime }
    tripPatterns {
      aimedStartTime
      legs {
        mode
        serviceDate
        aimedStartTime
        aimedEndTime
        line { id publicCode }
        serviceJourney { id }
        datedServiceJourney { id }
        fromPlace { name quay { id stopPlace { id } } }
        toPlace { name quay { id stopPlace { id } } }
        fromEstimatedCall { cancellation stopPositionInPattern }
        toEstimatedCall { cancellation stopPositionInPattern }
      }
    }
  }
}
"""

# Minutes of departures one page is assumed to cover when sizing the page bound: below the densest
# measured page (~36 min, Nationaltheatret -> Oslo S at peak), so a peak-hour range is not cut
# short and left `partial` on every fetch (the plan's risk table).
_PAGE_MINUTES = 30
# The bound's ceiling: the formula's own value for a full 24 h range (48 + 2), which covers a day
# even at the densest measured ~36 min per page (40 pages). A cap on a caller's range, not a
# tuning knob.
_MAX_PAGES = 50

FetchResultKind = Literal["complete", "partial", "failed"]


@dataclass(frozen=True)
class FetchResult:
    """One range's answer. Only `complete` covers the range (KTD1): `partial` means the page bound
    was reached or a later page failed or was refused its budget, and its trips are the pages that
    landed; `failed` means no page landed at all."""

    result: FetchResultKind
    trips: tuple[PlanTrip, ...]
    directs: tuple[DirectDeparture, ...]
    pages: int


def _page_bound(start: datetime, end: datetime) -> int:
    minutes = max(0.0, (end - start).total_seconds() / 60)
    return min(ceil(minutes / _PAGE_MINUTES) + 2, _MAX_PAGES)


def _render(from_stop: str, to_stop: str, date_time: datetime, cursor: str | None) -> str:
    return _PLAN_QUERY % {
        "from_stop": from_stop,
        "to_stop": to_stop,
        "date_time": date_time.isoformat(),
        # JSON's string escaping is GraphQL's, so a cursor cannot close the literal it sits in.
        "page_cursor": f"\n    pageCursor: {json.dumps(cursor)}" if cursor else "",
    }


def _quay(place: Any) -> str | None:
    return alternatives._as_dict(alternatives._as_dict(place).get("quay")).get("id")


def _leg(raw: dict, departure: datetime, arrival: datetime, walk_after_s: int) -> PlanLeg:
    # Both ref forms, dated first, as `alternatives._to_candidate` collects `transit_refs`.
    refs: list[tuple[str | None, str]] = []
    for key in ("datedServiceJourney", "serviceJourney"):
        ref = alternatives.cache_key(alternatives._as_dict(raw.get(key)).get("id"))
        if ref is not None and ref not in refs:
            refs.append(ref)
    line = alternatives._as_dict(raw.get("line"))
    return PlanLeg(
        operator=refs[0][0] if refs else None,
        refs=tuple(refs),
        dated_ref=alternatives._as_dict(raw.get("datedServiceJourney")).get("id"),
        line=line.get("id"),
        public_code=line.get("publicCode"),
        mode=raw.get("mode"),
        board_stop_place=alternatives._stop_place_ref(raw.get("fromPlace")),
        alight_stop_place=alternatives._stop_place_ref(raw.get("toPlace")),
        board_quay=_quay(raw.get("fromPlace")),
        alight_quay=_quay(raw.get("toPlace")),
        board_name=alternatives._as_dict(raw.get("fromPlace")).get("name"),
        alight_name=alternatives._as_dict(raw.get("toPlace")).get("name"),
        board_position=alternatives._as_dict(raw.get("fromEstimatedCall")).get(
            "stopPositionInPattern"
        ),
        alight_position=alternatives._as_dict(raw.get("toEstimatedCall")).get(
            "stopPositionInPattern"
        ),
        aimed_departure=departure,
        aimed_arrival=arrival,
        service_date=raw.get("serviceDate"),
        cancelled=alternatives._leg_is_cancelled(raw),
        walk_after_s=walk_after_s,
    )


def _parse_pattern(pattern: Any, from_places: frozenset[str]) -> PlanTrip | DirectDeparture | None:
    """One `tripPattern` as a trip, a direct departure, or None when it is neither (walk-only, or
    boarding outside `from_places`). Raises on a malformed pattern: a leg without aimed times, a
    ridden leg without a journey ref, stop place or service date.

    Stricter than `alternatives._to_candidate`, which degrades a broken field to null: a plan leg is
    an identity the verdict resolves calls on, so one that cannot be read fails its whole page (see
    :func:`fetch_range`).
    """
    # (raw leg, departure, arrival, seconds walked after it) per ridden leg, in travel order.
    ridden: list[list[Any]] = []
    for raw in pattern["legs"]:
        departure = alternatives._parse_ts(raw["aimedStartTime"])
        arrival = alternatives._parse_ts(raw["aimedEndTime"])
        if departure is None or arrival is None:
            raise ValueError("a leg without aimed times")
        if raw.get("mode") != "foot":
            ridden.append([raw, departure, arrival, 0])
        elif ridden:  # a walk after a ridden leg; one before the first leg is no transfer
            ridden[-1][3] += round((arrival - departure).total_seconds())
    if ridden:
        ridden[-1][3] = 0  # an egress walk after the last leg is no transfer either
    legs = [_leg(*entry) for entry in ridden]
    if not legs or legs[0].board_stop_place not in from_places:
        return None
    if len(legs) > 1:
        return PlanTrip(legs=tuple(legs))
    leg = legs[0]
    return DirectDeparture(
        operator=leg.operator,
        refs=leg.refs,
        board_stop_place=leg.board_stop_place,
        aimed_departure=leg.aimed_departure,
        aimed_arrival=leg.aimed_arrival,
    )


def fetch_range(
    from_stop: str,
    to_stop: str,
    start: datetime,
    end: datetime,
    *,
    from_places: frozenset[str],
    path: alternatives.FetchPath = "plans",
) -> FetchResult:
    """The pair's trips with a change and its direct departures whose first ridden leg leaves the
    from-place in `[start, end]` (aware datetimes, Oslo).

    `from_places` is every stop-place ref of the from-place: JP boards in child stop places, and a
    pattern boarding anywhere else is dropped. `path` is the fetch budget spent, one request per
    page: `"plans"` for the leader's phase, `"plans_pull"` for a public read's miss (KTD18).

    Pages until JP's next window starts past `end`, there is no further page, or the page bound
    (:func:`_page_bound`) is reached (`partial`). A budget refusal, a timeout, a GraphQL `errors`
    envelope, an unreadable reply or a malformed pattern ends the fetch: `failed` on the first
    page, `partial` after, and the failing page contributes nothing.

    A malformed pattern fails its page rather than costing itself only, because only `complete` is
    merged (KTD1) and a merge marks every stored trip it does not contain `dropped`: a JP change
    that broke every leg would otherwise read as a pair with no trips, silently. A walk-only
    pattern, or one boarding outside `from_places`, is an ordinary drop, not a malformed one.
    """
    started = perf_counter()
    trips: dict[PlanTrip, None] = {}  # insertion-ordered sets: a boundary repeat is kept once
    directs: dict[DirectDeparture, None] = {}
    pages = 0
    result: FetchResultKind = "complete"
    reason = error = None
    cursor = None
    bound = _page_bound(start, end)
    if not (alternatives._is_stop_ref(from_stop) and alternatives._is_stop_ref(to_stop)):
        # Both are spliced into the document; see `alternatives.fetch_candidates` for the guard.
        result, reason = "failed", "bad_stop_ref"
    while result == "complete":
        if pages == bound:
            result, reason = "partial", "page_bound"
            break
        if not alternatives._reserve_fetch(path, datetime.now(UTC)):
            result = "partial" if pages else "failed"
            reason = "rate_limited"
            break
        try:
            body = alternatives._post_json(
                alternatives._JP_URL,
                {"query": _render(from_stop, to_stop, start, cursor)},
                headers={"ET-Client-Name": alternatives.ET_CLIENT_NAME},
            )
            trip = body["data"]["trip"]
            patterns = trip["tripPatterns"]
            if not isinstance(patterns, list):
                raise TypeError("tripPatterns is not a list")
        except Exception as exc:  # noqa: BLE001 - fail open: every failure is a result, logged below
            # The class name only: `_post_json`'s message quotes JP's `errors`, which can echo the
            # document, and the document holds the stop pair.
            result = "partial" if pages else "failed"
            reason, error = "fetch_error", type(exc).__name__
            break
        try:
            parsed = [_parse_pattern(pattern, from_places) for pattern in patterns]
        except Exception as exc:  # noqa: BLE001 - fails the page, logged below (see docstring)
            result = "partial" if pages else "failed"
            reason, error = "malformed", type(exc).__name__
            break
        pages += 1
        for record in parsed:
            if record is not None and start <= record.aimed_departure <= end:
                (trips if isinstance(record, PlanTrip) else directs)[record] = None
        cursor = trip.get("nextPageCursor")
        if not cursor:
            break
        # Where JP's next page would start searching; without metadata, the last pattern's start.
        resume = alternatives._as_dict(trip.get("metadata")).get("nextDateTime") or (
            alternatives._as_dict(patterns[-1]).get("aimedStartTime") if patterns else None
        )
        resume = alternatives._parse_ts(resume)
        if resume is not None and resume > end:
            break
    log = logger.info if result == "complete" else logger.warning
    log(
        "Journey Planner trip plan fetch (%s)",
        result,
        extra={
            "fields": {
                "event": "trip_plans_fetch",
                "pair_digest": alternatives.pair_digest(from_stop, to_stop),
                "path": path,
                "result": result,
                "reason": reason,
                "error": error,
                "pages": pages,
                "trips": len(trips),
                "directs": len(directs),
                "ms": round((perf_counter() - started) * 1000),
            }
        },
    )
    return FetchResult(result=result, trips=tuple(trips), directs=tuple(directs), pages=pages)


# ---- The day plans: merge, memory, the leader's phase and every pod's follow (U2) ---------------

#: A covered range is refreshed when its fetch is this old (KTD1): a few fetches a day per pair
#: and date, so a timetable change published during the day reaches the plan within three hours.
PLAN_REFRESH_S = 3 * 3600
#: The phase stops starting fetches after this long; what is left stays due for the next cycle.
#: A third of the cycle's measured >= 60 s of headroom per period (CLAUDE.md, 2026-09-24), and
#: about a third of the `plans` path's 60 pages a minute even at `_HTTP_TIMEOUT_S` (3 s) a page,
#: so a JP outage costs this plus at most one fetch: each range then fails on its first page.
#: `ponytail:` checked between fetches, never inside one, so the overshoot is one fetch (at most
#: `_MAX_PAGES` pages for a day-long range, ~6 for a 1-2 h window). Upgrade path: a deadline
#: argument to `fetch_range`, if a phase reading ever shows the overshoot.
_PHASE_DEADLINE_S = 20

# (from_stop, to_stop, Oslo date) -> the pair's plan for the day. Written by the poller thread (the
# phase, the follow) and the lifespan's follow; read by request threads through `read_plan`.
_lock = threading.Lock()
_plans: dict[tuple[str, str, date], DayPlan] = {}
# Per key, the stored JSON the last follow parsed and the plan it parsed to: a sync re-reads its
# overlap every tick, and an unchanged document then keeps its plan object, so `read_plan`'s merge
# and `trips.situation_texts`, both keyed on identity, stay warm. The follow's alone, under `_lock`.
_followed: dict[tuple[str, str, date], tuple[str, DayPlan]] = {}
# Per key, `read_plan`'s merge of the leader's plan and a public read's, with the two it merged:
# valid while both are still the ones held (both stores replace a plan, never mutate one). Under
# `_lock`; dropped by date with the plans (`_prune`) and with the public cache's eviction.
_merged: dict[tuple[str, str, date], tuple[DayPlan, DayPlan, DayPlan]] = {}
# The store (`notification_store`, injected by main under the store backend, KTD2) and the sync's
# watermark: opaque here, the newest stamp the last seed or sync read. None in tst.
_store: Any | None = None
_watermark: Any | None = None


def configure(*, store: Any | None) -> None:
    """Set the store the plans are shared through (None = this pod's memory only, tst)."""
    global _store, _watermark
    _store, _watermark = store, None


def day_plan(from_stop: str, to_stop: str, oslo_date: date) -> DayPlan | None:
    """The leader's plan for the pair and Oslo date, from memory -- the stored plans alone, which the
    due gate and the write read; requests read :func:`read_plan`. Never the store or the network.
    None: no `complete` fetch yet."""
    with _lock:
        return _plans.get((from_stop, to_stop, oslo_date))


def held() -> list[DayPlan]:
    """Every plan in this pod's memory: what main names the `KIX:Trip:` situations' texts for
    (U3, `trips.situation_texts`)."""
    with _lock:
        return list(_plans.values())


def read_plan(from_stop: str, to_stop: str, oslo_date: date) -> DayPlan | None:
    """The reader `varsel` is configured with (U8): the leader's plan for the pair and date, then
    what this pod fetched for a public read that missed -- both, when both hold one, so a read whose
    range no registration covers still sees the part its own fetch added. :func:`day_plan` stays
    the leader's alone, which is what its due gate and its write read: a public fetch must never
    make a registered pair's range look covered, or the leader would not fetch it for the store."""
    key = (from_stop, to_stop, oslo_date)
    with _lock:
        stored = _plans.get(key)
        public = _public.get(key)
        if public is not None:
            _public.move_to_end(key)
        known = _merged.get(key)
    if public is None or stored is None:
        return stored or public
    if known is not None and known[0] is stored and known[1] is public:
        return known[2]
    merged = DayPlan(
        # Every trip and direct either holds, the leader's version winning (its `dropped` marks).
        trips=tuple(sorted(
            ({_trip_key(t): t for t in public.trips} | {_trip_key(t): t for t in stored.trips}).values(),
            key=lambda trip: trip.aimed_departure,
        )),
        directs=tuple(sorted(
            ({_direct_key(d): d for d in public.directs}
             | {_direct_key(d): d for d in stored.directs}).values(),
            key=lambda direct: direct.aimed_departure,
        )),
        covered=tuple(sorted((*stored.covered, *public.covered), key=lambda span: span.start)),
    )
    with _lock:
        _merged[key] = (stored, public, merged)
    return merged


def _trip_key(trip: PlanTrip) -> tuple:
    """A trip's identity across fetches: each leg's first ref, boarding stop place and aimed
    departure. Per leg, so KTD19's siblings -- one first train, two continuations -- stay two."""
    return tuple((leg.refs[0], leg.board_stop_place, leg.aimed_departure) for leg in trip.legs)


def _direct_key(direct: DirectDeparture) -> tuple:
    return direct.refs[0], direct.board_stop_place, direct.aimed_departure


def merge(
    stored: DayPlan | None, fetched: FetchResult, start: datetime, end: datetime,
    fetched_at: datetime,
) -> DayPlan:
    """A `complete` fetch of `[start, end]` merged into the stored plan (KTD2, KTD11). Pure: the
    store's transaction body calls it, and a body may run more than once.

    Every stored trip stays. One the fetch returned takes the fetch's fields, keeping a `dropped`
    mark it had (marks are sticky). One it did not return is marked `dropped` exactly when its first
    leg departs inside `[start, end]`, the range `fetch_range` keeps; outside it the fetch says
    nothing. Directs are unioned, the fetch's version winning. The covered ranges lose the part
    the fetch covered, which becomes one range at `fetched_at`, so they stay disjoint and bounded.
    """
    stored = stored or DayPlan()
    returned = {_trip_key(trip): trip for trip in fetched.trips}
    merged: dict[tuple, PlanTrip] = {}
    for trip in stored.trips:
        key = _trip_key(trip)
        new = returned.pop(key, None)
        if new is not None:
            merged[key] = new.model_copy(update={"dropped": True}) if trip.dropped else new
        elif start <= trip.aimed_departure <= end and not trip.dropped:
            merged[key] = trip.model_copy(update={"dropped": True})
        else:
            merged[key] = trip
    merged.update(returned)
    directs = {_direct_key(direct): direct for direct in stored.directs}
    directs.update((_direct_key(direct), direct) for direct in fetched.directs)
    covered: list[CoveredRange] = []
    for span in stored.covered:
        if span.end < start or span.start > end:
            covered.append(span)
            continue
        if span.start < start:
            covered.append(span.model_copy(update={"end": start}))
        if span.end > end:
            covered.append(span.model_copy(update={"start": end}))
    covered.append(CoveredRange(start=start, end=end, fetched_at=fetched_at))
    return DayPlan(
        trips=tuple(sorted(merged.values(), key=lambda trip: trip.aimed_departure)),
        directs=tuple(sorted(directs.values(), key=lambda direct: direct.aimed_departure)),
        covered=tuple(sorted(covered, key=lambda span: span.start)),
    )


def fetch_ranges(
    windows: Iterable[tuple[datetime, datetime]],
) -> list[tuple[date, datetime, datetime]]:
    """Boarding windows (`varsel.journey_windows`, UTC) as (Oslo date, start, end) fetch ranges,
    soonest first. A window belongs to the Oslo date it BEGINS on, so an overnight one is one range
    across midnight under that date, and that date keys both the plan and its due gate (memory
    `oslo-utc-gate-and-latch`). Overlapping or touching windows on one date are one range."""
    by_day: dict[date, list[tuple[datetime, datetime]]] = {}
    for start, end in windows:
        start, end = start.astimezone(OSLO), end.astimezone(OSLO)
        by_day.setdefault(start.date(), []).append((start, end))
    ranges: list[tuple[date, datetime, datetime]] = []
    for day, spans in by_day.items():
        spans.sort()
        merged = [list(spans[0])]
        for start, end in spans[1:]:
            if start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        ranges.extend((day, start, end) for start, end in merged)
    return sorted(ranges, key=lambda entry: entry[1])


def _due(plan: DayPlan | None, start: datetime, end: datetime, now: datetime) -> bool:
    """Outside the covered ranges, or covered only by a fetch at least `PLAN_REFRESH_S` old."""
    if plan is None:
        return True
    fresh = tuple(
        span for span in plan.covered
        if (now - span.fetched_at).total_seconds() < PLAN_REFRESH_S
    )
    return not DayPlan(covered=fresh).covers(start, end)


def _prune(now: datetime) -> None:
    """Forget plans for Oslo dates before yesterday, where the store's TTL (`expires_at`, the date
    plus two days) takes the documents; yesterday's stays for an overnight window."""
    oldest = now.astimezone(OSLO).date() - timedelta(days=1)
    with _lock:
        for key in [key for key in _plans if key[2] < oldest]:
            del _plans[key]
            _followed.pop(key, None)
        for key in [key for key in _merged if key[2] < oldest]:
            del _merged[key]


def _write(
    from_stop: str, to_stop: str, day: date, fetched: FetchResult, start: datetime,
    end: datetime, now: datetime,
) -> None:
    """Merge a `complete` fetch through the store's transaction and hold the STORED result; on a
    store failure, merge into memory instead (KTD2: never a reason to fail a tick)."""

    def apply(stored: str | None) -> str:
        # An unreadable stored plan raises, failing the write: replacing what cannot be read would
        # be exactly the replace KTD2 forbids. Memory still gets the local merge below.
        base = DayPlan.model_validate_json(stored) if stored is not None else None
        return merge(base, fetched, start, end, now).model_dump_json()

    plan = None
    if _store is not None:
        try:
            plan = DayPlan.model_validate_json(
                _store.merge_trip_plan(from_stop, to_stop, day, apply, now=now)
            )
        except Exception as exc:  # noqa: BLE001 - memory below; the next refresh writes again
            logger.warning(
                "Could not store a trip plan (%s); holding it in this pod's memory",
                type(exc).__name__,
                extra={"fields": {"event": "trip_plans_store", "result": "store_error",
                                  "error": type(exc).__name__,
                                  "pair_digest": alternatives.pair_digest(from_stop, to_stop)}},
            )
    if plan is None:
        plan = merge(day_plan(from_stop, to_stop, day), fetched, start, end, now)
    with _lock:
        _plans[(from_stop, to_stop, day)] = plan
        _followed.pop((from_stop, to_stop, day), None)  # what it held is not the plan any more


def refresh(
    windows: Mapping[tuple[str, str], Iterable[tuple[datetime, datetime]]],
    *,
    places: Callable[[str], Iterable[str]],
    now: datetime,
    beat: Callable[[], None] | None = None,
) -> dict[str, int]:
    """The leader's `trip_plans` phase: fetch what is due, soonest first, under the deadline.

    `windows` maps each canonical pair of a REGISTERED definition (both channels; main builds it,
    never from a public read) to its definitions' `journey_windows`. Each (pair, Oslo date)'s
    windows become fetch ranges (:func:`fetch_ranges`); a range is due when it is outside the
    plan's covered ranges or its covering fetch is `PLAN_REFRESH_S` old. `places` is
    `varsel.place_refs`, passed in so this module does not import `varsel`.

    Only a `complete` fetch is written (KTD1): a `partial` or `failed` one changes nothing, and
    the range stays due for the next cycle, as does every range the deadline did not reach.
    Raises only what `fetch_range` and the merge never do; the phase's own guard is main's.
    """
    started = monotonic()
    _prune(now)
    due = [
        (start, pair, day, end)
        for pair, spans in windows.items()
        for day, start, end in fetch_ranges(spans)
        if _due(day_plan(*pair, day), start, end, now)
    ]
    due.sort(key=lambda entry: entry[0])
    fetched = complete = 0
    for start, (from_stop, to_stop), day, end in due:
        if monotonic() - started >= _PHASE_DEADLINE_S:
            break
        result = fetch_range(
            from_stop, to_stop, start, end, from_places=frozenset(places(from_stop))
        )
        fetched += 1
        if result.result == "complete":
            _write(from_stop, to_stop, day, result, start, end, now)
            complete += 1
        if beat is not None:
            beat()
    counts = {"due": len(due), "fetched": fetched, "complete": complete,
              "left_due": len(due) - fetched}
    logger.info(
        "Trip plans: %d range(s) due, %d fetched, %d complete",
        len(due), fetched, complete,
        extra={"fields": {"event": "trip_plans_refresh", **counts,
                          "ms": round((monotonic() - started) * 1000)}},
    )
    return counts


def follow(*, beat: Callable[[], None] | None = None) -> None:
    """Read the store's plans into this pod's memory: a seed the first time, a watermark sync
    after. Called in the lifespan and on every follower tick (`main.follow_last_build`) and on
    taking the lease, as `varsel.follow_card_caches` is. Never raises: a store that cannot be read
    leaves memory as it was, and the next call tries again from the same watermark. With the flag
    off it reads nothing, so no pod touches the collection (AE6). An unreadable document is
    skipped and counted."""
    global _watermark
    store = _store
    if store is None or not trips.enabled():
        return
    # A follower never runs `refresh`, the other caller, so without this it would keep every
    # day's plans for as long as the pod lives.
    _prune(datetime.now(UTC))
    seed = _watermark is None
    try:
        if seed:
            entries, newest = store.seed_trip_plans(beat=beat)
        else:
            entries, newest = store.sync_trip_plans(since=_watermark, beat=beat)
    except Exception as exc:  # noqa: BLE001 - never a reason to fail a tick, the lifespan or an acquisition
        logger.warning(
            "Could not read the stored trip plans (%s); serving this pod's own",
            type(exc).__name__,
            extra={"fields": {"event": "trip_plans_follow", "result": "follow_error",
                              "error": type(exc).__name__}},
        )
        return
    plans: dict[tuple[str, str, date], DayPlan] = {}
    raws: dict[tuple[str, str, date], str] = {}
    unreadable = 0
    for entry in entries:
        key = (entry.from_stop, entry.to_stop, entry.oslo_date)
        known = _followed.get(key)
        try:
            plans[key] = (
                known[1] if known is not None and known[0] == entry.plan
                else DayPlan.model_validate_json(entry.plan)
            )
        except ValueError:
            unreadable += 1
        else:
            raws[key] = entry.plan
    with _lock:
        _plans.update(plans)
        _followed.update({key: (raws[key], plans[key]) for key in plans})
    if newest is not None:
        _watermark = newest
    if seed or plans or unreadable:
        logger.info(
            "Trip plans followed",
            extra={"fields": {"event": "trip_plans_follow", "result": "seeded" if seed else "synced",
                              "applied": len(plans), "unreadable": unreadable}},
        )


# ---- Plans fetched for a public read that missed (U8, KTD1, KTD18) -----------------------------

#: The most (pair, Oslo date) plans one pod keeps for public reads, least recently read dropped
#: first. Never written to the store and never refreshed by the leader (KTD1): a pair only a reader
#: wants is that pod's own. The bound is memory -- a plan is a few tens of KB -- and an evicted plan
#: costs its next read a fetch, never a wrong answer.
PUBLIC_PLANS_MAX = 1_000
#: Read-miss fetches in flight on one pod, one per (pair, Oslo date); past it a miss starts nothing
#: and a later read asks again. Each spends the `plans_pull` path's pages (20 a minute), so more at
#: once would only queue on that ceiling.
PUBLIC_FLIGHTS_MAX = 4

# Both under `_lock`. Written by the flights, read by request threads through `read_plan` and by
# the real-time refresh through `public_plans`.
_public: OrderedDict[tuple[str, str, date], DayPlan] = OrderedDict()
_public_flights: set[tuple[str, str, date]] = set()


def fetch_for_read(
    from_stop: str,
    to_stop: str,
    ranges: Iterable[tuple[date, datetime, datetime]],
    *,
    places: Callable[[str], Iterable[str]],
) -> list[threading.Thread]:
    """The read-miss trigger `main` injects into `notification_pull` (KTD1, KTD4): for the ranges no
    plan covers, one background flight per (pair, Oslo date), and back at once -- so a read never
    waits on it beyond the `wait` it already has, and no enrichment worker is held. A flight pages
    on the `plans_pull` path (KTD18) and keeps only a `complete` fetch, merged into this pod's
    public-read cache (:func:`merge`, so a second range of the date unions in); a `partial` or
    `failed` one leaves the read at `not_evaluated` and the next read asks again. `places` is
    `varsel.place_refs`, as :func:`refresh` takes it. The started threads are returned for a test
    to join. With the flag off nothing starts (AE6)."""
    if not trips.enabled():
        return []
    by_day: dict[date, list[tuple[datetime, datetime]]] = {}
    for day, start, end in ranges:
        by_day.setdefault(day, []).append((start, end))
    started = []
    for day, spans in by_day.items():
        key = (from_stop, to_stop, day)
        with _lock:
            if key in _public_flights or len(_public_flights) >= PUBLIC_FLIGHTS_MAX:
                continue
            _public_flights.add(key)
        # A daemon thread per flight rather than a pool: at most PUBLIC_FLIGHTS_MAX exist, and a
        # shutdown has nothing to wait for -- a lost flight costs a fetch on the next pod's read.
        flight = threading.Thread(
            target=_fly, args=(key, spans, frozenset(places(from_stop))), name="plans-pull",
            daemon=True,
        )
        flight.start()
        started.append(flight)
    return started


def _fly(key: tuple[str, str, date], spans: list[tuple[datetime, datetime]],
         from_places: frozenset[str]) -> None:
    from_stop, to_stop, _day = key
    try:
        for start, end in spans:
            fetched = fetch_range(from_stop, to_stop, start, end, from_places=from_places,
                                  path="plans_pull")
            if fetched.result != "complete":
                continue
            with _lock:
                base = _public.get(key)
            # Outside the lock: one flight per key, so nothing else writes this entry meanwhile.
            plan = merge(base, fetched, start, end, datetime.now(UTC))
            with _lock:
                _public[key] = plan
                _public.move_to_end(key)
                while len(_public) > PUBLIC_PLANS_MAX:
                    evicted, _plan = _public.popitem(last=False)
                    _merged.pop(evicted, None)
    except Exception as exc:  # noqa: BLE001 - a daemon thread's raise would go to stderr unlogged
        # The class name only, as `fetch_range` logs a failure: a message could quote the pair.
        logger.warning(
            "A trip plan fetch for a public read failed (%s)",
            type(exc).__name__,
            extra={"fields": {"event": "trip_plans_fetch", "path": "plans_pull", "result": "failed",
                              "reason": "flight_error", "error": type(exc).__name__,
                              "pair_digest": alternatives.pair_digest(from_stop, to_stop)}},
        )
    finally:
        with _lock:
            _public_flights.discard(key)


def public_plans() -> list[DayPlan]:
    """Every plan this pod fetched for a public read: `refresh_realtime(public=...)`'s second group,
    asked after the registered pairs' (KTD8)."""
    with _lock:
        return list(_public.values())


def reset_public() -> None:
    """Forget the public-read cache and its flights. Test-only."""
    with _lock:
        _public.clear()
        _public_flights.clear()
        _merged.clear()


# ---- Journey Planner real-time for unscored legs (U4, KTD8) ----------------------------------

#: How far ahead of its first departure a trip's unscored legs are read: JP has real-time for buses
#: only near departure, and an alert goes out only before the first leg departs (R10).
REALTIME_LOOKAHEAD_S = 2 * 3600
#: An answer older than this is no answer: about two leader cycles at a 120-185 s period, so one
#: failed refresh does not blank a leg and a second does (KTD8).
REALTIME_MAX_AGE_S = 360
#: Legs a request, and requests a tick: at most 30 legs, ~70 KB a request (each leg answers its
#: whole journey). Three a tick sits inside the `realtime` path's 20 a minute even at a follower's
#: 60 s tick, and bounds a JP outage to three `_HTTP_TIMEOUT_S` waits.
_REALTIME_BATCH = 10
_REALTIME_MAX_REQUESTS = 3

# One alias of the batched document, the verified selection (module docstring).
_REALTIME_LEG = """  l%(index)d: datedServiceJourney(id: %(id)s) {
    id operatingDay
    estimatedCalls { stopPositionInPattern realtime cancellation aimedArrivalTime expectedArrivalTime aimedDepartureTime expectedDepartureTime quay { stopPlace { id } } }
  }
"""


@dataclass(frozen=True)
class _Call:
    """One call of a JP real-time answer, as the reader matches it."""

    stop_place: str | None
    aimed_departure: datetime | None
    aimed_arrival: datetime | None
    realtime: bool
    cancelled: bool
    expected_departure: datetime | None
    expected_arrival: datetime | None


# `dated_ref` -> (when it was fetched, its journey's calls): this pod's own answers, never shared
# (KTD8). Written by the poller thread, read by request threads through `realtime_for`, under
# `_lock`; `realtime_for` takes it, so nothing that reads a leg's state may run while holding it.
_answers: dict[str, tuple[datetime, tuple[_Call, ...]]] = {}
# When `refresh_realtime` last ran with the flag on (monotonic), for the leader's tick to skip one
# its cycle already made. Poller thread only.
_refreshed_at: float | None = None


def realtime_refreshed_within(seconds: float) -> bool:
    """Did :func:`refresh_realtime` run, flag on, less than `seconds` ago?"""
    return _refreshed_at is not None and monotonic() - _refreshed_at < seconds


def _render_realtime(refs: Iterable[str]) -> str:
    # JSON's string escaping is GraphQL's, so a ref cannot close the literal it sits in (`_render`).
    return "{\n" + "".join(
        _REALTIME_LEG % {"index": i, "id": json.dumps(ref)} for i, ref in enumerate(refs)
    ) + "}\n"


def _calls_of(answer: dict) -> tuple[_Call, ...]:
    """A journey's calls; a broken field degrades to null or false, which the reader reads as no
    answer, never as on time."""
    calls = []
    for raw in alternatives._as_dict(answer).get("estimatedCalls") or ():
        call = alternatives._as_dict(raw)
        stop = alternatives._as_dict(alternatives._as_dict(call.get("quay")).get("stopPlace"))
        calls.append(_Call(
            stop_place=stop.get("id"),
            aimed_departure=alternatives._parse_ts(call.get("aimedDepartureTime")),
            aimed_arrival=alternatives._parse_ts(call.get("aimedArrivalTime")),
            realtime=call.get("realtime") is True,
            cancelled=call.get("cancellation") is True,
            expected_departure=alternatives._parse_ts(call.get("expectedDepartureTime")),
            expected_arrival=alternatives._parse_ts(call.get("expectedArrivalTime")),
        ))
    return tuple(calls)


def _wants_realtime(leg: PlanLeg) -> bool:
    """Is an end of this leg one our figures do not judge this cycle? `trips.LegState.unscored`, our
    figures alone, the real-time reader not asked: not a train out of the plan, and not scored at
    both its calls. So a leg scored at its boarding only is asked too: its alighting end is unknown
    until JP answers."""
    return trips.leg_state(leg, with_realtime=False).unscored


def _realtime_refs(groups: Iterable[Iterable[DayPlan]], now: datetime) -> list[str]:
    """The journeys to ask about, in priority order, each once: the unscored legs of watched trips
    (R3) that have not arrived and whose first leg leaves within `REALTIME_LOOKAHEAD_S`, a group at
    a time (registered pairs' plans first), soonest trip first within a group -- a trip already
    under way after every one still to leave, since only those can start an alert (R10)."""
    horizon = now + timedelta(seconds=REALTIME_LOOKAHEAD_S)
    refs: dict[str, None] = {}  # an insertion-ordered set
    for plans in groups:
        due = sorted(
            (
                (trip.aimed_departure, leg)
                for plan in plans for trip in plan.trips
                if trips.watched(trip) and trip.aimed_arrival > now
                and trip.aimed_departure <= horizon
                for leg in trip.legs
            ),
            key=lambda entry: (entry[0] < now, entry[0]),
        )
        for _departure, leg in due:
            if leg.dated_ref and leg.dated_ref not in refs and _wants_realtime(leg):
                refs[leg.dated_ref] = None
    return list(refs)


def refresh_realtime(now: datetime, *, public: Iterable[DayPlan] = ()) -> dict[str, int]:
    """Ask JP for the real-time state of the legs our figures do not score (KTD8), this tick.

    The leader runs this in its cycle (phase `trip_realtime`) and on a tick that did not rebuild
    (:func:`realtime_refreshed_within` tells the two apart), a follower on its tick: each pod holds
    its own answers. The legs are :func:`_realtime_refs`' of this pod's held plans (registered
    pairs, `held()`), then of `public` (the plans public reads fetched, :func:`public_plans`, which
    every caller in main passes), so with the bound reached a registered pair's legs are asked and
    a public read's wait. At most `_REALTIME_MAX_REQUESTS`
    requests of `_REALTIME_BATCH` legs, each on the `realtime` fetch path (KTD18).

    An answered request replaces each of its legs' answers, a null one included (JP has no such
    journey). A refused request ends the tick and a failed one costs only its own legs, which keep
    the answers they had: an older answer ages out on its own, and answers past
    `REALTIME_MAX_AGE_S` are dropped first. With the flag off nothing is read or fetched (AE6).

    Logs one `event="trip_realtime_fetch"` record with counts, never a ref, stop or time (R17).
    Raises only what a call-lookup read does; the callers' guards are main's.
    """
    global _refreshed_at
    if not trips.enabled():
        return {}
    _refreshed_at = monotonic()
    started = perf_counter()
    stale = now - timedelta(seconds=REALTIME_MAX_AGE_S)
    with _lock:
        for ref in [ref for ref, (fetched_at, _calls) in _answers.items() if fetched_at < stale]:
            del _answers[ref]
    wanted = _realtime_refs((held(), public), now)
    asked = answered = requests = failed = refused = 0
    error = None
    for start in range(0, min(len(wanted), _REALTIME_BATCH * _REALTIME_MAX_REQUESTS),
                       _REALTIME_BATCH):
        batch = wanted[start:start + _REALTIME_BATCH]
        if not alternatives._reserve_fetch("realtime", now):
            refused = 1
            break
        requests += 1
        asked += len(batch)
        try:
            body = alternatives._post_json(
                alternatives._JP_URL,
                {"query": _render_realtime(batch)},
                headers={"ET-Client-Name": alternatives.ET_CLIENT_NAME},
            )
        except Exception as exc:  # noqa: BLE001 - its legs keep their answers; logged below
            # The class name only, as `fetch_range` logs it: JP's `errors` can echo the document.
            failed += 1
            error = type(exc).__name__
            continue
        data = alternatives._as_dict(body.get("data"))
        with _lock:
            for i, ref in enumerate(batch):
                answer = data.get(f"l{i}")
                if isinstance(answer, dict):
                    _answers[ref] = (now, _calls_of(answer))
                    answered += 1
                else:
                    _answers.pop(ref, None)
    if not wanted:
        result = "none"
    elif requests == failed:
        result = "failed"
    elif failed or refused or asked < len(wanted):
        result = "partial"
    else:
        result = "complete"
    with _lock:
        held_now = len(_answers)
    counts = {"wanted": len(wanted), "asked": asked, "answered": answered, "requests": requests,
              "failed": failed, "refused": refused}
    log = logger.warning if failed or refused else logger.info
    log(
        "Trip real-time: %d leg(s) wanted, %d asked, %d answered",
        len(wanted), asked, answered,
        extra={"fields": {"event": "trip_realtime_fetch", "result": result, "error": error,
                          **counts, "held": held_now,
                          "ms": round((perf_counter() - started) * 1000)}},
    )
    return counts


def realtime_for(leg: PlanLeg, *, now: datetime | None = None) -> trips.LegRealtime | None:
    """The reader main injects into `trips.configure(realtime=...)`: JP's real-time for one leg,
    from this pod's answers, or None, which is unknown and never on time (KTD8).

    The leg's boarding call is its journey's call at its boarding stop place on its aimed departure,
    the alighting call likewise on its aimed arrival. None when the journey has no answer, the
    answer is more than `REALTIME_MAX_AGE_S` old (at `now`, the wall clock by default), either call
    is missing from it, or either reads `realtime: false`. JP's `cancellation: true` on either call
    sets the matching flag.
    """
    if leg.dated_ref is None:
        return None
    with _lock:
        held_answer = _answers.get(leg.dated_ref)
    if held_answer is None:
        return None
    fetched_at, calls = held_answer
    if ((now or datetime.now(UTC)) - fetched_at).total_seconds() > REALTIME_MAX_AGE_S:
        return None
    board = next((call for call in calls if call.stop_place == leg.board_stop_place
                  and call.aimed_departure == leg.aimed_departure), None)
    alight = next((call for call in calls if call.stop_place == leg.alight_stop_place
                   and call.aimed_arrival == leg.aimed_arrival), None)
    if board is None or alight is None or not (board.realtime and alight.realtime):
        return None
    return trips.LegRealtime(
        board_cancelled=board.cancelled,
        alight_cancelled=alight.cancelled,
        expected_departure=board.expected_departure,
        expected_arrival=alight.expected_arrival,
    )
