"""Unit checks for the /impact trip-cell matcher and response shaping.

Runs standalone (`uv run --with fastapi --with pydantic python test_impact.py`) or under pytest.
Hermetic: the BigQuery client is stubbed out before main is imported, so nothing here touches
BigQuery or the network.

Covers the window matcher (edges, direction, per-line selection strategy, operator/jkey carry),
the severity ordering fix (impact probability first, not cancellation probability),
latest_observation resolution, and the /impact response contract togpuls depends on.
"""

from datetime import UTC, datetime, timedelta

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import main
from test_varsel import FakeCall

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
    return main._trip_cells(
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
        main._trip_cells(_scope(S1=_train("j1", 8)), A, B, _at(7), _at(9), select="nearest")
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
    low_cancel_high_impact = main._departure_prediction(
        FakeCall(jkey="a", stop_place=A, stop_order=1, aimed_dep=_at(8), p_cancel=0.05,
                 p_impact_dep=0.95)
    )
    high_cancel_low_impact = main._departure_prediction(
        FakeCall(jkey="b", stop_place=A, stop_order=1, aimed_dep=_at(8), p_cancel=0.40,
                 p_impact_dep=0.42)
    )
    worst = main._worst([high_cancel_low_impact, low_cancel_high_impact])
    assert worst is low_cancel_high_impact


def test_worst_ignores_none_entries():
    pred = main._departure_prediction(
        FakeCall(jkey="a", stop_place=A, stop_order=1, aimed_dep=_at(8))
    )
    assert main._worst([None, pred, None]) is pred
    assert main._worst([None, None]) is None


def test_multi_line_situation_rolls_up_to_the_highest_impact_line():
    scope = _scope(
        S1=_train("r10", 8, line="VYG:Line:R10", p_cancel=0.9, p_impact_dep=0.91)
        + _train("r11", 8, 5, line="VYG:Line:R11", p_cancel=0.1, p_impact_dep=0.99)
    )
    cells = _cells(scope)
    sit = main._worst([c.departure for c in cells])
    assert sit.impact_probability == 0.99


# ---- latest_observation -----------------------------------------------------------------------
def test_latest_observation_absent_when_nothing_observed():
    cells = _cells(_scope(S1=_train("j1", 8)))  # FakeCall defaults to an unobserved future call
    assert main._latest_observation(cells) is None


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
    obs = main._latest_observation([main._TripCell(
        situation_number="S1", summary=None, description=None, line="VYG:Line:R10",
        operator="VYG", jkey="j1", departure=main._departure_prediction(calls[0]),
        arrival=None, calls=calls,
    )])
    assert (obs.at_stop, obs.at_stop_name, obs.delayed_min) == ("NSR:StopPlace:X", "Asker", 18)
    assert obs.cancelled is False
    assert obs.observed_at == dep + timedelta(minutes=20)


def test_latest_observation_reports_cancellation_without_a_delay():
    """An ET-declared cancellation is an observation even when no delay was recorded."""
    call = FakeCall(jkey="j1", stop_place=A, stop_order=1, aimed_dep=_at(8), stop_name="Oslo S",
                    call_source="recorded", et_declared_cancelled=True, et_dep_delay_min=None)
    obs = main._latest_observation([main._TripCell(
        situation_number="S1", summary=None, description=None, line="VYG:Line:R10",
        operator="VYG", jkey="j1", departure=main._departure_prediction(call),
        arrival=None, calls=[call],
    )])
    assert obs.cancelled is True
    assert obs.delayed_min is None


def test_latest_observation_ignores_estimated_calls():
    """`estimated` means the vehicle has not served the stop yet -- a delay there is not observed."""
    call = FakeCall(jkey="j1", stop_place=A, stop_order=1, aimed_dep=_at(8),
                    call_source="estimated", et_dep_delay_min=9)
    obs = main._latest_observation([main._TripCell(
        situation_number="S1", summary=None, description=None, line="VYG:Line:R10",
        operator="VYG", jkey="j1", departure=main._departure_prediction(call),
        arrival=None, calls=[call],
    )])
    assert obs is None


# ---- response contract ------------------------------------------------------------------------
def test_impact_response_shape_matches_the_togpuls_contract():
    """togpuls/clients/disruptions.py reads exactly two things: a `situations` list, and a
    non-empty `situation_number` on each entry. Everything else it ignores."""
    scope = _scope(S1=_train("j1", 8))
    cells = _cells(scope)
    resp = main._impact_response(
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
    main._call_pred_by_situation = _scope(S1=_train("j1", 8))
    try:
        resp = main._impact_for_trip(
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
    resp = main._impact_response(
        _cells(scope), from_stop=A, to_stop=B, refreshed_at=None, alternative_journey=None
    )
    assert resp.situations[0].description == "Buss for tog"
    assert resp.lines[0].operator == "RUT"
    assert resp.alternative_journey is None


def test_real_http_route_satisfies_the_togpuls_contract():
    """Drive the actual `@app.get("/impact")` route, not just the helpers.

    Review 2026-07-29 pointed out that every other check here calls internals, so query-param
    parsing, the response envelope and HTTP-level serialization of the new fields were untested — the
    contract this file claims to pin was never exercised end to end. This sends exactly what
    togpuls/clients/disruptions.py sends and asserts on exactly what it reads.
    """
    from fastapi.testclient import TestClient

    main._call_pred_by_situation = _scope(S1=_train("j1", 8, description="Signalfeil"))
    main._call_pred_refreshed_at = _at(8)
    original = main.alternatives.find_faster
    main.alternatives.find_faster = lambda *a, **k: None  # no network from the suite
    try:
        r = TestClient(main.app).get(
            "/impact",
            params={"from_stop": A, "to_stop": B, "at": _at(8).isoformat()},
        )
    finally:
        main.alternatives.find_faster = original

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

    main._call_pred_by_situation = {}
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
