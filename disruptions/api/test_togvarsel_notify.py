"""Checks for the togvarsel push-notification policy core (togvarsel_notify.py).

Hermetic and pure: every scenario is a plain-function call with explicit issue dicts, a marker
map, and `now` — no BigQuery, no Web Push, no clock patching. The checks that matter most are
the gates: an untimely round must be recorded as suppressed and never revisited, a follow-up
must be compared against what its baseline round PUSHED (not what was recomputed), the all-clear
is the one green that pushes, and one broken round must never stop the others in the same tick.

Run: uv run python test_togvarsel_notify.py  (or under pytest)
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import togvarsel_notify

_OSLO = ZoneInfo("Europe/Oslo")

# 2026-08-31 is a Monday; 2026-09-05 a Saturday.
SATURDAY = datetime(2026, 9, 5, 8, 0, tzinfo=_OSLO)


def _monday(hour: int, minute: int, second: int = 0) -> datetime:
    """An Oslo-aware instant on Monday 2026-08-31 — every scenario pins its own clock."""
    return datetime(2026, 8, 31, hour, minute, second, tzinfo=_OSLO)


def _issue(key: str, level: str, late: bool = False) -> dict:
    """An issue dict as togvarsel's snapshot hands them over; the policy only reads issue+level."""
    computed = _monday(7, 30) if late else _monday(int(key[:2]) % 24, int(key[2:]) % 60)
    return {"issue": key, "level": level, "late": late, "computed_at": computed}


class _SpyMarkers(dict):
    """A marker map that records reads and writes, so the commit-before-read contract (a same-tick
    follow-up must see the just-decided baseline marker, KTD9) is asserted, not assumed."""

    def __init__(self, *args):
        super().__init__(*args)
        self.ops: list[tuple[str, str]] = []

    def __setitem__(self, key, value):
        self.ops.append(("set", key))
        super().__setitem__(key, value)

    def get(self, key, default=None):
        self.ops.append(("get", key))
        return super().get(key, default)


# ---------------------------------------------------------------------------- the spec table

def test_notify_specs_pin_deadlines_and_baseline_pairing():
    specs = {s.key: s for s in togvarsel_notify.NOTIFY_SPECS}
    assert list(specs) == ["0630", "0700", "1200", "1400"]  # due-time order
    assert {k: s.deadline_h for k, s in specs.items()} == {
        "0630": 7.0, "0700": 9.0, "1200": 14.0, "1400": 18.0,
    }
    assert {k: s.baseline for k, s in specs.items()} == {
        "0630": None, "0700": "0630", "1200": None, "1400": "1200",
    }


# ---------------------------------------------------------------------------- baseline rounds

def test_baseline_yellow_before_the_deadline_pushes_a_warning():
    """AE1 (round level): 0630 yellow at 06:31 with no marker pushes exactly once."""
    markers = {}
    decisions = togvarsel_notify.decide_rounds(
        [_issue("0630", "yellow")], markers, now=_monday(6, 31)
    )
    (d,) = decisions
    assert d.push is True and d.kind == "warning"
    assert markers == {"0630": {"level": "yellow", "pushed": True}}
    assert d.marker == markers["0630"]


def test_baseline_green_is_recorded_but_not_pushed():
    markers = {}
    (d,) = togvarsel_notify.decide_rounds([_issue("0630", "green")], markers, now=_monday(6, 31))
    assert d.push is False and d.reason == "green_baseline"
    assert markers == {"0630": {"level": "green", "pushed": False}}


# ---------------------------------------------------------------------------- follow-up rounds

def test_follow_up_at_the_same_pushed_level_is_suppressed_as_no_change():
    """AE2: 0700 yellow after 0630 pushed yellow — no repeat of the same message."""
    markers = {"0630": {"level": "yellow", "pushed": True}}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "yellow")], markers, now=_monday(7, 1))
    assert d.push is False and d.reason == "no_change"
    assert markers["0700"] == {"level": "yellow", "pushed": False}


