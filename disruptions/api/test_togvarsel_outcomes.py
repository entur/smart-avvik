"""Checks for togvarsel_outcomes — the realized rush outcome /rushhistorikk scores against.

Hermetic: the BigQuery client is a fake dispatching on SQL text, and every entry point takes an
explicit Oslo-aware `now`, so nothing here reads a clock or a network.

Run: uv run --with google-cloud-bigquery python test_togvarsel_outcomes.py   (or under pytest)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# --- Stub the BigQuery client BEFORE importing the module (main builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import togvarsel_outcomes

_OSLO = ZoneInfo("Europe/Oslo")
MONDAY = date(2026, 8, 31)


def _oslo(hour: int, minute: int = 0, d: date = MONDAY) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=_OSLO)


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self, timeout=None):
        return list(self._rows)


class _FakeBQ:
    """Dispatches on SQL text. Records every MERGE's parameters, which is the only way a wrong
    write is visible — the module logs and swallows, so an unrecorded failure passes silently."""

    def __init__(self, provisional=(), settled=(), work_set=(), fail_on=None):
        self.provisional = list(provisional)
        self.settled = list(settled)
        self.work_set = list(work_set)
        self.fail_on = fail_on  # a substring: any query containing it raises
        self.ddl = 0
        self.merges: list[dict] = []
        self.queries: list[str] = []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"fake failure on {self.fail_on}")
        params = {}
        if job_config is not None:
            for p in job_config.query_parameters:
                params[p.name] = list(p.values) if hasattr(p, "values") else p.value
        if sql.lstrip().startswith("CREATE TABLE"):
            self.ddl += 1
            return _FakeJob([])
        if sql.lstrip().startswith("MERGE"):
            kind = "settled" if "settled = TRUE" in sql else "provisional"
            self.merges.append({"kind": kind, **params})
            return _FakeJob([])
        if "togvarsel_issued" in sql:
            return _FakeJob([{"d": d} for d in self.work_set])
        if "rail_calls_planned_today" in sql:
            return _FakeJob(self.provisional)
        if "last_recorded" in sql:
            return _FakeJob(self.settled)
        raise AssertionError(f"unexpected query: {sql[:80]}")

    def merge_for(self, scope: str, kind: str) -> dict:
        rows = [m for m in self.merges if m["scope"] == scope and m["kind"] == kind]
        assert len(rows) == 1, f"expected one {kind} merge for {scope}, got {len(rows)}"
        return rows[0]


def _row(scope: str, trouble10, n_calls=1000, n_resolved=990, d: date = MONDAY) -> dict:
    return {
        "d": d,
        "scope": scope,
        "trouble10": trouble10,
        "n_calls": n_calls,
        "n_resolved": n_resolved,
        "n_unresolved": n_calls - n_resolved,
    }


def _reset(fake) -> None:
    togvarsel_outcomes.configure(bq_client=fake)
    togvarsel_outcomes._state.clear()
    togvarsel_outcomes._table_ready = False


# ---- The level mapping: the bars, the boundaries, and the identity it rests on ----------------


def test_the_level_bands_are_the_bar_and_twice_the_bar_per_scope():
    """green below the bar, yellow to twice it, red at or above. Both bounds INCLUSIVE at the
    lower edge, because the training label is `>= bar`."""
    for scope, cases in {
        "am": [(0.09, "green"), (0.10, "yellow"), (0.19, "yellow"), (0.20, "red")],
        "amrest": [(0.09, "green"), (0.10, "yellow"), (0.19, "yellow"), (0.20, "red")],
        "pm": [(0.14, "green"), (0.15, "yellow"), (0.29, "yellow"), (0.30, "red")],
    }.items():
        for value, expected in cases:
            got = togvarsel_outcomes.level_for(scope, value)
            assert got == expected, f"{scope} {value}: expected {expected}, got {got}"


def test_amrest_uses_the_morning_bar_not_the_afternoon_one():
    """The 0700 issue's label is `am_t10_rest0700 >= 0.10`, the morning bar over a shorter window.
    Reading pm's 0.15 there would call a troubled morning green."""
    assert togvarsel_outcomes.BARS["amrest"] == togvarsel_outcomes.BARS["am"] == 0.10
    assert togvarsel_outcomes.level_for("amrest", 0.12) == "yellow"
    assert togvarsel_outcomes.level_for("pm", 0.12) == "green"


