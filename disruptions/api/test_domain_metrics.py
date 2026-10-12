"""Checks for the domain metric instrumentation (U3): poller gauges and histograms, the push
counter, the LLM-wording counter and the decision-write counter.

Hermetic: the BigQuery client is stubbed before main is imported, the app's lifespan is never
entered, no exporter port is bound and no network is touched — `pywebpush.webpush` is patched on
the real module (varsel._send_push imports it at call time), and every backend behind
llm_message is a lambda. Registry rules are test_metrics.py's: assert counter/gauge DELTAS
(the default REGISTRY is process-global and other suites move it), and never
`importlib.reload(metrics)` (re-registering a name in the default REGISTRY raises).

What these pin, beyond "+1 happened":

- The TWO-CLOCK rule (KTD1): the tick and cycle gauges are EPOCH seconds (`> 1.7e9`), never the
  monotonic heartbeat value — a monotonic export looks healthy on a dashboard while it silently
  invalidates every `time() - value` age rule in the runbook.
- Exactly-once increments at the choke points: `varsel._send_push` on every return path
  (unexpected-exception path included), and `llm_message.write_message_detailed` at its return
  boundary — never at the internal log branches, which miss `truncated`/`unavailable` entirely
  and fire once per retried backend.
- The `surface` label is a CLOSED vocabulary (live/replay/webhook/pull/other): an arbitrary
  `caller` string must never mint a label value on a public-port-scrapable counter.

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery \
      --with google-cloud-storage --with pywebpush --with google-cloud-firestore \
      --with prometheus-client python test_domain_metrics.py  (or under pytest)
"""

import time
from contextlib import contextmanager
from types import SimpleNamespace

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import decisions
import llm_message
import main
import metrics
import varsel
from prometheus_client import REGISTRY


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Current value of one sample, 0.0 when the labelset has never been touched."""
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


class _SilentLogger:
    """Swallows every logging call — the log lines beside the increments are pinned by the
    existing suites (and by `git diff` staying additive around them), not re-asserted here."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


@contextmanager
def _quiet(module):
    """Silence one module's logger for the body; restore it after."""
    before = module.logger
    module.logger = _SilentLogger()
    try:
        yield
    finally:
        module.logger = before


# ---- kix_push_total: one increment per _send_push call, on every return path -------------------


def _push(webpush_stub) -> str:
    """Call the REAL varsel._send_push with pywebpush.webpush patched to `webpush_stub`."""
    import pywebpush

    before = pywebpush.webpush
    pywebpush.webpush = webpush_stub
    try:
        with _quiet(varsel):
            # U5 made the return (result, error_detail); only the result matters to the counter.
            result, _error_detail = varsel._send_push(
                {"endpoint": "https://push.example.com/sub/1"}, {"title": "t"}
            )
            return result
    finally:
        pywebpush.webpush = before


def _push_totals() -> dict[str, float]:
    return {r: _sample("kix_push_total", {"result": r}) for r in ("sent", "gone", "error")}


def _raiser(exc):
    def _raise(**_kwargs):
        raise exc

    return _raise


def test_push_counter_counts_sent_gone_and_error_exactly_once_per_call():
    """Every outcome is one increment on its own label and zero on the others — including the
    unexpected-exception path, which is the one a per-return-statement increment forgets."""
    import pywebpush

    before = _push_totals()
    assert _push(lambda **kwargs: None) == "sent"
    after = _push_totals()
    assert after == {**before, "sent": before["sent"] + 1}

    for status in (404, 410):
        before = _push_totals()
        exc = pywebpush.WebPushException(
            "gone", response=SimpleNamespace(status_code=status, text="Gone")
        )
        assert _push(_raiser(exc)) == "gone"
        after = _push_totals()
        assert after == {**before, "gone": before["gone"] + 1}, status

    before = _push_totals()
    exc = pywebpush.WebPushException(
        "server error", response=SimpleNamespace(status_code=500, text="boom")
    )
    assert _push(_raiser(exc)) == "error"
    after = _push_totals()
    assert after == {**before, "error": before["error"] + 1}

    # The unexpected-exception path: not a WebPushException at all.
    before = _push_totals()
    assert _push(_raiser(RuntimeError("socket melted"))) == "error"
    after = _push_totals()
    assert after == {**before, "error": before["error"] + 1}


# ---- kix_decision_write_total: ok / partial / error, and nothing when no write was attempted ----


