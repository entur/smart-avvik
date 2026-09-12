"""Unit checks for the /impact trip-cell matcher and response shaping.

Runs standalone (`uv run --with fastapi --with pydantic python test_impact.py`) or under pytest.
Hermetic: the BigQuery client is stubbed out before main is imported, so nothing here touches
BigQuery or the network.

Covers the window matcher (edges, direction, per-line selection strategy, operator/jkey carry),
the severity ordering fix (impact probability first, not cancellation probability),
latest_observation resolution, the scoring of Journey Planner alternatives against the per-call
index, and the /impact response contract togpuls depends on.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import alert_policy
import alternatives
import impact
import main
from test_varsel import FakeCall

# The engine reads its snapshot through an injected provider (main owns the cache). These tests
# drive it directly, so they own the provider too. The second reader — the (operator, jkey) index
# alternative-scoring resolves through — is owned here the same way.
_snapshot: dict = {}
_index: dict[tuple[str | None, str], list] = {}
impact.configure(
    snapshot_provider=lambda: (_snapshot, None),
    call_lookup=lambda operator, jkey: _index.get((operator, jkey)),
)


def _install_snapshot(by_situation):
    """Refill the snapshot this suite serves, and tell the engine its index is out of date.

    `impact` indexes a snapshot per GENERATION, and a generation is (dict identity, `refreshed_at`)
    — neither of which moves when this suite refills the one dict it owns under a fixed `None`
    stamp. Production never mutates in place (`main.refresh_call_predictions_cache` swaps the dict
    and stamps a new time under one lock), so the engine deliberately cannot see it; a test that
    refilled the dict without this would be served the PREVIOUS test's situations, which reads as
    the matcher having stopped matching.
    """
    _snapshot.clear()
    _snapshot.update(by_situation)
    impact.invalidate_generation()

A, B, C = "NSR:StopPlace:A", "NSR:StopPlace:B", "NSR:StopPlace:C"


def _at(h, mi=0):
    return datetime(2026, 7, 29, h, mi, tzinfo=UTC)


def _train(jkey, dep_h, dep_mi=0, *, line="VYG:Line:R10", operator="VYG", to_stop=B, **kw):
    """A two-call journey: boarding at A (stop_order 1), alighting at `to_stop` (stop_order 5)."""
    dep = _at(dep_h, dep_mi)
    board = FakeCall(
        jkey=jkey, stop_place=A, stop_order=1, aimed_dep=dep, line=line, operator=operator, **kw
    )
    alight = FakeCall(
        jkey=jkey,
        stop_place=to_stop,
        stop_order=5,
        aimed_dep=None,
        aimed_arr=dep + timedelta(minutes=40),
        line=line,
        operator=operator,
        **kw,
    )
    return [board, alight]


def _scope(**situations):
    return {sit: calls for sit, calls in situations.items()}


def _cells(scope, *, from_stop=A, to_stop=B, start=None, end=None, select="nearest", at=None):
    at = at or _at(8)
    return impact._trip_cells(
        scope,
        from_stop,
        to_stop,
        start or at - timedelta(minutes=90),
        end or at + timedelta(minutes=90),
        select=select,
        at=at,
    )


# ---- window edges -----------------------------------------------------------------------------
def test_window_edges_inclusive():
    """A boarding call exactly on either edge matches; one a minute outside does not."""
    start, end = _at(7), _at(9)
    for h, mi, expected in ((7, 0, 1), (9, 0, 1), (6, 59, 0), (9, 1, 0)):
        scope = _scope(S1=_train("j1", h, mi))
        cells = _cells(scope, start=start, end=end, at=_at(8))
        assert len(cells) == expected, f"{h}:{mi:02d} -> {len(cells)} cells, want {expected}"


def test_situation_with_no_qualifying_departure_is_skipped():
    scope = _scope(S1=_train("j1", 12))  # far outside the window
    assert _cells(scope, start=_at(7), end=_at(9)) == []


# ---- per-line selection strategy -------------------------------------------------------------
def test_nearest_picks_the_train_closest_to_at():
    """Two trains on one line: `nearest` takes the one closest to `at`, ignoring severity."""
    scope = _scope(
        S1=_train("close", 8, 20, p_impact_dep=0.2) + _train("far", 9, 10, p_impact_dep=0.99)
    )
    cells = _cells(scope, select="nearest", at=_at(8))
    assert len(cells) == 1
    assert cells[0].departure.scheduled_time == _at(8, 20)


def test_worst_picks_the_most_severe_train_regardless_of_distance():
    """`worst` takes the highest impact probability -- the quantity the alert gate fires on."""
    scope = _scope(
        S1=_train("close", 8, 20, p_impact_dep=0.2) + _train("far", 9, 10, p_impact_dep=0.99)
    )
    cells = _cells(scope, select="worst", at=None, start=_at(7), end=_at(10))
    assert len(cells) == 1
    assert cells[0].departure.scheduled_time == _at(9, 10)
    assert cells[0].departure.impact_probability == 0.99


def test_nearest_without_at_is_a_programming_error():
    try:
        impact._trip_cells(_scope(S1=_train("j1", 8)), A, B, _at(7), _at(9), select="nearest")
    except ValueError:
        return
    raise AssertionError("select='nearest' without `at` must raise, not silently pick a train")


# ---- direction --------------------------------------------------------------------------------
def test_reverse_direction_produces_no_cell():
    """to_stop reached at a LOWER stop_order than the boarding call is the opposite direction."""
    dep = _at(8)
    board = FakeCall(jkey="j1", stop_place=A, stop_order=5, aimed_dep=dep)
    earlier = FakeCall(
        jkey="j1", stop_place=B, stop_order=1, aimed_dep=None, aimed_arr=dep - timedelta(minutes=30)
    )
    assert _cells(_scope(S1=[board, earlier])) == []


def test_to_stop_not_served_yields_no_cell():
    scope = _scope(S1=_train("j1", 8, to_stop=C))
    assert _cells(scope, to_stop=B) == []


def test_a_call_the_traveller_may_not_alight_from_is_not_a_destination():
    """The 2026-09-01 bug report: Vy's 16:23 Oslo S - Bergen CALLS Drammen, with alighting closed
    for the whole local stretch. Matching on stop_order alone made it somebody's Oslo->Drammen train
    and pushed its cancellation to five registered journeys who could never have been aboard."""
    scope = _scope(S1=_train("f4", 16, 23, line="VYG:Line:F4", alighting_allowed=False))
    assert _cells(scope, to_stop=B) == []


def test_a_call_the_traveller_may_not_board_is_not_a_departure():
    """The mirror restriction, which is just as real: R31/RE30 close boarding on part of their run
    (417 mid-journey NO_BOARDING calls across three Vy lines on the day this was written)."""
    board = FakeCall(jkey="j1", stop_place=A, stop_order=1, aimed_dep=_at(8), boarding_allowed=False)
    alight = FakeCall(
        jkey="j1", stop_place=B, stop_order=5, aimed_dep=None, aimed_arr=_at(8, 40)
    )
    assert _cells(_scope(S1=[board, alight])) == []
    # ...and without a to_stop either: "can I get on here" does not depend on where I am going.
    assert _cells(_scope(S1=[board, alight]), to_stop=None) == []


def test_a_boarding_only_train_does_not_hide_a_usable_one_on_the_same_line():
    """The eligibility-before-selection invariant again, now for the usability filter. The severe
    train is boarding-only at the destination; the survivor must be the one the traveller can use,
    not an empty line."""
    scope = _scope(
        S1=_train("closed", 8, 10, p_impact_dep=0.99, alighting_allowed=False)
        + _train("usable", 8, 30, p_impact_dep=0.70)
    )
    cells = _cells(scope, to_stop=B, select="worst", at=None, start=_at(7), end=_at(9))
    assert len(cells) == 1
    assert cells[0].jkey == "usable"


def test_an_unstated_boarding_activity_permits():
    """Most operators leave both SIRI activities NULL on most calls, and the planned-call source
    reads a NULL as permission. CallPred's defaults have to agree, or a feed that says nothing
    silently stops every alert for that operator."""
    call = impact.CallPred(
        situation_number="S1", summary=None, description=None, jkey="j1", operator="VYG",
        line="VYG:Line:R10", stop_place=A, stop_name=None, stop_order=1,
        aimed_dep=_at(8), aimed_arr=None, et_declared_cancelled=False, et_dep_delay_min=None,
        call_source="estimated", p_cancel=0.1, dep_delay_typ_min=None, dep_delay_p90_min=None,
        arr_delay_typ_min=None, arr_delay_p90_min=None, p_arr_ge10=None, p_arr_ge20=None,
        arr_delay_p50_stack_min=None, arr_delay_p90_stack_min=None, p_impact=None,
        p_dep_ge10=None, p_dep_ge20=None, dep_delay_p50_stack_min=None,
        dep_delay_p90_stack_min=None, p_impact_dep=0.9,
    )
    assert call.boarding_allowed and call.alighting_allowed


def test_a_prediction_row_without_the_columns_still_permits():
    """A serving table built before 2026-09-01 has neither column, and the app can outrun the
    poller by a cycle. `_call_pred_from_row` must read that absence as permission -- reading it as
    a restriction would take out all alerting for as long as the old table stood."""

    class Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    # Every remaining column the mapper reads, all of them nullable and none of them the point here.
    numbers = dict.fromkeys(
        [
            "summary", "description", "stop_name", "aimed_arr", "et_dep_delay_min",
            "dep_delay_typ_min", "dep_delay_p90_min", "arr_delay_typ_min", "arr_delay_p90_min",
            "p_arr_ge10", "p_arr_ge20", "arr_delay_p50_stack_min", "arr_delay_p90_stack_min",
            "p_impact", "p_dep_ge10", "p_dep_ge20", "dep_delay_p50_stack_min",
            "dep_delay_p90_stack_min",
        ]
    )
    fields = {
        "situationNumber": "S1", "jkey": "j1", "lineRef": "VYG:Line:R10", "stop_place": A,
        "stop_order": 1, "aimed_dep": _at(8), "et_declared_cancelled": False,
        "call_source": "estimated", "p_cancel": 0.1, "p_impact_dep": 0.9, **numbers,
    }
    before = main._call_pred_from_row(Row(**fields), "VYG")
    assert before.boarding_allowed and before.alighting_allowed
    after = main._call_pred_from_row(
        Row(**fields, boarding_allowed=True, alighting_allowed=False), "VYG"
    )
    assert after.boarding_allowed and not after.alighting_allowed


def test_a_severe_train_that_skips_to_stop_does_not_hide_a_less_severe_one_that_serves_it():
    """The eligibility-before-selection invariant, which _trip_cells says was got wrong twice.

    Two trains on ONE line: the more severe terminates short of `to_stop`, the less severe reaches
    it. Selecting per line first and checking the destination afterwards drops the whole line — the
    traveller loses the alert for the train they can actually take. Its twin (`catchable_after`) has
    regression tests in test_varsel.py; this one had only a comment.
    """
    scope = _scope(
        S1=_train("skips", 8, 10, to_stop=C, p_impact_dep=0.99)
        + _train("serves", 8, 30, to_stop=B, p_impact_dep=0.70)
    )
    cells = _cells(scope, to_stop=B, select="worst", at=None, start=_at(7), end=_at(9))
    assert len(cells) == 1, "the line must still be represented"
    assert cells[0].jkey == "serves"
    assert cells[0].departure.impact_probability == 0.70


def test_journey_planner_is_skipped_below_the_impact_gate():
    """The poller only KEEPS an alternative for a situation that crosses the alert gate, so asking
    the Journey Planner about every disruption-touched journey spent a blocking 3 s HTTPS round trip
    on the ingestion thread to compute a value that was then discarded.
    """
    asked = []
    original = impact.alternatives.fetch_candidates
    impact.alternatives.fetch_candidates = lambda *a, **k: asked.append(1) or []
    try:
        # Whether the Journey Planner is consulted at all, not how many queries a consultation
        # costs -- that is test_an_empty_first_query_falls_back_to_a_depart_after_search's business.
        for p_impact, gate, expected in ((0.20, 0.50, False), (0.93, 0.50, True), (0.20, 0.0, True)):
            asked.clear()
            _install_snapshot(_scope(S1=_train("j1", 8, p_impact_dep=p_impact)))
            impact.for_trip(
                from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
                select="worst", with_alternative=True, min_impact=gate,
            )
            assert bool(asked) == expected, (
                f"p_impact={p_impact} against gate={gate} should have asked: {expected}"
            )
    finally:
        impact.alternatives.fetch_candidates = original


def test_to_stop_omitted_gives_a_cell_with_no_arrival():
    cells = _cells(_scope(S1=_train("j1", 8)), to_stop=None)
    assert len(cells) == 1
    assert cells[0].arrival is None


# ---- cell metadata ---------------------------------------------------------------------------
def test_cell_carries_operator_jkey_and_description():
    scope = _scope(
        S1=_train("j1", 8, operator="RUT", line="RUT:Line:31", description="Buss for tog")
    )
    cells = _cells(scope)
    assert (cells[0].operator, cells[0].jkey) == ("RUT", "j1")
    assert cells[0].description == "Buss for tog"


def test_operator_collision_keeps_journeys_separate():
    """Bus ServiceJourney jkeys are not date-scoped, so two operators can share one jkey."""
    scope = _scope(
        S1=_train("shared", 8, operator="SKY", line="SKY:Line:1")
        + _train("shared", 8, 5, operator="ATB", line="ATB:Line:2")
    )
    cells = _cells(scope)
    assert {(c.operator, c.line) for c in cells} == {("SKY", "SKY:Line:1"), ("ATB", "ATB:Line:2")}


# ---- severity ordering ------------------------------------------------------------------------
def test_worst_orders_on_impact_probability_not_cancellation():
    """The regression this fixes: p_cancel-first ordering could hand a situation's numbers to a
    train other than the one that crossed the alert threshold."""
    low_cancel_high_impact = impact._departure_prediction(
        FakeCall(jkey="a", stop_place=A, stop_order=1, aimed_dep=_at(8), p_cancel=0.05,
                 p_impact_dep=0.95)
    )
    high_cancel_low_impact = impact._departure_prediction(
        FakeCall(jkey="b", stop_place=A, stop_order=1, aimed_dep=_at(8), p_cancel=0.40,
                 p_impact_dep=0.42)
    )
    worst = impact._worst([high_cancel_low_impact, low_cancel_high_impact])
    assert worst is low_cancel_high_impact


def test_worst_ignores_none_entries():
    pred = impact._departure_prediction(
        FakeCall(jkey="a", stop_place=A, stop_order=1, aimed_dep=_at(8))
    )
    assert impact._worst([None, pred, None]) is pred
    assert impact._worst([None, None]) is None


def test_multi_line_situation_rolls_up_to_the_highest_impact_line():
    scope = _scope(
        S1=_train("r10", 8, line="VYG:Line:R10", p_cancel=0.9, p_impact_dep=0.91)
        + _train("r11", 8, 5, line="VYG:Line:R11", p_cancel=0.1, p_impact_dep=0.99)
    )
    cells = _cells(scope)
    sit = impact._worst([c.departure for c in cells])
    assert sit.impact_probability == 0.99


# ---- latest_observation -----------------------------------------------------------------------
def test_latest_observation_absent_when_nothing_observed():
    cells = _cells(_scope(S1=_train("j1", 8)))  # FakeCall defaults to an unobserved future call
    assert impact._latest_observation(cells) is None


def test_latest_observation_takes_the_furthest_observed_call():
    dep = _at(7)
    calls = [
        FakeCall(jkey="j1", stop_place=A, stop_order=1, aimed_dep=dep, stop_name="Oslo S",
                 call_source="recorded", et_dep_delay_min=5),
        FakeCall(jkey="j1", stop_place="NSR:StopPlace:X", stop_order=3,
                 aimed_dep=dep + timedelta(minutes=20), stop_name="Asker",
                 call_source="recorded", et_dep_delay_min=18),
        FakeCall(jkey="j1", stop_place=B, stop_order=5, aimed_dep=None,
                 aimed_arr=dep + timedelta(minutes=40), stop_name="Drammen"),
    ]
    obs = impact._latest_observation([impact.TripCell(
        situation_number="S1", summary=None, description=None, line="VYG:Line:R10",
        operator="VYG", jkey="j1", departure=impact._departure_prediction(calls[0]),
        arrival=None, calls=calls,
    )])
    assert (obs.at_stop, obs.at_stop_name, obs.delayed_min) == ("NSR:StopPlace:X", "Asker", 18)
    assert obs.cancelled is False
    assert obs.observed_at == dep + timedelta(minutes=20)


def test_latest_observation_reports_cancellation_without_a_delay():
    """An ET-declared cancellation is an observation even when no delay was recorded."""
    call = FakeCall(jkey="j1", stop_place=A, stop_order=1, aimed_dep=_at(8), stop_name="Oslo S",
                    call_source="recorded", et_declared_cancelled=True, et_dep_delay_min=None)
    obs = impact._latest_observation([impact.TripCell(
        situation_number="S1", summary=None, description=None, line="VYG:Line:R10",
        operator="VYG", jkey="j1", departure=impact._departure_prediction(call),
        arrival=None, calls=[call],
    )])
    assert obs.cancelled is True
    assert obs.delayed_min is None


def test_latest_observation_ignores_estimated_calls():
    """`estimated` means the vehicle has not served the stop yet -- a delay there is not observed."""
    call = FakeCall(jkey="j1", stop_place=A, stop_order=1, aimed_dep=_at(8),
                    call_source="estimated", et_dep_delay_min=9)
    obs = impact._latest_observation([impact.TripCell(
        situation_number="S1", summary=None, description=None, line="VYG:Line:R10",
        operator="VYG", jkey="j1", departure=impact._departure_prediction(call),
        arrival=None, calls=[call],
    )])
    assert obs is None


def _observation_of(calls, *, now):
    return impact._latest_observation(
        [impact.TripCell(
            situation_number="S1", summary=None, description=None, line="VYG:Line:R10",
            operator="VYG", jkey="j1", departure=impact._departure_prediction(calls[0]),
            arrival=None, calls=calls,
        )],
        now=now,
    )


def test_latest_observation_ignores_a_cancellation_declared_for_a_future_call():
    """The 2026-08-05 report: a cancellation announced ahead of the train is not a sighting.

    Vy cancelled RE10 80323 Drammen-Skøyen at 13:54; every call up to Lysaker 15:15 came back from
    ET flagged cancelled, all of them still upcoming. `_latest_observation` takes the FURTHEST-along
    observed call, so it picked the most distant one and the card read "Sist observert: Lysaker
    stasjon, innstilt (kl 15:15)" at 14:28 -- 47 minutes into the future.
    """
    now = _at(14, 28)
    calls = [
        FakeCall(jkey="j1", stop_place=A, stop_order=2, aimed_dep=_at(15, 3), stop_name="Asker",
                 call_source="estimated", et_declared_cancelled=True),
        FakeCall(jkey="j1", stop_place=B, stop_order=4, aimed_dep=_at(15, 15), stop_name="Lysaker",
                 call_source="estimated", et_declared_cancelled=True),
    ]
    assert _observation_of(calls, now=now) is None


def test_latest_observation_keeps_a_cancellation_whose_time_has_passed():
    """Once the call's own time is behind us it IS an observation: that stop went unserved."""
    now = _at(15, 20)
    calls = [
        FakeCall(jkey="j1", stop_place=A, stop_order=2, aimed_dep=_at(15, 3), stop_name="Asker",
                 call_source="estimated", et_declared_cancelled=True),
        FakeCall(jkey="j1", stop_place=B, stop_order=4, aimed_dep=_at(15, 15), stop_name="Lysaker",
                 call_source="estimated", et_declared_cancelled=True),
        # Still upcoming, so it must not win the "furthest along" pick even now.
        FakeCall(jkey="j1", stop_place=C, stop_order=6, aimed_dep=_at(15, 45), stop_name="Oslo S",
                 call_source="estimated", et_declared_cancelled=True),
    ]
    obs = _observation_of(calls, now=now)
    assert (obs.at_stop_name, obs.cancelled, obs.observed_at) == ("Lysaker", True, _at(15, 15))


