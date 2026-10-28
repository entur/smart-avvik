"""Hermetic checks for `journey_status`: the «I dag» strip's per-train grading (plan 2026-09-22-001).

Runs standalone (`uv run python test_journey_status.py`) or under pytest. No BigQuery, no network:
the spine and the actuals are built from literal rows, and the prediction cache is a dict the
engine reads through `impact.configure(call_lookup=...)`, exactly as `main` wires it.
"""

from __future__ import annotations

import ast
import os
import random
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import impact
import journey_status as js
import trips

OSLO = ZoneInfo("Europe/Oslo")
FROM = "NSR:StopPlace:337"  # Oslo S
MID = "NSR:StopPlace:444"  # Sandvika
TO = "NSR:StopPlace:418"  # Asker, rail side -- where every rail call sits
TO_CANONICAL = "NSR:StopPlace:3247"  # Asker, the busier bus side the picker collapses onto
VIA = "NSR:StopPlace:9"  # a second change point, for KTD19's sibling continuation

# 2026-09-22 is a Tuesday (weekday 1).
DEFINITION = {"from_stop": FROM, "to_stop": TO, "begin": "07:00", "end": "09:00", "days": [0, 1, 2, 3, 4]}
DAY = date(2026, 9, 22)

_index: dict[tuple[str, str], list[impact.CallPred]] = {}
impact.configure(
    snapshot_provider=lambda: ({}, None),
    call_lookup=lambda operator, jkey: _index.get((operator, jkey)),
)
# The trips with a change (multi-leg U9): the day plans the strip reads through its injected reader,
# keyed (from, to, Oslo date) as `trip_plans.read_plan` keys them, and JP's real-time answers per leg.
# The verdict reads the SAME call index the strip does, as `main` wires both.
_plans: dict[tuple[str, str, date], trips.DayPlan] = {}
_realtime: dict[trips.PlanLeg, trips.LegRealtime] = {}


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


def _install(rows, preds=(), *, actuals=None, place_refs=None, plan=None, realtime=None):
    _index.clear()
    for p in preds:
        _index.setdefault((p.operator, p.jkey), []).append(p)
    _plans.clear()
    if plan is not None:
        _plans[(FROM, TO, DAY)] = plan
    _realtime.clear()
    _realtime.update(realtime or {})
    trips.configure(call_lookup=lambda operator, jkey: _index.get((operator, jkey)), realtime=_realtime.get)
    js.reset_state()
    js.configure(
        predictions_refreshed_at=lambda: _oslo(7, 40), place_refs=place_refs or (lambda r: (r,)),
        trip_plans=lambda from_stop, to_stop, day: _plans.get((from_stop, to_stop, day)),
    )
    js.set_spine(js.CallTable(rows, _oslo(7, 5)))
    if actuals is not None:
        js.set_actuals(js.CallTable(actuals, _oslo(7, 35)))


@contextmanager
def _trips_on(on=True):
    """TRIP_PLANS_ENABLED, put back after."""
    before = os.environ.get("TRIP_PLANS_ENABLED")
    os.environ["TRIP_PLANS_ENABLED"] = "true" if on else "false"
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TRIP_PLANS_ENABLED", None)
        else:
            os.environ["TRIP_PLANS_ENABLED"] = before


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



