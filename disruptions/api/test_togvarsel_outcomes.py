"""Checks for togvarsel_outcomes — the realized rush outcome /rushhistorikk scores against.

Hermetic: the BigQuery client is a fake dispatching on SQL text, and every entry point takes an
explicit Oslo-aware `now`, so nothing here reads a clock or a network.

Run: uv run --with google-cloud-bigquery python test_togvarsel_outcomes.py   (or under pytest)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
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
        # (sql, params) for every query. self.queries carries the text only, which cannot answer
        # "what range did the settled SELECT actually ask for?".
        self.sent: list[tuple[str, dict]] = []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"fake failure on {self.fail_on}")
        params = {}
        if job_config is not None:
            for p in job_config.query_parameters:
                params[p.name] = list(p.values) if hasattr(p, "values") else p.value
        self.sent.append((sql, params))
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
    # Module state outside `_state` has to be reset here too, or a leaked latch makes half the suite
    # pass by accident — the shape varsel's own `_reset_state` lesson warns about.
    togvarsel_outcomes._settled_last_utc = None


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
    due = togvarsel_outcomes.settled_due
    assert due(datetime(2026, 8, 31, 3, 30, tzinfo=UTC), None) is False
    assert due(datetime(2026, 8, 31, 4, 5, tzinfo=UTC), None) is True
    assert due(datetime(2026, 8, 31, 4, 5, tzinfo=UTC), date(2026, 8, 31)) is False
    # A run recorded YESTERDAY does not satisfy today.
    assert due(datetime(2026, 8, 31, 4, 5, tzinfo=UTC), date(2026, 8, 30)) is True


def test_the_authoritative_pass_does_not_fire_at_oslo_midnight():
    """The gate is a UTC hour and the latch must be keyed on the UTC date, or the pass runs five
    hours BEFORE the batch it waits for.

    Oslo midnight is 22:00 UTC of the previous UTC day in CEST (23:00 in CET). While the latch was
    a boolean inside the module's Oslo-day state, the Oslo rollover cleared it at exactly the moment
    `hour >= 4` was trivially true, so the pass ran against a last_recorded whose newest rewrite
    predated both of that day's rushes, found nothing, and settled every scope as `unresolvable` —
    irreversibly, and with nothing logged as an error. Both offsets are pinned because the summer
    one is 22:00 and the winter one 23:00, and a fix that handled only one would look correct for
    half the year.
    """
    due = togvarsel_outcomes.settled_due
    for oslo_midnight, ran_on in (
        (datetime(2026, 9, 2, 0, 5, tzinfo=_OSLO), date(2026, 9, 1)),  # CEST: 22:05 UTC on the 1st
        (datetime(2026, 12, 2, 0, 5, tzinfo=_OSLO), date(2026, 12, 1)),  # CET: 23:05 UTC on the 1st
    ):
        assert oslo_midnight.astimezone(UTC).hour >= togvarsel_outcomes._SETTLED_DUE_UTC_HOUR
        assert due(oslo_midnight, ran_on) is False, oslo_midnight
        # ... and it does become due once the UTC day turns and 04:00 UTC passes.
        assert due(oslo_midnight.astimezone(UTC).replace(hour=4) + timedelta(days=1), ran_on) is True


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
    """The second tick is PAST the failure cooldown, deliberately. At 09:30 it sat inside the 15 min
    `_FAIL_COOLDOWN`, so this test could not tell "done" from "backing off after a raise" — and from
    2026-09-02 to 2026-09-10 it was the latter: `_run_provisional` returned None, `refresh` iterated
    it, and every pass wrote its rows, raised, and re-ran a quarter of an hour later. Both morning
    scopes are answered here because an unanswered one is legitimately still due."""
    fake = _FakeBQ(provisional=[_row("am", 0.12), _row("amrest", 0.11)])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    togvarsel_outcomes.refresh(now=_oslo(9, 25) + togvarsel_outcomes._FAIL_COOLDOWN + timedelta(minutes=1))
    assert len([q for q in fake.queries if "rail_calls_planned_today" in q]) == 1


def test_a_successful_provisional_pass_marks_its_scopes_done_and_backs_off_nothing():
    """The discriminator the suite lacked: a pass that writes and then raises leaves the done-set
    empty and a backoff entry behind, and `refresh` swallows the raise — so a test that reads only
    the merges passes either way. The backoff key is what makes the swallowed exception visible."""
    fake = _FakeBQ(provisional=[_row("am", 0.12), _row("amrest", 0.11)])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    assert togvarsel_outcomes._state["provisional_done"] == {"am", "amrest"}
    assert "provisional:am+amrest" not in togvarsel_outcomes._state["backoff"]


def test_the_afternoon_pass_asks_only_for_pm_once_the_morning_is_done():
    """With the morning never marked done, the 18:20 window asked for all three scopes under a fresh
    key with a fresh budget, and its MERGEs re-wrote the morning's provisional rows with an evening
    reading — `provisional_at` read ~20:10 on every prd weekday, and the morning skew measurement
    the pass exists for was overwritten before anybody could read it."""
    fake = _FakeBQ(provisional=[_row("am", 0.12), _row("amrest", 0.11)])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    fake.provisional = [_row("pm", 0.05)]
    togvarsel_outcomes.refresh(now=_oslo(18, 25))
    afternoon = [m for m in fake.merges if m["now"] == _oslo(18, 25)]
    assert {m["scope"] for m in afternoon} == {"pm"}
    assert togvarsel_outcomes._state["provisional_done"] == {"am", "amrest", "pm"}


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


def test_a_multi_day_backlog_is_one_select_and_only_the_work_set_is_written():
    """The catch-up path, which had no test at all — and it fires for real ONCE PER ENVIRONMENT,
    because togvarsel_issued already holds forecasts issued before this feature existed.

    One SELECT for the whole span, not one per day: at one SELECT plus three MERGEs per day a
    seven-weekday backlog was ~28 sequential BigQuery jobs in a single tick, on the sole poller
    thread, ahead of the SX rebuild and the notify pass.

    And only the WORK SET is written. The span min..max necessarily covers days that are not in it —
    a weekend, or a day that already settled — and writing those would resolve a day we never
    issued a forecast for."""
    monday, tuesday, thursday = date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 27)
    rows = []
    for d in (monday, tuesday, date(2026, 8, 26), thursday):
        rows += [_row("am", 0.12, 1500, 1490, d=d), _row("amrest", 0.11, 900, 890, d=d),
                 _row("pm", 0.05, 1400, 1390, d=d)]
    # Wednesday the 26th is deliberately absent from the work set though it lies inside the span.
    fake = _FakeBQ(settled=rows, work_set=[monday, tuesday, thursday])
    _reset(fake)
    togvarsel_outcomes.refresh(now=datetime(2026, 9, 1, 6, 5, tzinfo=_OSLO))

    # Identity, not a substring: `last_recorded` also appears in the DDL's column descriptions, so
    # a substring filter counts the table-creation job as a range SELECT.
    selects = [q for q in fake.queries if q == togvarsel_outcomes._SETTLED_QUERY]
    assert len(selects) == 1, f"expected ONE range SELECT for the whole span, got {len(selects)}"
    settled = [m for m in fake.merges if m["kind"] == "settled"]
    assert {m["d"] for m in settled} == {monday, tuesday, thursday}, "only the work set is written"
    assert len(settled) == 3 * len(togvarsel_outcomes.SCOPES)


def test_the_range_select_is_bounded_by_the_work_sets_own_min_and_max():
    monday, thursday = date(2026, 8, 24), date(2026, 8, 27)
    fake = _FakeBQ(settled=[_row("am", 0.12, d=monday)], work_set=[monday, thursday])
    _reset(fake)
    togvarsel_outcomes.refresh(now=datetime(2026, 9, 1, 6, 5, tzinfo=_OSLO))
    # the parameters the module actually sent, not the ones we hoped it would
    # Identity, not a substring: `last_recorded` also appears in the DDL's column descriptions.
    sent = [q for q in fake.sent if q[0] == togvarsel_outcomes._SETTLED_QUERY]
    assert sent, "no settled SELECT ran"
    params = sent[0][1]
    assert params["from_date"] == monday and params["to_date"] == thursday


def test_a_day_the_range_returned_nothing_for_at_all_is_left_for_the_retry_window():
    """A day NO scope resolved for writes nothing, where a day missing ONE scope settles it.

    That pair is the discriminator between the two causes of an empty read, and only one of them is
    final. With a sibling scope resolved the batch has demonstrably landed and "no resolved call in
    this window" is an answer about the data. With nothing at all, the likeliest cause is that the
    batch has not landed for that day yet — and because `resolve_outcome_state` reads
    `unresolvable` BEFORE the retry window, three rows written here would burn the day permanently
    on one early read. Left alone it stays in the work set, reads `venter`, is retried daily, and
    turns `ukjent` at day 11 by itself.
    """
    monday, thursday = date(2026, 8, 24), date(2026, 8, 27)
    fake = _FakeBQ(settled=[_row("am", 0.12, d=monday), _row("amrest", 0.11, d=monday),
                            _row("pm", 0.05, d=monday)],
                   work_set=[monday, thursday])
    _reset(fake)
    togvarsel_outcomes.refresh(now=datetime(2026, 9, 1, 6, 5, tzinfo=_OSLO))
    assert [m for m in fake.merges if m["d"] == thursday] == []
    # Monday, which did resolve, is written in full — the skip is per day, not a blanket bail-out.
    assert len([m for m in fake.merges if m["kind"] == "settled" and m["d"] == monday]) == len(
        togvarsel_outcomes.SCOPES
    )


def test_the_work_set_reaches_back_for_row_less_days_and_only_those():
    """Beyond the retry window the pass back-fills the pre-feature backlog — days with NO outcome
    row — out to `_BACKFILL_DAYS`, and nothing else: a day with rows there was settled or marked
    unresolvable, and re-reading either re-runs an immutable query to reach the same answer. The
    horizon exists so /rushhistorikk can be initialised from the forecast log rather than reading
    `ukjent` for every day older than ten days."""
    sql = togvarsel_outcomes._WORK_SET_SQL
    assert "INTERVAL @backfill_days DAY" in sql and "r.d IS NULL" in sql
    assert "INTERVAL @retry_days DAY" in sql  # the inner window is still the retry window
    assert togvarsel_outcomes._BACKFILL_DAYS > togvarsel_outcomes._RETRY_DAYS
    fake = _FakeBQ(work_set=[])
    _reset(fake)
    togvarsel_outcomes._work_set(MONDAY)
    params = next(p for sql, p in fake.sent if "togvarsel_issued" in sql)
    assert params["backfill_days"] == togvarsel_outcomes._BACKFILL_DAYS
    assert params["retry_days"] == togvarsel_outcomes._RETRY_DAYS


def test_a_wide_backlog_is_read_in_bounded_spans_and_every_day_is_written():
    """At 32.5 MB per day the whole backfill horizon in one SELECT sits at `_MAX_BYTES_BILLED`, so
    the first pass in a fresh environment would fail every retry and settle nothing. Spans are cut
    by CALENDAR days — the work set holds weekdays only, so a count would let a chunk stretch over
    more partitions than it has days — each SELECT bounded by its own chunk, and the MERGE loop
    still walks the whole work set."""
    days = [MONDAY - timedelta(days=40), MONDAY - timedelta(days=20), MONDAY - timedelta(days=1)]
    rows = []
    for d in days:
        rows += [_row("am", 0.12, d=d), _row("amrest", 0.11, d=d), _row("pm", 0.05, d=d)]
    fake = _FakeBQ(settled=rows, work_set=days)
    _reset(fake)
    togvarsel_outcomes.refresh(now=datetime(2026, 9, 1, 6, 5, tzinfo=_OSLO))
    selects = [p for sql, p in fake.sent if sql == togvarsel_outcomes._SETTLED_QUERY]
    assert len(selects) == 3, f"expected one SELECT per span, got {len(selects)}"
    for p in selects:
        assert (p["to_date"] - p["from_date"]).days < togvarsel_outcomes._SETTLED_CHUNK_DAYS
    settled = [m for m in fake.merges if m["kind"] == "settled"]
    assert {m["d"] for m in settled} == set(days)
    assert len(settled) == len(days) * len(togvarsel_outcomes.SCOPES)
    # and the bound is not fiction: a chunk plus the query's own ±1-day reach, at the measured
    # 32.5 MB a day, stays under half the job's bytes cap
    reach = togvarsel_outcomes._SETTLED_CHUNK_DAYS + 2
    assert reach * 32.5e6 < togvarsel_outcomes._MAX_BYTES_BILLED / 2


def test_the_two_provisional_windows_have_separate_give_up_budgets():
    """A morning outage must not forfeit the afternoon check.

    One shared "provisional" backoff key meant eight failed morning attempts retired the pass for
    the whole Oslo day, so an environment that was healthy by 18:20 still recorded nothing for the
    afternoon rush — and the page reads that as `venter`, then `ukjent`, with no error anywhere.
    """
    fake = _FakeBQ(fail_on="rail_calls_planned_today")
    _reset(fake)
    for minute in range(0, 8 * 20, 20):  # burn the morning budget past _MAX_FAILS_PER_DAY
        togvarsel_outcomes.refresh(now=_oslo(9, 25) + timedelta(minutes=minute))
    morning_attempts = len(fake.queries)
    assert morning_attempts >= togvarsel_outcomes._MAX_FAILS_PER_DAY
    togvarsel_outcomes.refresh(now=_oslo(18, 25))  # the afternoon window, same day
    assert len(fake.queries) > morning_attempts, "the afternoon window inherited the morning's budget"


def test_a_startup_ddl_failure_is_retried_on_the_next_write():
    """ensure_table's own log promises "retrying on write", and until it did that was untrue: one
    BigQuery blip at startup left the table absent until somebody redeployed. Its sibling
    togvarsel_notify._record_round has always carried this latch."""
    fake = _FakeBQ(provisional=[_row("am", 0.12)], fail_on="CREATE TABLE")
    _reset(fake)
    togvarsel_outcomes.ensure_table()  # swallowed
    assert togvarsel_outcomes._table_ready is False
    fake.fail_on = None
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    assert togvarsel_outcomes._table_ready is True
    assert fake.merges, "the pass ran but wrote nothing"


def test_an_unanswered_provisional_scope_stays_due():
    """Marking a scope done on an empty read retires it for the rest of the Oslo day, and the page
    then shows `venter` until the overnight pass with nothing retrying."""
    fake = _FakeBQ(provisional=[])
    _reset(fake)
    togvarsel_outcomes.refresh(now=_oslo(9, 25))
    assert togvarsel_outcomes._state["provisional_done"] == set()
    fake.provisional = [_row("am", 0.12), _row("amrest", 0.11)]
    togvarsel_outcomes.refresh(now=_oslo(9, 45))
    assert {m["scope"] for m in fake.merges} == {"am", "amrest"}
    assert togvarsel_outcomes._state["provisional_done"] == {"am", "amrest"}


def test_the_backoff_constants_stay_in_step_with_togvarsels():
    """A source-text parity check, not an import.

    This module's backoff is a deliberate copy of togvarsel's (same cooldown, same give-up count,
    same datetime.max sentinel), and it has to be a copy: those functions close over
    togvarsel._state["backoff"], so calling them from here would file this module's pass names into
    togvarsel's per-issue failure state and cross-contaminate two domains the poller's per-step
    isolation exists to keep apart. Sharing them properly means parameterising togvarsel's pair on
    the backoff dict, which is a change to a module this feature does not otherwise touch.

    So: pin the values instead, the way SCOPE_START_H is pinned against NOTIFY_SPECS. Grepped
    rather than imported to keep this suite hermetic."""
    source = (Path(__file__).parent / "togvarsel.py").read_text()
    assert "_FAIL_COOLDOWN = timedelta(minutes=15)" in source, (
        "togvarsel's cooldown moved; togvarsel_outcomes._FAIL_COOLDOWN must move with it"
    )
    assert "_MAX_FAILS_PER_DAY = 8" in source, (
        "togvarsel's give-up count moved; togvarsel_outcomes._MAX_FAILS_PER_DAY must move with it"
    )
    assert togvarsel_outcomes._FAIL_COOLDOWN == timedelta(minutes=15)
    assert togvarsel_outcomes._MAX_FAILS_PER_DAY == 8


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
