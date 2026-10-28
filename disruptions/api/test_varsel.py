"""Matcher checks for varsel.match_journey — synthetic _CallPred-shaped rows.

Runs standalone (`uv run --with fastapi --with pydantic python test_varsel.py`) or under pytest.
No BigQuery, no network: the BigQuery client is stubbed before main is imported, and the impact
provider is main's real one driven over an injected synthetic snapshot — so these exercise the
actual /impact matcher plus varsel's policy on top of it, end to end.

Covers direction, window edges, the weekday filter, windows over midnight, the departed-train filter
(no alert once the expected departure has passed), the alert-worthiness gate, the per-operator
thresholds, and the notification wording.
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import alert_policy
import alternatives
import impact
import llm_message
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
    report_type: str | None = None
    # Whether a passenger may board / alight at this call. Default True, matching CallPred: every
    # case written before the F4 boarding-only bug is about a call anyone may use.
    boarding_allowed: bool = True
    alighting_allowed: bool = True
    # The rest of the two stacks, which a trip's connection reads (multi-leg KTD7). None, like
    # CallPred's defaults, so no case written before them moves.
    p_arr_ge5: float | None = None
    p_arr_ge20: float | None = None
    p_dep_ge5: float | None = None
    p_dep_ge20: float | None = None


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


def _install_snapshot(by_situation):
    """Refill the snapshot this suite serves, and tell the engine its index is out of date.

    `impact` indexes a snapshot per GENERATION, and a generation is (dict identity, `refreshed_at`)
    — neither of which moves when this suite refills the one dict it owns under a fixed `None`
    stamp. Production never mutates in place (`main.refresh_call_predictions_cache` swaps the dict
    and stamps a new time under one lock), so the engine deliberately cannot see it; a test that
    refilled the dict without this is served the PREVIOUS test's situations, which reads as the
    matcher having stopped matching rather than as leaked state.
    """
    _snapshot.clear()
    _snapshot.update(by_situation)
    impact.invalidate_generation()


def install(by_situation):
    """Point varsel at main's real /impact provider over this synthetic snapshot.

    `with_alternative=False` keeps the Journey Planner out of the matcher tests — the alternative
    lookup has its own hermetic suite in test_alternatives.py.
    """
    _install_snapshot(by_situation)

    def provider(*, from_stop, to_stop, window_start, window_end, catchable_after=None,
                 with_alternative=False, min_impact=0.0, path="poller", plans=None):
        # Pass the flag THROUGH rather than hard-coding False: hard-coding it made the suite blind to
        # a Journey Planner call reappearing on the request path (review 2026-07-29). `path` likewise,
        # and `plans` (multi-leg U5), as `main._varsel_impact` forwards it.
        return impact.for_trip(
            from_stop=from_stop,
            to_stop=to_stop,
            window_start=window_start,
            window_end=window_end,
            select="worst",
            catchable_after=catchable_after,
            with_alternative=with_alternative,
            min_impact=min_impact,
            path=path,
            plans=plans,
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
    _install_snapshot({"S1": _train(MON_0800)})
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
    _install_snapshot({"S1": _twin(MON_0800, "A2", p_cancel=0.8, p_impact_dep=0.9)})
    asked = []

    def provider(*, from_stop, to_stop, window_start, window_end, catchable_after=None,
                 with_alternative=False, min_impact=0.0, path="poller", plans=None):
        asked.append((from_stop, with_alternative))
        return impact.for_trip(
            from_stop=from_stop, to_stop=to_stop, window_start=window_start,
            window_end=window_end, select="worst", catchable_after=catchable_after,
            with_alternative=False, min_impact=min_impact, plans=plans,
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


def test_a_train_the_traveller_may_not_alight_from_does_not_match():
    """The 2026-09-01 bug report, end to end through the matcher a push is built from.

    Vy's 16:23 Oslo S - Bergen calls Drammen, Asker and Sandvika with alighting closed: board it for
    Bergen, but nobody gets off before Hønefoss. It was cancelled, and five registered journeys —
    Oslo S to Drammen (three of them), Asker and Sandvika — were pushed "Tog F4 16:23 er innstilt"
    across eight devices. None of them could have been on it, and one of them wrote in.
    """
    calls = [
        FakeCall("f4", "A", 1, MON_0800, line="VYG:Line:F4", p_cancel=1.0, p_impact_dep=1.0),
        FakeCall("f4", "B", 5, None, aimed_arr=MON_0800 + timedelta(minutes=40),
                 line="VYG:Line:F4", alighting_allowed=False),
    ]
    assert match(_journey(), {"S1": calls}) == []
    # The same train with alighting open is exactly the alert we still want.
    calls[1].alighting_allowed = True
    assert len(match(_journey(), {"S1": calls})) == 1


def test_a_train_the_traveller_may_not_board_does_not_match():
    """The mirror restriction, on the boarding stop."""
    calls = [
        FakeCall("t1", "A", 1, MON_0800, p_impact_dep=0.9, boarding_allowed=False),
        FakeCall("t1", "B", 5, None, aimed_arr=MON_0800 + timedelta(minutes=40)),
    ]
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


def test_a_day_window_does_not_wrap_past_midnight():
    # A day-window [07:00, 09:00] must not catch a 23:30 departure (no wrap-around).
    assert not match(_journey(), {"S1": _train(_oslo(2026, 7, 6, 23, 30))})


def test_an_overnight_window_ends_the_next_day_and_is_still_open_after_midnight():
    """KIX-22: `end < begin` runs over midnight and belongs to the weekday it begins on. At 00:30 Tuesday,
    Monday's 22:00-01:00 is still open. A day window is never offered from yesterday, even inside
    the catch-up grace."""
    tue_0030 = _oslo(2026, 7, 7, 0, 30)

    def utc(*a):
        return _oslo(*a).astimezone(UTC)

    windows = varsel.journey_windows(_journey(begin="22:00", end="01:00", days=(0, 1)), tue_0030)
    assert windows == [
        (utc(2026, 7, 6, 22, 0), utc(2026, 7, 7, 1, 0)),
        (utc(2026, 7, 7, 22, 0), utc(2026, 7, 8, 1, 0)),
    ]
    late = varsel.journey_windows(_journey(begin="22:00", end="23:59", days=(0, 1)), tue_0030)
    assert late == [(utc(2026, 7, 7, 22, 0), utc(2026, 7, 7, 23, 59))]
    assert match(
        _journey(begin="22:00", end="01:00", days=(0,)), {"S1": _train(_oslo(2026, 7, 7, 0, 40))}, now=tue_0030
    )


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
    # The body is the route on its own first line, then the wording (2026-09-02); the wording is
    # what these checks are about, and what the decision log records.
    return _wording(_notification_payload(journey, alerts[0], message_input=message_input))


def _wording(payload: dict) -> str:
    """The wording part of a push body: everything after the route line the body leads with."""
    route, _, wording = payload["body"].partition("\n")
    assert " → " in route, f"the body must lead with the route line, got {route!r}"
    return wording


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


# ---- U2: pending-ness is per RECIPIENT, on either channel (KTD3/KTD4) ------------------------


def test_pending_pushes_is_channel_neutral():
    """One question, two ledgers, and the unit-level statement of KTD3.

    `_notified` answers it for a browser registration (`"<journey_id>|<situation>"`, grown only on a
    successful send) and `WebhookRecipient.delivered` answers it for a webhook subscription
    (Firestore's copy, read once per cycle). A train is pending while ANY recipient on either
    channel still needs telling, which is what makes one wording serve both.

    The two ledgers are NOT at one grain, and that is the channel's own shape rather than an
    accident: the browser is pushed once per situation, so `_notified` is per situation; the store
    mints one event per (subscription, train), so `delivered` holds `alert_id`s and the question is
    asked per train (`_webhook_pending`). The marks here are derived through the same
    `notification_events.alert_id_for` the store's `_sent_body` keys on.
    """
    import notification_events

    varsel._notified.clear()
    alerts = [_alert("S1"), _alert("S2", jkey="t2", dep=MON_0800 + timedelta(minutes=30))]

    def mark(alert):
        train_id = notification_events.alert_id_for(
            "sub-1",
            operator=alert.operator,
            line=alert.line,
            journey_ref=alert.jkey,
            stop_place=alert.stop_place,
            stop_order=alert.stop_order,
            departure_time=alert.departure_time,
        )
        assert train_id is not None
        return train_id

    client = varsel.WebhookRecipient("sub-1", {"from_stop": "A"}, frozenset({mark(alerts[0])}))

    # Browser only, nothing notified yet: both trains pending.
    assert [a.situation_number for a in varsel._pending_pushes(["j1"], alerts)] == ["S1", "S2"]
    # Browser told about S1: only S2 remains -- for that registration.
    varsel._notified.add("j1|S1")
    assert [a.situation_number for a in varsel._pending_pushes(["j1"], alerts)] == ["S2"]
    # A SECOND registration on the same definition has heard nothing, so S1 is pending again.
    assert [a.situation_number for a in varsel._pending_pushes(["j1", "j2"], alerts)] == [
        "S1", "S2",
    ]
    # No browser recipient at all (which is what the caller passes when push is disabled) and a
    # client that has had S1: S2 alone, and it is the client's ledger saying so.
    assert [
        a.situation_number for a in varsel._pending_pushes((), alerts, [client])
    ] == ["S2"]
    # And a client that has had both leaves nothing pending on any channel.
    done = varsel.WebhookRecipient(
        "sub-1", {"from_stop": "A"}, frozenset({mark(alerts[0]), mark(alerts[1])})
    )
    assert varsel._pending_pushes((), alerts, [done]) == []
    # A situation NUMBER in the ledger marks nothing: the client's grain is the train.
    by_situation = varsel.WebhookRecipient("sub-1", {"from_stop": "A"}, frozenset({"S1", "S2"}))
    assert [a.situation_number for a in varsel._pending_pushes((), alerts, [by_situation])] == [
        "S1", "S2",
    ]
    varsel._notified.clear()


def test_a_sub_threshold_train_is_never_pending_on_either_channel():
    """The gate comes first, on both channels alike (KTD2/R3): a webhook recipient must not earn a
    wording for a train `/varsel` would not have alerted on. `alertworthy` is the whole test, and
    it is applied before either ledger is consulted."""
    varsel._notified.clear()
    mild = _alert("S1", p_impact=0.2)
    mild.alertworthy = False
    client = varsel.WebhookRecipient("sub-1", {"from_stop": "A"}, frozenset())
    assert varsel._pending_pushes(["j1"], [mild], [client]) == []


def test_a_webhook_recipient_carries_only_the_five_shared_registration_fields():
    """R16 and KTD3: what enters the pass is a journey DEFINITION and a delivered set, nothing else.

    The five fields are exactly what `journey_key`, `journey_windows` and `_impact` read off a
    registration — `llm_message.build_message_input` resolves the two place NAMES from the stop
    registry rather than from the record, which is why none is needed here. A client's callback
    URL, quota or opaque reference has no way into this pass, and a wider record is how one would
    arrive.
    """
    recipient = varsel._parse_webhook_recipient(
        {
            "recipient_id": "sub-1",
            "from_stop": "A",
            "to_stop": "B",
            "begin": "07:00",
            "end": "09:00",
            "days": [4, 0, 0],
            "delivered": ["S1", "S1"],
            "callback_url": "https://client.example.com/hook",
        }
    )
    assert set(recipient.journey) == {"from_stop", "to_stop", "begin", "end", "days"}
    assert recipient.journey["days"] == [0, 4], "de-duplicated and sorted, as JourneyIn does"
    assert recipient.delivered == frozenset({"S1"})
    assert "client.example.com" not in str(recipient.journey)
    # And the definition keys exactly as a browser registration with the same shape would.
    assert varsel.journey_key(recipient.journey) == varsel.journey_key(
        {"from_stop": "A", "to_stop": "B", "begin": "07:00", "end": "09:00", "days": [0, 4]}
    )


def test_the_webhook_channel_is_off_without_both_seams():
    """Un-wired means off, and it takes BOTH halves to be on.

    The default is what makes an un-wired deployment byte-for-byte the code before the channel
    existed, and it is asserted rather than assumed because it is the entire rollback story: a
    provider with no flag, or a flag with no provider, must contribute nothing.
    """
    before = (varsel._webhook_enabled, varsel._webhook_provider)
    try:
        varsel._webhook_enabled = varsel._webhook_provider = None
        assert varsel.webhook_channel_enabled() is False
        assert varsel._load_webhook_recipients() == []
        varsel._webhook_enabled = lambda: True
        assert varsel.webhook_channel_enabled() is False, "a flag with no store is off"
        varsel._webhook_enabled, varsel._webhook_provider = None, list
        assert varsel.webhook_channel_enabled() is False, "a store with no flag is off"
        varsel._webhook_enabled = lambda: True
        assert varsel.webhook_channel_enabled() is True
    finally:
        varsel._webhook_enabled, varsel._webhook_provider = before


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


def test_the_fallback_input_quotes_a_trips_first_train_figures_only_where_the_template_does():
    """`llm_message.trip_quotes_figures` on the last-resort path too: a confirmed trip whose first
    train we do not score carries the 1.0 certainty, and quoting it would say «100 % sjanse for
    innstilling» about a first train that runs while a later leg is what is cancelled."""
    def trip_alert(kind, index, **figures):
        problem = TripProblem(kind=kind, index=index, probability=0.9, threshold=0.65)
        return Alert(
            situation_number="S9", summary="Signalfeil", line="VYG:Line:R12",
            departure_time=MON_0800, alert_threshold=0.65, alertworthy=True,
            trip=TripFacts(legs=[], transfers=[], problem=problem),
            **{"delay_p50_min": None, "delay_p90_min": None, **figures},
        )

    confirmed = _message_input_from_alert(trip_alert(
        "leg_cancelled", 1, trip_confirmed=True, cancel_probability=1.0, impact_probability=1.0,
    ))
    assert "innstilling_prosent" not in confirmed and "forsinkelse_min" not in confirmed, confirmed
    assert "%" not in _deterministic_body(confirmed)
    first = _message_input_from_alert(trip_alert(
        "departure", 0, cancel_probability=0.6, impact_probability=0.9, delay_p50_min=4,
    ))
    assert first["innstilling_prosent"] == 60 and first["forsinkelse_min"]["typisk"] == 4


def test_a_no_sx_alert_gets_its_own_headline_instead_of_quoting_one():
    """A train nobody published about still gets a wording, and the wording must not pretend to be
    relaying somebody.

    `summary` on a no-SX alert is a placeholder this codebase wrote, and left in `situasjoner` it
    reaches the model in the field the prompt documents as the OPERATOR'S OWN WORDS -- a model told
    to relay what the operator said will relay it. So the list is emptied and the template writes
    its own headline. Since 2026-08-20 that headline states the warning ("Mulig avvik") and not the
    absence behind it: the caveat ran longer than the warning it qualified, and the prompt dropped
    the matching sentence in the same change.
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
    assert body.startswith("Mulig avvik")
    # The wording no longer spends a line on the missing operator message.
    assert "melding fra operatøren" not in body
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
        "Alternativ tog R11 08:24, framme kl 09:05"
    )


