"""The webhook delivery worker: its own workload, the same image, one bounded round at a time.

Delivery was a phase of the poller's cycle (`main._rebuild_all`'s `notification_webhooks`), which
put it behind ~30 s of BigQuery on a loop whose period is set by how often SIRI-SX moves — about
296 s. A pending event is valid for an hour, so at a few hundred subscriptions that is fine and at a
hundred thousand it is not: a bad morning's backlog cannot drain in the ~20 s the phase's own
deadline gives it, and the events expire undelivered while the poller is busy scanning ET
partitions. This process does nothing else. It calls
:func:`webhook_delivery.deliver_pending` in a loop and sleeps only when there is nothing to send.

**The same image, a second Helm release.** The Entur common chart renders exactly ONE Deployment per
release — `containers` is a list of containers in one POD, not a list of workloads — so a second
workload is a second release of the same chart with `container.args` pointed here (KTD18). Nothing
about the image changes: `helm/kix-avvik/env/worker-values-kub-ent-*.yaml` turns the Service and the
Ingress off, points the probes at this module's own health port, and names only the `notifications`
secret group. There is deliberately no second service account: the chart does not create one (the
platform provisions `application` in the namespace), so both releases run as the identity that
already holds `roles/datastore.user`.

**Which process delivers is ONE environment variable, on the poller's side.**
`NOTIFICATION_DELIVERY_WORKER_ENABLED` is read by `main`, not here — this process delivers
unconditionally, because it exists only where delivery is wanted. That asymmetry is the rollout: the
worker release lands first with the poller still delivering, both processes deliver for a window,
and only then does the flag turn the poller's half off. Doing it the other way round means a worker
deploy that fails to become Ready leaves NOBODY delivering, and gha-helm's rollback only reaches the
release it deployed. What the overlap costs is worth stating rather than discovering: two readers
each apply the per-destination attempt ceiling to their own read, so a destination can see up to
twice its `deliveries_per_minute` for that window, and U8's lease is what keeps the traveller from
being notified twice — `leased` and `lease_lost` on `webhook_delivery_round` become reachable for
the first time here, and seeing them is the evidence that the lease is doing its job.

**It fails loudly at startup and never at runtime.** A missing database or an unusable signing root
is a deployment that cannot deliver anything, so :func:`main` exits non-zero and `helm --wait`
rolls the release back rather than leaving a pod that loops writing warnings. Once the loop is
running the opposite rule holds: `deliver_pending` never raises, every round is wrapped anyway, and
a round that hit the store's own errors sleeps longer instead of exiting. A worker that exits on a
Firestore blip is a CrashLoopBackOff whose backoff is the outage.

**The health rule is `main`'s, minus the branch it cannot need.** `/health` answers 503 when no
round has finished inside `HEALTH_STALE_AFTER_S` (600 s by default, the same variable the API
reads). It has no "the thread has died" branch because the loop IS the main thread: a loop that
dies ends the process, and Kubernetes restarts the container without needing a probe to notice.
"""

import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import logging_setup  # noqa: F401  (import side effect: golden-path JSON logging on stdout)
import metrics  # sibling module: the Prometheus registry and the log-level counting handler
import notification_auth  # sibling module: client accounts, quotas, destinations, R12's pause
import notification_store  # sibling module: durable webhook subscriptions, events and deliveries
import tracing_setup  # import side effect: OTel tracing when TRACING_ENABLED (fail-open)
import webhook_delivery  # sibling module: one bounded webhook attempt per pending notification event
import webhook_signing  # sibling module: Standard Webhooks signing, per destination per attempt

logger = logging.getLogger(__name__)

#: How long to wait after a round that found nothing. Short, because the whole point of the worker
#: is that a delivery does not wait for a poller cycle: at ~200 ms per Firestore read this is about
#: 20 000 reads a day, on the order of $0.01 — the latency is worth more than the read is.
_IDLE_SLEEP_S = 3.0

#: How long to wait after a round the store could not answer. Longer than the idle sleep, and for a
#: different reason: hammering a Firestore that is returning errors is how a blip becomes a bill.
#: Still far inside an event's one-hour validity, so a recovered store loses nothing.
_ERROR_SLEEP_S = 20.0

#: The port this module's own `/health` listens on. NOT 8080: the chart's default probes and its
#: Service both point at `service.internalPort`, and a worker answering on the app's port would make
#: the two releases look interchangeable to anything probing by port.
_HEALTH_PORT_ENV = "WORKER_HEALTH_PORT"
_DEFAULT_HEALTH_PORT = 8081

