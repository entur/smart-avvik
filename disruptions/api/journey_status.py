"""Every train in a journey's window today, graded — the «I dag» strip on `/varsel` and the public
`GET /notification-api/v1/journey-status` (plan 2026-09-22-001).

The matcher answers a different question on purpose: it keeps the trains worth a push and drops the
ones that can no longer be caught. The strip wants all of them, the ones that already ran included,
so it is assembled here from three in-memory sources the poller already builds:

* **the spine** — `rail_calls_planned_today`, loaded after each planned rebuild. It decides WHICH
  trains exist (KTD3). The prediction cache cannot: it holds calls from 60 min before its build to
  12 h after, so a train cancelled before it ran is gone from it an hour after its time.
* **the prediction cache** — read per train through `impact._lookup_journey_calls`, every scored
  call of that train across every situation, then paired as the matcher pairs them
  (`_paired_prediction`). That is deliberately not `_trip_cells`, which keeps one train per line
  per situation and would lose two of three trains one SX message names.
* **the actuals** — `rail_calls_today`, loaded every 15 min (U4). Before that loads, the spine's own
  `recorded` calls stand in, up to 60 min stale.

Since 2026-09-23 the spine also holds the Vy trains taken OUT of today's plan
(`rail_calls_removed_today`, plan 2026-09-23-001): every call `call_cancelled`, `call_source`
`removed`. They are read by the same rule as a declared cancellation, and marked `removed_from_plan`
so the strip and the morning digest can say what happened rather than «innstilt».

Precedence (KTD3): a declared cancellation, then a recorded alighting call, then a prediction, then
unknown. Nothing here queries BigQuery; every read is a dict lookup on a swapped-whole snapshot.

**Departures with a change** (multi-leg U9, KTD14), with TRIP_PLANS_ENABLED on and the pair's day
plan held: one more row per first-leg departure, `trip` set, from `trips.judge` -- the matcher's
own enumeration and verdict, so the strip lists what the matcher judges, the ones that already ran
included (no `catchable_after`). Red is therefore the gate's (KTD6, KTD19), and a broken change whose
KTD19 cushion holds is graded on the cushion's arrival: the strip is never red where the alert stays
silent. On top of it the spine's facts for the rail legs: a cancellation the cache has let go of, a
change observed missed (AE8), the arrival recorded at the destination. A leg nothing can judge
(KTD8) keeps the trip off green. A request still never touches the network: no plan, no trip rows,
and the public read says `not_evaluated` and starts the fetch (`notification_pull`).

A leaf module: it imports `impact` and `alert_policy` and never `varsel` or `notification_pull`, so
both surfaces can call it without breaking the rule that `varsel` never imports `notification_pull`.
What it needs from `varsel` (`place_refs`) and from `trip_plans` (the plan reader) is injected
through :func:`configure`; the trip logic it reaches through `impact`, which imports `trips`.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import alert_policy
import impact
import line_modes
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

_OSLO = ZoneInfo("Europe/Oslo")

#: The operators the spine carries: Vy and, since KIX-39, SJ Norge. A client says which trains it
#: covers off this. SJN's actuals come from the spine's recorded calls only (`rail_calls_today` stays
#: Vy), so its facts can be up to the planned TTL old.
OPERATORS: tuple[str, ...] = ("VYG", "SJN")
#: A no-SX figure further out than this is served `unknown`, not green (KTD7): without an operator
#: message the only signal is the last hour of observation, and beyond ~2 h the no-SX layer collapses
#: to the base rate — a green there would be the manufactured all-clear this codebase refuses.
NOSX_HORIZON_MIN = 120
#: The product bar (`alert_policy.TROUBLE_DELAY_MIN`) and the yellow floor: under 5 min is green
#: (the 2026-09-01 review set 3; raised to 5 on 2026-09-24).
RED_DELAY_MIN = alert_policy.TROUBLE_DELAY_MIN
YELLOW_DELAY_MIN = 5

State = Literal["confirmed", "predicted", "unknown"]
Level = Literal["red", "yellow", "green", "grey"]


@dataclass(frozen=True, slots=True)
class PlannedCall:
    """One row of `rail_calls_planned_today` (the spine) or `rail_calls_today` (the actuals).

    `operator` is the lineRef codespace, derived exactly as `main.refresh_call_predictions_cache`
    derives it, so `(operator, jkey)` joins the prediction cache. `arr_delay_min` is None today:
    neither SQL carries it, because reading the arrival times measured +11 % on the planned rebuild
    and +58 % on the 15-min actuals one (2026-09-22). The field stays so the grading picks the
    arrival up the day a table carries the column; the loader reads it with getattr.
    """

    operator: str
    jkey: str
    line: str | None
    stop_place: str | None
    stop_order: int
    aimed_dep: datetime | None
    aimed_arr: datetime | None
    dep_delay_min: int | None
    arr_delay_min: int | None
    call_cancelled: bool
    call_source: str | None
    boarding_allowed: bool = True
    alighting_allowed: bool = True
    #: A removed train's replacement bus (`rail_calls_removed_today.bus_for_train`); False elsewhere.
    bus_for_train: bool = False
    #: The stop name, on the terminus call only (the spine query nulls it everywhere else).
    destination: str | None = None


class CallTable:
    """A loaded call table, indexed per train and per stop. Built once, swapped whole, never mutated."""

    __slots__ = ("by_key", "by_stop", "refreshed_at")

    def __init__(self, calls: Iterable[PlannedCall], refreshed_at: datetime) -> None:
        by_key: dict[tuple[str, str], list[PlannedCall]] = {}
        by_stop: dict[str, list[PlannedCall]] = {}
        for c in calls:
            by_key.setdefault((c.operator, c.jkey), []).append(c)
            if c.stop_place is not None:
                by_stop.setdefault(c.stop_place, []).append(c)
        self.by_key = {k: tuple(sorted(v, key=lambda c: c.stop_order)) for k, v in by_key.items()}
        self.by_stop = by_stop
        self.refreshed_at = refreshed_at

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_key.values())


@dataclass(frozen=True, slots=True)
class RemovedTrain:
    """A train out of today's plan as the morning digest names and marks it (KTD6): `jkey` the
    relay's key, whose `KIX:PlanRemoved:` situation, dated by the day, is the digest's mark; `line`,
    `departure_time` (aimed, at its boarding stop) and `destination` its label. A direct row's own
    train, or a trip's removed ridden leg -- the connecting train, never the first leg that runs."""

    jkey: str
    line: str | None
    departure_time: datetime
    destination: str | None
    bus_for_train: bool


