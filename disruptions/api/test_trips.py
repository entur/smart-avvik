"""Checks for the trip verdict (multi-leg U3): leg resolution, every reason of KTD6, P(miss) of
KTD7, KTD19's composition over continuations, KTD9's expected arrival, KTD20's admission and
KTD3's "is the first train itself direct?".

Hermetic: `trips` is handed an in-memory call lookup and a real-time reader here, and the calls are
real `impact.CallPred`s, so a field the verdict reads that the cache does not carry fails here.

Run: uv run --with pydantic --with google-cloud-bigquery python test_trips.py  (or under pytest)
"""

import ast
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import alert_policy
import impact
import notification_wording
import situation_english
import trips
from trips import DayPlan, DirectDeparture, LegRealtime, PlanLeg, PlanTrip

LIER, DRAMMEN, OSLO_S, ASKER = (
    "NSR:StopPlace:426", "NSR:StopPlace:11", "NSR:StopPlace:337", "NSR:StopPlace:59616"
)
B, C, D = "NSR:StopPlace:B", "NSR:StopPlace:C", "NSR:StopPlace:D"
DAY = date(2026, 10, 28)
SX = "NSB:SituationNumber:1"


def _t(h, mi=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, mi, tzinfo=UTC)


# The prediction cache as the injected lookup sees it, and JP's real-time answers per leg.
_cache: dict[tuple[str | None, str], list] = {}
_realtime: dict[PlanLeg, LegRealtime] = {}


def _reset():
    _cache.clear()
    _realtime.clear()
    trips.configure(
        call_lookup=lambda operator, jkey: _cache.get((operator, jkey)),
        realtime=_realtime.get,
    )


def _call(jkey, stop, order, *, dep=None, arr=None, operator="VYG", situation=SX, **kw):
    """One scored call, a real `impact.CallPred`, filed in the cache under its journey."""
    fields = {
        "situation_number": situation, "summary": "Signalfeil", "description": None,
        "jkey": jkey, "operator": operator, "line": f"{operator}:Line:R13", "stop_place": stop,
        "stop_name": None, "stop_order": order, "aimed_dep": dep, "aimed_arr": arr,
        "et_declared_cancelled": False, "et_dep_delay_min": None, "call_source": "estimated",
        "p_cancel": 0.05, "dep_delay_typ_min": None, "dep_delay_p90_min": None,
        "arr_delay_typ_min": None, "arr_delay_p90_min": None, "p_arr_ge10": None,
        "p_arr_ge20": None, "arr_delay_p50_stack_min": None, "arr_delay_p90_stack_min": None,
        "p_impact": None, "p_dep_ge10": None, "p_dep_ge20": None,
        "dep_delay_p50_stack_min": None, "dep_delay_p90_stack_min": None, "p_impact_dep": None,
        **kw,
    }
    call = impact.CallPred(**fields)
    _cache.setdefault((operator, jkey), []).append(call)
    return call


def _leg(jkey, board, alight, dep, arr, *, operator="VYG", mode="rail", walk=0, day=DAY,
         plain=None, position=3):
    refs = [(operator, jkey)] + ([(operator, plain)] if plain else [])
    return PlanLeg(
        operator=operator, refs=tuple(refs), line=f"{operator}:Line:R13", public_code="R13",
        mode=mode, board_stop_place=board, alight_stop_place=alight, board_quay=None,
        alight_quay=None, board_position=position, alight_position=position + 4,
        aimed_departure=dep, aimed_arrival=arr, service_date=day, walk_after_s=walk,
    )


def _trip(*legs):
    return PlanTrip(legs=legs)


def _direct(jkey, dep, arr, *, board=LIER):
    return DirectDeparture(operator="VYG", refs=(("VYG", jkey),), board_stop_place=board,
                           aimed_departure=dep, aimed_arrival=arr)


def _judge(plan, *, to_stop=OSLO_S, start=None, end=None, **kw):
    return trips.judge(plan, to_stop=to_stop, window_start=start or _t(7), window_end=end or _t(8),
                       **kw)


def _lier_trip(*, connecting_scored=False, arr_ge5=0.9, arr_ge10=0.8, has_situation=True,
               connecting=None, board=None):
    """AE1's 07:17: R13 Lier 07:17 -> Drammen 07:24, then R12 Drammen 07:30 -> Oslo S 08:05.

    Planned slack 6 min, and so is KTD7's (JP's 120 s stays in as margin): a read-off of 0.88,
    P(miss) 0.7943 with the operator message that names the R13, 0.7604 without. Its arrival at
    Drammen carries `p_arr_ge5`/`p_arr_ge10`.
    """
    first = _leg("DatedServiceJourney:1617_X", LIER, DRAMMEN, _t(7, 17), _t(7, 24))
    second = _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5))
    _call(first.refs[0][1], LIER, 4, dep=_t(7, 17), **{"p_impact_dep": 0.3, **(board or {})})
    _call(first.refs[0][1], DRAMMEN, 6, arr=_t(7, 24), dep=_t(7, 25), p_arr_ge5=arr_ge5,
          p_arr_ge10=arr_ge10, p_arr_ge20=0.1, has_situation=has_situation)
    if connecting_scored:
        _call(second.refs[0][1], DRAMMEN, 1, dep=_t(7, 30), **(connecting or {}))
        _call(second.refs[0][1], OSLO_S, 9, arr=_t(8, 5))
    return _trip(first, second)


# ---- P(miss) (KTD7) ------------------------------------------------------------------------------


def test_the_read_off_moves_with_the_slack_and_holds_flat_below_the_first_point():
    _reset()
    call = _call("j", DRAMMEN, 6, arr=_t(7, 24), p_arr_ge5=0.8, p_arr_ge10=0.45, p_arr_ge20=0.2)
    assert [trips._read_off(call, s) for s in (1, 4)] == [0.8, 0.8]
    assert abs(trips._read_off(call, 8) - 0.59) < 1e-9
    assert abs(trips._read_off(call, 15) - 0.325) < 1e-9
    assert trips._read_off(call, 25) == 0.2, "p_arr_ge20 beyond 20 minutes"
    without_five = replace(call, p_arr_ge5=None)
    assert {trips._read_off(without_five, s) for s in (1, 4, 8, 9.9)} == {0.45}, (
        "without p_arr_ge5 the curve starts at the 10-minute point, held flat below it"
    )
    no_point = replace(call, p_arr_ge5=None, p_arr_ge10=None, p_arr_ge20=None)
    assert trips._read_off(no_point, 4) is None


