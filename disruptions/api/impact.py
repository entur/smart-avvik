"""The /impact engine: the shared matcher behind the HTTP endpoint and /varsel-api.

Extracted from main.py on 2026-07-29. Two things drove it, both review findings:

1. main.py had grown to ~2500 lines holding the API, the poller, the caches AND this engine,
   while every symbol here was referenced from nowhere else in that file -- a clean seam that
   only looked entangled because everything shared one module.
2. varsel could not TYPE its dependency on this engine, because importing main would be a cycle.
   It therefore held the one cross-module contract in the design as `Callable[..., Any]`, guarded
   by a docstring -- and that contract silently broke on 2026-07-29, killing all push alerting
   with a green test suite. With the models here, varsel imports a real Protocol and a drift is a
   type error instead of a runtime TypeError swallowed by a per-journey guard.

This module owns no state. The per-call prediction snapshot lives in main (the poller writes it);
main injects a reader via `configure()`, the same pattern reisevalg/varsel/llm_message already use.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import alert_policy
import alternatives
from alternatives import AlternativeCandidate, AlternativeJourney, AlternativePrediction
from pydantic import BaseModel, Field

logger = logging.getLogger("impact")

# Injected by main.lifespan: returns (snapshot_by_situation, refreshed_at).
_snapshot_provider: Callable[[], tuple[dict[str, list[CallPred]], datetime | None]] | None = None
# Injected alongside it: (operator, jkey) -> that journey's scored calls, or None when no live
# situation touches it. The SAME rows as the snapshot, indexed the other way -- see `configure`.
_call_lookup: Callable[[str | None, str], list[CallPred] | None] | None = None
# Whether the "no call lookup wired" warning has been emitted yet. Once per process, not once per
# candidate per refresh: the condition is a startup wiring fault, not a per-trip event.
_warned_no_call_lookup = False


def configure(
    *,
    snapshot_provider: Callable[[], tuple[dict[str, list[CallPred]], datetime | None]],
    call_lookup: Callable[[str | None, str], list[CallPred] | None] | None = None,
) -> None:
    """Wire in main's per-call prediction caches. Called once from main.lifespan.

    Two readers over the same poller-owned rows, indexed the two ways the two questions need:

    * `snapshot_provider` -> `{situationNumber: [CallPred]}`, which is how the matcher finds the
      traveller's train from a live situation.
    * `call_lookup` -> the calls of ONE journey, keyed `(operator, jkey)`, which is how
      :func:`score_candidates` asks "what do we know about this *other* train the Journey Planner
      just offered?". Scanning the snapshot for that would be O(situations x calls) per lookup, on
      the ingestion thread, over data main already indexes (`main._call_pred_by_key`).

    `call_lookup` is keyword-only with a None default because three test suites configure the
    snapshot alone. A None lookup leaves every candidate unscored and says so ONCE at WARNING,
    rather than raising the way a missing `snapshot_provider` does: `score_candidates` runs inside
    `for_trip`'s fail-open guard, which would swallow the exception and make a wiring fault
    indistinguishable from "the Journey Planner had nothing to offer" — the exact class of silent
    contract break this module was extracted to prevent.
    """
    global _snapshot_provider, _call_lookup, _warned_no_call_lookup
    _snapshot_provider = snapshot_provider
    _call_lookup = call_lookup
    _warned_no_call_lookup = False  # re-arm: a re-wiring deserves its own warning


@dataclass(frozen=True)
class CallPred:
    """One scored call (stop on a journey) within a situation's 12h window."""

    situation_number: str
    summary: str | None
    description: str | None  # the operator's own explanation; often several sentences
    jkey: str
    operator: str  # ref codespace (== SIRI ref prefix): VYG, GOA, RUT, KOL, SKY, ATB, ...
    line: str | None
    stop_place: str
    stop_name: str | None
    stop_order: int
    aimed_dep: datetime | None
    aimed_arr: datetime | None
    # OBSERVED state (what ET already declares), as opposed to everything below it (what the models
    # predict). `call_source` is 'recorded' once the vehicle has actually served the stop and
    # 'estimated' while it is still upcoming, so it is the flag that says whether the two observed
    # fields mean anything. Freshness is bounded by the planned-source TTL, not by "now" -- see
    # maybe_refresh_planned_calls.
    et_declared_cancelled: bool | None
    et_dep_delay_min: int | None
    call_source: str | None
    p_cancel: float
    dep_delay_typ_min: int | None
    dep_delay_p90_min: int | None
    arr_delay_typ_min: int | None
    arr_delay_p90_min: int | None
    # Exceedance stacks (2026-07-02): calibrated P(delay >= t | runs) per side, the interval read
    # off each survival curve, and the alert P(cancelled OR >= 10 min late) per side. The arrival
    # fields are None at an origin; the departure fields are None at a terminal.
    p_arr_ge10: float | None
    p_arr_ge20: float | None
    arr_delay_p50_stack_min: int | None
    arr_delay_p90_stack_min: int | None
    p_impact: float | None
    p_dep_ge10: float | None
    p_dep_ge20: float | None
    dep_delay_p50_stack_min: int | None
    dep_delay_p90_stack_min: int | None
    p_impact_dep: float | None


class StopPrediction(BaseModel):
    """Prediction for one stop on one train: chance it is cancelled, plus delay if it runs.

    Serves as both a `departure_prediction` (the boarding stop — `scheduled_time` is the aimed
    departure and `delay_*` the departure delay, answering "will my train leave, how late?") and an
    `arrival_prediction` (the alighting stop — `scheduled_time` is the aimed arrival and `delay_*`
    the arrival delay, answering "will I reach my destination, how late?"). `delay_p90_min` is the
    conservative "bad case".
    """

    stop_place: str = Field(description="NSR StopPlace ref of this stop.")
    stop_name: str | None = Field(description="Human-readable stop name (null if unresolved).")
    stop_order: int = Field(description="Position of this stop along the journey (1 = origin).")
    scheduled_time: datetime | None = Field(
        description="Aimed departure (departure_prediction) or arrival (arrival_prediction), UTC.",
    )
    cancelled: bool = Field(
        default=False,
        description="ET has ALREADY declared this call cancelled — a fact about the timetable, not "
        "a model output. When true the train does not serve this stop, `cancel_probability` and "
        "`impact_probability` are 1.0, and every delay field is null (they all mean 'if it runs'). "
        "A traveller-facing wording must state the cancellation, never quote a probability for it.",
    )
    cancel_probability: float = Field(
        description="Calibrated P(this stop is cancelled / the train does not serve it), 0-1; "
        "exactly 1.0 when `cancelled` says ET has already declared it.",
    )
    delay_typ_min: int | None = Field(description="Typical delay (min) at this stop if the train runs.")
    delay_p90_min: int | None = Field(
        description="Bad-case (p90) delay (min) at this stop, from the calibrated exceedance "
        "stack for that side when available.",
    )
    delay_p50_min: int | None = Field(
        default=None,
        description="Median delay (min) from the exceedance stack; with delay_p90_min this forms "
        "the 'expect X-Y min' interval.",
    )
    ge10_probability: float | None = Field(
        default=None,
        description="Calibrated P(delay >= 10 min | the train runs) on this side, 0-1.",
    )
    impact_probability: float | None = Field(
        default=None,
        description="Calibrated P(cancelled OR >= 10 min late) on this side — the recommended "
        "single alerting score.",
    )


