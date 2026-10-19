"""Who runs the poller: the lease state machine (zero-downtime U2, KTD4/KTD17).

Every API pod runs the same poller loop, and the pod holding the lease runs the cycle and the tick
writers while every other pod only loads what the leader built (KTD1/KTD2). This module is the
state machine between the loop and the lease document, and nothing else: the three store calls
(`notification_store.acquire_poller_lease`, `renew_poller_lease`, `release_poller_lease`), the
heartbeat reader and the two callbacks are all INJECTED, composed in `main.lifespan`. It imports no
sibling module, which `test_poller_lease` pins off the source, so `varsel` never sees `main` and no
import cycle can appear through it.

**The local deadline is anchored on a HEARTBEAT, never on the clock at renewal.** Each successful
acquire or renewal writes `held_until` as the heartbeat it carried plus :data:`TTL_S`, and this
process believes it holds the lease until that same heartbeat plus the TTL minus :data:`MARGIN_S`
-- on the monotonic clock, so a wall-clock step cannot move it. So the lease lapses TTL after the
last beat however many renewals land: a leader whose thread wedged loses it 300 s after its last
step finished, and a leader whose Firestore died while its BigQuery kept beating loses it at the
last renewal's heartbeat plus 270 s, before the stored `held_until` lets a follower in. Anchored
on the LIVE heartbeat instead, that second leader would answer `held()` forever.

**Callbacks run on the poller thread only.** The renewal timer and :meth:`StoreLease.held` change
state under the lock and nothing else; :meth:`StoreLease.tick`, called at the top of each poller
iteration, notices a lost lease and runs `on_lose`, and runs `on_acquire` after an acquisition.
`on_lose` waits on the emit pass lock, which must never happen on the timer thread or inside a
phase's lease check.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: The lease's lifetime past the heartbeat it was written with. Kept equal to
#: `notification_store.POLLER_LEASE_S`, which writes `held_until`; this module may not import it,
#: so `test_poller_lease` asserts the two agree.
TTL_S = 300.0
#: How much earlier than the stored `held_until` this process stops believing it leads: clock skew
#: between pods (the acquirer compares against its own wall clock) plus the wait of the call that
#: would have renewed it.
MARGIN_S = 30.0
#: The renewal timer's period.
RENEW_EVERY_S = 30.0
#: Renew only while the heartbeat is younger than this. Past it the thread has not finished a step
#: in 250 s, and letting the lease lapse is what hands the cycle to a follower. 250 + the margin is
#: inside the TTL, so the last renewal still leaves the local deadline before the stored one.
RENEW_WHILE_FRESH_S = 250.0
#: How long the timer waits on one renewal before walking away from it (the call is left running
#: on its own thread; the next period skips while it is in flight). A transactional commit carries
#: no client-side deadline, so this is what bounds the timer, and the local deadline is what bounds
#: the lease: a hung store costs the lease after the margin, which is the outage behaviour wanted.
RENEW_WAIT_S = 20.0
#: Ticks a pod stays a follower after an acquisition whose `on_acquire` failed, so a partial store
#: outage does not spin every pod through acquire and release on every tick (KTD14).
BACKOFF_TICKS = 5
#: The longest wait that does not beat: one BigQuery call at `main._POLLER_QUERY_TIMEOUT_S`. The
#: local deadline (TTL - margin after the last beat) must exceed it, or a healthy leader inside one
#: slow query would demote itself.
LONGEST_BEATLESS_WAIT_S = 240.0

#: The pod's `terminationGracePeriodSeconds` and its preStop sleep (KTD9), in the Helm values. Not
#: read from the environment: Kubernetes does not hand the grace to the container, and a value
#: here that disagreed with the chart would be worse than a constant a test holds to it.
GRACE_S = 270.0
PRESTOP_S = 5.0
#: What the shutdown keeps back for the release and the exit after the join and the drain.
SHUTDOWN_MARGIN_S = 10.0
#: The one deadline the lifespan takes at SIGTERM: join the poller, wait for the emit pass, release.
SHUTDOWN_DEADLINE_S = GRACE_S - PRESTOP_S - SHUTDOWN_MARGIN_S


def _log(result: str, message: str, *args: Any, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(
        level,
        message,
        *args,
        extra={"fields": {"event": "poller_lease", "result": result, **fields}},
    )


class SoleLeader:
    """The lease stand-in for a process without the store backend (KTD5): tst, local runs and every
    hermetic suite. Always the leader, touches no store and starts no thread, so the loop around it
    is byte for byte the loop before leases existed."""

    holder = None

    def tick(self) -> None:
        return None

    def held(self) -> bool:
        return True

    def role(self) -> tuple[str, float | None]:
        return "leader", None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def release(self) -> None:
        return None


class StoreLease:
    """The lease over a Firestore document, renewed on a timer and fenced by a local deadline."""

    def __init__(
        self,
        *,
        holder: str,
        acquire: Callable[..., Any],
        renew: Callable[..., Any],
        release: Callable[..., Any],
        heartbeat: Callable[[], float | None],
        on_acquire: Callable[[], None],
        on_lose: Callable[[], None],
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        renew_wait_s: float = RENEW_WAIT_S,
    ) -> None:
        self.holder = holder
        self._acquire = acquire
        self._renew = renew
        self._release = release
        self._heartbeat = heartbeat
        self._on_acquire = on_acquire
        self._on_lose = on_lose
        self._monotonic = monotonic
        self._wall = wall
        self._renew_wait_s = renew_wait_s
        self._lock = threading.Lock()
        # The tenure: a token while held, None otherwise. Renewals carry it, and a renewal whose
        # answer arrives after the tenure ended (demoted, released, re-acquired) changes nothing.
        self._token: str | None = None
        # The monotonic heartbeat the last successful acquire or renewal carried (see the module
        # docstring for why it is this and never the live heartbeat).
        self._anchor: float | None = None
        # Whether `on_acquire` ran for the current tenure, i.e. whether `tick` owes an `on_lose`.
        self._leading = False
        self._renewing = False
        self._backoff = 0
        self._last_refused_by: object = object()  # log a refusal once per holder seen
        self._stopped = threading.Event()
        self._timer: threading.Thread | None = None

    # -- the two clocks ------------------------------------------------------------------------

    def _beat_now(self) -> float:
        """The last heartbeat on the monotonic clock; now when the loop has not beaten yet."""
        beat = self._heartbeat()
        return self._monotonic() if beat is None else beat

    def _as_wall(self, beat: float) -> datetime:
        """A monotonic instant on the wall clock, for the store, which compares across pods."""
        return datetime.fromtimestamp(self._wall() - (self._monotonic() - beat), UTC)

    # -- what the loop and the fences ask ------------------------------------------------------

    def held(self) -> bool:
        """Does this process hold the lease right now? Pure: no store call, no callback."""
        with self._lock:
            return (
                self._token is not None
                and self._anchor is not None
                and self._monotonic() < self._anchor + TTL_S - MARGIN_S
            )

    def role(self) -> tuple[str, float | None]:
        """`(role, lease_age_s)` for `/ready`: the age is seconds since the heartbeat the lease was
        last written with, so an operator can see a leader whose renewals stopped landing."""
        if not self.held():
            return "follower", None
        with self._lock:
            anchor = self._anchor
        return "leader", None if anchor is None else round(self._monotonic() - anchor, 1)

    def tick(self) -> None:
        """One poller iteration's lease step, on the poller thread: notice a loss, else acquire.

        Never raises: a store failure leaves the pod a follower for this tick, and a failing
        `on_acquire` releases the lease and backs off :data:`BACKOFF_TICKS` ticks.
        """
        if self._leading and not self.held():
            with self._lock:
                self._token = None
                self._leading = False
            _log("lost", "Poller lease lost; this pod is a follower", level=logging.WARNING,
                 holder=self.holder)
            try:
                self._on_lose()
            except Exception:
                logger.exception("on_lose failed; the pod stays a follower")
        if self._leading:
            return
        if self._backoff > 0:
            self._backoff -= 1
            return
        beat = self._beat_now()
        try:
            answer = self._acquire(self.holder, heartbeat=self._as_wall(beat))
        except Exception:
            logger.exception(
                "Poller lease acquire failed; following this tick",
                extra={"fields": {"event": "poller_lease", "result": "error", "holder": self.holder}},
            )
            return
        if answer.status != "acquired":
            if answer.holder != self._last_refused_by:
                self._last_refused_by = answer.holder
                _log("refused", "Poller lease held by %s; following", answer.holder,
                     holder=self.holder, held_by=answer.holder)
            return
        self._last_refused_by = object()
        with self._lock:
            self._token = answer.token
            self._anchor = beat
        try:
            self._on_acquire()
        except Exception:
            logger.exception(
                "on_acquire failed; releasing the lease and following for %d ticks",
                BACKOFF_TICKS,
                extra={"fields": {"event": "poller_lease", "result": "acquire_failed",
                                  "holder": self.holder}},
            )
            self.release()
            self._backoff = BACKOFF_TICKS
            return
        # Leading even if a renewal answered `lost` during a slow `on_acquire`: `held()` is then
        # already False, so the loop follows this tick, and the next tick owes -- and runs --
        # `on_lose`, which undoes what `on_acquire` just did.
        self._leading = True
        _log("acquired", "Poller lease acquired; this pod runs the cycle", holder=self.holder)

    def release(self) -> str | None:
        """Hand the lease back (by holder alone, so it also works after a lost acknowledgement).
        Ends the tenure first, so nothing renews it while the release is in flight. Returns the
        store's answer, or None when the store call raised (logged)."""
        with self._lock:
            self._token = None
            self._leading = False
        try:
            answer = self._release(self.holder)
        except Exception:
            logger.exception(
                "Poller lease release failed; the successor waits out the TTL",
                extra={"fields": {"event": "poller_lease", "result": "release_failed",
                                  "holder": self.holder}},
            )
            return None
        _log(answer.status, "Poller lease release: %s", answer.status, holder=self.holder)
        return answer.status

    # -- the renewal timer ---------------------------------------------------------------------

    def renew_if_due(self) -> str:
        """The timer's body: renew while held and the heartbeat is fresh, at most one in flight.

        Returns what happened -- ``not_held``, ``stale`` (heartbeat past
        :data:`RENEW_WHILE_FRESH_S`), ``skipped`` (one already in flight; skipped, never queued),
        ``renewed``, ``lost``, ``error`` or ``pending`` (still running after the bounded wait; its
        answer is applied whenever it arrives, if the tenure it was made for is still current).
        """
        with self._lock:
            if self._token is None:
                return "not_held"
            if self._renewing:
                return "skipped"
            beat = self._beat_now()
            if self._monotonic() - beat >= RENEW_WHILE_FRESH_S:
                return "stale"
            self._renewing = True
            token = self._token
        outcome: list[str] = []
        done = threading.Event()

        def call() -> None:
            try:
                answer = self._renew(self.holder, token, heartbeat=self._as_wall(beat))
            except Exception:
                logger.exception(
                    "Poller lease renewal failed; the local deadline stands",
                    extra={"fields": {"event": "poller_lease", "result": "renew_error",
                                      "holder": self.holder}},
                )
                outcome.append("error")
            else:
                with self._lock:
                    if self._token == token:
                        if answer.status == "renewed":
                            # Never backwards: a slow renewal must not undo a later one.
                            self._anchor = max(self._anchor or beat, beat)
                        else:
                            self._token = None  # `tick` runs `on_lose` on the poller thread
                outcome.append(answer.status)
            finally:
                with self._lock:
                    self._renewing = False
                done.set()

        threading.Thread(target=call, name="poller-lease-renew", daemon=True).start()
        done.wait(self._renew_wait_s)
        return outcome[0] if outcome else "pending"

    def start(self) -> None:
        """Start the renewal timer. Idempotent."""
        if self._timer is not None:
            return
        self._timer = threading.Thread(target=self._run_timer, name="poller-lease", daemon=True)
        self._timer.start()

    def _run_timer(self) -> None:
        while not self._stopped.wait(RENEW_EVERY_S):
            try:
                self.renew_if_due()
            except Exception:  # the timer must outlive any one bad renewal
                logger.exception("Poller lease timer failed one period")

    def stop(self) -> None:
        self._stopped.set()

