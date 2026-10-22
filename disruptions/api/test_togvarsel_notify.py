"""Checks for the togvarsel push-notification policy core, matcher, and delivery (togvarsel_notify.py).

Hermetic: every policy scenario is a plain-function call with explicit issue dicts, a marker map,
and `now` — no Web Push, no clock patching — and BigQuery is a fake that replays canned station
rows, like test_togvarsel's. The checks that matter most are the gates: an untimely round must be
recorded as suppressed and never revisited, a follow-up must be compared against what its
baseline round PUSHED (not what was recomputed), the all-clear is the one green that pushes, one
broken round or journey must never stop the others in the same tick, and while the station set is
unavailable nothing may be evaluated or marked — so the round still fires when the set arrives.

The composition and delivery sections wire the module to varsel's REAL delivery seams the way
main does (injected callables, never an import), with only `varsel._send_push` stubbed —
so the wording, brand assets, gone-grace-drop rule and round counts are checked against the same
machinery the SX pushes ride on.

Run: uv run --with fastapi --with httpx python test_togvarsel_notify.py  (or under pytest)
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

# Rushvarsel's master gate, pinned ON before the module is imported: both routes here carry a
# router-level dependency on it and 404 while it is off (its default). Set at import, the way
# test_varsel_api pins KIX_ENVIRONMENT — each suite is its own process. The policy core needs no
# such pin: `enabled` is an injected seam, and main.py is where the two flags are ANDed.
os.environ["SHOW_RUSHVARSEL"] = "true"

import deployment
import togvarsel_notify
import varsel
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    follow-up must see the just-decided baseline marker) is asserted, not assumed."""

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
    """A 0630 yellow at 06:31 with no marker pushes exactly once."""
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


# ---------------------------------------------------------------------------- red-only

def _red_only(monkeypatch) -> None:
    monkeypatch.setattr(deployment, "rushvarsel_red_only", lambda: True)


def test_red_only_holds_back_a_yellow_baseline_and_pushes_a_red_one(monkeypatch):
    _red_only(monkeypatch)
    markers = {}
    (d,) = togvarsel_notify.decide_rounds([_issue("0630", "yellow")], markers, now=_monday(6, 31))
    assert d.push is False and d.reason == "yellow_baseline"
    assert markers["0630"] == {"level": "yellow", "pushed": False}
    (d,) = togvarsel_notify.decide_rounds([_issue("1200", "red")], markers, now=_monday(12, 1))
    assert d.push is True and d.kind == "warning"


def test_red_only_follow_ups_say_red_or_green_never_yellow(monkeypatch):
    """The first row is 2026-09-28's morning: yellow at 06:30, red at 07:00 — a fresh warning, not
    an update, since nothing was pushed. A yellow never pushes, after a red or after nothing."""
    _red_only(monkeypatch)
    held, red = {"level": "yellow", "pushed": False}, {"level": "red", "pushed": True}
    cases = [
        (held, "red", (True, "warning", None, "green")),
        (red, "yellow", (False, None, "no_change", "red")),
        (red, "green", (True, "all_clear", None, "red")),
        (held, "yellow", (False, None, "no_change", "green")),
        (held, "green", (False, None, "no_change", "green")),
    ]
    for base, level, want in cases:
        (d,) = togvarsel_notify.decide_rounds(
            [_issue("0700", level)], {"0630": dict(base)}, now=_monday(7, 1)
        )
        assert (d.push, d.kind, d.reason, d.baseline_level) == want, (base, level)


def test_the_launch_rule_is_the_default():
    assert togvarsel_notify.push_levels() == {"yellow", "red"}


# ---------------------------------------------------------------------------- follow-up rounds

def test_follow_up_at_the_same_pushed_level_is_suppressed_as_no_change():
    """A 0700 yellow after 0630 pushed yellow — no repeat of the same message."""
    markers = {"0630": {"level": "yellow", "pushed": True}}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "yellow")], markers, now=_monday(7, 1))
    assert d.push is False and d.reason == "no_change"
    assert markers["0700"] == {"level": "yellow", "pushed": False}


def test_follow_up_escalation_pushes_an_update():
    """A 0700 red after 0630 pushed yellow — a level transition is the significant change."""
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
    """The one green that pushes — friskmelding after a pushed yellow."""
    markers = {"0630": {"level": "yellow", "pushed": True}}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "green")], markers, now=_monday(7, 1))
    assert d.push is True and d.kind == "all_clear"
    # the all-clear is marked like any push; no later round reads it, so the effect is dedup only
    assert markers["0700"] == {"level": "green", "pushed": True}


def test_green_follow_up_after_a_green_baseline_stays_quiet():
    """Green after a green (unpushed) baseline is no change, not an all-clear."""
    markers = {"0630": {"level": "green", "pushed": False}}
    (d,) = togvarsel_notify.decide_rounds([_issue("0700", "green")], markers, now=_monday(7, 1))
    assert d.push is False and d.reason == "no_change"


def test_pm_all_clear_after_a_red_1200():
    markers = {"1200": {"level": "red", "pushed": True}}
    (d,) = togvarsel_notify.decide_rounds([_issue("1400", "green")], markers, now=_monday(14, 2))
    assert d.push is True and d.kind == "all_clear"


# ---------------------------------------------------------------------------- timeliness

def test_restart_at_0730_suppresses_the_backfill_and_pushes_the_follow_up():
    """One tick back-fills 0630 (late, red) and issues 0700 (red). Handed over in the wrong
    order on purpose: evaluation must follow due-time order, not input order."""
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
    # 04:31 UTC on 2026-08-31 is 06:31 in Oslo (CEST): timely for 0630
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
    """Weekends and holidays issue nothing, so there is nothing to decide."""
    markers = {}
    assert togvarsel_notify.decide_rounds([], markers, now=SATURDAY) == []
    assert markers == {}


def test_one_broken_round_never_stops_the_rest_of_the_tick():
    # a restart at 13:30 back-fills 0630 with a malformed level alongside a fresh 1200: the broken
    # round records no marker; the healthy one still pushes
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


# ---------------------------------------------------------------------------- station set

# 39 refs like the real table's — the count is what the sanity log is asserted on.
_OSLO_REFS = tuple(f"NSR:StopPlace:{n}" for n in range(1, 40))


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self, timeout=None):
        return self._rows


class _StationsBQ:
    """Dispatches on the SQL text like test_togvarsel's _FakeBQ: the station read replays canned
    refs; `fail=True` raises like the real client on a missing table (prd before
    copy_runtime_to_prd.sh has landed — a real launch state, not an edge case)."""

    def __init__(self, refs=(), fail=False, rounds_fail=False):
        self.refs = list(refs)
        self.fail = fail
        self.rounds_fail = rounds_fail
        self.queries: list[str] = []
        # Every togvarsel_rounds MERGE, as {param: value}. Recording is best-effort in the module,
        # so without capturing it here a broken write is INVISIBLE: the except swallows it (an
        # assert in this fake included) and every other check still passes.
        self.rounds: list[dict] = []
        self.rounds_ddl = 0

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        if "togvarsel_rounds" in sql:
            if self.rounds_fail:
                raise RuntimeError("table disruptions.togvarsel_rounds is not there")
            if sql.lstrip().startswith("CREATE"):
                self.rounds_ddl += 1
            else:
                self.rounds.append(
                    {p.name: p.value for p in (job_config.query_parameters if job_config else [])}
                )
            return _FakeJob([])
        if self.fail:
            raise RuntimeError("table disruptions.togvarsel_oslo_stations is not there")
        assert "togvarsel_oslo_stations" in sql
        return _FakeJob([{"stop_place": r} for r in self.refs])

    def round_for(self, issue: str) -> dict:
        rows = [r for r in self.rounds if r["issue"] == issue]
        assert len(rows) == 1, f"expected exactly one round row for {issue}, got {len(rows)}"
        return rows[0]


def _reset_stations(fake) -> None:
    togvarsel_notify.configure(bq_client=fake)
    togvarsel_notify._station_state.clear()
    togvarsel_notify._rounds_table_ready = False


@contextmanager
def _capture_logs():
    """Collect the module's log records — handler-based so it works standalone AND under pytest."""
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler(level=logging.DEBUG)
    lg = togvarsel_notify.logger
    old_level = lg.level
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old_level)


def test_station_set_is_fetched_once_per_oslo_day_and_logs_the_count():
    fake = _StationsBQ(refs=_OSLO_REFS)
    _reset_stations(fake)
    with _capture_logs() as records:
        refs = togvarsel_notify.station_set(now=_monday(6, 30))
    assert refs == frozenset(_OSLO_REFS)
    # the sanity log: whoever watches a round can see the geography arrived whole (expect 39)
    assert any("39" in r.getMessage() for r in records)
    # cached for the rest of the Oslo day: a later tick runs no query at all
    assert togvarsel_notify.station_set(now=_monday(14, 1)) == refs
    assert len(fake.queries) == 1
    # a new day refetches — a retrain may have rebuilt the table overnight
    assert togvarsel_notify.station_set(now=datetime(2026, 9, 1, 6, 31, tzinfo=_OSLO)) == refs
    assert len(fake.queries) == 2


