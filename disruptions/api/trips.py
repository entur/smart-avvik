"""Trips with a change: the records a Journey Planner plan is parsed into.

A registered pair is served direct trains by `impact._trip_cells`, as before. What the Journey
Planner adds is the departures that reach the destination only with a change (KTD1, KTD3): each is
a :class:`PlanTrip`, its legs in travel order. The plan's direct patterns are kept as
:class:`DirectDeparture` records, never as trips, because KTD20 compares a trip against waiting
for the next direct train.

Every record is a frozen pydantic model, so a plan can be dumped to JSON (`model_dump(mode="json")`)
for the shared store and read back (`model_validate`) as an equal, hashable value.

The verdict lives here too (U3, :func:`judge`): each first-leg departure with a change is judged
end to end, its legs resolved against the prediction cache and the Journey Planner's real-time
data, both read through readers `main` injects (:func:`configure`). `impact.for_trip(plans=...)`
returns the verdicts beside the direct trains, never inside their per-situation selection.

This module imports nothing from `impact` or `trip_plans` (KTD4): `impact`, and through it
`journey_status`, consume these types, while `trip_plans` (the fetch) is imported by `main` only.
Calls are read by attribute, as `impact.CallPred` spells them, through the injected lookup.
"""

import functools
import itertools
import logging
import math
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import alert_policy
import notification_wording
from alternatives import (
    TripFacts,
    TripLeg,
    TripLegSource,
    TripProblem,
    TripReasonKind,
    TripTransfer,
)
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