def test_p_miss_corrects_the_read_off_down_where_it_alerts_and_never_invents_a_figure():
    """The fitted correction (data/queries/transfer_miss_backtest.py): a read-off of 0.9 at a
    6-minute slack is 0.8226 with an operator message and 0.7921 without; it stays monotone in the
    read-off, falls with the slack, and a call with no point has no figure."""
    _reset()
    call = _call("j", DRAMMEN, 6, arr=_t(7, 24), p_arr_ge5=0.9, p_arr_ge10=0.9)
    assert round(trips.p_miss(call, 6), 4) == 0.8226
    assert round(trips.p_miss(replace(call, has_situation=False), 6), 4) == 0.7921
    figures = [trips.p_miss(replace(call, p_arr_ge5=p, p_arr_ge10=p), 6) for p in (0.5, 0.7, 0.9, 0.99)]
    assert figures == sorted(figures), figures
    assert all(f < p for f, p in zip(figures[1:], (0.7, 0.9, 0.99), strict=True)), figures
    assert trips.p_miss(call, 8) < trips.p_miss(call, 6), "the same read-off further out"
    assert trips.p_miss(replace(call, p_arr_ge5=None, p_arr_ge10=None), 6) is None


def test_a_scored_connecting_train_shifts_the_slack_by_its_departure_exceedance():
    """`p_dep_ge5` 0.6 and `p_dep_ge10` 0.3 add 5 minutes (KTD7 slack 6 -> 11, read-off 0.88 ->
    0.73, P(miss) 0.7943 -> 0.5294); `p_dep_ge5` 0.4 adds nothing."""
    figures = {}
    for ge5 in (0.6, 0.4):
        _reset()
        trip = _lier_trip(connecting_scored=True,
                          connecting={"p_dep_ge5": ge5, "p_dep_ge10": 0.3, "p_impact_dep": 0.1},
                          board={"p_impact_dep": 0.1})
        [verdict] = _judge(DayPlan(trips=(trip,)))
        assert verdict.deciding.kind == "missed_connection"
        figures[ge5] = verdict.deciding.probability
    assert (round(figures[0.6], 4), round(figures[0.4], 4)) == (0.5294, 0.7943), figures


# ---- The acceptance examples ---------------------------------------------------------------------


def test_ae1_a_likely_late_arrival_at_drammen_is_a_predicted_missed_connection():
    _reset()
    trip = _lier_trip()
    [verdict] = _judge(DayPlan(trips=(trip,)))
    assert verdict.alertworthy and not verdict.confirmed
    assert (verdict.deciding.kind, verdict.deciding.index) == ("missed_connection", 0)
    assert round(verdict.deciding.probability, 4) == 0.7943, verdict.deciding.probability
    assert verdict.deciding.threshold == 0.65, "Vy's bar with a message"
    problem = verdict.facts.problem
    assert (problem.kind, problem.index, round(problem.probability, 4), problem.threshold) == (
        "missed_connection", 0, 0.7943, 0.65
    )
    # Attached to the arriving call's situation, the one an operator message named.
    assert verdict.reason.situation_number == SX and verdict.reason.has_situation
    [transfer] = verdict.facts.transfers
    assert (transfer.stop_place, transfer.walk_min, transfer.slack_min) == (DRAMMEN, 0, 6)
    assert [leg.board_stop_place for leg in verdict.facts.legs] == [LIER, DRAMMEN]
    assert verdict.key == ("VYG", "DatedServiceJourney:1617_X", _t(7, 17))
    assert verdict.facts.expected_arrival is None, "after a missed change, and no sibling to take"


def test_a_no_sx_arriving_call_is_gated_at_the_no_sx_bar():
    _reset()
    [verdict] = _judge(DayPlan(trips=(_lier_trip(has_situation=False),)))
    assert verdict.deciding.kind == "missed_connection"
    assert verdict.deciding.threshold == 0.85 and not verdict.alertworthy, (
        "P(miss) 0.7604 is under 0.85, though it would clear Vy's 0.65"
    )


def test_ae3_a_late_connecting_train_holds_the_connection_and_alerts_on_its_own_boarding():
    _reset()
    trip = _lier_trip(connecting_scored=True,
                      connecting={"p_dep_ge5": 0.7, "p_dep_ge10": 0.3, "p_impact_dep": 0.7})
    [verdict] = _judge(DayPlan(trips=(trip,)))
    assert verdict.alertworthy
    assert (verdict.facts.problem.kind, verdict.facts.problem.index) == ("leg_departure", 1)
    assert verdict.deciding.probability == 0.7


def test_ae2_a_declared_cancellation_of_leg_twos_boarding_call_is_a_confirmed_certainty():
    _reset()
    trip = _lier_trip(connecting_scored=True,
                      connecting={"et_declared_cancelled": True, "p_cancel": 1.0})
    [verdict] = _judge(DayPlan(trips=(trip,)))
    assert verdict.alertworthy and verdict.confirmed
    assert (verdict.facts.problem.kind, verdict.facts.problem.index) == ("leg_cancelled", 1)
    assert verdict.deciding.probability == 1.0
    assert verdict.facts.legs[1].cancelled and not verdict.facts.legs[0].cancelled


def test_a_cancelled_call_in_the_middle_of_a_leg_is_no_reason():
    _reset()
    trip = _lier_trip(arr_ge5=0.1, arr_ge10=0.05)
    _call(trip.legs[0].refs[0][1], ASKER, 5, dep=_t(7, 20), arr=_t(7, 20),
          et_declared_cancelled=True)  # between Lier (4) and Drammen (6)
    [verdict] = _judge(DayPlan(trips=(trip,)))
    assert not verdict.alertworthy and not verdict.facts.legs[0].cancelled


def test_ae7_a_first_train_that_reaches_the_destination_itself_is_not_a_trip():
    """Both ways KTD3 knows a direct first train: the plan's own direct pattern, or its calls in
    the cache alighting at the to-place."""
    _reset()
    trip = _lier_trip(board={"p_impact_dep": 0.95})
    first = trip.legs[0]
    assert len(_judge(DayPlan(trips=(trip,)))) == 1
    assert _judge(DayPlan(trips=(trip,), directs=(
        _direct(first.refs[0][1], first.aimed_departure, _t(7, 55)),
    ))) == []
    _call(first.refs[0][1], OSLO_S, 12, arr=_t(7, 55))
    assert _judge(DayPlan(trips=(trip,))) == []


def test_the_direct_check_reads_every_ref_of_the_to_place():
    _reset()
    trip = _lier_trip()
    _call(trip.legs[0].refs[0][1], "NSR:StopPlace:OsloS-rail", 12, arr=_t(7, 55))
    assert not trips.direct_for_pair(trip, DayPlan(trips=(trip,)), OSLO_S)
    trips.configure(call_lookup=lambda operator, jkey: _cache.get((operator, jkey)),
                    places=lambda ref: (ref, "NSR:StopPlace:OsloS-rail"))
    try:
        assert trips.direct_for_pair(trip, DayPlan(trips=(trip,)), OSLO_S)
    finally:
        _reset()