def test_station_fetch_failure_is_unavailable_then_recovers_with_no_markers_meanwhile():
    """Fetch fails at 06:30 → the caller skips evaluation entirely (no markers) and retries
    on the very next call — no cooldown, no daily give-up — so 0630 still pushes at 06:40."""
    fake = _StationsBQ(refs=_OSLO_REFS, fail=True)
    _reset_stations(fake)
    markers = {}
    with _capture_logs():
        assert togvarsel_notify.station_set(now=_monday(6, 30)) is None
        # the caller contract while unavailable: evaluate nothing, so the marker map is untouched
        assert markers == {}
        assert togvarsel_notify.station_set(now=_monday(6, 31)) is None
    assert len(fake.queries) == 2  # retried immediately: no cooldown between attempts
    fake.fail = False
    assert togvarsel_notify.station_set(now=_monday(6, 40)) == frozenset(_OSLO_REFS)
    (d,) = togvarsel_notify.decide_rounds([_issue("0630", "yellow")], markers, now=_monday(6, 40))
    assert d.push is True  # the round was never marked while the set was unavailable


def test_persistent_station_failure_logs_at_most_one_error_per_day():
    fake = _StationsBQ(fail=True)
    _reset_stations(fake)
    with _capture_logs() as records:
        for minute in (30, 31, 32):
            assert togvarsel_notify.station_set(now=_monday(6, minute)) is None
    assert len([r for r in records if r.levelno >= logging.ERROR]) == 1
    # the next day gets its own single ERROR — going quiet forever would hide a broken prd launch
    with _capture_logs() as records:
        assert togvarsel_notify.station_set(now=datetime(2026, 9, 1, 6, 30, tzinfo=_OSLO)) is None
    assert len([r for r in records if r.levelno >= logging.ERROR]) == 1


def test_an_empty_station_table_reads_as_unavailable():
    # an empty set would make every journey silently unaffected; honest "unavailable" instead
    fake = _StationsBQ(refs=())
    _reset_stations(fake)
    with _capture_logs():
        assert togvarsel_notify.station_set(now=_monday(6, 30)) is None


# ---------------------------------------------------------------------------- matching

# Three of the table's 39 stations are enough for the matcher: Asker, Oslo S, Sandvika.
STATIONS = frozenset({"NSR:StopPlace:418", "NSR:StopPlace:337", "NSR:StopPlace:610"})


def _ident(ref: str) -> tuple[str, ...]:
    return (ref,)


def _j(from_stop="NSR:StopPlace:418", to_stop="NSR:StopPlace:337", begin="07:00",
       end="08:30", days=(0, 1, 2, 3, 4), device="dev-1") -> dict:
    """A journey record shaped like varsel stores them (POST /journeys): a weekday commute
    Asker → Oslo S unless overridden."""
    return {"device_id": device, "from_stop": from_stop, "to_stop": to_stop,
            "begin": begin, "end": end, "days": list(days)}


def test_notify_specs_pin_the_scope_windows():
    specs = {s.key: s for s in togvarsel_notify.NOTIFY_SPECS}
    assert {k: (s.scope_start_h, s.scope_end_h) for k, s in specs.items()} == {
        "0630": (6.5, 9.0), "0700": (7.0, 9.0), "1200": (15.0, 18.0), "1400": (15.0, 18.0),
    }


def test_a_journey_with_both_endpoints_listed_matches():
    matched, devices = togvarsel_notify.match_journeys(
        "0630", {"j1": _j()}, STATIONS, _ident, now=_monday(6, 31)
    )
    assert matched == ["j1"] and devices == {"dev-1"}


def test_one_endpoint_outside_the_station_set_does_not_match():
    journeys = {"j1": _j(to_stop="NSR:StopPlace:99999")}  # ends beyond the network
    matched, devices = togvarsel_notify.match_journeys(
        "0630", journeys, STATIONS, _ident, now=_monday(6, 31)
    )
    assert matched == [] and devices == set()


def test_an_alias_ref_matches_through_expansion():
    # registered on a bus-side ref of Oslo S: not itself in the table, but the place_refs-style
    # expansion includes the primary, which is
    def expand(ref):
        if ref == "NSR:StopPlace:59872":
            return ("NSR:StopPlace:337", "NSR:StopPlace:59872")
        return (ref,)

    journeys = {"j1": _j(to_stop="NSR:StopPlace:59872")}
    matched, _devices = togvarsel_notify.match_journeys(
        "0630", journeys, STATIONS, expand, now=_monday(6, 31)
    )
    assert matched == ["j1"]
    # identity expansion of the same record misses: the match above came from the expander
    none, _devices = togvarsel_notify.match_journeys(
        "0630", journeys, STATIONS, _ident, now=_monday(6, 31)
    )
    assert none == []


def test_days_excluding_today_do_not_match():
    journeys = {"j1": _j(days=(5, 6))}  # weekend journeys, evaluated on a Monday
    matched, _devices = togvarsel_notify.match_journeys(
        "0630", journeys, STATIONS, _ident, now=_monday(6, 31)
    )
    assert matched == []


def test_the_window_comparison_is_half_open_at_the_scope_start():
    # inclusive [05:30, 06:30] merely TOUCHES the 06:30–09:00 scope: no overlap compared
    # half-open; one minute inside (06:00–06:31) is a match
    journeys = {"touching": _j(begin="05:30", end="06:30"), "inside": _j(begin="06:00", end="06:31")}
    matched, _devices = togvarsel_notify.match_journeys(
        "0630", journeys, STATIONS, _ident, now=_monday(6, 30, 30)
    )
    assert matched == ["inside"]


def test_a_window_ending_before_the_follow_up_scope_does_not_match_it():
    journeys = {"j1": _j(begin="06:30", end="06:50")}
    # affected by the 0630 issue, whose scope starts at 06:30 ...
    matched, _devices = togvarsel_notify.match_journeys(
        "0630", journeys, STATIONS, _ident, now=_monday(6, 31)
    )
    assert matched == ["j1"]
    # ... but not by the 0700 follow-up, whose scope starts at 07:00
    matched, _devices = togvarsel_notify.match_journeys(
        "0700", journeys, STATIONS, _ident, now=_monday(7, 1)
    )
    assert matched == []


def test_the_live_window_is_bounded_by_evaluation_time():
    # [max(scope start, now), scope end) — a traveller whose window has fully passed gets
    # nothing, warnings and friskmelding alike
    journeys = {"gone": _j(begin="07:00", end="07:40"), "still": _j(begin="07:00", end="08:30")}
    matched, _devices = togvarsel_notify.match_journeys(
        "0700", journeys, STATIONS, _ident, now=_monday(7, 50)
    )
    assert matched == ["still"]


def test_a_window_over_midnight_is_the_next_weekdays_small_hours():
    """KIX-22: a Monday 23:00-07:30 is Monday evening and TUESDAY morning, so it is Tuesday's
    morning rush and not Monday's; a weekday 22:00-01:00 touches no rush at all."""
    journeys = {
        "night_shift": _j(begin="23:00", end="07:30", days=(0,)),
        "late": _j(begin="22:00", end="01:00"),
    }
    tuesday = _monday(6, 31) + timedelta(days=1)
    monday_matched, _devices = togvarsel_notify.match_journeys("0630", journeys, STATIONS, _ident, now=_monday(6, 31))
    tuesday_matched, _devices = togvarsel_notify.match_journeys("0630", journeys, STATIONS, _ident, now=tuesday)
    assert monday_matched == [] and tuesday_matched == ["night_shift"]
    assert togvarsel_notify.affected_rushes("dev-1", journeys, STATIONS, _ident, tuesday) == {"am"}


def test_pm_issues_notify_the_afternoon_windows_in_advance():
    # both PM issues carry the 15:00–18:00 scope; a 16:00–17:00 commute matches at 12:01 already —
    # advance warning is the product's point
    journeys = {"j1": _j(begin="16:00", end="17:00")}
    for key, now in (("1200", _monday(12, 1)), ("1400", _monday(14, 1))):
        matched, _devices = togvarsel_notify.match_journeys(
            key, journeys, STATIONS, _ident, now=now
        )
        assert matched == ["j1"], key


def test_two_matching_journeys_on_one_device_yield_one_device():
    journeys = {"j1": _j(), "j2": _j(from_stop="NSR:StopPlace:610", begin="07:15", end="08:00")}
    matched, devices = togvarsel_notify.match_journeys(
        "0630", journeys, STATIONS, _ident, now=_monday(6, 31)
    )
    assert matched == ["j1", "j2"]
    assert devices == {"dev-1"}  # one push per device per round


def test_a_malformed_record_is_skipped_and_the_healthy_ones_still_match():
    journeys = {"bad": _j(begin="not-a-time"), "good": _j(device="dev-2")}
    with _capture_logs() as records:
        matched, devices = togvarsel_notify.match_journeys(
            "0630", journeys, STATIONS, _ident, now=_monday(6, 31)
        )
    assert matched == ["good"] and devices == {"dev-2"}
    assert any("bad" in r.getMessage() and r.levelno >= logging.ERROR for r in records)