def test_a_non_green_level_coincides_exactly_with_the_training_label():
    """KTD3's whole claim, asserted directly rather than argued: the green boundary IS the label's
    threshold, so the page splits "bad" into two severities and never disagrees with the models
    about whether a rush was bad at all."""
    for scope, bar in togvarsel_outcomes.BARS.items():
        for value in (0.0, bar / 2, bar - 1e-9, bar, bar + 1e-9, 2 * bar, 2 * bar + 0.1, 1.0):
            label_true = value >= bar
            level = togvarsel_outcomes.level_for(scope, value)
            assert (level != "green") == label_true, (
                f"{scope} {value}: level={level} but label={label_true}"
            )


def test_a_null_share_has_no_level_and_is_never_green():
    """trouble10 is NULL exactly when nothing in the window resolved. A day with no evidence is
    `ukjent`; calling it green would manufacture a correct rejection out of an outage."""
    assert togvarsel_outcomes.level_for("am", None) is None


# ---- Eligibility: the training chain's DAY-level test -----------------------------------------


def test_eligibility_is_day_level_and_amrest_inherits_it():
    """09_training.sql thresholds am_n and pm_n only. A per-scope test would mark 0700 `ukjent` on
    days the chain scored happily, because amrest's window is a strict subset of am's."""
    assert togvarsel_outcomes.eligible_for({"am": 1500, "pm": 1400, "amrest": 900}) is True
    assert togvarsel_outcomes.eligible_for({"am": 1500, "pm": 250, "amrest": 900}) is False
    assert togvarsel_outcomes.eligible_for({"am": 250, "pm": 1400, "amrest": 900}) is False
    # amrest's own count is never consulted, however thin it is
    assert togvarsel_outcomes.eligible_for({"am": 1500, "pm": 1400, "amrest": 1}) is True


def test_the_eligibility_boundary_is_strictly_above_300():
    assert togvarsel_outcomes.eligible_for({"am": 300, "pm": 1400}) is False
    assert togvarsel_outcomes.eligible_for({"am": 301, "pm": 301}) is True


# ---- The due gates ---------------------------------------------------------------------------


def test_the_morning_scopes_are_due_at_0920_and_not_before():
    """:20 rather than :05 because both provisional sources are TTL-gated (15 and 60 min) off the
    SX rebuild, so an earlier read routinely stops before the window it measures ends."""
    assert togvarsel_outcomes.provisional_due(9.0 + 19 / 60, set()) == ()
    assert togvarsel_outcomes.provisional_due(9.0 + 20 / 60, set()) == ("am", "amrest")


def test_the_afternoon_scope_is_not_due_in_the_morning():
    assert "pm" not in togvarsel_outcomes.provisional_due(9.0 + 30 / 60, set())
    assert togvarsel_outcomes.provisional_due(18.0 + 20 / 60, {"am", "amrest"}) == ("pm",)


def test_a_scope_already_checked_today_is_not_due_again():
    assert togvarsel_outcomes.provisional_due(18.5, {"am", "amrest", "pm"}) == ()


def test_the_authoritative_pass_is_due_after_0400_utc():
    """last_recorded's one daily rewrite landed at ~03:04 UTC when it was measured; 04:00 is
    varsel-label-daily's own slot, chosen the same way."""
    assert togvarsel_outcomes.settled_due(datetime(2026, 8, 31, 3, 30, tzinfo=UTC), False) is False
    assert togvarsel_outcomes.settled_due(datetime(2026, 8, 31, 4, 5, tzinfo=UTC), False) is True
    assert togvarsel_outcomes.settled_due(datetime(2026, 8, 31, 4, 5, tzinfo=UTC), True) is False


# ---- The provisional pass --------------------------------------------------------------------


