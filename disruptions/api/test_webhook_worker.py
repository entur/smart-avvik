"""Checks for webhook_worker.py: the delivery loop, its health rule and what it refuses to import.

Hermetic and store-free. `webhook_delivery.deliver_pending` is already covered by its own suite, so
nothing here reaches Firestore or a socket: the loop's seam is driven with a stub to exact arity,
and the health server is bound on port 0 and shut down again.

Nothing here sends a real signal. A SIGTERM in a test process is a test harness that stops mid-run
on any platform that delivers it slightly differently, and what the loop actually depends on is the
`threading.Event` the handler sets — so the tests drive the event and ONE test asserts, off the
source, that the handler sets it and nothing else.

The suite runs as its own process (`python test_webhook_worker.py`, never pytest), like every suite
in this directory, and its runner is at the END of the file: a runner placed mid-file silently skips
every test below it while still printing a passing count.
"""

from __future__ import annotations

import ast
import http.client
import inspect
import os
import pathlib
import threading
import time
from datetime import UTC, datetime, timedelta

import metrics
import webhook_delivery
import webhook_worker

MODULE_SOURCE = pathlib.Path(webhook_worker.__file__).read_text()


# ---- Helpers ----------------------------------------------------------------------------------


class _Deliver:
    """A `deliver_pending` stub to EXACT ARITY: the real one takes only keyword arguments and the
    worker passes none, so a stub taking any positional would hide a call that grew one.
    """

    def __init__(self, *reports: webhook_delivery.DeliveryReport) -> None:
        self.reports = list(reports)
        self.calls = 0

    def __call__(self) -> webhook_delivery.DeliveryReport:
        self.calls += 1
        if self.reports:
            return self.reports.pop(0)
        return webhook_delivery.DeliveryReport()


class _Sleeps:
    """A stop event that records what it was asked to wait for and stops the loop after N rounds.

    Only the two methods `run` actually calls -- `is_set` and `wait` -- because recording what the
    loop waited FOR is the whole assertion and a real `threading.Event` cannot be asked. The pairing
    is what keeps that honest: `test_a_stop_already_set_delivers_nothing_at_all` drives a real
    `threading.Event`, so a loop that started using a third method of it would fail there rather
    than pass here against a double that quietly grew.
    """

    def __init__(self, rounds: int) -> None:
        self.event = threading.Event()
        self.waits: list[float] = []
        self.remaining = rounds

    def is_set(self) -> bool:
        if self.remaining <= 0:
            return True
        self.remaining -= 1
        return False

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(float(timeout or 0.0))
        return False


def _reset() -> None:
    webhook_worker._last_round_monotonic = None
    os.environ.pop("HEALTH_STALE_AFTER_S", None)


def _report(**fields: object) -> webhook_delivery.DeliveryReport:
    return webhook_delivery.DeliveryReport(**fields)  # type: ignore[arg-type]


# ---- The loop ---------------------------------------------------------------------------------


def test_an_empty_queue_sleeps_and_the_heartbeat_still_beats():
    """The plan's first scenario. A quiet night is the state a liveness alert is most likely to
    misread: nothing is delivered, so a heartbeat tied to WORK would go stale and page for a worker
    doing exactly what it should. The beat is per ROUND, not per delivery.
    """
    _reset()
    deliver = _Deliver()
    stop = _Sleeps(rounds=2)
    rounds = webhook_worker.run(stop, deliver=deliver, idle_sleep_s=7.0)  # type: ignore[arg-type]
    assert rounds == 2 and deliver.calls == 2
    assert stop.waits == [7.0, 7.0], stop.waits
    assert webhook_worker._last_round_monotonic is not None
    assert webhook_worker.worker_health() == (True, None)


def test_a_round_that_found_work_loops_again_without_sleeping():
    """A backlog drains at the rate this loop turns, and the read is bounded per round -- so a
    sleep after a round that returned a full page would cap the drain rate at one page per idle
    sleep. 20 000 deliveries at 200 per round is 100 rounds: three seconds each is five minutes of
    pure waiting.
    """
    _reset()
    deliver = _Deliver(_report(ready=200, sent=200), _report(ready=0))
    stop = _Sleeps(rounds=2)
    webhook_worker.run(stop, deliver=deliver, idle_sleep_s=5.0)  # type: ignore[arg-type]
    assert stop.waits == [5.0], "a round with work must not sleep; a round without it must"