def test_ae12_on_a_window_with_a_direct_departure_only_a_faster_trip_is_a_subject():
    _reset()
    trip = _lier_trip()  # leaves 07:17, arrives 08:05
    direct_first = _direct("DatedServiceJourney:1641_X", _t(7, 41), _t(8, 0))
    trip_first = _direct("DatedServiceJourney:1641_X", _t(7, 41), _t(8, 20))
    before = _direct("DatedServiceJourney:1611_X", _t(7, 11), _t(7, 50))  # leaves before the trip
    by_directs = {
        "direct_first": (before, direct_first), "trip_first": (before, trip_first),
        "none_after": (before,),
    }
    admitted = {
        name: [v.admitted for v in _judge(DayPlan(trips=(trip,), directs=directs))]
        for name, directs in by_directs.items()
    }
    assert admitted == {"direct_first": [False], "trip_first": [True], "none_after": [True]}, (
        admitted
    )


def test_a_window_without_a_direct_departure_admits_every_trip():
    _reset()
    trip = _lier_trip()
    later = _direct("DatedServiceJourney:1641_X", _t(8, 1), _t(8, 4))  # after the window
    [verdict] = _judge(DayPlan(trips=(trip,), directs=(later,)))
    assert verdict.admitted


def test_a_cancelled_or_removed_direct_departure_does_not_count():
    """The cache is read at judge time, never the plan: the trip is a subject while the only direct
    departure after it is cancelled or taken out of the plan, and not once that is lifted."""
    trip_args = {}
    direct = _direct("DatedServiceJourney:1641_X", _t(7, 41), _t(8, 0))

    def admitted():
        trip = _lier_trip(**trip_args)
        return [v.admitted for v in _judge(DayPlan(trips=(trip,), directs=(direct,)))]

    _reset()
    assert admitted() == [False]
    _reset()
    _call(direct.refs[0][1], LIER, 2, dep=_t(7, 41), et_declared_cancelled=True)
    assert admitted() == [True], "a declared cancellation of its boarding call"
    _reset()
    _call(direct.refs[0][1], OSLO_S, 9, arr=_t(8, 0), et_declared_cancelled=True)
    assert admitted() == [True], "a declared cancellation of its alighting call"
    _reset()
    removed = notification_wording.plan_removed_situation("RemovedJourney:1641_2026-10-28")
    _call("RemovedJourney:1641_2026-10-28", LIER, 2, dep=_t(7, 41), situation=removed,
          et_declared_cancelled=True, has_situation=False)
    assert admitted() == [True], "its train taken out of today's plan"
    _reset()
    _call(direct.refs[0][1], LIER, 2, dep=_t(7, 41))
    assert admitted() == [False], "lifted: running again"


# ---- Leg resolution and coverage (R3, KTD8) ------------------------------------------------------


def test_a_bus_leg_on_tomorrows_service_date_does_not_resolve_to_todays_calls():
    _reset()
    tomorrow = DAY + timedelta(days=1)
    _call("ServiceJourney:b1", ASKER, 3, dep=_t(7, 5), operator="RUT")
    today = _leg("DatedServiceJourney:b1-d1", ASKER, DRAMMEN, _t(7, 5), _t(7, 25),
                 operator="RUT", mode="bus", plain="ServiceJourney:b1")
    tomorrows = _leg("DatedServiceJourney:b1-d2", ASKER, DRAMMEN, _t(7, 5, tomorrow),
                     _t(7, 25, tomorrow), operator="RUT", mode="bus", plain="ServiceJourney:b1",
                     day=tomorrow)
    assert trips.leg_state(today).state == "scored"
    assert trips.leg_state(tomorrows).state == "unknown"
    assert trips.leg_ref(today) == ("RUT", "ServiceJourney:b1"), "a bus keys on its plain ref"


def test_only_a_trip_with_a_vy_or_sj_leg_is_watched():
    _reset()
    bus = _leg("ServiceJourney:b1", ASKER, DRAMMEN, _t(7, 5), _t(7, 25), operator="RUT",
               mode="bus")
    bus2 = _leg("ServiceJourney:b2", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5), operator="RUT",
                mode="bus")
    train = _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5))
    _call("ServiceJourney:b1", ASKER, 3, dep=_t(7, 5), operator="RUT", p_impact_dep=0.95)
    assert not trips.watched(_trip(bus, bus2)) and trips.watched(_trip(bus, train))
    assert _judge(DayPlan(trips=(_trip(bus, bus2),)), to_stop=OSLO_S) == []
    [verdict] = _judge(DayPlan(trips=(_trip(bus, train),)), to_stop=OSLO_S)
    assert verdict.alertworthy and verdict.deciding.threshold == 0.75, "Ruter's own bar"


def test_a_ruter_leg_is_scored_while_its_message_lasts_and_judged_from_jp_after():
    _reset()
    bus = _leg("ServiceJourney:b1", ASKER, DRAMMEN, _t(7, 5), _t(7, 25), operator="RUT",
               mode="bus")
    _call("ServiceJourney:b1", ASKER, 3, dep=_t(7, 5), operator="RUT")
    _realtime[bus] = LegRealtime(expected_arrival=_t(7, 27))
    assert trips.leg_state(bus).state == "scored"
    _cache.clear()  # the message has ended: nothing in the cache scores the bus any more
    assert trips.leg_state(bus).state == "realtime"
    _realtime.clear()
    assert trips.leg_state(bus).state == "unknown"


def test_with_the_stub_reader_every_unscored_leg_is_unknown_and_draws_no_reason():
    _reset()
    trips.configure(call_lookup=lambda operator, jkey: _cache.get((operator, jkey)))
    try:
        trip = _lier_trip(board={"p_impact_dep": 0.1}, arr_ge5=0.1, arr_ge10=0.05)
        _realtime[trip.legs[1]] = LegRealtime(board_cancelled=True)  # not wired, so never read
        [verdict] = _judge(DayPlan(trips=(trip,)))
        assert trips.leg_state(trip.legs[1]).state == "unknown"
        assert not verdict.alertworthy and verdict.unknown_legs == (1,) and not verdict.complete
        assert verdict.deciding.call is not None, "every figure is one of ours"
    finally:
        _reset()