def test_follow_up_escalation_pushes_an_update():
    """AE3: 0700 red after 0630 pushed yellow — a level transition is the significant change."""
    markers = {"0630": {"level": "yellow", "pushed": True}}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "red")], markers, now=_monday(7, 1))
    assert d.push is True and d.kind == "update"
    assert markers["0700"] == {"level": "red", "pushed": True}


def test_follow_up_easing_from_red_to_yellow_also_pushes_an_update():
    markers = {"0630": {"level": "red", "pushed": True}}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "yellow")], markers, now=_monday(7, 1))
    assert d.push is True and d.kind == "update"


def test_follow_up_pushes_a_fresh_warning_when_the_baseline_pushed_nothing():
    markers = {"0630": {"level": "green", "pushed": False}}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "yellow")], markers, now=_monday(7, 1))
    # not an "update": nothing was pushed for this rush yet, so the wording must not claim one
    assert d.push is True and d.kind == "warning"


def test_follow_up_pushes_when_the_baseline_round_never_ran():
    # 0630 gave up for the day (e.g. 8 failed computes): no marker at all -> baseline reads green
    markers = {}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "red")], markers, now=_monday(8, 0))
    assert d.push is True and d.kind == "warning"
    assert "0630" not in markers  # deciding 0700 must not invent a baseline marker


def test_green_follow_up_after_a_pushed_warning_is_the_all_clear():
    """AE7: the one green that pushes — friskmelding after a pushed yellow (KTD11)."""
    markers = {"0630": {"level": "yellow", "pushed": True}}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "green")], markers, now=_monday(7, 1))
    assert d.push is True and d.kind == "all_clear"
    # the all-clear is marked like any push; no later round reads it, so the effect is dedup only
    assert markers["0700"] == {"level": "green", "pushed": True}


def test_green_follow_up_after_a_green_baseline_stays_quiet():
    """AE7, second half: green after a green (unpushed) baseline is no change, not an all-clear."""
    markers = {"0630": {"level": "green", "pushed": False}}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "green")], markers, now=_monday(7, 1))
    assert d.push is False and d.reason == "no_change"


def test_pm_all_clear_after_a_red_1200():
    markers = {"1200": {"level": "red", "pushed": True}}
    (d,) = togvarsel_notify.decide_rounds([_issue("1400", "green")], markers, now=_monday(14, 2))
    assert d.push is True and d.kind == "all_clear"


# ---------------------------------------------------------------------------- timeliness

def test_restart_at_0730_suppresses_the_backfill_and_pushes_the_follow_up():
    """AE4: one tick back-fills 0630 (late, red) and issues 0700 (red). Handed over in the wrong
    order on purpose: evaluation must follow due-time order, not input order (KTD9)."""
    markers = _SpyMarkers()
    decisions = togvarsel_notify.decide_rounds(
        [_issue("0700", "red"), _issue("0630", "red", late=True)], markers, now=_monday(7, 30)
    )
    assert [d.issue for d in decisions] == ["0630", "0700"]
    d0630, d0700 = decisions
    assert d0630.push is False and d0630.reason == "untimely"
    assert markers["0630"] == {"level": "red", "pushed": False}
    # 0700 pushes a fresh warning: its effective baseline is green, because 0630 — though red —
    # pushed nothing
    assert d0700.push is True and d0700.kind == "warning"
    # and 0700 read the JUST-committed 0630 marker, not a snapshot from before the pass
    assert markers.ops.index(("set", "0630")) < markers.ops.index(("get", "0630"))


def test_the_notify_deadline_is_strictly_before_the_cutoff():
    # a 1200 back-fill evaluated 14:00:30 is past the cutoff: suppressed, and never revisited
    markers = {}
    (d,) = togvarsel_notify.decide_rounds(
        [_issue("1200", "red", late=True)], markers, now=_monday(14, 0, 30)
    )
    assert d.push is False and d.reason == "untimely"
    assert markers["1200"] == {"level": "red", "pushed": False}
    # 06:59:59 is still strictly before 07:00; 07:00:00 sharp is not
    timely = togvarsel_notify.decide_issue(_issue("0630", "red"), {}, now=_monday(6, 59, 59))
    assert timely.push is True
    sharp = togvarsel_notify.decide_issue(_issue("0630", "red"), {}, now=_monday(7, 0, 0))
    assert sharp.push is False and sharp.reason == "untimely"