#: Set at the end of every round, whether or not it delivered anything — the same "beat on progress,
#: not on iteration" rule `main._beat` documents at length. A round is bounded at
#: `_DELIVERY_DEADLINE_S + _DELIVERY_WAIT_S` (~40 s) plus the stale sweep, so a beat per round is
#: already fine against a 600 s threshold; beating per iteration only would be wrong the day a round
#: grows an unbounded step.
_last_round_monotonic: float | None = None


def _beat() -> None:
    """Record that a delivery round finished. BOTH clocks, for `main._beat`'s reason.

    The monotonic one is `/health`'s (age arithmetic, immune to a wall-clock step); the epoch gauge
    is what the runbook's worker-stopped rule selects on. Beating only one leaves the alert and the
    probe disagreeing about the same process.
    """
    global _last_round_monotonic
    _last_round_monotonic = time.monotonic()
    metrics.WORKER_LAST_TICK.set(time.time())


def _health_stale_after_s() -> float:
    """Staleness threshold, from HEALTH_STALE_AFTER_S (seconds; default 600).

    Read at call time and sharing the API's variable deliberately: one number for "this process has
    stopped making progress" across both workloads. `0` disables the check; an unparsable value
    falls back to the default rather than taking the probe down with a ValueError.
    """
    raw = os.environ.get("HEALTH_STALE_AFTER_S", "").strip()
    try:
        return float(raw) if raw else 600.0
    except ValueError:
        return 600.0


def worker_health() -> tuple[bool, str | None]:
    """(healthy, reason when not) — the decision behind this process's GET /health.

    No heartbeat yet is healthy: that is the window between the port binding and the first round
    finishing, and a probe failing there would fail the deploy the startup probe exists to allow.
    """
    if _last_round_monotonic is None:
        return True, None
    stale_after = _health_stale_after_s()
    if stale_after > 0:
        age = time.monotonic() - _last_round_monotonic
        if age > stale_after:
            return False, f"last delivery round is {age:.0f} s old (limit {stale_after:.0f} s)"
    return True, None