# ---- declared cancellations are facts, not forecasts -------------------------------------------
def _cancelled_call(**kw):
    return FakeCall(jkey="j1", stop_place=A, stop_order=2, aimed_dep=_at(15, 3), stop_name="Asker",
                    call_source="estimated", et_declared_cancelled=True,
                    p_cancel=0.57, p_impact_dep=0.61, **kw)


def test_declared_cancellation_is_served_as_a_certainty_not_a_probability():
    """The other half of the 2026-08-05 report. The cancellation of the 15:03 from Asker was in our
    own cache, and the card still quoted the model: "91 % sjanse for innstilling" for a departure
    entur.no no longer offered. Every conditional figure goes with it -- they meant "if it runs"."""
    dep = impact._departure_prediction(_cancelled_call())
    assert dep.cancelled is True
    assert (dep.cancel_probability, dep.impact_probability) == (1.0, 1.0)
    assert (dep.delay_typ_min, dep.delay_p50_min, dep.delay_p90_min, dep.ge10_probability) == (
        None, None, None, None,
    )
    assert dep.scheduled_time == _at(15, 3)  # still the train it is about


def test_declared_cancellation_clears_the_alert_gate_a_low_score_would_have_missed():
    """At 14:17 the same known-cancelled call scored 0.57 -- under the 0.65 push gate -- so nobody
    was told about a train we already knew was gone. The certainty is what closes that miss."""
    assert impact._departure_prediction(_cancelled_call()).impact_probability >= 0.65


def test_worst_selection_prefers_a_known_cancelled_train_over_a_high_forecast():
    """`select='worst'` must land on the train that is certainly gone, not the one merely likely to
    be late: `_call_severity` restates the ordering for speed and has to agree with the prediction."""
    scope = _scope(S1=(
        _train("forecast", 15, 13, p_cancel=0.4, p_impact_dep=0.99)
        + _train("cancelled", 15, 3, et_declared_cancelled=True, p_cancel=0.57, p_impact_dep=0.61)
    ))
    cells = _cells(scope, select="worst", at=_at(15))
    assert [c.jkey for c in cells] == ["cancelled"]
    assert cells[0].departure.cancelled is True


# ---- ET-declared cancellations that no situation named (the relay rows) -------------------------
RELAY_SIT = "KIX:ETCancellation:DatedServiceJourney:relay"


def _relay_call(**kw):
    """One call shaped as predict_calls*.sql's ET-cancellation relay emits it.

    Not the same thing as `_cancelled_call` above, which is a SCORED call that ET later also
    declared cancelled. A relay row was never scored: it exists because ET declared a cancellation
    and no SX situation named the call, so every conditional figure on it is NULL -- including the
    alerting score on whichever side the call does not have. Nothing may read one of those as a zero
    and conclude the train is fine; the certainty is carried entirely by `et_declared_cancelled`.
    """
    fields = {
        "jkey": "DatedServiceJourney:relay",
        "situation_number": RELAY_SIT,
        "summary": "Innstilt Drammen–Skøyen",
        "description": "Avgangen er markert innstilt i operatørens sanntidsdata.",
        "stop_place": A,
        "stop_order": 1,
        "stop_name": "Asker",
        "aimed_dep": _at(15, 3),
        "aimed_arr": None,
        "call_source": "estimated",
        "et_declared_cancelled": True,
        "p_cancel": 1.0,
        "p_impact_dep": 1.0,
        "p_impact": None,  # an origin has no arrival, so no arrival-side score exists
        "p_dep_ge10": None,
        "p_arr_ge10": None,
        "dep_delay_typ_min": None,
        "dep_delay_p90_min": None,
        "dep_delay_p50_stack_min": None,
        "dep_delay_p90_stack_min": None,
    }
    fields.update(kw)
    return FakeCall(**fields)


def _relay_journey():
    """A relay journey: boarding at A (an origin -- departure side only) and alighting at B (a
    terminal -- arrival side only). Between them the two sides cover every NULL the shape allows."""
    return [
        _relay_call(),
        _relay_call(
            stop_place=B,
            stop_order=5,
            stop_name="Skøyen",
            aimed_dep=None,
            aimed_arr=_at(15, 43),
            p_impact_dep=None,
            p_impact=1.0,
        ),
    ]


NOSX_SIT = "KIX:NoSituation:DatedServiceJourney:nosx"


def _nosx_call(**kw):
    """One call shaped as predict_calls.sql's SCORED no-SX branch emits it.

    Distinct from both neighbours above. A relay row carries a certainty and no model output; this
    one is the opposite -- a full set of model figures with no situation behind them. Its
    situationNumber is synthetic, its summary is text this codebase wrote rather than an operator's,
    and `has_situation` is what says so.
    """
    fields = {
        "jkey": "DatedServiceJourney:nosx",
        "situation_number": NOSX_SIT,
        "summary": "Mulig avvik",
        "description": "Ingen situasjonsmelding er publisert for denne avgangen.",
        "stop_place": A,
        "stop_order": 1,
        "stop_name": "Asker",
        "aimed_dep": _at(15, 3),
        "aimed_arr": None,
        "has_situation": False,
        "et_declared_cancelled": False,
        "p_cancel": 0.10,
        "p_impact_dep": 0.62,
        "p_impact": None,
        "p_dep_ge10": 0.58,
    }
    fields.update(kw)
    return FakeCall(**fields)


def test_a_no_sx_call_is_gated_at_the_no_sx_threshold():
    """The gate is chosen by POPULATION, not by operator. GOA's tuned 0.50 is a number fitted to
    predictions calibrated on flagged calls; applying it to a no-SX probability would cut a
    different distribution at a bar that was never measured against it."""
    call = _nosx_call(operator="GOA")
    assert alert_policy.threshold_for(call.operator) == 0.50
    assert alert_policy.threshold_for(
        call.operator, has_situation=call.has_situation
    ) == alert_policy._NOSX_ALERT_THRESHOLD


def test_a_no_sx_situation_reports_its_population_to_its_readers():
    """`/impact` has to say which population a situation came from, or every reader downstream --
    the alert gate, both wordings, /historikk's segmentation -- is left inferring it from the
    `KIX:` prefix, which makes a naming convention load-bearing for alerting policy."""
    _install_snapshot({NOSX_SIT: [_nosx_call()]})
    resp = impact.for_trip(
        from_stop=A, to_stop=None,
        window_start=_at(14, 0), window_end=_at(16, 0), with_alternative=False,
        select="worst",
    )
    (sit,) = resp.situations
    assert sit.situation_number == NOSX_SIT
    assert sit.has_situation is False


def test_a_flagged_situation_still_reports_itself_as_flagged():
    """The default has to survive: every situation that predates this population is a flagged one,
    and a field that silently flipped would move which threshold they are all cut at."""
    _install_snapshot({RELAY_SIT: [_relay_call()]})
    resp = impact.for_trip(
        from_stop=A, to_stop=None,
        window_start=_at(14, 0), window_end=_at(16, 0), with_alternative=False,
        select="worst",
    )
    (sit,) = resp.situations
    # A relay row is not scored, but it IS a CallPred like any other and defaults to flagged unless
    # the serving row said otherwise -- the FakeCall default, matching impact.CallPred's.
    assert sit.has_situation is True


def test_relay_row_alerts_on_the_certainty_alone():
    """A row with no prediction on it still clears every gate, because "this train is cancelled" is
    not a prediction. Both orderings have to reach that verdict without touching a NULL figure."""
    call = _relay_call()
    assert impact._alert_score(call) == 1.0
    assert impact._call_severity(call) == (1.0, 0, 1.0)


def test_relay_row_is_worded_as_a_cancellation_and_quotes_no_delay():
    dep = impact._departure_prediction(_relay_call())
    assert (dep.cancelled, dep.cancel_probability, dep.impact_probability) == (True, 1.0, 1.0)
    assert (dep.delay_typ_min, dep.delay_p50_min, dep.delay_p90_min, dep.ge10_probability) == (
        None, None, None, None,
    )
    assert dep.scheduled_time == _at(15, 3)  # still the train it is about


def test_relay_row_expected_departure_survives_all_null_delays():
    """`_expected_departure` is aimed time plus the predicted typical delay, and decides whether a
    train is still catchable. A relay row carries no such figure; treating the absence as zero is
    the honest reading -- the train is not leaving late, it is not leaving."""
    assert impact._expected_departure(_relay_call()) == _at(15, 3)


def test_relay_situation_reaches_the_traveller_like_a_scored_one():
    """End to end through the matcher: one cell, under the relay's own synthetic situation number,
    with a cancelled departure that clears the widest alternative-lookup gate there is."""
    cells = _cells({RELAY_SIT: _relay_journey()}, at=_at(15))
    assert len(cells) == 1
    assert cells[0].situation_number == RELAY_SIT
    assert cells[0].departure.cancelled is True
    assert cells[0].departure.impact_probability == 1.0
    assert impact._worth_an_alternative(cells, alert_policy.min_threshold()) is True


def test_relay_situation_outranks_a_scored_forecast_for_the_same_traveller():
    """Two situations in the window, one scored high and one a relay. `select='worst'` must land on
    the train that is certainly gone -- the relay carries no p_impact_dep to compare, so an
    ordering that reached for the figure instead of the fact would rank it last."""
    scope = {
        "S1": _train("forecast", 15, 13, p_cancel=0.4, p_impact_dep=0.99),
        RELAY_SIT: _relay_journey(),
    }
    cells = _cells(scope, select="worst", at=_at(15))
    worst = impact._worst_cell(cells)
    assert worst is not None
    assert worst.situation_number == RELAY_SIT