def enabled() -> bool:
    """`TRIP_PLANS_ENABLED`, the multi-leg journeys' one flag (KTD17), read per call.

    An allow-list parse with unset = off, `main._notification_webhooks_enabled`'s convention: a
    typo is off, and the code default is the closed gate. Here rather than in `main` so `varsel`,
    `impact` (and through it `journey_status`) and `main` read one definition; the tuple is
    written out rather than imported as `llm_message._TRUTHY`, so the flag's meaning does not
    hang on another module's private name.
    """
    return os.environ.get("TRIP_PLANS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


# `(operator, jkey)` exactly as the prediction cache keys a call (`alternatives.cache_key`, so an
# SJ Norge ref is already rewritten to the ET key).
CacheRef = tuple[str | None, str]


class PlanLeg(BaseModel):
    """One ridden vehicle journey of a trip, from boarding to alighting, on planned times."""

    model_config = ConfigDict(frozen=True)

    # The codespace of the first ref, e.g. "VYG", "RUT", "SJN".
    operator: str | None
    # The dated ref first, then the plain one: rail's cache keys on `DatedServiceJourney:…`, bus's
    # on `ServiceJourney:<hash>`, and JP gives every leg both. Never empty.
    refs: tuple[CacheRef, ...] = Field(min_length=1)
    # JP's own `datedServiceJourney.id`, whole (`RUT:DatedServiceJourney:djj-…`): what U4's
    # real-time read asks JP by. Not rebuildable from `refs`, which `cache_key` rewrites for SJN.
    # None on a plan stored before it was kept: such a leg is never fetched and stays unknown.
    dated_ref: str | None = None
    line: str | None
    public_code: str | None
    mode: str
    board_stop_place: str
    alight_stop_place: str
    board_quay: str | None
    alight_quay: str | None
    # JP's own stop names. Our rows name a leg we score; a leg the cache does not hold (a bus no
    # message names) has no other source. None on a plan stored before they were kept.
    board_name: str | None = None
    alight_name: str | None = None
    # JP's `stopPositionInPattern`, as JP sends it: 0 at the journey's origin. ET's `stop_order` is
    # this + 1 for VYG (verified 2026-10-27, `trip_plans` docstring), unverified for buses, so a
    # call is resolved on stop place, aimed time and service date, never on position alone.
    board_position: int | None
    alight_position: int | None
    aimed_departure: datetime
    aimed_arrival: datetime
    # The day the journey runs on. A bus `ServiceJourney` ref carries no date, so this is what
    # tells tomorrow's leg from today's.
    service_date: date
    # JP's call-level flag on either end. Reads false for a real-time cancellation: the plan is
    # fetched with `ignoreRealtimeUpdates`. Only a planned (NeTEx) cancellation could set it.
    cancelled: bool = False
    # Seconds on foot between this leg's alighting and the next leg's boarding; 0 when the change
    # is made without walking, and on the last leg.
    walk_after_s: int = 0


class PlanTrip(BaseModel):
    """A departure from the from-stop that reaches the destination with at least one change."""

    model_config = ConfigDict(frozen=True)

    legs: tuple[PlanLeg, ...] = Field(min_length=2)
    # Set when a later complete fetch no longer returns the trip; it is still judged until its
    # first leg departs (KTD11).
    dropped: bool = False

    @property
    def aimed_departure(self) -> datetime:
        return self.legs[0].aimed_departure

    @property
    def aimed_arrival(self) -> datetime:
        return self.legs[-1].aimed_arrival


class DirectDeparture(BaseModel):
    """A plan pattern with one ridden leg: what KTD20 compares a trip against."""

    model_config = ConfigDict(frozen=True)

    operator: str | None
    refs: tuple[CacheRef, ...] = Field(min_length=1)
    board_stop_place: str
    aimed_departure: datetime
    # At the leg's alighting stop, the same anchor `PlanTrip.aimed_arrival` uses, so the two
    # compare like for like.
    aimed_arrival: datetime


class CoveredRange(BaseModel):
    """A fetch range a `complete` fetch covered, and when (aware datetimes)."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    fetched_at: datetime


class DayPlan(BaseModel):
    """One canonical pair's plan for one Oslo date: every trip any `complete` fetch returned (never
    removed, KTD11), the direct departures, and the ranges those fetches covered (KTD2)."""

    model_config = ConfigDict(frozen=True)

    trips: tuple[PlanTrip, ...] = ()
    directs: tuple[DirectDeparture, ...] = ()
    covered: tuple[CoveredRange, ...] = ()

    def covers(self, start: datetime, end: datetime) -> bool:
        """Does `[start, end]` lie inside the union of the covered ranges? A window outside it is
        a miss: the plan cannot say which trips run there (KTD2)."""
        reach = start
        for span in sorted(self.covered, key=lambda span: span.start):
            if span.start > reach:
                return False
            reach = max(reach, span.end)
            if reach >= end:
                return True
        return False


# ---- The verdict: one departure with a change, judged end to end (U3) ---------------------------
#
# A trip is judged on its first-leg departure from the from-stop (R5, KTD5). Its legs are resolved
# against the prediction cache through the injected call lookup, and where the cache does not score
# a leg, through the injected Journey Planner real-time reader (KTD8). Every reason enters the one
# gate, `alert_policy.gate`, as a figure (KTD6), and continuations of the same first train are
# composed per KTD19. Calls are read by attribute only (`impact.CallPred`'s), never imported.

#: Whose trains we score on every call (R3): Vy and SJ Norge, `journey_status.OPERATORS` restated
#: (journey_status imports impact, which imports this module). A trip is watched only with one of
#: their legs; one without is outside coverage, reported as not evaluated and never as calm.
WATCHED_OPERATORS = frozenset({"VYG", "SJN"})
#: KTD7's correction over :func:`_read_off`: logit P(miss) = B + A * logit(read-off) + FLAGGED *
#: has_situation + SLACK * slack. Fit 2026-10-28 by data/queries/transfer_miss_backtest.py on 3.4 M
#: asks of historical Vy-to-Vy changes (Apr 1 - Oct 12 2026, leak-free arrival heads), whose header
#: carries the method and the numbers. Refit with that script after the arrival heads are retrained.
_MISS_B, _MISS_A, _MISS_FLAGGED, _MISS_SLACK = -0.2143, 0.8932, 0.1962, -0.0684

LegStateName = Literal["scored", "realtime", "unknown"]

OSLO = ZoneInfo("Europe/Oslo")


@dataclass(frozen=True)
class LegRealtime:
    """The Journey Planner's real-time answer for one leg (KTD8), as the injected reader hands it
    over. The reader answers None for a leg it has no usable answer for -- absent, `realtime: false`
    (JP then copies the aimed times) or too old -- and None is unknown, never on time."""

    board_cancelled: bool = False
    alight_cancelled: bool = False
    expected_departure: datetime | None = None
    expected_arrival: datetime | None = None


def _no_realtime(leg: PlanLeg) -> LegRealtime | None:
    """The unwired reader: every leg is unknown. main injects `trip_plans.realtime_for` (U4)."""
    return None


def _own_place(ref: str) -> tuple[str, ...]:
    return (ref,)


# Injected by main (`configure`). The defaults are what an unwired suite sees: no calls, no real-time
# answer, and a stop that is its own place.
_call_lookup: Callable[[str | None, str], Iterable[Any] | None] | None = None
_realtime: Callable[[PlanLeg], LegRealtime | None] = _no_realtime
_places: Callable[[str], Iterable[str]] = _own_place
_warned_unwired = False


def configure(
    *,
    call_lookup: Callable[[str | None, str], Iterable[Any] | None],
    realtime: Callable[[PlanLeg], LegRealtime | None] | None = None,
    places: Callable[[str], Iterable[str]] | None = None,
) -> None:
    """Wire in main's readers. Called from main.lifespan beside `impact.configure`.

    `call_lookup` is the one `impact.configure(call_lookup=...)` takes: `(operator, jkey)` -> that
    journey's scored calls, or None. `realtime` is the per-leg Journey Planner reader (KTD8),
    `trip_plans.realtime_for` in main; left None, every leg it would answer is unknown. `places` is
    `varsel.place_refs` (every ref of a stop's place), which KTD3's check needs for the to-place and
    which this module may not import; left None, a stop is its own place.
    """
    global _call_lookup, _realtime, _places, _warned_unwired
    _call_lookup = call_lookup
    _realtime = realtime or _no_realtime
    _places = places or _own_place
    _warned_unwired = False


def _calls(operator: str | None, jkey: str) -> list[Any]:
    """One journey's scored calls, or `[]`; an unwired lookup says so once, as `impact`'s does,
    because "nobody called configure" must not read like "no situation touches this train"."""
    global _warned_unwired
    if _call_lookup is None:
        if not _warned_unwired:
            _warned_unwired = True
            logger.warning(
                "trips.configure(call_lookup=...) was never called; every trip leg is unknown",
                extra={"fields": {"event": "trips_unwired"}},
            )
        return []
    return list(_call_lookup(operator, jkey) or ())


def _to_places(to_stop: str) -> frozenset[str]:
    return frozenset((to_stop, *_places(to_stop)))


# ---- Identity (KTD5) ------------------------------------------------------------------------------


def leg_ref(leg: PlanLeg) -> CacheRef:
    """The leg's journey in the prediction cache's key form, from the plan alone (KTD5): the dated
    ref for rail, the plain `ServiceJourney:` ref for every other mode, as `PlanLeg.refs` says the
    two caches key. Pinned in the plan, so a first leg keeps one identity whether or not a call of
    it is scored this cycle."""
    if leg.mode != "rail":
        plain = next((ref for ref in leg.refs if ref[1].startswith("ServiceJourney:")), None)
        if plain is not None:
            return plain
    return leg.refs[0]


def subject_key(trip: PlanTrip) -> tuple[str | None, str, datetime]:
    """KTD3's source-independent departure key: (operator, journey, aimed departure at the
    from-stop), the first leg's boarding call. One subject per key (R5)."""
    operator, jkey = leg_ref(trip.legs[0])
    return operator, jkey, trip.aimed_departure


def trip_situation(leg: PlanLeg) -> str:
    """The synthetic situation a Journey Planner reason on this leg attaches to (U3 step 5), dated
    by the leg's service date so the same bus on another day is another situation."""
    return notification_wording.trip_situation(*leg_ref(leg), leg.service_date)


# id(plan) -> (plan, its legs' situations in walk order), for the plans of the last
# `situation_texts` call. A plan is replaced, never mutated, so its identity is its key, and holding
# it keeps its id from being reused while it is cached. Rebuilt on every call, so it holds the last
# call's plans only.
_plan_situations: dict[int, tuple[DayPlan, tuple[str, ...]]] = {}