def test_an_unscored_trip_with_nothing_known_is_no_subject():
    _reset()
    first = _leg("DatedServiceJourney:1617_X", LIER, DRAMMEN, _t(7, 17), _t(7, 24))
    second = _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5))
    assert _judge(DayPlan(trips=(_trip(first, second),))) == []


def test_unjudged_returns_the_departure_nothing_has_a_figure_for_with_no_reason():
    """journey-status lists every departure (U9), the figure-less ones grey: `unjudged` returns
    them, with no deciding reason, nothing alert-worthy and every leg unknown -- and the facts of
    the planned route, at its aimed arrival."""
    _reset()
    first = _leg("DatedServiceJourney:1617_X", LIER, DRAMMEN, _t(7, 17), _t(7, 24))
    second = _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5))
    [verdict] = _judge(DayPlan(trips=(_trip(first, second),)), unjudged=True)
    assert (verdict.deciding, verdict.reason, verdict.alertworthy, verdict.confirmed) == (None, None, False, False)
    assert verdict.unknown_legs == (0, 1) and not verdict.complete
    assert verdict.facts.problem is None and verdict.facts.expected_arrival == _t(8, 5)


def test_a_jp_reason_attaches_to_a_kix_trip_situation_with_texts_in_three_languages():
    _reset()
    trip = _lier_trip(board={"p_impact_dep": 0.1}, arr_ge5=0.1, arr_ge10=0.05)
    _realtime[trip.legs[1]] = LegRealtime(board_cancelled=True)
    [verdict] = _judge(DayPlan(trips=(trip,)))
    assert verdict.alertworthy and verdict.confirmed
    assert (verdict.facts.problem.kind, verdict.facts.problem.index) == ("leg_cancelled", 1)
    number = verdict.reason.situation_number
    assert number == "KIX:Trip:VYG:DatedServiceJourney:1230_X:2026-10-28", "dated by the leg"
    assert notification_wording.is_trip_situation(number)
    assert not notification_wording.is_plan_removed(number)
    assert verdict.reason.has_situation is False and verdict.reason.report_type is None
    assert verdict.reason.threshold == alert_policy.threshold_for("VYG", has_situation=False)
    nb_summary, nb_description = notification_wording.TRIP_SITUATION_TEXTS[:2]
    assert (verdict.reason.summary, verdict.reason.description) == (nb_summary, nb_description)
    # Fed through `situation_english.update`'s provided-text path, as main does.
    texts = trips.situation_texts([DayPlan(trips=(trip,))])
    assert set(texts) == {trips.trip_situation(leg) for leg in trip.legs}
    situation_english.update(texts, translate=False)
    try:
        english = situation_english.english(number, nb_summary, nb_description)
        nynorsk = situation_english.nynorsk(number, nb_summary, nb_description)
        assert english.summary == "Disruption on part of the journey"
        assert nynorsk.summary == "Avvik på ein del av reisa"
        assert not english.machine_translated and not nynorsk.machine_translated
    finally:
        situation_english.reset_state()


def test_a_jp_expected_miss_is_a_certainty_and_an_on_time_answer_is_an_observation():
    _reset()
    first = _leg("ServiceJourney:b1", ASKER, DRAMMEN, _t(7, 5), _t(7, 24), operator="RUT",
                 mode="bus", walk=120)
    second = _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5))
    plan = DayPlan(trips=(_trip(first, second),))
    _realtime[first] = LegRealtime(expected_arrival=_t(7, 29))  # + 2 min walk passes 07:30
    [late] = _judge(plan, to_stop=OSLO_S)
    assert late.alertworthy and not late.confirmed and late.deciding.probability == 1.0
    assert late.reason.situation_number == "KIX:Trip:RUT:ServiceJourney:b1:2026-10-28"
    _realtime[first] = LegRealtime(expected_arrival=_t(7, 24))
    [on_time] = _judge(plan, to_stop=OSLO_S)
    assert not on_time.alertworthy
    assert on_time.deciding.probability == 0.0 and on_time.deciding.kind == "missed_connection"
    assert on_time.board == (), "an unscored first leg has no boarding figure of ours"


def test_a_jp_expected_arrival_30_s_late_at_the_minimum_transfer_is_no_miss():
    """JP builds a trip at the walk plus its 120 s transfer slack: here 07:24 in, 2 min on foot,
    07:28 out. KTD8 leaves the 120 s in as margin, so 30 s late is no miss, and only a delay past
    the margin (2 min 30 s) is the certainty."""
    _reset()
    first = _leg("ServiceJourney:b1", ASKER, DRAMMEN, _t(7, 5), _t(7, 24), operator="RUT",
                 mode="bus", walk=120)
    second = _leg("DatedServiceJourney:1228_X", DRAMMEN, OSLO_S, _t(7, 28), _t(8, 5))
    plan = DayPlan(trips=(_trip(first, second),))
    _realtime[first] = LegRealtime(expected_arrival=_t(7, 24) + timedelta(seconds=30))
    [verdict] = _judge(plan)
    assert (verdict.deciding.kind, verdict.deciding.probability) == ("missed_connection", 0.0)
    assert not verdict.alertworthy
    _realtime[first] = LegRealtime(expected_arrival=_t(7, 26) + timedelta(seconds=30))
    [late] = _judge(plan)
    assert late.alertworthy and late.deciding.probability == 1.0


def test_a_jp_reason_whose_leg_turns_unknown_is_not_read_as_an_all_clear():
    """Two cycles after JP's answer aged out (the reader answers None), the leg it stood on is
    unknown: no reason is drawn from it, the subject reads `complete=False` and names the leg in
    `unknown_legs`, which is what U5 keeps a subject standing on that leg from withdrawing on."""
    # U5 pins the withdrawal itself
    _reset()
    trip = _lier_trip(board={"p_impact_dep": 0.1}, arr_ge5=0.1, arr_ge10=0.05)
    _realtime[trip.legs[1]] = LegRealtime(board_cancelled=True)
    [standing] = _judge(DayPlan(trips=(trip,)))
    assert standing.alertworthy and (standing.reason.kind, standing.reason.index) == (
        "leg_cancelled", 1)
    _realtime.clear()
    for _cycle in range(2):
        [verdict] = _judge(DayPlan(trips=(trip,)))
        assert trips.leg_state(trip.legs[1]).state == "unknown"
        assert not verdict.alertworthy and verdict.facts.problem is None
        assert verdict.deciding.call is not None and verdict.deciding.index == 0, (
            "the figure left is one of ours, on the first train"
        )
        assert not verdict.facts.legs[1].cancelled
        assert not verdict.complete and verdict.unknown_legs == (1,)