class LatestObservation(BaseModel):
    """What has actually been *observed* on a line so far, as opposed to predicted.

    Sourced from the ET-derived planned call tables (`recordedCalls` for operated stops, plus any
    call ET has already declared cancelled) — so these are facts, not model output.

    **Freshness:** the underlying planned source is rebuilt on a TTL (60 min for rail, 120 min for
    bus), so an observation can be up to that old. `observed_at` is part of this object precisely so
    the age is visible rather than implied by the word "latest" — quote it with its time.

    **Scope:** the prediction cache only holds calls belonging to journeys a live situation touches,
    so this is the latest stop on this line *that a live situation reaches*, not a whole-line view.
    """

    cancelled: bool = Field(description="Whether ET has declared this call cancelled.")
    at_stop: str | None = Field(description="NSR StopPlace ref of the latest observed stop.")
    at_stop_name: str | None = Field(default=None, description="Human-readable name of that stop.")
    delayed_min: int | None = Field(
        description="Minutes late at `at_stop` (0 when on time or early); null if not recorded.",
    )
    observed_at: datetime | None = Field(
        default=None, description="Aimed time of the observed call (UTC) — how old this fact is.",
    )


class SituationImpact(BaseModel):
    """How one situation impacts the passenger's trip, worst-case across the lines it affects."""

    situation_number: str = Field(description="SIRI-SX situationNumber of this situation.")
    summary: str | None = Field(description="Short Norwegian summary of the situation.")
    description: str | None = Field(
        default=None,
        description="The operator's fuller Norwegian explanation; often several sentences.",
    )
    line: str | None = Field(
        default=None,
        description="Line ref the worst-case prediction below came from — which of this "
        "situation's lines the numbers describe. Look it up in `lines` for that line's "
        "`latest_observation`.",
    )
    operator: str | None = Field(
        default=None,
        description="Ref codespace of that line's journey (VYG, GOA, RUT, ...). Determines which "
        "calibration family and alerting threshold apply.",
    )
    jkey: str | None = Field(
        default=None,
        description="Journey key of the train `departure_prediction` describes — the SIRI-ET "
        "`datedVehicleJourneyRef` with its codespace prefix stripped, so `(operator, jkey, "
        "call_date, stop_order)` joins back to realized ET outcomes. Describes the DEPARTURE side "
        "only: `arrival_prediction` is picked worst-first over arrivals independently and may come "
        "from a different train. Null when the cell carried no journey ref.",
    )
    departure_prediction: StopPrediction = Field(
        description="Worst-case boarding prediction across the lines this situation affects.",
    )
    arrival_prediction: StopPrediction | None = Field(
        description="Worst-case alighting prediction; null when `to_stop` is omitted.",
    )


class LineImpact(BaseModel):
    """How one line is impacted for the passenger's trip, worst-case across the situations on it.

    The line's train is the one departing `from_stop` nearest `at`; the arrival prediction is that
    same train at `to_stop`.
    """

    line: str | None = Field(description="Line ref of the affected train, e.g. 'VYG:Line:R31'.")
    operator: str | None = Field(
        default=None,
        description="Ref codespace of the affected journey (VYG, GOA, RUT, ...). Determines which "
        "calibration family and alerting threshold apply.",
    )
    departure_prediction: StopPrediction = Field(
        description="Worst-case boarding prediction across the situations affecting this line.",
    )
    arrival_prediction: StopPrediction | None = Field(
        description="Worst-case alighting prediction; null when `to_stop` is omitted. When "
        "`to_stop` IS given, only trains that actually reach it after the boarding stop are "
        "returned at all, so this is non-null — a line whose trains all terminate short is "
        "omitted from the response rather than returned with a null arrival.",
    )
    latest_observation: LatestObservation | None = Field(
        default=None,
        description="What has actually been observed on this line so far; null when nothing has "
        "been observed yet (every affected call is still upcoming).",
    )


class ImpactResponse(BaseModel):
    """Situations and lines affecting a passenger's intended trip, per-train scored.

    Same per-train model as /journey, but trains are resolved from each situation plus the trip
    (`from_stop` + intended departure `at`, optional `to_stop`) rather than a journey ref. The
    underlying unit is a (situation × line) cell — the train on that line departing `from_stop`
    nearest `at` — exposed two ways: `situations` rolls cells up per situation (worst-case over
    its lines), `lines` rolls them up per line (worst-case over the situations affecting it), so a
    line is never listed once per situation. When `situation` is given, `situations` is length 1
    (or 0 if that situation does not affect the trip) and `lines` is its per-line breakdown.
    """

    as_of: datetime = Field(description="Server time of the response (UTC).")
    refreshed_at: datetime | None = Field(
        description="When the per-call cache was last rebuilt (UTC); null until first refresh.",
    )
    from_stop: str = Field(description="The boarding StopPlace ref queried (echoes the request).")
    to_stop: str | None = Field(description="The alighting StopPlace ref queried, if any.")
    situations: list[SituationImpact] = Field(
        description="Situations affecting the trip, most severe first; length 1 when `situation` is given.",
    )
    lines: list[LineImpact] = Field(
        description="Lines affecting the trip, most severe first; each line appears once.",
    )
    alternative_journey: AlternativeJourney | None = Field(
        default=None,
        description="The best remaining way from `from_stop` to `to_stop`, from the Journey "
        "Planner — NOT necessarily a faster one. When nothing arrives before the worst affected "
        "train's aimed arrival, the fallback tier offers the earliest arrival there is, which for "
        "a cancelled train legitimately lands after the timetable promised; `minutes_saved` is "
        "signed and `arrives_before_original` says which. Null when `to_stop` is omitted, when the "
        "Journey Planner is unavailable, and — the reading that changed — when the traveller's own "
        "train is still the best option (`original_is_best` distinguishes that from 'we did not "
        "look'). One per trip, not per line: the traveller picks one way to get there.",
    )
    # Internal working state, kept off the wire (exclude=True) the way varsel.Alert.impact is.
    original_is_best: bool = Field(
        default=False,
        exclude=True,
        repr=False,
        description="A VERDICT, not the absence of one: the selection ran and chose the "
        "traveller's own train over everything the Journey Planner offered. `alternative_journey` "
        "is null in this case and also when no lookup ran at all (no `to_stop`, below the impact "
        "gate, a Journey Planner outage), and those two must not be worded alike — 'bli sittende' "
        "is a claim, 'vi vet ikke' is not.",
    )
    alternative_subject: tuple[str | None, str] | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description="The (operator, jkey) of the train `original_is_best` is a claim about — the "
        "worst affected train across this whole response. A caller that back-fills one trip's "
        "lookup onto several alerts (varsel.match_journey) needs it: 'stay put' about a train two "
        "minutes late says nothing about a cancelled one on another ref of the same place.",
    )