def situation_texts(plans: Iterable[DayPlan]) -> dict[str, tuple[str, ...]]:
    """Every leg's `KIX:Trip:` situation in `plans`, each with the fixed texts, in the shape
    `situation_english.update` takes: what main feeds it beside the prediction rows' texts. A plan
    the previous call was also given is not walked again (main asks twice a cycle)."""
    global _plan_situations
    known = _plan_situations
    current: dict[int, tuple[DayPlan, tuple[str, ...]]] = {}
    for plan in plans:
        current[id(plan)] = known.get(id(plan)) or (
            plan, tuple(trip_situation(leg) for trip in plan.trips for leg in trip.legs)
        )
    _plan_situations = current
    return dict.fromkeys(
        (situation for _plan, situations in current.values() for situation in situations),
        notification_wording.TRIP_SITUATION_TEXTS,
    )


def watched(trip: PlanTrip) -> bool:
    """R3: at least one leg is a train of an operator we score on every call."""
    return any(leg.operator in WATCHED_OPERATORS for leg in trip.legs)


#: A read's `coverage.transfers` (KTD13): did the answer judge the trips with a change?
TransferCoverage = Literal["none", "not_evaluated", "evaluated"]


def transfer_coverage(
    windows: Iterable[tuple[DayPlan | None, datetime, datetime]],
) -> tuple[TransferCoverage, bool]:
    """`coverage.transfers` over a read's windows, each with the plan its matcher reads (None: no
    plan) and the range the read needs it for; and whether a plan is MISSING -- none, or not covering
    a range -- the one `not_evaluated` a fetch can still turn into an answer, so the read says
    `complete: false` until it lands (KTD1). The one statement of the rule, for journey-alerts here
    and journey-status (U9), which reaches it through `impact`.

    `not_evaluated` with the flag off (R16), with no plan or a range outside its covered ranges
    (KTD2), and when a range's trips with a change are none of them watched (R3); `none` when no
    range holds a trip with a change; else `evaluated`. Conservative across ranges: one
    `not_evaluated` makes the answer's, so a pair served only with a change is never reported calm
    (AE5).
    """
    if not enabled():
        return "not_evaluated", False
    found: set[TransferCoverage] = set()
    for plan, start, end in windows:
        if plan is None or not plan.covers(start, end):
            return "not_evaluated", True
        here = [trip for trip in plan.trips if start <= trip.aimed_departure <= end]
        if here:
            found.add("evaluated" if any(watched(trip) for trip in here) else "not_evaluated")
    if "not_evaluated" in found:
        return "not_evaluated", False
    return ("evaluated" if found else "none"), False


def _boarding(leg: PlanLeg) -> tuple:
    """The first train's boarding, which every continuation of it shares (KTD19)."""
    return leg.refs[0], leg.board_stop_place, leg.aimed_departure


# ---- Leg resolution (U3 step 1, KTD8) ------------------------------------------------------------

# A removed train's number as `rail_calls_removed_today.sql` reads it off a GTFS trip id, which JP's
# refs carry too: Vy's `…ServiceJourney:1606_…` (dated or plain), SJ Norge's ET key `49:2026-10-15`.
_VY_TRAIN = re.compile(r"ServiceJourney:(\d+)[-_]")
_SJN_TRAIN = re.compile(r"(\d+):\d{4}-\d{2}-\d{2}")


def _removed_ref(operator: str | None, refs: Iterable[CacheRef], day: date) -> CacheRef | None:
    """Where the prediction cache holds this train if Vy or SJ took it out of `day`'s plan: under
    the `RemovedJourney:<train number>_<day>` key the relay builds, never under the leg's own ref
    (a removed train is not in ET at all)."""
    if operator not in WATCHED_OPERATORS:
        return None
    for _operator, jkey in refs:
        found = _VY_TRAIN.search(jkey) or _SJN_TRAIN.fullmatch(jkey)
        if found:
            return operator, f"RemovedJourney:{found[1]}_{day.isoformat()}"
    return None


def removed_ref(leg: PlanLeg) -> CacheRef | None:
    """Where this leg's train would be held if it were taken out of the day's plan: the relay's
    key, which is also the spine's for its `rail_calls_removed_today` rows (journey-status, U9)."""
    return _removed_ref(leg.operator, leg.refs, leg.service_date)


@dataclass(frozen=True)
class LegState:
    """What this cycle knows about one leg: KTD8's three states, decided per leg per cycle.

    `board` and `alight` are the calls' rows in the prediction cache, one per situation that scored
    the call; `removed` the leg's `KIX:PlanRemoved:` rows; `realtime` the Journey Planner's answer.
    Our figures take precedence wherever a row exists.
    """

    board: tuple = ()
    alight: tuple = ()
    removed: tuple = ()
    realtime: LegRealtime | None = None

    @property
    def state(self) -> LegStateName:
        if self.board or self.alight or self.removed:
            return "scored"
        return "realtime" if self.realtime is not None else "unknown"

    @property
    def unscored(self) -> bool:
        """An end of this leg our figures do not judge this cycle: not a train out of the plan, and
        not scored at both its calls. What `trip_plans` asks JP about (KTD8)."""
        return not self.removed and not (self.board and self.alight)

    @property
    def unknown(self) -> bool:
        """An end of this leg that neither our figures nor JP's real-time can judge this cycle."""
        return self.unscored and self.realtime is None

    @property
    def data_source(self) -> TripLegSource:
        """What the leg is judged from, as `TripLeg.data_source` publishes it: our figures where
        they judge both ends (or the train is out of the plan), else JP's real-time, else nothing
        -- exactly when :attr:`unknown`."""
        if self.removed or (self.board and self.alight):
            return "prediction"
        return "jp_realtime" if self.realtime is not None else "none"