class _InsertClient:
    """Stands in for decisions._bq_client on the write path only (the DDL half is skipped by
    setting _table_ready — test_decisions.py owns that half)."""

    def __init__(self, errors=None, raise_exc=None):
        self._errors = errors or []
        self._raise = raise_exc

    def insert_rows_json(self, table, rows, timeout=None, retry=None):
        if self._raise is not None:
            raise self._raise
        return self._errors


@contextmanager
def _decisions_client(client):
    before = (decisions._bq_client, decisions._table_ready, decisions._ensure_failed_at)
    decisions._bq_client = client
    decisions._table_ready = client is not None
    decisions._ensure_failed_at = None
    try:
        with _quiet(decisions):
            yield
    finally:
        decisions._bq_client, decisions._table_ready, decisions._ensure_failed_at = before


def _decision_totals() -> dict[str, float]:
    return {
        r: _sample("kix_decision_write_total", {"result": r}) for r in ("ok", "partial", "error")
    }


def test_decision_write_counter_counts_ok_partial_and_error():
    rows = [{"situation_number": "s1"}, {"situation_number": "s2"}]

    before = _decision_totals()
    with _decisions_client(_InsertClient(errors=[])):
        assert decisions.record(rows) == 2
    assert _decision_totals() == {**before, "ok": before["ok"] + 1}

    before = _decision_totals()
    with _decisions_client(_InsertClient(errors=[{"index": 0, "errors": ["bad column"]}])):
        assert decisions.record(rows) == 1
    assert _decision_totals() == {**before, "partial": before["partial"] + 1}

    before = _decision_totals()
    with _decisions_client(_InsertClient(raise_exc=RuntimeError("bq down"))):
        assert decisions.record(rows) == 0
    assert _decision_totals() == {**before, "error": before["error"] + 1}


def test_decision_write_counter_is_silent_when_no_write_was_attempted():
    """`record([])` and an unconfigured module attempted no write, so they must count nothing —
    an `error` there would page on every hermetic deployment that simply has no client."""
    before = _decision_totals()
    with _decisions_client(_InsertClient(errors=[])):
        assert decisions.record([]) == 0
    with _decisions_client(None):
        assert decisions.record([{"situation_number": "s1"}]) == 0
    assert _decision_totals() == before


def test_a_failed_table_ensure_counts_an_error_rather_than_flatlining():
    """A configured client whose DDL failed is silent data loss, and must not look like silence.

    `ensure_table()` returns False for two very different states. No client is a deployment that
    never meant to record — counted, it would page forever on every hermetic environment. But a
    CONFIGURED client whose structural CREATE failed is the 2026-08-07 mode: BigQuery's 5-per-10 s
    table-update limit turns a cosmetic description edit into a decision log that records nothing,
    behind one WARNING per retry window — below the runbook's ERROR rule. Uncounted, the counter
    simply flatlines, which on a dashboard is indistinguishable from "no alerts happened" — the
    exact silent loss this counter was added to catch.
    """

    class _EnsureFailsClient:
        """Configured, and its structural DDL raises — `ensure_table` returns False."""

        def query(self, sql, **kwargs):
            raise RuntimeError("Exceeded rate limits: too many table update operations")

    before = _decision_totals()
    with _decisions_client(_EnsureFailsClient()):
        decisions._table_ready = False
        decisions._ensure_failed_at = None
        assert decisions.record([{"situation_number": "s1"}]) == 0
    after = _decision_totals()
    assert after["error"] == before["error"] + 1, (
        "a configured client that cannot ensure its table is a failed write, not a no-op"
    )


# ---- The poller cycle: epoch gauge + duration histogram, both from `finally` -------------------


def test_a_rebuild_cycle_sets_the_epoch_gauge_and_observes_the_duration():
    """The gauge must be EPOCH-plausible (> 1.7e9) — asserting a small monotonic-scale value here
    is exactly the two-clock confusion KTD1 exists to prevent."""
    before_count = _sample("kix_poller_cycle_duration_seconds_count")
    before_main = (main._rebuild_all, main.logger)
    main._rebuild_all = lambda timings: None
    main.logger = _SilentLogger()
    try:
        main._on_ingestion_change(None)
    finally:
        main._rebuild_all, main.logger = before_main
    assert _sample("kix_poller_cycle_duration_seconds_count") == before_count + 1
    gauge = _sample("kix_poller_last_cycle_timestamp_seconds")
    assert gauge > 1.7e9, "the cycle gauge must be epoch seconds, never a monotonic reading"
    assert abs(time.time() - gauge) < 60


