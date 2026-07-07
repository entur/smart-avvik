"""Matcher unit checks for varsel.match_journey — synthetic _CallPred-shaped rows.

Runs standalone (`python test_varsel.py`) or under pytest. Covers direction, window edges, the
weekday filter, overnight rejection, and the alert-worthiness gate. No BigQuery, no network.
"""

from dataclasses import dataclass
from datetime import datetime

import varsel
from varsel import _OSLO, match_journey


@dataclass
class FakeCall:
    """Duck-types main._CallPred for the fields the matcher reads."""

    jkey: str
    stop_place: str
    stop_order: int
    aimed_dep: datetime | None
    operator: str = "VYG"
    p_cancel: float = 0.5
    p_impact_dep: float | None = 0.9
    line: str | None = "VYG:Line:R10"
    summary: str | None = "Signalfeil"
    dep_delay_typ_min: int | None = 4
    dep_delay_p90_min: int | None = 15
    dep_delay_p90_stack_min: int | None = 12


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


def _train(dep, jkey="t1", board_order=1, dest_order=5, operator="VYG", **board_kw):
    """A boarding call at A and a later call at B (same train)."""
    return [
        FakeCall(jkey=jkey, stop_place="A", stop_order=board_order, aimed_dep=dep,
                 operator=operator, **board_kw),
        FakeCall(jkey=jkey, stop_place="B", stop_order=dest_order, aimed_dep=None,
                 operator=operator),
    ]


def test_basic_match():
    alerts = match_journey(_journey(), {"S1": _train(MON_0800, p_cancel=0.8, p_impact_dep=0.9)})
    assert len(alerts) == 1
    a = alerts[0]
    assert a.situation_number == "S1"
    assert a.cancel_probability == 0.8
    assert a.delay_p90_min == 12  # prefers the exceedance-stack p90
    assert a.alertworthy is True


def test_direction_reverse_no_match():
    # Same stops but to_stop (B) comes BEFORE from_stop (A) on this train — wrong direction.
    calls = [
        FakeCall("t1", "A", 5, MON_0800),
        FakeCall("t1", "B", 1, None),
    ]
    assert match_journey(_journey(), {"S1": calls}) == []


def test_missing_destination_no_match():
    # Boarding in window but the train never calls at B.
    calls = [FakeCall("t1", "A", 1, MON_0800)]
    assert match_journey(_journey(), {"S1": calls}) == []


def test_window_edges():
    j = _journey(begin="07:00", end="09:00")
    assert match_journey(j, {"S1": _train(_oslo(2026, 7, 6, 7, 0))})  # exactly begin -> match
    assert match_journey(j, {"S1": _train(_oslo(2026, 7, 6, 9, 0))})  # exactly end -> match
    assert not match_journey(j, {"S1": _train(_oslo(2026, 7, 6, 6, 59))})  # just before
    assert not match_journey(j, {"S1": _train(_oslo(2026, 7, 6, 9, 1))})  # just after


def test_weekday_filter():
    weekdays_only = _journey(days=(0, 1, 2, 3, 4))
    assert not match_journey(weekdays_only, {"S1": _train(SAT_0800)})  # Saturday excluded
    weekend = _journey(days=(5, 6))
    assert match_journey(weekend, {"S1": _train(SAT_0800)})  # Saturday included


def test_overnight_rejection():
    # A day-window [07:00, 09:00] must not catch a 23:30 departure (no wrap-around).
    assert not match_journey(_journey(), {"S1": _train(_oslo(2026, 7, 6, 23, 30))})


def test_alertworthy_gate():
    # Below threshold on both impact and cancel -> listed but not push-worthy.
    mild = match_journey(_journey(), {"S1": _train(MON_0800, p_cancel=0.1, p_impact_dep=0.2)})
    assert mild and mild[0].alertworthy is False
    # Impact prob unavailable -> fall back to cancellation prob.
    fallback = match_journey(_journey(), {"S1": _train(MON_0800, p_cancel=0.7, p_impact_dep=None)})
    assert fallback and fallback[0].alertworthy is True


def test_per_operator_threshold():
    # RUT's tuned threshold is 0.75 (stricter than the 0.65 default), TRO's is 0.55 (looser):
    # the same 0.70 impact prob is push-worthy for TRO but not for RUT.
    rut = match_journey(_journey(), {"S1": _train(MON_0800, operator="RUT", p_impact_dep=0.70)})
    assert rut and rut[0].alertworthy is False
    tro = match_journey(_journey(), {"S1": _train(MON_0800, operator="TRO", p_impact_dep=0.70)})
    assert tro and tro[0].alertworthy is True


def test_worst_train_wins():
    # Two trains in the window; the alert reports the more severe one.
    calls = _train(_oslo(2026, 7, 6, 7, 30), jkey="t1", p_cancel=0.3) + _train(
        _oslo(2026, 7, 6, 8, 30), jkey="t2", p_cancel=0.85
    )
    alerts = match_journey(_journey(), {"S1": calls})
    assert len(alerts) == 1 and alerts[0].cancel_probability == 0.85


def test_multiple_situations_sorted_worst_first():
    s1 = _train(MON_0800, jkey="a", p_cancel=0.4)
    s2 = _train(MON_0800, jkey="b", p_cancel=0.9)
    alerts = match_journey(_journey(), {"S1": s1, "S2": s2})
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
    alerts = match_journey(_journey(), {"S1": sky + atb_board_only})
    assert len(alerts) == 1
    assert alerts[0].cancel_probability == 0.8  # SKY's train, not ATB's boarding-only 0.99


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} matcher checks passed.")