@dataclass(frozen=True)
class TripCell:
    """One (situation × line) impact for the passenger's trip: the train on this line selected out
    of the boarding window, scored at the boarding stop (and the alighting stop if to_stop given).

    `calls` are the selected train's own calls and `line_calls` every call on the same line within
    the situation — the two pools :func:`_latest_observation` reads, preferring the traveller's own
    train over the line as a whole.
    """

    situation_number: str
    summary: str | None
    description: str | None
    line: str | None
    operator: str | None
    jkey: str | None
    departure: StopPrediction
    arrival: StopPrediction | None
    calls: tuple = ()
    line_calls: tuple = ()


def _certainly_cancelled(c: CallPred) -> bool:
    """ET has already declared this call cancelled: it is a fact about the timetable, not a forecast.

    The one place the distinction is named, so the three readers of it — the two predictions below,
    and `_call_severity`'s cheap restatement of their ordering — cannot drift apart.
    """
    return bool(c.et_declared_cancelled)


def _cancelled_prediction(c: CallPred, scheduled: datetime | None) -> StopPrediction:
    """The prediction for a call ET has already declared cancelled.

    Certainty *replaces* the model's scores rather than sitting beside them. The train does not
    serve this stop, so P(cancelled) is 1, the alerting score is 1, and every delay figure is
    dropped — each one means "if it runs", and this one does not.

    Both halves of that mattered on 2026-08-05, when Vy cancelled RE10 80323 Drammen–Skøyen: the
    cancellation of the 15:03 from Asker was sitting in our own cache as `et_declared_cancelled`,
    and the card still read "91 % sjanse for innstilling" for a departure the journey planner had
    already stopped offering. Worse, the first cycle scored the same known-cancelled call at 0.57 —
    *below* the alert gate — so a traveller was not told about a train we knew was gone.
    """
    return StopPrediction(
        stop_place=c.stop_place,
        stop_name=c.stop_name,
        stop_order=c.stop_order,
        scheduled_time=scheduled,
        cancelled=True,
        cancel_probability=1.0,
        delay_typ_min=None,
        delay_p90_min=None,
        delay_p50_min=None,
        ge10_probability=None,
        impact_probability=1.0,
    )


def _departure_prediction(c: CallPred) -> StopPrediction:
    """Boarding-stop prediction: cancellation chance + departure delay at this call."""
    if _certainly_cancelled(c):
        return _cancelled_prediction(c, c.aimed_dep)
    return StopPrediction(
        stop_place=c.stop_place,
        stop_name=c.stop_name,
        stop_order=c.stop_order,
        scheduled_time=c.aimed_dep,
        cancel_probability=c.p_cancel,
        delay_typ_min=c.dep_delay_typ_min,
        delay_p90_min=(
            c.dep_delay_p90_stack_min
            if c.dep_delay_p90_stack_min is not None
            else c.dep_delay_p90_min
        ),
        delay_p50_min=c.dep_delay_p50_stack_min,
        ge10_probability=c.p_dep_ge10,
        impact_probability=c.p_impact_dep,
    )


def _arrival_prediction(c: CallPred) -> StopPrediction:
    """Alighting-stop prediction: cancellation chance + arrival delay at this call."""
    if _certainly_cancelled(c):
        return _cancelled_prediction(c, c.aimed_arr)
    return StopPrediction(
        stop_place=c.stop_place,
        stop_name=c.stop_name,
        stop_order=c.stop_order,
        scheduled_time=c.aimed_arr,
        cancel_probability=c.p_cancel,
        delay_typ_min=c.arr_delay_typ_min,
        delay_p90_min=(
            c.arr_delay_p90_stack_min
            if c.arr_delay_p90_stack_min is not None
            else c.arr_delay_p90_min
        ),
        delay_p50_min=c.arr_delay_p50_stack_min,
        ge10_probability=c.p_arr_ge10,
        impact_probability=c.p_impact,
    )


def severity_key(p: StopPrediction) -> tuple[float, int, float]:
    """Order impacts most-severe first, by the *alerting* score — the calibrated impact probability
    the /varsel push gate and /demo's would_alert both fire on — then bad-case delay, then
    cancellation probability as a deterministic tie-break.

    Ordering by cancellation probability instead (as this did until 2026-07-29) could hand a
    situation's headline numbers to a train other than the one that crossed the alert threshold:
    a 5 %-cancel / 95 %-impact train is the one the traveller cares about, not a
    40 %-cancel / 42 %-impact one. Same key as varsel's matcher, which had it right.
    """
    return (p.impact_probability or 0.0, p.delay_p90_min or 0, p.cancel_probability)


def _worst(preds: list[StopPrediction | None]) -> StopPrediction | None:
    """Most severe prediction by :func:`severity_key`.

    None-valued entries (e.g. a train that does not serve the alighting stop) are ignored; returns
    None only if nothing remains."""
    present = [p for p in preds if p is not None]
    if not present:
        return None
    return max(present, key=severity_key)


def _call_severity(c: CallPred) -> tuple[float, int, float]:
    """:func:`severity_key` on a raw boarding call, for the `worst` selection strategy.

    Deliberately not `severity_key(_departure_prediction(c))`: this runs once per candidate call in
    the matcher's inner loop, and building a pydantic model there is what the 2026-07-29 O(calls x
    lines) fix was about. The cost of the shortcut is that it has to restate the certainty rule —
    a call ET has already declared cancelled scores exactly what `_cancelled_prediction` gives it,
    so a known-cancelled train outranks any forecast and is the one the alert is about.
    """
    if _certainly_cancelled(c):
        return (1.0, 0, 1.0)
    p90 = (
        c.dep_delay_p90_stack_min
        if c.dep_delay_p90_stack_min is not None
        else c.dep_delay_p90_min
    )
    return (c.p_impact_dep or 0.0, p90 or 0, c.p_cancel)


def _expected_departure(c: CallPred) -> datetime | None:
    """When the vehicle is expected to actually leave this stop: aimed departure plus the predicted
    typical delay. A train whose aimed time has passed can still be catchable if it is predicted
    25 min late, which is why this is not simply a comparison against the aimed time."""
    if c.aimed_dep is None:
        return None
    typ = (
        c.dep_delay_p50_stack_min
        if c.dep_delay_p50_stack_min is not None
        else c.dep_delay_typ_min
    )
    return c.aimed_dep + timedelta(minutes=max(typ or 0, 0))