def test_a_subject_on_the_first_trains_figure_observes_its_fall_while_a_later_leg_is_unknown():
    """KTD8: an unknown leg blocks withdrawal only of a subject standing on it. One standing on the
    first train's departure gets that figure's below-threshold observation, with the R12 unknown."""
    # U5 pins the withdrawal itself
    for p_impact_dep, alertworthy in ((0.9, True), (0.3, False)):
        _reset()
        trip = _lier_trip(board={"p_impact_dep": p_impact_dep}, arr_ge5=0.1, arr_ge10=0.05)
        [verdict] = _judge(DayPlan(trips=(trip,)))
        assert verdict.alertworthy is alertworthy
        assert (verdict.deciding.kind, verdict.deciding.probability) == ("departure", p_impact_dep)
        assert verdict.unknown_legs == (1,) and not verdict.complete


# ---- A train out of today's plan (KTD6, AE13) ----------------------------------------------------


def _removed_leg_two(trip):
    """Vy took the R12 1230 out of today's plan after it was fetched."""
    removed = notification_wording.plan_removed_situation("RemovedJourney:1230_2026-10-28")
    _call("RemovedJourney:1230_2026-10-28", DRAMMEN, 1, dep=_t(7, 30), situation=removed,
          et_declared_cancelled=True, has_situation=False, p_impact_dep=1.0)
    return removed


def test_ae13_a_ridden_leg_on_a_removed_train_attaches_to_its_plan_removed_situation():
    _reset()
    trip = _lier_trip(board={"p_impact_dep": 0.1}, arr_ge5=0.1, arr_ge10=0.05)
    removed = _removed_leg_two(trip)
    [verdict] = _judge(DayPlan(trips=(trip,)))
    assert verdict.alertworthy and verdict.confirmed
    assert (verdict.facts.problem.kind, verdict.facts.problem.index) == ("leg_removed", 1)
    assert verdict.reason.situation_number == removed
    assert notification_wording.is_plan_removed(verdict.reason.situation_number)


def test_ae13_unless_a_continuation_within_ten_minutes_holds():
    _reset()
    trip = _lier_trip(board={"p_impact_dep": 0.1}, arr_ge5=0.1, arr_ge10=0.05)
    _removed_leg_two(trip)
    later = _leg("DatedServiceJourney:1232_X", DRAMMEN, OSLO_S, _t(7, 36), _t(8, 11))
    _call(later.refs[0][1], DRAMMEN, 1, dep=_t(7, 36), p_impact_dep=0.1)
    _call(later.refs[0][1], OSLO_S, 9, arr=_t(8, 11))
    [verdict] = _judge(DayPlan(trips=(trip, _trip(trip.legs[0], later))))
    assert not verdict.alertworthy


def test_only_a_first_train_that_never_leaves_is_first_cancelled():
    """`first_cancelled` is a confirmed shared reason: the boarding call declared cancelled by ET,
    or by JP for a leg we do not score, or the train out of today's plan. A declared cancellation
    of its call at the change is reason index 0 too, but the train leaves, so it is not."""
    def judged(first_rows):
        _reset()
        first = _leg("DatedServiceJourney:1617_X", LIER, DRAMMEN, _t(7, 17), _t(7, 24))
        second = _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5))
        _call(second.refs[0][1], DRAMMEN, 1, dep=_t(7, 30), p_impact_dep=0.1)
        _call(second.refs[0][1], OSLO_S, 9, arr=_t(8, 5))
        first_rows(first)
        [verdict] = _judge(DayPlan(trips=(_trip(first, second),)))
        assert verdict.alertworthy and verdict.confirmed and verdict.facts.problem.index == 0
        return verdict.facts.problem.kind, verdict.first_cancelled

    removed = notification_wording.plan_removed_situation("RemovedJourney:1617_2026-10-28")
    assert judged(lambda leg: _call(leg.refs[0][1], LIER, 4, dep=_t(7, 17),
                                    et_declared_cancelled=True)) == ("leg_cancelled", True)
    assert judged(lambda leg: _realtime.update({leg: LegRealtime(board_cancelled=True)})) == (
        "leg_cancelled", True)
    assert judged(lambda leg: _call("RemovedJourney:1617_2026-10-28", LIER, 4, dep=_t(7, 17),
                                    situation=removed, et_declared_cancelled=True,
                                    has_situation=False)) == ("leg_removed", True)
    assert judged(lambda leg: (
        _call(leg.refs[0][1], LIER, 4, dep=_t(7, 17), p_impact_dep=0.1),
        _call(leg.refs[0][1], DRAMMEN, 6, arr=_t(7, 24), et_declared_cancelled=True),
    )) == ("leg_cancelled", False)


# ---- Continuations of one first train (KTD19, KTD9) ----------------------------------------------
# A first train from A (07:00) continues two ways: change at B (07:30 arrival) to a train reaching D
# at 08:00, or stay on to C (07:40) and change there to reach D at 08:06 -- or at 08:12.


FIRST = "DatedServiceJourney:100_A"


def _first_train(*, board=None, alight_b=None, alight_c=None):
    healthy = {"p_arr_ge5": 0.1, "p_arr_ge10": 0.05}
    _call(FIRST, LIER, 1, dep=_t(7), **{"p_impact_dep": 0.1, **(board or {})})
    _call(FIRST, B, 4, arr=_t(7, 30), dep=_t(7, 31), **{**healthy, **(alight_b or {})})
    _call(FIRST, C, 6, arr=_t(7, 40), **{**healthy, **(alight_c or {})})


def _via(stop, arrival, connecting_jkey, dep, arr, *, connecting=None, known=True):
    """One continuation of the first train: change at `stop` to `connecting_jkey`, scored at both
    its calls -- or, with `known=False`, at its boarding only, so that leg is unknown (KTD8)."""
    first = _leg(FIRST, LIER, stop, _t(7), arrival)
    second = _leg(connecting_jkey, stop, D, dep, arr)
    _call(connecting_jkey, stop, 1, dep=dep, **{"p_impact_dep": 0.1, **(connecting or {})})
    if known:
        _call(connecting_jkey, D, 9, arr=arr)
    return _trip(first, second)


def _ae11(*, c_arrives=None, b_miss=0.9, c_miss=0.1, b_connecting=None, c_name=None):
    c_arrives = c_arrives or _t(8, 6)
    _reset()
    _first_train(alight_b={"p_arr_ge5": b_miss, "p_arr_ge10": b_miss},
                 alight_c={"p_arr_ge5": c_miss, "p_arr_ge10": c_miss, "stop_name": c_name})
    via_b = _via(B, _t(7, 30), "DatedServiceJourney:200_B", _t(7, 36), _t(8),
                 connecting=b_connecting)
    via_c = _via(C, _t(7, 40), "DatedServiceJourney:300_C", _t(7, 46), c_arrives)
    return _judge(DayPlan(trips=(via_c, via_b)), to_stop=D)


