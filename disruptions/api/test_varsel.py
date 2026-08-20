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
from datetime import UTC, datetime, timedelta

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import alternatives
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
    # Whether an SX message named this call. Defaults True, matching CallPred: every case written
    # before the no-SX population existed is about a train a situation pointed at.
    has_situation: bool = True


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
        original = impact.alternatives.fetch_candidates
        impact.alternatives.fetch_candidates = lambda *a, **k: []  # no network
        try:
            assert varsel.match_journey(_journey(), NOW, with_alternative=True)
        finally:
            impact.alternatives.fetch_candidates = original
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


def _twin(dep, ref, jkey="t-twin", **board_kw):
    """A train boarding at `ref` (a co-located twin of A) and alighting at B."""
    return [
        FakeCall(jkey=jkey, stop_place=ref, stop_order=1, aimed_dep=dep, **board_kw),
        FakeCall(jkey=jkey, stop_place="B", stop_order=5, aimed_dep=None,
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
    """A lookup is a blocking external call, and asking it per ref pair would multiply it by the
    number of refs a place collapses. Which ref of one place the question names does not change the
    answer — the trains and the deadlines are the same — so the extra passes buy nothing. (Which
    TRAIN it is about does change it; that is `_alternatives_by_situation`'s axis, not this one.)"""
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


def _live_candidate(now):
    """A Journey Planner reply good enough to be a real pool, for tests about verdicts not offers."""
    return alternatives.AlternativeCandidate(
        departure_time=now + timedelta(minutes=15),
        arrival_time=now + timedelta(minutes=55),
        transit_refs=[("VYG", "DatedServiceJourney:other")],
        lines=["VYG:Line:R11"],
        board_stop_place="A",
        alight_stop_place="B",
    )


def _subject_jkey(original):
    """Which train a `select_best` call is deciding for, off the synthesized original candidate."""
    return original.transit_refs[0][1] if original and original.transit_refs else None


def test_a_stay_put_verdict_reaches_only_the_train_it_is_about():
    """`None` from the lookup used to mean "nothing was faster" — a fact about the TRIP, safe to
    back-fill onto every alert. It now also means "the traveller's own train is still the best
    option", which is a claim about ONE train.

    Since 2026-08-12 each train is looked up for itself, so the risk this guards has moved: it is no
    longer that one lookup is shared, but that the three per-train fields are projected onto the
    wrong alert on the way out (`_collect_alerts`'s model_copy, `match_journey`'s back-fill). The
    three alerts below are deliberately in three different states, and telling them apart is the
    whole point — a verdict, a lookup that found nothing, and no lookup at all.
    """
    live = datetime.now(UTC)
    snapshot = {
        # Canonical ref (A), and the worst departure on it: the verdict is about this train.
        "S1": _train(MON_0800, jkey="subject", p_impact_dep=0.90),
        # Canonical ref too, milder — same pass, same response object, different train.
        "S2": _train(MON_0800, jkey="bystander", situation="S2", p_impact_dep=0.70),
        # Only the collapsed ref records this one, so it is asked about in its own pass, without
        # the lookup. It is also the worst of the three.
        "S3": _twin(MON_0800, "A2", situation_number="S3", p_impact_dep=0.95),
    }
    install(snapshot)
    varsel.set_stop_aliases({"A2": "A"})
    fetch, select = alternatives.fetch_candidates, alternatives.select_best

    def stay_put_for_the_subject_only(*a, original=None, outcome=None, **k):
        """Stay-put for `subject`; for anyone else, a pool that yielded nothing.

        The verdict rides on `select_best`'s out-parameter, not on its `None` return: the two are
        different answers ("we chose your train" vs "nothing was established") and the caller must
        never infer the first from the second. Answering per train is what makes this test able to
        tell a projected verdict from an inherited one.
        """
        if outcome is not None:
            outcome["original_is_best"] = _subject_jkey(original) == "subject"

    # A real pool, and a selection that answers "stay put" over it — which is what makes the verdict
    # a verdict rather than the silence of an empty Journey Planner reply.
    alternatives.fetch_candidates = lambda *a, **k: [_live_candidate(live)]
    alternatives.select_best = stay_put_for_the_subject_only
    try:
        alerts = varsel.match_journey(_journey(), NOW, with_alternative=True)
    finally:
        alternatives.fetch_candidates, alternatives.select_best = fetch, select
        varsel.set_stop_aliases({})

    verdicts = {a.jkey: a.impact.original_is_best for a in alerts}
    assert verdicts == {"subject": True, "bystander": False, "t-twin": False}, verdicts
    # And the two Falses are not the same False. `bystander` was looked up and nothing was
    # established; `t-twin` was never looked up at all — `decisions._alternative_status` writes
    # `none_found` for the first and `not_worth_lookup` for the second, and the column is only worth
    # having while they stay distinguishable.
    looked_up = {a.jkey: a.impact.alternative_subject for a in alerts}
    assert looked_up == {
        "subject": ("VYG", "subject"),
        "bystander": ("VYG", "bystander"),
        "t-twin": None,
    }, looked_up


def test_one_trains_alternative_is_never_published_for_another():
    """The prd defect of 2026-08-12, at the surface that shipped it.

    A registered window is a *window*, and during a mass cancellation it can hold a train an hour
    from now and another six hours out. The Journey Planner answer is selected against one train's
    aimed arrival, so it is an answer about that train — and the traveller whose 11:59 was cancelled
    was told to catch the 05:45, because a single lookup anchored on the window's earliest departure
    was stamped on all thirteen alerts.
    """
    live = datetime.now(UTC)
    # A wide registered window, which is what makes two trains this far apart one journey's problem.
    journey = _journey(begin="06:00", end="13:00")
    snapshot = {
        "S1": _train(_oslo(2026, 7, 6, 6, 30), jkey="soon", p_impact_dep=0.95),
        "S2": _train(_oslo(2026, 7, 6, 12, 30), jkey="later", situation="S2", p_impact_dep=0.95),
    }

    def offer_for(*a, original=None, outcome=None, **k):
        """One distinguishable offer per train, so an inherited answer is visible as such."""
        minutes = 15 if _subject_jkey(original) == "soon" else 375
        return _alternative(
            departure_time=live + timedelta(minutes=minutes),
            arrival_time=live + timedelta(minutes=minutes + 40),
        )

    install(snapshot)
    fetch, select = alternatives.fetch_candidates, alternatives.select_best
    alternatives.fetch_candidates = lambda *a, **k: [_live_candidate(live)]
    alternatives.select_best = offer_for
    try:
        alerts = varsel.match_journey(journey, NOW, with_alternative=True)
    finally:
        alternatives.fetch_candidates, alternatives.select_best = fetch, select

    offered = {
        a.jkey: round((a.alternative_journey.departure_time - live).total_seconds() / 60)
        for a in alerts
        if a.alternative_journey is not None
    }
    assert offered == {"soon": 15, "later": 375}, offered


def test_a_collapsed_refs_alert_inherits_a_lookup_only_for_its_own_train():
    """What is left of the back-fill, and the line it must not cross.

    The lookup runs on the canonical ref pair only, so a situation whose worst reading arrives from a
    collapsed ref carries a response that was asked without one. When that reading is about a train
    the canonical pass DID look up — the same train, so the same deadline and the same answer — the
    outcome transfers whole. When it is about a different train, nothing transfers: that is the case
    the collapsed ref is asked separately for in the first place.
    """
    live = datetime.now(UTC)
    snapshot = {
        # Canonical ref: this pass looks the train up.
        "S1": _train(MON_0800, jkey="shared", p_impact_dep=0.90),
        # Collapsed ref, SAME train — a second situation about it, recorded on the other side of the
        # station. Its own pass runs without a lookup, and the answer for `shared` is its answer.
        "S2": _twin(MON_0800, "A2", jkey="shared", situation_number="S2", p_impact_dep=0.95),
        # Collapsed ref, DIFFERENT train. Nobody established anything about this one.
        "S3": _twin(MON_0800, "A2", jkey="stranger", situation_number="S3", p_impact_dep=0.95),
    }
    offer = _alternative(
        departure_time=live + timedelta(minutes=15),
        arrival_time=live + timedelta(minutes=55),
    )
    install(snapshot)
    varsel.set_stop_aliases({"A2": "A"})
    fetch, select = alternatives.fetch_candidates, alternatives.select_best
    alternatives.fetch_candidates = lambda *a, **k: [_live_candidate(live)]
    alternatives.select_best = lambda *a, **k: offer
    try:
        alerts = varsel.match_journey(_journey(), NOW, with_alternative=True)
    finally:
        alternatives.fetch_candidates, alternatives.select_best = fetch, select
        varsel.set_stop_aliases({})

    carried = {a.jkey: a.alternative_journey is not None for a in alerts}
    assert carried == {"shared": True, "stranger": False}, carried
    # And the transfer carries the whole outcome, not just the journey: `alt_status` reads the
    # subject off `alert.impact` to tell `none_found` from `not_worth_lookup`.
    subjects = {a.jkey: a.impact.alternative_subject for a in alerts}
    assert subjects == {"shared": ("VYG", "shared"), "stranger": None}, subjects


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


def _alternative(**kw) -> alternatives.AlternativeJourney:
    """A Journey Planner answer in the shape `select_best` returns it.

    Departs 08:24 and arrives 09:05 by default — the in-time case. The selection rule has its own
    hermetic suite; these checks are about what the traveller is told, so the answer is handed in
    rather than searched for.
    """
    fields = {
        "departure_time": MON_0800 + timedelta(minutes=24),
        "arrival_time": MON_0800 + timedelta(minutes=65),
        "duration_min": 41,
        "minutes_saved": 12,
        "arrives_before_original": True,
        "at_risk": False,
        "legs": [
            alternatives.AlternativeLeg(
                mode="rail", line="VYG:Line:R11", public_code="R11",
                from_name="Oslo S", to_name="Drammen",
            )
        ],
    }
    return alternatives.AlternativeJourney(**{**fields, **kw})


# 09:35 — half an hour past what the disrupted train promised. The routine shape now that the
# fallback tier offers a replacement for a cancelled train rather than nothing at all.
_LATE_ALTERNATIVE = {
    "minutes_saved": -18,
    "arrives_before_original": False,
    "arrival_time": MON_0800 + timedelta(minutes=95),
}


def _push_body(alternative: alternatives.AlternativeJourney | None = None, **board_kw) -> str:
    """The Web Push body for the single match produced by a train with these boarding numbers.

    Through the real message input, not the alert directly: the deterministic wording is assembled
    from the same document the model is prompted with, so these checks have to run over it.

    `alternative` is set on the matched alert, which is where the real cycle puts it: the lookup is
    per disrupted train, and `_collect_alerts` projects each train's answer onto its own alert.
    """
    journey = _journey()
    alerts = match(journey, {"S1": _train(MON_0800, **board_kw)})
    assert len(alerts) == 1
    if alternative is not None:
        alerts[0].alternative_journey = alternative
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
    f-string a Python None straight into the notification ("Tog R10 08:00: None").
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
    assert body == "Signalfeil\nTog R10 08:00"


# ---- one card per train ------------------------------------------------------------------------
def _alert(situation, *, jkey="t1", p_impact=0.9, line="VYG:Line:R10", dep=MON_0800,
           operator="VYG", p_cancel=0.8, **kw):
    return Alert(
        situation_number=situation, summary=situation, line=line, operator=operator, jkey=jkey,
        stop_place="A", stop_order=1, departure_time=dep, cancel_probability=p_cancel,
        delay_p50_min=None, delay_p90_min=None, impact_probability=p_impact,
        alert_threshold=0.65, alertworthy=True, **kw,
    )


def test_two_notices_about_one_departure_collapse_to_the_worst():
    kept = varsel.one_alert_per_train([_alert("S1", p_impact=0.9), _alert("S2", p_impact=0.87)])
    assert [a.situation_number for a in kept] == ["S1"]


def test_alerts_about_different_departures_are_kept_apart():
    """Same line, different train; and same train ref under a different operator (bus
    ServiceJourney jkeys repeat across codespaces, which is why operator is in the key)."""
    later = _alert("S2", jkey="t2", dep=MON_0800 + timedelta(minutes=30))
    other_operator = _alert("S3", operator="RUT")
    kept = varsel.one_alert_per_train([_alert("S1"), later, other_operator])
    assert [a.situation_number for a in kept] == ["S1", "S2", "S3"]


def test_an_unidentifiable_alert_is_never_merged():
    """No jkey and no line means we cannot say which train this is. One card too many is a far
    smaller error than silently hiding an unrelated disruption."""
    anon = [_alert("S1", jkey=None, line=None), _alert("S2", jkey=None, line=None)]
    assert len(varsel.one_alert_per_train(anon)) == 2


def test_the_survivor_of_a_tie_does_not_depend_on_input_order():
    """Every reading of a declared cancellation is (1.0, 0, 1.0), so ties are now the normal case --
    and an unstable winner would flip the card's wording, and which situation the written message
    was cached under, on every 60 s refresh."""
    a = _alert("S1", p_impact=1.0, p_cancel=1.0, cancelled=True)
    b = _alert("S2", p_impact=1.0, p_cancel=1.0, cancelled=True)
    assert varsel.one_alert_per_train([a, b])[0].situation_number == "S2"
    assert varsel.one_alert_per_train([b, a])[0].situation_number == "S2"


def test_push_body_states_a_declared_cancellation_instead_of_scoring_it():
    """The 2026-08-05 report. ET had already cancelled the 15:03 from Asker, and the push (and the
    card wording it) still said "91 % sjanse for innstilling" for a train the journey planner had
    stopped offering -- so the traveller went looking for a departure that did not exist."""
    body = _push_body(et_declared_cancelled=True, p_cancel=0.57, p_impact_dep=0.61)
    assert body == "Signalfeil\nTog R10 08:00: Innstilt"
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
    assert body == "Signalfeil\nTog R10 08:00: Innstilt"


def test_a_no_sx_alert_says_there_is_no_message_instead_of_quoting_one():
    """A train nobody published about still gets a wording, and the wording must not pretend to be
    relaying somebody.

    `summary` on a no-SX alert is a placeholder this codebase wrote ("Mulig avvik - ingen melding
    fra operatoren"). Left in `situasjoner` it reaches the model in the field the prompt documents
    as the OPERATOR'S OWN WORDS, and a model told to relay what the operator said will relay it.
    Both writers therefore see the absence as its own fact instead.
    """
    alert = Alert(
        situation_number="KIX:NoSituation:VYG:DSJ:1", summary="Mulig avvik", line="VYG:Line:R10",
        departure_time=MON_0800, has_situation=False,
        cancel_probability=0.1, delay_p50_min=12, delay_p90_min=25,
        alert_threshold=0.65, alertworthy=True,
    )
    message_input = _message_input_from_alert(alert)
    assert message_input["ingen_melding"] is True
    assert message_input["situasjoner"] == []

    body = _deterministic_body(message_input)
    assert body.startswith("Ingen melding fra operatøren")
    # The placeholder must not survive into what the traveller reads.
    assert "Mulig avvik" not in body
    # ...and the train is still named: dropping the summary must not drop the news with it.
    assert "Tog R10 08:00" in body


def test_a_flagged_alert_is_untouched_by_the_no_sx_branch():
    """The default is the flagged population, and it has to stay byte-identical: every existing
    wording case is about a train a situation named."""
    alert = Alert(
        situation_number="S1", summary="Signalfeil", line="VYG:Line:R10",
        departure_time=MON_0800, cancel_probability=1.0, cancelled=True,
        delay_p50_min=None, delay_p90_min=None, alert_threshold=0.65, alertworthy=True,
    )
    message_input = _message_input_from_alert(alert)
    assert "ingen_melding" not in message_input
    assert _deterministic_body(message_input) == "Signalfeil\nTog R10 08:00: Innstilt"


def test_push_body_omits_a_missing_summary():
    """`summary` is nullable (an SX situation need not carry Norwegian text) -- the same
    unguarded interpolation rendered it as "None" on the body's first line."""
    body = _push_body(summary=None, p_cancel=0.95)
    assert "None" not in body
    assert body == "Tog R10 08:00: 95 % sjanse for innstilling"


# ---- the alternative, in the wording prd actually ships ---------------------------------------
# VARSEL_LLM_ENABLED=false in prd, so these strings -- not the model's -- are what real travellers
# read. A caveat that reaches only the prompt reaches nobody.
_DELAYED = {"p_cancel": 0.02, "p_impact_dep": 0.93,
            "dep_delay_p50_stack_min": 15, "dep_delay_p90_stack_min": 27}


def test_push_body_without_an_alternative_is_unchanged():
    """The regression guard for this whole change: when nothing was found, the body is exactly the
    two lines it has always been. Pinned, not asserted `in`, so an unconditional clause shows up."""
    assert _push_body(**_DELAYED) == "Signalfeil\nTog R10 08:00: Typisk 15–27 min forsinket"


def test_push_body_names_the_train_by_its_line():
    """"Tog R10 08:00", not "Tog kl 08:00" — the label a departure board would use.

    A registered journey matches whatever lines run between its two stops, so a time alone leaves
    the reader working out which of them left at 08:00, on a lock screen, in the minutes before they
    would have to walk to the platform. Pinned as its own check because every other body assertion
    here carries the line incidentally: this one is about the label itself.
    """
    assert _push_body(**_DELAYED).splitlines()[1].startswith("Tog R10 08:00: ")


def test_a_train_with_no_known_line_keeps_the_old_label():
    """`line` is nullable on the Alert, and the fallback must say less rather than something untrue.

    "Tog 08:00" alone would read as a quantity, so the "kl" stays in exactly this case — and no
    line is invented to fill the slot, which is the one failure worse than naming none.
    """
    journey = _journey()
    alerts = match(journey, {"S1": _train(MON_0800, line=None, **_DELAYED)})
    body = _deterministic_body(_message_input(journey, alerts[0]))
    assert body == "Signalfeil\nTog kl 08:00: Typisk 15–27 min forsinket"


def test_push_body_names_an_in_time_alternative():
    body = _push_body(_alternative(), **_DELAYED)
    assert body == (
        "Signalfeil\nTog R10 08:00: Typisk 15–27 min forsinket\n"
        "Alternativ Tog R11 08:24, framme kl 09:05"
    )


def test_the_alternative_names_the_leg_the_traveller_has_to_catch():
    """The FIRST transit leg, and a foot leg is never it.

    "Alternativ kl 08:24" says when to be on a platform but not what to board, which at a station
    with several departures in that minute is the half of the advice the traveller cannot supply.
    A later leg's line is a connection they will read off a board once they are moving; naming it
    here would put two line numbers in one clause with nothing saying which to board -- and naming
    the walk to the platform would name no vehicle at all.
    """
    walk_then_bus_then_train = _alternative(legs=[
        alternatives.AlternativeLeg(mode="foot", from_name="Oslo S", to_name="Jernbanetorget"),
        alternatives.AlternativeLeg(mode="bus", line="RUT:Line:31", public_code="31",
                                    from_name="Jernbanetorget", to_name="Lysaker"),
        alternatives.AlternativeLeg(mode="rail", line="VYG:Line:R11", public_code="R11",
                                    from_name="Lysaker", to_name="Drammen"),
    ])
    body = _push_body(walk_then_bus_then_train, **_DELAYED)
    assert body.endswith("Alternativ Buss 31 08:24, framme kl 09:05"), body
    assert "R11" not in body, "the connection is not the departure they have to catch"


def test_an_alternative_this_codebase_has_no_noun_for_still_names_its_line():
    """Each missing part costs only itself. A mode outside `_LEG_MODE_NOUN` yields no vehicle noun
    -- a guessed one would put the wrong vehicle on a lock screen -- while the line and the time
    still identify the departure, and a leg with neither falls back to the time alone."""
    unknown_mode = _alternative(legs=[
        alternatives.AlternativeLeg(mode="funicular", line="X:Line:F1", public_code="F1"),
    ])
    assert _push_body(unknown_mode, **_DELAYED).endswith("Alternativ F1 08:24, framme kl 09:05")
    nameless = _alternative(legs=[alternatives.AlternativeLeg(mode="foot")])
    assert _push_body(nameless, **_DELAYED).endswith("Alternativ kl 08:24, framme kl 09:05")


def test_push_body_says_when_the_alternative_arrives_after_the_planned_arrival():
    """The case this change exists for: the new rule routinely offers the next possibility rather
    than a faster one, so a body that named a departure time without saying so would tell the
    traveller they still make their appointment. Under the old rule this shape was unreachable —
    a journey arriving after the disrupted train could never clear "at least three minutes
    faster" — which is the part of the old baseline that no measurement can revise."""
    body = _push_body(_alternative(**_LATE_ALTERNATIVE), **_DELAYED)
    assert body == (
        "Signalfeil\nTog R10 08:00: Typisk 15–27 min forsinket\n"
        "Alternativ Tog R11 08:24, framme kl 09:35 – senere enn planlagt ankomst"
    )
    assert "spart" not in body, "a negative saving is not a saving"


def test_push_body_hedges_an_alternative_that_is_itself_at_risk():
    """Offered because nothing safer reached the destination in time -- so it must not read as a
    clean escape."""
    body = _push_body(_alternative(at_risk=True), **_DELAYED)
    assert body == (
        "Signalfeil\nTog R10 08:00: Typisk 15–27 min forsinket\n"
        "Alternativ Tog R11 08:24, framme kl 09:05 (kan også bli rammet)"
    )


def test_push_body_leads_with_a_declared_cancellation_even_with_an_alternative():
    """The cancellation is the fact; the alternative is the advice that follows it. And no
    probability anywhere -- the 2026-08-05 report, which an extra clause must not reopen."""
    body = _push_body(_alternative(**_LATE_ALTERNATIVE), et_declared_cancelled=True,
                      p_cancel=0.57, p_impact_dep=0.61)
    assert body == (
        "Signalfeil\nTog R10 08:00: Innstilt\n"
        "Alternativ Tog R11 08:24, framme kl 09:35 – senere enn planlagt ankomst"
    )
    assert "%" not in body and "sjanse" not in body


def test_the_last_resort_wording_names_the_alternative_for_a_cancelled_train():
    """`_message_input_from_alert` returns early on a declared cancellation, so an alternative
    appended after that branch would be dropped for exactly the case this change exists to serve:
    the push would say "Innstilt" and name no replacement."""
    alert = Alert(
        situation_number="S1", summary="Signalfeil", line="VYG:Line:R10",
        departure_time=MON_0800, cancelled=True, cancel_probability=1.0,
        delay_p50_min=None, delay_p90_min=None, alert_threshold=0.65, alertworthy=True,
        alternative_journey=_alternative(**_LATE_ALTERNATIVE),
    )
    body = _deterministic_body(_message_input_from_alert(alert))
    assert body == (
        "Signalfeil\nTog R10 08:00: Innstilt\n"
        "Alternativ Tog R11 08:24, framme kl 09:35 – senere enn planlagt ankomst"
    )


def test_the_last_resort_wording_quotes_a_real_saving_but_never_a_negative_one():
    """The same `minutter_spart` discipline as the message input: the key exists only when it is
    the figure worth quoting, so the two wording sources cannot disagree about it."""
    def input_for(**alt_kw):
        return _message_input_from_alert(Alert(
            situation_number="S1", summary="Signalfeil", line="VYG:Line:R10",
            departure_time=MON_0800, cancel_probability=0.02, impact_probability=0.93,
            delay_p50_min=15, delay_p90_min=27, alert_threshold=0.65, alertworthy=True,
            alternative_journey=_alternative(**alt_kw),
        ))

    assert input_for()["alternativ_reise"]["minutter_spart"] == 12
    late = input_for(**_LATE_ALTERNATIVE)["alternativ_reise"]
    assert "minutter_spart" not in late
    assert late["ankommer_i_tide"] is False


# ---- the caches the poller carries between cycles ----------------------------------------------
# `_alternatives` and `_messages` outlive the tick that filled them (they are pruned when the
# SITUATION ends, up to 12 h later) while the alerts underneath are re-scored every 60 s. Nothing
# ever cleared an entry before 2026-08-06, on the belief that an alternative was rare enough for a
# stale one not to be worth the code -- a belief measured for the first time on 2026-08-06 and found
# wrong (26 % of alerting subjects under the OLD rule). Both outcomes are routine, and one of them
# -- "stay on your own train" -- is advice that contradicts what may already be cached.
_RECONCILE_KEY = "A|B|07:00|09:00|0,1,2,3,4"


def _reconcile_cycle(*, cached=None, fresh=None, original_is_best=False, alertworthy=True,
                     message=None, now=NOW):
    """One reconciliation over a single freshly-matched alert; returns what survived in the caches.

    The alert comes out of the real matcher (so `impact` is a real ImpactResponse and the verdict is
    set the way `/impact` sets it), and the cached state is planted under the same message key the
    poller would have used.
    """
    alerts = match(_journey(), {"S1": _train(MON_0800, p_impact_dep=0.9 if alertworthy else 0.1)})
    assert len(alerts) == 1 and alerts[0].alertworthy is alertworthy
    alert = alerts[0]
    alert.alternative_journey = fresh
    if original_is_best:
        # What impact.for_trip records when select_best returned None over a real pool: the
        # traveller's own train won. `alternative_subject` must name THIS train, or match_journey
        # strips the verdict as a claim about somebody else's.
        alert.impact = alert.impact.model_copy(
            update={"original_is_best": True, "alternative_subject": (alert.operator, alert.jkey)}
        )
    key = varsel._message_key(_RECONCILE_KEY, alert)
    for cache in (varsel._alternatives, varsel._messages, varsel._message_meta):
        cache.clear()
    if cached is not None:
        varsel._alternatives[key] = cached
    if message is not None:
        varsel._messages[key] = message
        varsel._message_meta[key] = {"status": "ok", "provider": "vertex-anthropic"}
    try:
        varsel._reconcile_caches(_RECONCILE_KEY, alerts, now)
        return varsel._alternatives.get(key), varsel._messages.get(key), varsel._message_meta.get(key)
    finally:
        for cache in (varsel._alternatives, varsel._messages, varsel._message_meta):
            cache.clear()


def test_a_selection_that_chose_the_travellers_own_train_clears_the_cached_alternative():
    """The new outcome this whole change introduced. `original_is_best` is a VERDICT — the selection
    ran, over a real pool, and decided the traveller should stay put — so it supersedes whatever was
    cached, and the text written from that cached answer with it."""
    alt, message, meta = _reconcile_cycle(
        cached=_alternative(), message="Ta R11 kl 08:24 i stedet.", original_is_best=True
    )
    assert alt is None
    assert message is None, "the wording naming it must not outlive the alternative"
    assert meta is None, "a dropped message's provenance would file the next row as ok_cached"


def test_a_cycle_that_never_looked_keeps_the_cached_alternative():
    """A null alternative with no verdict means "we did not establish it" — a collapsed stop ref is
    asked without the lookup on purpose, and an outage or the fetch budget skip it too. Clearing on
    silence would make the card offer an escape route on alternate ticks and none in between."""
    alt, message, _ = _reconcile_cycle(cached=_alternative(), message="Ta R11 kl 08:24 i stedet.")
    assert alt is not None and alt.departure_time == MON_0800 + timedelta(minutes=24)
    assert message == "Ta R11 kl 08:24 i stedet."


def test_a_match_that_fell_below_the_gate_drops_both_caches():
    """The commonest way this advice goes stale: the disruption eases off until nothing crosses the
    threshold. That path takes check_and_notify's early return, so before 2026-08-06 it reached no
    pop at all and the card kept last hour's escape route beside this tick's mild numbers."""
    alt, message, _ = _reconcile_cycle(
        cached=_alternative(), message="Ta R11 kl 08:24 i stedet.", alertworthy=False
    )
    assert alt is None and message is None


def test_an_alternative_departing_exactly_now_is_no_longer_catchable():
    """One rule, one operator: `select_best` pools on `> now` (a departure exactly at `now` is not
    offered), and the card read `>= now` — so for one second it advised a train the selection rule
    had already refused to choose."""
    assert varsel._still_catchable(_alternative(departure_time=NOW), NOW) is False
    assert varsel._still_catchable(_alternative(departure_time=NOW + timedelta(seconds=1)), NOW)
    # And an unreadable departure is kept: "we cannot tell whether it has left" is not "it has left".
    assert varsel._still_catchable(_alternative(departure_time=None), NOW)


def test_a_cached_alternative_that_has_departed_is_dropped_with_its_wording():
    """Kept-on-silence has one exception. A stale alternative is worse than none — it advises
    catching a train that has already left — and so is the message that named its departure time."""
    alt, message, _ = _reconcile_cycle(
        cached=_alternative(departure_time=NOW), message="Ta R11 kl 06:00 i stedet."
    )
    assert alt is None and message is None


def test_a_different_alternative_invalidates_the_message_written_from_the_old_one():
    """A written message now quotes a specific departure time, and it is reused for the situation's
    whole life (`ok_cached`). Offering a new alternative under the old wording would advise one train
    on the card and another in the text."""
    fresh = _alternative(
        departure_time=MON_0800 + timedelta(minutes=40),
        arrival_time=MON_0800 + timedelta(minutes=80),
    )
    alt, message, _ = _reconcile_cycle(
        cached=_alternative(), fresh=fresh, message="Ta R11 kl 08:24 i stedet."
    )
    assert alt.departure_time == fresh.departure_time
    assert message is None


def test_a_sub_minute_realtime_nudge_is_the_same_advice():
    """Both times are Journey Planner EXPECTED times at second precision, and every wording renders
    `%H:%M`. So a realtime nudge from 08:24:12 to 08:24:41 changed the identity, dropped the message
    and spent an LLM call producing byte-identical prose — roughly every other cycle per alerting
    journey, out of a budget of 8 for the whole tick.

    The comparison has to run at the resolution of the advice, not at the resolution of the feed.
    """
    cached = _alternative(
        departure_time=MON_0800 + timedelta(minutes=24, seconds=12),
        arrival_time=MON_0800 + timedelta(minutes=65, seconds=3),
    )
    nudged = _alternative(
        departure_time=MON_0800 + timedelta(minutes=24, seconds=41),
        arrival_time=MON_0800 + timedelta(minutes=65, seconds=50),
    )
    alt, message, meta = _reconcile_cycle(
        cached=cached, fresh=nudged, message="Ta R11 kl 08:24 i stedet."
    )
    assert message == "Ta R11 kl 08:24 i stedet.", "08:24 is still 08:24; nothing to re-write"
    assert meta is not None, "and its provenance stays with it, or the next row lies about it"
    assert alt.departure_time == nudged.departure_time, "the stored copy is still refreshed"

    # A change the wording WOULD quote differently still discards it — the point is the resolution,
    # not that the check got weaker.
    _, dropped, _ = _reconcile_cycle(
        cached=cached,
        fresh=_alternative(
            departure_time=MON_0800 + timedelta(minutes=25),
            arrival_time=MON_0800 + timedelta(minutes=65, seconds=3),
        ),
        message="Ta R11 kl 08:24 i stedet.",
    )
    assert dropped is None, "08:24 -> 08:25 is different advice"


def test_the_same_alternative_re_scored_keeps_the_written_message():
    """`ok_cached` has to stay reachable: re-writing on every tick is exactly what the per-cycle LLM
    budget exists to prevent. The two prediction blocks carry continuous probabilities re-scored
    against the live cache every 60 s, so they must not count as "a different alternative" — while
    the fresh reading of them still replaces the stored copy, so the card shows this tick's numbers.
    """
    rescored = _alternative(
        departure_prediction=alternatives.AlternativePrediction(scored=False, delay_p50_min=4),
    )
    alt, message, _ = _reconcile_cycle(
        cached=_alternative(), fresh=rescored, message="Ta R11 kl 08:24 i stedet."
    )
    assert message == "Ta R11 kl 08:24 i stedet.", "the wording quotes none of those figures"
    assert alt.departure_prediction is not None, "the stored copy must still be refreshed"


def test_an_alternative_that_became_risky_drops_the_written_message():
    """The one re-scored field the wording DOES state out loud, so it cannot be treated as noise.

    `at_risk` earns "(kan også bli rammet)" and the model is told to hedge on it. Left out of the
    identity, `_reconcile_caches` stored the rescored journey and kept the text written when the
    flag was false — so the traveller was still being promised a clean escape after it stopped being
    one. A boolean flips at most once or twice in a situation's life, so paying a re-write for it
    cannot reintroduce the per-tick churn the minute-rounding above exists to stop.
    """
    _, dropped, _ = _reconcile_cycle(
        cached=_alternative(), fresh=_alternative(at_risk=True), message="Ta R11 kl 08:24 i stedet."
    )
    assert dropped is None, "a clean escape that stopped being one is different advice"

    # The same for the other flag a wording states: arriving after the planned time is the caveat
    # `_alternative_phrase` adds, and it must not survive on a journey that now arrives in time.
    _, dropped, _ = _reconcile_cycle(
        cached=_alternative(**_LATE_ALTERNATIVE),
        fresh=_alternative(**{**_LATE_ALTERNATIVE, "arrives_before_original": True}),
        message="Ta R11 kl 09:35 i stedet, du er framme 30 min etter planen.",
    )
    assert dropped is None, "'later than you planned' must not outlive being true"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} matcher checks passed.")