def test_a_dying_rebuild_observes_its_duration_but_does_not_advance_the_freshness_gauge():
    """The two cycle metrics answer different questions, so they sit on different sides of the try.

    The DURATION stays in `finally`, same contract as the poller_cycle log line: a rebuild that
    died partway through is when that reading matters most. The freshness GAUGE must not move —
    it was in the `finally` too, which made the runbook's staleness rule unfirable by
    construction: with `predict_sql` raising every cycle (one of the four steps deliberately not
    wrapped in its own try/except, so it propagates), every cache goes hours stale while the
    gauge keeps advancing and the rule stays green forever.
    """

    def exploding_rebuild(timings):
        raise RuntimeError("rebuild died")

    before_count = _sample("kix_poller_cycle_duration_seconds_count")
    before_main = (main._rebuild_all, main.logger)
    # A known, distinctly old value, so "did not move" is assertable rather than inferred from a
    # gauge some earlier test happened to set.
    metrics.POLLER_LAST_CYCLE.set(1_700_000_000.0)
    main._rebuild_all = exploding_rebuild
    main.logger = _SilentLogger()
    try:
        try:
            main._on_ingestion_change(None)
        except RuntimeError:
            pass  # poll_for_changes is the one that catches it in production
        else:
            raise AssertionError("the rebuild failure must still propagate")
    finally:
        main._rebuild_all, main.logger = before_main
    assert _sample("kix_poller_cycle_duration_seconds_count") == before_count + 1, (
        "a failed cycle's duration is the reading worth having"
    )
    assert _sample("kix_poller_last_cycle_timestamp_seconds") == 1_700_000_000.0, (
        "a cycle that died must not report the caches as freshly rebuilt"
    )


# ---- The per-phase histogram and outcome counter ------------------------------------------------


def test_a_phase_observes_its_histogram_and_counts_ok():
    phase = {"phase": "metrics_test_step"}
    before_hist = _sample("kix_poller_phase_duration_seconds_count", phase)
    before_ok = _sample("kix_poller_phase_total", {**phase, "result": "ok"})
    timings: dict[str, int] = {}
    with _quiet(main), main._phase("metrics_test_step", timings):
        pass
    assert _sample("kix_poller_phase_duration_seconds_count", phase) == before_hist + 1
    assert _sample("kix_poller_phase_total", {**phase, "result": "ok"}) == before_ok + 1


def test_a_phase_observes_interpreter_cpu_and_never_a_negative_value():
    """KTD17's budget is INTERPRETER time, so wall clock alone cannot answer it.

    The phase timings say a step took 9 s; they cannot say whether those 9 s were one CPython
    thread executing bytecode (which a second core does not help and a wider pool does not help
    either) or a socket wait (which both do). `kix_poller_phase_cpu_seconds{phase}` is the series
    that separates them, and it is the one number every later unit's capacity claim is checked
    against.

    "Not negative" is the one arithmetic property worth pinning: `process_time()` is monotonic per
    process, but a delta computed against the wrong baseline — a per-call start captured after the
    body, a counter reset — reads as a negative observation, which prometheus_client accepts into
    the sum and which no dashboard makes obvious.
    """
    phase = {"phase": "metrics_cpu_step"}
    before = _sample("kix_poller_phase_cpu_seconds_count", phase)
    before_sum = _sample("kix_poller_phase_cpu_seconds_sum", phase)
    timings: dict[str, int] = {}
    with _quiet(main), main._phase("metrics_cpu_step", timings):
        sum(i * i for i in range(200_000))  # real bytecode, so the delta cannot be exactly zero
    assert _sample("kix_poller_phase_cpu_seconds_count", phase) == before + 1
    observed = _sample("kix_poller_phase_cpu_seconds_sum", phase) - before_sum
    assert observed >= 0.0, f"a phase observed {observed} CPU seconds"


def test_the_phase_record_carries_cpu_and_rss_beside_the_wall_clock():
    """The harness reads phases out of Cloud Logging, not out of Prometheus.

    `kix_*` lives in Thanos behind Grafana and is not in Google Managed Prometheus (checked
    2026-09-11), so `bench_cycle.py` cannot scrape the histogram above — it reads the
    `poller_phase` records. The three figures therefore have to be ON the record: `ms` was there,
    `cpu_ms` and `rss_delta_bytes` are what R16 adds.

    `rss_delta_bytes` is allowed to be absent (it is read from /proc, which exists in the
    container and not on a developer's macOS), but never present-and-wrong: an absent reading must
    omit the field rather than report 0, which would be indistinguishable from a phase that
    allocated nothing.
    """
    records: list[dict] = []

    class _Capture(_SilentLogger):
        def info(self, *a, **kw):
            records.append(dict((kw.get("extra") or {}).get("fields") or {}))

    before = main.logger
    main.logger = _Capture()
    timings: dict[str, int] = {}
    try:
        with main._phase("metrics_fields_step", timings):
            sum(i * i for i in range(200_000))
    finally:
        main.logger = before
    record = next(r for r in records if r.get("event") == "poller_phase")
    assert record["phase"] == "metrics_fields_step"
    assert isinstance(record["ms"], int)
    assert isinstance(record["cpu_ms"], int) and record["cpu_ms"] >= 0
    if "rss_delta_bytes" in record:
        assert isinstance(record["rss_delta_bytes"], int)
    assert timings == {"metrics_fields_step": record["ms"]}, (
        "the cycle's `phases` breakdown must stay a name -> int map: poller_cycle publishes it "
        "verbatim and test_poller_refresh pins its shape"
    )