def test_a_store_that_cannot_be_read_sleeps_longer_and_the_loop_continues():
    """The plan's second scenario. `deliver_pending` reports a failed read as `store_errors` rather
    than raising, so the loop sees a perfectly ordinary report -- and must not treat it as an empty
    queue, which would dial the same failing store every idle sleep for the length of the outage.
    """
    _reset()
    deliver = _Deliver(_report(store_errors=1), _report(ready=0))
    stop = _Sleeps(rounds=2)
    webhook_worker.run(stop, deliver=deliver, idle_sleep_s=3.0, error_sleep_s=41.0)  # type: ignore[arg-type]
    assert stop.waits == [41.0, 3.0], stop.waits
    assert deliver.calls == 2, "the loop keeps going after a store error"


def test_a_round_that_raises_does_not_end_the_process():
    """`deliver_pending` documents that it never raises, and the loop catches anyway: being wrong
    about that costs a CrashLoopBackOff whose backoff IS the outage, and being right costs one
    unreachable branch. The heartbeat still beats -- a worker that is retrying is alive.
    """
    _reset()

    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> webhook_delivery.DeliveryReport:
            self.calls += 1
            raise RuntimeError("firestore said no")

    deliver = _Boom()
    stop = _Sleeps(rounds=2)
    rounds = webhook_worker.run(stop, deliver=deliver, error_sleep_s=13.0)  # type: ignore[arg-type]
    assert deliver.calls == 2 and rounds == 0, "a raised round is not a round that ran"
    assert stop.waits == [13.0, 13.0]
    assert webhook_worker._last_round_monotonic is not None


def test_the_stop_flag_is_read_between_rounds_and_never_inside_one():
    """The SIGTERM contract. A round is already bounded and holds claims under U8's lease, so
    tearing one down halfway would leave deliveries parked until the lease lapses. The loop
    therefore checks the flag at the top and at the sleep, and `deliver` takes no cancellation
    argument at all -- which is what this asserts, off the signature rather than off behaviour.
    """
    _reset()
    params = inspect.signature(webhook_worker.run).parameters
    assert list(params) == ["stop", "deliver", "idle_sleep_s", "error_sleep_s"], list(params)
    source = inspect.getsource(webhook_worker.run)
    assert "while not stop.is_set():" in source
    # Nothing inside the round consults it: the only other mention is the interruptible sleep.
    assert source.count("stop.") == 2, source


def test_a_stop_already_set_delivers_nothing_at_all():
    """A pod that is told to shut down while it is idle must not start one more round: the round
    would claim deliveries with a 20 s lease that this process will not be alive to resolve.
    """
    _reset()
    deliver = _Deliver()
    stop = threading.Event()
    stop.set()
    assert webhook_worker.run(stop, deliver=deliver) == 0
    assert deliver.calls == 0