def test_match_journeys_refuses_an_unknown_issue_key():
    try:
        togvarsel_notify.match_journeys("9999", {}, STATIONS, _ident, now=_monday(6, 31))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown issue key")


# ---------------------------------------------------------------------------- composition

# Reasons as togvarsel's rule engine really words them (test_togvarsel pins that vocabulary).
_REASONS = (
    "2 tog er innstilt før kl. 06:30",
    "forsinkelser i morgentimene så langt",
    "personalmangel meldt",
)


def _issued(key: str = "0630", level: str = "yellow", scope: str = "morgenrushet 06:30–09:00",
            reasons=_REASONS, p: float = 0.21) -> dict:
    """An issue dict with the payload fields togvarsel's Issue model really serves: composition
    reads `scope` and `reasons` on top of the policy core's issue+level, and the rush outlook
    reads `p` on top of those."""
    return {**_issue(key, level), "scope": scope, "reasons": list(reasons), "p": p}


def test_warning_payload_is_forecast_framed_with_level_env_prefix_and_reasons():
    """The title says rush forecast + level (never train-alert framing), the
    injected tagger prefixes the environment, the body is scope + leading reasons — and there is
    no situation_number: this is not an SX situation (precedent: the /varsel-api/test push)."""
    decision = togvarsel_notify.decide_issue(_issued(), {}, now=_monday(6, 31))
    payload = togvarsel_notify.build_payload(
        decision, _issued(), "entur",
        brand_assets=varsel._BRAND_ASSETS, tag_title=lambda t: f"[dev] {t}",
    )
    assert payload["title"] == "[dev] Rushvarsel: gult nivå for morgenrushet"
    assert payload["body"] == (
        "Prognosen for morgenrushet 06:30–09:00 er gult nivå. "
        "2 tog er innstilt før kl. 06:30. Forsinkelser i morgentimene så langt."
    )
    assert "personalmangel" not in payload["body"]  # leading reasons only — a lock screen is small
    assert payload["url"] == "/rushvarsel"  # the entur brand lands on the forecast page
    assert payload["icon"] == varsel._BRAND_ASSETS["entur"]["icon"]
    assert "situation_number" not in payload


def test_none_brand_gets_its_own_icon_and_the_unbranded_varsel_url():
    """The unbranded install has no /rushvarsel to land on, so it keeps its own varsel URL —
    the body carries the essentials. The WORDING is identical across brands (varsel precedent)."""
    decision = togvarsel_notify.decide_issue(_issued(), {}, now=_monday(6, 31))
    entur, none = (
        togvarsel_notify.build_payload(
            decision, _issued(), brand,
            brand_assets=varsel._BRAND_ASSETS, tag_title=lambda t: t,
        )
        for brand in ("entur", "none")
    )
    assert none["url"] == varsel._BRAND_ASSETS["none"]["url"]
    assert none["icon"] == varsel._BRAND_ASSETS["none"]["icon"]
    assert none["icon"] != entur["icon"]
    assert (none["title"], none["body"]) == (entur["title"], entur["body"])


def test_afternoon_warning_names_the_afternoon_rush():
    issue = _issued("1200", "red", scope="ettermiddagsrushet 15:00–18:00",
                    reasons=("12 innstilte avganger så langt i dag",))
    decision = togvarsel_notify.decide_issue(issue, {}, now=_monday(12, 1))
    title, body = togvarsel_notify.compose_message(decision, issue)
    assert title == "Rushvarsel: rødt nivå for ettermiddagsrushet"
    assert body == ("Prognosen for ettermiddagsrushet 15:00–18:00 er rødt nivå. "
                    "12 innstilte avganger så langt i dag.")


def test_update_payload_marks_the_forecast_as_updated():
    """A follow-up against a pushed baseline says the forecast is UPDATED, so the second
    push in one rush reads as a revision, not a repeat."""
    markers = {"0630": {"level": "yellow", "pushed": True}}
    issue = _issued("0700", "red", scope="morgenrushet 07:00–09:00")
    decision = togvarsel_notify.decide_issue(issue, markers, now=_monday(7, 1))
    assert decision.kind == "update"
    title, body = togvarsel_notify.compose_message(decision, issue)
    assert title == "Oppdatert rushvarsel: rødt nivå for morgenrushet"
    assert body.startswith("Prognosen for morgenrushet 07:00–09:00 er endret til rødt nivå.")


def test_a_fresh_warning_from_a_follow_up_round_is_not_worded_as_an_update():
    # the baseline pushed nothing, so there is no forecast to have "updated"
    markers = {"0630": {"level": "green", "pushed": False}}
    decision = togvarsel_notify.decide_issue(_issued("0700", "yellow"), markers, now=_monday(7, 1))
    title, _body = togvarsel_notify.compose_message(decision, _issued("0700", "yellow"))
    assert title == "Rushvarsel: gult nivå for morgenrushet"


def test_all_clear_payload_is_a_friskmelding_without_warning_language():
    """The drop back to green is framed as a friskmelding — calm, and free of every word the
    warnings use. No reasons either: green's own reasons still talk about trouble signs."""
    markers = {"0630": {"level": "yellow", "pushed": True}}
    issue = _issued("0700", "green", scope="morgenrushet 07:00–09:00",
                    reasons=("ingen tegn til forstyrrelser i morgentimene",))
    decision = togvarsel_notify.decide_issue(issue, markers, now=_monday(7, 1))
    assert decision.kind == "all_clear"
    title, body = togvarsel_notify.compose_message(decision, issue)
    assert title == "Friskmelding: grønt nivå for morgenrushet"
    assert body == "Utsiktene for morgenrushet 07:00–09:00 er tilbake på grønt nivå."
    for word in ("rushvarsel", "avvik", "forsinkel", "innstil", "forstyrrelser", "advarsel"):
        assert word not in title.lower() and word not in body.lower(), word