def test_two_continuations_of_one_first_train_are_one_subject_on_the_earliest_arrival():
    [verdict] = _ae11(b_miss=0.1)
    assert len(verdict.continuations) == 2
    assert verdict.trip.legs[0].alight_stop_place == B, "judged on the earliest-arriving route"
    assert verdict.facts.expected_arrival == _t(8), "the planned route's arrival while it holds"


def test_ae11_a_likely_miss_at_b_beside_a_healthy_continuation_via_c_does_not_alert():
    [verdict] = _ae11()
    assert not verdict.alertworthy and verdict.facts.problem is None
    assert verdict.facts.expected_arrival == _t(8, 6), "after the miss: the next sibling's arrival"


def test_ae11_a_continuation_ten_or_more_minutes_later_does_not_cushion():
    for arrives in (_t(8, 10), _t(8, 12)):
        [verdict] = _ae11(c_arrives=arrives)
        assert verdict.alertworthy, arrives
        assert (verdict.facts.problem.kind, verdict.facts.problem.index) == ("missed_connection", 0)
        assert round(verdict.deciding.probability, 4) == 0.8226
        assert verdict.facts.expected_arrival == arrives


def test_ae11_with_both_changes_in_trouble_the_figure_recorded_is_the_one_clearing_by_least():
    [verdict] = _ae11(c_miss=0.8)
    assert verdict.alertworthy and not verdict.confirmed
    # The problem names the planned route's change; the figure and bar are C's, the binding one.
    assert (verdict.facts.problem.kind, verdict.facts.problem.index) == ("missed_connection", 0)
    figures = (verdict.deciding.probability, verdict.facts.problem.probability)
    assert tuple(round(f, 4) for f in figures) == (0.6921, 0.6921), figures
    assert verdict.facts.expected_arrival is None, "no continuation holds"


def test_arrives_via_says_where_a_holding_cushion_leaves_the_planned_route_and_nothing_else():
    """`TripFacts.arrives_via` (U10's «fram HH:MM via Asker»): set exactly when a KTD19 cushion
    supplies the expected arrival -- the stop the same first train stays on to (AE11), or the
    change a later connection leaves from -- and null on the planned route, however late it is,
    and when the continuation that gets there arrives 10 or more minutes later (no cushion)."""
    [verdict] = _ae11(c_name="Asker")
    assert (verdict.facts.arrives_via, verdict.facts.expected_arrival) == ("Asker", _t(8, 6))
    [verdict] = _ae11()
    assert verdict.facts.arrives_via == C, "no name on our rows there: the NSR ref"
    [verdict] = _ae11(b_miss=0.1)
    assert verdict.facts.arrives_via is None, "the planned route holds"
    [verdict] = _ae11(c_arrives=_t(8, 12))
    assert verdict.alertworthy and verdict.facts.arrives_via is None, "no cushion: KTD9 only"

    _reset()
    _first_train(alight_b={"p_arr_ge5": 0.99, "p_arr_ge10": 0.1, "stop_name": "Drammen"})
    planned = _via(B, _t(7, 30), "DatedServiceJourney:200_B", _t(7, 36), _t(8))
    later = _via(B, _t(7, 30), "DatedServiceJourney:202_B", _t(7, 45), _t(8, 7))
    _call("DatedServiceJourney:202_B", B, 1, dep=_t(7, 45), stop_name="Drammen")
    [verdict] = _judge(DayPlan(trips=(planned, later)), to_stop=D)
    assert verdict.facts.arrives_via == "Drammen", "a later connection leaves at the same change"

    _reset()
    trip = _lier_trip(connecting_scored=True, arr_ge5=0.1, arr_ge10=0.05, board={"p_impact_dep": 0.1})
    _call(trip.legs[1].refs[0][1], OSLO_S, 9, arr=_t(8, 5), arr_delay_p50_stack_min=7)
    [verdict] = _judge(DayPlan(trips=(trip,)))
    assert verdict.facts.expected_arrival == _t(8, 12) and verdict.facts.arrives_via is None, (
        "a plain delay on the planned route"
    )


def test_the_decision_log_gets_every_continuation_and_the_planned_routes_own_p_miss():
    """Multi-leg U11 (KTD16): the labeller's arrival delay covers every continuation the verdict was
    taken over, so the snapshot holds them all, planned first and marked, each leg by the cache's
    key form -- `leg_ref`'s, the jkey the decision row carries -- with its stops, aimed times and
    walk. P(miss) is the planned route's own KTD7 figure, whatever the deciding reason: here the
    sibling via C binds nothing and B's 0.8226 is still recorded. A JP-expected miss is no figure of
    ours, so it records none."""
    [verdict] = _ae11()
    assert round(verdict.p_miss, 4) == 0.8226
    snapshot = trips.legs_snapshot(verdict.continuations)
    assert [c["planned"] for c in snapshot["continuations"]] == [True, False]
    planned, sibling = (c["legs"] for c in snapshot["continuations"])
    assert [leg["alight_stop_place"] for leg in planned] == [B, D]
    assert [leg["board_stop_place"] for leg in sibling] == [LIER, C]
    assert planned[0] == {
        "operator": "VYG", "jkey": FIRST, "board_stop_place": LIER, "alight_stop_place": B,
        "aimed_departure": _t(7).isoformat(), "aimed_arrival": _t(7, 30).isoformat(),
        "walk_after_s": 0,
    }
    bus = _leg("DatedServiceJourney:x", LIER, B, _t(7), _t(7, 30), operator="RUT", mode="bus",
               plain="ServiceJourney:b9")
    [only] = trips.legs_snapshot([_trip(bus, _leg(FIRST, B, D, _t(7, 36), _t(8)))])["continuations"]
    assert only["legs"][0]["jkey"] == "ServiceJourney:b9", "a bus keys on its plain ref, as the cache"

    _reset()
    first = _leg("ServiceJourney:b1", ASKER, DRAMMEN, _t(7, 5), _t(7, 24), operator="RUT",
                 mode="bus", walk=120)
    second = _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5))
    _realtime[first] = LegRealtime(expected_arrival=_t(7, 29))
    [jp] = _judge(DayPlan(trips=(_trip(first, second),)), to_stop=OSLO_S)
    assert jp.deciding.kind == "missed_connection" and jp.p_miss is None


