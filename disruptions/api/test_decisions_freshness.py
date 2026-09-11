"""Checks for the labelling-freshness detector (U4): `decisions.check_labelling_freshness()` and
its isolated block in `main.poll_for_changes`.

The failure this detector exists for is silent twice over: `varsel-label-daily` is a scheduled
query that runs OUTSIDE this process, so nothing in the app ever calls it and nothing notices it
dying — and its retry window means every decision still unlabelled after 10 days is lost forever.
What these tests pin is R8's work-to-label gate, which is what keeps the detector honest in both
directions: labelling stale or absent WHILE recent decisions exist is an ERROR (the prd-outage
case — and it must fire even when the outage has outlasted the 10-day window and emptied the MAX
to NULL), while an empty decision log (tst, a quiet period) is one latched INFO and never an
ERROR, so the alert channel cannot be trained into being ignored.

Hermetic: the BigQuery client is stubbed before main is imported and the lifespan is never
entered. Every test resets the module's freshness state (rate-limit stamp, quiet latch) and
restores it after — a leaked half-spent window reads exactly like the give-up bug under test
(the documented `_message_attempts` trap, one module over).

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery \
    --with google-cloud-storage --with pywebpush --with google-cloud-firestore \
    --with prometheus-client python test_decisions_freshness.py  (or under pytest)
"""

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "ent-kixavvik-test")

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import decisions
import main
from prometheus_client import REGISTRY

NOW = datetime.now(tz=UTC)


class _Row:
    """The one row the two-aggregate freshness query returns, attribute-access like bigquery.Row."""

    def __init__(self, latest_labelled, labellable_decisions):
        self.latest_labelled = latest_labelled
        self.labellable_decisions = labellable_decisions


class _Job:
    def __init__(self, row):
        self._row = row

    def result(self, timeout=None):
        return [self._row]


class _FreshnessClient:
    """Serves the freshness row; counts calls so the 6 h rate window is assertable."""

    def __init__(self, latest_labelled, labellable_decisions):
        self.calls = 0
        self.sqls: list[str] = []
        self._row = _Row(latest_labelled, labellable_decisions)

    def query(self, sql):
        self.calls += 1
        self.sqls.append(sql)
        return _Job(self._row)


class _RaisingClient:
    def __init__(self):
        self.calls = 0

    def query(self, sql):
        self.calls += 1
        raise RuntimeError("BigQuery is having a day")


class _Recorder:
    """Captures (level, fields) for every log call decisions makes."""

    def __init__(self):
        self.records: list[tuple[str, dict]] = []

    def error(self, *args, **kwargs):
        self.records.append(("ERROR", (kwargs.get("extra") or {}).get("fields", {})))

    def warning(self, *args, **kwargs):
        self.records.append(("WARNING", (kwargs.get("extra") or {}).get("fields", {})))

    def info(self, *args, **kwargs):
        self.records.append(("INFO", (kwargs.get("extra") or {}).get("fields", {})))

    def __getattr__(self, _name):  # exception/debug/etc. are not under test here
        return lambda *a, **k: None

    def by_level(self, level: str) -> list[dict]:
        return [fields for lvl, fields in self.records if lvl == level]


class _SilentLogger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


@contextmanager
def _freshness(client):
    """Install `client` with the freshness state RESET; restore everything after.

    The reset is the point, not hygiene: the rate-limit stamp and the quiet-notice latch are
    module globals, and a value leaked from an earlier test would make "no query ran" or "no INFO
    logged" pass for the wrong reason.
    """
    recorder = _Recorder()
    before = (
        decisions._bq_client,
        decisions._freshness_last_checked,
        decisions._freshness_quiet_logged,
        decisions.logger,
    )
    decisions._bq_client = client
    decisions._freshness_last_checked = None
    decisions._freshness_quiet_logged = False
    decisions.logger = recorder
    try:
        yield recorder
    finally:
        (
            decisions._bq_client,
            decisions._freshness_last_checked,
            decisions._freshness_quiet_logged,
            decisions.logger,
        ) = before