def test_composition_refuses_a_non_push_decision():
    decision = togvarsel_notify.decide_issue(_issued("0630", "green"), {}, now=_monday(6, 31))
    assert decision.push is False
    try:
        togvarsel_notify.compose_message(decision, _issued("0630", "green"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a non-push decision")


# ---------------------------------------------------------------------------- round delivery

def _dev(endpoint: str, brand: str | None = None, created: str | None = None) -> dict:
    """A varsel device record: subscription + created_ts (the grace window reads it), brand opt."""
    d = {
        "subscription": {"endpoint": endpoint, "keys": {"p256dh": "k", "auth": "a"}},
        "created_ts": created or datetime.now(UTC).isoformat(),
    }
    if brand:
        d["brand"] = brand
    return d


@contextmanager
def _push_devices(devices: dict):
    """Swap varsel's device map for the scenario's and restore it — the delivery tests run against
    varsel's real seams, so they must not leak state into (or from) the other suites' checks."""
    with varsel._state_lock:
        saved = dict(varsel._devices)
        varsel._devices.clear()
        varsel._devices.update(devices)
    try:
        yield
    finally:
        with varsel._state_lock:
            varsel._devices.clear()
            varsel._devices.update(saved)


def _wiring() -> dict:
    """push_round's injected seams, wired to varsel exactly as main will."""
    return {
        "resolve_device": varsel.device_subscription,
        "deliver": varsel.deliver_to_devices,
        "handle_gone": varsel.handle_gone_device,
        "brand_assets": varsel._BRAND_ASSETS,
        "tag_title": lambda t: t,
    }


def test_push_round_pushes_once_per_device_with_each_brands_assets(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        varsel, "_send_push", lambda sub, payload: sent.append((sub["endpoint"], payload)) or "sent"
    )
    decision = togvarsel_notify.decide_issue(_issued(), {}, now=_monday(6, 31))
    devices = {
        "dev-e": _dev("https://push.example.com/e", brand="entur"),
        "dev-n": _dev("https://push.example.com/n", brand="none"),
    }
    with _push_devices(devices):
        summary = togvarsel_notify.push_round(
            # dev-x matched but was deleted before delivery: skipped, counted, never a crash
            decision, _issued(), ["j1", "j2", "j3"], {"dev-e", "dev-n", "dev-x"}, **_wiring()
        )
    assert len(sent) == 2  # one push per device per round, and only to live subscriptions
    by_endpoint = dict(sent)
    entur = by_endpoint["https://push.example.com/e"]
    none = by_endpoint["https://push.example.com/n"]
    assert entur["url"] == "/rushvarsel" and entur["icon"] == varsel._BRAND_ASSETS["entur"]["icon"]
    assert none["url"] == varsel._BRAND_ASSETS["none"]["url"]
    assert none["icon"] == varsel._BRAND_ASSETS["none"]["icon"]
    assert "situation_number" not in entur and "situation_number" not in none
    assert summary["sent"] == 2 and summary["error"] == 0 and summary["gone"] == 0
    assert summary["no_subscription"] == 1 and summary["matched_devices"] == 3


def test_push_round_sends_nothing_once_the_lease_says_stop(monkeypatch):
    """The lease's seam (zero-downtime U2, KTD8): asked before the delivery, so a leader that lost
    the lease or is stopping pushes nothing for the round — and says so by raising RoundFenced,
    which is what keeps `evaluate` from persisting a marker for a round nobody delivered."""
    sent: list = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    delivered: list = []

    def deliver(per_device, prepare):
        delivered.append(per_device)
        return varsel.deliver_to_devices(per_device, prepare)

    decision = togvarsel_notify.decide_issue(_issued(), {}, now=_monday(6, 31))
    devices = {"dev-e": _dev("https://push.example.com/e")}
    with _push_devices(devices):
        try:
            togvarsel_notify.push_round(
                decision, _issued(), ["j1"], set(devices),
                **{**_wiring(), "deliver": deliver}, should_continue=lambda: False,
            )
        except togvarsel_notify.RoundFenced:
            pass
        else:
            raise AssertionError("a fenced round must say so, or its marker is persisted")
        assert sent == [] and delivered == [], "no fan-out at all after the lease said stop"
        togvarsel_notify.push_round(
            decision, _issued(), ["j1"], set(devices),
            **{**_wiring(), "deliver": deliver}, should_continue=lambda: True,
        )
    assert len(sent) == 1, "a leader that still holds the lease delivers as before"


def test_push_round_asks_the_lease_before_every_send(monkeypatch):
    """Lost mid-fan-out: the lease is asked again before each device's send, so nothing new is
    submitted after it answers False — and a round only PARTLY delivered is still fenced, because
    the devices it withdrew would otherwise never hear of the round."""
    sent: list = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    lock = threading.Lock()
    answers = iter([True, True])  # the pre-check and ONE send, then the lease is gone

    def should_continue():
        with lock:
            return next(answers, False)

    decision = togvarsel_notify.decide_issue(_issued(), {}, now=_monday(6, 31))
    devices = {f"dev-{i}": _dev(f"https://push.example.com/{i}") for i in range(4)}
    with _push_devices(devices):
        try:
            togvarsel_notify.push_round(
                decision, _issued(), ["j1"], set(devices), **_wiring(),
                should_continue=should_continue,
            )
        except togvarsel_notify.RoundFenced:
            pass
        else:
            raise AssertionError("a partly-withdrawn round must be fenced")
    assert len(sent) == 1, "no send is submitted once the lease said stop"


def test_a_gone_device_is_dropped_after_grace_and_the_round_completes(monkeypatch):
    results = {
        "https://push.example.com/old": "gone",
        "https://push.example.com/fresh": "gone",
        "https://push.example.com/ok": "sent",
    }
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: results[sub["endpoint"]])
    decision = togvarsel_notify.decide_issue(_issued("0630", "red"), {}, now=_monday(6, 31))
    devices = {
        "dev-old": _dev("https://push.example.com/old", created="2000-01-01T00:00:00+00:00"),
        "dev-fresh": _dev("https://push.example.com/fresh"),  # inside the propagation grace window
        "dev-ok": _dev("https://push.example.com/ok"),
    }
    with _push_devices(devices):
        summary = togvarsel_notify.push_round(
            decision, _issued("0630", "red"), ["j1"], set(devices), **_wiring()
        )
        assert "dev-old" not in varsel._devices  # past grace: the same drop rule as SX pushes
        assert "dev-fresh" in varsel._devices  # a fresh 404/410 is transient: kept
    assert summary["sent"] == 1 and summary["gone"] == 2
    assert summary["dropped"] == 1  # what tells the caller device state changed and needs persisting


def test_one_erroring_device_never_stops_the_others_and_the_round_is_logged(monkeypatch):
    """A transient send error is counted, never retried within the round — and the round's
    marker stays pushed, so the round never re-fires for the error's sake."""
    results = {
        "https://push.example.com/a": "sent",
        "https://push.example.com/b": "error",
        "https://push.example.com/c": "sent",
    }
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: results[sub["endpoint"]])
    markers: dict = {}
    (decision,) = togvarsel_notify.decide_rounds([_issued("0630", "red")], markers, now=_monday(6, 31))
    devices = {
        "dev-a": _dev("https://push.example.com/a"),
        "dev-b": _dev("https://push.example.com/b"),
        "dev-c": _dev("https://push.example.com/c"),
    }
    with _push_devices(devices), _capture_logs() as records:
        summary = togvarsel_notify.push_round(
            decision, _issued("0630", "red"), ["j1", "j2", "j3"], set(devices), **_wiring()
        )
    assert summary["sent"] == 2 and summary["error"] == 1 and summary["gone"] == 0
    assert markers["0630"] == {"level": "red", "pushed": True}  # the marker was never unwound
    rounds = [r for r in records if getattr(r, "fields", {}).get("event") == "togvarsel_notify_round"]
    assert len(rounds) == 1, "exactly one structured round event"
    f = rounds[0].fields
    assert (f["issue"], f["level"], f["kind"]) == ("0630", "red", "warning")
    assert (f["matched_journeys"], f["matched_devices"]) == (3, 3)
    assert (f["sent"], f["error"], f["gone"], f["dropped"]) == (2, 1, 0, 0)


def test_push_round_refuses_a_non_push_decision(monkeypatch):
    monkeypatch.setattr(
        varsel, "_send_push", lambda sub, payload: (_ for _ in ()).throw(AssertionError("no send"))
    )
    decision = togvarsel_notify.decide_issue(_issued("0630", "green"), {}, now=_monday(6, 31))
    try:
        togvarsel_notify.push_round(decision, _issued("0630", "green"), [], set(), **_wiring())
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a non-push decision")


# ---------------------------------------------------------------------------- evaluate

MONDAY_STR = "2026-08-31"


class _MarkerStore:
    """A fake of varsel's marker accessor pair (togvarsel_rounds_for / store_togvarsel_rounds):
    load returns a detached copy like the real one; store counts persists and keeps only the
    stored date (the real one prunes to the current Oslo day)."""

    def __init__(self, initial: dict | None = None):
        self.by_date: dict = deepcopy(initial or {})
        self.stores = 0

    def load(self, date_str: str) -> dict:
        return deepcopy(self.by_date.get(date_str, {}))

    def store(self, date_str: str, markers: dict) -> None:
        self.stores += 1
        self.by_date = {date_str: deepcopy(dict(markers))}


def _configure_evaluate(**overrides) -> tuple[_MarkerStore, _StationsBQ]:
    """Wire configure() the way main does, with fakes for the gates and the marker
    store; every delivery seam is varsel's REAL one, so evaluate() is exercised against the same
    machinery the SX pushes ride on. Returns (marker store, stations fake) for assertions."""
    store = overrides.pop("store", None) or _MarkerStore()
    issues = overrides.pop("issues", [])
    fake = overrides.pop("bq", None) or _StationsBQ(refs=sorted(STATIONS))
    kwargs = {
        "bq_client": fake,
        "enabled": lambda: True,
        "push_ready": lambda: True,
        "issued_today": lambda: deepcopy(list(issues)),
        "journeys": lambda: {"j1": _j()},
        "expand_refs": _ident,
        "resolve_device": varsel.device_subscription,
        "deliver": varsel.deliver_to_devices,
        "handle_gone": varsel.handle_gone_device,
        "brand_assets": varsel._BRAND_ASSETS,
        "tag_title": lambda t: t,
        "load_markers": store.load,
        "store_markers": store.store,
    }
    kwargs.update(overrides)
    togvarsel_notify._station_state.clear()
    # The DDL latch is module state: a leaked True makes the next test's "was the table created?"
    # assertion pass by accident, which is the shape of the _reset_state trap.
    togvarsel_notify._rounds_table_ready = False
    togvarsel_notify.configure(**kwargs)
    return store, fake


def test_evaluate_flag_off_evaluates_nothing_and_writes_no_markers(monkeypatch):
    """While disabled the evaluator must not even LOOK — no station fetch, no markers — which
    is exactly what lets an enable-restart inside a timeliness window still deliver."""
    monkeypatch.setattr(
        varsel, "_send_push",
        lambda sub, payload: (_ for _ in ()).throw(AssertionError("sent while disabled")),
    )
    store, fake = _configure_evaluate(issues=[_issued("0630", "yellow")], enabled=lambda: False)
    togvarsel_notify.evaluate(now=_monday(6, 31))
    assert store.stores == 0 and store.by_date == {}
    assert fake.queries == []  # not even the station fetch ran


def test_evaluate_without_vapid_is_a_no_op_with_no_markers(monkeypatch):
    """No VAPID keys degrades to a no-op with no markers, same contract as the flag."""
    monkeypatch.setattr(
        varsel, "_send_push",
        lambda sub, payload: (_ for _ in ()).throw(AssertionError("sent without VAPID")),
    )
    store, fake = _configure_evaluate(issues=[_issued("0630", "red")], push_ready=lambda: False)
    togvarsel_notify.evaluate(now=_monday(6, 31))
    assert store.stores == 0 and fake.queries == []