def _trip_cells(
    scope: dict[str, list[CallPred]],
    from_stop: str,
    to_stop: str | None,
    window_start: datetime,
    window_end: datetime,
    *,
    select: str = "nearest",
    at: datetime | None = None,
    catchable_after: datetime | None = None,
) -> list[TripCell]:
    """Build one (situation × line) cell per line with a departure from `from_stop` in the window.

    A boarding call qualifies when its aimed departure falls inside the inclusive
    ``[window_start, window_end]`` window and the same train later calls at `to_stop` (a higher
    stop_order — the travel direction). Situations with no qualifying departure are skipped.

    The window is the primitive because the two callers ask different questions of the same data:
    ``/impact`` asks about one intended departure (window = ``at`` ± :data:`_IMPACT_AT_TOLERANCE`),
    while a registered /varsel journey is a recurring ``[begin, end]`` window. `select` is how they
    differ on which train to keep per line:

    * ``"nearest"`` — the boarding call closest to `at` (which is then required). What a passenger
      asking "my train leaves about now" means.
    * ``"worst"`` — the most severe boarding call by :func:`_call_severity`. What an alert gate
      means: if any train in the window is alert-worthy, the traveller needs to know.

    `catchable_after` drops boardings whose *expected* departure has already passed. It belongs here,
    in candidate selection, and not in a post-filter on the rolled-up result: filtering afterwards
    would let a departed worst-case train suppress the whole situation, hiding a later train on the
    same line that is both still catchable and alert-worthy. (That is the same ordering trap as the
    `to_stop` eligibility check below — worth stating twice, since it was made twice.)
    """
    if select == "nearest" and at is None:
        raise ValueError("select='nearest' requires `at` (the intended departure to measure from)")
    if select not in ("nearest", "worst"):
        raise ValueError(f"unknown boarding selection strategy: {select!r}")

    def better(candidate: CallPred, current: CallPred) -> bool:
        if select == "nearest":
            return abs(candidate.aimed_dep - at) < abs(current.aimed_dep - at)
        # Tie-break on aimed departure then journey ref: without it an exact three-way severity tie
        # resolves on BigQuery row order, so the reported train could flip between refreshes.
        return (_call_severity(candidate), -candidate.aimed_dep.timestamp(), candidate.jkey) > (
            _call_severity(current),
            -current.aimed_dep.timestamp(),
            current.jkey,
        )

    cells: list[TripCell] = []
    for calls in scope.values():
        # Group by (operator, jkey): bus ServiceJourney refs are not date-scoped, so one situation
        # (e.g. a notice fanning across county codespaces) can carry the same jkey for two
        # operators' journeys — keying on jkey alone would merge them and let one train's boarding
        # ride the other's direction.
        by_journey: dict[tuple[str, str], list[CallPred]] = {}
        by_line_all: dict[str | None, list[CallPred]] = {}
        for c in calls:
            by_journey.setdefault((c.operator, c.jkey), []).append(c)
            by_line_all.setdefault(c.line, []).append(c)

        # Eligibility BEFORE per-line selection. Selecting first and checking the destination after
        # would drop a whole line whenever its most severe train happens not to serve `to_stop`,
        # silently losing an alert for a less severe train on that line that does.
        eligible: list[tuple[CallPred, CallPred | None]] = []
        for group in by_journey.values():
            bcall = None
            for c in group:
                if c.stop_place != from_stop or c.aimed_dep is None:
                    continue
                if not (window_start <= c.aimed_dep <= window_end):
                    continue
                if catchable_after is not None:
                    expected = _expected_departure(c)
                    if expected is not None and expected < catchable_after:
                        continue  # already gone -- cannot be the traveller's train
                if bcall is None or better(c, bcall):
                    bcall = c
            if bcall is None:
                continue
            acall = None
            if to_stop is not None:
                acall = next(
                    (
                        c
                        for c in group
                        if c.stop_place == to_stop
                        and c.aimed_arr is not None
                        and c.stop_order > bcall.stop_order
                    ),
                    None,
                )
                if acall is None:
                    continue  # this train does not reach to_stop in the travel direction
            eligible.append((bcall, acall))

        # Per line, keep one eligible train per the selection strategy.
        by_line: dict[str | None, tuple[CallPred, CallPred | None]] = {}
        for bcall, acall in eligible:
            cur = by_line.get(bcall.line)
            if cur is None or better(bcall, cur[0]):
                by_line[bcall.line] = (bcall, acall)

        for line, (bcall, acall) in by_line.items():
            cells.append(
                TripCell(
                    situation_number=bcall.situation_number,
                    summary=bcall.summary,
                    description=bcall.description,
                    line=line,
                    operator=bcall.operator,
                    jkey=bcall.jkey,
                    departure=_departure_prediction(bcall),
                    arrival=_arrival_prediction(acall) if acall is not None else None,
                    # Reuse the groupings built above rather than re-scanning the situation's whole
                    # call list once per emitted cell (that made a single O(calls) pass into
                    # O(calls x lines), on both the /impact request path and every poller journey).
                    calls=tuple(by_journey.get((bcall.operator, bcall.jkey), ())),
                    line_calls=tuple(by_line_all.get(line, ())),
                )
            )
    return cells


def _call_ts(c: CallPred) -> datetime | None:
    return c.aimed_dep or c.aimed_arr


def _is_observed(c: CallPred, now: datetime) -> bool:
    """True when ET has told us something about this call rather than us predicting it.

    Two ways that happens, and only one of them is time-free. A `recorded` call is one the vehicle
    has actually served, so it is an observation by construction. A declared cancellation is not:
    operators publish it for calls the train has NOT reached yet — announcing "innstilt
    Drammen–Skøyen" ahead of time is the entire point of the message — so it becomes an observation
    only once its own time has passed.

    Without that cut, an announcement was rendered as a sighting. On 2026-08-05 the card read
    "Sist observert: Lysaker stasjon, innstilt (kl 15:15)" at 14:28 — a stop 47 minutes in the
    future, chosen precisely *because* `_latest_observation` takes the furthest-along call. The
    model then repeated it, having been told by the prompt that the timestamp is in the past.

    An upcoming cancellation is not lost by this; it is reported where a fact about the future
    belongs, as a certainty on the prediction itself (:func:`_cancelled_prediction`).
    """
    if c.call_source == "recorded":
        return True
    ts = _call_ts(c)
    return bool(c.et_declared_cancelled) and ts is not None and ts <= now


def _latest_observation(cells: list[TripCell], *, now: datetime | None = None) -> LatestObservation | None:
    """The furthest-along observed call across `cells`, or None when nothing has been observed.

    Prefers an observation on the traveller's own selected train — "your train is 18 min late at
    Asker" is more actionable than the same fact about a different service — and falls back to the
    line as a whole, which is what matters when their own train has not started yet.

    `now` is the cut that keeps an observation in the past (see :func:`_is_observed`); it defaults to
    the current time and is passed explicitly by :func:`_impact_response` so one response cannot
    straddle two instants.
    """
    if now is None:
        now = datetime.now(UTC)
    own = [c for cell in cells for c in cell.calls]
    line = [c for cell in cells for c in cell.line_calls]
    for pool in (own, line):
        # An observation must actually say something. A recorded call at a TERMINAL has no departure,
        # so et_dep_delay_min is structurally NULL: admitting it made the PWA render "i rute" and
        # hide the line's real observed state (review 2026-07-29).
        observed = [
            c
            for c in pool
            if _is_observed(c, now)
            and _call_ts(c) is not None
            and (c.et_dep_delay_min is not None or c.et_declared_cancelled)
        ]
        if not observed:
            continue
        best = max(observed, key=lambda c: (_call_ts(c), c.stop_order))
        return LatestObservation(
            cancelled=bool(best.et_declared_cancelled),
            at_stop=best.stop_place,
            at_stop_name=best.stop_name,
            # Clamped at 0: a vehicle that left early is not "delayed -2 min", and this field is
            # read as a delay by both the PWA and the message writer.
            delayed_min=(
                max(best.et_dep_delay_min, 0) if best.et_dep_delay_min is not None else None
            ),
            observed_at=_call_ts(best),
        )
    return None