class _Ticks:
    """Duck-types the stop_event: poll_for_changes runs exactly `n` loop iterations."""

    def __init__(self, n: int) -> None:
        self.left = n

    def wait(self, interval: float) -> bool:
        self.left -= 1
        return self.left <= 0


# ---- the work-to-label gate ---------------------------------------------------------------------
def test_a_stale_labelling_with_work_waiting_is_one_error_and_the_window_holds():
    """A 3-day-old labelled_at beside recent decisions is the alert case, and the ERROR carries
    the age as a number an alert rule can threshold on. The second call inside the 6 h window
    must run NO query — the detector rides a 60 s poller tick and cannot cost a BigQuery job per
    tick."""
    assert decisions._FRESHNESS_CHECK_INTERVAL_S == 6 * 3600
    client = _FreshnessClient(NOW - timedelta(hours=72), labellable_decisions=15)
    with _freshness(client) as recorder:
        decisions.check_labelling_freshness()
        errors = recorder.by_level("ERROR")
        assert len(errors) == 1, recorder.records
        assert errors[0]["event"] == "labelling_stale"
        assert isinstance(errors[0]["hours_stale"], float)
        assert 70 <= errors[0]["hours_stale"] <= 74, errors[0]
        assert "reason" not in errors[0], "a measured age and 'no rows at all' are different facts"
        decisions.check_labelling_freshness()
        assert client.calls == 1, "a call inside the 6 h window must not query"
        assert len(recorder.by_level("ERROR")) == 1


def test_absent_labelling_with_work_waiting_alerts_with_a_named_reason():
    """The prd-outage-past-the-window case. An outage that outlasts the 10-day retry window
    empties the partition-bounded MAX to NULL — precisely when loss becomes permanent — so NULL
    beside recent decisions must alert, never read as "nothing to report"."""
    client = _FreshnessClient(None, labellable_decisions=8)
    with _freshness(client) as recorder:
        decisions.check_labelling_freshness()
        errors = recorder.by_level("ERROR")
        assert len(errors) == 1, recorder.records
        assert errors[0]["event"] == "labelling_stale"
        assert errors[0]["reason"] == "no_labelled_rows"
        assert "hours_stale" not in errors[0], "there is no timestamp to compute an age from"


def test_no_work_to_label_is_one_latched_info_never_error():
    """tst decides nothing, so tst labels nothing — stale-or-NULL labelling there is the system
    working, not failing. One INFO per process says the detector looked; repeating it per check
    (or raising it to ERROR) would train whoever reads the channel to ignore labelling_stale."""
    client = _FreshnessClient(None, labellable_decisions=0)
    with _freshness(client) as recorder:
        decisions.check_labelling_freshness()
        assert recorder.by_level("ERROR") == []
        assert len(recorder.by_level("INFO")) == 1, recorder.records
        # Re-open the rate window: the query runs again, the notice stays latched.
        decisions._freshness_last_checked = None
        decisions.check_labelling_freshness()
        assert client.calls == 2
        assert len(recorder.by_level("INFO")) == 1, "the notice is per process, not per check"
        assert recorder.by_level("ERROR") == []
        # A stale timestamp with no recent decisions is the same quiet verdict.
        decisions._freshness_last_checked = None
        decisions._bq_client = _FreshnessClient(NOW - timedelta(days=9), labellable_decisions=0)
        decisions.check_labelling_freshness()
        assert recorder.by_level("ERROR") == []
        assert len(recorder.by_level("INFO")) == 1


def test_fresh_labelling_sets_the_gauge_and_logs_nothing():
    """The healthy path is a gauge move and silence: `time() - value` is the PromQL age rule the
    runbook alerts on, so the gauge must be the labelled_at EPOCH, not "now"."""
    latest = NOW - timedelta(hours=2)
    client = _FreshnessClient(latest, labellable_decisions=40)
    with _freshness(client) as recorder:
        decisions.check_labelling_freshness()
        assert recorder.records == [], recorder.records
    value = REGISTRY.get_sample_value("kix_labelling_last_success_timestamp_seconds")
    assert value == latest.timestamp(), "the gauge is the labelled_at epoch, never time.time()"