def test_the_cycle_histogram_reaches_past_the_medians_period():
    """A harness cycle longer than the ~295 s period must land in a real bucket.

    The buckets were tuned for a 10-180 s range against a ~177 s period. Under load the whole
    question is how far past the period a cycle goes, and every answer beyond 300 s used to be the
    same answer: `+Inf`. 600 and 900 give the overshoot two readings before the count stops
    meaning anything.
    """
    upper = [
        float(b) for b in metrics.POLLER_CYCLE_DURATION._upper_bounds if b != float("inf")
    ]
    assert 600 in upper and 900 in upper, upper
    before = _sample("kix_poller_cycle_duration_seconds_bucket", {"le": "600.0"})
    metrics.POLLER_CYCLE_DURATION.observe(500.0)
    assert _sample("kix_poller_cycle_duration_seconds_bucket", {"le": "600.0"}) == before + 1


def test_a_raising_phase_counts_error_and_still_observes_the_duration():
    phase = {"phase": "metrics_failing_step"}
    before_hist = _sample("kix_poller_phase_duration_seconds_count", phase)
    before_err = _sample("kix_poller_phase_total", {**phase, "result": "error"})
    timings: dict[str, int] = {}
    try:
        with _quiet(main), main._phase("metrics_failing_step", timings):
            raise RuntimeError("boom")
    except RuntimeError:
        pass  # _phase must re-raise — pinned by test_poller_refresh.py
    else:
        raise AssertionError("_phase must not swallow the step's exception")
    assert _sample("kix_poller_phase_duration_seconds_count", phase) == before_hist + 1
    assert _sample("kix_poller_phase_total", {**phase, "result": "error"}) == before_err + 1


# ---- kix_llm_message_total: once per write_message_detailed call, at the return boundary --------


@contextmanager
def _llm(provider="claude", backend=None):
    """Pin one backend with a stub reply — the same direct-assignment idiom as
    test_llm_message.py's _reset, restored wholesale afterwards."""
    before = (
        llm_message._PROVIDER,
        llm_message._ENABLED,
        llm_message._PROJECT,
        llm_message._STATE,
        dict(llm_message._BACKENDS),
    )
    llm_message._PROVIDER = provider
    llm_message._ENABLED = True
    llm_message._PROJECT = "ent-kixavvik-test"
    llm_message._STATE = llm_message._LatchState()
    if backend is not None:
        llm_message._BACKENDS["claude"] = backend
    try:
        with _quiet(llm_message):
            yield
    finally:
        (
            llm_message._PROVIDER,
            llm_message._ENABLED,
            llm_message._PROJECT,
            llm_message._STATE,
            backends,
        ) = before
        llm_message._BACKENDS.clear()
        llm_message._BACKENDS.update(backends)


_OK_REPLY = '{"melding": "Toget er innstilt."}'


def test_llm_counter_counts_one_representative_branch_per_class():
    """ok, error and rejected each land on their own labelset, exactly once per call — a counter
    wired to the log branches instead of the return boundary double-counts retries and misses the
    statuses that never log."""
    labels = {"provider": "claude", "result": "ok", "surface": "live"}
    before = _sample("kix_llm_message_total", labels)
    with _llm(backend=lambda body: _OK_REPLY):
        result = llm_message.write_message_detailed({"a": 1})
    assert result.status == "ok"
    assert _sample("kix_llm_message_total", labels) == before + 1

    labels = {"provider": "claude", "result": "error", "surface": "live"}
    before = _sample("kix_llm_message_total", labels)
    with _llm(backend=_raiser(RuntimeError("vertex 429"))):
        result = llm_message.write_message_detailed({"a": 1})
    assert result.status == "error"
    assert _sample("kix_llm_message_total", labels) == before + 1

    labels = {"provider": "claude", "result": "rejected", "surface": "live"}
    before = _sample("kix_llm_message_total", labels)
    with _llm(backend=lambda body: ""):
        result = llm_message.write_message_detailed({"a": 1})
    assert result.status == "rejected"
    assert _sample("kix_llm_message_total", labels) == before + 1


