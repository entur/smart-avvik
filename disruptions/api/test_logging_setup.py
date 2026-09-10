"""Checks for the structured-logging contract (logging_setup.py) and the channels it must keep
trustworthy (U5): the JSON line format every Cloud Logging query depends on, the /health
access-log mute, the LOG_LEVEL env with its privacy interlock, the reisevalg chronic-warning
latch, and the symmetric push audit record (one `event="push"` line carrying the failure detail,
with no prose lines beside it).

Hermetic: no network, no BigQuery — reisevalg's client is a local stub and `pywebpush.webpush`
is patched on the real module (varsel._send_push imports it at call time), the
test_domain_metrics idiom. Tests that re-run `logging_setup.setup()` always restore it under
ambient env in `finally` — and re-attach the metrics counting handler the wipe removes (main's
own order: setup, then configure) — so later suites in the same process see INFO and keep
counting.

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery \
      --with google-cloud-storage --with pywebpush --with google-cloud-firestore \
      --with prometheus-client python test_logging_setup.py  (or under pytest)
"""

import contextlib
import inspect
import io
import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import logging_setup
import metrics
import reisevalg
import varsel

# ---- Helpers -----------------------------------------------------------------------------------


@contextmanager
def _env(**pairs):
    """Set (or clear, with None) env vars for the body, restoring ambient values afterwards —
    the test_demo_api._deployed_as idiom."""
    before = {k: os.environ.get(k) for k in pairs}
    for key, value in pairs.items():
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


def _restore_logging() -> None:
    """Re-run setup() under whatever env is ambient and re-attach the counting handler."""
    logging_setup.setup()
    metrics.configure()


@contextmanager
def _capture(logger_name: str):
    """Collect the records logged on one logger, without them reaching root's stdout handler."""
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    lg = logging.getLogger(logger_name)
    sink = _Sink()
    before = (lg.propagate, lg.level)
    lg.addHandler(sink)
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        lg.removeHandler(sink)
        lg.propagate, level = before
        lg.setLevel(level)


def _record(
    level: int = logging.INFO,
    msg: str = "hello",
    fields: dict | None = None,
    exc_info=None,
) -> logging.LogRecord:
    record = logging.LogRecord("test-logging", level, __file__, 0, msg, (), exc_info)
    if fields is not None:
        record.fields = fields
    return record


def _format(record: logging.LogRecord) -> str:
    return logging_setup.JsonFormatter().format(record)


def _access_record(path: str) -> logging.LogRecord:
    """A record shaped exactly like uvicorn's access line: five positional args, full path third
    (test_notification_pull's helper)."""
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "h11_impl.py", 0,
        '%s - "%s %s HTTP/%s" %d', ("10.0.0.1:1234", "GET", path, "1.1", 200), None,
    )


# ---- 1. The JSON line contract -----------------------------------------------------------------


