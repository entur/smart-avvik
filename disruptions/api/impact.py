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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import alternatives
from alternatives import AlternativeJourney
from pydantic import BaseModel, Field

logger = logging.getLogger("impact")

# Injected by main.lifespan: returns (snapshot_by_situation, refreshed_at).
_snapshot_provider: Callable[[], tuple[dict[str, list[CallPred]], datetime | None]] | None = None


def configure(
    *,
    snapshot_provider: Callable[[], tuple[dict[str, list[CallPred]], datetime | None]],
) -> None:
    """Wire in the per-call prediction snapshot reader. Called once from main.lifespan."""
    global _snapshot_provider
    _snapshot_provider = snapshot_provider


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
    cancel_probability: float = Field(
        description="Calibrated P(this stop is cancelled / the train does not serve it), 0-1.",
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
        description="A faster way from `from_stop` to `to_stop` than the worst affected train's "
        "predicted arrival, from the Journey Planner. Null when `to_stop` is omitted, when nothing "
        "is faster, or when the Journey Planner is unavailable. One per trip, not per line — the "
        "traveller picks one way to get there.",
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


def _departure_prediction(c: CallPred) -> StopPrediction:
    """Boarding-stop prediction: cancellation chance + departure delay at this call."""
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
    """:func:`severity_key` on a raw boarding call, for the `worst` selection strategy."""
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


def _is_observed(c: CallPred) -> bool:
    """True when ET has told us something about this call rather than us predicting it: the vehicle
    has served the stop ('recorded'), or ET has already declared the call cancelled."""
    return c.call_source == "recorded" or bool(c.et_declared_cancelled)


def _call_ts(c: CallPred) -> datetime | None:
    return c.aimed_dep or c.aimed_arr


def _latest_observation(cells: list[TripCell]) -> LatestObservation | None:
    """The furthest-along observed call across `cells`, or None when nothing has been observed.

    Prefers an observation on the traveller's own selected train — "your train is 18 min late at
    Asker" is more actionable than the same fact about a different service — and falls back to the
    line as a whole, which is what matters when their own train has not started yet.
    """
    own = [c for cell in cells for c in cell.calls]
    line = [c for cell in cells for c in cell.line_calls]
    for pool in (own, line):
        # An observation must actually say something. A recorded call at a TERMINAL has no departure,
        # so et_dep_delay_min is structurally NULL: admitting it made the PWA render "i rute" and
        # hide the line's real observed state (review 2026-07-29).
        observed = [
            c
            for c in pool
            if _is_observed(c)
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
) -> ImpactResponse:
    """Roll trip cells up into the two views the response exposes.

    Extracted from the endpoint so /varsel-api gets the same shaping (and the same severity order)
    through the in-process provider that an HTTP caller gets.
    """
    # situations: roll cells up per situation (worst-case over that situation's lines).
    by_sit: dict[str, list[TripCell]] = {}
    for cell in cells:
        by_sit.setdefault(cell.situation_number, []).append(cell)
    situations = []
    for sit_num, group in by_sit.items():
        # Which line the worst-case numbers came from, so a caller can join back to that line's
        # latest_observation (and, for /varsel, its per-operator alerting threshold).
        worst_cell = max(group, key=lambda c: severity_key(c.departure))
        situations.append(
            SituationImpact(
                situation_number=sit_num,
                summary=group[0].summary,
                description=group[0].description,
                line=worst_cell.line,
                operator=worst_cell.operator,
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
            latest_observation=_latest_observation(group),
        )
        for line, group in by_line.items()
    ]
    lines.sort(key=lambda x: severity_key(x.departure_prediction), reverse=True)

    return ImpactResponse(
        as_of=datetime.now(UTC),
        refreshed_at=refreshed_at,
        from_stop=from_stop,
        to_stop=to_stop,
        situations=situations,
        lines=lines,
        alternative_journey=alternative_journey,
    )


def _worth_an_alternative(cells: list[TripCell], min_impact: float) -> bool:
    """Whether this trip looks bad enough to spend a Journey Planner round trip on.

    The lookup is a blocking 3 s HTTPS call on the poller thread that also rebuilds every BigQuery
    cache, and the poller discards the result unless the situation crosses the operator alert gate.
    Computing it for every disruption-touched journey-window was pure waste at the ~95th percentile.
    """
    if min_impact <= 0.0:
        return True
    worst = _worst([c.departure for c in cells])
    return worst is not None and (worst.impact_probability or 0.0) >= min_impact


def _reference_arrival(cells: list[TripCell]) -> datetime | None:
    """The predicted arrival at `to_stop` of the worst affected train — the bar an alternative has
    to beat. Aimed arrival plus the predicted arrival delay, since the aimed time is not what the
    traveller will actually experience."""
    arrivals = [c.arrival for c in cells if c.arrival is not None]
    worst = _worst(arrivals)
    if worst is None or worst.scheduled_time is None:
        return None
    delay = worst.delay_p50_min if worst.delay_p50_min is not None else worst.delay_typ_min
    return worst.scheduled_time + timedelta(minutes=max(delay or 0, 0))


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

    alternative = None
    if with_alternative and to_stop is not None and cells and _worth_an_alternative(cells, min_impact):
        reference = _reference_arrival(cells)
        if reference is not None:
            # Leave from the worst affected train's aimed departure onwards: an "alternative" that
            # left before the traveller could get to the platform is not one.
            # Anchor on the train the alert is about, never earlier than now: planning from a
            # past instant returns "alternatives" that have already left.
            worst_dep = _worst([c.departure for c in cells])
            anchor = (worst_dep.scheduled_time if worst_dep else None) or window_start
            depart_after = max(anchor, datetime.now(UTC))
            alternative = alternatives.find_faster(
                from_stop,
                to_stop,
                reference_arrival=reference,
                depart_after=depart_after,
                excluded_jkeys={c.jkey for c in cells if c.jkey},
                excluded_lines={c.line for c in cells if c.line},
            )

    return _impact_response(
        cells,
        from_stop=from_stop,
        to_stop=to_stop,
        refreshed_at=refreshed_at,
        alternative_journey=alternative,
    )