def _resolve(
    operator: str | None,
    refs: Iterable[CacheRef],
    board_stop: str,
    aimed_dep: datetime,
    alight_stop: str | None,
    aimed_arr: datetime,
    day: date,
) -> tuple[tuple, tuple, tuple]:
    """A leg's (boarding rows, alighting rows, removed rows).

    A call is the leg's when its stop place AND its aimed time match the plan's. The aimed instant
    carries the date, which is what keeps an undated bus `ServiceJourney` ref on tomorrow's
    `serviceDate` off today's calls (the plan's risk table); `CallPred` has no date of its own to
    compare a service date with. Both stop places are NSR's own: ET's is its quay's `stopPlaceRef`
    (`rail_calls_planned_today.sql`), JP's its quay's `stopPlace`. `alight_stop` None matches the
    alighting call on its aimed time alone (a plan's direct pattern keeps no alighting stop).
    """
    board: list[Any] = []
    alight: list[Any] = []
    for ref_operator, jkey in refs:
        for call in _calls(ref_operator, jkey):
            if notification_wording.is_plan_removed(call.situation_number):
                continue  # a removed train's rows live under its own key, read below
            if call.aimed_dep == aimed_dep and call.stop_place == board_stop:
                board.append(call)
            if call.aimed_arr == aimed_arr and alight_stop in (None, call.stop_place):
                alight.append(call)
    removed: list[Any] = []
    removed_key = _removed_ref(operator, refs, day)
    if removed_key is not None:
        # On the stop alone: the key already names the train and the day, and the relay's calls
        # are its last run's, moved by whole days, so an aimed time is not what identifies them.
        removed = [
            call for call in _calls(*removed_key)
            if notification_wording.is_plan_removed(call.situation_number)
            and call.stop_place in (board_stop, alight_stop)
        ]
    return tuple(board), tuple(alight), tuple(removed)


def leg_state(leg: PlanLeg, *, with_realtime: bool = True) -> LegState:
    """Resolve one leg for this cycle (U3 step 1). `with_realtime=False` leaves the real-time
    reader unasked (`realtime` None): our figures alone, as `trip_plans` reads a leg before JP
    answers."""
    board, alight, removed = _resolve(
        leg.operator, leg.refs, leg.board_stop_place, leg.aimed_departure,
        leg.alight_stop_place, leg.aimed_arrival, leg.service_date,
    )
    return LegState(board, alight, removed, _realtime(leg) if with_realtime else None)


# ---- Reasons (KTD6, KTD7, KTD8) ------------------------------------------------------------------


@dataclass(frozen=True)
class Reason:
    """One reason a departure with a change may not get the traveller there, already gated (KTD6):
    `alert_policy.gate` at the deciding call's operator and `has_situation`, and attached to that
    call's situation (U3 step 5) -- or, with no call behind it, to the leg's `KIX:Trip:` one."""

    kind: TripReasonKind  # the vocabulary (KTD6), published with the trip shape (KTD13)
    index: int  # the leg index, or the transfer index for `missed_connection`
    probability: float
    threshold: float
    alertworthy: bool
    confirmed: bool  # a declared cancellation, or a train out of today's plan
    situation_number: str
    summary: str | None
    description: str | None
    has_situation: bool
    report_type: str | None
    call: Any | None = None  # the row behind the figure; None when JP's real-time is

    @property
    def margin(self) -> float:
        return self.probability - self.threshold


def _from_call(
    kind: TripReasonKind, index: int, probability: float, call: Any, *, confirmed: bool = False
) -> Reason:
    threshold, alertworthy = alert_policy.gate(
        probability, call.operator, has_situation=call.has_situation
    )
    return Reason(
        kind, index, probability, threshold, alertworthy, confirmed, call.situation_number,
        call.summary, call.description, call.has_situation, call.report_type, call,
    )


def _from_jp(
    kind: TripReasonKind, index: int, probability: float, leg: PlanLeg, *, confirmed: bool = False
) -> Reason:
    # No SX message behind it, so the no-SX bar (KTD6) and the synthetic situation's texts.
    threshold, alertworthy = alert_policy.gate(probability, leg.operator, has_situation=False)
    summary, description = notification_wording.TRIP_SITUATION_TEXTS[:2]
    return Reason(
        kind, index, probability, threshold, alertworthy, confirmed, trip_situation(leg),
        summary, description, False, None,
    )


def _strongest(reasons: Iterable[Reason]) -> Reason | None:
    """Clearing first, then by how far over (or under) its own bar. The situation number breaks a
    tie, so a call scored by several situations attaches to the same one every cycle."""
    return max(reasons, key=lambda r: (r.alertworthy, r.margin, r.situation_number), default=None)


def _best(reasons: Iterable[Reason]) -> list[Reason]:
    best = _strongest(reasons)
    return [best] if best is not None else []


def _declared(i: int, rows: Iterable[Any]) -> list[Reason]:
    """A declared cancellation among one call of leg `i`'s rows: 1.0, confirmed; `[]` without one."""
    return _best(
        _from_call("leg_cancelled", i, 1.0, call, confirmed=True)
        for call in rows if call.et_declared_cancelled
    )


def _board_reasons(i: int, leg: PlanLeg, st: LegState) -> list[Reason]:
    """The boarding call of leg `i`: a declaration (1.0, confirmed), else its departure figure --
    `departure` for the first leg, `leg_departure` for a later one. From JP only a cancellation:
    the table has no JP figure for a departure."""
    if st.board:
        declared = _declared(i, st.board)
        if declared:
            return declared
        kind: TripReasonKind = "departure" if i == 0 else "leg_departure"
        return _best(
            _from_call(kind, i, call.p_impact_dep, call)
            for call in st.board if call.p_impact_dep is not None
        )
    if leg.cancelled or (st.realtime is not None and st.realtime.board_cancelled):
        return [_from_jp("leg_cancelled", i, 1.0, leg, confirmed=True)]
    return []


def _alight_reasons(i: int, leg: PlanLeg, st: LegState) -> list[Reason]:
    """The alighting call of leg `i`: declarations only, as `impact._destination_cancelled` reads
    one -- its `p_cancel` is a forecast with no calibration as an alerting score."""
    declared = _declared(i, st.alight)
    if declared:
        return declared
    if not st.alight and st.realtime is not None and st.realtime.alight_cancelled:
        return [_from_jp("leg_cancelled", i, 1.0, leg, confirmed=True)]
    return []


