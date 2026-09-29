"""Hermetic checks for `journey_status`: the «I dag» strip's per-train grading (plan 2026-09-22-001).

Runs standalone (`uv run python test_journey_status.py`) or under pytest. No BigQuery, no network:
the spine and the actuals are built from literal rows, and the prediction cache is a dict the
engine reads through `impact.configure(call_lookup=...)`, exactly as `main` wires it.
"""

from __future__ import annotations

import ast
import random
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import impact
import journey_status as js

OSLO = ZoneInfo("Europe/Oslo")
FROM = "NSR:StopPlace:337"  # Oslo S
MID = "NSR:StopPlace:444"  # Sandvika
TO = "NSR:StopPlace:418"  # Asker, rail side -- where every rail call sits
TO_CANONICAL = "NSR:StopPlace:3247"  # Asker, the busier bus side the picker collapses onto

# 2026-09-22 is a Tuesday (weekday 1).
DEFINITION = {"from_stop": FROM, "to_stop": TO, "begin": "07:00", "end": "09:00", "days": [0, 1, 2, 3, 4]}

_index: dict[tuple[str, str], list[impact.CallPred]] = {}
impact.configure(
    snapshot_provider=lambda: ({}, None),
    call_lookup=lambda operator, jkey: _index.get((operator, jkey)),
)


def _oslo(h: int, m: int = 0, day: int = 22) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=OSLO).astimezone(UTC)


def _row(jkey, order, stop, aimed_dep, aimed_arr, *, source="estimated", **kw) -> js.PlannedCall:
    fields = {
        "operator": "VYG", "jkey": jkey, "line": "VYG:Line:L1", "stop_place": stop,
        "stop_order": order, "aimed_dep": aimed_dep, "aimed_arr": aimed_arr, "dep_delay_min": None,
        "arr_delay_min": None, "call_cancelled": False, "call_source": source,
    }
    fields.update(kw)
    return js.PlannedCall(**fields)


def _train(jkey, dep, *, board=None, mid=None, alight=None, terminus=False, line="VYG:Line:L1"):
    """Three planned calls: FROM (order 3), MID (5, +10 min), TO (8, +20 min). Keyword dicts
    override fields per call."""
    rows = [
        _row(jkey, 3, FROM, dep, dep - timedelta(minutes=1), line=line, **(board or {})),
        _row(jkey, 5, MID, dep + timedelta(minutes=10), dep + timedelta(minutes=9), line=line, **(mid or {})),
        _row(
            jkey, 8, TO, None if terminus else dep + timedelta(minutes=21), dep + timedelta(minutes=20),
            line=line, **(alight or {}),
        ),
    ]
    return rows


def _pred(jkey, order, stop, aimed_dep, aimed_arr, *, sit="S1", **kw) -> impact.CallPred:
    fields = {
        "situation_number": sit, "summary": None, "description": None, "jkey": jkey, "operator": "VYG",
        "line": "VYG:Line:L1", "stop_place": stop, "stop_name": None, "stop_order": order,
        "aimed_dep": aimed_dep, "aimed_arr": aimed_arr, "et_declared_cancelled": False,
        "et_dep_delay_min": None, "call_source": "estimated", "p_cancel": 0.01,
        "dep_delay_typ_min": 1, "dep_delay_p90_min": 4, "arr_delay_typ_min": 1, "arr_delay_p90_min": 4,
        "p_arr_ge10": 0.02, "p_arr_ge20": 0.01, "arr_delay_p50_stack_min": None,
        "arr_delay_p90_stack_min": None, "p_impact": 0.03, "p_dep_ge10": 0.02, "p_dep_ge20": 0.01,
        "dep_delay_p50_stack_min": None, "dep_delay_p90_stack_min": None, "p_impact_dep": 0.03,
        "has_situation": True,
    }
    fields.update(kw)
    return impact.CallPred(**fields)