def test_a_fresh_environment_with_only_unlabellable_decisions_stays_quiet():
    """The regression that made every new deployment cry wolf on day one.

    A brand-new environment (a rebuilt dev table, tst the moment a journey registers, prd's first
    day) has decisions but nothing labelled yet — and nothing the nightly job COULD have labelled,
    because it joins last_recorded and that stops at yesterday. Gated on "decisions in the last
    48 h" this fired `reason="no_labelled_rows"` — "varsel-label-daily appears to have stopped" —
    from the first decision written, every 6 h for over a day, tripping the runbook's ERROR rule
    each time. The gate counts only decisions old enough to have been labelled, so this state is
    the quiet one it always should have been.
    """
    client = _FreshnessClient(None, labellable_decisions=0)  # decisions exist, none yet labellable
    with _freshness(client) as recorder:
        decisions.check_labelling_freshness()
        assert recorder.by_level("ERROR") == [], (
            "a deployment whose decisions are all younger than the labelling job's reach must "
            "not be reported as a dead labelling job"
        )


def test_the_gauge_is_set_even_when_labelling_is_stale():
    """R8: the gauge must exist whenever a labelled row does, or the age rule cannot age it.

    Set only on the healthy path, `kix_labelling_last_success_timestamp_seconds` disappeared
    exactly when labelling stopped — so the runbook's `time() - value` rule saw no-data precisely
    in the failure it exists to catch, and had to alert on no-data to compensate, which then
    paged on every quiet environment instead.
    """
    stale = NOW - timedelta(hours=72)
    client = _FreshnessClient(stale, labellable_decisions=15)
    with _freshness(client) as recorder:
        decisions.check_labelling_freshness()
        assert len(recorder.by_level("ERROR")) == 1, "stale with work waiting is still the alert"
    value = REGISTRY.get_sample_value("kix_labelling_last_success_timestamp_seconds")
    assert value == stale.timestamp(), (
        "the gauge reports the last labelling that DID happen, however long ago — its age is the "
        "alert, and an absent series has no age"
    )


# ---- failure posture ----------------------------------------------------------------------------
def test_a_raising_client_warns_and_spends_the_window():
    """A query failure is a WARNING with a bounded field, never a raise (decisions' fail-open
    posture: the detector must never take the poller down) — and it spends the 6 h window, so a
    broken BigQuery is not re-asked on every 60 s tick."""
    client = _RaisingClient()
    with _freshness(client) as recorder:
        decisions.check_labelling_freshness()
        warnings = recorder.by_level("WARNING")
        assert len(warnings) == 1, recorder.records
        assert warnings[0]["event"] == "labelling_stale"
        assert warnings[0]["result"] == "check_failed"
        assert recorder.by_level("ERROR") == []
        decisions.check_labelling_freshness()
        assert client.calls == 1, "a failed check backs off for the whole window"


def test_the_poller_tick_continues_past_a_failing_freshness_check():
    """One real poll_for_changes iteration with a raising BigQuery client behind the real
    check: the loop must complete its tick (returning at all proves it got past the block — the
    stop_event is only consulted after it) with the heartbeat written."""
    client = _RaisingClient()
    before = (
        main.get_last_ingestion_time,
        main.togvarsel.refresh,
        main.togvarsel_notify.evaluate,
        main.togvarsel_outcomes.refresh,
        main.logger,
        main._poller_heartbeat_monotonic,
    )
    main.get_last_ingestion_time = lambda: None
    main.togvarsel.refresh = lambda: None
    main.togvarsel_notify.evaluate = lambda: None
    main.togvarsel_outcomes.refresh = lambda: None
    main.logger = _SilentLogger()
    main._poller_heartbeat_monotonic = None
    try:
        with _freshness(client) as recorder:
            main.poll_for_changes(lambda ts: None, _Ticks(1))
            assert client.calls == 1, "the tick reached the freshness check"
            assert len(recorder.by_level("WARNING")) == 1
        assert main._poller_heartbeat_monotonic is not None
    finally:
        (
            main.get_last_ingestion_time,
            main.togvarsel.refresh,
            main.togvarsel_notify.evaluate,
            main.togvarsel_outcomes.refresh,
            main.logger,
            main._poller_heartbeat_monotonic,
        ) = before