def test_each_leg_says_what_it_was_judged_from():
    """`TripLeg.data_source` (U10's «mangler sanntid» per leg): `prediction` where our cache scores
    both its ends, `jp_realtime` where only the Journey Planner answers, `none` otherwise -- and
    null on a leg no verdict judged."""
    _reset()
    bus = _leg("ServiceJourney:b1", LIER, DRAMMEN, _t(7, 17), _t(7, 24), operator="RUT", mode="bus")
    train = _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5))
    _call(train.refs[0][1], DRAMMEN, 1, dep=_t(7, 30), p_impact_dep=0.1)
    _call(train.refs[0][1], OSLO_S, 9, arr=_t(8, 5))
    plan = DayPlan(trips=(_trip(bus, train),))
    [verdict] = _judge(plan)
    assert [leg.data_source for leg in verdict.facts.legs] == ["none", "prediction"]
    _realtime[bus] = LegRealtime(expected_departure=_t(7, 17), expected_arrival=_t(7, 24))
    [verdict] = _judge(plan)
    assert [leg.data_source for leg in verdict.facts.legs] == ["jp_realtime", "prediction"]
    assert trips.TripLeg(mode="rail").data_source is None


def test_a_leg_we_do_not_score_is_named_from_the_plan():
    """Our rows name the stops of a leg we score; a bus leg the cache does not hold has no row, so
    the plan's own JP names stand in. Without them the card, the strip and `payload.trip` showed a
    trip with a change through stops with no name."""
    _reset()
    bus = _leg("ServiceJourney:b1", LIER, DRAMMEN, _t(7, 17), _t(7, 24), operator="RUT",
               mode="bus").model_copy(update={"board_name": "Lier", "alight_name": "Drammen"})
    train = _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5)).model_copy(
        update={"board_name": "Drammen (JP)", "alight_name": "Oslo S (JP)"})
    _call(train.refs[0][1], DRAMMEN, 1, dep=_t(7, 30), p_impact_dep=0.1, stop_name="Drammen stasjon")
    _call(train.refs[0][1], OSLO_S, 9, arr=_t(8, 5))
    [verdict] = _judge(DayPlan(trips=(_trip(bus, train),)))
    first, second = verdict.facts.legs
    assert (first.from_name, first.to_name) == ("Lier", "Drammen")
    assert verdict.facts.transfers[0].stop_name == "Drammen"
    assert second.from_name == "Drammen stasjon", "a scored row's name still wins"
    assert second.to_name == "Oslo S (JP)", "the row there has no name, so the plan's stands in"


def test_a_late_departure_of_the_shared_first_train_is_never_cushioned():
    _reset()
    _first_train(board={"p_impact_dep": 0.9})
    via_b = _via(B, _t(7, 30), "DatedServiceJourney:200_B", _t(7, 36), _t(8))
    via_c = _via(C, _t(7, 40), "DatedServiceJourney:300_C", _t(7, 46), _t(8, 6))
    [verdict] = _judge(DayPlan(trips=(via_b, via_c)), to_stop=D)
    assert verdict.alertworthy and (verdict.facts.problem.kind, verdict.facts.problem.index) == (
        "departure", 0
    )
    assert verdict.deciding.probability == 0.9


def test_a_cancelled_second_leg_beside_a_healthy_continuation_does_not_alert():
    [verdict] = _ae11(b_miss=0.1, b_connecting={"et_declared_cancelled": True})
    assert not verdict.alertworthy


def test_a_sibling_nothing_judges_or_jp_no_longer_returns_never_cushions_a_cancellation():
    """KTD8 under KTD19: a continuation cushions only when it is judged to hold. One with a leg of its
    own nothing can judge (unknown is no reading, never on time) or one JP no longer returns
    (`dropped`) leaves the planned route's declared cancellation alerting: predicted, since the
    sibling may still get there, on the planned reason's own figure, and with no arrival supplied
    by the sibling. AE11's known healthy sibling still cushions."""
    def judged(*, known=True, dropped=False):
        _reset()
        _first_train()
        planned = _via(B, _t(7, 30), "DatedServiceJourney:200_B", _t(7, 36), _t(8),
                       connecting={"et_declared_cancelled": True})
        via_c = _via(C, _t(7, 40), "DatedServiceJourney:300_C", _t(7, 46), _t(8, 6), known=known)
        [verdict] = _judge(DayPlan(trips=(planned, via_c.model_copy(update={"dropped": dropped}))),
                           to_stop=D)
        return verdict

    healthy = judged()
    assert not healthy.alertworthy and healthy.complete
    assert healthy.facts.expected_arrival == _t(8, 6)
    for verdict, complete in ((judged(known=False), False), (judged(dropped=True), True)):
        assert verdict.alertworthy and not verdict.confirmed and verdict.complete is complete
        assert (verdict.facts.problem.kind, verdict.facts.problem.index) == ("leg_cancelled", 1)
        assert verdict.deciding.probability == 1.0, "the planned reason binds, not the sibling's"
        assert verdict.facts.expected_arrival is None and verdict.facts.arrives_via is None


def test_with_every_continuation_cancelled_the_subject_is_confirmed():
    _reset()
    _first_train()
    via_b = _via(B, _t(7, 30), "DatedServiceJourney:200_B", _t(7, 36), _t(8),
                 connecting={"et_declared_cancelled": True})
    via_c = _via(C, _t(7, 40), "DatedServiceJourney:300_C", _t(7, 46), _t(8, 6),
                 connecting={"et_declared_cancelled": True})
    [verdict] = _judge(DayPlan(trips=(via_b, via_c)), to_stop=D)
    assert verdict.alertworthy and verdict.confirmed
    assert (verdict.facts.problem.kind, verdict.facts.problem.index) == ("leg_cancelled", 1)


def test_a_later_connection_at_the_same_stop_cushions_like_another_transfer_stop():
    _reset()
    _first_train(alight_b={"p_arr_ge5": 0.99, "p_arr_ge10": 0.1})
    planned = _via(B, _t(7, 30), "DatedServiceJourney:200_B", _t(7, 36), _t(8))
    later = _via(B, _t(7, 30), "DatedServiceJourney:202_B", _t(7, 45), _t(8, 7))
    [verdict] = _judge(DayPlan(trips=(planned, later)), to_stop=D)
    assert not verdict.alertworthy, "the 07:45 at B still gets there within 10 minutes"
    assert verdict.facts.expected_arrival == _t(8, 7)