# ---- Wire shape --------------------------------------------------------------------------------


class TrainStatus(BaseModel):
    """One train in the window: what it is, what we know, and how bad. Or, with `trip` set, one
    departure with a change, in its first leg's identity (multi-leg KTD5) and graded end to end."""

    model_config = ConfigDict(extra="forbid")
    departure_time: datetime = Field(description="Aimed departure from the boarding stop, UTC.")
    arrival_time: datetime | None = Field(
        description="Aimed arrival at the alighting stop, UTC; at the destination for a trip."
    )
    operator: str
    line: str | None = Field(description="SIRI lineRef, e.g. `VYG:Line:L1`.")
    jkey: str = Field(description="Dated journey ref with its codespace stripped.")
    state: State = Field(
        description="`confirmed` when the alighting call is recorded, or the train is declared "
        "cancelled and its aimed departure has passed; `predicted` when the model has a figure; "
        "`unknown` otherwise."
    )
    level: Level = Field(
        description="`red` cancelled, >= 10 min late, or (predicted) over the alert gate; `yellow` "
        "5-9 min; `green` below; `grey` when nothing can be said."
    )
    cancelled: bool
    delay_min: int | None = Field(description="Confirmed delay at the alighting stop, minutes.")
    expected_departure_time: datetime | None = Field(
        description="Aimed departure plus the typical predicted departure delay (predicted only)."
    )
    delay_p50_min: int | None = Field(description="Typical predicted arrival delay (predicted only).")
    impact_probability: float | None = Field(
        description="P(cancelled OR >= 10 min late) at the boarding stop — what the push gates on. "
        "1.0 when cancelled; null on a confirmed or unknown train, and on a forecast the cache "
        "scored at one end only (the push cannot fire on one). For a trip, the figure its verdict "
        "was taken on (multi-leg KTD6)."
    )
    alert_worthy: bool
    observed_at: datetime | None = Field(
        description="Aimed time of the call the confirmed figure was read off, UTC."
    )
    removed_from_plan: bool = Field(
        description="Missing from today's timetable: it runs on this weekday, today's plan lacks "
        "it, and no cancellation was published. Always `cancelled` too. False on a trip, whose "
        "ridden leg out of the plan is `trip.problem.kind` `leg_removed`."
    )
    bus_for_train: bool = Field(description="A removed train Vy has set up a replacement bus for.")
    destination: str | None = Field(
        default=None, description="Where the train terminates, as a departure board names it: `Hamar`."
    )
    mode: str | None = Field(
        default=None,
        description="The line's Journey Planner transport mode: `rail` for every train listed today.",
    )
    # Required and nullable (multi-leg KTD13): the strict schema lists every property, so it is
    # present with TRIP_PLANS_ENABLED off too, as null (R16).
    trip: impact.TripFacts | None = Field(
        description="The departure with a change this row is, as `payload.trip` describes one: its "
        "legs, changes, expected arrival and, when it alerts or broke, the problem. Null on a direct "
        "train."
    )
    # A trip's ridden leg out of today's plan, for the morning digest (`removed_train`). Never on the
    # wire: the relay's key is ours, and `removed_from_plan` would name this row's first train.
    _removed: RemovedTrain | None = PrivateAttr(default=None)


class WindowSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime


class AsOf(BaseModel):
    """When each source was loaded. Up to 60 min (plan), 15 min (actuals) and one cycle (predictions)."""

    model_config = ConfigDict(extra="forbid")
    plan_refreshed_at: datetime | None
    predictions_refreshed_at: datetime | None
    actuals_refreshed_at: datetime | None


class JourneyStatus(BaseModel):
    """The strip for one definition at one instant."""

    model_config = ConfigDict(extra="forbid")
    now: datetime = Field(description="The server's instant; the client draws «nå» against it.")
    date: dt.date = Field(
        description="The Oslo date the window begins on: yesterday's while a window over midnight "
        "that began yesterday is still open."
    )
    window: WindowSpan | None = Field(
        description="Today's window, or last night's until a window over midnight ends; null on a "
        "day not in `days`. Past midnight, once the plan is rebuilt, last night's trains that "
        "arrived before midnight drop out of it; the ones still running or to come stay."
    )
    reason: Literal["not_a_travel_day"] | None = Field(description="`not_a_travel_day`, or null.")
    operators: list[str] = Field(description="Operators whose trains are listed (Vy and SJ Norge).")
    as_of: AsOf
    trains: list[TrainStatus]