def test_a_detector_bug_is_isolated_by_its_own_block():
    """check_labelling_freshness never raises on its own, so main's try/except is belt-and-braces
    — driven anyway, because a defect in the detector must cost the check and nothing else."""

    def _boom():
        raise RuntimeError("detector bug")

    before = (
        main.get_last_ingestion_time,
        main.togvarsel.refresh,
        main.togvarsel_notify.evaluate,
        main.togvarsel_outcomes.refresh,
        decisions.check_labelling_freshness,
        main.logger,
        main._poller_heartbeat_monotonic,
    )
    main.get_last_ingestion_time = lambda: None
    main.togvarsel.refresh = lambda: None
    main.togvarsel_notify.evaluate = lambda: None
    main.togvarsel_outcomes.refresh = lambda: None
    decisions.check_labelling_freshness = _boom
    main.logger = _SilentLogger()
    main._poller_heartbeat_monotonic = None
    try:
        main.poll_for_changes(lambda ts: None, _Ticks(1))  # must not raise
        assert main._poller_heartbeat_monotonic is not None
    finally:
        (
            main.get_last_ingestion_time,
            main.togvarsel.refresh,
            main.togvarsel_notify.evaluate,
            main.togvarsel_outcomes.refresh,
            decisions.check_labelling_freshness,
            main.logger,
            main._poller_heartbeat_monotonic,
        ) = before


def test_an_unconfigured_module_is_a_no_op():
    """No client means no query and no records — and the window is NOT spent, because
    configure() can land a moment later and the first real check should run immediately."""
    with _freshness(None) as recorder:
        decisions.check_labelling_freshness()
        assert recorder.records == []
        assert decisions._freshness_last_checked is None


# ---- the query text (nothing in this repo executes SQL) -----------------------------------------
def test_the_query_is_partition_bounded_on_both_tables():
    """Source-level assertion, the repo's cheap guard for unexecutable SQL (no test or CI job has
    GCP credentials). varsel_decision_outcomes is PARTITION BY call_date and labelled_at is NOT
    the partition key, so an unbounded MAX(labelled_at) is a full-table scan every 6 h forever;
    varsel_decisions is PARTITION BY DATE(decided_at), so the 48 h cut needs a DATE() predicate
    beside it to prune partitions at all."""
    sql = decisions._FRESHNESS_SQL
    assert "call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY)" in sql
    # The work gate: decisions the nightly job HAS had the chance to label. The upper bound
    # (today-2) is the whole point — the job joins last_recorded, which stops at yesterday, so a
    # decision about today's departure is not late, it is not yet labellable. Counting it made
    # every fresh environment fire a false labelling_stale ERROR from its first decision.
    assert "call_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY)" in sql
    assert "AND DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)" in sql
    # varsel_decisions is PARTITION BY DATE(decided_at), and call_date is not that key, so the
    # gate above prunes nothing on its own: a decision precedes its call by at most ~1 day
    # (measured), which is what makes today-11 a safe cover for a call_date floor of today-10.
    assert "DATE(decided_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 11 DAY)" in sql
    assert "TIMESTAMP_SUB" not in sql, (
        "the 48 h decision window was the false-positive source; it must not come back"
    )


def test_the_check_rides_the_wall_clock_tick_never_the_sx_rebuild():
    """KTD7: NOT a _rebuild_all phase (test_poller_refresh pins that phase list, and the check
    must run on quiet nights when no SX moves and no rebuild happens), and after the
    togvarsel_outcomes block, mirroring its three wall-clock neighbours."""
    import inspect

    assert "check_labelling_freshness" not in inspect.getsource(main._rebuild_all)
    assert "check_labelling_freshness" not in inspect.getsource(main._on_ingestion_change)
    poll = inspect.getsource(main.poll_for_changes)
    assert poll.index("togvarsel_outcomes.refresh()") < poll.index(
        "decisions.check_labelling_freshness()"
    )


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} labelling-freshness checks passed.")


if __name__ == "__main__":
    _run()