# ---- scoring Journey Planner alternatives ------------------------------------------------------
ALT_REF = ("VYG", "DatedServiceJourney:alt")


def _candidate(
    *,
    refs=(ALT_REF,),
    lines=("VYG:Line:R11",),
    board=A,
    alight=B,
    dep_h=9,
    dep_mi=0,
    delay_min=0,
    cancelled=False,
    ordinal=0,
):
    """A Journey Planner candidate, as `alternatives.parse_candidates` would have built it."""
    aimed_dep = _at(dep_h, dep_mi)
    aimed_arr = aimed_dep + timedelta(minutes=40)
    shift = timedelta(minutes=delay_min)
    return alternatives.AlternativeCandidate(
        departure_time=aimed_dep + shift,
        arrival_time=aimed_arr + shift,
        aimed_departure_time=aimed_dep,
        aimed_arrival_time=aimed_arr,
        duration_min=40,
        legs=[alternatives.AlternativeLeg(mode="rail", line=line) for line in lines],
        transit_refs=list(refs),
        lines=list(lines),
        board_stop_place=board,
        alight_stop_place=alight,
        cancelled=cancelled,
        ordinal=ordinal,
    )


def _scored(candidates, *, affected_refs=frozenset(), affected_lines=frozenset(), index=None):
    """Score `candidates` against `index` (installed for the call and torn down after)."""
    _index.clear()
    _index.update(index or {})
    try:
        return impact.score_candidates(
            candidates, affected_refs=set(affected_refs), affected_lines=set(affected_lines)
        )
    finally:
        _index.clear()


def _alt_call(**kw):
    """A scored call on the alternative's own journey (jkey/operator from ALT_REF by default)."""
    kw.setdefault("jkey", ALT_REF[1])
    kw.setdefault("operator", ALT_REF[0])
    kw.setdefault("line", "VYG:Line:R11")
    return FakeCall(**kw)


def test_a_candidate_in_the_index_is_scored_from_the_cache():
    """Both ends: the boarding call's departure-side numbers and the alighting call's arrival ones."""
    index = {
        ALT_REF: [
            _alt_call(stop_place=A, stop_order=1, aimed_dep=_at(9), stop_name="Oslo S",
                      p_cancel=0.12, p_impact_dep=0.33),
            # A terminal call: no departure from it, so the departure-side figures are structurally
            # null and the arrival side is the only one that says anything.
            _alt_call(stop_place=B, stop_order=5, aimed_dep=None, stop_name="Drammen",
                      aimed_arr=_at(9, 40), p_cancel=0.12, p_impact_dep=None, p_impact=0.41),
        ]
    }
    score = _scored([_candidate()], index=index)[0]
    dep = score.departure_prediction
    assert dep.scored is True
    assert (dep.cancel_probability, dep.impact_probability) == (0.12, 0.33)
    assert (dep.stop_place, dep.stop_name) == (A, "Oslo S")
    arr = score.arrival_prediction
    assert arr is not None and arr.scored is True
    assert (arr.stop_place, arr.impact_probability) == (B, 0.41)
    assert score.candidate.risky is False, "0.33 is under VYG's 0.65 gate"


def test_a_candidate_absent_from_the_index_is_unscored_with_null_probabilities():
    """The COMMON case, and not a failure: the cache only holds journeys a live situation touches.

    Null, never 0.0 — a zero would assert a calibrated certainty of safety that nothing established,
    the fact-vs-forecast confusion this codebase spent 2026-08-05 removing.
    """
    score = _scored([_candidate()], index={})[0]
    dep = score.departure_prediction
    assert dep.scored is False
    assert dep.cancel_probability is None and dep.impact_probability is None
    assert dep.cancel_probability != 0.0 and dep.impact_probability != 0.0
    assert dep.stop_place == A, "the JP-only shape still says which stop it is about"
    assert score.arrival_prediction is None


def test_the_resolved_call_carries_its_identity_so_the_advice_can_be_labelled():
    """`(operator, jkey, stop_order)` is the grain label_varsel_decisions.sql joins realized ET
    outcomes on. Scoring resolves exactly that call; keeping only its numbers is what left "take
    this instead" the one traveller-facing claim nothing could ever score.

    Both ends, and each from its OWN call — the alighting prediction must not inherit the boarding
    call's stop_order, or a recommendation would be labelled against the wrong stop.
    """
    index = {
        ALT_REF: [
            _alt_call(stop_place=A, stop_order=4, aimed_dep=_at(9), p_impact_dep=0.11),
            _alt_call(stop_place=B, stop_order=9, aimed_dep=None, aimed_arr=_at(9, 40),
                      p_impact_dep=None, p_impact=0.14),
        ]
    }
    score = _scored([_candidate()], index=index)[0]
    dep = score.departure_prediction
    assert (dep.operator, dep.jkey, dep.stop_order) == (ALT_REF[0], ALT_REF[1], 4)
    arr = score.arrival_prediction
    assert (arr.operator, arr.jkey, arr.stop_order) == (ALT_REF[0], ALT_REF[1], 9)


def test_an_unscored_candidate_names_no_train_rather_than_guessing_from_the_codespace():
    """Null identity, not a reconstruction. The Journey Planner's own refs are right there on the
    candidate, and the codespace they carry is JP's — the same one `risky` refuses to gate on. Two
    thirds of a join key joins to nothing, and a filled column that never matches reads as a train
    we could not find rather than as a train we never scored.
    """
    score = _scored([_candidate()], index={})[0]
    dep = score.departure_prediction
    assert dep.scored is False
    assert (dep.operator, dep.jkey, dep.stop_order) == (None, None, None)
    assert score.candidate.transit_refs[0] == ALT_REF, "the ref was available and deliberately unused"


def test_operator_collision_scores_each_candidate_against_its_own_journey():
    """Bus ServiceJourney jkeys are not date-scoped, so two operators can share one.

    Mirrors test_operator_collision_keeps_journeys_separate on the scoring path: keying on the jkey
    alone would let one operator's alert-worthy train make the other's clean one look doomed.
    """
    jkey = "ServiceJourney:shared"
    index = {
        ("SKY", jkey): [FakeCall(jkey=jkey, operator="SKY", line="SKY:Line:1", stop_place=A,
                                 stop_order=1, aimed_dep=_at(9), p_impact_dep=0.05)],
        ("ATB", jkey): [FakeCall(jkey=jkey, operator="ATB", line="ATB:Line:2", stop_place=A,
                                 stop_order=1, aimed_dep=_at(9), p_impact_dep=0.95)],
    }
    sky, atb = _scored(
        [
            _candidate(refs=(("SKY", jkey),), lines=("SKY:Line:1",)),
            _candidate(refs=(("ATB", jkey),), lines=("ATB:Line:2",), ordinal=1),
        ],
        index=index,
    )
    assert sky.departure_prediction.impact_probability == 0.05
    assert atb.departure_prediction.impact_probability == 0.95
    assert (sky.candidate.risky, atb.candidate.risky) == (False, True)


def test_a_declared_cancellation_beats_the_journey_planners_own_flag():
    """JP's default search excludes realtime-cancelled departures, so its per-leg flag is
    belt-and-braces: our ET cache is the working guard. A candidate JP reports as running, which we
    already know is cancelled, must not be recommended."""
    index = {
        ALT_REF: [
            _alt_call(stop_place=A, stop_order=1, aimed_dep=_at(9), et_declared_cancelled=True,
                      p_cancel=0.57, p_impact_dep=0.61)
        ]
    }
    score = _scored([_candidate(cancelled=False, delay_min=3)], index=index)[0]
    dep = score.departure_prediction
    assert dep.cancelled is True
    assert (dep.cancel_probability, dep.impact_probability) == (1.0, 1.0)
    assert dep.delay_min is None, "every delay figure means 'if it runs', and this one does not"
    assert score.candidate.cancelled is True
    assert score.candidate.risky is True


def test_a_risky_second_leg_makes_a_multi_leg_candidate_risky():
    """`risky` reads EVERY transit leg, so a connection about to be cancelled is not scored safe.

    Only the first leg's boarding stop is knowable from a JP pattern, so a single-leg resolution
    would leave every multi-leg candidate structurally unable to look risky.
    """
    leg2 = ("RUT", "ServiceJourney:leg2")
    index = {
        ALT_REF: [_alt_call(stop_place=A, stop_order=1, aimed_dep=_at(9), p_impact_dep=0.04)],
        leg2: [FakeCall(jkey=leg2[1], operator="RUT", line="RUT:Line:31", stop_place=C,
                        stop_order=2, aimed_dep=_at(9, 30), p_impact_dep=0.92)],
    }
    score = _scored(
        [_candidate(refs=(ALT_REF, leg2), lines=("VYG:Line:R11", "RUT:Line:31"))], index=index
    )[0]
    assert score.candidate.risky is True
    assert score.departure_prediction.impact_probability == 0.04, (
        "the departure prediction is still the FIRST leg's boarding call"
    )


def test_risky_uses_the_found_calls_operator_threshold():
    """Same 0.55, two answers: GOA alerts at 0.50, VYG at 0.65. The gate is the found call's
    operator, never the Journey Planner's codespace."""
    verdicts = {}
    for operator, expected in (("GOA", True), ("VYG", False)):
        jkey = f"DatedServiceJourney:{operator}"
        index = {
            (operator, jkey): [FakeCall(jkey=jkey, operator=operator, line=f"{operator}:Line:1",
                                        stop_place=A, stop_order=1, aimed_dep=_at(9),
                                        p_impact_dep=0.55)]
        }
        score = _scored(
            [_candidate(refs=((operator, jkey),), lines=(f"{operator}:Line:1",))], index=index
        )[0]
        verdicts[operator] = score.candidate.risky
        assert score.candidate.risky is expected, f"{operator} at 0.55 -> risky={expected}"
    assert verdicts["GOA"] != verdicts["VYG"]


def test_an_unscored_leg_on_an_affected_line_is_risky():
    """Where we have no score, line membership is the only signal there is — and an unknown train on
    the disrupted line must not beat a scored clean one."""
    score = _scored(
        [_candidate(lines=("VYG:Line:R10",))], affected_lines={"VYG:Line:R10"}, index={}
    )[0]
    assert score.candidate.risky is True
    assert score.departure_prediction.scored is False


def test_a_scored_clean_leg_on_an_affected_line_is_not_risky():
    """The other half of the asymmetry, and the whole of the mitigation.

    Marking every affected-line leg risky regardless of score reinstates the whole-line veto in all
    but name — it is what threw away "ta neste tog", the operator's own advice in the 2026-08-05 Vy
    RE10 case. A next train our own models score at 0.02 must not lose to one we know nothing about.
    """
    index = {
        ALT_REF: [
            _alt_call(stop_place=A, stop_order=1, aimed_dep=_at(9), line="VYG:Line:R10",
                      p_impact_dep=0.02)
        ]
    }
    score = _scored(
        [_candidate(lines=("VYG:Line:R10",))], affected_lines={"VYG:Line:R10"}, index=index
    )[0]
    assert score.candidate.risky is False
    assert score.departure_prediction.impact_probability == 0.02


def test_a_call_touched_by_two_situations_resolves_to_the_worse():
    """One call carries one row per situation that touches it; the cache holds both."""
    index = {
        ALT_REF: [
            _alt_call(stop_place=A, stop_order=1, aimed_dep=_at(9), situation_number="S1",
                      p_impact_dep=0.20),
            _alt_call(stop_place=A, stop_order=1, aimed_dep=_at(9), situation_number="S2",
                      p_impact_dep=0.88),
        ]
    }
    score = _scored([_candidate()], index=index)[0]
    assert score.departure_prediction.impact_probability == 0.88


def test_delay_min_is_observed_and_clamped_at_zero():
    """expected - aimed, from the Journey Planner's own times; an early vehicle is not late."""
    late, early = _scored([_candidate(delay_min=7), _candidate(delay_min=-2, ordinal=1)], index={})
    assert late.departure_prediction.delay_min == 7
    assert early.departure_prediction.delay_min == 0


def test_arrival_prediction_is_null_when_no_alighting_call_resolves():
    """It never borrows the departure's numbers: "when do I get there" answered with the departure's
    delay would be a fabricated figure."""
    index = {ALT_REF: [_alt_call(stop_place=A, stop_order=1, aimed_dep=_at(9), p_impact_dep=0.3)]}
    score = _scored([_candidate(alight=C)], index=index)[0]
    assert score.departure_prediction.scored is True
    assert score.arrival_prediction is None


def test_scoring_never_scans_the_snapshot():
    """The injected (operator, jkey) reader is the only access path. Scanning the by-situation
    snapshot instead would be O(situations x calls) per lookup, on the ingestion thread, over data
    main already indexes."""
    scans = []
    original = impact._snapshot_provider
    impact._snapshot_provider = lambda: scans.append(1) or ({}, None)
    try:
        index = {ALT_REF: [_alt_call(stop_place=A, stop_order=1, aimed_dep=_at(9))]}
        score = _scored([_candidate()], index=index)[0]
    finally:
        impact._snapshot_provider = original
    assert scans == [], "score_candidates must never read the by-situation snapshot"
    assert score.departure_prediction.scored is True, "...and must still resolve through the index"


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_scoring_without_a_reader_is_unscored_logged_once_and_never_raises():
    """A missing lookup must be VISIBLE but harmless: `for_trip` wraps this path in a fail-open
    guard, so raising (the way a missing snapshot_provider does) would be swallowed and leave a
    wiring fault indistinguishable from "the Journey Planner had nothing"."""
    recorder = _Recorder()
    original, warned = impact._call_lookup, impact._warned_no_call_lookup
    impact._call_lookup, impact._warned_no_call_lookup = None, False
    impact.logger.addHandler(recorder)
    try:
        first = impact.score_candidates(
            [_candidate(), _candidate(ordinal=1)], affected_refs=set(), affected_lines=set()
        )
        second = impact.score_candidates([_candidate()], affected_refs=set(), affected_lines=set())
    finally:
        impact.logger.removeHandler(recorder)
        impact._call_lookup, impact._warned_no_call_lookup = original, warned
    assert [s.departure_prediction.scored for s in first + second] == [False, False, False]
    unwired = [r for r in recorder.records if getattr(r, "fields", {}).get("event") == "impact_scoring_unwired"]
    assert len(unwired) == 1, f"expected one warning for the whole process, got {len(unwired)}"