def test_evaluate_pushes_after_an_enable_restart_inside_the_window(monkeypatch):
    """The flag was off all morning so no markers exist; the 06:50 restart with the flag on
    evaluates 0630 fresh and delivers — and the stored marker dedups the very next tick."""
    sent: list[dict] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    store, _fake = _configure_evaluate(issues=[_issued("0630", "yellow")])
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}):
        togvarsel_notify.evaluate(now=_monday(6, 50))
        assert len(sent) == 1
        assert sent[0]["title"] == "Rushvarsel: gult nivå for morgenrushet"
        assert store.by_date[MONDAY_STR]["0630"] == {"level": "yellow", "pushed": True}
        togvarsel_notify.evaluate(now=_monday(6, 51))
    assert len(sent) == 1  # the marker dedups; a completed round never re-fires


def test_evaluate_after_a_late_restart_suppresses_and_never_revisits(monkeypatch):
    """The flip side: a 07:10 restart is past 0630's deadline — no push, and the suppression is
    itself marked (and persisted) so the round is never revisited."""
    monkeypatch.setattr(
        varsel, "_send_push",
        lambda sub, payload: (_ for _ in ()).throw(AssertionError("untimely push")),
    )
    store, _fake = _configure_evaluate(issues=[_issued("0630", "yellow")])
    togvarsel_notify.evaluate(now=_monday(7, 10))
    assert store.by_date[MONDAY_STR]["0630"] == {"level": "yellow", "pushed": False}
    assert store.stores == 1  # the suppression-only tick persists once, at the end


def test_evaluate_writes_no_markers_while_the_station_set_is_unavailable(monkeypatch):
    """At the evaluate grain: while the geography cannot be fetched, nothing is evaluated or
    marked — so the round still fires, once, when the set arrives inside the window."""
    sent: list[dict] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    fake = _StationsBQ(refs=sorted(STATIONS), fail=True)
    store, _ = _configure_evaluate(issues=[_issued("0630", "red")], bq=fake)
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}), _capture_logs():
        togvarsel_notify.evaluate(now=_monday(6, 31))
        assert store.stores == 0 and sent == []
        fake.fail = False
        togvarsel_notify.evaluate(now=_monday(6, 40))
    assert len(sent) == 1
    assert store.by_date[MONDAY_STR]["0630"] == {"level": "red", "pushed": True}


def test_evaluate_with_nothing_issued_is_a_natural_no_op():
    """A weekend (or a weekday tick before 06:30) has no issued rounds — nothing is decided,
    delivered, or marked."""
    store, _fake = _configure_evaluate(issues=[])
    with _capture_logs():
        togvarsel_notify.evaluate(now=SATURDAY)
    assert store.stores == 0


def test_evaluate_asks_the_lease_before_each_rounds_fan_out(monkeypatch):
    """U4 wires push_round's `should_continue` to the lease through configure: a leader demoted
    between deciding and delivering sends nothing, persists NO marker, records no round and stops
    the pass — so the successor, reloading the persisted map, delivers the rounds itself (KTD8: a
    duplicate in the window is accepted, a lost round is not)."""
    sent: list[dict] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    asked: list[bool] = []
    issues = [_issued("1200", "red", scope="ettermiddagsrushet 15:00–18:00"),
              _issued("1400", "yellow", scope="ettermiddagsrushet 15:00–18:00")]
    journeys = lambda: {"j1": _j(begin="15:30", end="17:00")}
    store, fake = _configure_evaluate(issues=issues, journeys=journeys,
                                      should_continue=lambda: asked.append(False) or False)
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}), _capture_logs():
        togvarsel_notify.evaluate(now=_monday(13, 30))
    assert sent == []
    assert store.stores == 0 and MONDAY_STR not in store.by_date, "no marker for an undelivered round"
    assert fake.rounds == [], "a fenced round is not recorded; the successor records it"
    assert asked == [False], "the pass stops at the first fenced round"
    _successor, fake = _configure_evaluate(issues=issues, journeys=journeys, store=store,
                                           should_continue=lambda: True)
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}), _capture_logs():
        togvarsel_notify.evaluate(now=_monday(13, 31))
    assert len(sent) == 2
    assert store.by_date[MONDAY_STR]["1200"] == {"level": "red", "pushed": True}
    assert store.by_date[MONDAY_STR]["1400"] == {"level": "yellow", "pushed": True}
    assert {r["issue"] for r in fake.rounds} == {"1200", "1400"}


def test_a_fence_after_a_delivered_round_keeps_only_that_rounds_marker(monkeypatch):
    """A lease lost BETWEEN two rounds: the first is delivered, persisted and recorded; the second's
    marker (committed in memory before any fan-out) must not ride along on the first's persist."""
    sent: list[dict] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    answers = iter([True, True])  # 1200's pre-check and its one send; 1400 is fenced
    issues = [_issued("1200", "red", scope="ettermiddagsrushet 15:00–18:00"),
              _issued("1400", "yellow", scope="ettermiddagsrushet 15:00–18:00")]
    journeys = lambda: {"j1": _j(begin="15:30", end="17:00")}
    store, fake = _configure_evaluate(issues=issues, journeys=journeys,
                                      should_continue=lambda: next(answers, False))
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}), _capture_logs():
        togvarsel_notify.evaluate(now=_monday(13, 30))
    assert len(sent) == 1
    assert store.by_date[MONDAY_STR] == {"1200": {"level": "red", "pushed": True}}
    assert [r["issue"] for r in fake.rounds] == ["1200"]


def test_evaluate_isolates_a_failing_round_and_never_retries_it(monkeypatch):
    """At the evaluate grain: the first round's delivery blowing up must not stop the
    second round in the same tick — and the failed round's already-committed marker means
    it is never retried."""
    sent: list[dict] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    calls = {"n": 0}

    def flaky_deliver(per_device, prepare):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("push service down")
        return varsel.deliver_to_devices(per_device, prepare)

    store, _fake = _configure_evaluate(
        issues=[_issued("1200", "red", scope="ettermiddagsrushet 15:00–18:00"),
                _issued("1400", "yellow", scope="ettermiddagsrushet 15:00–18:00")],
        journeys=lambda: {"j1": _j(begin="15:30", end="17:00")},
        deliver=flaky_deliver,
    )
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}), _capture_logs() as records:
        togvarsel_notify.evaluate(now=_monday(13, 30))
        # 1200 red failed in delivery; 1400 yellow (an update against red) still went out
        assert len(sent) == 1 and "gult" in sent[0]["title"]
        day = store.by_date[MONDAY_STR]
        assert day["1200"] == {"level": "red", "pushed": True}  # committed before fan-out
        assert day["1400"] == {"level": "yellow", "pushed": True}
        assert any(r.levelno >= logging.ERROR and "1200" in r.getMessage() for r in records)
        togvarsel_notify.evaluate(now=_monday(13, 31))
    assert len(sent) == 1  # the failed round is never retried — its marker is committed


def test_evaluate_persists_after_each_delivery_and_once_for_suppression_only_ticks(monkeypatch):
    """Ordering: the persist rides AFTER the round's fan-out (the in-memory marker already
    protects this process), and a tick that only recorded suppressions still persists them."""
    order: list[str] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: order.append("send") or "sent")

    class _OrderedStore(_MarkerStore):
        def store(self, date_str, markers):
            order.append("store")
            super().store(date_str, markers)

    _configure_evaluate(issues=[_issued("0630", "yellow")], store=_OrderedStore())
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}):
        togvarsel_notify.evaluate(now=_monday(6, 31))
    assert order == ["send", "store"]
    # suppression-only tick: a green baseline decides, nothing delivers, one persist at the end
    order.clear()
    store2, _fake = _configure_evaluate(issues=[_issued("0630", "green")], store=_OrderedStore())
    togvarsel_notify.evaluate(now=_monday(6, 31))
    assert order == ["store"]
    assert store2.by_date[MONDAY_STR]["0630"] == {"level": "green", "pushed": False}


def test_evaluate_survives_a_lost_persist_and_a_restart_is_bounded_by_timeliness(monkeypatch):
    """The accepted crash window, end to end against varsel's REAL marker accessors: when persisting
    the state blob fails (GCS down), the in-memory marker still dedups this process; a restart
    that lost the marker re-evaluates — the one accepted duplicate — bounded by the deadline."""
    sent: list[dict] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(varsel, "_save_state", lambda: None)  # every persist silently lost
    with varsel._state_lock:
        saved_rounds = deepcopy(varsel._togvarsel_rounds)
    _configure_evaluate(
        issues=[_issued("0630", "yellow")],
        load_markers=varsel.togvarsel_rounds_for,
        store_markers=varsel.store_togvarsel_rounds,
    )
    try:
        with _push_devices({"dev-1": _dev("https://push.example.com/1")}):
            togvarsel_notify.evaluate(now=_monday(6, 31))
            assert len(sent) == 1
            # the persist was lost, but the IN-MEMORY marker still prevents a re-push this process
            togvarsel_notify.evaluate(now=_monday(6, 35))
            assert len(sent) == 1
            # "restart" before the deadline: the unpersisted marker is gone → the accepted duplicate
            with varsel._state_lock:
                varsel._togvarsel_rounds.clear()
            togvarsel_notify.evaluate(now=_monday(6, 40))
            assert len(sent) == 2
            # "restart" past the deadline: timeliness bounds the window — suppressed, no third push
            with varsel._state_lock:
                varsel._togvarsel_rounds.clear()
            togvarsel_notify.evaluate(now=_monday(7, 10))
            assert len(sent) == 2
            marker = varsel.togvarsel_rounds_for(MONDAY_STR)["0630"]
            assert marker == {"level": "yellow", "pushed": False}
    finally:
        with varsel._state_lock:
            # IN PLACE, not a rebind: since U13 the markers live in `varsel_state` and `varsel`
            # only aliases them, so assigning to the alias restores nothing and leaks this test's
            # markers into every test that runs after it.
            varsel._togvarsel_rounds.clear()
            varsel._togvarsel_rounds.update(saved_rounds)