def test_a_trip_split_across_two_situations_is_paired_as_the_matcher_pairs_it():
    """A situation whose onset falls mid-trip scores the boarding call while the alighting call sits
    in the no-SX branch. Since 2026-10-22 the matcher pairs the two and the push fires on the
    boarding figure, so the strip reds it by the gate too. With no alighting row in the cache at
    all, nothing fires, and the strip may grade the train but not red it."""
    dep = _oslo(7, 48)
    board, _ = _scored("a", dep, sit="S1", p_impact_dep=0.95, dep_delay_p50_stack_min=4)
    _, alight = _scored("a", dep, sit="KIX:NoSituation:a", has_situation=False, arr_delay_p50_stack_min=5)
    _install(_train("a", dep), [board, alight])
    t = _strip(_oslo(7, 42))["07:48"]
    assert (t.state, t.level, t.alert_worthy, t.impact_probability) == ("predicted", "red", True, 0.95)
    assert t.delay_p50_min == 5, "the arrival side's figure, off the other situation's row"
    [cell] = impact._trip_cells(
        {"S1": [board], "KIX:NoSituation:a": [alight]}, FROM, TO, _oslo(7, 0), _oslo(9, 0), select="worst"
    )
    assert (cell.situation_number, cell.departure.impact_probability) == ("S1", 0.95)

    _install(_train("a", dep), [board])
    t = _strip(_oslo(7, 42))["07:48"]
    # Graded off the boarding call's own 4 min, not red although it scored 0.95.
    assert (t.state, t.level, t.alert_worthy, t.impact_probability) == ("predicted", "green", False, None)


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
    each so the matcher's per-line pick cannot hide one (a split train's second half is in a
    situation of its own), go through both, and the train sets must match. Trains call the origin
    at most once: for a loop the two knowingly differ (see `_trains`).

    Over both paths since multi-leg U9: the trips with a change too, against `trips.judge` (below).
    A trip that alerted while admitted and still stands for a definition (`varsel`'s `standing`) is
    the one the two knowingly differ on: that is per-registration state a public strip cannot see.
    """
    rng = random.Random(7)
    stops = [MID, TO, "NSR:StopPlace:9"]
    start, end = _oslo(7, 0), _oslo(9, 0)
    seen = {"listed": 0, "direct_won": 0, "not_admitted": 0}
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
            # Some trains have their calls split across two situations, the way `predict_calls.sql`
            # splits a partial cancellation or an SX message whose onset falls mid-trip. The matcher
            # pairs across them (2026-10-22), so the split must not change the train set.
            cut = rng.randint(1, len(train_rows)) if not removed and rng.random() < 0.4 else 0
            for part, part_rows in ((sit, train_rows[cut:]), (f"N{j}", train_rows[:cut])):
                if part_rows:
                    scope[part] = [
                        _pred(r.jkey, r.stop_order, r.stop_place, r.aimed_dep, r.aimed_arr, sit=part,
                              boarding_allowed=r.boarding_allowed,
                              alighting_allowed=r.alighting_allowed, **certainty)
                        for r in part_rows
                    ]
        # The trips with a change (multi-leg U9): the same matcher's other half, `trips.judge`, over a
        # random day plan. Every first leg carries a figure, so the strip's figure-less departures
        # (which `judge` leaves out by default) cannot make the comparison tautological. Some first
        # legs are a spine train's own departure (AE7: the direct row wins, as `varsel`'s merge has a
        # direct alert win), and some trips arrive after a direct departure leaving at or after them
        # (KTD20: not a subject, so the request-path matcher does not list them either).
        plan_trips, directs, trip_preds = [], [], []
        boards = [r for r in rows if r.stop_place == FROM and r.aimed_dep is not None]
        for k in range(rng.randint(0, 4)):
            first, dep = f"t{k}", _oslo(6, 45) + timedelta(minutes=rng.randint(0, 150))
            if boards and rng.random() < 0.25:
                shared = rng.choice(boards)
                first, dep = shared.jkey, shared.aimed_dep
            via = rng.choice([MID, VIA])
            plan_trips.append(trips.PlanTrip(legs=(
                _plan_leg(first, FROM, via, dep, dep + timedelta(minutes=10)),
                _plan_leg(f"u{k}", via, TO, dep + timedelta(minutes=16),
                          dep + timedelta(minutes=rng.randint(25, 60))),
            )))
            trip_preds.append(_pred(first, 1, FROM, dep, None, sit=f"T{k}", p_impact_dep=rng.random()))
            if rng.random() < 0.3:
                leaves = dep + timedelta(minutes=rng.randint(-10, 30))
                directs.append(trips.DirectDeparture(
                    operator="VYG", refs=(("VYG", f"d{k}"),), board_stop_place=FROM,
                    aimed_departure=leaves, aimed_arrival=leaves + timedelta(minutes=rng.randint(15, 70)),
                ))
        plan = trips.DayPlan(trips=tuple(plan_trips), directs=tuple(directs))
        _install(rows, trip_preds, plan=plan)
        with _trips_on():
            status = js.status_for(DEFINITION, _oslo(8, 0))
            judged = trips.judge(plan, to_stop=TO, window_start=start, window_end=end)
        strip = {t.jkey for t in status.trains if t.trip is None}
        matcher = {c.jkey for c in impact._trip_cells(scope, FROM, TO, start, end, select="worst")}
        assert strip == matcher, (strip, matcher)
        direct_keys = {(t.operator, t.jkey, t.departure_time) for t in status.trains if t.trip is None}
        strip_trips = {(t.operator, t.jkey, t.departure_time) for t in status.trains if t.trip is not None}
        subjects = {v.key for v in judged if v.admitted}
        assert strip_trips == subjects - direct_keys, (strip_trips, subjects, direct_keys)
        seen["listed"] += len(strip_trips)
        seen["direct_won"] += len(subjects & direct_keys)
        seen["not_admitted"] += sum(not v.admitted for v in judged)
    # Each rule did some work, or the comparison above proved less than it claims.
    assert all(seen.values()), seen


# ---- Trips with a change (multi-leg U9, KTD14) --------------------------------------------------
# `_change(dep)`: train `f` from FROM at `dep` to MID ten minutes later, a six-minute change, then
# train `s` from MID to TO, arriving forty minutes after `dep`. Neither train calls at both ends, so
# neither is a direct row. The plan is (FROM, TO)'s for 2026-09-22.


def _plan_leg(jkey, board, alight, dep, arr, *, operator="VYG", mode="rail", walk=0):
    return trips.PlanLeg(
        operator=operator, refs=((operator, jkey),), line=f"{operator}:Line:R12", public_code="R12",
        mode=mode, board_stop_place=board, alight_stop_place=alight, board_quay=None,
        alight_quay=None, board_position=0, alight_position=4, aimed_departure=dep,
        aimed_arrival=arr, service_date=dep.astimezone(OSLO).date(), walk_after_s=walk,
    )


def _change(dep, *, first="f", second="s", first_operator="VYG", first_mode="rail"):
    return trips.PlanTrip(legs=(
        _plan_leg(first, FROM, MID, dep, dep + timedelta(minutes=10), operator=first_operator,
                  mode=first_mode),
        _plan_leg(second, MID, TO, dep + timedelta(minutes=16), dep + timedelta(minutes=40)),
    ))


def _change_rows(dep, *, first="f", second="s", board_kw=None, transfer_kw=None, connect_kw=None,
                 alight_kw=None):
    """The spine's rows for `_change`'s trains: `f` FROM (3) -> MID (5) -> VIA (7), and `s` MID (2)
    -> TO (6), its terminus."""
    return [
        _row(first, 3, FROM, dep, None, **(board_kw or {})),
        _row(first, 5, MID, dep + timedelta(minutes=11), dep + timedelta(minutes=10), **(transfer_kw or {})),
        _row(first, 7, VIA, dep + timedelta(minutes=21), dep + timedelta(minutes=20)),
        _row(second, 2, MID, dep + timedelta(minutes=16), dep + timedelta(minutes=15), **(connect_kw or {})),
        _row(second, 6, TO, None, dep + timedelta(minutes=40), **(alight_kw or {})),
    ]


def _change_preds(dep, *, first="f", second="s", board=None, transfer=None, connect=None, arrive=None):
    """The cache's rows for `_change`: both trains scored at both their ends, quiet by default --
    a 1-minute typical delay and a P(miss) read-off of 0.02 against the change's 6 minutes of slack."""
    return [
        _pred(first, 3, FROM, dep, None, **(board or {})),
        _pred(first, 5, MID, dep + timedelta(minutes=11), dep + timedelta(minutes=10), **(transfer or {})),
        _pred(second, 2, MID, dep + timedelta(minutes=16), dep + timedelta(minutes=15), **(connect or {})),
        _pred(second, 6, TO, None, dep + timedelta(minutes=40), **(arrive or {})),
    ]


def _plan(*trip_list, directs=()):
    return trips.DayPlan(trips=trip_list, directs=directs)


def _status(now):
    status = js.status_for(DEFINITION, now)
    assert status is not None
    return status


def _trip_row(now):
    """The one trip row on the strip at `now`."""
    [row] = [t for t in _status(now).trains if t.trip is not None]
    return row


def test_a_trip_with_a_change_is_listed_keyed_on_its_first_leg_beside_the_direct_trains():
    """R13: one row per first-leg departure, in departure order with the direct trains, carrying
    the one trip shape (KTD13). Graded end to end: green on an expected arrival 1 min late."""
    dep = _oslo(7, 50)
    with _trips_on():
        _install(_train("d", _oslo(7, 20)) + _change_rows(dep), _change_preds(dep), plan=_plan(_change(dep)))
        status = _status(_oslo(7, 42))
    assert [(js_time(t.departure_time), t.trip is not None) for t in status.trains] == [
        ("07:20", False), ("07:50", True)
    ]
    row = status.trains[1]
    assert (row.operator, row.jkey, row.line, row.mode) == ("VYG", "f", "VYG:Line:R12", "rail")
    assert (row.departure_time, row.arrival_time) == (dep, dep + timedelta(minutes=40))
    assert (row.state, row.level, row.cancelled, row.alert_worthy) == ("predicted", "green", False, False)
    assert row.delay_p50_min == 1, "the end-to-end arrival, not the first train's"
    assert row.impact_probability == 0.03, "the figure the verdict was taken on"
    assert row.expected_departure_time == dep + timedelta(minutes=1)
    assert [leg.board_stop_place for leg in row.trip.legs] == [FROM, MID]
    assert [t.stop_place for t in row.trip.transfers] == [MID]
    assert row.trip.expected_arrival == dep + timedelta(minutes=41) and row.trip.problem is None
    assert status.trains[0].trip is None, "a direct train carries no trip"


def test_a_bus_then_train_trip_grades_the_bus_from_jp_real_time_and_the_train_from_the_spine():
    """KTD14: an unscored bus leg is graded from JP's real-time answer, the train as today. JP's
    expected arrival 1 min late keeps the change; 12 min late misses it (AE4), which is red. With no
    answer for the bus the trip is grey, never green. Once the train has arrived, its recorded call
    in the spine grades the trip, as it grades a train, and nothing else is needed."""
    dep = _oslo(7, 50)
    trip = _change(dep, first="ServiceJourney:b1", first_operator="RUT", first_mode="bus")
    bus = trip.legs[0]
    rows = _change_rows(dep)[3:]  # the train only: the spine is Vy and SJ Norge
    preds = _change_preds(dep)[2:]  # the train's two ends; no message names the bus
    late = {
        minutes: trips.LegRealtime(expected_departure=dep, expected_arrival=dep + timedelta(minutes=minutes))
        for minutes in (11, 22)
    }
    with _trips_on():
        _install(rows, preds, plan=_plan(trip), realtime={bus: late[11]})
        row = _trip_row(_oslo(7, 42))
        assert (row.operator, row.mode, row.state, row.level) == ("RUT", "bus", "predicted", "green")
        assert [leg.data_source for leg in row.trip.legs] == ["jp_realtime", "prediction"]
        _install(rows, preds, plan=_plan(trip), realtime={bus: late[22]})
        row = _trip_row(_oslo(7, 42))
        assert (row.state, row.level, row.alert_worthy) == ("predicted", "red", True)
        assert (row.trip.problem.kind, row.trip.problem.index) == ("missed_connection", 0)
        _install(rows, preds, plan=_plan(trip))
        row = _trip_row(_oslo(7, 42))
        assert (row.state, row.level, row.alert_worthy) == ("unknown", "grey", False), "never green"
        assert [leg.data_source for leg in row.trip.legs] == ["none", "prediction"], "«mangler sanntid»"
        recorded = _change_rows(dep, alight_kw={"source": "recorded", "arr_delay_min": 6})[3:]
        _install(recorded, plan=_plan(trip))
        row = _trip_row(_oslo(8, 45))
    assert (row.state, row.level, row.delay_min) == ("confirmed", "yellow", 6)
    assert row.observed_at == dep + timedelta(minutes=40)


def test_a_trip_is_never_green_while_a_leg_is_grey():
    """R13: a leg nothing can say anything about greys the trip, however quiet the rest is (the bus
    case above). A no-SX figure more than `NOSX_HORIZON_MIN` out is nothing, as it is for a direct
    train (KTD7): the same quiet figures are green inside the horizon and grey beyond it."""
    dep = _oslo(8, 50)
    nosx = [replace(p, situation_number="KIX:NoSituation:x", has_situation=False) for p in _change_preds(dep)]
    with _trips_on():
        _install(_change_rows(dep), nosx, plan=_plan(_change(dep)))
        far, near = _trip_row(_oslo(6, 30)), _trip_row(_oslo(7, 42))
    assert (far.state, far.level) == ("unknown", "grey")
    assert (near.state, near.level) == ("predicted", "green")


def test_an_observed_missed_connection_after_departure_is_red():
    """AE8: the first train recorded 9 min late at the change, the connecting train recorded on
    time, and its departure has passed: the change was not made. Red and a fact, named as the
    problem, whatever the (stale) forecast said. Before the connecting train leaves it is not yet
    an observation."""
    dep = _oslo(7, 20)
    arrived_late = {"source": "recorded", "dep_delay_min": 9}
    left = {"source": "recorded", "dep_delay_min": 0}
    with _trips_on():
        _install(_change_rows(dep, board_kw=left, transfer_kw=arrived_late, connect_kw=left),
                 _change_preds(dep), plan=_plan(_change(dep)))
        row = _trip_row(_oslo(7, 45))
        assert (row.state, row.level, row.cancelled, row.alert_worthy) == ("confirmed", "red", False, False)
        assert (row.trip.problem.kind, row.trip.problem.index) == ("missed_connection", 0)
        assert (row.trip.problem.probability, row.observed_at) == (1.0, dep + timedelta(minutes=10))
        assert (row.trip.expected_arrival, row.impact_probability) == (None, None)
        _install(_change_rows(dep, board_kw=left, transfer_kw=arrived_late),
                 _change_preds(dep), plan=_plan(_change(dep)))
        row = _trip_row(_oslo(7, 35))
    assert (row.state, row.level, row.trip.problem) == ("predicted", "green", None)


def test_a_change_is_not_observed_missed_before_the_connecting_departure_is_recorded():
    """AE8: the first train recorded 9 min late at the change and the connecting train's aimed
    departure passed, but its departure is not recorded yet -- it may be held or late too, so the
    change is no fact yet. JP's expected departure for it, once passed, is one."""
    dep = _oslo(7, 20)
    arrived_late = {"source": "recorded", "dep_delay_min": 9}
    rows = _change_rows(dep, board_kw={"source": "recorded", "dep_delay_min": 0}, transfer_kw=arrived_late)
    trip = _change(dep)
    with _trips_on():
        _install(rows, _change_preds(dep), plan=_plan(trip))
        row = _trip_row(_oslo(7, 45))
        assert (row.state, row.trip.problem) == ("predicted", None)
        expected = trips.LegRealtime(expected_departure=dep + timedelta(minutes=18),
                                     expected_arrival=dep + timedelta(minutes=42))
        _install(rows, _change_preds(dep), plan=_plan(trip), realtime={trip.legs[1]: expected})
        row = _trip_row(_oslo(7, 45))
    assert (row.state, row.level) == ("confirmed", "red")
    assert (row.trip.problem.kind, row.trip.problem.index) == ("missed_connection", 0)


def test_a_broken_change_whose_cushion_holds_is_graded_on_the_cushions_arrival_never_red():
    """AE11 and KTD19: the change at MID is likely missed (P(miss) 0.79), but the same first train
    stays on to VIA and reaches TO 6 min later through a change that holds. The alert stays silent,
    so the strip is not red: it is graded on the cushion's arrival, 7 min late (yellow). With the
    VIA continuation in trouble too, both are red."""
    dep = _oslo(7, 50)
    planned = _change(dep)
    sibling = trips.PlanTrip(legs=(
        _plan_leg("f", FROM, VIA, dep, dep + timedelta(minutes=20)),
        _plan_leg("u", VIA, TO, dep + timedelta(minutes=26), dep + timedelta(minutes=46)),
    ))
    plan = _plan(planned, sibling)
    rows = _change_rows(dep) + [
        _row("u", 2, VIA, dep + timedelta(minutes=26), dep + timedelta(minutes=25)),
        _row("u", 6, TO, None, dep + timedelta(minutes=46)),
    ]

    def preds(u_board):
        return _change_preds(dep, transfer={"p_arr_ge5": 0.9, "p_arr_ge10": 0.8}) + [
            _pred("f", 7, VIA, dep + timedelta(minutes=21), dep + timedelta(minutes=20)),
            _pred("u", 2, VIA, dep + timedelta(minutes=26), dep + timedelta(minutes=25), p_impact_dep=u_board),
            _pred("u", 6, TO, None, dep + timedelta(minutes=46)),
        ]

    window = {"to_stop": TO, "window_start": _oslo(7), "window_end": _oslo(9)}
    with _trips_on():
        _install(rows, preds(0.03), plan=plan)
        row = _trip_row(_oslo(7, 42))
        [verdict] = trips.judge(plan, **window)
        assert (row.level, row.alert_worthy, verdict.alertworthy) == ("yellow", False, False)
        assert (row.delay_p50_min, row.trip.expected_arrival) == (7, dep + timedelta(minutes=47))
        assert row.trip.problem is None and row.arrival_time == dep + timedelta(minutes=40)
        assert row.trip.arrives_via == VIA, "«fram 08:37 via ...»: no name on our rows, the ref"
        _install(rows, preds(0.9), plan=plan)
        row = _trip_row(_oslo(7, 42))
        [verdict] = trips.judge(plan, **window)
    assert (row.level, row.alert_worthy, verdict.alertworthy) == ("red", True, True)
    assert row.trip.problem.kind == "missed_connection", "the planned route's reason (KTD13)"


def test_a_trip_whose_connecting_train_is_taken_out_of_the_plan_is_red_and_says_so():
    """AE13 and KTD6: the connecting train is out of today's plan (the relay's `KIX:PlanRemoved:`
    certainty), with no continuation to cushion it. Red, cancelled, and the problem says
    `leg_removed` on leg 1 -- «toget videre fra Sandvika er tatt ut av rutetabellen».

    The morning digest's set has it, as the CONNECTING train (`removed_train`): its own relay key,
    dated by the day, its line and its departure from the change -- never the first leg, which
    runs. `removed_from_plan` stays False on the wire, since it would name this row's first train;
    and `removed_today` names the trip's from-place, which the removed train never calls at."""
    dep = _oslo(7, 50)
    removed = "RemovedJourney:2105_2026-09-22"
    trip = _change(dep, second="DatedServiceJourney:2105_SVI-ASR_26-09-22")
    certainty = {"et_declared_cancelled": True, "p_cancel": 1.0, "p_impact_dep": 1.0,
                 "call_source": "removed", "has_situation": False, "sit": f"KIX:PlanRemoved:{removed}"}
    preds = _change_preds(dep)[:2] + [
        _pred(removed, 2, MID, dep + timedelta(minutes=16), dep + timedelta(minutes=15), **certainty),
        _pred(removed, 6, TO, None, dep + timedelta(minutes=40), **certainty),
    ]
    out = {"source": "removed", "call_cancelled": True, "bus_for_train": True}
    rows = _change_rows(dep, second=removed, connect_kw=out, alight_kw={**out, "destination": "Asker stasjon"})
    with _trips_on():
        _install(rows, preds, plan=_plan(trip))
        js.configure(predictions_refreshed_at=lambda: _oslo(7, 40), place_refs=lambda r: (r,),
                     trip_plans=lambda *key: _plans.get(key), held_plans=lambda: list(_plans.values()))
        row = _trip_row(_oslo(7, 42))
        stops = js.removed_today()
    assert (row.state, row.level, row.cancelled, row.alert_worthy, row.impact_probability) == (
        "predicted", "red", True, True, 1.0
    )
    assert (row.trip.problem.kind, row.trip.problem.index) == ("leg_removed", 1)
    assert (row.removed_from_plan, row.bus_for_train) == (False, False)
    assert js.removed_train(row) == js.RemovedTrain(
        jkey=removed, line="VYG:Line:R12", departure_time=dep + timedelta(minutes=16),
        destination="Asker", bus_for_train=True,
    )
    assert FROM in stops, "the trip's from-place, which the removed train never calls at"
    with _trips_on(False):
        assert FROM not in js.removed_today(), "flag off: the spine's own stops only"


def test_a_removed_connecting_train_a_cushion_holds_is_no_digest_train():
    """KTD19 first (KTD6): the same first train stays on to VIA and gets there 6 min later, so the
    trip is not red and the digest does not name the removed train for it. With that cushion only
    predicted to fail, the trip is red on a forecast and the digest does name it."""
    dep = _oslo(7, 50)
    removed = "RemovedJourney:2105_2026-09-22"
    planned = _change(dep, second="DatedServiceJourney:2105_SVI-ASR_26-09-22")
    sibling = trips.PlanTrip(legs=(
        _plan_leg("f", FROM, VIA, dep, dep + timedelta(minutes=20)),
        _plan_leg("u", VIA, TO, dep + timedelta(minutes=26), dep + timedelta(minutes=46)),
    ))
    certainty = {"et_declared_cancelled": True, "p_cancel": 1.0, "p_impact_dep": 1.0,
                 "call_source": "removed", "has_situation": False, "sit": f"KIX:PlanRemoved:{removed}"}
    preds = _change_preds(dep)[:2] + [
        _pred(removed, 2, MID, dep + timedelta(minutes=16), dep + timedelta(minutes=15), **certainty),
        _pred("f", 7, VIA, dep + timedelta(minutes=21), dep + timedelta(minutes=20)),
        _pred("u", 2, VIA, dep + timedelta(minutes=26), dep + timedelta(minutes=25)),
        _pred("u", 6, TO, None, dep + timedelta(minutes=46)),
    ]
    with _trips_on():
        _install(_change_rows(dep, second=removed), preds, plan=_plan(planned, sibling))
        row = _trip_row(_oslo(7, 42))
        assert (row.level, row.cancelled, row.trip.problem) == ("yellow", False, None)
        assert js.removed_train(row) is None
        # The cushion only predicted to fail: red on a forecast, not confirmed, and the removed
        # train is still the digest's -- the per-train push and the event drop its situation.
        preds[-2] = replace(preds[-2], p_impact_dep=0.9)
        _install(_change_rows(dep, second=removed), preds, plan=_plan(planned, sibling))
        row = _trip_row(_oslo(7, 42))
    assert (row.state, row.level, row.cancelled, row.alert_worthy) == ("predicted", "red", False, True)
    assert (row.trip.problem.kind, row.trip.problem.index) == ("leg_removed", 1)
    assert js.removed_train(row) == js.RemovedTrain(
        jkey=removed, line="VYG:Line:R12", departure_time=dep + timedelta(minutes=16),
        destination=None, bus_for_train=False,
    )


def test_a_run_trip_keeps_a_cancellation_the_cache_has_let_go_of():
    """The cache holds calls from 60 min back, so an hour after a cancelled connecting train its
    rows are gone. The spine still has the cancellation, as it does for a direct train: still red,
    filled once the last leg's aimed departure has passed, and named on leg 1."""
    dep = _oslo(7, 20)
    gone = {"call_cancelled": True}
    with _trips_on():
        _install(_change_rows(dep, connect_kw=gone, alight_kw=gone), plan=_plan(_change(dep)))
        row = _trip_row(_oslo(8, 50))
    assert (row.state, row.level, row.cancelled) == ("confirmed", "red", True)
    assert (row.trip.problem.kind, row.trip.problem.index) == ("leg_cancelled", 1)


def test_a_first_leg_the_spine_lists_as_a_direct_train_is_one_direct_row():
    """AE7 and KTD3: the train boards at FROM and reaches TO itself, so the traveller stays on board
    and the direct row decides that departure. Here the cache has only its boarding row and JP's
    plan dropped it as a direct pattern, so `direct_for_pair` cannot see it -- the strip drops the
    trip on its departure key, as `varsel`'s merge has a direct alert win."""
    dep = _oslo(7, 50)
    trip = _change(dep, first="d")
    plan = _plan(trip)
    with _trips_on():
        _install(_train("d", dep) + _change_rows(dep)[3:], [_pred("d", 3, FROM, dep, None, p_impact_dep=0.9)]
                 + _change_preds(dep)[2:], plan=plan)
        status = _status(_oslo(7, 42))
        judged = trips.judge(plan, to_stop=TO, window_start=_oslo(7), window_end=_oslo(9))
    assert [(t.jkey, t.trip) for t in status.trains] == [("d", None)]
    assert [v.key for v in judged] == [("VYG", "d", dep)], "control: the matcher's half does list it"


def test_a_trip_waiting_for_the_next_direct_train_beats_is_not_listed():
    """KTD20: the direct 07:55 reaches TO at 08:20, before the trip's 08:30, so the trip is only an
    alternative -- not a subject, and the request-path matcher shows admitted trips only."""
    dep = _oslo(7, 50)
    direct = trips.DirectDeparture(operator="VYG", refs=(("VYG", "dd"),), board_stop_place=FROM,
                                   aimed_departure=dep + timedelta(minutes=5),
                                   aimed_arrival=dep + timedelta(minutes=30))
    plan = _plan(_change(dep), directs=(direct,))
    with _trips_on():
        _install(_change_rows(dep), _change_preds(dep), plan=plan)
        status = _status(_oslo(7, 42))
        [verdict] = trips.judge(plan, to_stop=TO, window_start=_oslo(7), window_end=_oslo(9))
    assert [t for t in status.trains if t.trip is not None] == []
    assert verdict.admitted is False


def test_a_trip_already_run_is_listed():
    """R13: the strip lists today's trips in the window, the ones that already ran included -- the
    matcher drops those (R10), the strip does not."""
    dep = _oslo(7, 5)
    with _trips_on():
        _install(_change_rows(dep, alight_kw={"source": "recorded", "arr_delay_min": 2}),
                 plan=_plan(_change(dep)))
        row = _trip_row(_oslo(8, 10))
        assert trips.judge(_plan(_change(dep)), to_stop=TO, window_start=_oslo(7), window_end=_oslo(9),
                           catchable_after=_oslo(8, 10)) == []
    assert (row.state, row.level, row.delay_min) == ("confirmed", "green", 2)


def test_with_the_flag_off_or_no_plan_the_strip_is_todays_with_trip_null_on_every_train():
    """R16 and AE6: off, the answer is exactly what it is with no plan at all, and every train
    carries `trip: null`; the plan is not even read. On with no plan held, no trip row (the read
    says `not_evaluated`, never calm)."""
    dep = _oslo(7, 50)
    rows, preds = _change_rows(dep) + _train("d", _oslo(7, 20)), _change_preds(dep)
    read = []

    def counting(*key):
        read.append(key)
        return _plan(_change(dep))

    with _trips_on(False):
        _install(rows, preds, plan=_plan(_change(dep)))
        js.configure(predictions_refreshed_at=lambda: _oslo(7, 40), place_refs=lambda r: (r,),
                     trip_plans=counting)
        off = _status(_oslo(7, 42))
    with _trips_on():
        _install(rows, preds)
        unplanned = _status(_oslo(7, 42))
    assert read == []
    assert [t.trip for t in off.trains] == [None]
    assert off.model_dump() == unplanned.model_dump()


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