def _removed_reasons(i: int, st: LegState) -> list[Reason]:
    """Leg `i` rides a train out of today's plan: the relay's certainty, on its `KIX:PlanRemoved:`
    situation, which keeps it out of per-train pushes and events downstream (KTD6)."""
    return _best(_from_call("leg_removed", i, 1.0, call, confirmed=True) for call in st.removed)


def _late_by(call: Any) -> int:
    """KTD7's shift for a scored connecting call: the largest of 5, 10 and 20 min its departure is
    at least even odds to exceed -- a lower bound on its median delay that never reads the stack's
    0-5 min segment. 0 when none is."""
    for minutes, p in ((20, call.p_dep_ge20), (10, call.p_dep_ge10), (5, call.p_dep_ge5)):
        if p is not None and p >= 0.5:
            return minutes
    return 0


def p_miss(call: Any, slack_min: float) -> float | None:
    """KTD7: P(the change is missed), :func:`_read_off` put through the correction fitted on
    historical changes (`_MISS_*`). The read-off alone is overconfident where it alerts: on those
    changes a read-off of 0.65-0.85 was missed about half the time. None when the read-off is None:
    the correction never makes a figure for a call with no point.

    `ponytail:` the two legs' delays are still treated as independent; the correction removes their
    average effect only (the connecting train's own late departure saved 44 % of the arrivals that
    would have missed a punctual one). Upgrade path: a joint figure per corridor, if the measured
    precision on `/historikk`'s «med bytte» segment asks for one.
    """
    p = _read_off(call, slack_min)
    if p is None:
        return None
    p = min(max(p, 1e-4), 1 - 1e-4)
    z = (
        _MISS_B + _MISS_A * math.log(p / (1 - p))
        + _MISS_FLAGGED * bool(call.has_situation) + _MISS_SLACK * slack_min
    )
    return 1 / (1 + math.exp(-z))


def _read_off(call: Any, slack_min: float) -> float | None:
    """P(the arriving call is at least `slack_min` late), read off the stack's own survival points
    `p_arr_ge5`, `p_arr_ge10` and `p_arr_ge20`, linear between them and held flat beyond the last.
    Below the first point it is held flat too, which understates the miss. The 0-5 min segment, a
    straight line from certainty, is never read; it is what put a 3-minute floor under the served
    median. A missing point is skipped, so without `p_arr_ge5` the curve starts at 10. None with no
    point at all.
    """
    points = [
        (minutes, p)
        for minutes, p in ((5, call.p_arr_ge5), (10, call.p_arr_ge10), (20, call.p_arr_ge20))
        if p is not None
    ]
    if not points:
        return None
    for (m0, p0), (m1, p1) in itertools.pairwise(points):
        if slack_min < m1:
            return p0 if slack_min <= m0 else p0 + (p1 - p0) * (slack_min - m0) / (m1 - m0)
    return points[-1][1]


def connection_slack_min(
    arrival: datetime, walk_s: float, departure: datetime, connecting: Iterable[Any]
) -> float:
    """KTD7's slack at a change, in minutes: the next leg's aimed `departure` minus the arriving
    leg's aimed `arrival` and the walk, plus the shift of the connecting call's rows (`connecting`,
    empty when it is unscored). JP's minimum change (`transferSlack`, 120 s) stays in as margin, as
    in KTD8: taking it off too read every change two minutes tighter than the trains ran. The one
    definition: the verdict's transfer reason and the alternative lookup's check on a candidate
    with a change (multi-leg U6) both read it."""
    slack = ((departure - arrival).total_seconds() - walk_s) / 60
    return slack + max((_late_by(call) for call in connecting), default=0)


def _transfer_reasons(
    i: int, arriving: PlanLeg, a_st: LegState, connecting: PlanLeg, c_st: LegState
) -> list[Reason]:
    """Transfer `i`, between legs `i` and `i + 1`: P(miss) from our figures (KTD7), else 1.0 or 0.0
    by JP's expected times (KTD8), else nothing.

    KTD7's slack is the next aimed departure minus the aimed arrival and the walk, plus the
    connecting call's shift when it is scored. KTD8's miss is the expected arrival plus the walk
    passing the next aimed departure. Both leave JP's 120 s minimum change in as margin: JP builds
    trips at it, and a few seconds of delay would otherwise fire a certainty.
    """
    if a_st.alight:
        if any(call.et_declared_cancelled for call in a_st.alight):
            return []  # the arriving call itself is gone, which `leg_cancelled` already says
        slack = connection_slack_min(
            arriving.aimed_arrival, arriving.walk_after_s, connecting.aimed_departure, c_st.board
        )
        figures = [(call, p_miss(call, slack)) for call in a_st.alight]
        return _best(
            _from_call("missed_connection", i, p, call) for call, p in figures if p is not None
        )
    realtime = a_st.realtime
    if realtime is not None and realtime.expected_arrival is not None:
        at = realtime.expected_arrival + timedelta(seconds=arriving.walk_after_s)
        return [_from_jp("missed_connection", i, float(at > connecting.aimed_departure), arriving)]
    return []


def _shared(first: PlanLeg, st: LegState) -> list[Reason]:
    """The reasons every continuation of the first train shares, never cushioned (KTD19): its
    departure figure, or a declaration on its boarding call, or the whole train out of the plan."""
    return _removed_reasons(0, st) if st.removed else _board_reasons(0, first, st)


def _continuation(trip: PlanTrip, state: Callable[[PlanLeg], LegState]) -> list[Reason]:
    """The reasons that belong to one continuation (KTD19): the first train's alighting call, each
    change, and the later legs' calls at both ends. Only the endpoints of ridden legs count, and
    only declarations of them (KTD6): a cancelled call in the middle of a leg is no reason."""
    reasons: list[Reason] = []
    for i, leg in enumerate(trip.legs):
        st = state(leg)
        if st.removed:
            if i > 0:  # the first train out of the plan is a shared reason
                reasons += _removed_reasons(i, st)
            continue
        if i > 0:
            reasons += _board_reasons(i, leg, st)
        reasons += _alight_reasons(i, leg, st)
        if i + 1 < len(trip.legs):
            following = trip.legs[i + 1]
            reasons += _transfer_reasons(i, leg, st, following, state(following))
    return reasons