def test_a_cancelled_call_of_the_first_train_at_b_puts_every_continuation_at_b_in_trouble():
    def judged(*, via_c):
        _reset()
        _first_train(alight_b={"et_declared_cancelled": True})
        trips_ = [
            _via(B, _t(7, 30), "DatedServiceJourney:200_B", _t(7, 36), _t(8)),
            _via(B, _t(7, 30), "DatedServiceJourney:202_B", _t(7, 45), _t(8, 7)),
        ]
        if via_c:
            trips_.append(_via(C, _t(7, 40), "DatedServiceJourney:300_C", _t(7, 46), _t(8, 6)))
        return _judge(DayPlan(trips=tuple(trips_)), to_stop=D)

    [only_b] = judged(via_c=False)
    assert only_b.alertworthy and only_b.confirmed, "a later connection at B never cushions"
    assert (only_b.facts.problem.kind, only_b.facts.problem.index) == ("leg_cancelled", 0)
    [with_c] = judged(via_c=True)
    assert not with_c.alertworthy, "staying on to C still does"


# ---- Window, catchability, the plan's own marks --------------------------------------------------


def test_only_departures_inside_the_inclusive_window_and_still_catchable_are_judged():
    _reset()
    plan = DayPlan(trips=(_lier_trip(),))
    assert len(_judge(plan, start=_t(7, 17), end=_t(7, 17))) == 1
    assert _judge(plan, start=_t(7, 18), end=_t(8)) == []
    assert len(_judge(plan, catchable_after=_t(7, 17))) == 1
    assert _judge(plan, catchable_after=_t(7, 18)) == [], "R10: only before the first leg departs"


def test_a_dropped_trip_is_still_judged_until_its_first_leg_departs():
    _reset()
    trip = _lier_trip().model_copy(update={"dropped": True})
    [verdict] = _judge(DayPlan(trips=(trip,)))
    assert verdict.alertworthy, "AE10: a refresh that no longer returns it withdraws nothing"


# ---- Module boundary (KTD4) ----------------------------------------------------------------------


def test_transfer_coverage_says_whether_the_trips_with_a_change_were_judged():
    """KTD13's `coverage.transfers`: `evaluated` with a watched trip in a covered range, `none` with
    no trip with a change there, `not_evaluated` otherwise -- and only a MISSING plan (none, or a
    range outside it) is the flavour a fetch can still answer. Conservative across ranges, so a pair
    served only with a change is never reported calm (AE5); `not_evaluated` with the flag off (R16)."""
    import os

    def leg(jkey, board, alight, dep, arr, operator="VYG"):
        return _leg(jkey, board, alight, dep, arr, operator=operator,
                    mode="rail" if operator == "VYG" else "bus")

    covered = (trips.CoveredRange(start=_t(7), end=_t(9), fetched_at=_t(6)),)
    rail = _trip(leg("1617_X", LIER, DRAMMEN, _t(7, 17), _t(7, 24)),
                 leg("1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5)))
    bus = _trip(leg("b1", LIER, B, _t(7, 20), _t(7, 40), "RUT"), leg("b2", B, OSLO_S, _t(7, 45), _t(8), "RUT"))
    watched, unwatched, empty = (DayPlan(trips=t, covered=covered) for t in ((rail,), (bus,), ()))
    window = (_t(7), _t(8))
    before = os.environ.get("TRIP_PLANS_ENABLED")
    os.environ["TRIP_PLANS_ENABLED"] = "true"
    try:
        coverage = trips.transfer_coverage
        assert coverage([(watched, *window)]) == ("evaluated", False)
        assert coverage([(empty, *window)]) == ("none", False)
        assert coverage([(unwatched, *window)]) == ("not_evaluated", False), "R3: not ours to judge"
        assert coverage([(None, *window)]) == ("not_evaluated", True)
        assert coverage([(watched, _t(8), _t(10))]) == ("not_evaluated", True), "outside its ranges"
        assert coverage([(watched, *window), (empty, *window)]) == ("evaluated", False)
        assert coverage([(watched, *window), (unwatched, *window)]) == ("not_evaluated", False)
        assert coverage([(empty, *window), (None, *window)]) == ("not_evaluated", True)
        assert coverage([(watched, _t(8, 30), _t(9))]) == ("none", False), "only the range's trips"
        assert coverage([]) == ("none", False)
        os.environ["TRIP_PLANS_ENABLED"] = "false"
        assert coverage([(watched, *window)]) == ("not_evaluated", False)
        assert coverage([(None, *window)]) == ("not_evaluated", False), "off: nothing to fetch"
    finally:
        os.environ.pop("TRIP_PLANS_ENABLED", None)
        if before is not None:
            os.environ["TRIP_PLANS_ENABLED"] = before


def test_the_module_imports_neither_impact_nor_trip_plans():
    tree = ast.parse(Path(trips.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    local = names - set(sys.stdlib_module_names)
    assert local == {"alert_policy", "alternatives", "notification_wording", "pydantic"}, local


def test_situation_texts_walks_a_plan_once_across_calls_and_answers_as_a_fresh_walk():
    """main asks twice a cycle, the second time for a subset: a plan the previous call was given is
    not walked again, and every answer is exactly a fresh walk's, keys in walk order."""
    def plan(*jkeys):
        return DayPlan(trips=tuple(
            _trip(_leg(jkey, LIER, DRAMMEN, _t(7, 17), _t(7, 24)),
                  _leg("DatedServiceJourney:1230_X", DRAMMEN, OSLO_S, _t(7, 30), _t(8, 5)))
            for jkey in jkeys
        ))

    original = trips.trip_situation

    def walk(plans):
        return dict.fromkeys(
            (original(leg) for p in plans for trip in p.trips for leg in trip.legs),
            notification_wording.TRIP_SITUATION_TEXTS,
        )

    held, public = plan("DatedServiceJourney:1617_X"), plan("DatedServiceJourney:1619_X")
    walked: list = []
    trips.trip_situation = lambda leg: walked.append(leg) or original(leg)
    try:
        for plans, legs_walked in (
            ([held, public], 4),
            ([held], 0),  # the subset: walked by the call before
            ([held, public], 2),  # only the plans of the last call are kept
            ([plan("DatedServiceJourney:1617_X")], 2),  # a new object, however equal
        ):
            walked.clear()
            texts = trips.situation_texts(plans)
            assert list(texts.items()) == list(walk(plans).items()), plans
            assert len(walked) == legs_walked, (plans, len(walked))
    finally:
        trips.trip_situation = original


def test_the_reason_vocabulary_is_the_published_one():
    from typing import get_args, get_type_hints

    kind = get_type_hints(trips.Reason)["kind"]  # the verdict's own reason vocabulary
    assert get_args(kind) == (
        "departure", "leg_cancelled", "leg_departure", "missed_connection", "leg_removed"
    )
    assert get_args(kind) == get_args(trips.TripProblem.model_fields["kind"].annotation)


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} trip verdict checks passed.")


if __name__ == "__main__":
    _run()