# ---------------------------------------------------------------------------- the dev-only test trigger


def _trigger_client() -> TestClient:
    """The trigger's router mounted the way main mounts it (include_router), nothing else."""
    app = FastAPI()
    app.include_router(togvarsel_notify.router)
    return TestClient(app)


@contextmanager
def _deployed_as(project: str | None, override: str | None = None):
    """Run the body as if deployed in `project` (same shape as test_varsel_api's helper —
    deployment.environment_label reads the environment per call, so setting it around the
    request is enough)."""
    before = {k: os.environ.get(k) for k in ("GOOGLE_CLOUD_PROJECT", "KIX_ENVIRONMENT")}
    for key, value in (("GOOGLE_CLOUD_PROJECT", project), ("KIX_ENVIRONMENT", override)):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_test_trigger_sends_the_synthetic_round_through_the_real_pipe(monkeypatch):
    """One POST in dev delivers one synthetic push through the REAL composition + delivery
    path — forecast-framed title with the environment prefix, brand icon and click-through, a
    body that says it is a test — and never touches the round markers, so a morning of test
    pushes cannot eat a real round's dedup or feed a follow-up a synthetic baseline."""
    sent: list[dict] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    # tag_title is varsel's REAL tagger here: the env prefix is part of what the trigger proves.
    store, fake = _configure_evaluate(issues=[], tag_title=varsel._tag_environment)
    with (
        _deployed_as("ent-kixavvik-dev"),
        _push_devices({"dev-trig-1": _dev("https://push.example.com/t", brand="entur")}),
    ):
        r = _trigger_client().post("/rushvarsel-api/test-notify", json={"device_id": "dev-trig-1"})
    assert r.status_code == 200 and r.json() == {"status": "sent"}
    (payload,) = sent
    assert payload["title"] == "[dev] Rushvarsel: gult nivå for morgenrushet"  # real composition
    assert "testvarsel" in payload["body"].lower()  # clearly a test, never a real forecast
    assert payload["url"] == "/rushvarsel"
    assert payload["icon"] == varsel._BRAND_ASSETS["entur"]["icon"]
    assert "situation_number" not in payload
    assert store.stores == 0 and store.by_date == {}  # NEVER touches round markers
    assert fake.queries == []  # and needs no geography: the round is synthetic, not matched


def test_the_test_trigger_is_refused_everywhere_but_dev(monkeypatch):
    """The trigger fails closed: tst, prd, unset and unrecognized labels are all refused with 404 —
    before any delivery seam is touched, so nothing can be sent however the module is wired."""
    monkeypatch.setattr(
        varsel, "_send_push",
        lambda sub, payload: (_ for _ in ()).throw(AssertionError("sent outside dev")),
    )
    _configure_evaluate(issues=[], tag_title=varsel._tag_environment)
    client = _trigger_client()
    scenarios = (
        ("ent-kixavvik-tst", None),  # tst
        ("ent-kixavvik-prd", None),  # prd: environment_label() is None
        (None, None),  # unset: reads as production (unlabelled), still refused
        ("ent-kixavvik-prd", "staging"),  # an unrecognized label is not dev either
    )
    with _push_devices({"dev-trig-1": _dev("https://push.example.com/t")}):
        for project, override in scenarios:
            with _deployed_as(project, override):
                r = client.post("/rushvarsel-api/test-notify", json={"device_id": "dev-trig-1"})
                assert r.status_code == 404, (project, override, r.status_code)


def test_the_test_trigger_maps_delivery_outcomes_to_the_test_push_statuses(monkeypatch):
    """The non-happy statuses mirror /varsel-api/test — 503 push-not-ready, 404 unknown device,
    425 fresh-gone (kept for retry), 410 stale-gone (dropped), 502 send error — each read from
    the REAL delivery outcome, and no failure branch touches the round markers."""
    client = _trigger_client()
    with _deployed_as("ent-kixavvik-dev"):
        store, _ = _configure_evaluate(issues=[], push_ready=lambda: False)
        r = client.post("/rushvarsel-api/test-notify", json={"device_id": "dev-trig-1"})
        assert r.status_code == 503  # VAPID/push not configured

        store, _ = _configure_evaluate(issues=[])
        with _push_devices({}):
            r = client.post("/rushvarsel-api/test-notify", json={"device_id": "unknown-dev"})
            assert r.status_code == 404  # no such device

        monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "gone")
        with _push_devices({"dev-trig-1": _dev("https://push.example.com/t")}):
            r = client.post("/rushvarsel-api/test-notify", json={"device_id": "dev-trig-1"})
            assert r.status_code == 425  # fresh device saying gone is kept (grace window)
            with varsel._state_lock:
                assert "dev-trig-1" in varsel._devices

        with _push_devices(
            {"dev-trig-1": _dev("https://push.example.com/t", created="2020-01-01T00:00:00+00:00")}
        ):
            r = client.post("/rushvarsel-api/test-notify", json={"device_id": "dev-trig-1"})
            assert r.status_code == 410  # stale device saying gone is dropped
            with varsel._state_lock:
                assert "dev-trig-1" not in varsel._devices

        monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "error")
        with _push_devices({"dev-trig-1": _dev("https://push.example.com/t")}):
            r = client.post("/rushvarsel-api/test-notify", json={"device_id": "dev-trig-1"})
            assert r.status_code == 502  # transient send error
    assert store.stores == 0  # no failure branch touches the round markers



# ---------------------------------------------------------------------------- the rush outlook

def _configure_outlook(**overrides):
    """The read-only subset rush_outlook needs, wired the way main does. Deliberately WITHOUT the
    delivery seams: the panel must work off the four it names and nothing else. `journeys` are
    the STORE's (`device_journeys`, one device per read); the cycle's mirror stays empty, which is
    every follower's mirror (zero-downtime U4)."""
    issues = overrides.pop("issues", [_issued("0630", "yellow")])
    journeys = overrides.pop("journeys", {"j1": _j()})
    mirror = overrides.pop("mirror", {})
    fake = _StationsBQ(refs=sorted(STATIONS))
    kwargs = {
        "bq_client": fake,
        "enabled": lambda: True,
        "push_ready": lambda: True,
        "issued_today": lambda: deepcopy(list(issues)),
        "journeys": lambda: deepcopy(dict(mirror)),
        "device_journeys": lambda device_id: {
            jid: deepcopy(j) for jid, j in journeys.items() if j.get("device_id") == device_id
        },
        "expand_refs": _ident,
    }
    kwargs.update(overrides)
    togvarsel_notify._station_state.clear()
    togvarsel_notify.configure(**kwargs)
    return fake


def _warm_stations(fake, now):
    """What the poller does on its first tick of the day; the panel reads this cache and never
    fetches, so every outlook test has to warm it explicitly."""
    assert togvarsel_notify.station_set(now) == frozenset(STATIONS)
    return fake


def test_the_rushes_and_their_windows_are_derived_from_the_notify_specs():
    """One source for "which rush" and "how long it lasts" — a scope change must move the panel
    and the pushes together, so these are computed from NOTIFY_SPECS, never restated."""
    assert togvarsel_notify.RUSH_OF_ISSUE == {
        "0630": "am", "0700": "am", "1200": "pm", "1400": "pm",
    }
    assert togvarsel_notify.RUSH_WINDOWS == {"am": (6.5, 9.0), "pm": (15.0, 18.0)}


def test_the_panel_reads_the_pollers_station_cache_and_never_queries():
    """cached_station_set is read-only: /rushvarsel-api is public and unauthenticated, and
    station_set has no failure cooldown, so one scan per page view during an outage is the thing
    this prevents. A cold cache yields no panel rather than a query."""
    fake = _configure_outlook()
    assert togvarsel_notify.cached_station_set(_monday(6, 31)) is None
    assert togvarsel_notify.rush_outlook("dev-1", now=_monday(6, 31)) == []
    assert fake.queries == []  # the request path issued nothing

    _warm_stations(fake, _monday(6, 31))
    assert len(fake.queries) == 1
    assert [f.rush for f in togvarsel_notify.rush_outlook("dev-1", now=_monday(6, 31))] == ["am"]
    assert len(fake.queries) == 1  # still just the poller's own fetch
    # yesterday's cache is not today's geography
    assert togvarsel_notify.cached_station_set(datetime(2026, 9, 1, 6, 31, tzinfo=_OSLO)) is None