def test_a_candidate_that_raises_is_dropped_and_the_rest_still_score():
    """Third fail-open boundary on this path (the fetch and the parse have their own): one
    uninterpretable candidate costs that candidate, not every alternative for the trip."""

    class _Exploding:
        ordinal = 7

        def __getattr__(self, name):
            raise RuntimeError("boom")

    index = {ALT_REF: [_alt_call(stop_place=A, stop_order=1, aimed_dep=_at(9))]}
    scored = _scored([_candidate(), _Exploding(), _candidate(ordinal=2)], index=index)
    assert [s.candidate.ordinal for s in scored] == [0, 2]
    assert all(s.departure_prediction.scored for s in scored)


def test_the_real_call_lookup_resolves_through_mains_index():
    """Wire main._impact_journey_calls itself, not a hand-written double — the same reason
    test_varsel's test_the_real_provider_signature_matches exists: this is a cross-module contract,
    and a suite that only ever drives a double cannot see it break.

    Also pins the collision rule: a codespace that misses is a MISS. Answering "VYG's
    ServiceJourney:1 is not in the cache" with ATB's row is exactly what keying on
    (operator, jkey) exists to prevent.
    """
    jkey = "ServiceJourney:1"
    call = FakeCall(jkey=jkey, operator="ATB", line="ATB:Line:2", stop_place=A, stop_order=1,
                    aimed_dep=_at(9), p_impact_dep=0.77)
    original_key, original_index = main._call_pred_by_key, main._call_pred_by_jkey_index
    original_lookup = impact._call_lookup
    main._call_pred_by_key = {("ATB", jkey): [call]}
    main._call_pred_by_jkey_index = {jkey: {"ATB"}}
    impact._call_lookup = main._impact_journey_calls
    try:
        hit = impact.score_candidates(
            [_candidate(refs=(("ATB", jkey),), lines=("ATB:Line:2",))],
            affected_refs=set(), affected_lines=set(),
        )[0]
        wrong_codespace = impact.score_candidates(
            [_candidate(refs=(("VYG", jkey),), lines=("VYG:Line:R11",))],
            affected_refs=set(), affected_lines=set(),
        )[0]
        unprefixed = impact.score_candidates(
            [_candidate(refs=((None, jkey),), lines=("ATB:Line:2",))],
            affected_refs=set(), affected_lines=set(),
        )[0]
    finally:
        main._call_pred_by_key, main._call_pred_by_jkey_index = original_key, original_index
        impact._call_lookup = original_lookup
    assert hit.departure_prediction.impact_probability == 0.77
    assert wrong_codespace.departure_prediction.scored is False
    assert unprefixed.departure_prediction.impact_probability == 0.77, (
        "a ref with no codespace at all may still resolve when a single operator claims the jkey"
    )


def test_an_unresolved_leg_riding_an_affected_journey_is_risky():
    """`affected_refs` is the sharper of the two unscored fallbacks: the journey itself is known to
    be touched by a live situation, which says more than its line does."""
    score = _scored([_candidate()], affected_refs={ALT_REF}, index={})[0]
    assert score.candidate.risky is True


# ---- choosing an alternative, end to end -------------------------------------------------------
# These drive the whole fetch -> score -> select chain through `impact.for_trip`, with only the
# Journey Planner replaced. They cannot use the fixed `_at` clock the rest of this file runs on:
# `for_trip` samples `datetime.now(UTC)` itself (deliberately — one instant governs `as_of`, the
# observation cut and the departure filter alike), and every candidate on a 2026-07-29 clock has
# therefore "already departed". So each test captures one live `now` and states its fixtures as
# offsets from it, which is also how they read: `+10` is "ten minutes from now".
ALT_JKEY = "DatedServiceJourney:alternative"


def _live_train(now, jkey, *, dep_min, arr_min, line="VYG:Line:R10", operator="VYG", **kw):
    """A two-call journey boarding at A and alighting at B, timed relative to `now`."""
    dep, arr = now + timedelta(minutes=dep_min), now + timedelta(minutes=arr_min)
    return [
        FakeCall(jkey=jkey, stop_place=A, stop_order=1, aimed_dep=dep, line=line,
                 operator=operator, **kw),
        FakeCall(jkey=jkey, stop_place=B, stop_order=5, aimed_dep=None, aimed_arr=arr,
                 line=line, operator=operator, **kw),
    ]


def _live_candidate(
    now, *, dep_min, arr_min, refs=(("VYG", ALT_JKEY),), lines=("VYG:Line:R11",),
    board=A, alight=B, cancelled=False, ordinal=0,
):
    """A Journey Planner candidate as `parse_candidates` would have built it, relative to `now`."""
    dep, arr = now + timedelta(minutes=dep_min), now + timedelta(minutes=arr_min)
    return alternatives.AlternativeCandidate(
        departure_time=dep,
        arrival_time=arr,
        aimed_departure_time=dep,
        aimed_arrival_time=arr,
        duration_min=arr_min - dep_min,
        legs=[alternatives.AlternativeLeg(mode="rail", line=line, public_code="R11")
              for line in lines],
        transit_refs=list(refs),
        lines=list(lines),
        board_stop_place=board,
        alight_stop_place=alight,
        cancelled=cancelled,
        ordinal=ordinal,
    )


class _JourneyPlanner:
    """A `fetch_candidates` double: records every query and answers them from a queue of replies.

    A queue rather than one canned answer, because the two-query rule is the thing several of these
    tests are about — "how many times were you asked, and with which anchor" is the assertion.

    `answered` is the real function's out-parameter: False says no reply was obtained at all (an
    outage, the post-failure cooldown, a spent fetch budget), as distinct from a reply that came
    back empty. It defaults to True, because that is what the real lookup reports for every reply it
    actually got — including an empty one. A sequence answers per call (the last value repeats), so
    a test can have the first query reply and the fallback go unanswered.
    """

    def __init__(self, *replies, raises=False, answered=True):
        self.replies = [list(r) for r in replies]
        self.raises = raises
        self.answered = list(answered) if isinstance(answered, (list, tuple)) else [answered]
        self.calls: list[dict] = []

    def __call__(self, from_stop, to_stop, *, arrive_by, date_time, now, path="request",
                 outcome=None):
        self.calls.append(
            {"from_stop": from_stop, "to_stop": to_stop, "arrive_by": arrive_by,
             "date_time": date_time, "now": now, "path": path}
        )
        index = len(self.calls) - 1
        if outcome is not None:
            outcome["answered"] = self.answered[min(index, len(self.answered) - 1)]
        if self.raises:
            raise RuntimeError("JP down")
        return self.replies[index] if index < len(self.replies) else []


def _for_trip(scope, jp, *, now, to_stop=B, min_impact=0.0, window_min=(-5, 120), index=None):
    """Run `for_trip` over `scope` with `jp` standing in for the Journey Planner."""
    _install_snapshot(scope)
    _index.clear()
    _index.update(index or {})
    original = alternatives.fetch_candidates
    alternatives.fetch_candidates = jp
    try:
        return impact.for_trip(
            from_stop=A,
            to_stop=to_stop,
            window_start=now + timedelta(minutes=window_min[0]),
            window_end=now + timedelta(minutes=window_min[1]),
            select="worst",
            with_alternative=True,
            min_impact=min_impact,
        )
    finally:
        alternatives.fetch_candidates = original
        _index.clear()


def test_the_public_endpoint_cannot_spend_the_pollers_fetch_budget():
    """`for_trip` charges its fetches to the caller's own budget, defaulting to the public one.

    `alternatives` keeps two per-minute fetch tallies so an unauthenticated caller flooding
    `/impact` with invented stop pairs cannot starve the push cycle it shares one module-global
    cache with. That reservation is inert unless the path actually reaches `fetch_candidates`, and
    an unwired `path` fails open in the wrong direction — silently, since both budgets refill every
    minute and nothing else would ever notice. Hence a test on the argument itself.

    The default is the conservative side: a caller that says nothing is treated as public.
    """
    now = datetime.now(UTC)
    scope = _scope(
        S1=_live_train(
            now, "late", dep_min=10, arr_min=50,
            p_impact_dep=0.90, arr_delay_p50_stack_min=40,
        )
    )

    jp = _JourneyPlanner([_live_candidate(now, dep_min=15, arr_min=45)])
    _for_trip(scope, jp, now=now)
    assert [c["path"] for c in jp.calls] == ["request"], "an unwired caller is charged publicly"

    poller = _JourneyPlanner([_live_candidate(now, dep_min=15, arr_min=45)])
    _install_snapshot(scope)
    original = alternatives.fetch_candidates
    alternatives.fetch_candidates = poller
    try:
        impact.for_trip(
            from_stop=A,
            to_stop=B,
            window_start=now - timedelta(minutes=5),
            window_end=now + timedelta(minutes=120),
            select="worst",
            with_alternative=True,
            path="poller",
        )
    finally:
        alternatives.fetch_candidates = original
    assert [c["path"] for c in poller.calls] == ["poller"], (
        "main._varsel_impact passes this; without it the poller competes with the public endpoint"
    )


def test_a_cancelled_train_is_offered_a_replacement_that_arrives_late():
    """The end-to-end case the whole rewrite exists for, and the one the old rule could not answer.

    ET has declared the traveller's train cancelled, and nothing gets them there by the time the
    timetable promised. The old rule demanded three minutes SAVED, so it offered nothing at all —
    to the one traveller who has no train. The fallback tier offers the earliest arrival there is,
    even though it lands half an hour after the original was due, and says so in the signs of
    `minutes_saved` and `arrives_before_original` rather than by dressing it up as faster.
    """
    now = datetime.now(UTC)
    jp = _JourneyPlanner([], [_live_candidate(now, dep_min=20, arr_min=80)])
    resp = _for_trip(
        _scope(S1=_live_train(now, "gone", dep_min=10, arr_min=50, et_declared_cancelled=True)),
        jp,
        now=now,
    )
    alt = resp.alternative_journey
    assert alt is not None, "a cancelled train must not leave the traveller with no answer"
    assert alt.arrives_before_original is False
    assert alt.minutes_saved == -30, "signed, and honest about arriving later"
    assert resp.original_is_best is False
    assert [c["arrive_by"] for c in jp.calls] == [True, False], (
        "deadline first, then the depart-after fallback the empty reply calls for"
    )


def test_a_two_minute_delay_leaves_the_traveller_on_their_own_train():
    """The materiality gate, end to end. Rail alerts are mostly delay-driven and the deadline is the
    AIMED arrival, so without it the systematic advice becomes "abandon your train" — for a delay
    nobody would get out of their seat for."""
    now = datetime.now(UTC)
    jp = _JourneyPlanner([_live_candidate(now, dep_min=15, arr_min=51)])
    resp = _for_trip(
        _scope(
            S1=_live_train(
                now, "slightly-late", dep_min=10, arr_min=50,
                p_impact_dep=0.20, arr_delay_p50_stack_min=2,
            )
        ),
        jp,
        now=now,
    )
    assert resp.alternative_journey is None
    assert resp.original_is_best is True, (
        "a VERDICT: the pool was real and the traveller's own train won it"
    )
    assert resp.alternative_subject == ("VYG", "slightly-late")


def test_forty_minutes_late_with_an_earlier_arrival_available_is_offered():
    """The other side of the same gate: once the train is genuinely in trouble, the earlier arrival
    is exactly what should be said."""
    now = datetime.now(UTC)
    jp = _JourneyPlanner([_live_candidate(now, dep_min=15, arr_min=45)])
    resp = _for_trip(
        _scope(
            S1=_live_train(
                now, "very-late", dep_min=10, arr_min=50,
                p_impact_dep=0.93, arr_delay_p50_stack_min=40,
            )
        ),
        jp,
        now=now,
    )
    alt = resp.alternative_journey
    assert alt is not None
    assert alt.minutes_saved == 45 and alt.arrives_before_original is True
    assert resp.original_is_best is False
    assert len(jp.calls) == 1, "a usable first reply must not cost a second query"


def test_the_original_is_read_off_one_cell_not_assembled_from_several():
    """The deadline, the departure and the predicted arrival all come from the WORST cell.

    `SituationImpact.jkey` already warns that `arrival_prediction` is picked worst-first over
    arrivals independently and may describe a different train. Under the rule this replaced that
    only shifted a filter; here the same time is the traveller's deadline and decides which
    candidate wins — so a worst-arrival train on another line would push the deadline hours out and
    hand the answer to whatever departs last before it.
    """
    now = datetime.now(UTC)
    scope = _scope(
        S1=(
            # Worst DEPARTURE (0.95) and a mild arrival: the train the alert is actually about.
            _live_train(now, "boarded", dep_min=10, arr_min=50, line="VYG:Line:R10",
                        p_impact_dep=0.95, p_impact=0.10, arr_delay_p50_stack_min=5)
            # Worst ARRIVAL (0.99), five hours out, on another line entirely.
            + _live_train(now, "elsewhere", dep_min=12, arr_min=300, line="VYG:Line:R11",
                          p_impact_dep=0.30, p_impact=0.99)
        )
    )
    jp = _JourneyPlanner([_live_candidate(now, dep_min=15, arr_min=45)])
    resp = _for_trip(scope, jp, now=now, window_min=(-5, 360))
    assert jp.calls[0]["date_time"] == now + timedelta(minutes=50), (
        "the deadline is the boarded train's aimed arrival, not the other line's"
    )
    # 55 (boarded: aimed 50 + 5 min predicted) - 45. Read off "elsewhere" it would be 255.
    assert resp.alternative_journey is not None
    assert resp.alternative_journey.minutes_saved == 10
    assert resp.alternative_subject == ("VYG", "boarded")


def test_the_deadline_is_the_appointment_not_the_delayed_trains_own_arrival():
    """Two anchors go into the selection and they must not be collapsed into one.

    `original_arrival_time` is the AIMED arrival — when the traveller needs to be there. The
    predicted arrival is when their train will actually manage it. Passing the predicted time as the
    deadline lets a delayed train move the appointment: everything that arrives before the train
    would have becomes "in time", and the rule then keeps the LATEST such departure. Here that hands
    the answer to a journey landing 25 minutes after the traveller was due.
    """
    now = datetime.now(UTC)
    in_time = _live_candidate(now, dep_min=15, arr_min=49, ordinal=0)
    too_late = _live_candidate(now, dep_min=40, arr_min=75, ordinal=1,
                               refs=(("VYG", "DatedServiceJourney:late"),))
    jp = _JourneyPlanner([in_time, too_late])
    resp = _for_trip(
        _scope(
            S1=_live_train(now, "j1", dep_min=10, arr_min=50,
                           p_impact_dep=0.93, arr_delay_p50_stack_min=30)
        ),
        jp, now=now,
    )
    alt = resp.alternative_journey
    assert alt is not None
    assert alt.arrival_time == now + timedelta(minutes=49), (
        "the +75 journey beats the deadline only if the deadline is the delayed arrival"
    )
    assert alt.minutes_saved == 31