# ---- Providers ---------------------------------------------------------------------------------

_lock = threading.Lock()
_spine: CallTable | None = None
_actuals: CallTable | None = None
_predictions_refreshed_at: Callable[[], datetime | None] = lambda: None
_place_refs: Callable[[str], tuple[str, ...]] = lambda ref: (ref,)
#: The pair's day plan, `(from_stop, to_stop, Oslo date) -> trips.DayPlan | None` (multi-leg U9):
#: `trip_plans.read_plan`, memory only. None: no departure with a change is listed.
_trip_plans: Callable[[str, str, dt.date], impact.trips.DayPlan | None] | None = None
#: Every plan the leader holds (`trip_plans.held`), for :func:`removed_today`'s trips. None: none.
_held_plans: Callable[[], Iterable[impact.trips.DayPlan]] | None = None


def configure(
    *,
    predictions_refreshed_at: Callable[[], datetime | None],
    place_refs: Callable[[str], tuple[str, ...]],
    trip_plans: Callable[[str, str, dt.date], impact.trips.DayPlan | None] | None = None,
    held_plans: Callable[[], Iterable[impact.trips.DayPlan]] | None = None,
) -> None:
    """Wire the prediction-cache stamp, `varsel.place_refs` and the plan reader, once, at startup.

    `place_refs` is not optional in production: a definition carries the CANONICAL ref, which for a
    station like Asker is its busiest (bus) side, while every rail call in the spine sits on the rail
    ref. Walked on the canonical ref alone the strip would be empty for exactly the stations the
    2026-08-03 collapse was about. It is injected because this module may not import `varsel`.

    `trip_plans` is `trip_plans.read_plan`, the reader `varsel` matches with (KTD4), and
    `held_plans` `trip_plans.held`, every plan the leader holds: injected, so this module never
    imports `trip_plans`, and read only with TRIP_PLANS_ENABLED on.
    """
    global _predictions_refreshed_at, _place_refs, _trip_plans, _held_plans
    _predictions_refreshed_at = predictions_refreshed_at
    _place_refs = place_refs
    _trip_plans = trip_plans
    _held_plans = held_plans


def set_spine(table: CallTable | None) -> None:
    global _spine
    with _lock:
        _spine = table


def set_actuals(table: CallTable | None) -> None:
    global _actuals
    with _lock:
        _actuals = table


def spine_refreshed_at() -> datetime | None:
    with _lock:
        return _spine.refreshed_at if _spine is not None else None


def actuals_refreshed_at() -> datetime | None:
    with _lock:
        return _actuals.refreshed_at if _actuals is not None else None


def _destination(calls: tuple[PlannedCall, ...]) -> str | None:
    """The train's terminus name, «Hamar» rather than «Hamar stasjon», or None.

    Read off the LAST call only (`by_key` is sorted by stop_order), and only when that call is a
    terminus -- no aimed departure -- so a journey the table holds only part of names nothing
    rather than a mid-route stop.
    """
    last = calls[-1] if calls else None
    if last is None or last.aimed_dep is not None or not last.destination:
        return None
    return last.destination.removesuffix(" stasjon")


def destination(operator: str | None, jkey: str | None) -> str | None:
    """Where one train terminates, from the spine; None while unloaded or for an unknown train
    (every bus: the spine is Vy and SJ Norge only)."""
    with _lock:
        spine = _spine
    if spine is None or operator is None or jkey is None:
        return None
    return _destination(spine.by_key.get((operator, jkey), ()))


def removed_today() -> frozenset[str]:
    """The stop places the spine's trains Vy took out of today's plan call at; empty while unloaded.
    With TRIP_PLANS_ENABLED on, also the boarding place of every held trip with a change whose
    ridden leg is such a train (AE13): the train calls at the change, never at the trip's from-place.

    `varsel`'s morning digest reads them to walk only registrations whose from-place a removed
    train calls at, so a day without a cut costs one scan of the spine and nothing per registration.
    A superset of the strip's own set (a trip whose KTD19 cushion holds is asked and names nothing),
    which is all a pre-filter must be.
    """
    with _lock:
        spine = _spine
    if spine is None:
        return frozenset()
    stops = {
        c.stop_place
        for calls in spine.by_key.values()
        for c in calls
        if c.call_source == "removed" and c.stop_place is not None
    }
    if _held_plans is not None and impact.trips.enabled():

        def removed(leg: impact.trips.PlanLeg) -> bool:
            key = impact.trips.removed_ref(leg)
            return key is not None and bool(spine.by_key.get(key) or impact._lookup_journey_calls(*key))

        stops.update(
            trip.legs[0].board_stop_place
            for plan in _held_plans() for trip in plan.trips
            if any(removed(leg) for leg in trip.legs)
        )
    return frozenset(stops)