def test_the_provisional_pass_writes_the_provisional_trio_and_the_served_value():
    """The page needs something to show after the rush, so the served value is filled too — but
    marked unsettled, and excluded from every rate."""
    fake = _FakeBQ(provisional=[_row("am", 0.12), _row("amrest", 0.08)])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    am = fake.merge_for("am", "provisional")
    assert am["trouble10"] == 0.12 and am["level"] == "yellow"
    assert am["now"] == _oslo(9, 25)
    assert fake.merge_for("amrest", "provisional")["level"] == "green"


def test_the_provisional_pass_leaves_eligibility_unset():
    """rail_calls_planned_today's n_calls is not 03_day_windows.sql's COUNT(*) — deriving
    eligibility from it would manufacture an `ukjent` verdict at 09:20 for a reason unrelated to
    the day. The MERGE writes NULL, so only the settled pass decides."""
    fake = _FakeBQ(provisional=[_row("am", 0.12, n_calls=10)])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    assert "eligible" not in fake.merge_for("am", "provisional")


def test_the_provisional_pass_asks_only_for_the_scopes_whose_rush_has_ended():
    fake = _FakeBQ(provisional=[])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    asked = [q for q in fake.queries if "rail_calls_planned_today" in q]
    assert len(asked) == 1


def test_the_provisional_pass_runs_once_per_rush_per_day():
    fake = _FakeBQ(provisional=[_row("am", 0.12)])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    togvarsel_outcomes.refresh(now=_oslo(9, 30))
    assert len([q for q in fake.queries if "rail_calls_planned_today" in q]) == 1


# ---- The authoritative pass ------------------------------------------------------------------


def test_the_settled_pass_settles_every_scope_and_computes_day_level_eligibility():
    fake = _FakeBQ(
        settled=[_row("am", 0.12, 1500, 1490), _row("amrest", 0.11, 900, 890),
                 _row("pm", 0.05, 1400, 1390)],
        work_set=[MONDAY],
    )
    _reset(fake)
    togvarsel_outcomes.refresh(now=datetime(2026, 9, 1, 6, 5, tzinfo=_OSLO))
    assert {m["scope"] for m in fake.merges if m["kind"] == "settled"} == {"am", "amrest", "pm"}
    for scope in togvarsel_outcomes.SCOPES:
        assert fake.merge_for(scope, "settled")["eligible"] is True


def test_an_ineligible_day_marks_every_scope_including_amrest():
    fake = _FakeBQ(
        settled=[_row("am", 0.12, 1500, 1490), _row("amrest", 0.11, 900, 890),
                 _row("pm", 0.05, 250, 240)],
        work_set=[MONDAY],
    )
    _reset(fake)
    togvarsel_outcomes.refresh(now=datetime(2026, 9, 1, 6, 5, tzinfo=_OSLO))
    for scope in togvarsel_outcomes.SCOPES:
        assert fake.merge_for(scope, "settled")["eligible"] is False


def test_a_scope_the_query_returns_nothing_for_settles_as_unresolvable():
    """last_recorded is an immutable daily batch, so once it has landed "no resolved call in this
    window" is FINAL. Retrying it for ten days would re-run the same query to the same NULL while
    the page said `venter`."""
    fake = _FakeBQ(settled=[_row("am", 0.12, 1500, 1490), _row("pm", 0.05, 1400, 1390)],
                   work_set=[MONDAY])
    _reset(fake)
    togvarsel_outcomes.refresh(now=datetime(2026, 9, 1, 6, 5, tzinfo=_OSLO))
    amrest = fake.merge_for("amrest", "settled")
    assert amrest["unresolvable"] is True and amrest["trouble10"] is None
    assert amrest["level"] is None
    assert fake.merge_for("am", "settled")["unresolvable"] is False


def test_the_settled_merge_never_touches_the_provisional_columns():
    """The overnight disagreement rate is the measurement that decides whether the provisional pass
    earns its keep. Overwriting provisional_* would destroy it — which is the exact defect this
    design diagnoses in the /varsel marker store."""
    sql = togvarsel_outcomes._SETTLED_MERGE
    update = sql.split("THEN UPDATE SET", 1)[1].split("WHEN NOT MATCHED", 1)[0]
    assert "provisional_" not in update, "the settled UPDATE must leave provisional_* alone"
    assert "provisional_trouble10" in sql, "the INSERT arm still names them (as NULL)"