def _scored(jkey, dep, *, sit="S1", board=None, alight=None, **both):
    """Scored boarding and alighting calls of one train in one situation."""
    b = _pred(jkey, 3, FROM, dep, dep - timedelta(minutes=1), sit=sit, **{**both, **(board or {})})
    a = _pred(jkey, 8, TO, dep + timedelta(minutes=21), dep + timedelta(minutes=20), sit=sit,
              **{**both, **(alight or {})})
    return [b, a]


def _install(rows, preds=(), *, actuals=None, place_refs=None):
    _index.clear()
    for p in preds:
        _index.setdefault((p.operator, p.jkey), []).append(p)
    js.reset_state()
    js.configure(predictions_refreshed_at=lambda: _oslo(7, 40), place_refs=place_refs or (lambda r: (r,)))
    js.set_spine(js.CallTable(rows, _oslo(7, 5)))
    if actuals is not None:
        js.set_actuals(js.CallTable(actuals, _oslo(7, 35)))


def _strip(now, definition=DEFINITION):
    status = js.status_for(definition, now)
    assert status is not None
    return {js_time(t.departure_time): t for t in status.trains}


def js_time(t: datetime) -> str:
    return t.astimezone(OSLO).strftime("%H:%M")


def _morning():
    """The acceptance example's nine trains, spine and predictions."""
    rows, preds = [], []
    rows += _train("t0703", _oslo(7, 3), board={"source": "recorded", "dep_delay_min": 0},
                   alight={"source": "recorded", "dep_delay_min": 1, "arr_delay_min": 1})
    rows += _train("t0718", _oslo(7, 18), board={"call_cancelled": True}, mid={"call_cancelled": True},
                   alight={"call_cancelled": True})
    rows += _train("t0733", _oslo(7, 33), board={"source": "recorded", "dep_delay_min": 4},
                   alight={"source": "recorded", "dep_delay_min": 5, "arr_delay_min": 5})
    rows += _train("t0748", _oslo(7, 48))
    preds += _scored("t0748", _oslo(7, 48), p_impact_dep=0.71, arr_delay_p50_stack_min=6)
    rows += _train("t0803", _oslo(8, 3))
    preds += _scored("t0803", _oslo(8, 3))
    rows += _train("t0811", _oslo(8, 11), line="VYG:Line:R13")  # no figure at all
    rows += _train("t0818", _oslo(8, 18))
    preds += _scored("t0818", _oslo(8, 18), sit="KIX:NoSituation:t0818", has_situation=False,
                     arr_delay_p50_stack_min=6)
    for jkey, h, m in (("t0833", 8, 33), ("t0848", 8, 48)):
        rows += _train(jkey, _oslo(h, m))
        preds += _scored(jkey, _oslo(h, m), sit=f"KIX:NoSituation:{jkey}", has_situation=False)
    return rows, preds


def _shape(strip):
    return {k: (t.state, t.level, t.cancelled) for k, t in strip.items()}


# ---- The acceptance examples -------------------------------------------------------------------


def test_the_morning_at_0742_reads_as_the_design():
    rows, preds = _morning()
    _install(rows, preds)
    strip = _strip(_oslo(7, 42))
    assert list(strip) == ["07:03", "07:18", "07:33", "07:48", "08:03", "08:11", "08:18", "08:33", "08:48"]
    assert _shape(strip) == {
        "07:03": ("confirmed", "green", False),
        "07:18": ("confirmed", "red", True),
        "07:33": ("confirmed", "yellow", False),
        "07:48": ("predicted", "red", False),
        "08:03": ("predicted", "green", False),
        "08:11": ("unknown", "grey", False),
        "08:18": ("predicted", "yellow", False),
        "08:33": ("predicted", "green", False),
        "08:48": ("predicted", "green", False),
    }
    assert strip["07:48"].alert_worthy and strip["07:48"].impact_probability == 0.71
    assert strip["07:48"].arrival_time == _oslo(8, 8)
    # Aimed 07:48 plus the typical departure delay (the regression's 1 min, no stack figure).
    assert strip["07:48"].expected_departure_time == _oslo(7, 49)
    assert strip["07:33"].delay_min == 5
    assert strip["08:18"].delay_p50_min == 6
    # A fact carries no forecast figures, and neither does a train nothing can be said about.
    for hhmm in ("07:03", "07:33", "08:11"):
        assert (strip[hhmm].impact_probability, strip[hhmm].alert_worthy) == (None, False), hhmm
    assert strip["07:18"].expected_departure_time is None