def removed_train(train: TrainStatus) -> RemovedTrain | None:
    """The train out of today's plan a strip row stands for, as the morning digest names it: a
    direct row's own train when it is `removed_from_plan`, or an alerting trip's ridden leg whose
    train is out of the plan (its `problem` `leg_removed`, confirmed or on a forecast). None for any
    other row."""
    if train.trip is not None:
        return train._removed
    if not train.removed_from_plan:
        return None
    return RemovedTrain(train.jkey, train.line, train.departure_time, train.destination, train.bus_for_train)


def reset_state() -> None:
    """Test-only: forget both tables and the wiring."""
    global _spine, _actuals, _predictions_refreshed_at, _place_refs, _trip_plans, _held_plans
    with _lock:
        _spine = None
        _actuals = None
    _predictions_refreshed_at = lambda: None
    _place_refs = lambda ref: (ref,)
    _trip_plans = None
    _held_plans = None


# ---- The answer --------------------------------------------------------------------------------


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def window_today(definition: dict, now: datetime) -> tuple[datetime, datetime] | None:
    """Today's `[begin, end]` as UTC instants, or None when today is not one of `days`.

    `varsel.journey_windows`' Oslo arithmetic, restated because this module may not import `varsel`
    — minus its "drop a window whose end has passed" branch: the past half of today IS the strip's
    filled half. A window over midnight (`end < begin`) ends the next day and stays the strip's
    until then: at 00:30 Tuesday it is Monday's 22:00-01:00, from 01:00 Tuesday night's.
    """
    local = now.astimezone(_OSLO)
    days = set(definition["days"])
    begin, end = _minutes(definition["begin"]), _minutes(definition["end"])
    overnight = end < begin
    today = local.date()
    for date in (today - timedelta(days=1), today) if overnight else (today,):
        if date.weekday() not in days:
            continue
        midnight = datetime.combine(date, time(0, 0), tzinfo=_OSLO)
        start = midnight + timedelta(minutes=begin)
        stop = midnight + timedelta(days=int(overnight), minutes=end)
        if date < today and stop <= local:
            continue  # last night's window is over
        return start.astimezone(UTC), stop.astimezone(UTC)
    return None


def _trains(
    spine: CallTable, from_refs: tuple[str, ...], to_refs: set[str], start: datetime, end: datetime
) -> list[tuple[PlannedCall, PlannedCall]]:
    """(boarding, alighting) per train, in departure order — the matcher's eligibility rule on the
    spine's own rows: board at a from-ref inside the window with boarding open, alight later at a
    to-ref with alighting open. The flags are the same columns the prediction rows carry them from.

    Candidates are walked earliest boarding first, so a train calling the from-place twice (a loop)
    boards at its first usable call, whatever order the table was loaded in. `impact._trip_cells`
    tests only its most SEVERE boarding call for an alighting call; this accepts any, which is the
    right question for "which trains can I take" and the one place the two knowingly differ.
    """
    candidates = [
        b
        for ref in from_refs
        for b in spine.by_stop.get(ref, ())
        if b.aimed_dep is not None and b.boarding_allowed and start <= b.aimed_dep <= end
    ]
    candidates.sort(key=lambda b: (b.aimed_dep, b.stop_order, b.jkey))
    found: dict[tuple[str, str], tuple[PlannedCall, PlannedCall]] = {}
    for b in candidates:
        key = (b.operator, b.jkey)
        if key in found:
            continue
        a = next(
            (
                c
                for c in spine.by_key[key]
                if c.stop_place in to_refs
                and c.aimed_arr is not None
                and c.alighting_allowed
                and c.stop_order > b.stop_order
            ),
            None,
        )
        if a is not None:
            found[key] = (b, a)
    return sorted(found.values(), key=lambda pair: (pair[0].aimed_dep, pair[0].jkey))


def _confirmed_delay(calls: tuple[PlannedCall, ...], a: PlannedCall) -> tuple[int, PlannedCall] | None:
    """The delay a recorded alighting call says, and the call it was read off.

    Arrival first, when a table carries `arr_delay_min` (neither does today, for cost); otherwise
    the departure delay at the same call, within a minute or so of the arrival on a through train;
    at a terminus, which has no departure, the last recorded call before it (KTD6).
    """
    if a.arr_delay_min is not None:
        return a.arr_delay_min, a
    if a.dep_delay_min is not None:
        return a.dep_delay_min, a
    for c in reversed(calls):
        if c.stop_order < a.stop_order and c.call_source == "recorded" and c.dep_delay_min is not None:
            return c.dep_delay_min, c
    return None


def _arrival_read(recorded: dict[int, PlannedCall], at: PlannedCall) -> tuple[int, PlannedCall] | None:
    """`_confirmed_delay` of the recorded call `at`, over its train's recorded calls (`_recorded`)
    in stop order."""
    return _confirmed_delay(tuple(recorded[o] for o in sorted(recorded)), at)


def _level_for_delay(delay: int | None) -> Level:
    if delay is None:
        return "grey"
    if delay >= RED_DELAY_MIN:
        return "red"
    if delay >= YELLOW_DELAY_MIN:
        return "yellow"
    return "green"


def _worst_prediction(
    preds: list[impact.CallPred], stop_order: int
) -> impact.CallPred | None:
    """The most severe scored call at this stop, situation number as the tie-break: two SX messages
    about one cancelled train tie exactly, and an unstable pick would flip between refreshes."""
    at = [p for p in preds if p.stop_order == stop_order]
    if not at:
        return None
    return max(at, key=lambda p: (impact._call_severity(p), p.situation_number))


