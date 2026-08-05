"""Matcher checks for varsel.match_journey — synthetic _CallPred-shaped rows.

Runs standalone (`uv run --with fastapi --with pydantic python test_varsel.py`) or under pytest.
No BigQuery, no network: the BigQuery client is stubbed before main is imported, and the impact
provider is main's real one driven over an injected synthetic snapshot — so these exercise the
actual /impact matcher plus varsel's policy on top of it, end to end.

Covers direction, window edges, the weekday filter, overnight rejection, the departed-train filter
(no alert once the expected departure has passed), the alert-worthiness gate, the per-operator
thresholds, and the notification wording.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import impact
import main
import varsel
from varsel import (
    _OSLO,
    Alert,
    _deterministic_body,
    _message_input,
    _message_input_from_alert,
    _notification_payload,
)


@dataclass
class FakeCall:
    """Duck-types impact.CallPred for the fields the matcher reads."""

    jkey: str
    stop_place: str
    stop_order: int
    aimed_dep: datetime | None
    stop_name: str | None = None
    situation_number: str = "S1"
    operator: str = "VYG"
    p_cancel: float = 0.5
    p_impact_dep: float | None = 0.9
    p_dep_ge10: float | None = 0.8
    line: str | None = "VYG:Line:R10"
    summary: str | None = "Signalfeil"
    description: str | None = None
    dep_delay_typ_min: int | None = 4
    dep_delay_p90_min: int | None = 15
    dep_delay_p50_stack_min: int | None = 3
    dep_delay_p90_stack_min: int | None = 12
    # Observed state. Defaults describe an upcoming call with nothing observed yet, so existing
    # cases keep their meaning and a test opts in to an observation explicitly.
    et_declared_cancelled: bool | None = False
    et_dep_delay_min: int | None = None
    call_source: str | None = "estimated"
    # Arrival side -- unused by the pre-U2 matcher, needed once /impact builds cells from these.
    aimed_arr: datetime | None = None
    arr_delay_typ_min: int | None = None
    arr_delay_p90_min: int | None = None
    arr_delay_p50_stack_min: int | None = None
    arr_delay_p90_stack_min: int | None = None
    p_arr_ge10: float | None = None
    p_impact: float | None = None


def _journey(begin="07:00", end="09:00", days=(0, 1, 2, 3, 4)):
    return {
        "device_id": "dev-123456",
        "from_stop": "A",
        "from_name": "Oslo S",
        "to_stop": "B",
        "to_name": "Lillehammer",
        "begin": begin,
        "end": end,
        "days": list(days),
        "created_ts": "2026-07-06T00:00:00+00:00",
    }


def _oslo(y, mo, d, h, mi):
    """A tz-aware instant at the given Europe/Oslo wall-clock time (as BigQuery TIMESTAMPs arrive
    UTC-aware; the matcher converts to Oslo, so building in Oslo tz is equivalent)."""
    return datetime(y, mo, d, h, mi, tzinfo=_OSLO)


# 2026-07-06 is a Monday (weekday 0); 2026-07-11 is a Saturday (weekday 5).
MON_0800 = _oslo(2026, 7, 6, 8, 0)
assert MON_0800.weekday() == 0
SAT_0800 = _oslo(2026, 7, 11, 8, 0)
assert SAT_0800.weekday() == 5

# The tests pin their own "now" (early Monday, before every test departure) — the matcher drops
# boardings whose expected departure has passed, and the fixed test dates lie in the real past.
NOW = _oslo(2026, 7, 6, 6, 0)


# The engine reads its snapshot through an injected provider (main owns the real cache).
_snapshot: dict = {}
impact.configure(snapshot_provider=lambda: (_snapshot, None))


def install(by_situation):
    """Point varsel at main's real /impact provider over this synthetic snapshot.

    `with_alternative=False` keeps the Journey Planner out of the matcher tests — the alternative
    lookup has its own hermetic suite in test_alternatives.py.
    """
    _snapshot.clear(); _snapshot.update(by_situation)

    def provider(*, from_stop, to_stop, window_start, window_end, catchable_after=None,
                 with_alternative=False, min_impact=0.0):
        # Pass the flag THROUGH rather than hard-coding False: hard-coding it made the suite blind to
        # a Journey Planner call reappearing on the request path (review 2026-07-29).
        return impact.for_trip(
            from_stop=from_stop,
            to_stop=to_stop,
            window_start=window_start,
            window_end=window_end,
            select="worst",
            catchable_after=catchable_after,
            with_alternative=with_alternative,
            min_impact=min_impact,
        )

    varsel.configure(impact_provider=provider, stops_provider=list)


def match(journey, by_situation, now=NOW):
    install(by_situation)
    return varsel.match_journey(journey, now)


def test_the_real_provider_signature_matches():
    """Wire main._varsel_impact itself into varsel.configure, not a hand-written double.

    This is the one cross-module contract the /impact rewrite introduced, and on 2026-07-29 it was
    broken for real: varsel._impact passed `with_alternative=` to a provider that did not accept it,
    so every matcher call raised TypeError — swallowed by check_and_notify's per-journey guard, which
    meant ALL push alerting was dead and both journey endpoints 500'd. The suite stayed green because
    `install()` above and test_varsel_api's provider both hand-write a signature that happens to fit.
    Any test that only uses a double cannot catch this; this one must call the real function.
    """
    _snapshot.clear(); _snapshot.update({"S1": _train(MON_0800)})
    varsel.configure(impact_provider=main._varsel_impact, stops_provider=list)
    try:
        # Every keyword varsel._impact actually sends, through the real provider.
        alerts = varsel.match_journey(_journey(), NOW)
        assert alerts, "the real provider must answer the matcher"
        # And the poller's path, which additionally asks for an alternative.
        original = impact.alternatives.find_faster
        impact.alternatives.find_faster = lambda *a, **k: None  # no network
        try:
            assert varsel.match_journey(_journey(), NOW, with_alternative=True)
        finally:
            impact.alternatives.find_faster = original
    finally:
        install({})  # restore the double for the remaining tests


def _train(dep, jkey="t1", board_order=1, dest_order=5, operator="VYG", situation="S1",
           **board_kw):
    """A boarding call at A and a later call at B (same train, 40 min later).

    `situation` goes on both calls: the cache tags every scored call with its own situationNumber
    (that is how main groups them), so a fixture that puts two situations in one scope has to say
    which is which on the calls themselves, not just in the dict key.
    """
    return [
        FakeCall(jkey=jkey, stop_place="A", stop_order=board_order, aimed_dep=dep,
                 operator=operator, situation_number=situation, **board_kw),
        FakeCall(jkey=jkey, stop_place="B", stop_order=dest_order, aimed_dep=None,
                 aimed_arr=dep + timedelta(minutes=40) if dep is not None else None,
                 operator=operator, situation_number=situation),
    ]


def _twin(dep, ref, **board_kw):
    """A train boarding at `ref` (a co-located twin of A) and alighting at B."""
    return [
        FakeCall(jkey="t-twin", stop_place=ref, stop_order=1, aimed_dep=dep, **board_kw),
        FakeCall(jkey="t-twin", stop_place="B", stop_order=5, aimed_dep=None,
                 aimed_arr=dep + timedelta(minutes=40)),
    ]


def test_a_situation_on_a_twin_ref_of_the_boarding_stop_matches():
    """The picker offers one row per physical place, but each feed records its calls on its own ref
    (rail at the station ref, bus at the bus-side ref). If the matcher only asked about the ref that
    won the picker row, merging the rows would hide the other side's traffic — the same silence the
    merge exists to remove."""
    journey = _journey()
    snapshot = {"S1": _twin(MON_0800, "A2", p_cancel=0.8, p_impact_dep=0.9)}
    assert not match(journey, snapshot), "control: an unrelated ref must not match"
    varsel.set_stop_aliases({"A2": "A"})
    try:
        alerts = match(journey, snapshot)
    finally:
        varsel.set_stop_aliases({})
    assert [a.situation_number for a in alerts] == ["S1"]
    assert alerts[0].alertworthy is True


def test_a_journey_left_on_a_collapsed_ref_still_sees_its_own_stop():
    """A registration can sit on a non-primary ref: made before the rewrite existed, or left alone
    by it. Asking about the place must include the ref the traveller actually picked — an earlier
    cut of place_refs filtered that one out, which is precisely the stop their trip calls at."""
    journey = _journey()
    journey["from_stop"] = "A2"
    varsel.set_stop_aliases({"A2": "A"})
    try:
        assert varsel.place_refs("A2") == ("A", "A2")
        alerts = match(journey, {"S1": _twin(MON_0800, "A2", p_cancel=0.8, p_impact_dep=0.9)})
    finally:
        varsel.set_stop_aliases({})
    assert [a.situation_number for a in alerts] == ["S1"]


def test_one_situation_seen_through_two_refs_is_one_alert():
    """Both refs of a place are asked about, so the same situation can come back twice. The worst
    reading is what a traveller needs; two cards for one disruption is what they do not."""
    snapshot = {
        "S1": [
            *_train(MON_0800, jkey="t1", p_cancel=0.30, p_impact_dep=0.55),
            *_twin(MON_0800 + timedelta(minutes=5), "A2", p_cancel=0.80, p_impact_dep=0.95),
        ]
    }
    varsel.set_stop_aliases({"A2": "A"})
    try:
        alerts = match(_journey(), snapshot)
    finally:
        varsel.set_stop_aliases({})
    assert len(alerts) == 1
    assert alerts[0].impact_probability == 0.95, "the worse of the two readings survives"


def test_the_journey_planner_is_asked_once_per_window_not_once_per_ref():
    """The alternative is a property of the trip ("a faster way from here to there"), and the lookup
    is a blocking external call. Asking it per ref pair would multiply it by the number of refs."""
    _snapshot.clear()
    _snapshot.update({"S1": _twin(MON_0800, "A2", p_cancel=0.8, p_impact_dep=0.9)})
    asked = []

    def provider(*, from_stop, to_stop, window_start, window_end, catchable_after=None,
                 with_alternative=False, min_impact=0.0):
        asked.append((from_stop, with_alternative))
        return impact.for_trip(
            from_stop=from_stop, to_stop=to_stop, window_start=window_start,
            window_end=window_end, select="worst", catchable_after=catchable_after,
            with_alternative=False, min_impact=min_impact,
        )

    varsel.configure(impact_provider=provider, stops_provider=list)
    varsel.set_stop_aliases({"A2": "A"})
    try:
        varsel.match_journey(_journey(), NOW, with_alternative=True)
    finally:
        varsel.set_stop_aliases({})
    # Two windows are in scope (today and tomorrow), each asking both refs of the boarding place.
    windows = len(varsel.journey_windows(_journey(), NOW))
    assert [ref for ref, _ in asked] == ["A", "A2"] * windows
    assert [flag for _, flag in asked] == [True, False] * windows, (
        "the lookup runs once per window, on the canonical ref only"
    )


def test_basic_match():
    alerts = match(_journey(), {"S1": _train(MON_0800, p_cancel=0.8, p_impact_dep=0.9)})
    assert len(alerts) == 1
    a = alerts[0]
    assert a.situation_number == "S1"
    assert a.cancel_probability == 0.8
    assert a.delay_p50_min == 3  # exceedance-stack p50, not the dep_delay_typ_min regression (4)
    assert a.delay_p90_min == 12  # prefers the exceedance-stack p90
    assert a.impact_probability == 0.9
    assert a.alert_threshold == 0.65  # VYG -> the default cut
    assert a.alertworthy is True


def test_alert_carries_the_et_join_key():
    """An alert has to name the train it is about, or a recorded decision can never be checked
    against what actually happened.

    The grain is (operator, jkey, call_date, stop_order): rail refs are date-scoped and unique on
    their own, but bus ServiceJourney jkeys repeat every operating day, so the stop and the date
    are not optional extras. `decided_at` is what turns a row into a lead-time observation.
    """
    alerts = match(_journey(), {"S1": _train(MON_0800, jkey="j-77", p_impact_dep=0.9)})
    a = alerts[0]
    assert (a.jkey, a.operator, a.stop_place, a.stop_order) == ("j-77", "VYG", "A", 1)
    assert a.decided_at == NOW
    # The boarding call, not the alighting one — the side the alert fires on.
    assert a.departure_time == MON_0800


def test_direction_reverse_no_match():
    # Same stops but to_stop (B) comes BEFORE from_stop (A) on this train — wrong direction.
    calls = [
        FakeCall("t1", "A", 5, MON_0800),
        FakeCall("t1", "B", 1, None, aimed_arr=MON_0800 - timedelta(minutes=30)),
    ]
    assert match(_journey(), {"S1": calls}) == []


def test_missing_destination_no_match():
    # Boarding in window but the train never calls at B.
    calls = [FakeCall("t1", "A", 1, MON_0800)]
    assert match(_journey(), {"S1": calls}) == []


def test_window_edges():
    j = _journey(begin="07:00", end="09:00")
    assert match(j, {"S1": _train(_oslo(2026, 7, 6, 7, 0))})  # exactly begin -> match
    assert match(j, {"S1": _train(_oslo(2026, 7, 6, 9, 0))})  # exactly end -> match
    assert not match(j, {"S1": _train(_oslo(2026, 7, 6, 6, 59))})  # just before
    assert not match(j, {"S1": _train(_oslo(2026, 7, 6, 9, 1))})  # just after


def test_weekday_filter():
    # `now` sits early on the Saturday so the horizon is not what decides these — the weekday is.
    sat_morning = _oslo(2026, 7, 11, 6, 0)
    weekdays_only = _journey(days=(0, 1, 2, 3, 4))
    assert not match(weekdays_only, {"S1": _train(SAT_0800)}, now=sat_morning)  # Sat excluded
    weekend = _journey(days=(5, 6))
    assert match(weekend, {"S1": _train(SAT_0800)}, now=sat_morning)  # Sat included


def test_only_today_and_tomorrow_are_considered():
    """The prediction cache is built from today's ET partition, so its horizon is roughly the
    operating day. A window several days out is not something we can score yet — and offering
    tomorrow matters because late in the evening that is the next relevant window."""
    saturday_journey = _journey(days=(5,))
    # Monday morning: Saturday's window is five days away -> nothing to say.
    assert match(saturday_journey, {"S1": _train(SAT_0800)}, now=_oslo(2026, 7, 6, 6, 0)) == []
    # Friday evening: Saturday is tomorrow -> the window is offered.
    assert match(saturday_journey, {"S1": _train(SAT_0800)}, now=_oslo(2026, 7, 10, 22, 0))


def test_overnight_rejection():
    # A day-window [07:00, 09:00] must not catch a 23:30 departure (no wrap-around).
    assert not match(_journey(), {"S1": _train(_oslo(2026, 7, 6, 23, 30))})


def test_departed_train_no_match():
    # The situation stays open all day, but the 08:00 train (expected 08:03 with the 3 min
    # typical delay) left long ago — no alert lingering at noon.
    assert match(_journey(), {"S1": _train(MON_0800)}, now=_oslo(2026, 7, 6, 12, 0)) == []
    # Still catchable just before the expected departure.
    assert match(_journey(), {"S1": _train(MON_0800)}, now=_oslo(2026, 7, 6, 8, 2))


def test_departed_worst_train_does_not_suppress_a_catchable_one_same_line():
    """Regression (review 2026-07-29): catchability used to be applied AFTER /impact rolled up to
    the worst train per situation, so a departed worst-case train hid a later, still-catchable,
    alert-worthy train on the same line. The alert must survive and report the catchable train."""
    calls = (
        # 07:30, the most severe train in the window -- but long gone by 09:00.
        _train(_oslo(2026, 7, 6, 7, 30), jkey="gone", p_cancel=0.9, p_impact_dep=0.99)
        # 08:45, less severe but still over the 0.65 threshold, and still catchable.
        + _train(_oslo(2026, 7, 6, 8, 45), jkey="catchable", p_cancel=0.2, p_impact_dep=0.80)
    )
    alerts = match(_journey(begin="07:00", end="09:00"), {"S1": calls},
                   now=_oslo(2026, 7, 6, 8, 40))
    assert len(alerts) == 1, "the situation must not vanish because its worst train departed"
    assert alerts[0].impact_probability == 0.80, "must report the catchable train, not the gone one"
    assert alerts[0].departure_time == _oslo(2026, 7, 6, 8, 45)
    assert alerts[0].alertworthy is True


def test_departed_worst_train_does_not_suppress_a_catchable_one_other_line():
    """Same trap across two lines of one situation: the rollup takes the worst line, so a departed
    train on line A must not hide a catchable alert-worthy train on line B."""
    calls = (
        _train(_oslo(2026, 7, 6, 7, 30), jkey="gone", p_cancel=0.9, p_impact_dep=0.99,
               line="VYG:Line:R10")
        + _train(_oslo(2026, 7, 6, 8, 45), jkey="ok", p_cancel=0.2, p_impact_dep=0.80,
                 line="VYG:Line:R11")
    )
    alerts = match(_journey(begin="07:00", end="09:00"), {"S1": calls},
                   now=_oslo(2026, 7, 6, 8, 40))
    assert len(alerts) == 1
    assert alerts[0].line == "VYG:Line:R11"
    assert alerts[0].impact_probability == 0.80


def test_window_just_ended_but_train_still_catchable():
    """Regression (review 2026-07-29): journey_windows dropped a window the moment its end passed,
    discarding the case the alert exists for — a train inside the window, predicted heavily late, that
    the traveller can still make. Per-train catchability is `catchable_after`'s job, not the window's.
    """
    late = _train(_oslo(2026, 7, 6, 7, 52), dep_delay_p50_stack_min=45, p_impact_dep=0.9)
    j = _journey(begin="07:00", end="08:00")
    # 08:02: window ended two minutes ago, train expected 08:37 -> still catchable, must alert.
    assert match(j, {"S1": late}, now=_oslo(2026, 7, 6, 8, 2))
    # 08:40: expected departure has passed -> correctly silent.
    assert match(j, {"S1": late}, now=_oslo(2026, 7, 6, 8, 40)) == []


def test_delayed_train_catchable_until_expected_departure():
    # 08:00 aimed + 25 min predicted typical delay -> still listed at 08:15, gone at 08:26.
    late = {"S1": _train(MON_0800, dep_delay_p50_stack_min=25)}
    assert match(_journey(), late, now=_oslo(2026, 7, 6, 8, 15))
    assert match(_journey(), late, now=_oslo(2026, 7, 6, 8, 26)) == []


def test_expected_departure_falls_back_to_typ_delay():
    # No calibrated stack p50 -> the expected departure uses dep_delay_typ_min (4 min default).
    train = {"S1": _train(MON_0800, dep_delay_p50_stack_min=None)}
    assert match(_journey(), train, now=_oslo(2026, 7, 6, 8, 3))
    assert match(_journey(), train, now=_oslo(2026, 7, 6, 8, 5)) == []


def test_alertworthy_gate():
    # Below the impact threshold -> listed but not push-worthy.
    mild = match(_journey(), {"S1": _train(MON_0800, p_cancel=0.1, p_impact_dep=0.2)})
    assert mild and mild[0].alertworthy is False
    # No impact prob -> never pushed, exactly as /demo's would_alert (no p_cancel fallback), even
    # with a high cancellation probability.
    no_impact = match(_journey(), {"S1": _train(MON_0800, p_cancel=0.7, p_impact_dep=None)})
    assert no_impact and no_impact[0].alertworthy is False


def test_per_operator_threshold():
    # RUT's tuned threshold is 0.75 (stricter than the 0.65 default), TRO's is 0.55 (looser):
    # the same 0.70 impact prob is push-worthy for TRO but not for RUT.
    rut = match(_journey(), {"S1": _train(MON_0800, operator="RUT", p_impact_dep=0.70)})
    assert rut and rut[0].alertworthy is False
    tro = match(_journey(), {"S1": _train(MON_0800, operator="TRO", p_impact_dep=0.70)})
    assert tro and tro[0].alertworthy is True


def test_worst_train_wins():
    # Two trains in the window; the alert reports the one with the higher impact probability — the
    # score the push gate reads — even though the other looks worse on cancellation probability.
    calls = _train(
        _oslo(2026, 7, 6, 7, 30), jkey="t1", p_cancel=0.55, p_impact_dep=0.6
    ) + _train(_oslo(2026, 7, 6, 8, 30), jkey="t2", p_cancel=0.05, p_impact_dep=0.95)
    alerts = match(_journey(), {"S1": calls})
    assert len(alerts) == 1
    assert alerts[0].impact_probability == 0.95
    assert alerts[0].alertworthy is True  # would have been False if t1 supplied the numbers


def test_multiple_situations_sorted_worst_first():
    s1 = _train(MON_0800, jkey="a", situation="S1", p_impact_dep=0.4)
    s2 = _train(MON_0800, jkey="b", situation="S2", p_impact_dep=0.9)
    alerts = match(_journey(), {"S1": s1, "S2": s2})
    assert [a.situation_number for a in alerts] == ["S2", "S1"]


def test_operator_collision_not_merged():
    # Two operators share the bare jkey "x" within one situation (bus ServiceJourney refs are not
    # date-scoped). SKY's train travels A->B (matches); ATB's "x" only boards at A with no later B
    # call (wrong direction). Keying on jkey alone would merge the two groups and let ATB's boarding
    # ride SKY's direction. Operator-aware grouping keeps them separate -> exactly one match (SKY).
    sky = _train(MON_0800, jkey="x", operator="SKY", p_cancel=0.8)
    atb_board_only = [
        FakeCall(jkey="x", stop_place="A", stop_order=1, aimed_dep=MON_0800, operator="ATB",
                 p_cancel=0.99),
    ]
    alerts = match(_journey(), {"S1": sky + atb_board_only})
    assert len(alerts) == 1
    assert alerts[0].cancel_probability == 0.8  # SKY's train, not ATB's boarding-only 0.99


def _push_body(**board_kw) -> str:
    """The Web Push body for the single match produced by a train with these boarding numbers.

    Through the real message input, not the alert directly: the deterministic wording is assembled
    from the same document the model is prompted with, so these checks have to run over it.
    """
    journey = _journey()
    alerts = match(journey, {"S1": _train(MON_0800, **board_kw)})
    assert len(alerts) == 1
    message_input = _message_input(journey, alerts[0])
    assert message_input is not None, "the wording's source must actually be buildable"
    return _notification_payload(journey, alerts[0], message_input=message_input)["body"]


def test_push_body_leads_with_the_alerting_score():
    # The real regression (prd, 2026-07-27): a 93 % impact alert on a train with a 1 % cancellation
    # probability read "1 % sjanse for innstilling, typisk 2 min forsinket" — the two numbers that
    # matter least. The body must quote the impact probability and the exceedance-stack interval.
    body = _push_body(
        p_cancel=0.01,
        p_impact_dep=0.93,
        dep_delay_typ_min=2,
        dep_delay_p50_stack_min=15,
        dep_delay_p90_stack_min=27,
    )
    assert "Typisk 15–27 min forsinket" in body
    assert "1 %" not in body and "2 min" not in body


def test_push_body_leads_with_cancellation_when_that_dominates():
    # Bus alerts are mostly cancellation-driven (p_cancel .75-.95): a small delay interval would be
    # beside the point for a train that probably isn't running.
    body = _push_body(operator="SKY", p_cancel=0.95, p_impact_dep=0.97,
                      dep_delay_p50_stack_min=3, dep_delay_p90_stack_min=8)
    assert "95 % sjanse for innstilling" in body
    assert "min forsinket" not in body


def test_push_body_collapses_a_single_delay_figure():
    body = _push_body(p_cancel=0.02, dep_delay_p50_stack_min=12, dep_delay_p90_stack_min=12)
    assert "Typisk 12 min forsinket" in body


def test_push_body_drops_the_detail_when_neither_driver_is_known():
    """A pushed alert can have no driver figure at all, and the body must not say "None".

    The gate reads impact_probability off the exceedance head, not off these columns, so p_cancel
    under _CANCEL_LIKELY with every delay figure NULL is a reachable combination -- and it used to
    f-string a Python None straight into the notification ("Tog kl 08:00: None").
    """
    body = _push_body(
        p_cancel=0.02,
        p_impact_dep=0.93,  # still over the alert bar: this is a pushed alert, not a listed one
        dep_delay_typ_min=None,  # _typ_delay falls back to this, so it has to go too
        dep_delay_p50_stack_min=None,
        dep_delay_p90_stack_min=None,
        dep_delay_p90_min=None,
    )
    assert "None" not in body
    assert "sjanse for innstilling" not in body and "forsinket" not in body
    # What survives is the summary and the train, so the push still says what and when.
    assert body == "Signalfeil\nTog kl 08:00"


def test_push_body_states_a_declared_cancellation_instead_of_scoring_it():
    """The 2026-08-05 report. ET had already cancelled the 15:03 from Asker, and the push (and the
    card wording it) still said "91 % sjanse for innstilling" for a train the journey planner had
    stopped offering -- so the traveller went looking for a departure that did not exist."""
    body = _push_body(et_declared_cancelled=True, p_cancel=0.57, p_impact_dep=0.61)
    assert body == "Signalfeil\nTog kl 08:00: Innstilt"
    assert "%" not in body and "forsinket" not in body


def test_the_wording_falls_back_to_the_certainty_when_the_message_input_cannot_be_built():
    """`_message_input_from_alert` is the last-resort wording source, so it has to carry the same
    distinction -- otherwise an llm_message failure turns a cancellation back into a percentage."""
    alert = Alert(
        situation_number="S1", summary="Signalfeil", line="VYG:Line:R10",
        departure_time=MON_0800, cancelled=True, cancel_probability=1.0,
        delay_p50_min=None, delay_p90_min=None, alert_threshold=0.65, alertworthy=True,
    )
    body = _deterministic_body(_message_input_from_alert(alert))
    assert body == "Signalfeil\nTog kl 08:00: Innstilt"


def test_push_body_omits_a_missing_summary():
    """`summary` is nullable (an SX situation need not carry Norwegian text) -- the same
    unguarded interpolation rendered it as "None" on the body's first line."""
    body = _push_body(summary=None, p_cancel=0.95)
    assert "None" not in body
    assert body == "Tog kl 08:00: 95 % sjanse for innstilling"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} matcher checks passed.")