def test_the_entry_carries_the_four_keys_and_an_iso8601_z_timestamp():
    entry = json.loads(_format(_record(level=logging.WARNING, msg="careful")))
    assert set(entry) == {"timestamp", "severity", "message", "logger"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", entry["timestamp"])
    assert entry["severity"] == "WARNING"
    assert entry["message"] == "careful"
    assert entry["logger"] == "test-logging"


def test_an_exception_renders_into_the_single_records_message():
    """One JSON object per record, traceback included — never one entry per traceback line,
    which is what GKE makes of a raw stderr traceback."""
    try:
        raise ValueError("boom")
    except ValueError:
        formatted = _format(_record(level=logging.ERROR, msg="it broke", exc_info=sys.exc_info()))
    assert "\n" not in formatted, "the formatted entry must be a single physical line"
    entry = json.loads(formatted)
    assert entry["message"].startswith("it broke\nTraceback")
    assert "ValueError: boom" in entry["message"]


def test_fields_merge_flat_and_reserved_keys_are_not_overridable():
    entry = json.loads(
        _format(_record(fields={"event": "x", "n": 3, "message": "spoof", "severity": "HACKED"}))
    )
    assert entry["event"] == "x" and entry["n"] == 3
    assert entry["message"] == "hello", "a fields dict must not override the entry's own message"
    assert entry["severity"] == "INFO"


def test_norwegian_text_survives_unescaped():
    formatted = _format(_record(msg="blåbær på Ås"))
    assert "blåbær på Ås" in formatted
    assert "\\u00e5" not in formatted


# ---- 2. The /health access-log mute (R9) --------------------------------------------------------


def test_health_probe_access_lines_are_dropped_and_only_the_exact_path():
    mute = next(
        f
        for f in logging.getLogger("uvicorn.access").filters
        if isinstance(f, logging_setup._HealthAccessMute)
    )
    assert mute.filter(_access_record("/health")) is False
    assert mute.filter(_access_record("/health?x=1")) is False
    assert mute.filter(_access_record("/healthz")) is True, "exact path only — never a prefix"
    assert mute.filter(_access_record("/health/sub")) is True
    assert mute.filter(_access_record("/varsel-api/journeys")) is True
    # Anything not shaped like a uvicorn access record passes untouched.
    plain = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, "started %s", ("/health",), None
    )
    assert mute.filter(plain) is True
    none_args = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "no args", None, None)
    assert mute.filter(none_args) is True


def test_the_mute_and_the_redaction_compose_on_the_access_logger():
    """A /health line is dropped; a record that passes the mute is still rewritten by the
    redaction filter beside it."""
    access = logging.getLogger("uvicorn.access")
    seen: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    sink = _Sink()
    before_propagate = access.propagate
    access.addHandler(sink)
    access.propagate = False
    try:
        access.handle(_access_record("/health?probe=1"))
        redacted_path = logging_setup.REDACTED_QUERY_PATHS[0]
        access.handle(_access_record(redacted_path + "?from_stop=NSR:StopPlace:1"))
    finally:
        access.removeHandler(sink)
        access.propagate = before_propagate
    assert len(seen) == 1, "the /health line must be dropped, the other kept"
    assert seen[0].args[2] == redacted_path + "?<redacted>", (
        "the redaction filter must still rewrite records that pass the mute"
    )


# ---- 3+4. LOG_LEVEL and the privacy interlock (R12, KTD12) --------------------------------------


def test_log_level_env_sets_the_root_level_and_garbage_means_info():
    cases = (
        ("DEBUG", logging.DEBUG),
        ("debug", logging.DEBUG),
        ("warning", logging.WARNING),
        ("Error", logging.ERROR),
        ("garbage", logging.INFO),
        ("NOTSET", logging.INFO),  # not a level anyone means; treated as garbage, never 0
        (None, logging.INFO),
    )
    try:
        for raw, want in cases:
            with _env(LOG_LEVEL=raw, VARSEL_LLM_LOG_PROMPTS=None):
                logging_setup.setup()
            assert logging.getLogger().level == want, (raw, want)
    finally:
        with _env(LOG_LEVEL=None, VARSEL_LLM_LOG_PROMPTS=None):
            _restore_logging()


def test_debug_with_prompt_logging_is_refused_with_one_structured_error():
    """DEBUG + VARSEL_LLM_LOG_PROMPTS would put travellers' origin/destination/departure into
    Cloud Logging as a side effect of a verbosity change — the interlock keeps INFO and says so
    exactly once, naming both variables."""
    try:
        for truthy in ("1", "true", "yes"):
            buf = io.StringIO()
            with _env(LOG_LEVEL="DEBUG", VARSEL_LLM_LOG_PROMPTS=truthy), \
                    contextlib.redirect_stdout(buf):
                logging_setup.setup()
            assert logging.getLogger().level == logging.INFO, truthy
            entries = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            conflicts = [e for e in entries if e.get("event") == "log_level_conflict"]
            assert len(conflicts) == 1, entries
            [entry] = conflicts
            assert entry["severity"] == "ERROR"
            assert "LOG_LEVEL" in entry["message"]
            assert "VARSEL_LLM_LOG_PROMPTS" in entry["message"]
        # A falsey prompt flag (llm_message's own parse: only "1"/"true"/"yes" are truthy)
        # does not trip the interlock.
        with _env(LOG_LEVEL="DEBUG", VARSEL_LLM_LOG_PROMPTS="false"):
            logging_setup.setup()
        assert logging.getLogger().level == logging.DEBUG
    finally:
        with _env(LOG_LEVEL=None, VARSEL_LLM_LOG_PROMPTS=None):
            _restore_logging()