def test_the_same_morning_at_0610_is_all_rings_and_grey_past_the_nosx_horizon():
    rows, preds = _morning()
    # Nothing has run: the recorded rows are what the morning WILL be, so pin them unrecorded here.
    rows = [replace(r, call_source="estimated", dep_delay_min=None, arr_delay_min=None) for r in rows]
    _install(rows, preds)
    strip = _strip(_oslo(6, 10))
    assert all(t.state != "confirmed" for t in strip.values())
    assert _shape(strip)["07:18"] == ("predicted", "red", True)  # cancelled, not yet due
    # 08:18 is 128 min out and no SX named it: grey, not green (KTD7).
    assert _shape(strip)["08:18"] == ("unknown", "grey", False)
    assert _shape(strip)["08:48"] == ("unknown", "grey", False)
    # An SX figure is served at any horizon.
    assert _shape(strip)["07:48"] == ("predicted", "red", False)


def test_a_day_not_in_days_answers_empty_with_a_reason():
    _install(_train("t", _oslo(7, 3, day=26)))
    status = js.status_for(DEFINITION, _oslo(7, 42, day=26))  # a Saturday
    assert status.trains == [] and status.reason == "not_a_travel_day" and status.window is None


def test_no_spine_is_none_never_an_empty_strip():
    js.reset_state()
    assert js.status_for(DEFINITION, _oslo(7, 42)) is None


def test_a_loaded_spine_with_no_predictions_is_every_future_train_unknown():
    _install(_train("a", _oslo(7, 48)) + _train("b", _oslo(8, 3)))
    status = js.status_for(DEFINITION, _oslo(7, 42))
    assert [t.state for t in status.trains] == ["unknown", "unknown"]
    assert status.as_of.plan_refreshed_at == _oslo(7, 5)
    assert status.as_of.actuals_refreshed_at is None


def test_a_train_carries_its_lines_mode():
    """KIX-23: `rail` for a Vy train, from `line_modes` -- the one rule journey-alerts uses too."""
    _install(_train("a", _oslo(7, 48)))
    assert [t.mode for t in js.status_for(DEFINITION, _oslo(7, 42)).trains] == ["rail"]


# ---- Precedence and levels ---------------------------------------------------------------------


def test_a_prediction_for_a_train_the_spine_lacks_is_dropped():
    _install(_train("a", _oslo(7, 48)), _scored("ghost", _oslo(7, 50), p_impact_dep=0.99))
    assert list(_strip(_oslo(7, 42))) == ["07:48"]


def test_a_declared_cancellation_turns_from_ring_to_fill_at_its_aimed_departure():
    _install(_train("a", _oslo(7, 48)), _scored("a", _oslo(7, 48), et_declared_cancelled=True))
    before = _strip(_oslo(7, 47))["07:48"]
    after = _strip(_oslo(7, 48))["07:48"]
    assert (before.state, before.level, before.cancelled) == ("predicted", "red", True)
    assert (after.state, after.level, after.cancelled) == ("confirmed", "red", True)
    assert before.impact_probability == 1.0


def test_the_nosx_horizon_greys_a_distant_nosx_figure_and_only_that():
    dep = _oslo(8, 30)
    for sx, minutes_out, expected in ((False, 150, "unknown"), (False, 90, "predicted"), (True, 150, "predicted")):
        _install(_train("a", dep), _scored("a", dep, has_situation=sx, sit="S1" if sx else "KIX:NoSituation:a"))
        assert _strip(dep - timedelta(minutes=minutes_out))["08:30"].state == expected, (sx, minutes_out)