def test_the_journey_planners_gloomier_view_of_the_disrupted_train_wins():
    """JP's reply routinely contains the disrupted train's own pattern, giving two readings of one
    arrival: our p50 stack and the operator's published realtime picture. The later one is taken.

    The asymmetry is the argument. Telling somebody their train is fine when it is not strands them;
    offering an alternative to a train that turns out fine costs them a choice they can decline.
    """
    now = datetime.now(UTC)
    own_pattern = _live_candidate(now, dep_min=10, arr_min=70, refs=(("VYG", "j1"),),
                                  lines=("VYG:Line:R10",))
    rival = _live_candidate(now, dep_min=15, arr_min=45, ordinal=1)
    jp = _JourneyPlanner([own_pattern, rival])
    resp = _for_trip(
        _scope(
            S1=_live_train(now, "j1", dep_min=10, arr_min=50,
                           p_impact_dep=0.93, arr_delay_p50_stack_min=5)
        ),
        jp, now=now,
    )
    alt = resp.alternative_journey
    assert alt is not None
    # 70 (JP) - 45, not 55 (our aimed + p50) - 45. The disrupted train's own pattern is still
    # excluded from the pool -- it is read for its arrival, never offered back.
    assert alt.minutes_saved == 25
    assert alt.arrival_time == now + timedelta(minutes=45)


def test_an_empty_first_query_falls_back_to_a_depart_after_search():
    """Two queries, and only when the first came back with nothing the traveller could take.

    A reply consisting solely of the disrupted train's own pattern is an empty answer wearing a full
    one — JP does not know we are alerting on it — so it counts as empty here.
    """
    now = datetime.now(UTC)
    scope = _scope(S1=_live_train(now, "j1", dep_min=10, arr_min=50, p_impact_dep=0.93))
    usable = _live_candidate(now, dep_min=15, arr_min=45)
    own_pattern = _live_candidate(now, dep_min=10, arr_min=50, refs=(("VYG", "j1"),),
                                  lines=("VYG:Line:R10",))
    departed = _live_candidate(now, dep_min=-10, arr_min=40)

    one = _JourneyPlanner([usable])
    _for_trip(scope, one, now=now)
    assert len(one.calls) == 1

    for label, first in (("only its own pattern", own_pattern), ("only departed trains", departed)):
        jp = _JourneyPlanner([first], [usable])
        resp = _for_trip(scope, jp, now=now)
        assert len(jp.calls) == 2, f"{label} must trigger the depart-after fallback"
        assert jp.calls[1]["arrive_by"] is False
        assert jp.calls[1]["date_time"] == jp.calls[1]["now"], "the fallback anchors on now"
        assert resp.alternative_journey is not None, f"{label}: the fallback must be used"


def test_a_first_query_that_obtained_no_reply_does_not_buy_a_second_one():
    """The fallback exists for "JP answered, and had nothing", not for "JP did not answer".

    Every non-answer — timeout, GraphQL `errors`, the post-failure cooldown, single-flight, a spent
    fetch budget, a refused ref — fails open to the same `[]` an answered-but-empty search returns.
    Reading that as "nothing available" issued a SECOND blocking 3 s query on the ingestion thread,
    so a degraded Journey Planner cost twice what a healthy one does.
    """
    now = datetime.now(UTC)
    scope = _scope(S1=_live_train(now, "j1", dep_min=10, arr_min=50, p_impact_dep=0.93))

    silent = _JourneyPlanner([], answered=False)
    resp = _for_trip(scope, silent, now=now)
    assert len(silent.calls) == 1, "an unanswered query is not evidence that nothing is available"
    assert resp.alternative_journey is None
    assert resp.original_is_best is False, "and silence is still not a stay-put verdict"

    # The contrast, on the same scope: an answered-but-empty first query DOES earn the fallback.
    answered = _JourneyPlanner([], [_live_candidate(now, dep_min=15, arr_min=45)])
    resp = _for_trip(scope, answered, now=now)
    assert [c["arrive_by"] for c in answered.calls] == [True, False]
    assert resp.alternative_journey is not None


def test_the_stay_put_verdict_needs_the_selections_own_pool():
    """`original_is_best` was derived from a local pre-filter that applied no window bound and never
    held the original at all — so it claimed "the traveller should keep their train" for cases the
    selection never compared. Here the traveller's train is cancelled (dropped from the pool) and
    every candidate departs past the window's catch-up grace (dropped too): the selection saw an
    empty pool and established nothing.

    `varsel._reconcile_caches` acts on this verdict — it evicts a cached alternative and the wording
    written from it — so a false one is not cosmetic. And "stay on the train ET has cancelled" is
    the worst answer this endpoint can give.
    """
    now = datetime.now(UTC)
    # In the future, so the old pre-filter counted it — but far beyond window_end + 90 min grace,
    # so the selection drops it. That gap is the whole finding below.
    too_far = _live_candidate(now, dep_min=250, arr_min=300)
    jp = _JourneyPlanner([too_far])
    resp = _for_trip(
        _scope(S1=_live_train(now, "gone", dep_min=10, arr_min=50, et_declared_cancelled=True)),
        jp, now=now,
    )
    assert resp.alternative_journey is None
    assert resp.original_is_best is False, "nothing was compared, so nothing may be claimed"


def test_a_reply_the_selection_cannot_use_still_earns_the_fallback():
    """The reply was "full" and the pool was empty, and the fallback was decided on the first one.

    The old rule asked "did the query come back with anything?" — a reply consisting entirely of
    departures beyond the traveller's own window answers YES, so no depart-after query was issued
    and the traveller was told there was no alternative. The population that hits this is the one
    that needs it most: somebody whose train is cancelled, for whom the depart-after query is the
    only tier that can still answer. The question is now "did anything survive the selection?".
    """
    now = datetime.now(UTC)
    unusable = _live_candidate(now, dep_min=250, arr_min=300)  # past window_end + 90 min grace
    rescue = _live_candidate(now, dep_min=15, arr_min=75)      # later than aimed, still catchable
    jp = _JourneyPlanner([unusable], [rescue])
    resp = _for_trip(
        _scope(S1=_live_train(now, "gone", dep_min=10, arr_min=50, et_declared_cancelled=True)),
        jp, now=now,
    )
    assert [c["arrive_by"] for c in jp.calls] == [True, False], "the fallback must be asked"
    assert resp.alternative_journey is not None, "and its answer must reach the traveller"
    assert resp.alternative_journey.arrives_before_original is False, (
        "later than they planned, which is the honest answer for a cancelled train"
    )


def test_the_selection_failure_log_carries_a_digest_not_the_travellers_stops():
    """On the poller path `from_stop` and `to_stop` ARE a registered traveller's own journey — the
    pair `/historikk-api` refuses to serve and `llm_message` keeps out of Cloud Logging by default.
    The same change scrubbed the identical exposure one module over (`_KeyedTTLCache.get` logs a
    digest label now); this record, in the guard around the whole lookup, kept interpolating both.
    """
    now = datetime.now(UTC)
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    def boom(*a, **k):
        raise RuntimeError("boom")

    handler = _Capture()
    impact.logger.addHandler(handler)
    original = alternatives.select_best
    alternatives.select_best = boom
    try:
        resp = _for_trip(
            _scope(S1=_live_train(now, "j1", dep_min=10, arr_min=50, p_impact_dep=0.93)),
            _JourneyPlanner([_live_candidate(now, dep_min=15, arr_min=45)]),
            now=now,
        )
    finally:
        alternatives.select_best = original
        impact.logger.removeHandler(handler)

    assert resp.alternative_journey is None, "it still fails open"
    failures = [r for r in records if getattr(r, "fields", {}).get("reason") == "selection_failed"]
    assert failures, "the failure must still be visible"
    for record in failures:
        fields = getattr(record, "fields", {})
        # Everything a log backend could publish from one record, the stack trace included.
        text = " ".join(
            [record.getMessage(), repr(fields)]
            + ([logging.Formatter().formatException(record.exc_info)] if record.exc_info else [])
        )
        assert A not in text and B not in text, f"the record publishes a stop ref: {text}"
        assert fields["pair_digest"], "and it must still be correlatable"


def test_the_lookup_gate_admits_everything_that_could_earn_a_push():
    """varsel passes `alert_policy.min_threshold()`, the floor of the per-operator table, so a trip
    sitting exactly on it still gets a lookup — the requirement is met by fixing selection, not by
    widening this gate (see `_worth_an_alternative`)."""
    now = datetime.now(UTC)
    gate = alert_policy.min_threshold()
    for p_impact, expected in ((gate, True), (gate - 0.01, False)):
        jp = _JourneyPlanner([_live_candidate(now, dep_min=15, arr_min=45)])
        _for_trip(
            _scope(S1=_live_train(now, "j1", dep_min=10, arr_min=50, p_impact_dep=p_impact)),
            jp, now=now, min_impact=gate,
        )
        assert bool(jp.calls) is expected, f"p_impact={p_impact} against the floor {gate}"


def test_a_deadline_beyond_the_lookahead_horizon_costs_no_query_at_all():
    """Including the fallback. It anchors on `now`, so it clears `fetch_candidates`' own horizon
    guard on its own and would fire for a trip nobody can act on for hours — once per poller cycle,
    from the moment the journey is registered."""
    now = datetime.now(UTC)
    jp = _JourneyPlanner([_live_candidate(now, dep_min=400, arr_min=430)])
    resp = _for_trip(
        _scope(S1=_live_train(now, "tomorrow", dep_min=390, arr_min=420, p_impact_dep=0.93)),
        jp, now=now, window_min=(-5, 480),
    )
    assert jp.calls == [], "7 hours out is outside the 6 h lookahead, on both queries"
    assert resp.alternative_journey is None and resp.original_is_best is False


class _PerDeadlinePlanner:
    """A `fetch_candidates` double that answers by ANCHOR rather than by call order.

    The queue-based `_JourneyPlanner` above cannot express the question these tests ask — "what did
    you offer the 06:00 train, and what did you offer the 12:00 one" — because its replies are keyed
    on the order the queries happened to arrive in, which is the very thing under test.
    """

    def __init__(self, by_offset_min, now):
        self.by_offset_min = by_offset_min  # deadline offset from `now` (min) -> candidates
        self.now = now
        self.calls: list[dict] = []

    def __call__(self, from_stop, to_stop, *, arrive_by, date_time, now, path="request",
                 outcome=None):
        self.calls.append({"arrive_by": arrive_by, "date_time": date_time})
        if outcome is not None:
            outcome["answered"] = True
        if not arrive_by:
            return []  # the fallback tier is not what these tests are about
        offset = round((date_time - self.now).total_seconds() / 60)
        return list(self.by_offset_min.get(offset, []))

    def deadlines(self):
        """The anchors of the deadline queries, as minutes from `now`, in the order asked."""
        return [
            round((c["date_time"] - self.now).total_seconds() / 60)
            for c in self.calls
            if c["arrive_by"]
        ]


def test_each_disrupted_train_is_answered_against_its_own_deadline():
    """The regression behind the 2026-08-12 fix, at the layer that caused it.

    `select_best`'s in-time tier is "the latest departure that still arrives before the disrupted
    train's AIMED arrival", so the winner is chosen against one train's deadline. One lookup per
    window therefore answered every train in it with the worst train's answer — measured in prd, a
    registered journey whose 00:31 cycle alerted on thirteen cancelled L1 departures between 05:59
    and 11:59 offered all thirteen the 05:45.
    """
    now = datetime.now(UTC)
    jp = _PerDeadlinePlanner(
        {
            50: [_live_candidate(now, dep_min=5, arr_min=45)],
            230: [_live_candidate(now, dep_min=180, arr_min=225)],
        },
        now,
    )
    resp = _for_trip(
        _scope(
            SOON=_live_train(now, "soon", dep_min=10, arr_min=50, p_impact_dep=0.93,
                             situation_number="SOON"),
            LATER=_live_train(now, "later", dep_min=190, arr_min=230, p_impact_dep=0.91,
                              line="VYG:Line:R12", situation_number="LATER"),
        ),
        jp, now=now, window_min=(-5, 300),
    )
    assert sorted(jp.deadlines()) == [50, 230], "one deadline query per train, its own"
    offered = {
        s.jkey: round((s.alternative_journey.departure_time - now).total_seconds() / 60)
        for s in resp.situations
        if s.alternative_journey is not None
    }
    assert offered == {"soon": 5, "later": 180}, offered
    assert {s.jkey: s.alternative_subject for s in resp.situations} == {
        "soon": ("VYG", "soon"),
        "later": ("VYG", "later"),
    }, "each answer names the train it is an answer about"


def test_a_train_past_the_horizon_gets_nothing_rather_than_a_nearer_trains_answer():
    """The prd symptom itself: an 11:59 departure told to catch the 05:45.

    The horizon guard was always there; under one lookup per window it only ever screened the WORST
    train, and every other train in the window inherited that train's answer however far off it was.
    Per train, it finally applies to the train being advised — and "we did not establish that" is the
    honest answer for a departure nobody can act on for hours.
    """
    now = datetime.now(UTC)
    jp = _PerDeadlinePlanner({50: [_live_candidate(now, dep_min=5, arr_min=45)]}, now)
    resp = _for_trip(
        _scope(
            SOON=_live_train(now, "soon", dep_min=10, arr_min=50, p_impact_dep=0.93,
                             situation_number="SOON"),
            FAR=_live_train(now, "far", dep_min=420, arr_min=460, p_impact_dep=0.99,
                            line="VYG:Line:R12", situation_number="FAR"),
        ),
        jp, now=now, window_min=(-5, 500),
    )
    assert jp.deadlines() == [50], "7 h out is past the lookahead, and no query is issued for it"
    far = next(s for s in resp.situations if s.jkey == "far")
    soon = next(s for s in resp.situations if s.jkey == "soon")
    assert soon.alternative_journey is not None, "the near train still gets its answer"
    assert far.alternative_journey is None, "and the far one is not handed that answer"
    assert far.alternative_subject is None, (
        "nor is it recorded as a lookup that ran — `not_worth_lookup`, not `none_found`"
    )


def test_several_situations_about_one_train_share_a_single_lookup():
    """Operators publish several situations about one disruption (varsel.one_alert_per_train exists
    for the same reason). They name one train, so they share a deadline and an answer — per TRAIN is
    what bounds the cost, and per situation would multiply a blocking 3 s call by however many
    messages the operator felt like sending."""
    now = datetime.now(UTC)
    jp = _PerDeadlinePlanner({50: [_live_candidate(now, dep_min=5, arr_min=45)]}, now)
    resp = _for_trip(
        _scope(
            S1=_live_train(now, "j1", dep_min=10, arr_min=50, p_impact_dep=0.93,
                           situation_number="S1"),
            S2=_live_train(now, "j1", dep_min=10, arr_min=50, p_impact_dep=0.93,
                           situation_number="S2", summary="Ta neste tog"),
        ),
        jp, now=now,
    )
    assert jp.deadlines() == [50], "one train, one query"
    assert len(resp.situations) == 2
    assert all(s.alternative_journey is not None for s in resp.situations), (
        "and both situations carry it"
    )