def test_the_vehicle_noun_is_capitalized_only_where_it_heads_a_line():
    """"Alternativ tog R11 08:24", not "Alternativ Tog R11 08:24" (2026-08-20).

    Both labels come from `notification_wording.train_label`, but they land in different places:
    the traveller's own train heads its own line, where the capital is the sentence's, and the
    alternative sits mid-clause after "Alternativ", where bokmål wants an ordinary common noun.
    `notification_wording._LEG_MODE_NOUN` carries the lower-case form for exactly that reason — the alert's `mode` ("Tog") is a label in
    the payload, not a spelling. The system prompt asks the model for the same thing, so the two
    writers stay comparable on this axis rather than differing by orthography.
    """
    lines = _push_body(_alternative(), **_DELAYED).splitlines()
    assert lines[1].startswith("Tog R10 08:00"), "line-initial: the capital is the sentence's"
    assert lines[2] == "Alternativ tog R11 08:24, framme kl 09:05"
    assert "Tog R11" not in "\n".join(lines)


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
    assert body.endswith("Alternativ buss 31 08:24, framme kl 09:05"), body
    assert "R11" not in body, "the connection is not the departure they have to catch"


def test_an_alternative_this_codebase_has_no_noun_for_still_names_its_line():
    """Each missing part costs only itself. A mode outside `notification_wording._LEG_MODE_NOUN`
    yields no vehicle noun -- a guessed one would put the wrong vehicle on a lock screen -- while
    the line and the time still identify the departure, and a leg with neither falls back to the
    time alone."""
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
        "Alternativ tog R11 08:24, framme kl 09:35 – senere enn planlagt ankomst"
    )
    assert "spart" not in body, "a negative saving is not a saving"


def test_push_body_hedges_an_alternative_that_is_itself_at_risk():
    """Offered because nothing safer reached the destination in time -- so it must not read as a
    clean escape."""
    body = _push_body(_alternative(at_risk=True), **_DELAYED)
    assert body == (
        "Signalfeil\nTog R10 08:00: Typisk 15–27 min forsinket\n"
        "Alternativ tog R11 08:24, framme kl 09:05 (kan også bli rammet)"
    )