def test_both_merges_refuse_to_rewrite_a_settled_row():
    for name, sql in (
        ("provisional", togvarsel_outcomes._PROVISIONAL_MERGE),
        ("settled", togvarsel_outcomes._SETTLED_MERGE),
    ):
        assert "WHEN MATCHED AND NOT IFNULL(t.settled, FALSE)" in sql, name


def test_the_work_set_is_bounded_to_the_retry_window_and_past_days_only():
    sql = togvarsel_outcomes._WORK_SET_SQL
    assert "INTERVAL @retry_days DAY" in sql
    assert "DATE_SUB(@today, INTERVAL 1 DAY)" in sql, "today is never resolved authoritatively"
    assert togvarsel_outcomes._RETRY_DAYS == 10, "matches label_varsel_decisions.sql's window"


def test_a_day_with_no_issued_forecast_is_never_resolved():
    fake = _FakeBQ(settled=[_row("am", 0.12)], work_set=[])
    _reset(fake)
    togvarsel_outcomes.refresh(now=datetime(2026, 9, 1, 6, 5, tzinfo=_OSLO))
    assert fake.merges == []


# ---- Failure containment ---------------------------------------------------------------------


def test_a_failing_provisional_pass_does_not_stop_the_authoritative_one():
    """Per-pass isolation: the two answer different questions from different sources, and a dead
    same-day table must not cost the day's real verdicts."""
    fake = _FakeBQ(settled=[_row("am", 0.12)], work_set=[MONDAY],
                   fail_on="rail_calls_planned_today")
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(18, 25))
    assert any(m["kind"] == "settled" for m in fake.merges)


def test_a_failing_pass_backs_off_and_gives_up_after_eight_tries():
    fake = _FakeBQ(fail_on="rail_calls_planned_today")
    _reset(fake)
    for i in range(12):
        # each attempt is past the previous cooldown, so the only thing that stops it is the cap
        togvarsel_outcomes.refresh(now=_oslo(9, 25) + togvarsel_outcomes._FAIL_COOLDOWN * (i + 1))
    attempts = len([q for q in fake.queries if "rail_calls_planned_today" in q])
    assert attempts == togvarsel_outcomes._MAX_FAILS_PER_DAY, attempts


def test_the_state_resets_at_the_oslo_day_rollover():
    fake = _FakeBQ(provisional=[_row("am", 0.12)])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    before = len([q for q in fake.queries if "rail_calls_planned_today" in q])
    togvarsel_outcomes.refresh(now=_oslo(9, 25, d=date(2026, 9, 1)))
    after = len([q for q in fake.queries if "rail_calls_planned_today" in q])
    assert after == before + 1, "a new Oslo day is a fresh set of due passes"


def test_the_table_is_created_once():
    fake = _FakeBQ()
    _reset(fake)
    togvarsel_outcomes.ensure_table()
    togvarsel_outcomes.ensure_table()
    assert fake.ddl == 1


def test_refresh_is_a_no_op_without_a_client():
    togvarsel_outcomes.configure(bq_client=None)
    togvarsel_outcomes._state.clear()
    togvarsel_outcomes.refresh(now=_oslo(9, 25))  # must not raise


def test_every_query_is_bounded_by_maximum_bytes_billed():
    """A blast-radius bound on an unattended query against an upstream ET table. Not a pruning
    check: a too-narrow date bound reads as a CHEAPER query, so cost can never detect it."""
    captured: list = []

    class _Spy(_FakeBQ):
        def query(self, sql, job_config=None):
            captured.append(job_config)
            return super().query(sql, job_config)

    fake = _Spy(provisional=[_row("am", 0.12)])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    configs = [c for c in captured if c is not None]
    assert configs, "no parameterised query ran"
    assert all(c.maximum_bytes_billed == togvarsel_outcomes._MAX_BYTES_BILLED for c in configs)


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} togvarsel_outcomes checks passed.")


if __name__ == "__main__":
    _run()