def _paired_prediction(
    preds: list[impact.CallPred], board_order: int, alight_order: int
) -> tuple[impact.CallPred, impact.CallPred] | None:
    """The worst (boarding, alighting) pair, paired as the push matcher pairs them.

    Each situation that scored the boarding call is paired with its own alighting row, or, when it
    has none, with the worst row any situation scored there (`impact._destination_call`, since
    2026-10-22). A train whose calls `predict_calls.sql` split across situations, through a partial
    cancellation or an SX message whose onset falls mid-trip, is still a train the push can fire
    on. So no pair means one end is not in the cache at all, and a figure read off one end alone may
    not be red: no push fires on it, and the gate-derived red exists to rule that disagreement out.
    """
    boards: dict[str, impact.CallPred] = {}
    alights: dict[str, impact.CallPred] = {}
    for p in preds:
        if p.stop_order == board_order:
            boards[p.situation_number] = p
        elif p.stop_order == alight_order:
            alights[p.situation_number] = p
    fallback = _worst_prediction(preds, alight_order)
    if fallback is None:
        return None
    pairs = [(b, alights.get(sit, fallback)) for sit, b in boards.items()]
    return max(pairs, key=lambda pr: (impact._call_severity(pr[0]), pr[0].situation_number), default=None)


def _end_cancelled(planned: PlannedCall, recorded: PlannedCall | None, preds: list[impact.CallPred]) -> bool:
    """Whether one end of the trip is cancelled. A recorded row for the stop is the latest fact and
    wins either way — a train the spine and the prediction cache (built from the same hourly table)
    still call cancelled has RUN if its call is recorded as served, which is how a reinstatement
    shows. Without one, the spine's flag or ET's declaration on any scored call at that stop."""
    if recorded is not None:
        return recorded.call_cancelled
    return planned.call_cancelled or any(
        p.stop_order == planned.stop_order and p.et_declared_cancelled for p in preds
    )


def _recorded(key: tuple[str, str], spine: CallTable, actuals: CallTable | None) -> dict[int, PlannedCall]:
    """A train's recorded calls by stop order: the spine's own, overlaid per stop by the actuals
    index, which is fresher (15 min against 60). An overlay and not a replacement:
    `rail_calls_today` keeps only calls with an aimed DEPARTURE, so it never holds a terminus
    arrival, and letting it stand alone for a train it knows would hide the spine's recorded
    terminus call."""
    by_order = {c.stop_order: c for c in spine.by_key.get(key, ()) if c.call_source == "recorded"}
    if actuals is not None:
        by_order.update({c.stop_order: c for c in actuals.by_key.get(key, ())})
    return by_order


def _blank_row(
    *,
    departure_time: datetime,
    arrival_time: datetime | None,
    operator: str,
    line: str | None,
    jkey: str,
    destination: str | None,
    mode: str | None,
    trip: impact.TripFacts | None,
) -> dict:
    """A row's identity with every figure blank: what each grade of `_status` and `_trip_status`
    fills in."""
    return {
        "departure_time": departure_time,
        "arrival_time": arrival_time,
        "operator": operator,
        "line": line,
        "jkey": jkey,
        "destination": destination,
        "mode": mode,
        "impact_probability": None,
        "alert_worthy": False,
        "expected_departure_time": None,
        "delay_min": None,
        "delay_p50_min": None,
        "observed_at": None,
        "removed_from_plan": False,
        "bus_for_train": False,
        "trip": trip,
    }