def test_1400_is_gated_by_what_a_1359_1200_pushed():
    markers = {}
    (d1200,) = togvarsel_notify.decide_rounds([_issue("1200", "red")], markers, now=_monday(13, 59))
    assert d1200.push is True and d1200.kind == "warning"
    # next tick at 14:01 carries both PM issues; 1200 is done, 1400 red is no change
    (d1400,) = togvarsel_notify.decide_rounds(
        [_issue("1200", "red"), _issue("1400", "red")], markers, now=_monday(14, 1)
    )
    assert d1400.issue == "1400" and d1400.push is False and d1400.reason == "no_change"
    # with a level change instead, the 14:01 round pushes as an update
    markers2 = {"1200": {"level": "red", "pushed": True}}
    (chg,) = togvarsel_notify.decide_rounds([_issue("1400", "yellow")], markers2, now=_monday(14, 1))
    assert chg.push is True and chg.kind == "update"


def test_now_in_utc_is_read_as_oslo_wall_clock():
    # 04:31 UTC on 2026-08-31 is 06:31 in Oslo (CEST): timely for 0630 (KTD10)
    markers = {}
    (d,) = togvarsel_notify.decide_rounds(
        [_issue("0630", "yellow")], markers, now=datetime(2026, 8, 31, 4, 31, tzinfo=UTC)
    )
    assert d.push is True


# ---------------------------------------------------------------------------- idempotency + safety

def test_a_marked_round_is_never_reevaluated():
    markers = {"0630": {"level": "yellow", "pushed": True}}
    before = deepcopy(markers)
    assert togvarsel_notify.decide_rounds([_issue("0630", "yellow")], markers, now=_monday(6, 45)) == []
    # even when a recompute changed the level after the fact — the round is done for the day
    assert togvarsel_notify.decide_rounds([_issue("0630", "red")], markers, now=_monday(6, 46)) == []
    assert markers == before


def test_an_empty_issue_list_is_a_natural_no_op():
    """AE5 (round level): weekends and holidays issue nothing, so there is nothing to decide."""
    markers = {}
    assert togvarsel_notify.decide_rounds([], markers, now=SATURDAY) == []
    assert markers == {}


def test_one_broken_round_never_stops_the_rest_of_the_tick():
    # a restart at 13:30 back-fills 0630 with a malformed level alongside a fresh 1200: the broken
    # round records no marker; the healthy one still pushes (R10)
    markers = {}
    decisions = togvarsel_notify.decide_rounds(
        [_issue("0630", "purple", late=True), _issue("1200", "yellow")], markers, now=_monday(13, 30)
    )
    assert [d.issue for d in decisions] == ["1200"]
    assert decisions[0].push is True
    assert "0630" not in markers and "1200" in markers
    # the failed round is retried on the next tick (here it is sane again — and untimely by now,
    # so it is recorded as suppressed rather than left dangling)
    retried = togvarsel_notify.decide_rounds(
        [_issue("0630", "red", late=True), _issue("1200", "yellow")], markers, now=_monday(13, 31)
    )
    assert [d.issue for d in retried] == ["0630"]
    assert retried[0].reason == "untimely"
    assert markers["0630"] == {"level": "red", "pushed": False}


def test_an_unknown_issue_key_is_skipped_without_a_marker():
    markers = {}
    decisions = togvarsel_notify.decide_rounds(
        [_issue("9999", "red"), _issue("0630", "red")], markers, now=_monday(6, 40)
    )
    assert [d.issue for d in decisions] == ["0630"]
    assert list(markers) == ["0630"]


def test_decide_issue_is_pure_and_mutates_nothing():
    markers = {"0630": {"level": "yellow", "pushed": True}}
    issue = _issue("0700", "red")
    before_markers, before_issue = deepcopy(markers), deepcopy(issue)
    d = togvarsel_notify.decide_issue(issue, markers, now=_monday(7, 5))
    assert d.push is True
    assert markers == before_markers and issue == before_issue


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
