"""Checks for the poller heartbeat behind GET /health, and the thread-death excepthook (U1).

Hermetic: the BigQuery client is stubbed before main is imported, and the app's lifespan is never
entered — so no poller starts and nothing here touches BigQuery or the network. That un-started
state is itself the first thing under test: /health must stay 200 for it, or every hermetic suite
(and every local run without the lifespan) would read as an unhealthy pod.

What the rest pins is the ordered liveness decision (KTD1): a dead thread handle 503s regardless
of configuration, a heartbeat older than HEALTH_STALE_AFTER_S 503s while the thread still looks
alive (the wedged-poller case a plain is_alive() can never see), the 200→503 transition leaves
exactly ONE `event="health_degraded"` record (the access log will not carry /health, so this is
the surviving forensic line), and the heartbeat beats at the TOP of the loop — before anything
that can fail — so a broken upstream keeps the pod alive while a wedged loop does not. The last
section covers `threading.excepthook`: a dying thread leaves one structured ERROR
(`event="thread_died"`) instead of the interpreter's raw stderr traceback.

Every test restores the module globals and env it touches, so the suite is order-independent.

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery \
    --with google-cloud-storage --with pywebpush --with google-cloud-firestore \
    python test_health_heartbeat.py  (or under pytest)
"""

import json
import logging
import os
import threading
import time
from contextlib import contextmanager

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import logging_setup
import main
from fastapi.testclient import TestClient

# Not entered as a context manager on purpose: that would run the lifespan hook, which starts the
# background poller and loads BigQuery lookups.
client = TestClient(main.app)


class _FakeThread:
    """Duck-types the one thing /health reads off the poller thread handle."""

    name = "poller"

    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _Recorder:
    """Collects the `extra={"fields": ...}` payloads main.logger.error is handed."""

    def __init__(self) -> None:
        self.fields: list[dict] = []

    def error(self, *args, **kwargs) -> None:
        extra = kwargs.get("extra") or {}
        self.fields.append(extra.get("fields", {}))

    def __getattr__(self, _name):  # info/exception/etc. are not under test here
        return lambda *a, **k: None


@contextmanager
def _poller_state(thread, heartbeat=None):
    """Install a fake poller handle + heartbeat with main.logger stubbed; yield the recorder.

    Restores ALL health state (handle, heartbeat, transition latch, logger) afterwards — the
    suite must stay order-independent, and a leaked latch reads exactly like a missing record.
    """
    recorder = _Recorder()
    before = (
        main._poller_thread,
        main._poller_heartbeat_monotonic,
        main._health_degraded_logged,
        main.logger,
    )
    main._poller_thread = thread
    main._poller_heartbeat_monotonic = heartbeat
    main._health_degraded_logged = False
    main.logger = recorder
    try:
        yield recorder
    finally:
        (
            main._poller_thread,
            main._poller_heartbeat_monotonic,
            main._health_degraded_logged,
            main.logger,
        ) = before


@contextmanager
def _env(**values):
    """Set env vars for the body and restore whatever was there (including absence) after."""
    before = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_health_is_ok_when_the_poller_was_never_started():
    """No thread handle means "not started", never "dead" — hermetic suites and local runs
    construct the app without its lifespan, and a probe must not read that as an unhealthy pod."""
    assert main._poller_thread is None, "this suite never enters the lifespan"
    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}


def test_a_dead_poller_thread_fails_the_probe():
    """A dead thread 503s regardless of the staleness setting — is_alive() is the direct read,
    and the body + transition record must name the cause so the restart is attributable."""
    with _poller_state(_FakeThread(alive=False)) as recorder:
        r = client.get("/health")
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["status"] != "ok"
        assert "thread" in body["reason"], body
        degraded = [f for f in recorder.fields if f.get("event") == "health_degraded"]
        assert len(degraded) == 1, degraded
        assert degraded[0]["reason"] == body["reason"]