def test_red_for_a_forecast_is_the_alert_gate_with_the_nosx_cut():
    dep = _oslo(7, 48)
    _install(_train("a", dep), _scored("a", dep, p_impact_dep=0.70, has_situation=False, sit="KIX:NoSituation:a"))
    nosx = _strip(_oslo(7, 42))["07:48"]
    assert (nosx.level, nosx.alert_worthy) == ("green", False)  # 0.70 < 0.85
    _install(_train("a", dep), _scored("a", dep, p_impact_dep=0.70))
    sx = _strip(_oslo(7, 42))["07:48"]
    assert (sx.level, sx.alert_worthy) == ("red", True)  # 0.70 >= 0.65


def test_a_typical_delay_of_ten_under_the_gate_is_yellow_not_red():
    dep = _oslo(7, 48)
    _install(_train("a", dep), _scored("a", dep, p_impact_dep=0.40, arr_delay_p50_stack_min=11))
    assert _strip(_oslo(7, 42))["07:48"].level == "yellow"


def test_the_typical_arrival_delay_prefers_the_stack_then_the_regression():
    dep = _oslo(7, 48)
    _install(_train("a", dep), _scored("a", dep, arr_delay_typ_min=5, arr_delay_p50_stack_min=None))
    assert _strip(_oslo(7, 42))["07:48"].delay_p50_min == 5
    _install(_train("a", dep), _scored("a", dep, arr_delay_typ_min=5, arr_delay_p50_stack_min=1))
    assert _strip(_oslo(7, 42))["07:48"].level == "green"


def test_a_terminus_alighting_call_is_graded_by_the_previous_recorded_call():
    dep = _oslo(7, 3)
    rows = _train("a", dep, terminus=True, board={"source": "recorded", "dep_delay_min": 2},
                  mid={"source": "recorded", "dep_delay_min": 7}, alight={"source": "recorded"})
    _install(rows)
    t = _strip(_oslo(7, 42))["07:03"]
    assert (t.state, t.level, t.delay_min) == ("confirmed", "yellow", 7)
    assert t.observed_at == dep + timedelta(minutes=9)  # the MID call's aimed arrival


def test_a_recorded_alighting_call_without_any_delay_figure_is_confirmed_grey():
    rows = _train("a", _oslo(7, 3), terminus=True, alight={"source": "recorded"})
    _install(rows)
    t = _strip(_oslo(7, 42))["07:03"]
    assert (t.state, t.level) == ("confirmed", "grey")


def test_the_arrival_delay_wins_over_the_departure_delay():
    rows = _train("a", _oslo(7, 3), alight={"source": "recorded", "dep_delay_min": 3, "arr_delay_min": 12})
    _install(rows)
    t = _strip(_oslo(7, 42))["07:03"]
    assert (t.level, t.delay_min) == ("red", 12)


def test_before_the_arrival_column_the_departure_delay_at_the_alighting_call_grades():
    rows = _train("a", _oslo(7, 3), alight={"source": "recorded", "dep_delay_min": 6})
    _install(rows)
    assert _strip(_oslo(7, 42))["07:03"].level == "yellow"


def test_under_five_minutes_late_is_green_five_is_yellow_ten_is_red():
    for delay, level in ((4, "green"), (5, "yellow"), (9, "yellow"), (10, "red")):
        _install(_train("a", _oslo(7, 3), alight={"source": "recorded", "dep_delay_min": delay}))
        assert _strip(_oslo(7, 42))["07:03"].level == level, delay


def test_an_alighting_cancellation_cancels_the_trip():
    dep = _oslo(7, 48)
    _install(_train("a", dep, alight={"call_cancelled": True}), _scored("a", dep))
    t = _strip(_oslo(7, 42))["07:48"]
    assert (t.cancelled, t.level, t.alert_worthy, t.impact_probability) == (True, "red", True, 1.0)
    # A cancellation only the spine knows about has no prediction, and still reads 1.0.
    _install(_train("a", dep, board={"call_cancelled": True}))
    assert _strip(_oslo(7, 42))["07:48"].impact_probability == 1.0
    # The same through the prediction cache: ET's flag on the alighting call alone.
    _install(_train("a", dep), _scored("a", dep, alight={"et_declared_cancelled": True}))
    assert _strip(_oslo(7, 42))["07:48"].cancelled