# ---- 7. setup() re-runs ------------------------------------------------------------------------


def test_setup_rerun_keeps_one_handler_one_filter_each_and_rederives_the_level():
    try:
        with _env(LOG_LEVEL="warning", VARSEL_LLM_LOG_PROMPTS=None):
            logging_setup.setup()
            logging_setup.setup()
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
        assert root.level == logging.WARNING
        access = logging.getLogger("uvicorn.access")
        assert sum(isinstance(f, logging_setup._AccessLogRedaction) for f in access.filters) == 1
        assert sum(isinstance(f, logging_setup._HealthAccessMute) for f in access.filters) == 1
        with _env(LOG_LEVEL=None, VARSEL_LLM_LOG_PROMPTS=None):
            logging_setup.setup()
        assert root.level == logging.INFO, "a re-run must re-derive the level from env"
    finally:
        with _env(LOG_LEVEL=None, VARSEL_LLM_LOG_PROMPTS=None):
            _restore_logging()


# ---- 5. The reisevalg chronic-warning latch (R10) ------------------------------------------------


class _FailingClient:
    def query(self, sql):
        raise RuntimeError("svv_baselines not found")


class _EmptyClient:
    def query(self, sql):
        return SimpleNamespace(result=list)


@contextmanager
def _reisevalg_state(client):
    before = (
        reisevalg._bq_client,
        reisevalg._baselines,
        reisevalg._baselines_built_at,
        reisevalg._baselines_refreshed_at,
        reisevalg._baselines_unavailable_warned,
    )
    reisevalg._bq_client = client
    reisevalg._baselines_unavailable_warned = False
    try:
        yield
    finally:
        (
            reisevalg._bq_client,
            reisevalg._baselines,
            reisevalg._baselines_built_at,
            reisevalg._baselines_refreshed_at,
            reisevalg._baselines_unavailable_warned,
        ) = before


def test_the_chronic_baselines_warning_latches_to_info_and_a_success_rearms_it():
    """First unavailable refresh warns; the chronic repeat (prd deliberately has no
    svv_baselines) drops to INFO under the SAME event name; a successful refresh re-arms the
    WARNING so a NEW outage is loud again."""
    with _reisevalg_state(_FailingClient()), _capture(reisevalg.logger.name) as records:
        reisevalg.refresh_baselines()
        reisevalg.refresh_baselines()
        assert [(r.levelno, r.fields["event"]) for r in records] == [
            (logging.WARNING, "reisevalg_baselines_unavailable"),
            (logging.INFO, "reisevalg_baselines_unavailable"),
        ]
        records.clear()
        reisevalg._bq_client = _EmptyClient()
        reisevalg.refresh_baselines()
        assert [r.fields["event"] for r in records] == ["reisevalg_baselines_refreshed"]
        records.clear()
        reisevalg._bq_client = _FailingClient()
        reisevalg.refresh_baselines()
        reisevalg.refresh_baselines()
        assert [(r.levelno, r.fields["event"]) for r in records] == [
            (logging.WARNING, "reisevalg_baselines_unavailable"),
            (logging.INFO, "reisevalg_baselines_unavailable"),
        ], "a success between outages must re-arm the first-warning latch"


# ---- 6. The symmetric push audit (R11) ----------------------------------------------------------


def _raiser(exc):
    def _raise(**_kwargs):
        raise exc

    return _raise


