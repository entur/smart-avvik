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
from datetime import UTC, datetime, timedelta

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

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
    original = impact.alternatives.find_faster
    impact.alternatives.find_faster = lambda *a, **k: asked.append(1) or None
    try:
        for p_impact, gate, expected in ((0.20, 0.50, 0), (0.93, 0.50, 1), (0.20, 0.0, 1)):
            asked.clear()
            _snapshot.clear()
            _snapshot.update(_scope(S1=_train("j1", 8, p_impact_dep=p_impact)))
            impact.for_trip(
                from_stop=A, to_stop=B, window_start=_at(7), window_end=_at(9),
                select="worst", with_alternative=True, min_impact=gate,
            )
            assert len(asked) == expected, (
                f"p_impact={p_impact} against gate={gate} should have asked {expected} time(s)"
            )
    finally:
        impact.alternatives.find_faster = original


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


# ---- response contract ------------------------------------------------------------------------
def test_impact_response_shape_matches_the_togpuls_contract():
    """togpuls/clients/disruptions.py reads exactly two things: a `situations` list, and a
    non-empty `situation_number` on each entry. Everything else it ignores."""
    scope = _scope(S1=_train("j1", 8))
    cells = _cells(scope)
    resp = impact._impact_response(
        cells, from_stop=A, to_stop=B, refreshed_at=None, alternative_journey=None
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
    original = alternatives.find_faster
    alternatives.find_faster = lambda *a, **k: calls.append(1)
    _snapshot.clear(); _snapshot.update(_scope(S1=_train("j1", 8)))
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
        alternatives.find_faster = original
    assert calls == [], "the Journey Planner must not be consulted without a to_stop"
    assert resp.alternative_journey is None


def test_impact_response_carries_the_new_fields():
    scope = _scope(S1=_train("j1", 8, operator="RUT", description="Buss for tog"))
    resp = impact._impact_response(
        _cells(scope), from_stop=A, to_stop=B, refreshed_at=None, alternative_journey=None
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
        _cells(scope), from_stop=A, to_stop=B, refreshed_at=None, alternative_journey=None
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
        cells, from_stop=A, to_stop=B, refreshed_at=None, alternative_journey=None
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

    _snapshot.clear(); _snapshot.update(_scope(S1=_train("j1", 8, description="Signalfeil")))
    main._call_pred_refreshed_at = _at(8)
    original = impact.alternatives.find_faster
    impact.alternatives.find_faster = lambda *a, **k: None  # no network from the suite
    try:
        r = TestClient(main.app).get(
            "/impact",
            params={"from_stop": A, "to_stop": B, "at": _at(8).isoformat()},
        )
    finally:
        impact.alternatives.find_faster = original

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


def test_real_http_route_handles_a_trip_with_nothing_affecting_it():
    from fastapi.testclient import TestClient

    _snapshot.clear(); _snapshot.update({})
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