def test_a_stale_heartbeat_fails_the_probe_and_a_fresh_one_passes():
    """The wedged-poller case: the thread is alive (blocked in a BigQuery call that will never
    return) but the heartbeat has stopped moving. is_alive() cannot see it; the age check can."""
    with _poller_state(_FakeThread(alive=True), heartbeat=time.monotonic() - 601):
        r = client.get("/health")
        assert r.status_code == 503, r.text
        assert "heartbeat" in r.json()["reason"], r.text
    with _poller_state(_FakeThread(alive=True), heartbeat=time.monotonic() - 5):
        r = client.get("/health")
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "ok"}


def test_the_degraded_record_is_written_once_per_transition():
    """One record per 200→503 TRANSITION, not per probe: kubelet probes every few seconds for the
    whole stale window, and a per-probe record would bury the one line that says why the pod was
    restarted. Recovery re-arms it, so the next stale window is its own record."""
    with _poller_state(_FakeThread(alive=True), heartbeat=time.monotonic() - 700) as recorder:
        assert client.get("/health").status_code == 503
        assert client.get("/health").status_code == 503
        degraded = [f for f in recorder.fields if f.get("event") == "health_degraded"]
        assert len(degraded) == 1, degraded
        assert "heartbeat" in degraded[0]["reason"]
        # Recovery re-arms: a fresh heartbeat serves 200 and the next stale window logs anew.
        main._poller_heartbeat_monotonic = time.monotonic()
        assert client.get("/health").status_code == 200
        main._poller_heartbeat_monotonic = time.monotonic() - 700
        assert client.get("/health").status_code == 503
        degraded = [f for f in recorder.fields if f.get("event") == "health_degraded"]
        assert len(degraded) == 2, degraded


def test_the_threshold_reads_the_env_at_call_time_and_defaults_to_600():
    """HEALTH_STALE_AFTER_S is read per call, so an env change (and these tests) takes effect
    without a restart; unset means 600 s."""
    with _env(HEALTH_STALE_AFTER_S=None):
        assert main._health_stale_after_s() == 600.0
        with _poller_state(_FakeThread(alive=True), heartbeat=time.monotonic() - 601):
            assert client.get("/health").status_code == 503
    with _env(HEALTH_STALE_AFTER_S="30"):
        with _poller_state(_FakeThread(alive=True), heartbeat=time.monotonic() - 31):
            assert client.get("/health").status_code == 503
        with _poller_state(_FakeThread(alive=True), heartbeat=time.monotonic() - 5):
            assert client.get("/health").status_code == 200


def test_stale_after_zero_disables_staleness_but_not_thread_death():
    """`0` turns the staleness branch off (a debugging escape hatch, and the documented meaning),
    but thread-death detection is not configurable — a dead poller is never a healthy pod."""
    with _env(HEALTH_STALE_AFTER_S="0"):
        with _poller_state(_FakeThread(alive=True), heartbeat=time.monotonic() - 10_000):
            assert client.get("/health").status_code == 200
        with _poller_state(_FakeThread(alive=False)):
            assert client.get("/health").status_code == 503


class _Ticks:
    """Duck-types the stop_event: poll_for_changes runs exactly `n` loop iterations."""

    def __init__(self, n: int) -> None:
        self.left = n

    def wait(self, interval: float) -> bool:
        self.left -= 1
        return self.left <= 0