def _status(
    b: PlannedCall,
    a: PlannedCall,
    spine: CallTable,
    actuals: CallTable | None,
    now: datetime,
) -> TrainStatus:
    key = (b.operator, b.jkey)
    preds = impact._lookup_journey_calls(b.operator, b.jkey)

    by_order = _recorded(key, spine, actuals)
    recorded_a = by_order.get(a.stop_order)
    recorded_b = by_order.get(b.stop_order)

    common = _blank_row(
        departure_time=b.aimed_dep,
        arrival_time=a.aimed_arr,
        operator=b.operator,
        line=b.line,
        jkey=b.jkey,
        destination=_destination(spine.by_key[key]),
        mode=line_modes.transport_mode(b.operator, b.line),
        trip=None,
    )

    # R3: a trip is cancelled when EITHER end is. Vy cancels partial legs.
    if _end_cancelled(b, recorded_b, preds) or _end_cancelled(a, recorded_a, preds):
        # A declared cancellation is an observation only once its own time has passed
        # (`impact._is_observed`'s rule): before that it is an announcement, drawn as a ring.
        # 1.0, as `impact._cancelled_prediction` serves it, whichever source knew.
        passed = now >= b.aimed_dep
        removed = b.call_source == "removed"
        return TrainStatus(
            **{**common, "impact_probability": 1.0, "alert_worthy": True,
               "observed_at": b.aimed_dep if passed else None,
               "removed_from_plan": removed, "bus_for_train": removed and b.bus_for_train},
            state="confirmed" if passed else "predicted",
            level="red",
            cancelled=True,
        )

    if recorded_a is not None:
        read = _arrival_read(by_order, recorded_a)
        delay, source = read if read is not None else (None, recorded_a)
        return TrainStatus(
            **{**common, "delay_min": delay, "observed_at": source.aimed_arr or source.aimed_dep},
            state="confirmed",
            level=_level_for_delay(delay),
            cancelled=False,
        )

    pair = _paired_prediction(preds, b.stop_order, a.stop_order)
    if pair is not None:
        bpred, apred = pair
    else:
        # One end is not in the cache at all, typically the boarding call aged out (the train is
        # under way: the cache holds calls from 60 min back). Whatever IS scored still says how late
        # the train looks, but never red by the gate: the push cannot fire on an unpaired figure,
        # so neither may the strip's forecast red.
        bpred = _worst_prediction(preds, b.stop_order)
        apred = _worst_prediction(preds, a.stop_order)
    figure = bpred or apred
    if figure is None or (
        not figure.has_situation and b.aimed_dep - now > timedelta(minutes=NOSX_HORIZON_MIN)
    ):
        return _unknown(common)

    typ = None
    if apred is not None:
        typ = apred.arr_delay_p50_stack_min
        if typ is None:
            typ = apred.arr_delay_typ_min
    if typ is None and bpred is not None:
        typ = bpred.dep_delay_p50_stack_min
        if typ is None:
            typ = bpred.dep_delay_typ_min

    impact_p = bpred.p_impact_dep if pair is not None else None
    alert_worthy = pair is not None and alert_policy.gate(
        impact_p, b.operator, has_situation=bpred.has_situation
    ).alertworthy
    if alert_worthy:
        level: Level = "red"
    elif typ is None:
        return _unknown(common)
    else:
        level = _level_for_delay(typ)
        if level == "red":
            # Gate-not-passed but a typical delay >= 10: the gate is the one statement of "red" for
            # a forecast (Key Decisions), so the strip may not call red what the push would not.
            level = "yellow"
    return TrainStatus(
        **{**common, "impact_probability": impact_p, "alert_worthy": alert_worthy,
           "delay_p50_min": typ,
           "expected_departure_time": impact._expected_departure(bpred) if bpred is not None else None},
        state="predicted",
        level=level,
        cancelled=False,
    )


def _unknown(common: dict) -> TrainStatus:
    return TrainStatus(**common, state="unknown", level="grey", cancelled=False)


# ---- Departures with a change (multi-leg U9, KTD14) ---------------------------------------------

# A rail leg as the spine holds it: its boarding and alighting calls, and its train's recorded calls
# by stop order (`_recorded`).
_SpineLeg = tuple[PlannedCall, PlannedCall, dict[int, PlannedCall]]


def _trip_rows(
    definition: dict,
    start: datetime,
    end: datetime,
    spine: CallTable,
    actuals: CallTable | None,
    now: datetime,
    direct: set[tuple[str, str, datetime]],
) -> list[TrainStatus]:
    """One row per first-leg departure with a change in today's window, the ones that already ran
    included, or none while TRIP_PLANS_ENABLED is off or no plan is held (a request never fetches).

    The set is the request-path matcher's (`varsel.match_journey` without a standing alert):
    `trips.judge`'s departures, the figure-less ones too, KTD20's admitted ones only -- a trip that
    waiting for the next direct train beats is an alternative, and listed it could show red where no
    alert would go -- and never one whose first-leg departure is a direct row here (AE7), as a
    direct alert wins `varsel`'s merge. `direct_for_pair` cannot always see that: it reads the
    cache, an hour back at most, and the plan's direct patterns, which JP thins by dominance.

    `ponytail:` KTD20 reads the prediction cache, so a trip admitted only because the direct train
    after it was cancelled drops off the strip once that train's calls age out of the cache, an hour
    after it. Upgrade path: the cancellation from the spine, if the strip ever needs it all day.
    """
    reader = _trip_plans
    if reader is None or not impact.trips.enabled():
        return []
    plan = reader(definition["from_stop"], definition["to_stop"], start.astimezone(_OSLO).date())
    if plan is None:
        return []
    verdicts = impact.trips.judge(
        plan, to_stop=definition["to_stop"], window_start=start, window_end=end, unjudged=True
    )
    return [
        _trip_status(verdict, spine, actuals, now)
        for verdict in verdicts
        if verdict.admitted and verdict.key not in direct
    ]


def _spine_leg(leg: impact.trips.PlanLeg, spine: CallTable, actuals: CallTable | None) -> _SpineLeg | None:
    """A rail leg's two calls in the spine, matched as the verdict matches the cache's, on stop
    place and aimed time; a train out of the plan under the relay's key, as the spine holds it. None
    for a leg the spine does not hold: every bus, and a train it lacks."""
    for key in (impact.trips.leg_ref(leg), impact.trips.removed_ref(leg)):
        calls = spine.by_key.get(key) if key is not None else None
        if not calls:
            continue
        b = next((c for c in calls if c.stop_place == leg.board_stop_place
                  and c.aimed_dep == leg.aimed_departure), None)
        a = next((c for c in calls if c.stop_place == leg.alight_stop_place
                  and c.aimed_arr == leg.aimed_arrival), None)
        if b is not None and a is not None:
            return b, a, _recorded(key, spine, actuals)
    return None