# ---- The verdict (KTD19, KTD9, KTD20) ------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """One first-leg departure with a change, judged end to end.

    `trip` is the planned continuation, the earliest-arriving one of this first train, which the
    alert describes (KTD9); `continuations` every one of them, planned first. `deciding` is the
    figure and bar the verdict was taken on, what the decision row and the severity rank record
    (KTD6, KTD19): the shared reason when it clears, else the one that binds. `reason` is what the
    subject attaches to: the problem's reason when it alerts (the planned route's, KTD13), else
    `deciding`. Both are None only when no reason has a figure at all -- no observation, which
    :func:`judge` leaves out unless asked for it (journey-status lists such a departure, grey).
    `first_cancelled` says the first train does not leave at all, as a fact (a confirmed shared
    reason), which the alert words as the first train's cancellation, never as a missed change.
    `admitted` is KTD20's answer, which `varsel` overrides for a trip with a standing alert;
    False means waiting for the next direct train is at least as fast, so the trip is only an
    alternative. `complete` is False while any leg of a cushion-set continuation is unknown (reads
    then say `complete: false`, KTD8); `unknown_legs` are the planned route's unknown legs, by index,
    which `varsel` reads to keep a subject standing on one of them from withdrawing
    (`Alert._hold_withdrawal`). `leg_states` are the planned route's legs' states the verdict was
    taken on, in leg order, for journey-status to grade the row on the same reading.
    """

    trip: PlanTrip
    continuations: tuple[PlanTrip, ...]
    board: tuple  # the first leg's boarding-call rows; empty when it is unscored
    reason: Reason | None
    deciding: Reason | None
    alertworthy: bool
    confirmed: bool
    first_cancelled: bool
    admitted: bool
    complete: bool
    unknown_legs: tuple[int, ...]
    facts: TripFacts
    leg_states: tuple[LegState, ...] = field(compare=False, repr=False)
    # The planned route's KTD7 P(miss), the highest over its changes whatever reason decided, for the
    # decision log (U11); None where no change of it is scored by us -- a JP-expected miss (KTD8) is
    # a verdict, not a figure, and would read as a calibrated 1.0.
    p_miss: float | None = None

    @property
    def key(self) -> tuple[str | None, str, datetime]:
        return subject_key(self.trip)


def legs_snapshot(continuations: Iterable[PlanTrip]) -> dict:
    """The decision log's `trip_legs` (U11, KTD16), JSON-ready: every continuation the verdict was
    taken over, the planned one first and marked, each leg by what the nightly labeller resolves
    its realized calls on -- the cache's key form (`leg_ref`, as the row's own `jkey`), the two stop
    places, the aimed times and the walk. Never stop positions: JP's are unverified for buses."""
    return {"continuations": [
        {"planned": i == 0, "legs": [
            {
                "operator": operator, "jkey": jkey,
                "board_stop_place": leg.board_stop_place, "alight_stop_place": leg.alight_stop_place,
                "aimed_departure": leg.aimed_departure.isoformat(),
                "aimed_arrival": leg.aimed_arrival.isoformat(),
                "walk_after_s": leg.walk_after_s,
            }
            for leg in trip.legs for operator, jkey in (leg_ref(leg),)
        ]}
        for i, trip in enumerate(continuations)
    ]}


def direct_for_pair(
    trip: PlanTrip, plan: DayPlan, to_stop: str, *, to_places: frozenset[str] | None = None
) -> bool:
    """KTD3: is the trip's first train itself direct for the pair? Then the traveller can stay on
    board (AE7), the direct verdict decides that departure, and the trip is never judged -- whether
    or not the direct train survives `_trip_cells`' per-situation selection. U5 and U9 call this
    too, so the matcher and the strip drop the same patterns.

    Direct when the plan holds it as a direct pattern, or when its own calls in the prediction cache
    pass `impact._trip_cells`' eligibility, restated on calls: board at the first leg's stop at its
    aimed departure with boarding allowed, and a later call (higher `stop_order`) at any ref of the
    to-place with an aimed arrival and alighting allowed. The cache holds a journey's rows across
    every situation, which is `_trip_cells`' own fallback to another situation's alighting call.
    `to_places` is `to_stop`'s places when the caller already holds them (:func:`judge`, once).
    """
    first = trip.legs[0]
    if any(
        direct.board_stop_place == first.board_stop_place
        and direct.aimed_departure == first.aimed_departure
        and set(direct.refs) & set(first.refs)
        for direct in plan.directs
    ):
        return True
    if to_places is None:
        to_places = _to_places(to_stop)
    for operator, jkey in first.refs:
        calls = _calls(operator, jkey)
        for board in calls:
            if not (
                board.stop_place == first.board_stop_place
                and board.aimed_dep == first.aimed_departure
                and board.boarding_allowed
            ):
                continue
            if any(
                call.stop_place in to_places
                and call.aimed_arr is not None
                and call.alighting_allowed
                and call.stop_order > board.stop_order
                for call in calls
            ):
                return True
    return False


def _counts(direct: DirectDeparture) -> bool:
    """KTD20: a direct departure counts unless the prediction cache, read now and never the plan,
    has its boarding or alighting call declared cancelled or its train out of today's plan."""
    board, alight, removed = _resolve(
        direct.operator, direct.refs, direct.board_stop_place, direct.aimed_departure, None,
        direct.aimed_arrival, direct.aimed_departure.astimezone(OSLO).date(),
    )
    return not removed and not any(call.et_declared_cancelled for call in (*board, *alight))


def _admitted(
    planned: PlanTrip,
    plan: DayPlan,
    *,
    window_has_direct: bool,
    counts: Callable[[DirectDeparture], bool],
) -> bool:
    """KTD20: on a window with direct departures, a trip with a change is a subject only when its
    planned arrival beats the earliest planned arrival of a counting direct departure leaving at or
    after it. With no such departure in the plan, it is a subject. `counts` is :func:`_counts`,
    memoized per :func:`judge`, so a direct is resolved once however many subjects ask."""
    if not window_has_direct:
        return True
    later = [
        direct.aimed_arrival for direct in plan.directs
        if direct.aimed_departure >= planned.aimed_departure and counts(direct)
    ]
    return not later or planned.aimed_arrival < min(later)