def _impact_response(
    cells: list[TripCell],
    *,
    from_stop: str,
    to_stop: str | None,
    refreshed_at: datetime | None,
    alternative_journey: AlternativeJourney | None,
    now: datetime | None = None,
    original_is_best: bool = False,
    alternative_subject: tuple[str | None, str] | None = None,
) -> ImpactResponse:
    """Roll trip cells up into the two views the response exposes.

    Extracted from the endpoint so /varsel-api gets the same shaping (and the same severity order)
    through the in-process provider that an HTTP caller gets.

    `now` is read twice — as the response's `as_of` and as the cut that keeps a `latest_observation`
    in the past — so it is taken once here rather than sampled per use.
    """
    if now is None:
        now = datetime.now(UTC)
    # situations: roll cells up per situation (worst-case over that situation's lines).
    by_sit: dict[str, list[TripCell]] = {}
    for cell in cells:
        by_sit.setdefault(cell.situation_number, []).append(cell)
    situations = []
    for sit_num, group in by_sit.items():
        # Which line the worst-case numbers came from, so a caller can join back to that line's
        # latest_observation (and, for /varsel, its per-operator alerting threshold).
        # `_worst` is max-by-severity_key over the same group in the same order, so worst_cell IS
        # the cell behind departure_prediction -- which is what makes carrying its jkey honest
        # rather than approximate. Keep the two selections in step if either ever changes.
        worst_cell = max(group, key=lambda c: severity_key(c.departure))
        situations.append(
            SituationImpact(
                situation_number=sit_num,
                summary=group[0].summary,
                description=group[0].description,
                line=worst_cell.line,
                operator=worst_cell.operator,
                jkey=worst_cell.jkey,
                departure_prediction=_worst([c.departure for c in group]),
                arrival_prediction=_worst([c.arrival for c in group]),
            )
        )
    situations.sort(key=lambda s: severity_key(s.departure_prediction), reverse=True)

    # lines: roll cells up per line (worst-case over the situations affecting that line).
    by_line: dict[str | None, list[TripCell]] = {}
    for cell in cells:
        by_line.setdefault(cell.line, []).append(cell)
    lines = [
        LineImpact(
            line=line,
            operator=group[0].operator,
            departure_prediction=_worst([c.departure for c in group]),
            arrival_prediction=_worst([c.arrival for c in group]),
            latest_observation=_latest_observation(group, now=now),
        )
        for line, group in by_line.items()
    ]
    lines.sort(key=lambda x: severity_key(x.departure_prediction), reverse=True)

    return ImpactResponse(
        as_of=now,
        refreshed_at=refreshed_at,
        from_stop=from_stop,
        to_stop=to_stop,
        situations=situations,
        lines=lines,
        alternative_journey=alternative_journey,
        original_is_best=original_is_best,
        alternative_subject=alternative_subject,
    )


def _worth_an_alternative(cells: list[TripCell], min_impact: float) -> bool:
    """Whether this trip looks bad enough to spend a Journey Planner round trip on.

    The lookup is a blocking 3 s HTTPS call on the poller thread that also rebuilds every BigQuery
    cache, and the poller discards the result unless the situation crosses the operator alert gate.
    Computing it for every disruption-touched journey-window was pure waste at the ~95th percentile.

    **This gate is already wide enough; do not widen it again.** `varsel` passes
    `alert_policy.min_threshold()` (0.50 — GOA's cut, the floor of the whole per-operator table), so
    every trip that could possibly earn a push is at or above it, and an ET-declared cancellation
    scores exactly 1.0. A trip that reaches this gate and gets no alternative is a *selection*
    outcome, not a screening one, and lowering the number here would only buy blocking round trips
    for journeys nobody is ever told about.
    """
    if min_impact <= 0.0:
        return True
    worst = _worst([c.departure for c in cells])
    return worst is not None and (worst.impact_probability or 0.0) >= min_impact


def _worst_cell(cells: list[TripCell]) -> TripCell | None:
    """The cell the trip's headline numbers come from: the worst affected train, by DEPARTURE.

    One cell, read four ways (aimed departure, aimed arrival, predicted arrival, and the flags that
    decide whether the traveller is in real trouble) — which is the whole point of it being a
    function rather than four independent `_worst` calls.

    `SituationImpact.jkey` already documents why: `arrival_prediction` is picked worst-first over
    arrivals **independently and may come from a different train**. Under the rule this replaced,
    that only shifted a filter — the reference arrival was a bar an alternative had to beat, and a
    bar in the wrong place cost at most a suggestion. Under `select_best` the same time is the
    traveller's *deadline*, and it is what decides which candidate wins: a worst-arrival train
    running on some other line would push the deadline hours out and hand the answer to whatever
    departs last before it.

    Same key and same order as `_impact_response`'s per-situation rollup, so "the worst train" means
    one thing across the response.
    """
    if not cells:
        return None
    return max(cells, key=lambda c: severity_key(c.departure))


def _predicted_arrival(cell: TripCell) -> datetime | None:
    """When `cell`'s train is actually expected at `to_stop`: aimed arrival plus its predicted delay.

    Distinct from the aimed arrival, and the two must not be collapsed. The aimed time is the
    traveller's deadline (`select_best`'s `original_arrival_time`); this one is what we expect of
    the train they have (`original_predicted_arrival`), and what `minutes_saved` measures against.
    Passing the predicted time as the deadline would let a delayed train move the appointment.
    """
    arrival = cell.arrival
    if arrival is None or arrival.scheduled_time is None:
        return None
    delay = arrival.delay_p50_min if arrival.delay_p50_min is not None else arrival.delay_typ_min
    return arrival.scheduled_time + timedelta(minutes=max(delay or 0, 0))


# ---- Scoring Journey Planner candidates -------------------------------------------------------
# The Journey Planner tells us what a traveller *could* take instead. It does not know what our own
# models think of those trips, and offering a replacement that is itself about to be alerted on is
# worse than saying nothing. This section answers two questions per candidate — "is it already
# gone?" and "would we alert on it?" — from the same per-call predictions that scored the disrupted
# train, plus the same operator gate `varsel` pushes on (`alert_policy`).


@dataclass(frozen=True)
class CandidateScore:
    """One Journey Planner candidate after scoring, with what we know about the two ends it is
    ridden between.

    `candidate` is the SAME object that was passed in: `risky` and `cancelled` are annotated in
    place rather than returned on a copy. That is deliberate — `alternatives.select_best` recognises
    the disrupted train by identity (`cand is original`), so handing selection copies would let the
    original be appended to the pool a second time and beat its own replacement.

    `arrival_prediction` is None whenever no alighting call resolved. It never borrows the
    departure's numbers: "when will I get there" answered with the departure's delay would be a
    fabricated figure, and the JP-only shape already exists for "we do not know".
    """

    candidate: AlternativeCandidate
    departure_prediction: AlternativePrediction
    arrival_prediction: AlternativePrediction | None


