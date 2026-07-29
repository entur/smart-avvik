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

import main
import varsel
from varsel import _OSLO, _notification_payload


@dataclass
class FakeCall:
    """Duck-types main._CallPred for the fields the matcher reads."""

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


def install(by_situation):
    """Point varsel at main's real /impact provider over this synthetic snapshot.

    `with_alternative=False` keeps the Journey Planner out of the matcher tests — the alternative
    lookup has its own hermetic suite in test_alternatives.py.
    """
    main._call_pred_by_situation = by_situation

    def provider(*, from_stop, to_stop, window_start, window_end):
        return main._impact_for_trip(
            from_stop=from_stop,
            to_stop=to_stop,
            window_start=window_start,
            window_end=window_end,
            select="worst",
            with_alternative=False,
        )

    varsel.configure(impact_provider=provider, stops_provider=list)


def match(journey, by_situation, now=NOW):
    install(by_situation)
    return varsel.match_journey(journey, now)


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
    """The Web Push body for the single match produced by a train with these boarding numbers."""
    journey = _journey()
    alerts = match(journey, {"S1": _train(MON_0800, **board_kw)})
    assert len(alerts) == 1
    return _notification_payload(journey, alerts[0])["body"]


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
    assert "93 % sjanse for innstilling eller ≥ 10 min forsinkelse" in body
    assert "typisk 15–27 min forsinket" in body
    assert "1 %" not in body and "2 min" not in body


def test_push_body_leads_with_cancellation_when_that_dominates():
    # Bus alerts are mostly cancellation-driven (p_cancel .75-.95): a small delay interval would be
    # beside the point for a train that probably isn't running.
    body = _push_body(operator="SKY", p_cancel=0.95, p_impact_dep=0.97,
                      dep_delay_p50_stack_min=3, dep_delay_p90_stack_min=8)
    assert "97 % sjanse for innstilling eller ≥ 10 min forsinkelse" in body
    assert "mest sannsynlig innstilt" in body
    assert "min forsinket" not in body


def test_push_body_collapses_a_single_delay_figure():
    body = _push_body(p_cancel=0.02, dep_delay_p50_stack_min=12, dep_delay_p90_stack_min=12)
    assert "typisk 12 min forsinket" in body


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} matcher checks passed.")