def test_the_actuals_index_overrides_the_spine_and_owns_its_stamp():
    dep = _oslo(7, 3)
    spine = _train("a", dep)  # the spine is an hour old and still says estimated
    actuals = [
        _row("a", 3, FROM, dep, None, source="recorded", dep_delay_min=0),
        _row("a", 8, TO, dep + timedelta(minutes=21), dep + timedelta(minutes=20), source="recorded",
             dep_delay_min=2, arr_delay_min=2),
    ]
    _install(spine, actuals=actuals)
    status = js.status_for(DEFINITION, _oslo(7, 42))
    t = status.trains[0]
    assert (t.state, t.level, t.delay_min) == ("confirmed", "green", 2)
    assert status.as_of.actuals_refreshed_at == _oslo(7, 35)
    assert status.as_of.plan_refreshed_at == _oslo(7, 5)


def test_an_actuals_index_that_lacks_the_terminus_does_not_hide_the_spines_recorded_one():
    """`rail_calls_today` keeps calls with an aimed departure only, so it never holds a terminus
    arrival. The spine's recorded terminus call must still confirm the train."""
    dep = _oslo(7, 3)
    spine = _train("a", dep, terminus=True, board={"source": "recorded", "dep_delay_min": 1},
                   mid={"source": "recorded", "dep_delay_min": 4}, alight={"source": "recorded"})
    actuals = [  # fresher, and graded later on the line -- but no terminus row
        _row("a", 3, FROM, dep, None, source="recorded", dep_delay_min=1),
        _row("a", 5, MID, dep + timedelta(minutes=10), None, source="recorded", dep_delay_min=11),
    ]
    _install(spine, actuals=actuals)
    t = _strip(_oslo(7, 42))["07:03"]
    assert (t.state, t.level, t.delay_min) == ("confirmed", "red", 11), "the actuals' MID figure wins"

def test_the_worst_situation_wins_and_ties_break_on_the_situation_number():
    dep = _oslo(7, 48)
    preds = _scored("a", dep, sit="S1", p_impact_dep=0.20) + _scored("a", dep, sit="S2", p_impact_dep=0.90)
    _install(_train("a", dep), preds)
    assert _strip(_oslo(7, 42))["07:48"].impact_probability == 0.90
    tied = _scored("a", dep, sit="S9", et_declared_cancelled=True) + _scored("a", dep, sit="S3", et_declared_cancelled=True)
    assert js._worst_prediction(tied, 3).situation_number == "S9"
    assert js._worst_prediction(list(reversed(tied)), 3).situation_number == "S9"



def test_a_situation_that_scores_only_the_boarding_call_is_never_red():
    """The push matcher pairs a boarding and an alighting call inside ONE situation. A situation
    whose onset falls mid-trip scores the boarding call only (here the alighting call sits in the
    no-SX branch), so no push fires; the strip may grade the train but not red it by the gate."""
    dep = _oslo(7, 48)
    board, _ = _scored("a", dep, sit="S1", p_impact_dep=0.95, dep_delay_p50_stack_min=4)
    _, alight = _scored("a", dep, sit="KIX:NoSituation:a", has_situation=False, arr_delay_p50_stack_min=5)
    _install(_train("a", dep), [board, alight])
    t = _strip(_oslo(7, 42))["07:48"]
    assert (t.state, t.level, t.alert_worthy, t.impact_probability) == ("predicted", "yellow", False, None)
    assert t.delay_p50_min == 5, "the arrival side's figure, as a paired train's would be"


def test_a_train_under_way_is_graded_off_its_alighting_call_once_the_boarding_one_ages_out():
    """The cache keeps calls from 60 min back, so a long trip's boarding call leaves it while the
    alighting call is still scored. That train is not grey."""
    dep = _oslo(7, 3)
    _, alight = _scored("a", dep, arr_delay_p50_stack_min=6)
    _install(_train("a", dep), [alight])
    t = _strip(_oslo(8, 10))["07:03"]
    assert (t.state, t.level, t.delay_p50_min, t.expected_departure_time) == ("predicted", "yellow", 6, None)