def test_the_heartbeat_beats_even_when_the_ingestion_read_raises():
    """R1: the heartbeat is written at the TOP of every iteration, before anything that can fail.
    A broken upstream then surfaces as the existing per-tick logged error while the pod stays
    alive — restarting cannot fix BigQuery — and only a loop that stopped ITERATING goes stale."""

    def _raise():
        raise RuntimeError("BigQuery is having a day")

    before = (
        main.get_last_ingestion_time,
        main.togvarsel.refresh,
        main.togvarsel_notify.evaluate,
        main.togvarsel_outcomes.refresh,
        main.logger,
        main._poller_heartbeat_monotonic,
    )
    main.get_last_ingestion_time = _raise
    main.togvarsel.refresh = lambda: None
    main.togvarsel_notify.evaluate = lambda: None
    main.togvarsel_outcomes.refresh = lambda: None
    main.logger = _Recorder()
    main._poller_heartbeat_monotonic = None
    try:
        main.poll_for_changes(lambda ts: None, _Ticks(1))
        assert main._poller_heartbeat_monotonic is not None, (
            "the heartbeat must be written even on a tick whose ingestion read raised"
        )
        assert time.monotonic() - main._poller_heartbeat_monotonic < 60
    finally:
        (
            main.get_last_ingestion_time,
            main.togvarsel.refresh,
            main.togvarsel_notify.evaluate,
            main.togvarsel_outcomes.refresh,
            main.logger,
            main._poller_heartbeat_monotonic,
        ) = before


def test_a_dying_thread_leaves_one_structured_error_record():
    """R3: an uncaught exception in a thread becomes ONE structured ERROR through the JSON
    formatter (`event="thread_died"`, thread name, traceback in `message`) instead of the
    interpreter's raw stderr traceback, which GKE ingests split line-by-line. Installed by
    logging_setup.setup(), idempotently — re-running setup() must reassign the same hook."""
    logging_setup.setup()
    logging_setup.setup()  # idempotent across re-runs (main imports it once; tests re-run it)
    assert threading.excepthook is logging_setup._log_thread_exception

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    root = logging.getLogger()
    root.addHandler(handler)
    try:

        def _boom():
            raise RuntimeError("boom-in-thread")

        t = threading.Thread(target=_boom, name="dying-thread")
        t.start()
        t.join()
        # SystemExit is the benign way a thread asks to stop; threading's own default hook
        # ignores it, and so must this one — it is not a death worth an ERROR.
        quiet = threading.Thread(target=lambda: (_ for _ in ()).throw(SystemExit()))
        quiet.start()
        quiet.join()
    finally:
        root.removeHandler(handler)

    died = [r for r in records if getattr(r, "fields", {}).get("event") == "thread_died"]
    assert len(died) == 1, [r.getMessage() for r in records]
    assert died[0].levelno == logging.ERROR
    assert died[0].fields["thread"] == "dying-thread"  # type: ignore[attr-defined]
    entry = json.loads(logging_setup.JsonFormatter().format(died[0]))
    assert entry["severity"] == "ERROR"
    assert entry["event"] == "thread_died"
    assert "boom-in-thread" in entry["message"], entry


def test_no_poller_path_bigquery_wait_is_unbounded():
    """Every `.result()` in the togvarsel modules passes a timeout (KTD1's other half).

    The wedge guard is only as good as its coverage: the client's default is to wait FOREVER, so
    one unbounded call on the poller thread is enough to hold a tick past HEALTH_STALE_AFTER_S,
    503 /health and have liveness restart the replica into the same hang next tick — a crash loop
    against an outage no restart can fix. Three such calls were left behind when the timeouts went
    in (togvarsel `_rows` and the feature rebuild, togvarsel_notify's station fetch), all found by
    review rather than by a test, which is what this is.

    Source-level and scoped to these three modules on purpose. They are wholly poller-path, so the
    rule is unconditional here and needs no judgment about the caller. `main.py` is deliberately
    NOT covered: it mixes the poller path with the request path, where an unbounded wait holds an
    anyio slot instead of the tick and the five that remain there are legitimate — a grep cannot
    tell the two apart, and one asserting over both would have to be either wrong or disabled.
    """
    import pathlib
    import re

    offenders = []
    for name in ("togvarsel.py", "togvarsel_notify.py", "togvarsel_outcomes.py"):
        text = (pathlib.Path(__file__).parent / name).read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"\.result\(\s*\)", line):
                offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, "unbounded BigQuery wait on the poller thread:\n" + "\n".join(offenders)


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} health/heartbeat checks passed.")


if __name__ == "__main__":
    _run()
