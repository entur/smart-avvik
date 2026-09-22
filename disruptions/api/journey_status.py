"""Every train in a journey's window today, graded — the «I dag» strip on `/varsel` and the public
`GET /notification-api/v1/journey-status` (plan 2026-09-22-001).

The matcher answers a different question on purpose: it keeps the trains worth a push and drops the
ones that can no longer be caught. The strip wants all of them, the ones that already ran included,
so it is assembled here from three in-memory sources the poller already builds:

* **the spine** — `rail_calls_planned_today`, loaded after each planned rebuild. It decides WHICH
  trains exist (KTD3). The prediction cache cannot: it holds calls from 60 min before its build to
  12 h after, so a train cancelled before it ran is gone from it an hour after its time.
* **the prediction cache** — read per train through `impact._lookup_journey_calls`, every scored
  call of that train across every situation. That is deliberately not `_trip_cells`, which keeps
  one train per line per situation and would lose two of three trains one SX message names.
* **the actuals** — `rail_calls_today`, loaded every 15 min (U4). Before that loads, the spine's own
  `recorded` calls stand in, up to 60 min stale.

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
        description="P(cancelled OR >= 10 min late) at the boarding stop — what the push gates on."
    )
    alert_worthy: bool
    observed_at: datetime | None = Field(
        description="Aimed time of the call the confirmed figure was read off, UTC."
    )


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
    reason: str | None = Field(description="`not_a_travel_day`, or null.")
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
    """
    found: dict[tuple[str, str], tuple[PlannedCall, PlannedCall]] = {}
    for ref in from_refs:
        for b in spine.by_stop.get(ref, ()):
            if b.aimed_dep is None or not b.boarding_allowed or not (start <= b.aimed_dep <= end):
                continue
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


def _status(
    b: PlannedCall,
    a: PlannedCall,
    spine: CallTable,
    actuals: CallTable | None,
    now: datetime,
) -> TrainStatus:
    key = (b.operator, b.jkey)
    preds = impact._lookup_journey_calls(b.operator, b.jkey)
    bpred = _worst_prediction(preds, b.stop_order)
    apred = None
    if bpred is not None:
        # The arrival from the SAME situation as the boarding pick, so the two figures describe one
        # scoring of one train; any situation's when that one did not reach the alighting stop.
        same = [p for p in preds if p.situation_number == bpred.situation_number]
        apred = _worst_prediction(same, a.stop_order) or _worst_prediction(preds, a.stop_order)

    # Recorded calls: the spine's own, overlaid per stop by the actuals index, which is fresher
    # (15 min against 60). An overlay and not a replacement: `rail_calls_today` keeps only calls
    # with an aimed DEPARTURE, so it never holds a terminus arrival, and letting it stand alone for
    # a train it knows would hide the spine's recorded terminus call.
    by_order = {c.stop_order: c for c in spine.by_key[key] if c.call_source == "recorded"}
    if actuals is not None:
        by_order.update({c.stop_order: c for c in actuals.by_key.get(key, ())})
    recorded_calls = tuple(by_order[o] for o in sorted(by_order))
    recorded_a = next((c for c in recorded_calls if c.stop_order == a.stop_order), None)
    recorded_b = next((c for c in recorded_calls if c.stop_order == b.stop_order), None)

    # R3: a trip is cancelled when EITHER end is. Vy cancels partial legs.
    cancelled = any(
        (
            b.call_cancelled,
            a.call_cancelled,
            recorded_a is not None and recorded_a.call_cancelled,
            recorded_b is not None and recorded_b.call_cancelled,
            bpred is not None and bool(bpred.et_declared_cancelled),
            apred is not None and bool(apred.et_declared_cancelled),
        )
    )

    # 1.0 for any cancellation, as `impact._cancelled_prediction` serves it -- including one only the
    # spine knows about, which has no prediction to read a figure off.
    impact_p = 1.0 if cancelled else (None if bpred is None else bpred.p_impact_dep)
    alert_worthy = cancelled or (
        bpred is not None
        and alert_policy.gate(impact_p, b.operator, has_situation=bpred.has_situation).alertworthy
    )
    common = {
        "departure_time": b.aimed_dep,
        "arrival_time": a.aimed_arr,
        "operator": b.operator,
        "line": b.line,
        "jkey": b.jkey,
        "impact_probability": impact_p,
        "alert_worthy": alert_worthy,
        "cancelled": cancelled,
    }

    if cancelled:
        # A declared cancellation is an observation only once its own time has passed
        # (`impact._is_observed`'s rule): before that it is an announcement, drawn as a ring.
        passed = now >= b.aimed_dep
        return TrainStatus(
            **common,
            state="confirmed" if passed else "predicted",
            level="red",
            delay_min=None,
            expected_departure_time=None,
            delay_p50_min=None,
            observed_at=b.aimed_dep if passed else None,
        )

    if recorded_a is not None:
        read = _confirmed_delay(recorded_calls, recorded_a)
        delay, source = read if read is not None else (None, recorded_a)
        return TrainStatus(
            **common,
            state="confirmed",
            level=_level_for_delay(delay),
            delay_min=delay,
            expected_departure_time=None,
            delay_p50_min=None,
            observed_at=source.aimed_arr or source.aimed_dep,
        )

    if bpred is None or (
        not bpred.has_situation and b.aimed_dep - now > timedelta(minutes=NOSX_HORIZON_MIN)
    ):
        return _unknown(common)

    typ = None
    if apred is not None:
        typ = apred.arr_delay_p50_stack_min
        if typ is None:
            typ = apred.arr_delay_typ_min
    if typ is None:
        typ = bpred.dep_delay_p50_stack_min
        if typ is None:
            typ = bpred.dep_delay_typ_min
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
        **common,
        state="predicted",
        level=level,
        delay_min=None,
        expected_departure_time=impact._expected_departure(bpred),
        delay_p50_min=typ,
        observed_at=None,
    )


def _unknown(common: dict) -> TrainStatus:
    return TrainStatus(
        **{**common, "alert_worthy": False},
        state="unknown",
        level="grey",
        delay_min=None,
        expected_departure_time=None,
        delay_p50_min=None,
        observed_at=None,
    )


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