def test_push_body_leads_with_a_declared_cancellation_even_with_an_alternative():
    """The cancellation is the fact; the alternative is the advice that follows it. And no
    probability anywhere -- the 2026-08-05 report, which an extra clause must not reopen."""
    body = _push_body(_alternative(**_LATE_ALTERNATIVE), et_declared_cancelled=True,
                      p_cancel=0.57, p_impact_dep=0.61)
    assert body == (
        "Signalfeil\nTog R10 08:00: Innstilt\n"
        "Alternativ tog R11 08:24, framme kl 09:35 – senere enn planlagt ankomst"
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
        "Alternativ tog R11 08:24, framme kl 09:35 – senere enn planlagt ankomst"
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
_RECONCILE_PAIR = ("A", "B")


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
    # TWO KEYS since KTD9 (2026-09-12): the alternative lives under the coarse train slot, the
    # wording under a key that also names the advice. The wording key is derived AFTER the cached
    # alternative is in place, because that is what it names -- and that ordering is the property
    # under test here, since a forgotten wording is observed as the entry under THAT key going away.
    for cache in (varsel._alternatives, varsel._messages, varsel._message_meta):
        cache.clear()
    subject_key = varsel._subject_key(_RECONCILE_PAIR, alert)
    if cached is not None:
        varsel._alternatives[subject_key] = cached
    key = varsel._message_key(_RECONCILE_PAIR, alert)
    if message is not None:
        varsel._messages[key] = message
        varsel._message_meta[key] = {"status": "ok", "provider": "vertex-anthropic"}
    try:
        varsel._reconcile_caches(_RECONCILE_PAIR, alerts, now)
        return (
            varsel._alternatives.get(subject_key),
            varsel._messages.get(key),
            varsel._message_meta.get(key),
        )
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
    # `notification_wording.alternative_phrase` adds, and it must not survive on a journey that
    # now arrives in time.
    _, dropped, _ = _reconcile_cycle(
        cached=_alternative(**_LATE_ALTERNATIVE),
        fresh=_alternative(**{**_LATE_ALTERNATIVE, "arrives_before_original": True}),
        message="Ta R11 kl 09:35 i stedet, du er framme 30 min etter planen.",
    )
    assert dropped is None, "'later than you planned' must not outlive being true"


# ---- _WriteBudget: the per-cycle ceiling, now claimed from several threads (2026-08-21) --------
#
# The budget stopped being a loop counter when the writing pass was fanned out, and a ceiling that
# several threads can each satisfy is a different object from one a single loop increments. What is
# pinned here is that neither limit can be overshot by a race, and that the CLOCK limit reserves
# rather than checks — the property that made raising the count from 8 to 64 safe at all.


def test_the_write_budget_never_hands_out_more_than_its_count():
    budget = varsel._WriteBudget(3, monotonic() + 1000)
    assert [budget.claim() for _ in range(5)] == [True, True, True, False, False]
    assert budget.claimed == 3
    assert budget.refused == 2


def test_the_write_budget_holds_its_count_under_concurrent_claims():
    """The reason it owns a lock. 64 slots and 64 threads racing for them must yield 64, not 65 --
    a read-then-increment without the lock loses claims exactly when the budget binds, which is the
    only time anyone is looking."""
    budget = varsel._WriteBudget(64, monotonic() + 1000)
    granted: list[bool] = []
    lock = threading.Lock()
    start = threading.Event()

    def claim() -> None:
        start.wait(5)
        got = budget.claim()
        with lock:
            granted.append(got)

    threads = [threading.Thread(target=claim) for _ in range(128)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(5)
    assert sum(granted) == 64, f"handed out {sum(granted)} of 64 slots"
    assert budget.claimed == 64
    assert budget.refused == 64


def test_the_reserved_worst_case_is_one_call_at_the_backend_timeout():
    """What the write pass reserves has to be what one call can actually cost, and it has now been
    wrong in both directions.

    It was `2 * 8.0` for "two backend attempts" until 2026-09-19, and there was only ever one: every
    environment pins VARSEL_LLM_PROVIDER, so `llm_message._candidates` returns a single name, and
    the Gemini client was built with no `retry_options` — the SDK's "never retry" strategy, one HTTP
    request per call. Then on 2026-09-20 `_RETRY_OPTIONS` bought two retries, which is the second
    half of that sentence turning false, and 8.0 became an under-reservation: a call that retries
    twice runs ~20 s, so the pass could start one 12 s past the point where its own deadline says
    it must end.

    So this pins the reservation against the derived constant rather than against a timeout, and
    pins the derivation too — the number is attempts × the per-attempt deadline plus the waits
    between them, and a later change to either field of `_RETRY_OPTIONS` has to travel all the way
    here. The one-provider half is still asserted below, because a second provider in the order is
    the other way the reachable worst case grows.
    """
    assert varsel._WORST_CASE_CALL_S == llm_message._WORST_CASE_CALL_S, (
        "the reservation no longer matches one backend call"
    )
    attempts = llm_message._RETRY_OPTIONS["attempts"]
    assert llm_message._WORST_CASE_CALL_S == (
        llm_message._TIMEOUT_S * attempts
        + llm_message._RETRY_OPTIONS["max_delay"] * (attempts - 1)
    ), "the worst case is every attempt at its own deadline plus the waits between them"
    # The waits are only that long if `initial_delay` cannot exceed `max_delay`: google-genai reads
    # every retry field as `options.x or <default>`, so it clips the exponential-jitter wait at
    # `max_delay` but would silently restore its own 1.0 jitter for a falsy one.
    assert (
        0 < llm_message._RETRY_OPTIONS["initial_delay"] <= llm_message._RETRY_OPTIONS["max_delay"]
    ), "an unclipped backoff makes the reserved worst case an under-estimate"
    provider = llm_message._PROVIDER
    try:
        llm_message._PROVIDER = "gemini"
        assert len(llm_message._candidates(state=llm_message._LatchState())) == 1, (
            "a call can now try more than one backend; _WORST_CASE_CALL_S must grow with it"
        )
        # And prove that check CAN fail: `auto` is the branch where a call really does walk two
        # backends, so an assertion that passes for a pinned provider is not passing vacuously.
        llm_message._PROVIDER = "auto"
        assert len(llm_message._candidates(state=llm_message._LatchState())) == 2, (
            "the one-backend assertion above proves nothing if the unpinned branch is also one"
        )
    finally:
        llm_message._PROVIDER = provider


def test_the_write_budget_reserves_the_clock_rather_than_checking_it():
    """A deadline that is merely CHECKED lets a call start with a millisecond to spare and then run
    its full worst case, so the pass ends late by however long one call takes -- ~46 s against a
    documented 30 s ceiling, measured, before this was a reservation. With _WORST_CASE_CALL_S of
    room left the answer must already be no."""
    just_inside = monotonic() + varsel._WORST_CASE_CALL_S - 0.5
    budget = varsel._WriteBudget(100, just_inside)
    assert not budget.claim(), "a call was allowed to start that cannot finish by the deadline"
    assert budget.refused == 1

    comfortable = monotonic() + varsel._WORST_CASE_CALL_S + 30
    assert varsel._WriteBudget(100, comfortable).claim()


def test_the_two_write_limits_are_independent_after_the_fan_out():
    """Sequentially the count and the clock were the same limit wearing different clothes (8 calls
    x ~2.2 s ~= 18 s of a 30 s budget). If a later edit re-couples them -- by sizing the count from
    the clock again -- raising one silently stops doing anything."""
    assert varsel._MAX_LLM_CALLS_PER_CYCLE > varsel._MAX_LLM_SECONDS_PER_CYCLE / 2.2, (
        "the call cap is back below what one cycle's wall clock could serve sequentially; "
        "the fan-out is what makes them independent"
    )
    assert varsel._LLM_WORKERS > 1 and varsel._MATCH_WORKERS > 1


# --- Persisted state: togvarsel round markers + unknown-key round-trip (U4) --------------------


def test_togvarsel_rounds_and_unknown_keys_survive_a_state_round_trip():
    """Round markers ride the persisted blob (a completed togvarsel round never re-fires across
    restarts), and a top-level key THIS build does not know still survives serialize→apply —
    rollback insurance, so a blob written by a future feature outlives a roll-back-and-forward."""
    import json as _json

    with varsel._state_lock:
        saved_raw = varsel._serialize_state()
    save_state, varsel._save_state = varsel._save_state, lambda: None
    try:
        blob = _json.loads(saved_raw)
        blob["future_feature"] = {"anything": [1, 2, 3]}
        blob["togvarsel_rounds"] = {"2026-08-31": {"0630": {"level": "yellow", "pushed": True}}}
        varsel._apply_state(_json.dumps(blob))
        assert varsel.togvarsel_rounds_for("2026-08-31") == {
            "0630": {"level": "yellow", "pushed": True}
        }
        assert varsel.togvarsel_rounds_for("2026-08-30") == {}  # only the asked-for date
        round_tripped = _json.loads(varsel._serialize_state())
        assert round_tripped["future_feature"] == {"anything": [1, 2, 3]}
        assert round_tripped["togvarsel_rounds"] == {
            "2026-08-31": {"0630": {"level": "yellow", "pushed": True}}
        }
        # the known keys still round-trip beside the unknown one
        assert set(round_tripped) >= {"devices", "journeys", "notified", "togvarsel_rounds"}
    finally:
        varsel._save_state = save_state
        varsel._apply_state(saved_raw)


def test_store_togvarsel_rounds_prunes_to_the_given_date_saves_and_copies():
    """The store accessor keeps exactly one Oslo day (day rollover prunes yesterday's markers),
    persists on every write, and the read accessor hands out copies — togvarsel_notify mutating
    its working map must never reach back into varsel's state."""
    saves = []
    with varsel._state_lock:
        saved_raw = varsel._serialize_state()
    # `varsel_state.save_state` and NOT `varsel._save_state`: the markers persist through
    # `varsel_state.save_rounds`, which is the blob's own writer under this backend and one
    # Firestore document under the store. Patching the caller's helper would count nothing and
    # still write the blob — the totality argument `save_state`'s own docstring makes.
    save_state = varsel.varsel_state.save_state
    varsel.varsel_state.save_state = lambda: bool(saves.append(1)) or True
    try:
        varsel.store_togvarsel_rounds("2026-08-30", {"1400": {"level": "red", "pushed": True}})
        varsel.store_togvarsel_rounds("2026-08-31", {"0630": {"level": "yellow", "pushed": True}})
        assert varsel.togvarsel_rounds_for("2026-08-30") == {}  # yesterday pruned at rollover
        assert varsel.togvarsel_rounds_for("2026-08-31") == {
            "0630": {"level": "yellow", "pushed": True}
        }
        assert len(saves) == 2  # persisted on every store
        got = varsel.togvarsel_rounds_for("2026-08-31")
        got["0630"]["pushed"] = False
        assert varsel.togvarsel_rounds_for("2026-08-31")["0630"]["pushed"] is True
    finally:
        varsel.varsel_state.save_state = save_state
        varsel._apply_state(saved_raw)


# ---- design review 2026-09-01: the title, the tag, «i morgen» and «N min senere» ----------------


def test_the_push_title_is_the_fact_about_the_train_and_the_body_leads_with_the_route():
    """Finding 13: the title was the registration's name, which every phone truncated. It now says
    what happened to which train; the route moved to the body's first line, ABOVE the wording, so
    the wording itself -- what /historikk records -- is byte-for-byte what it was."""
    journey = _journey()
    alerts = match(journey, {"S1": _train(MON_0800, p_cancel=0.82, p_impact_dep=0.9)})
    message_input = _message_input(journey, alerts[0])
    payload = _notification_payload(journey, alerts[0], message_input=message_input, journey_id="j1")
    assert payload["title"] == "Tog R10 08:00 kan bli innstilt"
    route, _, wording = payload["body"].partition("\n")
    assert route == "Oslo S → Lillehammer"
    assert wording == _deterministic_body(message_input)
    # One tag per TRAIN per registration (finding 14), the key one_alert_per_train collapses on.
    assert payload["tag"] == "j1|VYG|t1|A|1"

    cancelled = match(journey, {"S1": _train(MON_0800, et_declared_cancelled=True, p_cancel=1.0)})
    assert varsel._notification_title(_message_input(journey, cancelled[0]), "A", "B") == (
        "Tog R10 08:00 er innstilt"
    )
    assert varsel._notification_title(
        _message_input(journey, cancelled[0]), "A", "B", confirmed=True
    ) == "Bekreftet: Tog R10 08:00 er innstilt"
    # Only a cancellation is a confirmation; a second forecast about the train is not.
    assert varsel._notification_title(message_input, "A", "B", confirmed=True) == (
        "Tog R10 08:00 kan bli innstilt"
    )
    delayed = match(journey, {"S1": _train(MON_0800, p_cancel=0.1, p_impact_dep=0.9)})
    assert varsel._notification_title(_message_input(journey, delayed[0]), "A", "B") == (
        "Tog R10 08:00 kan bli forsinket"
    )
    # No departure in the input: the route is the fallback, exactly the old title.
    assert varsel._notification_title({}, "Oslo S", "Lillehammer") == "Oslo S → Lillehammer"


def test_a_train_that_leaves_tomorrow_is_said_to():
    """Finding 48: the poller scores ~12 h ahead, so a push about the 07:18 goes out the evening
    before by design -- and read at 21:36 it sounded like tonight's train. Both wording sources say
    «i morgen», and the title with them; exactly tomorrow, never further out."""
    tomorrow_0800 = MON_0800 + timedelta(days=1)
    journey = _journey()
    alerts = match(journey, {"S1": _train(tomorrow_0800, et_declared_cancelled=True, p_cancel=1.0)})
    from_alert = varsel._message_input_from_alert(alerts[0])
    # `_departs_tomorrow` reads the wall clock, so pin its answer through the input directly.
    assert varsel._departs_tomorrow(tomorrow_0800, now=NOW) is True
    assert varsel._departs_tomorrow(MON_0800, now=NOW) is False
    assert varsel._departs_tomorrow(MON_0800 + timedelta(days=2), now=NOW) is False
    from_alert["i_morgen"] = True
    assert _deterministic_body(from_alert) == "Signalfeil\nTog R10 08:00 i morgen: Innstilt"
    assert varsel._notification_title(from_alert, "A", "B") == "Tog R10 08:00 i morgen er innstilt"


def test_the_template_quotes_how_late_the_replacement_is_against_the_plan():
    """Finding 16: «ankomst 18:54, som er senere enn planlagt» made the reader subtract two clock
    times on a lock screen. With `minutter_senere` in the input the template says the number; the
    figure rides only when the alternative is late, so an in-time one never gets a «0 min»."""
    late = _alternative(arrives_before_original=False, minutes_saved=-18, minutes_after_original=34)
    body = _push_body(late, **_DELAYED)
    assert body.endswith("Alternativ tog R11 08:24, framme kl 09:05 – 34 min senere enn planlagt ankomst")
    body = _deterministic_body(varsel._message_input_from_alert(_cancelled_alert(late)))
    assert body.endswith("– 34 min senere enn planlagt ankomst")
    # An input predating the figure keeps the wording it had.
    without = _alternative(arrives_before_original=False, minutes_saved=-18)
    assert _push_body(without, **_DELAYED).endswith(
        "Alternativ tog R11 08:24, framme kl 09:05 – senere enn planlagt ankomst"
    )
    in_time = varsel._message_input_from_alert(_cancelled_alert(_alternative(minutes_after_original=None)))
    assert "minutter_senere" not in in_time["alternativ_reise"]


def _cancelled_alert(alternative):
    return Alert(
        situation_number="S1", summary="Signalfeil", operator="VYG", line="VYG:Line:R10",
        departure_time=MON_0800, cancelled=True, cancel_probability=1.0,
        delay_p50_min=None, delay_p90_min=None, alert_threshold=0.65, alertworthy=True,
        alternative_journey=alternative,
    )


# ---- the extraction is a no-op for /varsel (U1 characterization) -------------------------------
def test_every_field_of_a_matched_alert_is_what_it_was_before_the_wording_moved():
    """The full row, not a spot check.

    U1 moved the wording out of this module and routed the alert gate through `alert_policy.gate`.
    Both are meant to be no-ops for `/varsel`, and a no-op is only demonstrable against the whole
    object: `alert_threshold` and `alertworthy` are recorded on every `varsel_decisions` row and
    are what `/historikk`'s precision is computed over, `jkey` / `stop_place` / `stop_order` are
    the ET join key the nightly labelling job uses, and `mode` is read straight into two wordings.
    A silent change in any of them is a changed decision log.
    """
    install({"S1": _train(MON_0800, jkey="j1")})
    alerts = varsel.match_journey(_journey(), NOW)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.model_dump(
        include={
            "situation_number", "summary", "operator", "mode", "has_situation", "line",
            "departure_time", "jkey", "stop_place", "stop_order", "cancelled",
            "cancel_probability", "delay_p50_min", "delay_p90_min", "ge10_probability",
            "impact_probability", "alert_threshold", "alertworthy",
        }
    ) == {
        "situation_number": "S1",
        "summary": "Signalfeil",
        "operator": "VYG",
        "mode": "Tog",
        "has_situation": True,
        "line": "VYG:Line:R10",
        "departure_time": MON_0800,
        "jkey": "j1",
        "stop_place": "A",
        "stop_order": 1,
        "cancelled": False,
        "cancel_probability": 0.5,
        "delay_p50_min": 3,
        "delay_p90_min": 12,
        "ge10_probability": 0.8,
        "impact_probability": 0.9,
        "alert_threshold": 0.65,
        "alertworthy": True,
    }
    assert alert.decided_at == NOW


def test_the_gate_is_reached_rather_than_restated_at_this_surface():
    """One threshold contract, two adapters (KTD1/KTD2).

    `alert_policy.gate` is `threshold_for` plus the comparison — including the rule that a missing
    probability is never alert-worthy. Restating `>= threshold` here is how a second surface
    quietly acquires a cancellation-probability fallback, or starts reading a NULL as a low score;
    the two severity orders drifted exactly that way before `impact` was extracted.

    Both halves matter, so both are asserted: the threshold this surface RECORDS and the verdict it
    APPLIES come out of one call. Asking twice is how the recorded bar and the applied bar come to
    be two different numbers on one decision row.
    """
    import inspect

    source = inspect.getsource(varsel._collect_alerts)
    assert "gate(impact_p, sit.operator" in source
    assert ">= threshold" not in source
    assert varsel.gate is alert_policy.gate, "reached, not copied"

    install({"S1": _train(MON_0800, jkey="j1", p_impact_dep=0.9)})
    alert = varsel.match_journey(_journey(), NOW)[0]
    assert (alert.alert_threshold, alert.alertworthy) == alert_policy.gate(
        alert.impact_probability, alert.operator, has_situation=alert.has_situation
    )


def test_the_wording_varsel_sends_is_the_shared_one_and_not_a_copy():
    """`main` injects `varsel._deterministic_body` into `/meldinger`, and two suites assert push
    bodies against it. The alias must BE the shared function: two implementations that agree today
    would split every wording comparison in the decision log the first time one of them changed."""
    import notification_wording

    assert varsel._deterministic_body is notification_wording.deterministic_body
    assert varsel._notification_title is notification_wording.notification_title
    assert varsel._train_label is notification_wording.train_label
    assert varsel._delay_phrase is notification_wording.delay_phrase
    assert varsel.mode_label is notification_wording.mode_label


# ---- The fetch path and the answered flag (the journey-alert pull read, 2026-09-10) ----------
NSR_A = "NSR:StopPlace:337"
NSR_B = "NSR:StopPlace:418"


def _nsr_journey():
    """An all-day, every-day registration between two NSR-shaped refs: `fetch_candidates` refuses
    a non-NSR ref before it reaches the budget, so a ledger test on `"A"`/`"B"` proves nothing."""
    return {**_journey(begin="00:00", end="23:59", days=range(7)),
            "from_stop": NSR_A, "to_stop": NSR_B}


def _nsr_train(dep, **board_kw):
    return [
        FakeCall(jkey="t1", stop_place=NSR_A, stop_order=1, aimed_dep=dep, **board_kw),
        FakeCall(jkey="t1", stop_place=NSR_B, stop_order=5, aimed_dep=None,
                 aimed_arr=dep + timedelta(minutes=40)),
    ]


def _install_budgeted_fetch(fake):
    """Stub the network one level BELOW `fetch_candidates` (test_alternatives' `_install_fetch`),
    so a lookup reaches `_reserve_fetch`; returns what to restore."""
    saved = (alternatives._fetch_trip_patterns, alternatives._cache, alternatives._fetch_calls)
    alternatives._fetch_trip_patterns = fake
    alternatives._cache = alternatives._KeyedTTLCache(alternatives._TTL, alternatives._FAILURE_COOLDOWN)
    alternatives._fetch_calls = {path: [] for path in alternatives._FETCH_CEILINGS}
    return saved


def _restore_budgeted_fetch(saved):
    alternatives._fetch_trip_patterns, alternatives._cache, alternatives._fetch_calls = saved


def test_match_journey_names_the_budget_its_lookups_spend():
    """`path` reaches the provider on EVERY call, keyword-only, and defaults to the poller's value
    so every existing caller is unchanged. Without it the pull read would spend the poller's
    Journey Planner budget — silently, since both refill every minute."""
    _install_snapshot({"S1": _train(MON_0800)})
    seen = []

    def provider(*, from_stop, to_stop, window_start, window_end, catchable_after=None,
                 with_alternative=False, min_impact=0.0, path="poller", plans=None):
        seen.append(path)  # falls off the end: _impact hands None back; the argument is the point

    varsel.configure(impact_provider=provider, stops_provider=list)
    try:
        varsel.match_journey(_journey(), NOW, with_alternative=True, path="pull")
        assert seen and set(seen) == {"pull"}, seen
        seen.clear()
        varsel.match_journey(_journey(), NOW, with_alternative=True)
        assert seen and set(seen) == {alternatives._POLLER_PATH}, seen
    finally:
        install({})


def test_the_real_provider_charges_the_pull_ledger():
    """`main._varsel_impact` hard-coded `"poller"` until 2026-09-10. A provider that ACCEPTS the
    keyword and ignores it satisfies the Protocol (CI runs no type checker) and every recording
    double alike; only the ledger can tell, so this asks the ledger through the real provider."""
    dep = datetime.now(tz=_OSLO) + timedelta(hours=2)
    _install_snapshot({"S1": _nsr_train(dep, p_cancel=0.8, p_impact_dep=0.9)})
    varsel.configure(impact_provider=main._varsel_impact, stops_provider=list)
    saved = _install_budgeted_fetch(lambda *a, **k: [])
    try:
        alerts = varsel.match_journey(_nsr_journey(), with_alternative=True, path="pull")
        assert alerts and alerts[0].alertworthy, "control: the train must be worth a lookup"
        assert alternatives._fetch_calls["pull"], "the real provider did not charge the pull ledger"
        assert alternatives._fetch_calls["poller"] == [], "and spent none of the poller's"
        assert alternatives._fetch_calls["request"] == [], "nor the public endpoint's"
    finally:
        _restore_budgeted_fetch(saved)
        install({})


def test_an_unanswered_lookup_reaches_the_alert():
    """`answered` travels from the outcome onto `alert.impact` through `_collect_alerts`' projection,
    and through the back-fill onto a collapsed-ref alert about the same train — the two places the
    other alternative fields are carried, and where a forgotten field silently reads as answered."""
    _install_snapshot({"S1": _train(MON_0800, p_cancel=0.8, p_impact_dep=0.9)})
    install(_snapshot.copy())
    original = impact._best_remaining_journey
    impact._best_remaining_journey = lambda cells, **kw: impact.AlternativeOutcome(
        None, False, ("VYG", "t1"), False
    )
    try:
        alerts = varsel.match_journey(_journey(), NOW, with_alternative=True)
        assert alerts and alerts[0].impact.alternative_subject == ("VYG", "t1")
        assert alerts[0].impact.answered is False, "the projection dropped the flag"

        # The back-fill: the worst reading comes from a collapsed ref (asked without a lookup), and
        # the canonical pass's whole outcome — the flag included — transfers onto it.
        _install_snapshot({"S1": [
            *_train(MON_0800, jkey="t1", p_cancel=0.30, p_impact_dep=0.55),
            *_twin(MON_0800, "A2", jkey="t1", p_cancel=0.80, p_impact_dep=0.95),
        ]})
        varsel.set_stop_aliases({"A2": "A"})
        try:
            alerts = varsel.match_journey(_journey(), NOW, with_alternative=True)
        finally:
            varsel.set_stop_aliases({})
        assert alerts[0].impact_probability == 0.95, "control: the collapsed reading won"
        assert alerts[0].impact.alternative_subject == ("VYG", "t1"), "control: back-filled"
        assert alerts[0].impact.answered is False, "the back-fill dropped the flag"
    finally:
        impact._best_remaining_journey = original
        install({})


# ---- U2 of the pull read: the read selects trains as the pass does, and knows a cold cache -------
def _sub_gate_no_sx(situation, *, jkey="t1", p_impact=0.80, dep=MON_0800):
    """A no-SX reading UNDER its own gate (0.85) that still outranks a published one over ITS gate
    (0.65) on severity -- the collapse keeps it, and the train is then not alert-worthy."""
    return Alert(
        situation_number=situation, summary="Mulig avvik", line="VYG:Line:R10", operator="VYG",
        jkey=jkey, stop_place="A", stop_order=1, departure_time=dep, has_situation=False,
        cancel_probability=0.3, delay_p50_min=None, delay_p90_min=None,
        impact_probability=p_impact, alert_threshold=alert_policy._NOSX_ALERT_THRESHOLD,
        alertworthy=False,
    )


def _train_of(subject_or_alert):
    return (
        subject_or_alert.operator,
        getattr(subject_or_alert, "journey_ref", None) or getattr(subject_or_alert, "jkey", None),
        subject_or_alert.stop_place,
        subject_or_alert.stop_order,
        subject_or_alert.departure_time,
    )


def test_alert_worthy_trains_is_the_collapse_then_the_gate_exactly_as_the_pass_iterates():
    """KTD7 / R2. The read must select trains in the notification pass's own order: collapse to
    one alert per train FIRST, then the push gate. The fixture is the case where the order shows:
    train t1 carries a published situation over its gate (0.70 > 0.65) and a no-SX reading under
    its own (0.80 < 0.85) that outranks it on severity, so the collapse keeps the sub-gate winner
    and t1 is NOT alert-worthy -- `_observations_for_key` iterates it as a withdrawal observation. A
    gate-first filter would list t1 through its published situation. Pinned against the pass's
    own loop rather than restated, and the order is the collapse's, which the read sorts after."""
    key = "A|B|07:00|09:00|0,1,2,3,4"
    alerts = [
        _alert("S_t2", jkey="t2", p_impact=0.95, dep=MON_0800 + timedelta(minutes=30)),
        _sub_gate_no_sx("KIX:NoSituation:VYG:t1"),
        _alert("S_t1_published", p_impact=0.70),
        _alert("S_t3", jkey="t3", p_impact=0.66, dep=MON_0800 + timedelta(minutes=60)),
    ]
    assert varsel.one_alert_per_train(alerts)[1].situation_number == "KIX:NoSituation:VYG:t1", (
        "control: the sub-gate no-SX reading wins the collapse for t1"
    )

    chosen = varsel.alert_worthy_trains(alerts)

    assert [a.situation_number for a in chosen] == ["S_t2", "S_t3"]
    assert "S_t1_published" not in {a.situation_number for a in chosen}, (
        "a gate-first filter would list t1 through its over-gate published situation"
    )
    assert [_train_of(a) for a in chosen] == [
        _train_of(a) for a in varsel.one_alert_per_train(alerts) if a.alertworthy
    ], "the collapse's own order is kept; the read sorts afterwards"

    # The pass's own loop on the same fixture: its alert-worthy observations are the helper's
    # trains, and t1 is there as a withdrawal observation -- present, and not alert-worthy.
    out: list = []
    recipient = varsel.WebhookRecipient("sub_1", _journey(), client_id="c1", destination_id="d1")
    unroutable = varsel._observations_for_key(
        out, key, [recipient], alerts_by_key={key: alerts}, match_targets={key: _journey()},
        message_inputs={}, webhook_llm=False,
    )
    assert unroutable == 0
    assert [_train_of(o) for o in out if o.alertworthy] == [_train_of(a) for a in chosen]
    t1 = [o for o in out if o.journey_ref == "t1"]
    assert len(t1) == 1 and t1[0].alertworthy is False, "t1 is the pass's withdrawal observation"


def test_the_freshness_provider_reads_none_before_the_first_load_and_the_timestamp_after():
    """R6: the read answers 503 on a prediction cache the poller has not loaded, and what tells a
    cold cache from a loaded one is `_call_pred_refreshed_at` -- None until the first rebuild --
    read under the same lock `_impact_snapshot` takes."""
    with main._call_pred_lock:
        previous = main._call_pred_refreshed_at
    loaded_at = datetime(2026, 7, 6, 5, 55, tzinfo=UTC)
    try:
        with main._call_pred_lock:
            main._call_pred_refreshed_at = None
        assert main._varsel_refreshed_at() is None
        with main._call_pred_lock:
            main._call_pred_refreshed_at = loaded_at
        assert main._varsel_refreshed_at() == loaded_at
        assert main._varsel_refreshed_at() == main._impact_snapshot()[1], (
            "one timestamp, read off the same snapshot /impact answers from"
        )
    finally:
        with main._call_pred_lock:
            main._call_pred_refreshed_at = previous


def _payload(alert):
    return varsel._webhook_payload(
        alert, [alert], message_input={"i_morgen": False}, title="t", body="b", summary=(None, None, None)
    )


def test_a_train_that_leaves_but_never_reaches_the_destination_is_a_confirmed_cancellation():
    """R14 on 2026-10-12: it left Oslo S 36 min late and every call past Skøyen, Asker included, was
    cancelled. For a traveller to Asker that is a cancelled TRIP, and since 2026-10-19 the alert
    says so the way a boarding cancellation does: the certainty in the figures, `effect`
    "cancelled", phase `confirmed` -- while `cancelled` stays the boarding call's own (False)."""
    board, alight = _train(MON_0800, p_cancel=0.10, p_impact_dep=0.97, p_dep_ge10=0.96)
    alight.et_declared_cancelled = True
    [alert] = match(_journey(), {"S1": [board, alight]})

    assert alert.cancelled is False and alert.destination_cancelled is True
    assert alert.trip_cancelled is True
    assert alert.alertworthy and alert.impact_probability == 1.0

    payload = _payload(alert)
    assert payload["arrival"] == {
        "stop_place": "B",
        "stop_order": 5,
        "aimed_arrival_time": (MON_0800 + timedelta(minutes=40)).isoformat(),
        "cancelled": True,
        "cancellation": 1.0,
        "delay_ge10": None,
        "delay": None,
    }
    # The schema's rule for a confirmed event: 1.0 and no delay figure, which all meant "if it runs".
    assert payload["probabilities"]["impact"] == 1.0
    assert payload["probabilities"]["cancellation"] == 1.0
    assert payload["probabilities"]["delay_ge10"] is None
    assert payload["delay"] is None
    assert payload["effect"] == "cancelled"
    # The wording says what happened and does not hedge it (both writers' input and the template).
    import notification_wording

    message_input = varsel._message_input_from_alert(alert)
    assert message_input.get("kommer_ikke_fram") is True and "innstilt" not in message_input
    assert notification_wording.outcome_verb(message_input) == "kjører ikke helt fram"
    assert varsel._feedback_detail(alert).endswith(notification_wording.DESTINATION_CANCELLED)


def test_a_cancelled_destination_alerts_a_train_whose_departure_looked_fine():
    """The widening, proved to fire for its own reason: 0.30 at boarding is far under the bar, and
    the alert comes from ET's declared cancellation of the destination call and nothing else."""
    board, alight = _train(MON_0800, p_cancel=0.02, p_impact_dep=0.30)
    [quiet] = match(_journey(), {"S1": [board, alight]})
    assert not quiet.alertworthy and not quiet.trip_cancelled

    alight.et_declared_cancelled = True
    [alert] = match(_journey(), {"S1": [board, alight]})
    assert alert.alertworthy and alert.destination_cancelled


def test_a_cancelled_intermediate_stop_does_not_cancel_the_trip():
    """Endpoints only: a stop the train skips between the traveller's two stops strands nobody."""
    board, alight = _train(MON_0800, p_cancel=0.02, p_impact_dep=0.30)
    skipped = FakeCall(jkey="t1", stop_place="M", stop_order=3, aimed_dep=MON_0800 + timedelta(minutes=20),
                       aimed_arr=MON_0800 + timedelta(minutes=19), et_declared_cancelled=True)
    [alert] = match(_journey(), {"S1": [board, skipped, alight]})
    assert not alert.destination_cancelled and not alert.alertworthy


def test_a_train_cut_short_outranks_a_delayed_sibling_on_the_same_line():
    """`worst` keeps one train per line, and it ranks the TRIP: a train that will not reach the
    destination must win over a merely delayed one, or it is dropped before any gate sees it."""
    delayed = _train(MON_0800, jkey="delayed", p_impact_dep=0.90)
    cut = _train(MON_0800 + timedelta(minutes=30), jkey="cut", p_impact_dep=0.20)
    cut[1].et_declared_cancelled = True
    [alert] = match(_journey(), {"S1": delayed + cut})
    assert alert.jkey == "cut" and alert.destination_cancelled


def test_a_train_cut_short_is_a_confirmed_webhook_observation():
    """The phase follows the TRIP: the pass's own loop hands the emit step `cancelled=True` for a
    train whose destination call ET cancelled, which `requested_phase` turns into `confirmed`."""
    import notification_events
    import notification_store

    board, alight = _train(MON_0800)
    alight.et_declared_cancelled = True
    alerts = match(_journey(), {"S1": [board, alight]})
    key = "A|B|07:00|09:00|0,1,2,3,4"
    out: list = []
    recipient = varsel.WebhookRecipient("sub_1", _journey(), client_id="c1", destination_id="d1")
    varsel._observations_for_key(
        out, key, [recipient], alerts_by_key={key: alerts}, match_targets={key: _journey()},
        message_inputs={}, webhook_llm=False,
    )
    [observation] = out
    assert observation.alertworthy and observation.cancelled is True
    subject = observation.subject(observation.recipients[0])
    assert notification_events.requested_phase(subject) == notification_store.PHASE_CONFIRMED


def test_arrival_is_the_boarded_journeys_own_never_another_lines():
    """`SituationImpact.arrival_prediction` is the worst arrival across the situation's lines and
    may belong to a different train. Reading it would put another line's cancelled destination on
    this train's alert, so `arrival` must come from the train the boarding figures describe."""
    mine = _train(MON_0800, jkey="mine", p_impact_dep=0.95, line="VYG:Line:R14")
    mine[1].p_cancel = 0.05
    theirs = _train(MON_0800 + timedelta(minutes=10), jkey="theirs", p_impact_dep=0.40,
                    line="VYG:Line:R13")
    # A bad arrival on the other line, but not a cancelled one: a cancelled destination would make
    # THAT train the worst trip (2026-10-19), which is a different rule with its own test.
    theirs[1].p_cancel, theirs[1].p_impact = 0.60, 0.99
    [alert] = match(_journey(), {"S1": mine + theirs})

    assert alert.jkey == "mine"
    assert alert.impact.situations[0].arrival_prediction.cancel_probability == 0.60  # the other train's
    arrival = _payload(alert)["arrival"]
    assert arrival["cancelled"] is False and arrival["cancellation"] == 0.05


# ---- Multi-leg U5: trips with a change, merged into the match ----------------------------------
# `trips.judge` is test_trips' and `for_trip(plans=)` test_impact's. These pin what varsel does with
# the subjects: the flag, which call carries the plan, KTD3's merge, KTD20's standing alerts, the
# Alert's trip fields, and KTD8's withdrawal hold. Every scenario runs the REAL judge over this
# suite's snapshot unless it says otherwise.

import contextlib
import logging
import os

import trips
from alternatives import TripFacts, TripProblem


def _snapshot_calls(operator, jkey):
    """The trip verdict's call lookup: the rows of whatever snapshot the engine is reading, by
    journey, as `main._impact_journey_calls` serves the poller's own (test_impact's helper)."""
    snapshot, _stamp = impact._snapshot_provider()
    found = [c for calls in snapshot.values() for c in calls if (c.operator, c.jkey) == (operator, jkey)]
    return found or None


trips.configure(call_lookup=_snapshot_calls, places=varsel.place_refs)

MON_0717 = _oslo(2026, 7, 6, 7, 17)
MONDAY = MON_0717.date()


def _plan_leg(jkey, board, alight, dep, arr, *, operator="VYG", mode="rail", position=0):
    return trips.PlanLeg(
        operator=operator, refs=((operator, jkey),), line=f"{operator}:Line:R12",
        public_code="R12", mode=mode, board_stop_place=board, alight_stop_place=alight,
        board_quay=None, alight_quay=None, board_position=position, alight_position=position + 2,
        aimed_departure=dep, aimed_arrival=arr, service_date=dep.astimezone(_OSLO).date(),
    )


def _change(dep=MON_0717, *, first="t17", second="t30"):
    """A -> C on `first` (7 min), a 6-minute change at C, then C -> B on `second` (35 min)."""
    return trips.PlanTrip(legs=(
        _plan_leg(first, "A", "C", dep, dep + timedelta(minutes=7)),
        _plan_leg(second, "C", "B", dep + timedelta(minutes=13), dep + timedelta(minutes=48)),
    ))


def _change_calls(dep=MON_0717, *, first="t17", second="t30", situation="S9", board=None,
                  connecting=None, scored_first=True, scored_second=True):
    """`_change`'s two trains as the cache scores them: the first train's boarding figure (0.9 by
    default) and a small P(miss) at C, the connecting train's own low figure."""
    calls = []
    if scored_first:
        calls += [
            FakeCall(first, "A", 1, dep, situation_number=situation,
                     **{"p_impact_dep": 0.9, **(board or {})}),
            FakeCall(first, "C", 3, None, aimed_arr=dep + timedelta(minutes=7),
                     situation_number=situation, p_arr_ge5=0.1, p_arr_ge10=0.05),
        ]
    if scored_second:
        calls += [
            FakeCall(second, "C", 1, dep + timedelta(minutes=13), situation_number=situation,
                     **{"p_impact_dep": 0.1, "p_dep_ge10": 0.1, **(connecting or {})}),
            FakeCall(second, "B", 4, None, aimed_arr=dep + timedelta(minutes=48),
                     situation_number=situation),
        ]
    return calls


@contextlib.contextmanager
def _trips_on(plans, *, enabled=True):
    """TRIP_PLANS_ENABLED and the injected plan reader, over `{(from, to, Oslo date): DayPlan}`.
    Entered AFTER `install`, whose `varsel.configure` resets the reader."""
    before_env = os.environ.get("TRIP_PLANS_ENABLED")
    before = varsel._trip_plans
    if enabled:
        os.environ["TRIP_PLANS_ENABLED"] = "true"
    else:
        os.environ.pop("TRIP_PLANS_ENABLED", None)
    varsel._trip_plans = lambda from_stop, to_stop, day: plans.get((from_stop, to_stop, day))
    try:
        yield
    finally:
        varsel._trip_plans = before
        if before_env is None:
            os.environ.pop("TRIP_PLANS_ENABLED", None)
        else:
            os.environ["TRIP_PLANS_ENABLED"] = before_env
        with varsel._state_lock:
            varsel._trip_reasons.clear()


def _monday(plan):
    return {("A", "B", MONDAY): plan}


def test_a_trip_with_a_change_joins_the_match_beside_the_direct_trains():
    """KTD3: the direct train is judged as today, and the departure with a change is added after the
    per-situation selection with its trip facts, its pinned identity (KTD5) and its deciding pair."""
    install({"S1": _train(MON_0800), "S9": _change_calls()})
    with _trips_on(_monday(trips.DayPlan(trips=(_change(),)))):
        alerts = varsel.match_journey(_journey(), NOW)
    install({})
    by = {a.situation_number: a for a in alerts}
    assert set(by) == {"S1", "S9"}, alerts
    trip = by["S9"]
    assert [leg.board_stop_place for leg in trip.trip.legs] == ["A", "C"]
    assert (trip.operator, trip.jkey, trip.stop_place, trip.stop_order, trip.departure_time) == (
        "VYG", "t17", "A", 1, MON_0717,
    )
    assert (trip.deciding_probability, trip.deciding_threshold) == (0.9, 0.65)
    assert trip.alertworthy and not trip.trip_cancelled and not trip.trip_confirmed
    assert (trip.trip.problem.kind, trip.trip.problem.index) == ("departure", 0)
    direct = by["S1"]
    assert direct.trip is None and direct.deciding_probability is None, "a direct train is today's"


def test_with_the_flag_off_no_plan_is_read_and_the_match_is_todays():
    """AE6: off means the plan reader is never asked, the provider is sent exactly today's keywords,
    and the alerts are byte-identical to a match with no plan anywhere."""
    install({"S1": _train(MON_0800), "S9": _change_calls()})
    baseline = varsel.match_journey(_journey(), NOW)
    asked, sent = [], []
    real = varsel._impact_provider

    def recording(**kw):
        sent.append(set(kw))
        return real(**kw)

    varsel._impact_provider = recording
    with _trips_on(_monday(trips.DayPlan(trips=(_change(),))), enabled=False):
        reader = varsel._trip_plans
        varsel._trip_plans = lambda *key: asked.append(key) or reader(*key)
        alerts = varsel.match_journey(_journey(), NOW)
    install({})
    assert asked == [], "no plan read with the flag off"
    assert sent and all("plans" not in keywords for keywords in sent), sent
    assert [a.model_dump_json() for a in alerts] == [a.model_dump_json() for a in baseline]


def _subject(dep, jkey="t2", *, situation="S9", alertworthy=True, admitted=True, complete=True,
             unknown=(), kind="departure", index=0, probability=0.9, confirmed=False):
    """A hand-made `impact.TripSubject`, for the merge's own rules apart from the judge's."""
    problem = (
        TripProblem(kind=kind, index=index, probability=probability, threshold=0.65)
        if alertworthy else None
    )
    return impact.TripSubject(
        situation_number=situation, summary="Feil i Drammen", description=None, has_situation=True,
        report_type=None, operator="VYG", line="VYG:Line:R12", jkey=jkey, stop_place="A",
        stop_order=1, departure_time=dep, departure_prediction=None,
        trip=TripFacts(legs=[], transfers=[], problem=problem), deciding_probability=probability,
        deciding_threshold=0.65, alertworthy=alertworthy, confirmed=confirmed, admitted=admitted,
        complete=complete, unknown_legs=unknown,
    )


def _install_subjects(by_situation, subjects, asked=None):
    """The real provider, with `subjects` attached to every response the plan rides on."""
    _install_snapshot(by_situation)

    def provider(*, from_stop, to_stop, window_start, window_end, catchable_after=None,
                 with_alternative=False, min_impact=0.0, path="poller", plans=None):
        if asked is not None:
            asked.append((from_stop, to_stop, plans is not None))
        response = impact.for_trip(
            from_stop=from_stop, to_stop=to_stop, window_start=window_start,
            window_end=window_end, select="worst", catchable_after=catchable_after,
            with_alternative=with_alternative, min_impact=min_impact, path=path,
        )
        if plans is not None:
            response._trip_subjects = [
                s for s in subjects() if window_start <= s.departure_time <= window_end
            ]
        return response

    varsel.configure(impact_provider=provider, stops_provider=list)


def test_ae7_a_direct_alert_on_the_same_departure_wins_over_the_trip():
    """KTD3's key is source-independent: (operator, journey, aimed departure at the from-stop). A
    subject the judge returned for a departure the direct path already alerts on adds nothing."""
    _install_subjects({"S1": _train(MON_0800)}, lambda: [_subject(MON_0800, jkey="t1")])
    with _trips_on(_monday(trips.DayPlan())):
        alerts = varsel.match_journey(_journey(), NOW)
    install({})
    assert [(a.situation_number, a.trip) for a in alerts] == [("S1", None)]


def test_a_trip_alert_carries_its_own_alternative_as_a_direct_alert_does():
    """Multi-leg U6: `impact` looks a departure with a change up itself (KTD10), and its Alert
    carries the answer where a direct Alert does -- on the Alert, and on the response copy it
    carries, which the decision log, the caches and the wording key read."""
    offer = alternatives.AlternativeJourney(
        departure_time=MON_0717, arrival_time=MON_0717 + timedelta(minutes=70), minutes_saved=-12,
    )
    subject = _subject(MON_0717).model_copy(update={
        "alternative_journey": offer, "alternative_subject": ("VYG", "t2"), "answered": False,
    })
    _install_subjects({"S1": _train(MON_0800)}, lambda: [subject])
    with _trips_on(_monday(trips.DayPlan())):
        alerts = varsel.match_journey(_journey(), NOW)
    install({})
    [trip] = [alert for alert in alerts if alert.trip is not None]
    assert trip.alternative_journey == offer
    copy = trip.impact
    assert (copy.alternative_journey, copy.original_is_best, copy.alternative_subject,
            copy.answered) == (offer, False, ("VYG", "t2"), False)


def test_a_trip_ktd20_no_longer_admits_stays_only_while_the_definition_holds_a_standing_alert():
    """KTD20: a lifted cancellation must never leave a standing alert without a subject to
    withdraw it. Standing is asked lazily, only about a subject KTD20 does not admit."""
    subject = _subject(MON_0717, admitted=False)
    _install_subjects({"S1": _train(MON_0800)}, lambda: [subject])
    asked: list = []

    def standing(answer):
        def stands(alert):
            asked.append(alert.jkey)
            return answer
        return stands

    with _trips_on(_monday(trips.DayPlan())):
        alone = varsel.match_journey(_journey(), NOW)
        held = varsel.match_journey(_journey(), NOW, standing=standing(True))
        gone = varsel.match_journey(_journey(), NOW, standing=standing(False))
        subject = _subject(MON_0717, admitted=True)
        admitted = varsel.match_journey(_journey(), NOW, standing=standing(False))
    install({})
    assert [a.situation_number for a in alone] == ["S1"], "no standing state: an alternative at most"
    assert {a.situation_number for a in held} == {"S1", "S9"}
    assert [a.situation_number for a in gone] == ["S1"]
    assert {a.situation_number for a in admitted} == {"S1", "S9"}
    assert asked == ["t2", "t2"], "asked once per non-admitted match, never about an admitted one"


def test_the_plan_rides_on_the_journeys_own_pair_and_a_parent_destination_gets_its_own_call():
    """A child ref expands only to its alias group, so the plan rides on the call whose `to_stop`
    is the journey's own (KTD3's direct check reads every ref of it). With ordinary refs that is the
    first pair, at no extra call; a parent to-ref is named by no pair, so with a plan in hand it gets
    one call of its own -- whose direct half is empty, a parent carrying no calls."""
    asked: list = []
    _install_subjects({}, list, asked)
    with _trips_on(_monday(trips.DayPlan())):
        varsel.match_journey(_journey(), NOW)
        assert asked[0] == ("A", "B", True) and len(asked) == 2, asked  # Monday's plan, Tuesday's none
        asked.clear()
        varsel.set_stop_aliases({}, parents={"P": ("B1", "B2")})
        varsel._trip_plans = lambda f, t, d: trips.DayPlan() if (f, t, d) == ("A", "P", MONDAY) else None
        try:
            varsel.match_journey(_journey() | {"to_stop": "P"}, NOW)
        finally:
            varsel.set_stop_aliases({})
    install({})
    assert asked[:3] == [("A", "B1", False), ("A", "B2", False), ("A", "P", True)], asked
    assert asked[3:] == [("A", "B1", False), ("A", "B2", False)], "no plan, no extra call"


def test_the_trips_are_looked_up_on_the_call_their_plan_rides():
    """Multi-leg U6: the lookup for a departure with a change runs inside the call that judges it.
    The direct lookup stays on the first pair only; a parent destination's own call, which carries
    the plan and no direct train, is asked with it too."""
    _install_subjects({}, list)
    real, sent = varsel._impact_provider, []
    varsel._impact_provider = lambda **kw: sent.append((kw["to_stop"], kw["with_alternative"])) or (
        real(**kw)
    )
    with _trips_on({}):
        varsel.set_stop_aliases({}, parents={"P": ("B1", "B2")})
        varsel._trip_plans = lambda f, t, d: trips.DayPlan() if (f, t, d) == ("A", "P", MONDAY) else None
        try:
            varsel.match_journey(_journey() | {"to_stop": "P"}, NOW, with_alternative=True)
        finally:
            varsel.set_stop_aliases({})
    install({})
    assert sent[:3] == [("B1", True), ("B2", False), ("P", True)], sent
    assert sent[3:] == [("B1", True), ("B2", False)], "no plan, no call of its own"


def test_a_confirmed_trip_is_trip_cancelled_and_carries_the_certainty_even_unscored():
    """KTD19: a declared cancellation of the second leg's boarding call, with no continuation to
    cushion it, is confirmed. `trip_cancelled` follows (so `phase` does), `cancelled` stays the
    boarding call's, and the figures are the destination-cancelled overwrite -- also when we score
    no call of the first leg, where there is no figure of ours to overwrite (KTD6)."""
    for scored_first in (True, False):
        install({"S9": _change_calls(board={"p_impact_dep": 0.1}, scored_first=scored_first,
                                     connecting={"et_declared_cancelled": True})})
        with _trips_on(_monday(trips.DayPlan(trips=(_change(),)))):
            [trip] = varsel.match_journey(_journey(), NOW)
        install({})
        assert trip.trip_confirmed and trip.trip_cancelled and not trip.cancelled, scored_first
        assert (trip.impact_probability, trip.cancel_probability) == (1.0, 1.0), scored_first
        assert (trip.delay_p50_min, trip.delay_p90_min) == (None, None), scored_first
        assert (trip.trip.problem.kind, trip.trip.problem.index) == ("leg_cancelled", 1)


def test_a_first_train_that_never_leaves_is_worded_cancelled_never_a_missed_change():
    """A confirmed shared reason on a first leg we do not score: Journey Planner's real-time has its
    boarding call cancelled, or Vy took the train out of today's plan. The first vehicle never
    leaves, so the alert is `cancelled` with the certainty, and the title, the body and the card's
    detail say «er innstilt» as for a scored first train ET has cancelled -- never «mister
    overgangen, ankomst ukjent»."""
    import notification_wording

    first = "DatedServiceJourney:1617_X"
    removed_jkey = f"RemovedJourney:1617_{MONDAY.isoformat()}"
    removed = notification_wording.plan_removed_situation(removed_jkey)
    out_of_plan = {removed: [FakeCall(removed_jkey, "A", 1, MON_0717, situation_number=removed,
                                      et_declared_cancelled=True, has_situation=False,
                                      p_impact_dep=1.0)]}

    def jp_cancelled(leg):
        return trips.LegRealtime(board_cancelled=True) if leg.refs[0][1] == first else None

    for realtime, extra, kind in ((jp_cancelled, {}, "leg_cancelled"), (None, out_of_plan, "leg_removed")):
        install({"S9": _change_calls(first=first, scored_first=False, board=_calm_board()), **extra})
        trips.configure(call_lookup=_snapshot_calls, places=varsel.place_refs, realtime=realtime)
        try:
            with _trips_on(_monday(trips.DayPlan(trips=(_change(first=first),)))):
                [trip] = varsel.match_journey(_journey(), NOW)
        finally:
            trips.configure(call_lookup=_snapshot_calls, places=varsel.place_refs)
            install({})
        assert (trip.trip.problem.kind, trip.trip.problem.index) == (kind, 0), kind
        assert trip.cancelled and trip.trip_confirmed, kind
        assert (trip.impact_probability, trip.cancel_probability, trip.delay_p50_min) == (1.0, 1.0, None)
        mi = _message_input(_journey(), trip)
        title = notification_wording.notification_title(mi, "Oslo S", "Lillehammer", confirmed=True)
        assert mi["innstilt"] is True and "er innstilt" in title and "overgangen" not in title, title
        assert "ankomst ukjent" not in title and "Ankomst ukjent" not in _deterministic_body(mi)
        assert varsel._feedback_detail(trip).endswith("Innstilt"), kind


def test_an_unscored_first_leg_has_no_figures_and_ranks_on_the_deciding_pair():
    """KTD6: no figure of ours for the boarding call, so none is made up -- and `_alert_severity`
    ranks the subject on its deciding figure, so a broken change never sorts as a calm train."""
    install({"S1": _train(MON_0800, p_impact_dep=0.7),
             "S9": _change_calls(scored_first=False, connecting={"p_impact_dep": 0.95})})
    with _trips_on(_monday(trips.DayPlan(trips=(_change(),)))):
        alerts = varsel.match_journey(_journey(), NOW)
    install({})
    assert [a.situation_number for a in alerts] == ["S9", "S1"], "worst first, on the deciding pair"
    trip = alerts[0]
    assert (trip.impact_probability, trip.cancel_probability, trip.ge10_probability) == (None,) * 3
    assert varsel._alert_severity(trip) == (0.95, 0, 0.0)
    assert (trip.trip.problem.kind, trip.trip.problem.index) == ("leg_departure", 1)
    assert [a.situation_number for a in varsel.one_alert_per_train(alerts)] == ["S9", "S1"]


def test_a_raise_while_merging_the_trips_costs_that_definition_its_trips_only():
    """Decision 7: the direct alerts still come back, and the record names the error class only
    (a message could quote a stop, R17)."""
    install({"S1": _train(MON_0800), "S9": _change_calls()})
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    sink = _Sink()
    varsel.logger.addHandler(sink)
    real = varsel._trip_alert
    varsel._trip_alert = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop NSR:StopPlace:A"))
    try:
        with _trips_on(_monday(trips.DayPlan(trips=(_change(),)))):
            alerts = varsel.match_journey(_journey(), NOW)
    finally:
        varsel._trip_alert = real
        varsel.logger.removeHandler(sink)
        install({})
    assert [a.situation_number for a in alerts] == ["S1"]
    [record] = [r for r in records if getattr(r, "fields", {}).get("event") == "trip_merge_error"]
    assert record.fields["error"] == "RuntimeError"
    assert "NSR" not in record.getMessage() and not record.exc_info


def test_the_trip_alert_hands_the_writer_its_own_situation_and_no_other():
    """`build_message_input` falls back to EVERY situation of the response when the alert's own is
    not in it, and a trip's situation is usually not in the direct, `to_stop`-narrowed response. The
    trip alert's response copy carries only its own, so the direct train's text never reaches the
    prompt as though it were the trip's."""
    direct = _train(MON_0800, summary="Signalfeil ved Lysaker")
    install({"S1": direct, "S9": _change_calls()})
    with _trips_on(_monday(trips.DayPlan(trips=(_change(),)))):
        trip = next(a for a in varsel.match_journey(_journey(), NOW) if a.trip is not None)
    install({})
    assert "Lysaker" not in str(_message_input(_journey(), trip))
    assert trip.impact.alternative_subject is None and trip.impact.answered
    assert not trip.impact.original_is_best and trip.impact.alternative_journey is None


def test_an_unknown_leg_holds_the_withdrawal_only_of_a_subject_standing_on_it():
    """KTD8. A subject standing on a reason that sits on a leg now unknown is not withdrawn however
    many cycles stay unknown; one standing on the first train's figure withdraws by today's rules
    when that figure falls; and a new leader, which knows nothing of what a subject stood on,
    withdraws nothing an unknown leg could have changed."""
    def cycle(subject):
        _install_subjects({}, lambda: [subject])
        with _trips_on(_monday(trips.DayPlan())):
            varsel._trip_reasons.update(memory)
            # As the pass asks: only it remembers a reason (`standing`, never asked of an admitted one).
            [alert] = varsel.match_journey(_journey(), NOW, standing=lambda _alert: False)
            memory.clear()
            memory.update(varsel._trip_reasons)
        return alert

    calm = {"alertworthy": False, "complete": False, "unknown": (1,), "probability": 0.2}
    memory: dict = {}
    assert not cycle(_subject(MON_0717, kind="missed_connection", index=0))._hold_withdrawal
    assert cycle(_subject(MON_0717, **calm))._hold_withdrawal
    assert cycle(_subject(MON_0717, **calm))._hold_withdrawal, "two cycles of unknown, still held"

    memory.clear()
    cycle(_subject(MON_0717, kind="departure", index=0, complete=False, unknown=(1,)))
    assert not cycle(_subject(MON_0717, **calm))._hold_withdrawal, "withdraws as today"

    memory.clear()  # a new leader
    assert cycle(_subject(MON_0717, **calm))._hold_withdrawal
    assert not cycle(_subject(MON_0717, alertworthy=False, probability=0.2))._hold_withdrawal, (
        "a complete verdict withdraws by today's rules whatever was remembered"
    )
    install({})


def test_only_the_pass_remembers_what_a_trip_alerted_on():
    """`_trip_reasons` is pruned by the leader's pass alone, so a request path (the card, a public
    read: no `standing`) on any pod reads it and never writes it -- or a follower's would grow for
    the life of the pod, one entry per alerting departure any caller asked about."""
    _install_subjects({}, lambda: [_subject(MON_0717, kind="missed_connection", index=0)])
    with _trips_on(_monday(trips.DayPlan())):
        [alert] = varsel.match_journey(_journey(), NOW)
        assert alert.trip is not None and varsel._trip_reasons == {}
        varsel.match_journey(_journey(), NOW, standing=lambda _alert: False)
        assert list(varsel._trip_reasons.values()) == [("missed_connection", 0)]
    install({})


def test_a_held_withdrawal_is_no_webhook_observation():
    """What the hold MEANS downstream: no observation reaches the emit step, so nothing counts
    toward the subject's withdrawal run, while the same reading unheld is an ordinary one."""
    _install_subjects({}, lambda: [
        _subject(MON_0717, alertworthy=False, complete=False, unknown=(1,), probability=0.2)
    ])
    with _trips_on(_monday(trips.DayPlan())):
        [alert] = varsel.match_journey(_journey(), NOW)
    install({})
    key = "A|B|07:00|09:00|0,1,2,3,4"
    recipient = varsel.WebhookRecipient("sub_1", _journey(), client_id="c1", destination_id="d1")

    def observed():
        out: list = []
        varsel._observations_for_key(
            out, key, [recipient], alerts_by_key={key: [alert]}, match_targets={key: _journey()},
            message_inputs={}, webhook_llm=False,
        )
        return [observation.alertworthy for observation in out]

    assert alert._hold_withdrawal and observed() == []
    alert._hold_withdrawal = False
    assert observed() == [False], "control: unheld, the reading is a withdrawal observation"


def test_the_thinning_ledger_tracks_the_figure_the_row_records():
    """The row records a trip's deciding figure as `impact_probability` (KTD6), and
    `max_impact_probability` is a MAX over rows, so the thinning rule's `new_max` must be asked of
    that same figure: a missed change climbing past the last peak is a row even while the calm
    boarding call's own figure stays below it."""
    trip = varsel.Alert(
        situation_number="S9", summary=None, line=None, departure_time=MON_0717,
        cancel_probability=0.05, delay_p50_min=None, delay_p90_min=None, impact_probability=0.3,
        alert_threshold=0.65, alertworthy=True, deciding_probability=0.9, deciding_threshold=0.65,
    )
    entry = {"alertworthy": True, "max_impact": 0.5, "written_at": NOW}
    assert varsel._thinning_reason(entry, trip, pushed=False, now=NOW) == "new_max"
    direct = trip.model_copy(update={"deciding_probability": None, "deciding_threshold": None})
    assert varsel._thinning_reason(entry, direct, pushed=False, now=NOW) is None, "control"


def test_a_reason_change_on_a_thinned_cycle_writes_a_fresh_row():
    """Multi-leg KTD11: the reason kind and the leg or transfer index are a thinning keep-reason, so
    a standing trip subject whose problem moves -- the departure figure, then a missed change on the
    same situation -- writes a row on the cycle it moves, with nothing else having changed. The
    ledger keeps the reason it last WROTE, so a lost write fires again; an entry from before the
    rule (no key) reads as no reason, which a direct train also has."""
    def alert(kind, index):
        return varsel.Alert(
            situation_number="S9", summary=None, line=None, departure_time=MON_0717,
            cancel_probability=0.05, delay_p50_min=None, delay_p90_min=None,
            impact_probability=0.3, alert_threshold=0.65, alertworthy=True,
            deciding_probability=0.9, deciding_threshold=0.65,
            trip=TripFacts(legs=[], transfers=[], problem=TripProblem(
                kind=kind, index=index, probability=0.9, threshold=0.65)),
        )

    entry = {"alertworthy": True, "max_impact": 0.9, "written_at": NOW,
             "trip_reason": ("departure", 0)}
    assert varsel._thinning_reason(entry, alert("departure", 0), pushed=False, now=NOW) is None
    assert varsel._thinning_reason(
        entry, alert("missed_connection", 0), pushed=False, now=NOW
    ) == "trip_reason"
    assert varsel._thinning_reason(
        entry, alert("departure", 1), pushed=False, now=NOW
    ) == "trip_reason", "the index alone moving is a reason change too"
    direct = alert("departure", 0).model_copy(update={"trip": None})
    assert varsel._thinning_reason(
        {**entry, "trip_reason": None}, direct, pushed=False, now=NOW
    ) is None, "control: a direct train has no reason to change"
    assert varsel._thinning_reason(
        {k: v for k, v in entry.items() if k != "trip_reason"}, direct, pushed=False, now=NOW
    ) is None, "an entry written before the rule"


def test_a_trip_alert_carries_every_continuation_to_its_decision_row():
    """U11's carrier: the siblings a verdict was taken over reach the decision row, never a wire.
    The Alert holds them excluded (as it holds its `impact`), so `/varsel-api` and every payload
    built off the Alert are unchanged; the row gets the snapshot, the planned route's P(miss) and
    the problem's kind and index -- the deciding pair stays in `impact_probability`."""
    import json

    import decisions

    by_situation = _sibling_calls()
    by_situation["S9"][1].p_arr_ge5 = by_situation["S9"][1].p_arr_ge10 = 0.9
    trip, _ = _trip_alert(by_situation, _sibling_plan())
    assert round(trip.trip_p_miss, 4) == 0.8226, "a read-off of 0.9 at a 6-minute change, corrected"
    assert {"trip_legs_snapshot", "trip_p_miss"}.isdisjoint(trip.model_dump()), "never on a wire"
    row = decisions.build_row({"from_stop": "A", "to_stop": "B"}, trip, journey_digest="d",
                              n_devices=1, pushed=False, message=None, message_source="deterministic")
    continuations = json.loads(row["trip_legs"])["continuations"]
    assert [[leg["alight_stop_place"] for leg in c["legs"]] for c in continuations] == [
        ["C", "B"], ["D", "B"],
    ], "the planned route first, then the sibling via D"
    assert (row["trip_reason_kind"], row["trip_reason_index"], round(row["trip_p_miss"], 4)) == (
        "missed_connection", 0, 0.8226,
    )
    assert (row["impact_probability"], row["alert_threshold"]) == (0.7, 0.65), "the deciding pair"


# ---- Multi-leg U8: the trip in the webhook payload ------------------------------------------------
# `payload.trip` is the dump of the one trip model, beside today's boarding-call figures; `effect`
# says `missed_connection` when the planned ride breaks at a change. The real judge, over this
# suite's snapshot, so the facts are the verdict's own and not a hand-made dict.


def _calm_board(**kw):
    """The first train's boarding call: under the bar and, by default, no delay figure at all."""
    return {"p_impact_dep": 0.1, "dep_delay_typ_min": 0, "dep_delay_p50_stack_min": 0,
            "dep_delay_p90_min": 0, "dep_delay_p90_stack_min": 0, **kw}


def _trip_alert(by_situation, plan):
    install(by_situation)
    with _trips_on(_monday(plan)):
        alerts = varsel.match_journey(_journey(), NOW)
    install({})
    trip = next(a for a in alerts if a.trip is not None)
    return trip, [a for a in alerts if a.trip is None]


def test_a_trip_subjects_payload_carries_the_trip_model_and_no_same_vehicle_arrival():
    """KTD13: `payload.trip` is `TripFacts` dumped -- legs, transfers, expected arrival, problem --
    and nothing else; `arrival` is null, because no one vehicle reaches the destination. The
    envelope's open sub-objects keep the direct train's exact key sets, and a direct train's
    payload has no `trip` key at all."""
    trip, [direct] = _trip_alert(
        {"S1": _train(MON_0800), "S9": _change_calls()}, trips.DayPlan(trips=(_change(),))
    )
    payload, plain = _payload(trip), _payload(direct)
    assert "trip" not in plain, "a direct train carries no trip key, not a null one"
    assert payload["trip"] == trip.trip.model_dump(mode="json")
    assert set(payload["trip"]) == {"legs", "transfers", "expected_arrival", "arrives_via", "problem"}
    assert payload["trip"]["arrives_via"] is None, "the planned route"
    assert [leg["board_stop_place"] for leg in payload["trip"]["legs"]] == ["A", "C"]
    assert [leg["data_source"] for leg in payload["trip"]["legs"]] == ["prediction", "prediction"]
    assert payload["trip"]["transfers"][0]["stop_place"] == "C"
    assert payload["trip"]["problem"] == {
        "kind": "departure", "index": 0, "probability": 0.9, "threshold": 0.65,
    }
    assert payload["arrival"] is None and trip.arrival_prediction is None
    assert set(payload) == set(plain) | {"trip"}, set(payload) ^ set(plain)
    for key in ("call", "probabilities", "notification"):
        assert set(payload[key]) == set(plain[key]), key


def test_a_predicted_missed_connection_keeps_the_boarding_figures_and_rides_its_own_in_the_problem():
    """KTD6: `probabilities` are the first train's own, which may be calm; P(miss) and its bar ride in
    `trip.problem`, with transfer 0 between legs 0 and 1. `effect` is `missed_connection` whatever
    the boarding delay -- none, or one today's rule would call `delayed`."""
    for board in (_calm_board(), _calm_board(dep_delay_p50_stack_min=4, dep_delay_p90_min=9)):
        calls = _change_calls(board=board)
        calls[1].p_arr_ge5 = calls[1].p_arr_ge10 = 0.9
        trip, _ = _trip_alert({"S9": calls}, trips.DayPlan(trips=(_change(),)))
        payload = _payload(trip)
        assert trip.alertworthy and not trip.trip_cancelled
        assert payload["probabilities"]["impact"] == 0.1, "the boarding call's own figure"
        problem = {**payload["trip"]["problem"]}
        assert round(problem.pop("probability"), 4) == 0.8226, "0.9 read off, corrected"
        assert problem == {"kind": "missed_connection", "index": 0, "threshold": 0.65}
        assert payload["effect"] == "missed_connection", board


def test_a_cancelled_later_leg_reads_missed_connection_predicted_and_cancelled_confirmed():
    """KTD13's effect rule both ways: a cancelled later leg with no cushion is a confirmed trip
    (`cancelled`, the destination-cancelled overwrite of 1.0 and null delays); the same leg
    beside a sibling only predicted to fail is `missed_connection`; and a later leg's own boarding
    figure keeps today's rule, since the ride does not break at the change."""
    gone = {"et_declared_cancelled": True}
    trip, _ = _trip_alert(
        {"S9": _change_calls(board=_calm_board(), connecting=gone)},
        trips.DayPlan(trips=(_change(),)),
    )
    payload = _payload(trip)
    assert trip.trip_confirmed and payload["effect"] == "cancelled"
    assert (payload["probabilities"]["impact"], payload["probabilities"]["cancellation"]) == (1.0, 1.0)
    assert payload["delay"] is None
    assert (payload["trip"]["problem"]["kind"], payload["trip"]["problem"]["index"]) == ("leg_cancelled", 1)

    trip, _ = _trip_alert(_sibling_calls(planned_connecting=gone), _sibling_plan())
    payload = _payload(trip)
    assert not trip.trip_cancelled and payload["effect"] == "missed_connection"
    assert payload["trip"]["problem"]["kind"] == "leg_cancelled"

    trip, _ = _trip_alert(
        {"S9": _change_calls(board=_calm_board(), connecting={"p_impact_dep": 0.9})},
        trips.DayPlan(trips=(_change(),)),
    )
    payload = _payload(trip)
    assert payload["trip"]["problem"]["kind"] == "leg_departure"
    assert payload["effect"] is None, "no boarding delay figure, so today's rule says nothing"


def _sibling_plan():
    """Two continuations of the 07:17 (KTD19): via C (planned, arrives 08:05) and via D (08:11,
    inside `TROUBLE_DELAY_MIN` of it, so it cushions)."""
    via_d = trips.PlanTrip(legs=(
        _plan_leg("t17", "A", "D", MON_0717, MON_0717 + timedelta(minutes=12)),
        _plan_leg("t40", "D", "B", MON_0717 + timedelta(minutes=20), MON_0717 + timedelta(minutes=54)),
    ))
    return trips.DayPlan(trips=(_change(), via_d))


def _sibling_calls(*, planned_connecting=None, sibling_board=0.7):
    calls = _change_calls(board=_calm_board(), connecting=planned_connecting)
    calls += [
        FakeCall("t17", "D", 4, None, aimed_arr=MON_0717 + timedelta(minutes=12),
                 situation_number="S9", p_arr_ge5=0.1, p_arr_ge10=0.05),
        FakeCall("t40", "D", 1, MON_0717 + timedelta(minutes=20), situation_number="S9",
                 p_impact_dep=sibling_board, p_dep_ge10=0.1),
        FakeCall("t40", "B", 5, None, aimed_arr=MON_0717 + timedelta(minutes=54),
                 situation_number="S9"),
    ]
    return {"S9": calls}


def test_a_figure_bound_by_a_sibling_names_the_planned_reason_beside_the_siblings_figure():
    """KTD13 under KTD19: the problem says what breaks the ride the traveller PLANNED (the miss at
    C, transfer 0), while the figure and bar beside it are the sibling's -- the later leg out of D at
    0.7, which clears by the least and so binds the verdict. `probabilities` stays the boarding
    call's."""
    by_situation = _sibling_calls()
    by_situation["S9"][1].p_arr_ge5 = by_situation["S9"][1].p_arr_ge10 = 0.9
    trip, _ = _trip_alert(by_situation, _sibling_plan())
    payload = _payload(trip)
    assert trip.alertworthy and (trip.deciding_probability, trip.deciding_threshold) == (0.7, 0.65)
    assert payload["trip"]["problem"] == {
        "kind": "missed_connection", "index": 0, "probability": 0.7, "threshold": 0.65,
    }
    assert payload["probabilities"]["impact"] == 0.1 and payload["effect"] == "missed_connection"


def test_an_unscored_first_leg_carries_null_figures_and_its_certainty_only_in_the_problem():
    """KTD6 and KTD8: a first leg we do not score has no figure of ours, so the pinned keys carry
    null; a miss by Journey Planner's own expected times is 1.0 and predicted, and that certainty
    rides in `trip.problem`, never in `probabilities` -- the confirmed-cancellation coherence rule
    the synthetic event and `wireContract.test.mjs` pin stays true of every predicted event."""
    subject = _subject(MON_0717, kind="missed_connection", index=0, probability=1.0)
    _install_subjects({}, lambda: [subject])
    with _trips_on(_monday(trips.DayPlan())):
        [trip] = varsel.match_journey(_journey(), NOW)
    install({})
    payload = _payload(trip)
    assert set(payload["probabilities"]) == {"impact", "cancellation", "delay_ge10", "alert_threshold"}
    assert payload["probabilities"]["impact"] is None
    assert payload["probabilities"]["cancellation"] is None
    assert payload["probabilities"]["delay_ge10"] is None
    assert payload["trip"]["problem"]["probability"] == 1.0
    assert payload["effect"] == "missed_connection" and not trip.trip_cancelled


def test_main_counts_the_plans_trip_situations_as_live_only_with_the_flag_on():
    """`main._varsel_live`: a trip reason judged from Journey Planner real-time attaches to a
    `KIX:Trip:` situation no prediction row carries, and the pass sweeps every mark, wording and
    decision subject whose situation is not live. With the flag off it is the snapshot itself."""
    plan = trips.DayPlan(trips=(_change(),))
    # A plan only a public read fetched: the pass matches through `read_plan`, which serves it too.
    public = trips.DayPlan(trips=(_change(first="t40", second="t50"),))
    real_held, real_public = main.trip_plans.held, main.trip_plans.public_plans
    main.trip_plans.held = lambda: [plan]
    main.trip_plans.public_plans = lambda: [public]
    try:
        with _trips_on({}, enabled=False):
            assert main._varsel_live() is main._varsel_snapshot()
        with _trips_on({}):
            live = main._varsel_live()
    finally:
        main.trip_plans.held, main.trip_plans.public_plans = real_held, real_public
    assert {"KIX:Trip:VYG:t17:2026-07-06", "KIX:Trip:VYG:t30:2026-07-06"} <= set(live)
    assert {"KIX:Trip:VYG:t40:2026-07-06", "KIX:Trip:VYG:t50:2026-07-06"} <= set(live)
    assert set(main._varsel_snapshot()) <= set(live)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} matcher checks passed.")
