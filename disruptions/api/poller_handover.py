"""How a poller tenure ends: the lease lost, or the process stopping (zero-downtime U2, KTD14).

`main` composes these into the lease (`holder=holder_id()`, `on_lose=on_lease_lost`) and its
lifespan's shutdown; `on_acquire` stays in `main`, which owns everything an acquisition touches
(`_force_rebuild`, `_rush_hydrate`, `_beat`, the store backend). Nothing here reads `main`, which
this module never imports, and `poller_lease` stays the stdlib-only state machine underneath.
"""

from __future__ import annotations

import logging
import os
import threading
from time import monotonic

import metrics
import notification_events
import notification_store
import poller_lease

logger = logging.getLogger(__name__)


def holder_id() -> str:
    """The lease holder: the pod name plus a per-process suffix, so a container restarted inside
    the same pod is a new holder and cannot renew its predecessor's tenure."""
    import secrets
    import socket

    return f"{os.environ.get('HOSTNAME') or socket.gethostname()}-{secrets.token_hex(3)}"


def on_lease_lost() -> None:
    """`on_lose` (KTD14), on the poller thread at the tick after the loss.

    Gauge first, so `sum(kix_poller_leader)` stops counting this pod at once. Then wait for the
    emit pass to drain -- its abandoned commits still apply to the subject mirror, which learns only
    from this process's own commits -- and only then reset both mirrors, so no straggler lands in a
    mirror this pod no longer owns. Bounded like the shutdown: past it the reset goes ahead, and the
    next acquisition resets again before it seeds.
    """
    metrics.POLLER_LEADER.set(0)
    if not notification_events.wait_until_idle(poller_lease.SHUTDOWN_DEADLINE_S):
        logger.warning(
            "Emit pass still running after the lease was lost; resetting the mirrors anyway",
            extra={"fields": {"event": "poller_lease", "result": "drain_timeout"}},
        )
    notification_store.reset_mirrors()


def shutdown_poller(
    thread: threading.Thread,
    stop_event: threading.Event,
    lease: poller_lease.SoleLeader | poller_lease.StoreLease,
    *,
    deadline_s: float | None = None,
) -> None:
    """The lifespan's shutdown, as one sequence under one deadline (KTD9/KTD14).

    Set stop, let the thread exit at its next boundary, join it up to the deadline, wait for the
    emit pass lock up to what is left, release the lease unless the thread is still alive, clear
    the gauge. A thread still alive is mid-phase -- a BigQuery job may yet replace a table -- so the
    lease is NOT released and the successor waits out the TTL instead; a pass lock still held at
    the deadline is released anyway, because its leftover commits are idempotent (KTD7).

    Under `SoleLeader` there is no successor to protect, so the join keeps its old 5 s cap and
    nothing else runs.
    """
    stop_event.set()
    if isinstance(lease, poller_lease.SoleLeader):
        thread.join(timeout=5)
        return
    deadline = monotonic() + (poller_lease.SHUTDOWN_DEADLINE_S if deadline_s is None else deadline_s)
    # The renewal timer keeps running through the join: a thread finishing a long query is still
    # the leader, and a lease lapsing under it would let a follower in beside it.
    thread.join(timeout=max(0.0, deadline - monotonic()))
    lease.stop()
    if not notification_events.wait_until_idle(max(0.0, deadline - monotonic())):
        logger.warning(
            "Emit pass still running at the shutdown deadline; releasing the lease anyway",
            extra={"fields": {"event": "poller_lease", "result": "drain_timeout"}},
        )
    if thread.is_alive():
        logger.warning(
            "Poller thread still running at the shutdown deadline; leaving the lease to lapse",
            extra={"fields": {"event": "poller_lease", "result": "release_skipped"}},
        )
    else:
        lease.release()
    metrics.POLLER_LEADER.set(0)