def typical_delay_min(p50: int | None, typ: int | None) -> int:
    """A call's predicted typical delay in whole minutes, never negative: the stack's median, else
    the regression's figure, else 0. The one rule `impact`'s expected times read too."""
    return max((p50 if p50 is not None else typ) or 0, 0)


def _expected_departure(leg: PlanLeg, st: LegState) -> datetime:
    """When the first train is expected to leave: `impact._expected_departure`'s rule (aimed plus
    the predicted typical delay, the latest any row says), JP's expected time for an unscored leg,
    else the aimed time."""
    if st.board:
        return max(
            leg.aimed_departure + timedelta(
                minutes=typical_delay_min(call.dep_delay_p50_stack_min, call.dep_delay_typ_min)
            )
            for call in st.board
        )
    if st.realtime is not None and st.realtime.expected_departure is not None:
        return st.realtime.expected_departure
    return leg.aimed_departure


def _expected_arrival(trip: PlanTrip, st: LegState) -> datetime:
    """A continuation's arrival at the destination: its last call's aimed arrival plus our
    predicted typical delay (`impact._predicted_arrival`'s rule), JP's expected time where only JP
    answers, else the plan's aimed time. `st` is the last leg's."""
    last = trip.legs[-1]
    if st.alight:
        delay = max(
            typical_delay_min(c.arr_delay_p50_stack_min, c.arr_delay_typ_min) for c in st.alight
        )
        return last.aimed_arrival + timedelta(minutes=delay)
    if st.realtime is not None and st.realtime.expected_arrival is not None:
        return st.realtime.expected_arrival
    return last.aimed_arrival


def _name(rows: tuple) -> str | None:
    return next((call.stop_name for call in rows if call.stop_name), None)


def _facts(
    trip: PlanTrip,
    state: Callable[[PlanLeg], LegState],
    expected_arrival: datetime | None,
    problem: TripProblem | None,
    arrives_via: str | None = None,
) -> TripFacts:
    """KTD13's trip shape for the planned route."""
    legs: list[TripLeg] = []
    transfers: list[TripTransfer] = []
    for i, leg in enumerate(trip.legs):
        st = state(leg)
        realtime = st.realtime
        legs.append(TripLeg(
            mode=leg.mode,
            line=leg.line,
            public_code=leg.public_code,
            from_name=_name(st.board) or leg.board_name,
            to_name=_name(st.alight) or leg.alight_name,
            # `AlternativeLeg`'s two are JP's expected times; a plan carries none of its own.
            start_time=realtime.expected_departure if realtime is not None else None,
            end_time=realtime.expected_arrival if realtime is not None else None,
            board_stop_place=leg.board_stop_place,
            alight_stop_place=leg.alight_stop_place,
            aimed_departure=leg.aimed_departure,
            aimed_arrival=leg.aimed_arrival,
            cancelled=(
                any(call.et_declared_cancelled for call in (*st.board, *st.alight))
                or leg.cancelled
                or (realtime is not None and (realtime.board_cancelled or realtime.alight_cancelled))
            ),
            data_source=st.data_source,
        ))
        if i + 1 < len(trip.legs):
            following = trip.legs[i + 1]
            planned = (following.aimed_departure - leg.aimed_arrival).total_seconds()
            transfers.append(TripTransfer(
                stop_place=leg.alight_stop_place,
                stop_name=_name(st.alight) or leg.alight_name,
                walk_min=round(leg.walk_after_s / 60),
                slack_min=round((planned - leg.walk_after_s) / 60),
            ))
    return TripFacts(
        legs=legs, transfers=transfers, expected_arrival=expected_arrival,
        arrives_via=arrives_via, problem=problem,
    )


def _leaves_at(planned: PlanTrip, other: PlanTrip, state: Callable[[PlanLeg], LegState]) -> str | None:
    """Where `other`, a continuation of the planned route's first train, leaves the planned route:
    the stop it stays on to when it rides the same vehicle on past the planned change (AE11's
    «via C»), or the stop its own vehicle boards at when it takes another one (a later connection
    at the same change). Its name off our rows there, else its NSR ref."""
    for mine, theirs in zip(planned.legs, other.legs):
        if mine == theirs:
            continue
        if _boarding(mine) == _boarding(theirs):
            return _name(state(theirs).alight) or theirs.alight_stop_place
        return _name(state(theirs).board) or theirs.board_stop_place
    return None