def _lookup_journey_calls(operator: str | None, jkey: str) -> list[CallPred]:
    """Scored calls of one journey, or `[]` — including when nothing is wired up.

    The empty list is the ordinary answer, not a failure: the prediction cache only holds calls
    belonging to journeys a live situation touches, so the great majority of alternatives are simply
    unscored. An unwired lookup returns the same thing but says so once, because "nobody called
    configure" and "no situation touches this train" must not look alike in the logs.
    """
    global _warned_no_call_lookup
    if _call_lookup is None:
        if not _warned_no_call_lookup:
            _warned_no_call_lookup = True
            logger.warning(
                "impact.configure(call_lookup=...) was never called; every Journey Planner "
                "alternative will be offered unscored",
                extra={"fields": {"event": "impact_scoring_unwired"}},
            )
        return []
    return list(_call_lookup(operator, jkey) or ())


def _worst_call(calls: Iterable[CallPred]) -> CallPred | None:
    """The most severe of several scored rows for one call, or None when there are none.

    One call carries one row per situation that touches it, so a train caught by two messages
    appears twice with two scores. Taking the worst is the same choice `_trip_cells` makes, by the
    same key.
    """
    present = list(calls)
    return max(present, key=_call_severity) if present else None


def _alert_score(c: CallPred) -> float:
    """How alert-worthy one call is, on the scale `alert_policy.threshold_for` cuts.

    Both sides, because a candidate is ridden *to* its alighting stop as much as *from* its boarding
    one: `p_impact_dep` is structurally None at a terminal and `p_impact` is None at an origin, so
    reading one side alone would score a candidate's destination call at zero. The operating point
    is the same on either side (eval/impact_eval.py: 0.65 -> ~91 % precision, arrival and departure
    alike), which is what makes one gate legitimate over both.

    A call ET has already declared cancelled scores 1.0 — the certainty rule
    :func:`_cancelled_prediction` states and :func:`_call_severity` restates, restated once more
    here so a known-cancelled alternative can never read as safe.
    """
    if _certainly_cancelled(c):
        return 1.0
    return max(c.p_impact_dep or 0.0, c.p_impact or 0.0)


def _observed_delay_min(aimed: datetime | None, expected: datetime | None) -> int | None:
    """Expected minus aimed, in minutes, clamped at 0 — a vehicle running early is not late.

    Both times are the Journey Planner's, so this is an OBSERVATION of the published realtime
    picture and never a forecast; the forecast lives in the probabilities beside it.
    """
    if aimed is None or expected is None:
        return None
    return max(round((expected - aimed).total_seconds() / 60), 0)


def _alternative_prediction(
    *,
    pred: StopPrediction | None,
    stop_place: str | None,
    aimed: datetime | None,
    expected: datetime | None,
    jp_cancelled: bool,
) -> AlternativePrediction:
    """Fuse what JP observed about one end of a candidate with what our models forecast for it.

    `pred` is None when the prediction cache had no row for that call — the ordinary case. Its
    probabilities then stay **null**, never 0.0: a zero would assert a calibrated certainty of
    safety that nothing established, which is the fact-vs-forecast confusion this codebase spent
    2026-08-05 removing. `scored` says which of the two it is without a reader having to infer it
    from the absence of a number.

    The times are JP's own (the pattern's endpoints, so a walk to the platform is included — it is
    part of the trip the traveller takes, and JP publishes an aimed time only at that level). When
    the call is cancelled the delay goes with it: every delay figure means "if it runs", and
    "innstilt, 3 min forsinket" is the wording that rule exists to prevent.
    """
    cancelled = bool(jp_cancelled) or bool(pred is not None and pred.cancelled)
    return AlternativePrediction(
        stop_place=pred.stop_place if pred is not None else stop_place,
        stop_name=pred.stop_name if pred is not None else None,
        scheduled_time=aimed,
        expected_time=expected,
        delay_min=None if cancelled else _observed_delay_min(aimed, expected),
        cancelled=cancelled,
        cancel_probability=pred.cancel_probability if pred is not None else None,
        impact_probability=pred.impact_probability if pred is not None else None,
        scored=pred is not None,
    )


def _score_candidate(
    cand: AlternativeCandidate,
    *,
    affected_refs: set[tuple[str | None, str]],
    affected_lines: set[str],
) -> CandidateScore:
    """Resolve one candidate against the per-call index and annotate it in place."""
    # Every transit ref, not just the first: a two-leg suggestion whose connection is about to be
    # cancelled must not be scored as safe. A leg contributes both its dated and its plain
    # service-journey ref (which form the cache holds depends on the family), so at most one of the
    # two resolves and the other lands in `unresolved` harmlessly — it is never in `affected_refs`
    # either, since those carry the cache's own key form.
    resolved: list[CallPred] = []
    unresolved: list[tuple[str | None, str]] = []
    for operator, jkey in cand.transit_refs:
        calls = _lookup_journey_calls(operator, jkey)
        if calls:
            resolved.extend(calls)
        else:
            unresolved.append((operator, jkey))

    board = None
    if cand.board_stop_place is not None:
        board = _worst_call(c for c in resolved if c.stop_place == cand.board_stop_place)
    alight = None
    if cand.alight_stop_place is not None:
        alight = _worst_call(
            c
            for c in resolved
            # No aimed arrival means an origin-side row, whose arrival figures are structurally
            # null — it would answer "when do I get there" with a column of Nones.
            if c.stop_place == cand.alight_stop_place and c.aimed_arr is not None
            # Direction, and only within one journey: on a multi-leg trip the two ends belong to
            # different journeys and their stop_orders are not comparable at all.
            and not (
                board is not None
                and (c.operator, c.jkey) == (board.operator, board.jkey)
                and c.stop_order <= board.stop_order
            )
        )

    departure = _alternative_prediction(
        pred=_departure_prediction(board) if board is not None else None,
        stop_place=cand.board_stop_place,
        aimed=cand.aimed_departure_time,
        expected=cand.departure_time,
        jp_cancelled=cand.cancelled,
    )
    arrival = (
        _alternative_prediction(
            pred=_arrival_prediction(alight),
            stop_place=cand.alight_stop_place,
            aimed=cand.aimed_arrival_time,
            expected=cand.arrival_time,
            jp_cancelled=cand.cancelled,
        )
        if alight is not None
        else None
    )

    # Score first where we have one; fall back to line membership only where we do not. The
    # asymmetry is the whole point, and both halves of it are load-bearing:
    #
    # * Marking every leg on an affected LINE risky regardless of its score would reinstate the
    #   whole-line veto this change exists to remove — it is what threw away "ta neste tog", the
    #   operator's own advice in the 2026-08-05 Vy RE10 case. A next train our own models score at
    #   0.02 would lose to a candidate we know nothing about.
    # * Marking unscored legs safe would fail the other way: an unknown sibling on the disrupted
    #   line would beat a scored clean one.
    #
    # The gate is the FOUND CALL's operator, never the JP codespace: `threshold_for` is calibrated
    # per operator against that operator's own predictions.
    risky = any(_alert_score(c) >= alert_policy.threshold_for(c.operator) for c in resolved)
    if not risky:
        # A leg is identified here by its line ref, which is the one key both the candidate and a
        # CallPred carry (JP legs expose no service-journey ref through `AlternativeLeg`). A line no
        # resolved call sits on is a leg nothing scored.
        scored_lines = {c.line for c in resolved if c.line}
        risky = any(
            line in affected_lines for line in cand.lines if line not in scored_lines
        ) or any(ref in affected_refs for ref in unresolved)

    # Cancelled is about the two ends the traveller actually uses, plus JP's own flag. A partial
    # cancellation somewhere else on a journey we ride makes it RISKY (via `_alert_score`, which
    # scores a declared cancellation at 1.0) rather than unusable — dropping it outright would veto
    # the very "take the next train" advice the operator is publishing.
    cand.cancelled = cand.cancelled or departure.cancelled or bool(arrival and arrival.cancelled)
    cand.risky = cand.risky or risky
    # Annotated in place as well as returned. `alternatives.select_best` answers with an
    # `AlternativeJourney`, not with the candidate that won, so nothing downstream can join the
    # winner back to its `CandidateScore` — identity is the only key and the journey does not carry
    # it. Riding on the candidate is what lets the chosen journey publish its own numbers.
    cand.departure_prediction = departure
    cand.arrival_prediction = arrival
    return CandidateScore(candidate=cand, departure_prediction=departure, arrival_prediction=arrival)