def test_only_a_boarding_figure_grades_off_the_departure_delay():
    dep = _oslo(7, 48)
    board, _ = _scored("a", dep, dep_delay_p50_stack_min=None, dep_delay_typ_min=6)
    _install(_train("a", dep), [board])
    t = _strip(_oslo(7, 42))["07:48"]
    assert (t.level, t.delay_p50_min, t.alert_worthy) == ("yellow", 6, False)


def test_a_cancellation_only_the_actuals_know_about_cancels_the_trip():
    for end in ("board", "alight"):
        dep = _oslo(7, 3)
        order, stop = (3, FROM) if end == "board" else (8, TO)
        actuals = [_row("a", order, stop, dep + timedelta(minutes=0 if end == "board" else 21), None,
                        source="recorded", call_cancelled=True)]
        _install(_train("a", dep), _scored("a", dep), actuals=actuals)
        t = _strip(_oslo(7, 42))["07:03"]
        assert (t.cancelled, t.level, t.impact_probability) == (True, "red", 1.0), end


def test_a_recorded_call_outranks_a_stale_cancellation_so_a_reinstated_train_reads_as_run():
    dep = _oslo(7, 3)
    spine = _train("a", dep, board={"call_cancelled": True}, alight={"call_cancelled": True})
    actuals = [
        _row("a", 3, FROM, dep, None, source="recorded", dep_delay_min=2),
        _row("a", 8, TO, dep + timedelta(minutes=21), None, source="recorded", dep_delay_min=2),
    ]
    _install(spine, _scored("a", dep, et_declared_cancelled=True), actuals=actuals)
    t = _strip(_oslo(7, 42))["07:03"]
    assert (t.cancelled, t.state, t.level, t.delay_min) == (False, "confirmed", "green", 2)


def test_a_train_calling_the_origin_twice_boards_at_its_first_call_whatever_the_row_order():
    dep = _oslo(7, 10)
    loop = [
        _row("a", 1, FROM, dep, None),
        _row("a", 4, MID, dep + timedelta(minutes=10), dep + timedelta(minutes=9)),
        _row("a", 6, FROM, dep + timedelta(minutes=20), dep + timedelta(minutes=19)),
        _row("a", 9, TO, dep + timedelta(minutes=41), dep + timedelta(minutes=40)),
    ]
    for rows in (loop, list(reversed(loop))):
        _install(rows)
        (t,) = js.status_for(DEFINITION, _oslo(7, 0)).trains
        assert t.departure_time == dep

# ---- Enumeration -------------------------------------------------------------------------------


def test_one_sx_message_naming_three_trains_puts_all_three_on_the_strip():
    """The case the per-train lookup exists for: `_trip_cells` keeps one train per line per
    situation, so enumerating through the matcher would show one circle where there are three."""
    deps = (_oslo(7, 48), _oslo(8, 3), _oslo(8, 18))
    rows, preds = [], []
    for i, dep in enumerate(deps):
        rows += _train(f"t{i}", dep)
        preds += _scored(f"t{i}", dep, sit="NSB:SituationNumber:1", p_impact_dep=0.8)
    _install(rows, preds)
    strip = _strip(_oslo(7, 42))
    assert [(k, t.level) for k, t in strip.items()] == [("07:48", "red"), ("08:03", "red"), ("08:18", "red")]
    cells = impact._trip_cells(
        {"NSB:SituationNumber:1": preds}, FROM, TO, _oslo(7, 0), _oslo(9, 0), select="worst"
    )
    assert len(cells) == 1


def test_boarding_closed_alighting_closed_and_the_wrong_direction_are_not_trains():
    rows = (
        _train("ok", _oslo(7, 10))
        + _train("no_board", _oslo(7, 20), board={"boarding_allowed": False})
        + _train("no_alight", _oslo(7, 30), alight={"alighting_allowed": False})
        + [  # reverse: Asker first, then Oslo S
            _row("rev", 2, TO, _oslo(7, 40), None),
            _row("rev", 6, FROM, _oslo(8, 0), _oslo(7, 59)),
        ]
        + _train("late", _oslo(9, 1))  # one minute outside the window
        + _train("edge", _oslo(9, 0))  # on the edge: inclusive
    )
    _install(rows)
    assert list(_strip(_oslo(7, 0))) == ["07:10", "09:00"]