def _removed_leg(leg: impact.trips.PlanLeg, spine: CallTable) -> RemovedTrain | None:
    """A trip's ridden leg out of the plan as the digest names it (AE13): the relay's key -- the
    same `trips.removed_ref` the verdict found its rows under, dated by the leg's service day, so
    the digest's `KIX:PlanRemoved:` mark ends in the Oslo day the blob prune keeps -- and the leg's
    own line and aimed departure at its boarding stop, with the train's terminus and replacement
    bus off the spine's rows for it."""
    key = impact.trips.removed_ref(leg)
    if key is None:
        return None
    calls = spine.by_key.get(key, ())
    return RemovedTrain(
        key[1], leg.line, leg.aimed_departure, _destination(calls), any(c.bus_for_train for c in calls)
    )


def _with_removed(row: TrainStatus, legs: tuple[impact.trips.PlanLeg, ...], spine: CallTable) -> TrainStatus:
    """An alerting trip row, carrying its ridden leg out of the plan for the digest (AE13) when
    that is its problem: the per-train push and the event drop a `KIX:PlanRemoved:` situation,
    confirmed or predicted, so the digest must name it either way."""
    problem = row.trip.problem if row.trip is not None else None
    if problem is not None and problem.kind == "leg_removed":
        row._removed = _removed_leg(legs[problem.index], spine)
    return row


def _problem(kind: str, index: int, leg: impact.trips.PlanLeg) -> impact.alternatives.TripProblem:
    """A certainty the spine observed, as KTD6 gates one: 1.0 at the leg's operator's no-SX bar,
    since no operator message stands behind an observation of ours."""
    threshold, _ = alert_policy.gate(1.0, leg.operator, has_situation=False)
    return impact.alternatives.TripProblem(kind=kind, index=index, probability=1.0, threshold=threshold)


def _cancelled_leg(
    states: list[impact.trips.LegState], rail: list[_SpineLeg | None]
) -> tuple[int, str] | None:
    """The first ridden rail leg the spine says is cancelled at either end while the cache no
    longer scores it, and the reason kind: an hour after its calls the spine is what still knows,
    as it is for a direct train. While the cache scores the leg the verdict decides, cushion and
    all (KTD19).

    `ponytail:` once the cache has let go, a cancelled change whose KTD19 cushion held reads red.
    Upgrade path: compose the spine's cancellations across the continuations, as the verdict does.
    """
    for i, (state, found) in enumerate(zip(states, rail, strict=True)):
        if found is None or state.board or state.alight or state.removed:
            continue
        b, a, recorded = found
        if _end_cancelled(b, recorded.get(b.stop_order), []) or _end_cancelled(a, recorded.get(a.stop_order), []):
            return i, "leg_removed" if b.call_source == "removed" else "leg_cancelled"
    return None


def _missed_change(
    legs: tuple[impact.trips.PlanLeg, ...],
    states: list[impact.trips.LegState],
    rail: list[_SpineLeg | None],
    now: datetime,
) -> tuple[int, PlannedCall] | None:
    """A change observed missed (AE8): the arriving train's recorded call at the change, plus the
    walk, after the connecting departure -- recorded when the spine has it, else JP's expected --
    and that departure has passed. The transfer index and the call it was read off. Never against
    the aimed departure alone: a connecting vehicle not yet recorded may be held or late too.

    The arrival is the recorded call's delay figure (`_confirmed_delay`: the departure delay at a
    through station, as the strip reads every arrival today). A bus arriving has no recorded call of
    ours, so its change is never observed here and stays predicted or grey (KTD8).

    `ponytail:` a real miss onto a train JP has no expected time for stays predicted until the
    actuals record that train's departure, up to 15 min later."""
    for i in range(len(legs) - 1):
        if rail[i] is None:
            continue
        _b, a, recorded = rail[i]
        at = recorded.get(a.stop_order)
        read = None if at is None else _arrival_read(recorded, at)
        if read is None:
            continue
        arrived = legs[i].aimed_arrival + timedelta(minutes=read[0], seconds=legs[i].walk_after_s)
        leaves = None
        if rail[i + 1] is not None:
            b, _a, onward = rail[i + 1]
            left = onward.get(b.stop_order)
            if left is not None:
                leaves = legs[i + 1].aimed_departure + timedelta(minutes=left.dep_delay_min or 0)
        realtime = states[i + 1].realtime
        if leaves is None and realtime is not None:
            leaves = realtime.expected_departure
        if leaves is not None and now >= leaves and arrived > leaves:
            return i, at
    return None