def score_candidates(
    candidates: Iterable[AlternativeCandidate],
    *,
    affected_refs: set[tuple[str | None, str]],
    affected_lines: set[str],
) -> list[CandidateScore]:
    """Score Journey Planner candidates against the live per-call predictions.

    Answers, per candidate: is it already cancelled, would we alert on it (`risky`), and what do our
    models say about its boarding and alighting calls. `risky` and `cancelled` are written back onto
    each candidate — that is what `alternatives.select_best` reads — while the two predictions come
    back on the returned :class:`CandidateScore` for the winner to carry into the response.

    `affected_refs` and `affected_lines` are what the live situations touch, passed explicitly the
    way `for_trip` already passes them to the alternative lookup. They only ever decide the
    *unscored* legs: a leg we did score is judged by its score, however affected its line is.

    Fail open per candidate. This is the third boundary on this path — `alternatives` guards the
    fetch inside its TTL cache and the parse after it — and it needs its own for the same reason:
    one uninterpretable candidate must cost that candidate, not every alternative for the trip.
    """
    refs = set(affected_refs or ())
    lines = set(affected_lines or ())
    scored: list[CandidateScore] = []
    for cand in candidates:
        try:
            scored.append(_score_candidate(cand, affected_refs=refs, affected_lines=lines))
        except Exception:
            logger.warning(
                "Could not score a Journey Planner candidate (#%s); dropping it",
                getattr(cand, "ordinal", "?"),
                exc_info=True,
                extra={"fields": {"event": "alternatives_error", "reason": "scoring_failed"}},
            )
    return scored


def _original_candidate(
    cell: TripCell,
    *,
    from_stop: str,
    to_stop: str,
    deadline: datetime,
    predicted_arrival: datetime,
) -> AlternativeCandidate:
    """The disrupted train, expressed as a candidate so it can compete for its own replacement.

    Synthesized from our own scoring rather than read out of JP's reply, because JP knows only the
    published realtime picture and this train's whole problem is what we predict about it. Every
    field comes from ONE cell (:func:`_worst_cell`) — see the note there on why mixing cells breaks
    the deadline.

    `risky` is the operator's own alert gate applied to the boarding call: the same question
    `score_candidates` asks of every rival, so "is the traveller in real trouble" is decided by one
    rule on both sides of the comparison. With `cancelled` it is what releases `select_best`'s
    materiality gate — a train that is merely two minutes late has to be beaten by three minutes
    before we tell anyone to abandon it; a cancelled one does not.
    """
    return AlternativeCandidate(
        # The AIMED departure: what the traveller has in their calendar, and the time the rival
        # candidates' own departures are compared against. Its expected departure is not used —
        # `catchable_after` upstream already established that this train can still be caught.
        departure_time=cell.departure.scheduled_time,
        aimed_departure_time=cell.departure.scheduled_time,
        # The PREDICTED arrival: aimed plus the delay we forecast. The aimed arrival is the
        # deadline, and is passed to `select_best` separately.
        arrival_time=predicted_arrival,
        aimed_arrival_time=deadline,
        cancelled=cell.departure.cancelled,
        risky=(cell.departure.impact_probability or 0.0)
        >= alert_policy.threshold_for(cell.operator),
        is_original=True,
        transit_refs=[(cell.operator, cell.jkey)] if cell.jkey else [],
        lines=[cell.line] if cell.line else [],
        board_stop_place=from_stop,
        alight_stop_place=to_stop,
        # Out of JP's 0-based band, so the original never shares a tie-break rank with a real
        # pattern. `select_best` breaks ties in the original's favour anyway; this keeps the two
        # from having to agree.
        ordinal=-1,
    )


def _usable(cand: AlternativeCandidate, *, affected_refs: set[tuple[str | None, str]], now: datetime) -> bool:
    """Whether `cand` is a live option at all — the cut that decides if a SECOND query is needed.

    Deliberately a subset of `select_best`'s own pool filter, not a copy of it. What it has to
    answer is narrower: "did this query come back with anything the traveller could actually take?"
    A reply consisting solely of the disrupted train's own pattern, or of departures that have
    already gone, is an empty answer wearing a full one — and it is exactly the case the
    depart-after fallback exists to rescue. The window bound stays in `select_best` alone: a
    candidate that reaches too far past the traveller's window is still evidence that the query
    answered, and re-asking would not produce an earlier one that this query missed.
    """
    if cand.cancelled or cand.arrival_time is None:
        return False
    if cand.departure_time is None or cand.departure_time <= now:
        return False
    return not any(ref in affected_refs for ref in cand.transit_refs)