class _HealthHandler(BaseHTTPRequestHandler):
    """`/health` and nothing else. 404 for every other path, so a stray probe cannot read as Ready."""

    #: HTTP/1.1 so the kubelet's probe does not re-open a connection per request. The default is
    #: HTTP/1.0, which closes after every response — three probes every 5 s is 0.6 handshakes a
    #: second for the life of the pod, for a body of about twenty bytes.
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # BaseHTTPRequestHandler dispatches on the method name
        if self.path.split("?", 1)[0] != "/health":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        healthy, reason = worker_health()
        body = (reason or "ok").encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence. Three probes every five seconds would otherwise be ~50 000 lines a day of
        `"GET /health HTTP/1.1" 200` on stderr, and they would be counted by the log-level metrics
        as well — a liveness probe is not an event.
        """


def start_health_server(port: int) -> ThreadingHTTPServer:
    """Bind `/health` on a daemon thread and return the server (so a test can shut it down).

    Threading rather than single-threaded: liveness, readiness and startup probes can arrive at
    once, and a serial server would queue the third behind the first two.
    """
    server = ThreadingHTTPServer(("", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="worker-health", daemon=True)
    thread.start()
    return server


def run(
    stop: threading.Event,
    *,
    deliver: object = None,
    idle_sleep_s: float = _IDLE_SLEEP_S,
    error_sleep_s: float = _ERROR_SLEEP_S,
) -> int:
    """Deliver until `stop` is set. Returns the number of rounds that ran.

    The stop flag is consulted BETWEEN rounds and never inside one: a round is already bounded, and
    tearing one down halfway would leave claims held until their leases lapse. With the Deployment's
    `terminationGracePeriodSeconds` above a round's worst case (~40 s plus the stale sweep), a
    SIGTERM therefore lets the round in flight finish and its attempts record their outcomes; below
    it, SIGKILL lands mid-round and U8's lease is what makes that safe — the deliveries return to
    the pending set when the lease lapses, rather than being lost or sent twice.

    `deliver` is the seam the suites drive. Defaulted to None rather than to the function itself so
    the module-level name is resolved per call: a test that patches
    `webhook_delivery.deliver_pending` after import still takes effect.
    """
    send = deliver if deliver is not None else webhook_delivery.deliver_pending
    rounds = 0
    while not stop.is_set():
        sleep_s = idle_sleep_s
        try:
            report = send()
        except Exception:
            # `deliver_pending` documents that it never raises, and this catch is here anyway: the
            # cost of being wrong about that is a CrashLoopBackOff whose backoff IS the outage, and
            # the cost of being right is one unreachable branch.
            logger.exception(
                "A webhook delivery round raised; the worker keeps going",
                extra={"fields": {"event": "webhook_worker", "result": "error"}},
            )
            sleep_s = error_sleep_s
        else:
            rounds += 1
            metrics.DELIVERY_BACKLOG_AGE.set(report.backlog_age_s)
            if report.store_errors:
                sleep_s = error_sleep_s
            elif report.ready:
                # There was work, so there may be more: the read is bounded by
                # `MAX_DELIVERY_ATTEMPTS_PER_CYCLE` and the per-destination ceiling, and a backlog
                # drains at the rate this loop turns. Sleeping here would cap the drain rate at one
                # page per `idle_sleep_s` for no reason.
                sleep_s = 0.0
        _beat()
        if sleep_s > 0:
            # `wait` rather than `sleep`, so a SIGTERM arriving during the idle gap is acted on at
            # once instead of after it: a pod that takes three seconds to notice it is being shut
            # down eats three seconds of every rollout's grace period.
            stop.wait(sleep_s)
    return rounds


def _install_stop(stop: threading.Event) -> None:
    """Make SIGTERM and SIGINT set `stop`.

    Python installs no SIGTERM handler of its own, so without this the default disposition
    terminates the process immediately — mid-round, every time, on every rollout.
    """

    def _handle(signum: int, _frame: object) -> None:
        logger.info(
            "Signal %d received; the worker stops after the round in flight",
            signum,
            extra={"fields": {"event": "webhook_worker", "result": "stopping", "signal": signum}},
        )
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def _configure() -> list[str]:
    """Wire the store, the identity plane and the exporter. Returns what is missing, if anything.

    The same two `configure` calls `main`'s lifespan makes and for the same reasons: ONE client for
    the channel (`notification_store.configure` is the only place that names the database, and
    `notification_auth` reads through `notification_store.firestore_client()`), and
    `notification_auth.configure()` for its ledger reset, which must not survive a restart-in-place.
    """
    metrics.configure()
    notification_store.configure(
        database=os.environ.get("NOTIFICATION_FIRESTORE_DATABASE", "").strip() or None,
        span=tracing_setup.span,
    )
    notification_auth.configure()
    missing = []
    if not notification_store.enabled():
        missing.append("NOTIFICATION_FIRESTORE_DATABASE")
    if not webhook_signing.configured():
        missing.append(webhook_signing.SIGNING_ROOT_ENV)
    return missing


def main() -> int:
    """The `python -m webhook_worker` entrypoint. Returns a process exit code."""
    missing = _configure()
    if missing:
        # Loud, and fatal. A worker that cannot sign or cannot read the store delivers nothing; a
        # pod that stays up logging that once a round looks alive to every probe and every
        # dashboard, and the deploy that introduced the mistake reports success.
        logger.error(
            "The delivery worker cannot start: %s missing or unusable",
            ", ".join(missing),
            extra={
                "fields": {
                    "event": "webhook_worker",
                    "result": "unconfigured",
                    "missing": ",".join(missing),
                }
            },
        )
        return 1
    try:
        metrics.start(int(os.environ.get("METRICS_PORT", "9464")))
    except Exception:
        # Tolerated exactly as `main`'s lifespan tolerates it: losing the exporter must not stop the
        # process delivering. The health port below is a different matter — a worker with no probe
        # endpoint fails its own readiness and is never Ready.
        logger.exception("Prometheus exporter failed to start; metrics unavailable this process")
    port = int(os.environ.get(_HEALTH_PORT_ENV, "").strip() or _DEFAULT_HEALTH_PORT)
    start_health_server(port)
    stop = threading.Event()
    _install_stop(stop)
    logger.info(
        "Webhook delivery worker started on health port %d",
        port,
        extra={"fields": {"event": "webhook_worker", "result": "started", "health_port": port}},
    )
    rounds = run(stop)
    logger.info(
        "Webhook delivery worker stopped after %d round(s)",
        rounds,
        extra={"fields": {"event": "webhook_worker", "result": "stopped", "rounds": rounds}},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