def test_every_ref_of_the_place_is_walked():
    """A definition carries the canonical ref (the bus side for Asker); the rail calls sit on the
    rail ref. Walked on the canonical ref alone the strip is empty."""
    definition = {**DEFINITION, "to_stop": TO_CANONICAL}
    _install(_train("a", _oslo(7, 48)))
    assert js.status_for(definition, _oslo(7, 42)).trains == []
    _install(_train("a", _oslo(7, 48)), place_refs=lambda r: (r, TO) if r == TO_CANONICAL else (r,))
    assert [t.jkey for t in js.status_for(definition, _oslo(7, 42)).trains] == ["a"]


def test_the_train_set_equals_a_brute_force_walk_over_random_spines():
    rng = random.Random(20260922)
    stops = [FROM, MID, TO, "NSR:StopPlace:9"]
    start, end = _oslo(7, 0), _oslo(9, 0)
    for _ in range(200):
        rows = []
        for j in range(rng.randint(0, 12)):
            order = 0
            t = _oslo(6, 30) + timedelta(minutes=rng.randint(0, 180))
            for _call in range(rng.randint(1, 5)):
                order += rng.randint(1, 3)
                t += timedelta(minutes=rng.randint(1, 15))
                rows.append(_row(
                    f"j{j}", order, rng.choice(stops),
                    None if rng.random() < 0.1 else t, None if rng.random() < 0.1 else t - timedelta(minutes=1),
                    boarding_allowed=rng.random() > 0.15, alighting_allowed=rng.random() > 0.15,
                ))
        _install(rows)
        got = {t.jkey for t in js.status_for(DEFINITION, _oslo(8, 0)).trains}
        expected = set()
        for j in {r.jkey for r in rows}:
            calls = [r for r in rows if r.jkey == j]
            for b in calls:
                boards = (b.stop_place == FROM and b.aimed_dep is not None and b.boarding_allowed
                          and start <= b.aimed_dep <= end)
                if boards and any(a.stop_place == TO and a.aimed_arr is not None and a.alighting_allowed
                                  and a.stop_order > b.stop_order for a in calls):
                    expected.add(j)
        assert got == expected



def test_a_train_taken_out_of_the_plan_is_a_red_cancellation_that_says_so():
    """R9 of plan 2026-09-23-001: red, cancelled, a ring until its aimed departure and filled after
    -- a declared cancellation's rule -- and marked, so the label reads «tatt ut av rutetabellen»."""
    removed = {"source": "removed", "call_cancelled": True, "bus_for_train": True}
    rows = _train("RemovedJourney:2105_2026-09-22", _oslo(7, 10),
                  board=removed, mid=removed, alight=removed)
    rows += _train("j0740", _oslo(7, 40))
    _install(rows)
    before = _strip(_oslo(7, 0))["07:10"]
    assert (before.state, before.level, before.cancelled) == ("predicted", "red", True)
    assert (before.removed_from_plan, before.bus_for_train, before.impact_probability) == (True, True, 1.0)
    after = _strip(_oslo(7, 20))["07:10"]
    assert (after.state, after.level, after.removed_from_plan) == ("confirmed", "red", True)
    other = _strip(_oslo(7, 0))["07:40"]
    assert (other.removed_from_plan, other.bus_for_train, other.cancelled) == (False, False, False)


def test_the_spine_says_where_its_removed_trains_call():
    """The digest's pre-filter reads this: a day with no removed train must answer an empty set,
    and so must an unloaded spine (never a raise)."""
    js.reset_state()
    assert js.removed_today() == frozenset()
    removed = {"source": "removed", "call_cancelled": True}
    rows = _train("RemovedJourney:2105_2026-09-22", _oslo(7, 10), board=removed, mid=removed,
                  alight=removed)
    _install(rows)
    stops = js.removed_today()
    assert FROM in stops and TO in stops
    _install(_train("j0740", _oslo(7, 40)))
    assert js.removed_today() == frozenset(), "a train that runs is not a removed one"