def test_the_lookup_budget_goes_to_the_soonest_departures():
    """A wide window during a mass cancellation has no natural bound on how many trains are
    alert-worthy, and each lookup is a blocking ~3 s call on the ingestion thread. The cap spends the
    budget on the next departures — the ones somebody on a platform can still act on — and answers
    the rest with silence rather than with a neighbour's answer."""
    now = datetime.now(UTC)
    trains = {
        f"S{i}": _live_train(now, f"j{i}", dep_min=10 + i * 30, arr_min=50 + i * 30,
                             p_impact_dep=0.93, line=f"VYG:Line:R{i}",
                             situation_number=f"S{i}")
        for i in range(5)
    }
    jp = _PerDeadlinePlanner(
        {50 + i * 30: [_live_candidate(now, dep_min=5 + i * 30, arr_min=45 + i * 30)]
         for i in range(5)},
        now,
    )
    resp = _for_trip(_scope(**trains), jp, now=now, window_min=(-5, 300))
    assert jp.deadlines() == [50, 80, 110], (
        "the three soonest deadlines, in order, and nothing past the cap"
    )
    answered = {s.jkey for s in resp.situations if s.alternative_subject is not None}
    assert answered == {"j0", "j1", "j2"}, answered
    assert all(
        s.alternative_journey is None for s in resp.situations if s.jkey in {"j3", "j4"}
    ), "the trains past the cap are told nothing, not told the 06:00 train's answer"


def test_the_chosen_alternative_carries_what_we_know_about_its_own_boarding():
    """The winner publishes its own numbers — JP's observed picture beside our forecast for it.

    They ride on the candidate rather than only on the `CandidateScore`, because `select_best`
    answers with a journey and nothing downstream could join the winner back to its score.
    """
    now = datetime.now(UTC)
    index = {
        ("VYG", ALT_JKEY): [
            FakeCall(jkey=ALT_JKEY, operator="VYG", line="VYG:Line:R11", stop_place=A,
                     stop_order=1, aimed_dep=now + timedelta(minutes=15), stop_name="Oslo S",
                     p_cancel=0.04, p_impact_dep=0.11),
        ]
    }
    jp = _JourneyPlanner([_live_candidate(now, dep_min=15, arr_min=45)])
    resp = _for_trip(
        _scope(S1=_live_train(now, "j1", dep_min=10, arr_min=50, p_impact_dep=0.93,
                              arr_delay_p50_stack_min=40)),
        jp, now=now, index=index,
    )
    alt = resp.alternative_journey
    assert alt is not None
    dep = alt.departure_prediction
    assert dep is not None and dep.stop_place == A and dep.stop_name == "Oslo S"
    assert dep.scored is True and dep.impact_probability == 0.11
    # The identity survives selection too, which is the only reason `decisions` can record it: the
    # winner reaches the response as an AlternativeJourney, and nothing downstream could join it
    # back to the CandidateScore the scorer returned.
    assert (dep.operator, dep.jkey, dep.stop_order) == ("VYG", ALT_JKEY, 1)
    assert alt.at_risk is False, "0.11 is far under VYG's gate"
    # Against the AIMED arrival (+50), which is the traveller's appointment — not against the
    # predicted one (+90), which is `minutes_saved`'s anchor and a different question.
    assert alt.arrives_before_original is (alt.arrival_time < now + timedelta(minutes=50)), (
        "the flag must agree with the arrival comparison it summarises"
    )


def test_every_step_of_the_lookup_fails_open_on_its_own():
    """Fetch, scoring and selection each get to raise, and the rest of the response must survive.

    Not bookkeeping. This runs on the poller thread inside varsel.check_and_notify's per-journey
    guard, which on 2026-07-29 turned one malformed Journey Planner pattern into every alert for
    that journey being silently dropped for the cycle. The guard that caught it lived inside the
    function this path replaced, and this path has two more places to raise from.
    """
    now = datetime.now(UTC)
    scope = _scope(S1=_live_train(now, "j1", dep_min=10, arr_min=50, p_impact_dep=0.93))
    good = _live_candidate(now, dep_min=15, arr_min=45)

    def boom(*a, **k):
        raise RuntimeError("boom")

    cases = {
        "fetch_candidates": (_JourneyPlanner(raises=True), impact, None),
        "score_candidates": (_JourneyPlanner([good]), impact, "score_candidates"),
        "select_best": (_JourneyPlanner([good]), alternatives, "select_best"),
    }
    for label, (jp, module, attr) in cases.items():
        original = getattr(module, attr) if attr else None
        if attr:
            setattr(module, attr, boom)
        try:
            resp = _for_trip(scope, jp, now=now)
        finally:
            if attr:
                setattr(module, attr, original)
        assert resp.alternative_journey is None, f"{label}: no alternative"
        assert resp.original_is_best is False, f"{label}: silence is not a stay-put verdict"
        assert resp.situations and resp.lines, f"{label}: the rest of the answer must survive"
        assert resp.situations[0].departure_prediction.impact_probability == 0.93


def test_a_forgotten_stub_cannot_reach_the_network():
    """Belt and braces for the whole suite, and the reason it exists is recorded in test_varsel_api.

    Every stub above names a function, so re-pointing the call chain under them disarms them
    silently — and this module fails open, so a live request from CI would eat a 3 s timeout, log a
    warning nobody reads, and leave the assertion under it passing. This guard needs nobody to
    remember anything: a test process cannot open the socket at all.
    """
    assert alternatives._network_allowed() is False, "a test process is not allowed to dial out"
    try:
        alternatives._post_json("https://api.entur.io/journey-planner/v3/graphql", {}, {})
    except RuntimeError as exc:
        assert "test process" in str(exc)
    else:
        raise AssertionError("_post_json must refuse to open a socket from a test process")

    # And it is an explicit opt-in, so a deliberate live check against the real Journey Planner is
    # still possible — the flag is the only way to get there.
    before = os.environ.get(alternatives._ALLOW_NETWORK_ENV)
    os.environ[alternatives._ALLOW_NETWORK_ENV] = "1"
    try:
        assert alternatives._network_allowed() is True
    finally:
        if before is None:
            os.environ.pop(alternatives._ALLOW_NETWORK_ENV, None)
        else:
            os.environ[alternatives._ALLOW_NETWORK_ENV] = before


# ---- response contract ------------------------------------------------------------------------
def test_impact_response_shape_matches_the_togpuls_contract():
    """togpuls/clients/disruptions.py reads exactly two things: a `situations` list, and a
    non-empty `situation_number` on each entry. Everything else it ignores."""
    scope = _scope(S1=_train("j1", 8))
    cells = _cells(scope)
    resp = impact._impact_response(
        cells, from_stop=A, to_stop=B, refreshed_at=None
    )
    payload = resp.model_dump(mode="json")
    assert isinstance(payload["situations"], list) and payload["situations"]
    for sit in payload["situations"]:
        assert isinstance(sit, dict)
        assert isinstance(sit["situation_number"], str) and sit["situation_number"]


def test_no_journey_planner_call_without_a_to_stop():
    """There is no arrival to beat without a destination, so /impact must not reach out at all —
    a public read endpoint should not make an external HTTP call it cannot use."""
    import alternatives

    calls = []
    original = alternatives.fetch_candidates
    alternatives.fetch_candidates = lambda *a, **k: calls.append(1) or []
    _install_snapshot(_scope(S1=_train("j1", 8)))
    try:
        resp = impact.for_trip(
            from_stop=A,
            to_stop=None,
            window_start=_at(7),
            window_end=_at(9),
            select="nearest",
            at=_at(8),
        )
    finally:
        alternatives.fetch_candidates = original
    assert calls == [], "the Journey Planner must not be consulted without a to_stop"
    assert resp.alternative_journey is None


def test_impact_response_carries_the_new_fields():
    scope = _scope(S1=_train("j1", 8, operator="RUT", description="Buss for tog"))
    resp = impact._impact_response(
        _cells(scope), from_stop=A, to_stop=B, refreshed_at=None
    )
    assert resp.situations[0].description == "Buss for tog"
    assert resp.lines[0].operator == "RUT"
    assert resp.alternative_journey is None


def test_situation_carries_the_jkey_of_the_train_the_numbers_describe():
    """The rollup must publish the WORST cell's jkey, not the first cell's.

    Without this the recorded decision would name one train and quote another's numbers, and every
    realized-outcome join built on it would be silently wrong rather than visibly missing.
    """
    scope = _scope(
        S1=_train("r10", 8, line="VYG:Line:R10", p_cancel=0.9, p_impact_dep=0.91)
        + _train("r11", 8, 5, line="VYG:Line:R11", p_cancel=0.1, p_impact_dep=0.99)
    )
    resp = impact._impact_response(
        _cells(scope), from_stop=A, to_stop=B, refreshed_at=None
    )
    sit = resp.situations[0]
    # r10 is first in the group but r11 is the worst, so a group[0] read would return "r10".
    assert sit.jkey == "r11"
    assert (sit.line, sit.departure_prediction.impact_probability) == ("VYG:Line:R11", 0.99)


def test_situation_jkey_is_absent_rather_than_wrong_when_unknown():
    """A cell with no journey ref yields a null jkey, so a consumer can tell "unknown" from a
    value it could otherwise mistake for a real train."""
    import dataclasses

    cells = [dataclasses.replace(c, jkey=None) for c in _cells(_scope(S1=_train("j1", 8)))]
    resp = impact._impact_response(
        cells, from_stop=A, to_stop=B, refreshed_at=None
    )
    assert resp.situations[0].jkey is None


def test_real_http_route_satisfies_the_togpuls_contract():
    """Drive the actual `@app.get("/impact")` route, not just the helpers.

    Review 2026-07-29 pointed out that every other check here calls internals, so query-param
    parsing, the response envelope and HTTP-level serialization of the new fields were untested — the
    contract this file claims to pin was never exercised end to end. This sends exactly what
    togpuls/clients/disruptions.py sends and asserts on exactly what it reads.
    """
    from fastapi.testclient import TestClient

    _install_snapshot(_scope(S1=_train("j1", 8, description="Signalfeil")))
    main._call_pred_refreshed_at = _at(8)
    original = impact.alternatives.fetch_candidates
    impact.alternatives.fetch_candidates = lambda *a, **k: []  # no network from the suite
    try:
        r = TestClient(main.app).get(
            "/impact",
            params={"from_stop": A, "to_stop": B, "at": _at(8).isoformat()},
        )
    finally:
        impact.alternatives.fetch_candidates = original

    assert r.status_code == 200, r.text
    body = r.json()
    # The two things togpuls actually depends on.
    assert isinstance(body["situations"], list) and body["situations"]
    for sit in body["situations"]:
        assert isinstance(sit["situation_number"], str) and sit["situation_number"]
    # And the additive fields, over the wire rather than in-process.
    assert body["situations"][0]["description"] == "Signalfeil"
    assert "latest_observation" in body["lines"][0]
    assert body["lines"][0]["operator"] == "VYG"
    assert body["alternative_journey"] is None
    # The alternative-selection verdict is internal working state for varsel's back-fill, not part
    # of the public contract: `exclude=True` has to hold over the wire, not just in-process.
    assert "original_is_best" not in body and "alternative_subject" not in body


def test_real_http_route_handles_a_trip_with_nothing_affecting_it():
    from fastapi.testclient import TestClient

    _install_snapshot({})
    r = TestClient(main.app).get("/impact", params={"from_stop": A, "to_stop": B})
    assert r.status_code == 200
    body = r.json()
    assert body["situations"] == [] and body["lines"] == []
    assert body["from_stop"] == A and body["to_stop"] == B


def test_impact_endpoint_is_not_a_coroutine():
    """It makes a blocking Journey Planner call, so FastAPI must run it in the threadpool. As
    `async def` it would block the single-worker event loop and freeze /health (review 2026-07-29;
    the rule is documented at reisevalg.py:28)."""
    import inspect

    assert not inspect.iscoroutinefunction(main.impact)


def test_an_unanswered_lookup_is_distinguishable_from_an_empty_one_on_the_response():
    """The journey-alert pull read (2026-09-10) serves `unanswered` for a lookup that obtained no
    reply and `none_found` for one that did and had nothing. Until now the two collapsed into one
    outcome the moment `_best_remaining_journey` returned, and every Alert downstream inherited the
    collapse — so a throttled Journey Planner would have been cached as "no alternative exists".

    Four readings, on one scope. Answered-and-empty: a lookup ran to completion (`subject` set) and
    `answered` is true. A throttled first query, a throttled fallback and a raising lookup: each is
    `answered` false. Capped by `_MAX_ALTERNATIVE_LOOKUPS`: terminal, so it stays true — "we did not
    look" and "we could not get an answer" must not be worded alike either.
    """
    now = datetime.now(UTC)
    scope = _scope(S1=_live_train(now, "j1", dep_min=10, arr_min=50, p_impact_dep=0.93))

    resp = _for_trip(scope, _JourneyPlanner([], []), now=now)
    sit = resp.situations[0]
    assert sit.alternative_subject == ("VYG", "j1") and sit.alternative_journey is None
    assert sit.answered is True, "an answered-but-empty search is a terminal answer"
    assert resp.answered is True, "the headline echoes situations[0]"
    assert "answered" not in sit.model_dump() and "answered" not in resp.model_dump(), (
        "internal working state, kept off the wire like the other two"
    )

    resp = _for_trip(scope, _JourneyPlanner([], answered=False), now=now)
    assert resp.situations[0].answered is False, "a throttled first query"
    assert resp.answered is False

    resp = _for_trip(scope, _JourneyPlanner([], [], answered=[True, False]), now=now)
    assert resp.situations[0].answered is False, "an answered first query, a throttled fallback"

    resp = _for_trip(scope, _JourneyPlanner(raises=True), now=now)
    assert resp.situations[0].answered is False, "a raising lookup obtained no answer"
    assert resp.situations[0].alternative_subject is None, (
        "and the failure branch still leaves the decision-log status untouched"
    )

    capped = _scope(**{
        f"S{i}": _live_train(now, f"j{i}", dep_min=10 + i, arr_min=50 + i, p_impact_dep=0.93,
                             line=f"VYG:Line:R{i}", situation_number=f"S{i}")
        for i in range(impact._MAX_ALTERNATIVE_LOOKUPS + 1)
    })
    resp = _for_trip(capped, _JourneyPlanner(answered=False), now=now)
    by_sit = {s.situation_number: s for s in resp.situations}
    last = f"S{impact._MAX_ALTERNATIVE_LOOKUPS}"
    assert by_sit[last].alternative_subject is None, "control: the last train was capped"
    assert by_sit[last].answered is True, "capped is terminal, not transient"
    assert by_sit["S0"].answered is False, "while the looked-up trains were throttled"