# A device registered for BOTH rushes: the default _j() is a morning commute only, and relevance
# is per rush, so a two-rush expectation needs a two-registration device (the real commuter case —
# one journey in each direction).
BOTH_RUSHES = {"j1": _j(begin="07:00", end="08:30"), "j2": _j(begin="16:00", end="17:00")}


def test_the_panel_serves_the_newest_forecast_per_rush():
    """A follow-up supersedes its baseline here exactly as it does on /rushvarsel — otherwise the
    two surfaces quote different numbers for one rush, and the reader cannot tell which applies."""
    fake = _configure_outlook(
        issues=[
            _issued("0630", "yellow", "morgenrushet 06:30–09:00", p=0.21),
            _issued("0700", "red", "morgenrushet 07:00–09:00", p=0.44),
            _issued("1200", "green", "ettermiddagsrushet 15:00–18:00", p=0.04),
        ],
        journeys=BOTH_RUSHES,
    )
    _warm_stations(fake, _monday(7, 5))
    outlook = togvarsel_notify.rush_outlook("dev-1", now=_monday(7, 5))
    assert [(f.rush, f.issue, f.level) for f in outlook] == [
        ("am", "0700", "red"),
        ("pm", "1200", "green"),
    ]
    # am before pm, and the payload is the issue's own text and probability
    assert outlook[0].scope == "morgenrushet 07:00–09:00" and outlook[0].p == 0.44


def test_a_rush_that_is_over_is_dropped_but_the_other_is_not():
    """"if not already over" is the RUSH's end (09:00 / 18:00): a level nobody can act on is
    noise, while the afternoon's forecast is exactly what a reader at 09:30 wants."""
    fake = _configure_outlook(
        issues=[_issued("0700", "red"), _issued("1400", "yellow", "ettermiddagsrushet 15:00–18:00")],
        journeys=BOTH_RUSHES,
    )
    _warm_stations(fake, _monday(8, 59))
    assert [f.rush for f in togvarsel_notify.rush_outlook("dev-1", now=_monday(8, 59))] == [
        "am", "pm",
    ]
    assert [f.rush for f in togvarsel_notify.rush_outlook("dev-1", now=_monday(9, 0))] == ["pm"]
    assert togvarsel_notify.rush_outlook("dev-1", now=_monday(18, 0)) == []


def test_the_panel_window_is_the_whole_rush_not_the_part_still_ahead():
    """The one deliberate difference from match_journeys. A 07:00–08:00 registration is matched
    for the push at 07:00 and would NOT be at 08:15 (its window has passed) — but the panel must
    still show the morning level while the morning rush is on, or it goes blank at 08:00 with the
    trains still late."""
    journeys = {"j1": _j(begin="07:00", end="08:00")}
    fake = _configure_outlook(issues=[_issued("0700", "red")], journeys=journeys)
    _warm_stations(fake, _monday(8, 15))
    matched, _devices = togvarsel_notify.match_journeys(
        "0700", journeys, STATIONS, _ident, now=_monday(8, 15)
    )
    assert matched == []  # the push would not go out any more
    assert [f.rush for f in togvarsel_notify.rush_outlook("dev-1", now=_monday(8, 15))] == ["am"]


def test_the_panel_is_per_rush_and_per_device():
    """Relevance is the push's own three tests, asked per rush: a morning-only registration gets
    the morning card and not the afternoon one, and another device's journey is not this one's."""
    fake = _configure_outlook(
        issues=[_issued("0700", "red"), _issued("1400", "yellow", "ettermiddagsrushet 15:00–18:00")],
        journeys={
            "j1": _j(begin="07:30", end="08:15", device="dev-morning"),
            "j2": _j(begin="16:00", end="16:45", device="dev-afternoon"),
            "j3": _j(begin="10:00", end="11:00", device="dev-midday"),
            "j4": _j(from_stop="NSR:StopPlace:99999", begin="07:30", end="08:15",
                     device="dev-elsewhere"),
            "j5": _j(begin="07:30", end="08:15", days=(5, 6), device="dev-weekend"),
        },
    )
    _warm_stations(fake, _monday(7, 5))
    at = _monday(7, 5)
    assert [f.rush for f in togvarsel_notify.rush_outlook("dev-morning", now=at)] == ["am"]
    assert [f.rush for f in togvarsel_notify.rush_outlook("dev-afternoon", now=at)] == ["pm"]
    assert togvarsel_notify.rush_outlook("dev-midday", now=at) == []  # between the rushes
    assert togvarsel_notify.rush_outlook("dev-elsewhere", now=at) == []  # outside the geography
    assert togvarsel_notify.rush_outlook("dev-weekend", now=at) == []  # not today's weekday
    assert togvarsel_notify.rush_outlook("dev-unknown", now=at) == []  # no registrations at all


def test_the_panel_reads_the_store_not_the_cycles_mirror():
    """A follower's mirror is never seeded (zero-downtime U4), so a panel reading it would be blank
    on every pod but the leader. The store answers per device on any pod; the mirror is not asked."""
    mirror = {"j-mirror": _j(device="dev-mirror")}
    fake = _configure_outlook(issues=[_issued("0700", "red")], journeys={"j1": _j()},
                              mirror=mirror)
    _warm_stations(fake, _monday(7, 5))
    assert [f.rush for f in togvarsel_notify.rush_outlook("dev-1", now=_monday(7, 5))] == ["am"]
    assert togvarsel_notify.rush_outlook("dev-mirror", now=_monday(7, 5)) == []


def test_the_outlook_route_runs_off_the_event_loop():
    """The store read is a blocking Firestore query (and the weekly renewal write the /varsel
    journey list also performs), so the route is a plain ``def`` in the threadpool."""
    import inspect

    assert not inspect.iscoroutinefunction(togvarsel_notify.outlook)


def test_the_panel_is_gated_on_the_delivery_flag():
    """The panel's claim is "you are being notified about this", so the delivery flag is what
    turns it on — the same read /varsel-api/config surfaces as togvarsel_notify."""
    fake = _configure_outlook(enabled=lambda: False)
    _warm_stations(fake, _monday(6, 31))
    assert togvarsel_notify.rush_outlook("dev-1", now=_monday(6, 31)) == []


def test_the_panel_is_empty_before_the_first_forecast_and_on_a_weekend():
    fake = _configure_outlook(issues=[])
    _warm_stations(fake, _monday(6, 0))
    assert togvarsel_notify.rush_outlook("dev-1", now=_monday(6, 0)) == []
    # a weekend issues nothing, so togvarsel's snapshot is empty and so is the panel
    fake = _configure_outlook(issues=[])
    assert togvarsel_notify.station_set(SATURDAY) == frozenset(STATIONS)
    assert togvarsel_notify.rush_outlook("dev-1", now=SATURDAY) == []


def test_one_malformed_registration_never_breaks_the_panel():
    """The per-journey guard match_journeys' loop has: a legacy begin/end must cost that one
    record, not the whole panel."""
    fake = _configure_outlook(
        issues=[_issued("0700", "red")],
        journeys={"bad": {**_j(), "begin": "kl. sju"}, "good": _j()},
    )
    _warm_stations(fake, _monday(7, 5))
    with _capture_logs() as records:
        assert [f.rush for f in togvarsel_notify.rush_outlook("dev-1", now=_monday(7, 5))] == ["am"]
    assert any("malformed" in r.getMessage() for r in records)


def test_the_outlook_endpoint_serves_the_panel(monkeypatch):
    """The route's own job: pass the device id through and shape the response.

    The policy is checked above as a pure function with an explicit clock; this one cannot be,
    because the route deliberately takes no `now` — so rush_outlook is stubbed here rather than
    pinning the suite to the wall clock."""
    asked: list[str] = []

    def _stub(device_id, now=None):
        asked.append(device_id)
        assert now is None  # the route must not invent a clock of its own
        return [togvarsel_notify.RushForecast(
            rush="am", issue="0700", scope="morgenrushet 07:00–09:00", level="red", p=0.44
        )]

    monkeypatch.setattr(togvarsel_notify, "rush_outlook", _stub)
    app = FastAPI()
    app.include_router(togvarsel_notify.router)
    with TestClient(app) as client:
        body = client.get("/rushvarsel-api/outlook", params={"device_id": "device-0001"}).json()
        # no device_id at all is a 422, as everywhere else that takes one
        assert client.get("/rushvarsel-api/outlook").status_code == 422
        # and the bounds /varsel-api's DeviceIn holds: each call is a store read on a public route
        for bad in ("", "short", "x" * 101):
            assert client.get("/rushvarsel-api/outlook",
                              params={"device_id": bad}).status_code == 422, bad
    assert asked == ["device-0001"]
    assert body == {"rushes": [{"rush": "am", "issue": "0700",
                                "scope": "morgenrushet 07:00–09:00", "level": "red", "p": 0.44}]}