def test_the_signal_handler_only_sets_the_event():
    """What a handler must NOT do is the point. It runs on the main thread between bytecodes, so it
    can interrupt anything -- a store write, `sys.exit` unwinding through a round in flight -- and
    the only thing it is allowed to do is flip a flag the loop reads between rounds.

    The one call beside `set` is `info`, which is deliberate and is ordered after it: the stdlib's
    logging locks are reentrant so it cannot deadlock, and setting first means the state change
    cannot be lost to a slow stream. Asserted as the exact SET of calls rather than a substring
    search, so a store write added here fails.
    """
    tree = ast.parse(inspect.getsource(webhook_worker._install_stop))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_handle"
    )
    calls = [
        node.func.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert sorted(calls) == ["info", "set"], calls


# ---- The backlog gauge ------------------------------------------------------------------------


def test_every_round_publishes_the_backlog_age_including_a_round_that_found_none():
    """Zero on an empty queue, not "leave the last value standing". A gauge that keeps its previous
    reading after the backlog drains reports a queue that no longer exists, and the alert on it
    fires for the length of the scrape interval after the incident is over.
    """
    _reset()
    metrics.DELIVERY_BACKLOG_AGE.set(0.0)
    deliver = _Deliver(_report(ready=3, backlog_age_s=612.0), _report(ready=0, backlog_age_s=0.0))
    webhook_worker.run(_Sleeps(rounds=1), deliver=deliver)  # type: ignore[arg-type]
    assert metrics.DELIVERY_BACKLOG_AGE._value.get() == 612.0
    webhook_worker.run(_Sleeps(rounds=1), deliver=deliver)  # type: ignore[arg-type]
    assert metrics.DELIVERY_BACKLOG_AGE._value.get() == 0.0


def test_the_report_ages_the_oldest_deliverable_record_and_ignores_one_with_no_stamp():
    """`created_at` arrives with U11, so every delivery minted before it is None -- and reading a
    missing stamp as "infinitely old" would make the gauge spike on the deploy that introduces it,
    which is the one reading nobody can act on. The oldest STAMPED row wins; an unstamped one is
    simply not in the answer.
    """
    import notification_store

    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    def _delivery(created_at):
        return notification_store.Delivery(
            delivery_id="d",
            event_id="e",
            alert_id="a",
            subscription_id="s",
            subscription_version=1,
            client_id="c",
            destination_id="dest",
            state="pending",
            attempts=0,
            revision=1,
            valid_until=now,
            retry_not_before=now,
            claimed_until=None,
            expires_at=now,
            situations=(),
            cap_exempt=False,
            test=False,
            created_at=created_at,
        )

    ready = (
        _delivery(None),
        _delivery(now - timedelta(seconds=300)),
        _delivery(now - timedelta(seconds=90)),
    )
    minted = [d.created_at for d in ready if d.created_at is not None]
    assert len(minted) == 2
    assert (now - min(minted)).total_seconds() == 300.0
    assert notification_store.Delivery(  # the default is what keeps every other construction valid
        delivery_id="d",
        event_id="e",
        alert_id="a",
        subscription_id="s",
        subscription_version=1,
        client_id="c",
        destination_id="dest",
        state="pending",
        attempts=0,
        revision=1,
        valid_until=None,
        retry_not_before=None,
        claimed_until=None,
        expires_at=None,
        situations=(),
        cap_exempt=False,
        test=False,
    ).created_at is None


# ---- The health server ------------------------------------------------------------------------


def _get(port: int, path: str = "/health") -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


def test_health_answers_200_before_the_first_round_and_after_one():
    """The window between the port binding and the first round is the startup probe's whole
    purpose; failing it there fails the deploy the probe exists to allow.
    """
    _reset()
    server = webhook_worker.start_health_server(0)
    try:
        assert _get(server.server_address[1]) == (200, "ok")
        webhook_worker._beat()
        assert _get(server.server_address[1]) == (200, "ok")
    finally:
        server.shutdown()
        server.server_close()


def test_a_stalled_loop_answers_503_and_says_how_old():
    """The state the probe exists for: the process is up, the port answers, and no round has
    finished. Driven by moving the heartbeat backwards rather than by waiting 600 s.
    """
    _reset()
    os.environ["HEALTH_STALE_AFTER_S"] = "30"
    server = webhook_worker.start_health_server(0)
    try:
        webhook_worker._last_round_monotonic = time.monotonic() - 45.0
        status, body = _get(server.server_address[1])
        assert status == 503 and "45 s old" in body, (status, body)
        assert "limit 30 s" in body
    finally:
        server.shutdown()
        server.server_close()
        _reset()


def test_the_staleness_check_can_be_switched_off_and_an_unparsable_value_is_the_default():
    """`0` disables it — the escape hatch for an environment whose rounds legitimately run long —
    and a typo must not take the probe down with a ValueError, which would answer no request at all.
    """
    _reset()
    webhook_worker._last_round_monotonic = time.monotonic() - 10_000.0
    os.environ["HEALTH_STALE_AFTER_S"] = "0"
    assert webhook_worker.worker_health() == (True, None)
    os.environ["HEALTH_STALE_AFTER_S"] = "not-a-number"
    assert webhook_worker._health_stale_after_s() == 600.0
    healthy, reason = webhook_worker.worker_health()
    assert healthy is False and reason is not None
    _reset()


def test_only_health_answers_and_probe_requests_are_not_logged():
    """Two things a probe endpoint gets wrong by default. Any other path must 404, or a probe
    misconfigured onto `/` reports a wedged worker as Ready; and `BaseHTTPRequestHandler` logs every
    request to stderr, which at three probes every five seconds is ~50 000 lines a day counted by
    `kix_log_records_total` as well.
    """
    _reset()
    server = webhook_worker.start_health_server(0)
    try:
        assert _get(server.server_address[1], "/")[0] == 404
        assert _get(server.server_address[1], "/metrics")[0] == 404
        assert _get(server.server_address[1], "/health?probe=liveness")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
    assert inspect.getsource(webhook_worker._HealthHandler.log_message).count("self.") == 0


# ---- Startup ----------------------------------------------------------------------------------


def test_a_worker_that_cannot_sign_or_read_the_store_exits_non_zero():
    """Loud and fatal, which is the opposite of every runtime failure in this module. A pod that
    stays up logging "unconfigured" once a round is Ready to every probe and green on every
    dashboard, and the deploy that introduced the mistake reports success. Exiting non-zero makes
    `helm --wait` roll the release back.
    """
    _reset()
    original = (
        webhook_worker.notification_store.enabled,
        webhook_worker.webhook_signing.configured,
        webhook_worker.run,
    )
    try:
        webhook_worker.notification_store.enabled = lambda: False
        webhook_worker.webhook_signing.configured = lambda: False
        # `run` is stubbed so that a `main` which WRONGLY got past the guard returns instead of
        # looping forever: a falsification of this test has to fail, and a suite that hangs is
        # a CI timeout twenty minutes later rather than a red assertion.
        webhook_worker.run = lambda _stop: 0
        missing = webhook_worker._configure()
        assert missing == ["NOTIFICATION_FIRESTORE_DATABASE", "WEBHOOK_SIGNING_ROOT"], missing
        assert webhook_worker.main() == 1

        webhook_worker.notification_store.enabled = lambda: True
        assert webhook_worker._configure() == ["WEBHOOK_SIGNING_ROOT"]
        assert webhook_worker.main() == 1, "one missing value is still a worker that cannot deliver"
    finally:
        webhook_worker.notification_store.enabled = original[0]
        webhook_worker.webhook_signing.configured = original[1]
        webhook_worker.run = original[2]


def test_the_store_is_wired_the_way_mains_lifespan_wires_it():
    """ONE client for the channel and one place that names the database. Two `configure` calls that
    drift apart is a worker reading a different database than the poller writes — which fails as an
    empty pending set, i.e. as a quiet day.
    """
    source = inspect.getsource(webhook_worker._configure)
    assert 'os.environ.get("NOTIFICATION_FIRESTORE_DATABASE"' in source
    assert "span=tracing_setup.span" in source
    assert "notification_auth.configure()" in source
    assert "notification_store.configure(" in source


# ---- What it must not import ------------------------------------------------------------------


def test_the_worker_imports_neither_main_nor_varsel():
    """Both would be wrong for reasons that do not look like import hygiene. `main` reads its `.sql`
    files at import and builds the whole FastAPI app, so the worker pod would pay the API's startup
    and hold its own copy of every route; `varsel` carries the browser channel's in-process state,
    which has exactly one owner and it is not this process. An AST walk rather than a substring
    search, because both names appear in this module's prose.
    """
    tree = ast.parse(MODULE_SOURCE)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert "main" not in imported and "varsel" not in imported, sorted(imported)
    assert "webhook_delivery" in imported and "notification_store" in imported


def test_the_worker_emits_one_event_name_of_its_own():
    """`webhook_worker` for the process's own lifecycle, and nothing else: the rounds are already
    `webhook_delivery_round`, written by the module that does the work. A second name for the same
    record would drop it out of every query that exists — the meldinger-rename lesson.
    """
    assert '"event": "webhook_worker"' in MODULE_SOURCE
    assert MODULE_SOURCE.count('"event": "') == MODULE_SOURCE.count('"event": "webhook_worker"')


if __name__ == "__main__":
    _ran = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            _ran += 1
            print(f"ok  {_name}")
    # The COUNT, in every other suite's shape: a runner that silently collects nothing still exits
    # 0, so "it passed" is not the check -- "the number moved" is.
    print(f"\n{_ran} delivery worker checks passed.")
