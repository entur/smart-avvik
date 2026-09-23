"""Every train in a journey's window today, graded — the «I dag» strip on `/varsel` and the public
`GET /notification-api/v1/journey-status` (plan 2026-09-22-001).

The matcher answers a different question on purpose: it keeps the trains worth a push and drops the
ones that can no longer be caught. The strip wants all of them, the ones that already ran included,
so it is assembled here from three in-memory sources the poller already builds:

* **the spine** — `rail_calls_planned_today`, loaded after each planned rebuild. It decides WHICH
  trains exist (KTD3). The prediction cache cannot: it holds calls from 60 min before its build to
  12 h after, so a train cancelled before it ran is gone from it an hour after its time.
* **the prediction cache** — read per train through `impact._lookup_journey_calls`, every scored
  call of that train across every situation, then paired within ONE situation as the matcher pairs
  them (`_paired_prediction`). That is deliberately not `_trip_cells`, which keeps one train per
  line per situation and would lose two of three trains one SX message names.
* **the actuals** — `rail_calls_today`, loaded every 15 min (U4). Before that loads, the spine's own
  `recorded` calls stand in, up to 60 min stale.

Since 2026-09-23 the spine also holds the Vy trains taken OUT of today's plan
(`rail_calls_removed_today`, plan 2026-09-23-001): every call `call_cancelled`, `call_source`
`removed`. They are read by the same rule as a declared cancellation, and marked `removed_from_plan`
so the strip and the morning digest can say what happened rather than «innstilt».

Precedence (KTD3): a declared cancellation, then a recorded alighting call, then a prediction, then
unknown. Nothing here queries BigQuery; every read is a dict lookup on a swapped-whole snapshot.

A leaf module: it imports `impact` and `alert_policy` and never `varsel` or `notification_pull`, so
both surfaces can call it without breaking the rule that `varsel` never imports `notification_pull`.
What it needs from `varsel` (`place_refs`) is injected through :func:`configure`.
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
from pydantic import BaseModel, ConfigDict, Field

_OSLO = ZoneInfo("Europe/Oslo")

#: Both call tables are `lineRef LIKE 'VYG:%'` (R8). A client says «kun Vy-tog» off this.
OPERATORS: tuple[str, ...] = ("VYG",)
#: A no-SX figure further out than this is served `unknown`, not green (KTD7): without an operator
#: message the only signal is the last hour of observation, and beyond ~2 h the no-SX layer collapses
#: to the base rate — a green there would be the manufactured all-clear this codebase refuses.
NOSX_HORIZON_MIN = 120
#: The product bar (`alert_policy.TROUBLE_DELAY_MIN`) and the 2026-09-01 review's yellow floor.
RED_DELAY_MIN = alert_policy.TROUBLE_DELAY_MIN
YELLOW_DELAY_MIN = 3

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


# ---- Wire shape --------------------------------------------------------------------------------


class TrainStatus(BaseModel):
    """One train in the window: what it is, what we know, and how bad."""

    model_config = ConfigDict(extra="forbid")
    departure_time: datetime = Field(description="Aimed departure from the boarding stop, UTC.")
    arrival_time: datetime | None = Field(description="Aimed arrival at the alighting stop, UTC.")
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
        "3-9 min; `green` below; `grey` when nothing can be said."
    )
    cancelled: bool
    delay_min: int | None = Field(description="Confirmed delay at the alighting stop, minutes.")
    expected_departure_time: datetime | None = Field(
        description="Aimed departure plus the typical predicted departure delay (predicted only)."
    )
    delay_p50_min: int | None = Field(description="Typical predicted arrival delay (predicted only).")
    impact_probability: float | None = Field(
        description="P(cancelled OR >= 10 min late) at the boarding stop — what the push gates on. "
        "1.0 when cancelled; null on a confirmed or unknown train, and on a forecast no single "
        "situation scored at both ends (the push cannot fire on one)."
    )
    alert_worthy: bool
    observed_at: datetime | None = Field(
        description="Aimed time of the call the confirmed figure was read off, UTC."
    )
    removed_from_plan: bool = Field(
        description="Missing from today's timetable: it runs on this weekday, today's plan lacks "
        "it, and no cancellation was published. Always `cancelled` too."
    )
    bus_for_train: bool = Field(description="A removed train Vy has set up a replacement bus for.")


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
    date: dt.date = Field(description="The Oslo date the window is on.")
    window: WindowSpan | None = Field(description="Today's window; null on a day not in `days`.")
    reason: Literal["not_a_travel_day"] | None = Field(description="`not_a_travel_day`, or null.")
    operators: list[str] = Field(description="Operators whose trains are listed (Vy only in v1).")
    as_of: AsOf
    trains: list[TrainStatus]


# ---- Providers ---------------------------------------------------------------------------------

_lock = threading.Lock()
_spine: CallTable | None = None
_actuals: CallTable | None = None
_predictions_refreshed_at: Callable[[], datetime | None] = lambda: None
_place_refs: Callable[[str], tuple[str, ...]] = lambda ref: (ref,)


def configure(
    *,
    predictions_refreshed_at: Callable[[], datetime | None],
    place_refs: Callable[[str], tuple[str, ...]],
) -> None:
    """Wire the prediction-cache stamp and `varsel.place_refs`, once, at startup.

    `place_refs` is not optional in production: a definition carries the CANONICAL ref, which for a
    station like Asker is its busiest (bus) side, while every rail call in the spine sits on the rail
    ref. Walked on the canonical ref alone the strip would be empty for exactly the stations the
    2026-08-03 collapse was about. It is injected because this module may not import `varsel`.
    """
    global _predictions_refreshed_at, _place_refs
    _predictions_refreshed_at = predictions_refreshed_at
    _place_refs = place_refs


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


def reset_state() -> None:
    """Test-only: forget both tables and the wiring."""
    global _spine, _actuals, _predictions_refreshed_at, _place_refs
    with _lock:
        _spine = None
        _actuals = None
    _predictions_refreshed_at = lambda: None
    _place_refs = lambda ref: (ref,)


# ---- The answer --------------------------------------------------------------------------------


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def window_today(definition: dict, now: datetime) -> tuple[datetime, datetime] | None:
    """Today's `[begin, end]` as UTC instants, or None when today is not one of `days`.

    `varsel.journey_windows`' Oslo arithmetic, restated because this module may not import `varsel`
    — minus its "drop a window whose end has passed" branch: the past half of today IS the strip's
    filled half.
    """
    local = now.astimezone(_OSLO).date()
    if local.weekday() not in set(definition["days"]):
        return None
    midnight = datetime.combine(local, time(0, 0), tzinfo=_OSLO)
    start = midnight + timedelta(minutes=_minutes(definition["begin"]))
    end = midnight + timedelta(minutes=_minutes(definition["end"]))
    return start.astimezone(UTC), end.astimezone(UTC)


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
    """The worst (boarding, alighting) pair scored by ONE situation, as the push matcher pairs them.

    The matcher groups a situation's calls per train and needs both the boarding and the alighting
    call inside that one group (`impact._trip_cells`). A situation whose onset falls mid-trip scores
    the boarding call and not the alighting one (or the reverse, with the other end in the no-SX
    branch), and the matcher never alerts on it for this trip. Taking the boarding figure from it
    anyway would paint red where no push fires, which is the disagreement the gate-derived red exists
    to rule out.
    """
    by_situation: dict[str, dict[int, impact.CallPred]] = {}
    for p in preds:
        if p.stop_order in (board_order, alight_order):
            by_situation.setdefault(p.situation_number, {})[p.stop_order] = p
    pairs = [
        (calls[board_order], calls[alight_order])
        for calls in by_situation.values()
        if board_order in calls and alight_order in calls
    ]
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


def _status(
    b: PlannedCall,
    a: PlannedCall,
    spine: CallTable,
    actuals: CallTable | None,
    now: datetime,
) -> TrainStatus:
    key = (b.operator, b.jkey)
    preds = impact._lookup_journey_calls(b.operator, b.jkey)

    # Recorded calls: the spine's own, overlaid per stop by the actuals index, which is fresher
    # (15 min against 60). An overlay and not a replacement: `rail_calls_today` keeps only calls
    # with an aimed DEPARTURE, so it never holds a terminus arrival, and letting it stand alone for
    # a train it knows would hide the spine's recorded terminus call.
    by_order = {c.stop_order: c for c in spine.by_key[key] if c.call_source == "recorded"}
    if actuals is not None:
        by_order.update({c.stop_order: c for c in actuals.by_key.get(key, ())})
    recorded_calls = tuple(by_order[o] for o in sorted(by_order))
    recorded_a = by_order.get(a.stop_order)
    recorded_b = by_order.get(b.stop_order)

    common = {
        "departure_time": b.aimed_dep,
        "arrival_time": a.aimed_arr,
        "operator": b.operator,
        "line": b.line,
        "jkey": b.jkey,
        "impact_probability": None,
        "alert_worthy": False,
        "expected_departure_time": None,
        "delay_min": None,
        "delay_p50_min": None,
        "observed_at": None,
        "removed_from_plan": False,
        "bus_for_train": False,
    }

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
        read = _confirmed_delay(recorded_calls, recorded_a)
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
        # No situation scored both ends. Either the boarding call has aged out of the cache (the
        # train is under way: it holds calls from 60 min back) or two situations split the trip.
        # Whatever IS scored still says how late the train looks, but never red by the gate: the
        # push cannot fire on an unpaired figure, so neither may the strip's forecast red.
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
    trains = [
        _status(b, a, spine, actuals, now) for b, a in _trains(spine, from_refs, to_refs, start, end)
    ]
    return JourneyStatus(
        now=now, date=local_date, window=WindowSpan(start=start, end=end), reason=None,
        operators=list(OPERATORS), as_of=as_of, trains=trains,
    )