# ---- The inverted prediction index (U2) ---------------------------------------------------------
#
# The matcher's cost must scale with distinct place PAIRS per cache generation, not with the number
# of registered definitions asking about them. Everything below pins that, plus the two things the
# pruning must not change: which cells come out, and in what order.


def _generation(scope, *, refreshed_at, index=None):
    """Install `scope` as a NEW snapshot object stamped with `refreshed_at`.

    A fresh dict every time on purpose: the generation is `(snapshot identity, refreshed_at)`, so a
    test that mutated one in place would be asking the engine to notice a change it deliberately
    cannot see (see `impact.invalidate_generation`).
    """
    _generation_state["snapshot"] = dict(scope)
    _generation_state["refreshed_at"] = refreshed_at
    _generation_state["index"] = index
    impact.invalidate_generation()


_generation_state: dict = {"snapshot": {}, "refreshed_at": None, "index": None}


def _with_generation(fn):
    """Run `fn()` with the engine reading `_generation_state` instead of the module `_snapshot`."""
    original_snapshot = impact._snapshot_provider
    original_index = impact._stop_ref_index_provider
    impact.configure(
        snapshot_provider=lambda: (
            _generation_state["snapshot"],
            _generation_state["refreshed_at"],
        ),
        call_lookup=lambda operator, jkey: _index.get((operator, jkey)),
        stop_ref_index=(
            (lambda: (_generation_state["index"], _generation_state["refreshed_at"]))
            if _generation_state["index"] is not None
            else None
        ),
    )
    try:
        return fn()
    finally:
        impact._snapshot_provider = original_snapshot
        impact._stop_ref_index_provider = original_index
        impact.invalidate_generation()


def _noise(n, *, at_from=A, at_to=C):
    """`n` situations that can never produce a cell for an A -> B trip."""
    return {
        f"N{i}": [
            FakeCall(f"noise{i}", at_from, 1, _at(3), line=f"VYG:Line:N{i}",
                     situation_number=f"N{i}"),
            FakeCall(f"noise{i}", at_to, 5, None, aimed_arr=_at(4), line=f"VYG:Line:N{i}",
                     situation_number=f"N{i}"),
        ]
        for i in range(n)
    }


def test_the_scan_is_bounded_by_the_pair_not_by_the_cache():
    """The whole point of the index: 500 situations that cannot serve this pair are never grouped.

    Before the index `_trip_cells` rebuilt `by_journey`/`by_line_all` for EVERY situation on EVERY
    scan -- once per definition per window per stop-ref pair -- so a cache the size of the network
    (~1 100 situations at the midnight peak) was re-walked for a question about two stops.
    """
    scope = dict(_noise(500))
    scope["S1"] = _train("j1", 8)
    seen: list[int] = []
    original = impact._trip_cells

    def counting(scope_arg, *args, **kwargs):
        seen.append(len(scope_arg))
        return original(scope_arg, *args, **kwargs)

    _generation(scope, refreshed_at=_at(7))
    impact._trip_cells = counting
    try:
        resp = _with_generation(
            lambda: impact.for_trip(
                from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
                select="worst", with_alternative=False,
            )
        )
    finally:
        impact._trip_cells = original
    assert [s.situation_number for s in resp.situations] == ["S1"], "the real situation still matches"
    assert seen == [1], f"scanned {seen} situations, want exactly the one that can emit"


def test_the_pair_and_window_memos_are_built_once_for_many_definitions():
    """200 definitions sharing one (pair, window) pay for the grouping once, not 200 times."""
    scope = dict(_noise(20))
    scope["S1"] = _train("j1", 8)
    _generation(scope, refreshed_at=_at(7))

    def run():
        for _ in range(200):
            impact.for_trip(
                from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
                select="worst", with_alternative=False,
            )
        return impact.index_stats()

    stats = _with_generation(run)
    assert stats["generation_builds"] == 1, stats
    assert stats["pair_builds"] == 1, stats
    assert stats["window_builds"] == 1, stats


def test_an_unaligned_window_is_answered_but_never_filed():
    """The public endpoint must not be able to grow the window memo one dead entry per request.

    `/impact` derives its window as `at ± main._IMPACT_AT_TOLERANCE` off `datetime.now(UTC)`, so no
    two requests ever present the same one and a filed entry could never be hit again — for the life
    of the generation, on a public unauthenticated endpoint. The poller's windows come from
    `varsel.journey_windows` and ARE minute-aligned, which is the sharing the memo exists for, so
    the guard is on the write and the answer is identical either way. `at` is deliberately not
    rounded to make them align: `select="nearest"` scores on `abs(aimed_dep - at)`, so that would
    move which train is picked.
    """
    scope = {"S1": _train("j1", 8)}
    _generation(scope, refreshed_at=_at(7))
    # The shape `/impact` presents: three requests a few hundred ms apart, so three `now()` instants
    # carrying seconds and microseconds, no two of them equal. Deliberately three DIFFERENT windows
    # rather than one repeated, because "one dead entry per request" is the leak being pinned.
    ragged = [_at(7) + timedelta(seconds=37, microseconds=412 + i * 190) for i in range(3)]

    def run():
        answers = []
        for start in ragged:
            answers.append(impact.for_trip(
                from_stop=A, to_stop=B, window_start=start,
                window_end=start + timedelta(minutes=180),
                select="nearest", at=_at(8), with_alternative=False,
            ))
        both_ragged = len(impact._index_generation._windows)
        # One end aligned is still not a window anybody else will present.
        impact.for_trip(
            from_stop=A, to_stop=B, window_start=_at(7),
            window_end=_at(9) + timedelta(microseconds=500),
            select="nearest", at=_at(8), with_alternative=False,
        )
        half_ragged = len(impact._index_generation._windows)
        for _ in range(3):
            answers.append(impact.for_trip(
                from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
                select="nearest", at=_at(8), with_alternative=False,
            ))
        return answers, both_ragged, half_ragged, len(
            impact._index_generation._windows
        ), impact.index_stats()

    answers, both_ragged, half_ragged, aligned, stats = _with_generation(run)
    for resp in answers:
        assert [s.situation_number for s in resp.situations] == ["S1"], "the answer is unchanged"
    assert both_ragged == 0, f"{both_ragged} dead entries filed for unaligned windows"
    assert half_ragged == 0, "a half-aligned window was filed"
    assert aligned == 1, "the poller's minute-aligned window was not filed"
    # 3 unaligned + 1 half-aligned, each recomputed; then ONE build for the three aligned asks.
    assert stats["window_builds"] == 5, stats
    # And the pair memo is shared by all seven, which is what makes declining to file the window
    # cheap: an unaligned caller re-walks nothing that scales with the cache.
    assert stats["pair_builds"] == 1, stats


def test_a_new_refreshed_at_drops_both_memos():
    """A rebuild stamps a new generation, and the next scan must not answer off the old one."""
    first = {"S1": _train("j1", 8)}
    _generation(first, refreshed_at=_at(7))

    def run():
        before = impact.for_trip(
            from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
            select="worst", with_alternative=False,
        )
        # A rebuild that dropped the situation and added another.
        _generation_state["snapshot"] = {"S2": _train("j2", 8, 30, situation_number="S2")}
        _generation_state["refreshed_at"] = _at(8)
        after = impact.for_trip(
            from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
            select="worst", with_alternative=False,
        )
        return before, after, impact.index_stats()

    before, after, stats = _with_generation(run)
    assert [s.situation_number for s in before.situations] == ["S1"]
    assert [s.situation_number for s in after.situations] == ["S2"], "the stale memo answered"
    assert stats["generation_builds"] == 2, stats