def _push(webpush_stub):
    """Call the REAL varsel._send_push with pywebpush.webpush patched to `webpush_stub`."""
    import pywebpush

    before = pywebpush.webpush
    pywebpush.webpush = webpush_stub
    try:
        return varsel._send_push({"endpoint": "https://push.example.com/sub/1"}, {"title": "t"})
    finally:
        pywebpush.webpush = before


def test_a_failed_send_logs_nothing_itself_and_returns_the_capped_detail():
    import pywebpush

    exc = pywebpush.WebPushException(
        "server error", response=SimpleNamespace(status_code=500, text="x" * 1000)
    )
    with _capture(varsel.logger.name) as records:
        result, detail = _push(_raiser(exc))
    assert records == [], "the prose failure lines are gone — the audit record is the only trace"
    assert result == "error"
    assert detail == {"status": 500, "host": "push.example.com", "body": "x" * 400}
    # Grep-style pin: _send_push makes no log calls at all any more.
    assert "logger." not in inspect.getsource(varsel._send_push)


def test_an_unexpected_exception_still_yields_a_detail_and_no_log_line():
    with _capture(varsel.logger.name) as records:
        result, detail = _push(_raiser(RuntimeError("socket melted")))
    assert records == []
    assert result == "error"
    assert detail == {
        "status": None,
        "host": "push.example.com",
        "body": "RuntimeError: socket melted",
    }


def test_a_failed_push_yields_exactly_one_push_record_carrying_the_error_fields():
    import pywebpush

    exc = pywebpush.WebPushException(
        "gone", response=SimpleNamespace(status_code=410, text="Gone")
    )
    with _capture(varsel.logger.name) as records:
        result, detail = _push(_raiser(exc))
        varsel._log_push("test", result, "dev-1", {"title": "t", "body": "b"}, error_detail=detail)
    assert len(records) == 1, "one event='push' record per delivery attempt — nothing beside it"
    fields = records[0].fields
    assert fields["event"] == "push"
    assert fields["result"] == "gone"
    assert fields["push_error_status"] == 410
    assert fields["push_error_host"] == "push.example.com"
    assert fields["push_error_body"] == "Gone"


def test_a_successful_push_record_carries_none_of_the_error_fields():
    with _capture(varsel.logger.name) as records:
        result, detail = _push(lambda **kwargs: None)
        varsel._log_push("test", result, "dev-1", {"title": "t"}, error_detail=detail)
    assert result == "sent" and detail is None
    [record] = records
    assert record.fields["event"] == "push"
    assert not any(
        key in record.fields
        for key in ("push_error_status", "push_error_host", "push_error_body")
    )


def test_deliver_to_devices_yields_the_detail_and_tolerates_bare_string_stubs():
    """The fan-out's tuple grew a fifth element (the error detail). Suites across this repo stub
    `_send_push` with a bare result string — that shape must keep flowing (detail None), the
    fetch_candidates precedent."""
    import pywebpush

    def prepare(item):
        return ({"endpoint": "https://push.example.com/s/1"}, {"title": "t"})

    exc = pywebpush.WebPushException(
        "boom", response=SimpleNamespace(status_code=500, text="b")
    )
    before = pywebpush.webpush
    pywebpush.webpush = _raiser(exc)
    try:
        with _capture(varsel.logger.name):
            rows = varsel.deliver_to_devices({"dev-1": ["item-1"]}, prepare)
    finally:
        pywebpush.webpush = before
    assert rows == [
        (
            "dev-1",
            "item-1",
            "error",
            {"title": "t"},
            {"status": 500, "host": "push.example.com", "body": "b"},
        )
    ]
    before_send = varsel._send_push
    varsel._send_push = lambda sub, payload: "sent"
    try:
        rows = varsel.deliver_to_devices({"dev-1": ["item-1"]}, prepare)
    finally:
        varsel._send_push = before_send
    assert rows == [("dev-1", "item-1", "sent", {"title": "t"}, None)]


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} logging checks passed.")


if __name__ == "__main__":
    _run()