def test_both_routes_404_while_the_master_gate_is_off(monkeypatch):
    """SHOW_RUSHVARSEL off (its default) 404s the outlook panel AND the dev-only test trigger.

    The trigger is the one that matters, and it is why the gate had to be at router level rather
    than folded into the `enabled` seam: _TRIGGER_SEAMS deliberately omits `enabled` so the pipe
    can be proven before the delivery flag opens, which means nothing else here would stop it
    sending a real rush-forecast push while the product is meant to be dark. Checked in dev, where
    it is otherwise reachable — anywhere else its own environment gate 404s it regardless, which
    would make this pass for the wrong reason.
    """
    monkeypatch.setattr(togvarsel_notify, "rush_outlook", lambda device_id, now=None: [])
    app = FastAPI()
    app.include_router(togvarsel_notify.router)
    with _deployed_as("ent-kixavvik-dev"), TestClient(app) as client:
        for value in ("false", "", "0", "off", "disabled"):
            os.environ["SHOW_RUSHVARSEL"] = value
            try:
                assert client.get(
                    "/rushvarsel-api/outlook", params={"device_id": "device-0001"}
                ).status_code == 404, value
                # The gate answers before the device_id bounds: a 422 would mention the route.
                assert client.get(
                    "/rushvarsel-api/outlook", params={"device_id": "x"}
                ).status_code == 404, value
                assert client.post(
                    "/rushvarsel-api/test-notify", json={"device_id": "dev-trig-1"}
                ).status_code == 404, value
            finally:
                os.environ["SHOW_RUSHVARSEL"] = "true"
        # And on again: the outlook answers, so the 404s above are the gate and not a broken app.
        assert client.get(
            "/rushvarsel-api/outlook", params={"device_id": "device-0001"}
        ).status_code == 200


# ---- The round record /rushhistorikk scores ------------------------------------------------


def _sent_ok(monkeypatch) -> list:
    """Stub delivery to succeed. Every test below that has a matching device needs this: without
    it varsel._send_push reaches the real pywebpush against push.example.com."""
    sent: list = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    return sent


def test_a_pushing_round_is_recorded_with_its_kind_and_device_counts(monkeypatch):
    """The row carries the policy decision AND the fan-out counts. `sent` means a push was
    ISSUED — the counts are what say whether it reached anybody."""
    sent = _sent_ok(monkeypatch)
    _store, fake = _configure_evaluate(issues=[_issued("0630", "yellow")])
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}):
        togvarsel_notify.evaluate(now=_monday(6, 31))
    assert len(sent) == 1
    row = fake.round_for("0630")
    assert row["level"] == "yellow" and row["sent"] is True
    assert row["kind"] == "warning" and row["reason"] is None
    assert row["baseline_level"] is None, "a baseline round has no baseline"
    assert row["matched_devices"] == 1 and row["n_sent"] == 1
    assert row["d"] == _monday(6, 31).date()


def test_a_suppressed_round_is_recorded_with_its_reason():
    """Suppressions are the rows a page needs most: on /historikk the sub-threshold decisions are
    83 % of the log and the only thing that makes a miss visible. A green baseline pushes nothing
    and must still leave a trace."""
    _store, fake = _configure_evaluate(issues=[_issued("0630", "green")])
    togvarsel_notify.evaluate(now=_monday(6, 31))
    row = fake.round_for("0630")
    assert row["sent"] is False and row["reason"] == "green_baseline"
    assert row["kind"] is None and row["n_sent"] is None


def test_a_confirming_follow_up_records_the_level_left_in_force(monkeypatch):
    """0700 confirms a pushed yellow: it sends nothing, yet the traveller holds a yellow warning.
    baseline_level is what /rushhistorikk grades that round on, and it is recorded rather than read
    off the 0630 row — one swallowed write there would otherwise invert the verdict."""
    _sent_ok(monkeypatch)
    issues = [_issued("0630", "yellow")]
    _store, fake = _configure_evaluate(issues=issues)
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}):
        togvarsel_notify.evaluate(now=_monday(6, 31))
        issues.append(_issued("0700", "yellow"))  # issued_today only grows as issues compute
        togvarsel_notify.evaluate(now=_monday(7, 1))
    row = fake.round_for("0700")
    assert row["sent"] is False and row["reason"] == "no_change"
    assert row["baseline_level"] == "yellow"


def test_an_untimely_follow_up_still_records_the_standing_level():
    """The 12:00 yellow / 14:00 red case. The 14:00 round is past its deadline so nothing is sent,
    and the level in force is still the yellow the traveller received — recorded here so the page
    grades that yellow rather than crediting a red nobody saw. This is why _effective_baseline is
    resolved BEFORE the deadline check."""
    issues = [_issued("1200", "yellow")]
    _store, fake = _configure_evaluate(issues=issues)
    togvarsel_notify.evaluate(now=_monday(12, 1))
    issues.append(_issued("1400", "red"))  # the 14:00 forecast does not exist at 12:01
    togvarsel_notify.evaluate(now=_monday(18, 0))
    row = fake.round_for("1400")
    assert row["sent"] is False and row["reason"] == "untimely"
    assert row["level"] == "red" and row["baseline_level"] == "yellow"


def test_a_mixed_pass_records_both_the_suppression_and_the_push(monkeypatch):
    """THE regression the recording placement exists for. One tick back-fills 0630 (untimely) and
    pushes 0700. The two sites that persist markers are inside the pushes loop and in the
    not-pushes branch, and neither iterates the suppressed decisions — so a hook on either would
    drop 0630 silently, and the page would read it as "never evaluated" rather than as the outcome
    it was."""
    _sent_ok(monkeypatch)
    _store, fake = _configure_evaluate(issues=[_issued("0700", "red"), _issued("0630", "red")])
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}):
        togvarsel_notify.evaluate(now=_monday(7, 30))
    assert sorted(r["issue"] for r in fake.rounds) == ["0630", "0700"]
    assert fake.round_for("0630")["reason"] == "untimely"
    assert fake.round_for("0700")["sent"] is True


def test_a_round_matching_zero_devices_is_still_recorded_as_sent():
    """`sent` is the policy decision, not delivery. Nobody is registered, so the push reaches no
    one — and the row says so through the counts rather than by pretending nothing was issued."""
    _store, fake = _configure_evaluate(issues=[_issued("0630", "red")], journeys=dict)
    togvarsel_notify.evaluate(now=_monday(6, 31))
    row = fake.round_for("0630")
    assert row["sent"] is True and row["matched_devices"] == 0 and row["n_sent"] == 0


def test_recording_failure_costs_neither_the_push_nor_the_markers(monkeypatch):
    """Best-effort, and the ordering proves it: the write is attempted after the markers persist,
    so a dead table cannot cost a traveller a notification."""
    sent = _sent_ok(monkeypatch)
    store, fake = _configure_evaluate(
        issues=[_issued("0630", "red")],
        bq=_StationsBQ(refs=sorted(STATIONS), rounds_fail=True),
    )
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}):
        togvarsel_notify.evaluate(now=_monday(6, 31))
    assert len(sent) == 1, "the push must survive a failed round write"
    assert store.by_date and store.stores >= 1, "markers must survive it too"
    assert fake.rounds == []


def test_the_test_trigger_records_no_round(monkeypatch):
    """POST /rushvarsel-api/test-notify reaches push_round directly on the public dev host with a
    caller-supplied device_id and a synthetic 0630 decision. On a first-write-wins MERGE that write
    would take the day's row and the real 06:30 round would be lost for good — so the recording
    hook lives in _evaluate and nowhere else."""
    _sent_ok(monkeypatch)
    _store, fake = _configure_evaluate(issues=[])
    decision = togvarsel_notify.decide_issue(_issued("0630", "yellow"), {}, now=_monday(6, 31))
    with _push_devices({"dev-1": _dev("https://push.example.com/1")}):
        togvarsel_notify.push_round(
            decision, _issued("0630", "yellow"), ["j1"], ["dev-1"], **_wiring()
        )
    assert fake.rounds == [], "push_round must never record a round"


def test_the_round_table_is_created_once_and_survives_a_closed_gate():
    """ensure_table runs at startup, not on first write: evaluate() returns before deciding
    anything while the flag is off — permanently true in tst — so a lazily created table would
    never exist in the one environment that never notifies, and the page would 503 there."""
    fake = _StationsBQ(refs=sorted(STATIONS))
    _reset_stations(fake)
    togvarsel_notify.ensure_table()
    togvarsel_notify.ensure_table()
    assert fake.rounds_ddl == 1, "the readiness latch must not re-issue the DDL"


class _MonkeyPatch:
    """Minimal monkeypatch shim so the tests run standalone (without pytest)."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


if __name__ == "__main__":
    import inspect

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            mp = _MonkeyPatch() if "monkeypatch" in inspect.signature(fn).parameters else None
            try:
                fn(mp) if mp else fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            finally:
                if mp:
                    mp.undo()
    raise SystemExit(1 if failures else 0)