def test_the_train_set_agrees_with_the_push_matchers_eligibility():
    """The strip restates `impact._trip_cells`' eligibility on the spine's rows (board at the
    from-stop inside the window with boarding open, alight later at the to-stop with alighting
    open), and a restated rule is how two surfaces drift. So the SAME random trains, one situation
    each so the matcher's per-line pick cannot hide one, go through both, and the train sets must
    match. Trains call the origin at most once: for a loop the two knowingly differ (see `_trains`).
    """
    rng = random.Random(7)
    stops = [MID, TO, "NSR:StopPlace:9"]
    start, end = _oslo(7, 0), _oslo(9, 0)
    for _ in range(200):
        rows, scope = [], {}
        for j in range(rng.randint(0, 10)):
            # Some trains are taken out of the plan (plan 2026-09-23-001): the spine's rows come from
            # rail_calls_removed_today, and the relay scores them as certainties under their own
            # situation. Eligibility must not care which table a train came from.
            removed = rng.random() < 0.3
            t = _oslo(6, 30) + timedelta(minutes=rng.randint(0, 180))
            calls = [FROM] + [rng.choice(stops) for _ in range(rng.randint(0, 4))]
            rng.shuffle(calls)
            train_rows = []
            for order, stop in enumerate(calls, start=1):
                t += timedelta(minutes=rng.randint(1, 15))
                train_rows.append(_row(
                    f"j{j}", order, stop,
                    None if rng.random() < 0.1 else t, None if rng.random() < 0.1 else t - timedelta(minutes=1),
                    boarding_allowed=rng.random() > 0.15, alighting_allowed=rng.random() > 0.15,
                    **({"source": "removed", "call_cancelled": True} if removed else {}),
                ))
            rows += train_rows
            sit = f"KIX:PlanRemoved:j{j}" if removed else f"S{j}"
            certainty = {"et_declared_cancelled": True, "p_cancel": 1.0, "p_impact_dep": 1.0,
                         "call_source": "removed", "has_situation": False} if removed else {}
            scope[sit] = [
                _pred(r.jkey, r.stop_order, r.stop_place, r.aimed_dep, r.aimed_arr, sit=sit,
                      boarding_allowed=r.boarding_allowed, alighting_allowed=r.alighting_allowed,
                      **certainty)
                for r in train_rows
            ]
        _install(rows)
        strip = {t.jkey for t in js.status_for(DEFINITION, _oslo(8, 0)).trains}
        matcher = {c.jkey for c in impact._trip_cells(scope, FROM, TO, start, end, select="worst")}
        assert strip == matcher, (strip, matcher)

# ---- Module boundary ---------------------------------------------------------------------------


def test_the_module_imports_only_impact_alert_policy_line_modes_pydantic_and_the_stdlib():
    tree = ast.parse(Path(js.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    local = names - set(sys.stdlib_module_names)
    assert local == {"impact", "alert_policy", "line_modes", "pydantic"}, local


def test_a_train_is_named_by_its_terminus_and_only_a_real_terminus():
    """«RE10 Hamar»: the last call's name, «stasjon» dropped, and nothing when the table's last call
    for the train still has a departure (a journey held only in part) or the train is unknown."""
    dep = _oslo(7, 3)
    whole = _train("a", dep, terminus=True, alight={"destination": "Hamar stasjon"})
    cut = _train("b", dep, alight={"destination": "Hamar stasjon"})
    js.set_spine(js.CallTable(whole + cut, dep))
    try:
        assert js.destination("VYG", "a") == "Hamar"
        assert js.destination("VYG", "b") is None
        assert js.destination("VYG", "nope") is None
        assert js.destination(None, "a") is None
    finally:
        js.reset_state()


def _run():
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} journey_status checks passed.")


if __name__ == "__main__":
    _run()