def test_three_threads_on_one_pair_build_the_memo_once():
    """Pass 1a matches `_MATCH_WORKERS` wide, so the memos meet contention on their first use."""
    import threading

    scope = dict(_noise(50))
    scope["S1"] = _train("j1", 8)
    _generation(scope, refreshed_at=_at(7))
    results: list = []
    errors: list = []

    def run():
        start = threading.Barrier(3)

        def worker():
            try:
                start.wait(timeout=5)
                results.append(
                    impact.for_trip(
                        from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
                        select="worst", with_alternative=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return impact.index_stats()

    stats = _with_generation(run)
    assert not errors, errors
    assert len(results) == 3, "every thread answered"
    for resp in results:
        assert [s.situation_number for s in resp.situations] == ["S1"]
    assert stats["generation_builds"] == 1, stats
    assert stats["pair_builds"] == 1, stats


# ---- Byte identity: the pruned scan against the unpruned one ------------------------------------
#
# The one claim U2 rests on. `for_trip` hands `_trip_cells` only the situations that could emit;
# this asks, over randomized snapshots built to contain every case the matcher has ever been got
# wrong on, whether that produces the same response as handing it the whole cache — field for
# field, including the orders the two stable severity sorts leave behind on a tie.
#
# It is green before this change and green after, by design: it is not a regression test but the
# ORACLE the prune is checked against, and it re-runs on every CI. The red evidence for U2 is
# elsewhere (the build counters, the dropped memo, the mid-pass beat).

_IDENTITY_SEEDS = tuple(range(24))
#: A slice of them re-run through an index shaped and ordered the way `main` builds one (Finding 2).
#: A slice rather than all 24 because what this adds over the run above is the BUILDER, not more
#: snapshots, and the seeds it does run already cover every awkwardness the generator draws.
_INJECTED_SEEDS = _IDENTITY_SEEDS[:8]


def _random_snapshot(rng):
    """A snapshot drawn to contain the cases the matcher's own comments are about.

    Deliberately small in situations and dense in awkwardness: two operators sharing one jkey (bus
    ServiceJourney refs are not date-scoped), both boarding activities closed in both positions,
    `PASS_THRU` (both closed at once), a circular route calling the boarding stop twice, departures
    exactly on each window edge, declared cancellations, and calls that reach `to_stop` in the wrong
    direction.
    """
    scope: dict[str, list] = {}
    # Insertion order deliberately NOT alphabetical: the pruned scope has to be rebuilt in the
    # snapshot's OWN order, and a prune that sorted by name would look right against `S0, S1, ...`
    # while quietly reordering `situations`/`lines` on every severity tie.
    names = [f"S{i}" for i in range(rng.randint(1, 4))]
    rng.shuffle(names)
    for sit in names:
        # The snapshot KEY is what scopes a scan; `CallPred.situation_number` is what the emitted
        # cell is labelled with. They agree in production (`main` keys `by_situation` on the same
        # `situationNumber`) and the suites rely on their being able to differ -- so the index must
        # carry the key and never read the field back off the call. Drawn apart sometimes, which is
        # what makes that a checked property rather than a comment.
        field_sit = sit if rng.random() < 0.7 else rng.choice(["S0", "S9", "ALIAS"])
        calls: list = []
        for t in range(rng.randint(1, 4)):
            jkey = rng.choice(["j1", "j2", "shared"])
            operator = rng.choice(["VYG", "VYG", "SKY"])  # `shared` + two operators is the trap
            line = rng.choice(["VYG:Line:R10", "VYG:Line:L1", None])
            # Window edges at 07:00 and 09:00, so these land inside, outside and exactly on them.
            minute = rng.choice([-1, 0, 1, 30, 59, 60, 90, 119, 120, 121])
            dep = _at(7) + timedelta(minutes=minute)
            board_order = rng.choice([1, 1, 3])
            common = {
                "situation_number": field_sit, "operator": operator, "line": line,
                "p_impact_dep": rng.choice([None, 0.1, 0.66, 0.9, 0.9]),
                "p_cancel": rng.choice([0.05, 0.5, 0.9]),
                "dep_delay_p90_stack_min": rng.choice([None, 5, 40]),
                "dep_delay_p50_stack_min": rng.choice([None, 0, 25]),
                "et_declared_cancelled": rng.random() < 0.2,
                "has_situation": rng.random() < 0.8,
            }
            calls.append(FakeCall(
                jkey, A, board_order, dep,
                boarding_allowed=rng.random() > 0.25, alighting_allowed=rng.random() > 0.25,
                **common,
            ))
            if rng.random() < 0.3:  # circular route: the same train calls A again, later
                calls.append(FakeCall(
                    jkey, A, board_order + 4, dep + timedelta(minutes=rng.choice([7, 45])),
                    boarding_allowed=rng.random() > 0.25,
                    alighting_allowed=rng.random() > 0.25, **common,
                ))
            for stop in (B, C):
                if rng.random() < 0.8:
                    order = rng.choice([board_order - 1, board_order + 2, board_order + 6])
                    calls.append(FakeCall(
                        jkey, stop, order, None,
                        aimed_arr=dep + timedelta(minutes=20 + order),
                        alighting_allowed=rng.random() > 0.25,
                        boarding_allowed=rng.random() > 0.25, **common,
                    ))
            rng.shuffle(calls)
        scope[sit] = calls
    return scope


def _identity_queries(rng, scope):
    """The query shapes `for_trip` is reachable with, including the two narrowings.

    Both memo DIMENSIONS vary across the list -- `to_stop` over {B, C, None} and the window over two
    values -- because one generation now serves this whole list (see `_assert_prune_identity`), so
    a memo that returned the WRONG entry is observable here and nowhere else.

    The order is load-bearing for the window half. The two windows overlap without nesting, but a
    memo that ignored the window serves whichever answer it built FIRST to every later query for
    that pair, so the later query is the one that has to MISS a situation for the oracle to see it.
    `_WINDOW_LATE` therefore comes first for each `to_stop` value: asked widest-first the mutation
    would hand out a superset and stay green, for exactly the reason falsification 7 below stays
    green.
    """
    def q(**kw):
        return {
            "to_stop": B, "select": "worst", "at": None, "catchable_after": None,
            "window": _WINDOW_MAIN, **kw,
        }

    queries = [
        # The second window first, once per `to_stop`, for the reason in the docstring above.
        q(window=_WINDOW_LATE),
        q(to_stop=None, window=_WINDOW_LATE),
        q(to_stop=C, window=_WINDOW_LATE, select="nearest", at=_at(9)),
        q(),
        q(to_stop=None),
        q(select="nearest", at=_at(8)),
        q(to_stop=C, select="nearest", at=_at(7, 30)),
        q(catchable_after=_at(8)),
        q(select="nearest", at=_at(8), catchable_after=_at(7, 45)),
    ]
    queries += [q(situation=sit) for sit in scope]
    queries.append(q(situation="nope"))
    return queries


#: The window most queries ask about: its edges are the 07:00/09:00 the generator draws departures
#: exactly on, one minute inside and one minute outside.
_WINDOW_MAIN = (_at(7), _at(9))
#: A second window, deliberately OVERLAPPING rather than nested: departures at 07:00-07:59 are in
#: `_WINDOW_MAIN` alone and one at 09:01 is in this one alone, so neither window's situation set is
#: a superset of the other's and a memo that confused them drops rows in either direction.
_WINDOW_LATE = (_at(8), _at(10))


def _assert_prune_identity(seeds, *, index_builder=None):
    """Run the differential over `seeds`, returning the number of responses compared.

    **One generation serves a seed's WHOLE query list**, which is what makes the memos observable.
    Installed per QUERY -- as this was until 2026-09-12 -- every generation served exactly one
    question, so neither memo ever held a second entry and a memo that returned the wrong one had
    nothing to return it instead of. The adversarial reviewer demonstrated that by deleting `to_ref`
    from the pair-memo key and watching all 41 suites stay green. Both halves of the hoist are
    needed and only one of them is obvious: `_with_generation` calls `invalidate_generation()` in
    its `finally`, so hoisting the `_generation(...)` install alone would still drop the memos after
    every query.

    What pins the hoist is the two COUNT assertions below, and it is worth being exact about which,
    because the obvious candidate is the wrong one: `invalidate_generation()` resets the counters
    as well as the generation, so under the old per-query shape `generation_builds` still reads 1 --
    measured, while proving falsification 8 -- and it was `pair_builds` that fired. So
    `pair_builds`/`window_builds` are what say one generation served the whole list;
    `generation_builds == 1` says something else worth saying, that nothing REBUILT mid-seed behind
    the test's back (a stamp mismatch, a flaking provider), which would silently empty the memos
    again.

    `index_builder` (Finding 2) swaps the engine's own fallback build for an index shaped and
    ordered the way `main.refresh_call_predictions_cache` builds one, injected through the provider.
    """
    import random

    compared = 0
    for seed in seeds:
        rng = random.Random(seed)
        scope = _random_snapshot(rng)
        stamp = _at(6) + timedelta(seconds=seed)
        queries = _identity_queries(rng, scope)
        _generation(scope, refreshed_at=stamp)
        if index_builder is not None:
            # Built over the dict the engine will actually serve, so the postings carry the same
            # situation KEYS and the same `CallPred` objects the reference scan reads.
            _generation_state["index"] = index_builder(_generation_state["snapshot"])

        def run_seed(queries=queries, stamp=stamp, seed=seed):
            n = 0
            for query in queries:
                situation = query.get("situation")
                window_start, window_end = query["window"]
                got = impact.for_trip(
                    from_stop=A, to_stop=query["to_stop"],
                    window_start=window_start, window_end=window_end,
                    select=query["select"], at=query["at"],
                    catchable_after=query["catchable_after"],
                    situation=situation, with_alternative=False,
                )
                full = _generation_state["snapshot"]
                if situation is not None:
                    reference_scope = {situation: full[situation]} if situation in full else {}
                else:
                    reference_scope = full
                cells = impact._trip_cells(
                    reference_scope, A, query["to_stop"], window_start, window_end,
                    select=query["select"], at=query["at"],
                    catchable_after=query["catchable_after"],
                )
                want = impact._impact_response(
                    cells, from_stop=A, to_stop=query["to_stop"], refreshed_at=stamp, now=got.as_of
                )
                assert got.model_dump() == want.model_dump(), (
                    f"seed {seed}, query {query}: the prune changed the answer"
                )
                n += 1
            return n, impact.index_stats()

        n, stats = _with_generation(run_seed)
        compared += n
        # The memos are KEYED, not merely built: one entry per distinct `to_stop` and one per
        # distinct (pair, window). Asserting the counts is what makes a key-shaped defect -- a memo
        # that answers the right question with another question's entry -- reachable at all; the
        # identity assertion above is what makes it FAIL.
        pairs = {(A, q["to_stop"]) for q in queries}
        windows = {(A, q["to_stop"], *q["window"]) for q in queries}
        assert stats["generation_builds"] == 1, (
            f"seed {seed}: {stats} — one generation must serve the whole query list, or the memos "
            "never hold a second entry and this oracle cannot see a key-shaped defect"
        )
        assert stats["pair_builds"] == len(pairs), (
            f"seed {seed}: {stats} — want one pair build per distinct to_stop ({len(pairs)})"
        )
        assert stats["window_builds"] == len(windows), (
            f"seed {seed}: {stats} — want one window build per distinct (pair, window) "
            f"({len(windows)})"
        )
    return compared


def test_the_pruned_scan_answers_exactly_what_the_full_scan_answers():
    """Randomized differential: `for_trip` (pruned) against `_trip_cells` over the whole snapshot.

    Compared as `model_dump()`, so a difference anywhere — a missing situation, a different worst
    train, two situations in the other order after a severity tie, a delay figure off a different
    call — fails. `now` is taken off the response under test and handed to the reference, because
    `for_trip` samples it once for both `as_of` and the observation cut: reproducing that instant
    is what makes the comparison total rather than "equal apart from timestamps".

    **This is an ORACLE, not a regression test, so it is green by design and its worth is entirely
    in what it can be made to fail on.** A differential test that cannot fail is the most
    comfortable kind of vacuous check: it passes before the change, after it, and after a change
    that breaks everything. So the falsifications it was proved against are recorded here, each one
    applied to the prune and each one observed RED before being reverted:

    1. Building the pruned scope in name order rather than the snapshot's own iteration order —
       caught, because a severity tie resolves on scope order.
    2. Requiring alighting permission on the BOARDING call.
    3. Reading the situation off `CallPred.situation_number` instead of carrying the snapshot key
       in the posting list (the two agree in production and three suites need them not to).
    4. An exclusive window end (`<` for `<=`) in the prune.
    5. Applying the catchable cut after the per-line rollup instead of in candidate selection.
    6. Requiring an arrival time on the boarding candidate — a plausible narrowing, since an origin
       station legitimately has none. Reported as
       `seed 0, query {'to_stop': ..., 'select': 'worst'}: the prune changed the answer`.

    Those six are all PREDICATE-shaped: each changes what the prune computes. Two more were added
    on 2026-09-12, and they are KEY-shaped — the memo computes the right answer and then files or
    fetches it under the wrong key — which this oracle could not see at all until one generation was
    made to serve a seed's whole query list (see `_assert_prune_identity`):

    7. Dropping `to_ref` from the pair memo's key, so an A→B answer is served to the next A→C and
       A→(no destination) question. Reported as
       `seed 0, query {'to_stop': None, ...}: the prune changed the answer`.
    8. Dropping the window from the window memo's key, so the first window asked about a pair
       answers every later one. Reported as
       `seed 0, query {'to_stop': ..., 'window': (07:00, 09:00)}: the prune changed the answer`.

    And TWO that are deliberately GREEN, both for one reason — a prune may only ever be too
    generous: dropping the operator from the alighting group key, and (7)/(8) asked in the other
    order, where the entry wrongly served is a SUPERSET of the right one and `_trip_cells` filters
    the extras out itself. Which is what shows the superset property, rather than the exact
    predicate, is the thing doing the work — and why `_identity_queries` is ordered as it is.
    """
    compared = _assert_prune_identity(_IDENTITY_SEEDS)
    assert compared >= 200, f"only {compared} comparisons — the generator stopped covering"


def _ingest_shaped_index(snapshot):
    """`{stop_place: [(situationNumber, CallPred)]}`, built the way `main`'s ingest loop builds it.

    `main.refresh_call_predictions_cache` appends in BigQuery ROW order — one stream of calls, with
    situations interleaved — while `impact._build_stop_ref_index` walks `snapshot.items()` and so
    emits each situation's calls contiguously. Same keys, same postings, different posting ORDER.
    This round-robins over the situations to reproduce the interleaving on purpose: copying the
    snapshot's order would just build the engine's own fallback a second time and prove nothing.

    Also note main keys the posting on `r.situationNumber`, the same column `by_situation` is keyed
    on — so the snapshot KEY is what belongs here, not `CallPred.situation_number`, which this
    suite deliberately draws apart from it.
    """
    index: dict[str, list] = {}
    remaining = {sit: list(calls) for sit, calls in snapshot.items()}
    while remaining:
        for sit in list(remaining):
            call = remaining[sit].pop(0)
            index.setdefault(call.stop_place, []).append((sit, call))
            if not remaining[sit]:
                del remaining[sit]
    return index


def test_the_pruned_scan_answers_the_same_through_the_pollers_own_index():
    """The two ends of U2 meeting: the oracle, over the index `main` actually injects.

    `main`'s builder is separately pinned for coverage, key set, per-stop ordering and stamp
    agreement (`test_poller_refresh.test_the_ingest_loop_indexes_every_call_by_the_stop_it_calls_at`)
    and `impact`'s fallback is what every other test here exercises — but nothing ran the matcher's
    identity claim through the injected one, so a divergence in SHAPE or ORDER between the two
    builders was uncaught by construction.

    The divergence that exists is order: main appends in BigQuery row order (situations interleaved)
    where the fallback walks situation by situation. It cannot reach the output, and the reason is
    worth writing down rather than re-deriving — `_pruned_scope` hands `_trip_cells` the SNAPSHOT's
    own `by_situation[sit]` lists, never the memo's candidate tuples, and posting order feeds only
    `situations()`' order-independent `any(...)`. This makes that a checked property.

    Two things make the test non-vacuous and both are asserted rather than reasoned about. The spy,
    because a provider that silently failed the stamp comparison would fall back to
    `_build_stop_ref_index` and the oracle would pass on the very build it was meant to stop
    exercising. And the divergence itself, because a seed drawing a single situation makes the
    round-robin index byte-identical to the fallback — if that held for every seed this would be the
    engine's own builder run twice under a second name.
    """
    import random

    diverged = 0
    for seed in _INJECTED_SEEDS:
        snapshot = _random_snapshot(random.Random(seed))  # deterministic; the same one below
        if _ingest_shaped_index(snapshot) != impact._build_stop_ref_index(snapshot):
            diverged += 1
    assert diverged, (
        "the two builders produced identical postings on every seed, so this runs the engine's own "
        "fallback twice rather than the divergence it claims to cover"
    )

    original = impact._build_stop_ref_index
    fell_back: list = []

    def spy(snapshot):
        fell_back.append(snapshot)
        return original(snapshot)

    impact._build_stop_ref_index = spy
    try:
        compared = _assert_prune_identity(_INJECTED_SEEDS, index_builder=_ingest_shaped_index)
    finally:
        impact._build_stop_ref_index = original
    assert not fell_back, (
        f"the engine rebuilt the index {len(fell_back)} time(s) instead of taking the injected one"
    )
    assert compared >= 50, f"only {compared} comparisons through the injected index"


def test_the_catchable_cut_is_per_call_and_never_in_the_pair_memo():
    """The review finding this design turns on, and the ordering trap it must not walk into.

    `catchable_after` comes from the CALLER's clock -- the poller's cycle instant, the journey
    list's, the pull read's -- so a memo keyed to include it would be shared by nobody, and one that
    ignored it would show a departed train as catchable for up to a cycle. So it is in neither: the
    memo holds the window-eligible situations and the cut stays exactly where `_trip_cells` applies
    it, in candidate selection, BEFORE the per-line rollup.

    That ordering is the whole point. Two trains on ONE line here: the severe one has gone, the
    milder one has not. Applied after the rollup, the departed train would win its line and then be
    filtered out, taking the line -- and the alert about the catchable train -- with it. Applied
    where it is, the catchable train wins the line and is reported.
    """
    scope = {
        "S1": [
            *_train("gone", 7, 10, p_impact_dep=0.99),
            *_train("later", 8, 30, p_impact_dep=0.60),
        ]
    }
    _generation(scope, refreshed_at=_at(6))

    def run():
        unfiltered = impact.for_trip(
            from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
            select="worst", with_alternative=False,
        )
        filtered = impact.for_trip(
            from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
            select="worst", catchable_after=_at(8), with_alternative=False,
        )
        return unfiltered, filtered, impact.index_stats()

    unfiltered, filtered, stats = _with_generation(run)
    assert unfiltered.situations[0].jkey == "gone", "control: the severe train wins while catchable"
    assert unfiltered.situations[0].departure_prediction.impact_probability == 0.99
    assert filtered.situations, "the departed train suppressed its whole line"
    assert filtered.situations[0].jkey == "later", "the cut ran after the rollup"
    assert filtered.situations[0].departure_prediction.impact_probability == 0.60
    # Both calls asked the same (pair, window) question, so both memos were built exactly once --
    # which is only possible because neither of them knows about `catchable_after`.
    assert stats["pair_builds"] == 1, stats
    assert stats["window_builds"] == 1, stats


def test_a_train_nobody_may_alight_from_is_excluded_by_the_pruned_scan_too():
    """The F4 rule at `for_trip`'s level, not just `_trip_cells`'.

    Vy sells the local stretch of every long-distance run as boarding-only, and until 2026-09-01 a
    cancellation on one of them was pushed to eight devices that could not have been aboard. The
    prune has its own copy of the eligibility question (it is what bounds the scan), so the rule has
    to be pinned on this path as well as on the matcher's.
    """
    board = FakeCall("f4", A, 1, _at(8), situation_number="S1")
    for at_destination, expected in ((False, 0), (True, 1)):
        scope = {"S1": [board, FakeCall(
            "f4", B, 5, None, aimed_arr=_at(8, 40), situation_number="S1",
            alighting_allowed=at_destination,
        )]}
        _generation(scope, refreshed_at=_at(6))
        resp = _with_generation(lambda: impact.for_trip(
            from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
            select="worst", with_alternative=False,
        ))
        assert len(resp.situations) == expected, (
            f"alighting_allowed={at_destination} gave {len(resp.situations)} situations"
        )


def test_nearest_still_measures_from_at_through_the_pruned_scan():
    """`select="nearest"` keeps the boarding call closest to `at`, severity notwithstanding.

    The prune keeps EVERY boarding candidate of a group rather than a pre-selected one, precisely so
    the two selection strategies still choose from the same pool; a prune that kept "the worst" here
    would answer /impact's question with /varsel's.
    """
    scope = {"S1": [
        *_train("close", 8, 20, p_impact_dep=0.2, line="VYG:Line:R10"),
        *_train("far", 9, 10, p_impact_dep=0.99, line="VYG:Line:R10"),
    ]}
    _generation(scope, refreshed_at=_at(6))
    resp = _with_generation(lambda: impact.for_trip(
        from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(10),
        select="nearest", at=_at(8), with_alternative=False,
    ))
    assert len(resp.situations) == 1
    assert resp.situations[0].departure_prediction.scheduled_time == _at(8, 20)


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} impact checks passed.")


if __name__ == "__main__":
    _run()