def test_llm_counter_tells_a_covered_error_apart():
    """An error the caller can cover with an earlier wording is `error_covered`, which the
    "LLM wording failures" rule does not select; the returned status stays `error`."""
    labels = {"provider": "claude", "result": "error_covered", "surface": "live"}
    before = _sample("kix_llm_message_total", labels)
    with _llm(backend=_raiser(RuntimeError("vertex 504"))):
        result = llm_message.write_message_detailed({"a": 1}, covered=True)
    assert result.status == "error"
    assert _sample("kix_llm_message_total", labels) == before + 1


def test_llm_counter_falls_back_to_a_bounded_provider_label():
    """`disabled` (and `unavailable`) carry no provider on the MessageResult; the label must be
    the bounded fallback "none", never an unlabelled series or a KeyError on the poller thread."""
    labels = {"provider": "none", "result": "disabled", "surface": "live"}
    before = _sample("kix_llm_message_total", labels)
    with _llm(provider="off"):
        result = llm_message.write_message_detailed({"a": 1})
    assert result.status == "disabled"
    assert _sample("kix_llm_message_total", labels) == before + 1


def test_llm_surface_label_is_a_closed_vocabulary():
    """replay=True → replay; a known caller → itself; caller=None → live (asserted throughout the
    branch test above); any OTHER caller string collapses to "other" rather than minting a
    series from an unbounded value."""
    labels = {"provider": "claude", "result": "ok", "surface": "replay"}
    before = _sample("kix_llm_message_total", labels)
    with _llm(backend=lambda body: _OK_REPLY):
        result = llm_message.write_message_detailed({"a": 1}, replay=True)
    assert result.status == "ok"
    assert _sample("kix_llm_message_total", labels) == before + 1

    labels = {"provider": "claude", "result": "ok", "surface": "webhook"}
    before = _sample("kix_llm_message_total", labels)
    with _llm(backend=lambda body: _OK_REPLY):
        llm_message.write_message_detailed({"a": 1}, caller="webhook")
    assert _sample("kix_llm_message_total", labels) == before + 1

    labels = {"provider": "claude", "result": "ok", "surface": "other"}
    before = _sample("kix_llm_message_total", labels)
    with _llm(backend=lambda body: _OK_REPLY):
        llm_message.write_message_detailed({"a": 1}, caller="some-novel-surface")
    assert _sample("kix_llm_message_total", labels) == before + 1
    # And the raw string never became a label value.
    assert _sample("kix_llm_message_total",
                   {"provider": "claude", "result": "ok", "surface": "some-novel-surface"}) == 0


# ---- The tick gauge: epoch beside the monotonic heartbeat (the two-clock rule, pinned) ----------


class _Ticks:
    """Duck-types the stop_event: poll_for_changes runs exactly `n` loop iterations."""

    def __init__(self, n: int) -> None:
        self.left = n

    def wait(self, interval: float) -> bool:
        self.left -= 1
        return self.left <= 0


def test_the_tick_gauge_is_epoch_while_the_heartbeat_stays_monotonic():
    """One loop iteration writes BOTH clocks: the monotonic heartbeat /health does age arithmetic
    on, and the epoch gauge PromQL does `time() - value` on. Conflating them breaks one silently."""
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
        main.poll_for_changes(lambda ts: None, _Ticks(1))
        gauge = _sample("kix_poller_last_tick_timestamp_seconds")
        assert gauge > 1.7e9, "the tick gauge must be epoch seconds, never the monotonic value"
        assert abs(time.time() - gauge) < 60
        heartbeat = main._poller_heartbeat_monotonic
        assert heartbeat is not None and abs(time.monotonic() - heartbeat) < 60
        assert heartbeat < 1.7e9, (
            "the /health heartbeat must STAY monotonic — exporting epoch into it would survive "
            "every test that only checks recency"
        )
    finally:
        (
            main.get_last_ingestion_time,
            main.togvarsel.refresh,
            main.togvarsel_notify.evaluate,
            main.togvarsel_outcomes.refresh,
            main.logger,
            main._poller_heartbeat_monotonic,
        ) = before


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} domain-metric checks passed.")


if __name__ == "__main__":
    _run()