def _verdict(
    continuations: list[PlanTrip],
    state: Callable[[PlanLeg], LegState],
    *,
    admitted: bool,
) -> Verdict:
    """KTD19's composition over the continuations of one first train, planned first. `deciding`
    None when no reason has a figure the verdict can be taken on: no observation, so nothing to
    alert on and nothing that counts toward a withdrawal (the plan's verdict diagram)."""
    planned = continuations[0]
    first = planned.legs[0]
    first_state = state(first)
    shared = _strongest(_shared(first, first_state))
    shared_clears = shared is not None and shared.alertworthy
    # A shared reason is confirmed only as the boarding call's declared cancellation or the whole
    # train out of the plan (`_shared`): the first train does not leave at all.
    first_cancelled = shared_clears and shared.confirmed
    per = [(trip, _continuation(trip, state)) for trip in continuations]
    in_trouble = [any(reason.alertworthy for reason in reasons) for _trip, reasons in per]
    # A continuation holds when it is not in trouble. A sibling must also be judged (KTD8): one JP
    # no longer returns, or with a leg of its own nothing can judge, is no reading that it gets
    # there, never an on-time one. The planned route's own unknown legs stay KTD8's: no reason.
    own_legs = set(planned.legs)
    holds = [
        not trouble and (trip is planned or not (
            trip.dropped or any(state(leg).unknown for leg in trip.legs if leg not in own_legs)
        ))
        for (trip, _reasons), trouble in zip(per, in_trouble, strict=True)
    ]
    # The cushion set: continuations arriving less than the one definition of trouble after the
    # planned arrival. The planned one is always in it, so with no sibling this is KTD6 unchanged.
    limit = planned.aimed_arrival + timedelta(minutes=alert_policy.TROUBLE_DELAY_MIN)
    cushion = [
        (trip, reasons, trouble, held)
        for (trip, reasons), trouble, held in zip(per, in_trouble, holds, strict=True)
        if trip.aimed_arrival < limit
    ]
    # The planned route holds exactly when it is not in trouble, so this alerts only on its trouble.
    alertworthy = shared_clears or not any(held for *_rest, held in cushion)
    confirmed = first_cancelled or all(
        any(reason.alertworthy and reason.confirmed for reason in reasons)
        for _trip, reasons, _trouble, _held in cushion
    )
    # The binding figure: the shared reason when it clears; else whichever is nearer its bar, the
    # shared reason or the cushion continuation whose strongest reason is weakest -- the number
    # that, below its bar, stops the alert (above it, would start one). A continuation with no
    # figure at all binds nothing, so then only the shared reason can. A sibling neither in trouble
    # nor holding can neither stop the alert nor start one, so it binds nothing either.
    strongest = [
        _strongest(reasons) for _trip, reasons, trouble, held in cushion if trouble or held
    ]
    binding = (
        None if any(reason is None for reason in strongest)
        else min(strongest, key=lambda reason: reason.margin)
    )
    if shared_clears:
        deciding = shared
    else:
        deciding = max(
            (reason for reason in (shared, binding) if reason is not None),
            key=lambda reason: reason.margin,
            default=None,
        )
    problem_reason = None
    if alertworthy:
        # What breaks the ride the traveller planned (KTD13): the shared reason, or the planned
        # continuation's strongest clearing one -- it is in trouble, or nothing would alert.
        problem_reason = shared if shared_clears else _strongest(cushion[0][1])
    # KTD9: the planned route's arrival while it holds, else the earliest continuation of the same
    # first train that holds, else unknown. Never invented, and none at all when the first train
    # itself is not running.
    expected = via = None
    if not first_cancelled:
        feasible = next((trip for (trip, _reasons), held in zip(per, holds, strict=True) if held), None)
        if feasible is not None:
            expected = _expected_arrival(feasible, state(feasible.legs[-1]))
            # A cushion continuation supplies the arrival (KTD19): say where it leaves the planned
            # route, so «fram HH:MM via Asker» is told from an ordinary delay on the planned route.
            if feasible is not planned and feasible.aimed_arrival < limit:
                via = _leaves_at(planned, feasible, state)
    problem = None
    if problem_reason is not None and deciding is not None:  # alerting means a reason cleared
        problem = TripProblem(
            kind=problem_reason.kind, index=problem_reason.index,
            probability=deciding.probability, threshold=deciding.threshold,
        )
    return Verdict(
        trip=planned,
        continuations=tuple(continuations),
        board=first_state.board,
        reason=problem_reason or deciding,
        deciding=deciding,
        alertworthy=alertworthy,
        confirmed=confirmed,
        first_cancelled=first_cancelled,
        admitted=admitted,
        complete=not any(
            state(leg).unknown for trip, *_rest in cushion for leg in trip.legs
        ),
        unknown_legs=tuple(i for i, leg in enumerate(planned.legs) if state(leg).unknown),
        facts=_facts(planned, state, expected, problem, via),
        leg_states=tuple(state(leg) for leg in planned.legs),
        p_miss=max(
            (r.probability for r in per[0][1] if r.kind == "missed_connection" and r.call is not None),
            default=None,
        ),
    )


def judge(
    plan: DayPlan,
    *,
    to_stop: str,
    window_start: datetime,
    window_end: datetime,
    catchable_after: datetime | None = None,
    unjudged: bool = False,
) -> list[Verdict]:
    """Every first-leg departure with a change in the window, judged (U3), in departure order.

    A departure is judged when its first leg leaves inside the inclusive window (`_trip_cells`'
    bound), it is still catchable (R10: the expected departure not before `catchable_after`), a leg
    is watched (R3), and its first train is not itself direct for the pair (KTD3). Every
    continuation of one first train is one subject (R5), composed per KTD19. A dropped trip is still
    judged until its first leg departs (KTD11, AE10). journey-status lists exactly these, with no
    `catchable_after` and `unjudged`, so the strip shows what the matcher judges (U9, KTD14).

    The plan is the canonical pair's, so its trips are judged whatever ref of the from-place the
    caller names: a caller asking about several refs of one place passes the plan once. `to_stop`
    is where KTD3 looks for the first train's alighting call, with every ref `places` gives it.

    Returns subjects KTD20 does not admit too, marked `admitted=False`, so `varsel` can keep one that
    holds a standing alert. A departure no reason has a figure for (`deciding` None) is returned
    only with `unjudged`: the matcher has nothing to alert on or to count toward a withdrawal
    there, while journey-status lists it all the same, grey (U9).
    """
    memo: dict[PlanLeg, LegState] = {}

    def state(leg: PlanLeg) -> LegState:
        found = memo.get(leg)
        if found is None:
            found = memo[leg] = leg_state(leg)
        return found

    subjects: dict[tuple, list[PlanTrip]] = {}
    for trip in plan.trips:
        if window_start <= trip.aimed_departure <= window_end and watched(trip):
            subjects.setdefault(_boarding(trip.legs[0]), []).append(trip)
    window_has_direct = any(
        window_start <= direct.aimed_departure <= window_end for direct in plan.directs
    )
    to_places = _to_places(to_stop)
    counts = functools.cache(_counts)
    verdicts: list[Verdict] = []
    for continuations in subjects.values():
        continuations.sort(key=lambda trip: (trip.aimed_arrival, len(trip.legs)))
        planned = continuations[0]
        first = planned.legs[0]
        if direct_for_pair(planned, plan, to_stop, to_places=to_places):
            continue
        if catchable_after is not None and _expected_departure(first, state(first)) < catchable_after:
            continue
        verdict = _verdict(
            continuations, state,
            admitted=_admitted(
                planned, plan, window_has_direct=window_has_direct, counts=counts
            ),
        )
        if unjudged or verdict.deciding is not None:
            verdicts.append(verdict)
    return verdicts