def _reconcile_original_arrival(
    original: AlternativeCandidate,
    candidates: Iterable[AlternativeCandidate],
    *,
    subject: tuple[str | None, str] | None,
) -> None:
    """Take the LATER of JP's expectation for the disrupted train and our own prediction of it.

    JP's reply routinely contains the disrupted train's own pattern (it is a legitimate way to get
    there, and the trip search does not know we are alerting on it). That gives two readings of one
    arrival: our p50 exceedance stack, and the operator's published realtime picture. They disagree.

    Resolved pessimistically, and only in that direction. This time is what every rival is measured
    against, so an optimistic reading of it is what produces "stay put" — and the cost of being
    wrong is asymmetric: telling a traveller their train is fine when it is not strands them, while
    offering an alternative to a train that turns out fine costs them a choice they can decline.
    """
    if subject is None or original.arrival_time is None:
        return
    own = next(
        (c for c in candidates if not c.is_original and subject in c.transit_refs), None
    )
    if own is None or own.arrival_time is None:
        return
    if abs((own.arrival_time - original.arrival_time).total_seconds()) >= 60:
        logger.info(
            "The Journey Planner and our own models disagree about the disrupted train's arrival",
            extra={
                "fields": {
                    "event": "alternative_original_arrival",
                    "jp_expected": own.arrival_time.isoformat(),
                    "predicted": original.arrival_time.isoformat(),
                    "operator": subject[0],
                    "jkey": subject[1],
                }
            },
        )
    original.arrival_time = max(own.arrival_time, original.arrival_time)


def _best_remaining_journey(
    cells: list[TripCell],
    *,
    from_stop: str,
    to_stop: str,
    window_end: datetime,
    now: datetime,
) -> tuple[AlternativeJourney | None, bool, tuple[str | None, str] | None]:
    """Fetch, score and choose: the best remaining way to `to_stop`, or "stay put".

    Returns `(journey, original_is_best, subject)`. The middle value is a VERDICT and is only ever
    True when a real pool was compared against: a null journey with `original_is_best=False` means
    nothing could be established, and the two must not be worded alike downstream.

    **Two queries, in this order, and the second one only when the first came back empty.**
    Query 1 is anchored on the traveller's *deadline* (`arriveBy` the disrupted train's aimed
    arrival), because a depart-after search cannot contain a journey that leaves earlier and still
    gets there in time — and leaving earlier is what actually rescues somebody whose train is
    cancelled. Query 2 anchors on `now` and is the fallback for the case where nothing arrives in
    time at all, which is both the least pleasant answer and the one the traveller most needs.
    """
    cell = _worst_cell(cells)
    if cell is None or cell.arrival is None:
        return None, False, None
    deadline = cell.arrival.scheduled_time
    predicted_arrival = _predicted_arrival(cell)
    if deadline is None or predicted_arrival is None:
        return None, False, None
    if not alternatives.within_lookahead(deadline, now):
        # Checked here as well as inside `fetch_candidates`: the fallback query is anchored at
        # `now`, so it clears the horizon guard on its own and would fire for a trip hours away.
        return None, False, None

    subject = (cell.operator, cell.jkey) if cell.jkey else None
    affected_refs = {(c.operator, c.jkey) for c in cells if c.jkey}
    affected_lines = {c.line for c in cells if c.line}
    original = _original_candidate(
        cell,
        from_stop=from_stop,
        to_stop=to_stop,
        deadline=deadline,
        predicted_arrival=predicted_arrival,
    )

    def scored_pool(candidates: list[AlternativeCandidate]) -> list[AlternativeCandidate]:
        scored = score_candidates(
            candidates, affected_refs=affected_refs, affected_lines=affected_lines
        )
        return [s.candidate for s in scored if _usable(s.candidate, affected_refs=affected_refs, now=now)]

    by_deadline = alternatives.fetch_candidates(
        from_stop, to_stop, arrive_by=True, date_time=deadline, now=now
    )
    _reconcile_original_arrival(original, by_deadline, subject=subject)
    pool = scored_pool(by_deadline)
    if not pool:
        pool = scored_pool(
            alternatives.fetch_candidates(
                from_stop, to_stop, arrive_by=False, date_time=now, now=now
            )
        )

    best = alternatives.select_best(
        pool,
        original=original,
        original_arrival_time=deadline,
        # The reconciled figure, not the raw prediction: whichever of the two readings is later.
        original_predicted_arrival=original.arrival_time,
        now=now,
        window_end=window_end,
        affected_refs=affected_refs,
    )
    # "The original won" is only a claim when it had something to win against. An empty pool means
    # the Journey Planner offered nothing usable, which is silence rather than endorsement.
    return best, best is None and bool(pool), subject


def for_trip(
    *,
    from_stop: str,
    to_stop: str | None,
    window_start: datetime,
    window_end: datetime,
    select: Literal["nearest", "worst"] = "nearest",
    at: datetime | None = None,
    situation: str | None = None,
    catchable_after: datetime | None = None,
    with_alternative: bool = True,
    min_impact: float = 0.0,
) -> ImpactResponse:
    """Score a trip against the live per-call cache and shape the /impact response.

    The single entry point for both surfaces: the HTTP endpoint calls it with a point-in-time window
    derived from `at`, and varsel calls it with a registered journey's recurring window. Keeping one
    entry point is what stops the two from drifting apart again.

    `min_impact` gates the Journey Planner lookup on how bad the trip actually looks. The poller
    passes the journey's operator alert threshold, because it only ever KEEPS the alternative for a
    situation that crossed that gate — without this, every disruption-touched journey-window paid a
    blocking 3 s HTTPS round trip on the ingestion thread to compute a value that was then discarded.
    The HTTP endpoint leaves it at 0.0: a caller who asked for `to_stop` wants the answer regardless.
    """
    if _snapshot_provider is None:
        raise RuntimeError("impact.configure(snapshot_provider=...) was never called")
    now = datetime.now(UTC)
    by_situation, refreshed_at = _snapshot_provider()
    if situation is not None:
        calls = by_situation.get(situation)
        scope = {situation: calls} if calls else {}
    else:
        scope = by_situation

    cells = _trip_cells(
        scope,
        from_stop,
        to_stop,
        window_start,
        window_end,
        select=select,
        at=at,
        catchable_after=catchable_after,
    )

    alternative: AlternativeJourney | None = None
    original_is_best = False
    subject: tuple[str | None, str] | None = None
    if with_alternative and to_stop is not None and cells and _worth_an_alternative(cells, min_impact):
        # ONE guard around fetch, scoring AND selection. Not bookkeeping: this module fails open,
        # and everything below it runs on the poller thread inside varsel.check_and_notify's
        # per-journey try/except. On 2026-07-29 a single malformed Journey Planner pattern raised
        # out of the lookup, through main._impact_for_trip, and into that guard — which turned "the
        # alternative could not be computed" into "every alert for this journey is silently dropped
        # for the cycle". The guard that caught it lived inside the function this replaced, and this
        # path adds two more raise sources (`score_candidates`, `select_best`) on top of the fetch.
        # A bad alternative must cost the alternative and nothing else.
        try:
            alternative, original_is_best, subject = _best_remaining_journey(
                cells, from_stop=from_stop, to_stop=to_stop, window_end=window_end, now=now
            )
        except Exception:
            logger.warning(
                "Could not choose an alternative for %s -> %s; offering none",
                from_stop,
                to_stop,
                exc_info=True,
                extra={"fields": {"event": "alternatives_error", "reason": "selection_failed"}},
            )
            alternative, original_is_best, subject = None, False, None

    return _impact_response(
        cells,
        from_stop=from_stop,
        to_stop=to_stop,
        refreshed_at=refreshed_at,
        alternative_journey=alternative,
        now=now,
        original_is_best=original_is_best,
        alternative_subject=subject,
    )