def _trip_status(
    verdict: impact.trips.Verdict, spine: CallTable, actuals: CallTable | None, now: datetime
) -> TrainStatus:
    """One departure with a change, graded (KTD14), in the direct row's precedence: a confirmed
    reason (KTD6, composed per KTD19), then a change observed missed (AE8), then the arrival
    recorded at the destination, then the verdict's forecast, then unknown.

    Red for a forecast is the verdict's gate, so a broken planned change whose cushion holds is
    graded on the cushion's arrival (KTD9's expected arrival) and never red where the alert stays
    silent. A leg nothing can judge greys the trip (KTD8), and so does a no-SX figure past the
    horizon, as on a direct train. The ring-then-fill rule is today's, on the trip's last leg; a
    change observed missed is a fact at once, and filled.
    """
    legs = verdict.trip.legs
    first, last = legs[0], legs[-1]
    operator, jkey = impact.trips.leg_ref(first)
    # The states the verdict was taken on, so the row and the verdict read one cache snapshot.
    states = list(verdict.leg_states)
    rail = [_spine_leg(leg, spine, actuals) for leg in legs]
    facts = verdict.facts
    common = _blank_row(
        departure_time=first.aimed_departure,
        arrival_time=last.aimed_arrival,
        operator=operator or "",
        line=first.line,
        jkey=jkey,
        destination=_destination(spine.by_key.get((operator, jkey), ())),
        mode=first.mode,
        trip=facts,
    )

    gone = None if verdict.confirmed else _cancelled_leg(states, rail)
    if verdict.confirmed or gone is not None:
        if gone is not None:
            index, kind = gone
            facts = facts.model_copy(update={
                "problem": _problem(kind, index, legs[index]), "expected_arrival": None,
            })
        passed = now >= last.aimed_departure
        return _with_removed(TrainStatus(
            **{**common, "trip": facts, "impact_probability": 1.0, "alert_worthy": True,
               "observed_at": last.aimed_departure if passed else None},
            state="confirmed" if passed else "predicted",
            level="red",
            cancelled=True,
        ), legs, spine)

    missed = _missed_change(legs, states, rail, now)
    if missed is not None:
        index, call = missed
        facts = facts.model_copy(update={
            "problem": _problem("missed_connection", index, legs[index]), "expected_arrival": None,
        })
        return TrainStatus(
            **{**common, "trip": facts, "observed_at": call.aimed_arr or call.aimed_dep},
            state="confirmed",
            level="red",
            cancelled=False,
        )

    if rail[-1] is not None:
        _b, a, recorded = rail[-1]
        at = recorded.get(a.stop_order)
        if at is not None:
            read = _arrival_read(recorded, at)
            delay, source = read if read is not None else (None, at)
            return TrainStatus(
                **{**common, "delay_min": delay, "observed_at": source.aimed_arr or source.aimed_dep},
                state="confirmed",
                level=_level_for_delay(delay),
                cancelled=False,
            )

    deciding = verdict.deciding
    if deciding is None or (
        not deciding.has_situation and first.aimed_departure - now > timedelta(minutes=NOSX_HORIZON_MIN)
    ):
        return _unknown(common)
    expected_departure = None
    if verdict.board:
        expected_departure = impact._expected_departure(impact._worst_call(verdict.board))
    elif states[0].realtime is not None:
        expected_departure = states[0].realtime.expected_departure
    delay = None
    if facts.expected_arrival is not None:
        delay = max(round((facts.expected_arrival - last.aimed_arrival).total_seconds() / 60), 0)
    forecast = {**common, "impact_probability": deciding.probability, "delay_p50_min": delay,
                "expected_departure_time": expected_departure}
    if verdict.alertworthy:
        # Red on a forecast can still be a ridden train out of the plan: a KTD19 cushion only
        # predicted to fail leaves the verdict unconfirmed, and the digest is its only channel.
        return _with_removed(
            TrainStatus(**{**forecast, "alert_worthy": True}, state="predicted", level="red", cancelled=False),
            legs, spine,
        )
    unknown_leg = any(
        state.unknown and not (found is not None and {found[0].stop_order, found[1].stop_order} <= found[2].keys())
        for state, found in zip(states, rail, strict=True)
    )
    if unknown_leg or delay is None:
        return _unknown(common)
    # Not over the gate, so not red whatever the delay: the gate is the one statement of red for a
    # forecast, as on a direct train.
    level = _level_for_delay(delay)
    return TrainStatus(**forecast, state="predicted", level="yellow" if level == "red" else level, cancelled=False)


def status_for(definition: dict, now: datetime) -> JourneyStatus | None:
    """The strip for `definition` (`from_stop`, `to_stop`, `begin`, `end`, `days`) at `now`, or None
    while the spine is not loaded — which a caller must never render as «alt i rute»."""
    with _lock:
        spine, actuals = _spine, _actuals
    if spine is None:
        return None
    as_of = AsOf(
        plan_refreshed_at=spine.refreshed_at,
        predictions_refreshed_at=_predictions_refreshed_at(),
        actuals_refreshed_at=actuals.refreshed_at if actuals is not None else None,
    )
    local_date = now.astimezone(_OSLO).date()
    window = window_today(definition, now)
    if window is None:
        return JourneyStatus(
            now=now, date=local_date, window=None, reason="not_a_travel_day",
            operators=list(OPERATORS), as_of=as_of, trains=[],
        )
    start, end = window
    from_refs = _place_refs(definition["from_stop"])
    to_refs = set(_place_refs(definition["to_stop"]))
    pairs = _trains(spine, from_refs, to_refs, start, end)
    trains = [_status(b, a, spine, actuals, now) for b, a in pairs]
    trains += _trip_rows(
        definition, start, end, spine, actuals, now,
        direct={(b.operator, b.jkey, b.aimed_dep) for b, _a in pairs},
    )
    # Stable, so the direct trains keep `_trains`' order and a trip follows one at the same minute.
    trains.sort(key=lambda t: t.departure_time)
    return JourneyStatus(
        now=now, date=start.astimezone(_OSLO).date(), window=WindowSpan(start=start, end=end), reason=None,
        operators=list(OPERATORS), as_of=as_of, trains=trains,
    )
