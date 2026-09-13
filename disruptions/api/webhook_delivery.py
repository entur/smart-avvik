"""One bounded attempt per pending notification event, on the poller's own thread (KTD6, R9, R12, R18).

The last step of the poller cycle and the first outbound HTTP in it with a **client-supplied
host**. ``varsel.py``'s Web Push comment records the same threat model -- an unresponsive host with
no timeout hangs the poller thread and stops ingestion for every other feature -- and everything
below is one of the five bounds KTD6 names against it:

1. **One attempt per pending event per cycle.** The next cycle is the retry, as it is everywhere
   else in this repo, so there is no outbox lease and no fence token. What makes that safe is a
   single writer, and the mechanism is nameable rather than assumed: one poller thread calls this,
   no route invokes it, and the deployment is one replica under the Recreate strategy, so two
   cycles cannot overlap.
2. **A 4-wide pool** -- ``/meldinger``'s width, the repo's narrowest. ``varsel``'s own pools are
   16/16/32 and are the wrong precedent: those run on a cadence nobody triggers, against endpoints
   Entur or a push service owns.
3. **5 s total and 2 s connect per attempt**, ``webhook_sender.deliver``'s own defaults.
4. **A 20 s deadline that RESERVES its worst case**, in ``varsel._WriteBudget``'s shape -- see
   :class:`_DeliveryBudget`.
5. **A destination is skipped for the rest of the cycle after its first failure**, in PASS-LOCAL
   state. Module-level and swept it would pause a destination permanently, which is
   indistinguishable from the intended 410 pause; pass-local, it is exactly "this endpoint is
   unwell right now" and the next cycle asks again.

**And a sixth bound the five do not cover.** ``socket.settimeout`` is per-recv, not a total
deadline, and ``http.client`` reads response headers a line at a time, so a client drip-feeding
one byte per second can hold a worker for several multiples of the read timeout --
``webhook_sender``'s docstring states that and delegates the containment here. ``notification_store``
bounds every READ with ``_CALL_DEADLINE_S`` but a transactional COMMIT carries no timeout the Python
client can express, so a hung store WRITE still has no ceiling of its own. Both are contained the
same way: every task is submitted to a pool and the pool is awaited
for :data:`_DELIVERY_WAIT_S`, then abandoned with ``shutdown(wait=False)``. The reservation budget
is what bounds the *ordinary* pass; the bounded wait is what bounds a pass with something hung in
it. They are different failures and neither one catches both.

**"Every task" includes the stale sweep, and did not until 2026-09-09.** ``_expire_stale`` is a loop
of ``mark_delivery_expired`` commits and it ran on the CALLING thread, before the delivery pool was
created -- outside the only bound that can contain a commit. A store call that hangs on a poller
thread outside a bounded wait is not a slow cycle, it is the end of ingestion: the web server's
request threads are separate, so nothing crashes and no liveness probe fires.

**Dedup is on success, exactly as /varsel's is.** An attempt that errors leaves no mark and is
re-offered next cycle, and R11's daily cap slot is spent inside the transaction that records the
success -- otherwise a dead destination burns a subscription's four for the Oslo day with nothing
delivered. All of that lives in ``notification_store``; this module classifies and reports.

**What this module does not do.** It never re-derives or re-sorts the pending set
(``notification_store.pending_deliveries`` already separates ``ready``, ``stale`` and ``throttled`` and
orders ``ready`` by validity), it writes nothing to the store itself, and it takes no Firestore call
while ``varsel._state_lock`` is held -- it never touches that lock at all, which is the strongest
form of ``_prepare_alert``'s rule.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol

import notification_auth
import notification_store
import reserved_budget
import webhook_sender
import webhook_signing

logger = logging.getLogger(__name__)

# ---- Bounds ------------------------------------------------------------------------------------

#: KTD6's number, and not ours to move: "reserves its worst case -- the attempt plus the durable
#: `sent` write that follows it -- against a 20 s deadline".
_DELIVERY_DEADLINE_S = 20.0
#: The backstop for something hung, and deliberately the SAME number: the reservation already makes
#: the deadline a ceiling on when the pass ends, so this only ever fires when a worker is stuck
#: somewhere the reservation cannot see. A task abandoned here may still land its POST without
#: recording it, which the next cycle re-sends -- at-least-once is the published contract (R12).
_DELIVERY_WAIT_S = 20.0
#: One bounded Firestore call, as a budgeting assumption. Three of them are reserved per attempt:
#: `claim_delivery`, the event read, and the durable `sent`/`failed` write. A transaction the client
#: retries five times can exceed this, which is what the bounded wait above is for.
_STORE_CALL_S = 2.0
#: `/meldinger`'s width. See the module docstring.
_WORKERS = 4
#: What one cycle may attempt, on top of the clock -- `notification_store`'s own per-cycle ceiling, so
#: the query's `limit` and this budget's count cannot disagree.
_MAX_ATTEMPTS_PER_CYCLE = notification_store.MAX_DELIVERY_ATTEMPTS_PER_CYCLE


def _worst_case_s(timeout_s: float, store_call_s: float) -> float:
    """The whole attempt's worst case: the outbound call plus the three store calls around it."""
    return timeout_s + 3 * store_call_s


class Sender(Protocol):
    """``webhook_sender.deliver``'s shape, narrowed to what this module passes.

    A Protocol for the same reason ``varsel.ImpactProvider`` is one: it is a cross-module contract
    whose drift is quiet, and the suites replace it wholesale. ``webhook_sender.deliver`` raises
    ``NetworkBlocked`` from a test process that forgot to, so a missing stub is loud rather than a
    request to a client-supplied host from CI.
    """

    def __call__(
        self,
        url: str,
        *,
        body: bytes,
        headers: dict[str, str],
        timeout_s: float,
        connect_timeout_s: float,
        max_in_flight: int,
    ) -> webhook_sender.DeliveryAttempt: ...


@dataclass(frozen=True)
class DeliveryReport:
    """What one delivery pass did, as counts.

    ``skipped_destinations`` is the count of destinations that stopped being attempted this cycle,
    which with ``failed`` is what tells "one endpoint is unwell" from "every endpoint is".

    **Two grains, and mixing them made a report field depend on thread interleaving.** Every
    counter here except ``paused`` and ``skipped_destinations`` is per EVENT -- an invariant of the
    work, so N pending events against a gone endpoint is N of them. Those two are per DESTINATION,
    counted off sets. ``paused`` used to be incremented per attempt that answered 410, so two
    events against one gone endpoint reported ``paused=2`` when the workers overlapped and
    ``paused=1`` when they did not: the second worker's `_Skips` check races the first worker's
    write, and the pool is four wide. Nothing was broken -- `notification_auth.pause_destination` is
    idempotent -- but a field whose value the scheduler decides is a bad operational signal, and
    this record is what a human reads during an incident. A 410 event is now counted as
    ``cancelled``, which is the state its record was just given, and ``paused`` answers the
    different question of how many destinations an operator has to resume.

    ``capped`` and ``deadline`` are split for the same reason. ``capped`` is R11's daily allowance
    refusing a notification -- a product decision, which the store records durably as a suppression
    (``capped_at``/``capped_count``) and which KTD12 needs rows for. ``deadline`` is this pass
    running out of clock or attempts, which nothing records anywhere and which the next cycle
    simply re-offers. Counted together, AE3's expected nine suppressions could silently absorb a
    deadline bind.

    ``sent`` counts FRESH successes only -- one notification the client did not already have, one
    of R11's four slots spent. Two counters exist beside it because a late arrival writes nothing
    and must not be filed as a send. The pass abandons an attempt's thread when its bounded wait
    expires (it cannot kill a thread parked in a socket read); the next cycle legitimately re-sends
    the same still-pending event; and the abandoned thread then returns and asks the store to
    record a success that has already been recorded. ``notification_store`` now refuses that write and
    answers ``already_resolved`` -- so this side has to stop counting it, or the log record U8
    publishes as operational telemetry over-reports deliveries by however many threads were
    abandoned.

    ``superseded`` is split out from ``already_resolved`` rather than folded into it because it
    means something different to a reader: a newer revision replaced the delivery, so nothing was
    recorded here AND the replacement spends its own slot. ``already_resolved`` is every other
    terminal state the record turned out to hold -- already ``sent``, ``cancelled``, ``expired``,
    ``failed``. All three are mutually exclusive with ``sent`` and with each other.

    Both counters can also rise on an attempt that FAILED at the socket, which is deliberate and
    would otherwise surprise a reader: the store refuses a terminal failure write to a resolved
    record too, so nothing was written and nothing will be retried, and counting that ``retryable``
    would be a false promise. The destination is still skipped for the rest of the cycle -- the
    endpoint really did misbehave.
    """

    ready: int = 0
    sent: int = 0
    already_resolved: int = 0
    superseded: int = 0
    deadline: int = 0
    failed: int = 0
    retryable: int = 0
    expired: int = 0
    cancelled: int = 0
    capped: int = 0
    gone: int = 0
    not_deliverable: int = 0
    paused: int = 0
    #: Attempts whose lease had lapsed and been re-claimed by the time the terminal write arrived
    #: (U8). Its own counter rather than folded into `already_resolved` because it means the
    #: opposite: the delivery is not finished, it is in somebody else's hands.
    #:
    #: **Reachable since U11** -- it was not while delivery had one writer. A non-zero count during
    #: the rollout window, when the poller and the worker both deliver, is the lease doing its job.
    #: A steady one afterwards means attempts are outliving their 20 s lease, i.e. the store or the
    #: receiver is slower than `DELIVERY_LEASE_S`'s arithmetic assumes.
    lease_lost: int = 0
    #: Claims refused because another worker already held the delivery. Reachable since U11 too, and
    #: the cheaper of the two: nothing was attempted, so the delivery is simply somebody else's.
    leased: int = 0
    #: Attempts not made because the destination is inside U7's durable backoff. Its own counter and
    #: not folded into `skipped`, because the two are different operator answers: `skipped` is "a
    #: sibling attempt in this pass took it out", this is "we are waiting exactly as long as the
    #: receiver asked". A cycle that is all `backed_off` is the system behaving.
    backed_off: int = 0
    skipped: int = 0
    skipped_destinations: int = 0
    store_errors: int = 0
    #: Seconds since the OLDEST deliverable record in this pass was minted (U11) -- the series the
    #: worker's backlog alert selects on, and 0.0 when nothing was ready.
    #:
    #: It measures the oldest DELIVERABLE row, not the oldest row in the collection: `ready`
    #: excludes what a lease holds, what U7's durable backoff is waiting on, and everything past the
    #: per-destination ceiling or the scan bound. So a single hung destination cannot drive it,
    #: which is the point -- what it answers is "is the queue draining", and `throttled` and
    #: `truncated` beside it answer "is a destination unwell". A delivery minted before U11 carries
    #: no `created_at` and contributes nothing, so the gauge climbs from zero on the deploy that
    #: introduces it instead of reporting the whole backlog as infinitely old.
    backlog_age_s: float = 0.0
    truncated: bool = False
    throttled: tuple[str, ...] = ()
    timed_out: bool = False
    ms: int = 0


class _DeliveryBudget(reserved_budget.ReservedBudget):
    """This pass's ceiling on attempts, claimed from four threads at once.

    The mechanism is :class:`reserved_budget.ReservedBudget`'s -- including why the claim belongs
    inside the worker as its task starts rather than at submit time, which is the one line here
    that is easy to move and fatal to move. What is this pass's own is what the reservation covers:
    the attempt PLUS the durable write that follows it, because a delivery that is not recorded is
    a delivery that repeats.
    """


class _Skips:
    """Which destinations are out for the rest of THIS pass, and the destination records read once.

    Pass-local, and KTD6 calls it out as the deliberate exception to the rule that every new ledger
    is registered in ``check_and_notify``'s stale sweep and ``test_varsel_api._reset_state``: made
    module-level and swept, one failure would pause a destination permanently, which is
    indistinguishable from the intended 410 pause. Created per call to :func:`deliver_pending` and
    dropped with it.

    The record cache is here too rather than beside it, because the two are read under one lock and
    are about the same question -- "may we dial this destination right now" -- and a destination that
    is not deliverable is added to the skip set by the same code that discovered it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._out: set[str] = set()
        self._paused: set[str] = set()
        self._backed_off: set[str] = set()
        self._recovered: set[str] = set()
        self._records: dict[str, notification_auth.Destination | None] = {}

    def is_out(self, destination_id: str) -> bool:
        with self._lock:
            return destination_id in self._out

    def drop(self, destination_id: str) -> None:
        with self._lock:
            self._out.add(destination_id)

    def count(self) -> int:
        with self._lock:
            return len(self._out)

    def pause_recorded(self, destination_id: str) -> None:
        """Note that this destination was successfully paused during this pass.

        A SET, so the report says how many destinations were paused rather than how many attempts
        happened to call `pause_destination` -- see :class:`DeliveryReport`. Recorded only on
        success, so the number means "an operator now has this to resume", which is what makes it
        worth a human's attention during an incident.
        """
        with self._lock:
            self._paused.add(destination_id)

    def paused_count(self) -> int:
        with self._lock:
            return len(self._paused)

    def claim_backoff(self, destination_id: str) -> bool:
        """First failure for this destination in this pass? Then it writes the backoff.

        A SET for `pause_recorded`'s reason one layer along: the durable backoff is per destination
        and per failure, not per attempt, and several workers can be inside one destination's
        failures at the same instant. Without the claim a hung endpoint holding eight workers writes
        eight times to one document and doubles the delay eight times for what is one round of
        failure -- turning a 30 s first backoff into an immediate cap.
        """
        with self._lock:
            if destination_id in self._backed_off:
                return False
            self._backed_off.add(destination_id)
            return True

    def claim_recovery(self, destination_id: str) -> bool:
        """First success for a destination whose CACHED record carried a backoff.

        The read is pass-local and the write is therefore at most one per destination per pass,
        which is the whole point: twenty thousand POSTs to a healthy endpoint must not be twenty
        thousand writes to its document. A destination with nothing to clear never reaches here --
        the caller tests the cached record first -- so the ordinary healthy pass writes nothing at
        all.
        """
        with self._lock:
            if destination_id in self._recovered:
                return False
            self._recovered.add(destination_id)
            return True

    def destination(self, destination_id: str) -> notification_auth.Destination | None:
        """The destination record, read at most once per pass.

        Cached because R12's pause and the signing generations are read per attempt and a
        destination holds many pending events on a bad morning -- one read per destination per cycle
        rather than one per event. The value is cached even when it is ``None``, so a destination
        record that has gone missing costs one read rather than one per pending event.
        """
        with self._lock:
            if destination_id in self._records:
                return self._records[destination_id]
        record = notification_auth.destination_for_delivery(destination_id)
        with self._lock:
            self._records[destination_id] = record
        return record


def event_body(event: dict[str, Any]) -> bytes:
    """The event as the exact bytes that are signed and sent.

    **Public, because there are two producers of a client-bound body and only one may decide what
    the bytes look like.** The other is ``/notification-api/v1/test-events``, whose synthetic event
    is signed and dialled on a request path; it builds a stored-shaped event and hands it to this
    function and to :func:`wire_event`, so a client writing a client against a test event is
    reading exactly the serialization a real one arrives in -- key order, UTF-8 and timestamp
    rendering included.

    Serialized ONCE and handed unchanged to ``webhook_signing.sign`` and to the transport.
    Re-serializing between signing and sending is the canonical webhook bug -- every provider's
    documentation carries the warning -- and the signature would then cover a document the receiver
    did not get.

    ``ensure_ascii=False`` because the wording is Norwegian and the signature is over BYTES: the
    npm verifier hashes ``update(str)`` as UTF-8, and the vectors file records a UTF-8/UTF-16 length
    disagreement as a live failure mode rather than a hypothetical. ``default=str`` renders the
    timestamps Firestore hands back as datetimes; ``sort_keys`` keeps two attempts at one event
    byte-identical, which is what makes a receiver's own body comparison meaningful.
    """
    return json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default
    ).encode("utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


# The wire envelope is EXACTLY the published schema's top-level property set, in its declaration
# order, and the projection below is an ALLOW-LIST rather than a deny-list. Two independent reasons,
# and the first is not hypothetical: `contracts/notifications/v1/event.schema.json` is
# `additionalProperties: false`, so a field the store holds and the schema does not names a body a
# conforming receiver REFUSES -- and the stored event legitimately holds three of them.
# `client_id` and `destination_id` are our own routing, recorded so a delivery can be traced back
# to the destination it was authorised by; `expires_at` is the store's TTL field, a retention
# decision and never validity, which on the wire would invite a receiver to read it as one. Sending
# any of the three costs every event for every client running the verifier U7 ships. The second
# reason is direction of failure: a deny-list fails OPEN, so the next field added to
# `notification_store._mint` would reach every client unreviewed, and KTD10's boundary would rest on
# whoever edits the store remembering this module exists.
# `test_the_wire_envelope_is_exactly_the_published_schemas_properties` pins this tuple against the
# schema file itself, so the two cannot drift in either direction: a schema property missing here
# would be omitted from a body that declares it required, which fails at the receiver rather than
# here.
_WIRE_FIELDS = (
    "schema_version",
    "event_id",
    "type",
    "phase",
    "alert_id",
    "revision",
    "occurred_at",
    "valid_until",
    "subscription_id",
    "subscription_version",
    "external_ref",
    "test",
    "operator",
    "line",
    "journey_ref",
    "call_date",
    "stop_place",
    "stop_order",
    "situations",
    "payload",
)


def wire_event(event: dict[str, Any]) -> dict[str, Any]:
    """The stored event projected onto the published envelope -- see ``_WIRE_FIELDS`` above.

    Public for :func:`event_body`'s reason: the test-event route projects through this too, so the
    allow-list decides what leaves this process on BOTH paths rather than on the one the poller
    drives.

    A key the store has not written is left out rather than sent as null: every optional property is
    optional in the schema too, and a null where a receiver expects a string is a parse failure it
    cannot distinguish from corruption.
    """
    return {key: event[key] for key in _WIRE_FIELDS if key in event}


def _attempt(
    delivery: notification_store.Delivery,
    *,
    skips: _Skips,
    budget: _DeliveryBudget,
    sender: Sender,
    timeout_s: float,
    connect_timeout_s: float,
    now: datetime,
) -> str:
    """One pending delivery: recheck, sign, send once, record. Returns the outcome's count key.

    Never raises. A raise here would reach ``pool.map``'s consumer and take the whole pass with it,
    which is the failure this repo has already had once in ``check_and_notify``'s pass 1a.
    """
    if skips.is_out(delivery.destination_id):
        return "skipped"
    # INSIDE the worker, as the task starts -- see `_DeliveryBudget`.
    if not budget.claim():
        # `deadline`, NOT `capped`: R11's cap is a product decision the store records durably as a
        # suppression, and this is the pass running out of clock or attempts, which nothing records
        # anywhere. One counter for both let AE3's `capped == 9` absorb a deadline bind silently --
        # the same "one number, two meanings" the `paused` counter was just fixed for.
        return "deadline"
    # The one store read `_attempt` used to make OUTSIDE `_store_step`: a raise here reached the
    # caller's outer guard with a poorer log line and no classification. Wrapped in a tuple for
    # `get_event`'s reason -- `None` is a legitimate answer ("no such record") that must resolve
    # the delivery, while a raise is a fact about the store and must not. Nothing is cached on a
    # raise (`_Skips.destination` records only a completed read), so the next attempt re-asks.
    looked_up = _store_step(
        "destination", delivery, lambda: (skips.destination(delivery.destination_id),)
    )
    if looked_up is None:
        return "store_errors"
    (destination,) = looked_up
    if destination is None or not destination.deliverable:
        # R12's pause, read before the attempt rather than rediscovered by it. The delivery stays
        # `pending` and expires at its own validity: a paused destination is an operator's to
        # resume, and writing a terminal state here would throw the event away on their behalf.
        skips.drop(delivery.destination_id)
        return "not_deliverable"
    if not destination.ready_to_dial(now):
        # U7's durable backoff: this destination failed retryably on an earlier pass and asked for,
        # or earned, a stretch of seconds. Its own outcome rather than `skipped`, because `skipped`
        # means "a sibling attempt in THIS pass took it out" and an operator reading a cycle where
        # nothing was attempted needs to tell "we are waiting as asked" from "it broke just now".
        # The delivery stays `pending` and is re-offered next cycle exactly as a skip is.
        skips.drop(delivery.destination_id)
        return "backed_off"
    claim = _store_step(
        "claim", delivery, lambda: notification_store.claim_delivery(delivery.delivery_id, now=now)
    )
    if claim is None:
        return "store_errors"
    if claim.status != "ready" or claim.delivery is None:
        # The store has already written whatever terminal state it decided (`expired`, `cancelled`)
        # or recorded the suppression (`capped`, left pending and re-offered) -- R13's recheck is
        # durable precisely so nothing re-offers a delivery whose subscription is gone.
        return claim.status

    read = _store_step(
        "event",
        delivery,
        # Wrapped in a tuple so "the read failed" and "there is no such event" stay distinguishable:
        # `get_event` answers `None` for an event past its RETENTION window, which is a fact about
        # the store's contents and must resolve the delivery, while a raise is a fact about the store
        # and must not.
        lambda: (notification_store.get_event(claim.delivery.event_id, now=now),),
    )
    if read is None:
        return "store_errors"
    event = read[0]
    if event is None or not notification_store.event_is_valid(event, now):
        # R9: past its validity a delivery is EXPIRED, not failed. "We ran out of time to tell you"
        # and "your endpoint rejected us" are different operator problems, and collapsing them
        # would hide a dead destination inside ordinary expiry.
        if _store_step(
            "expire",
            delivery,
            lambda: notification_store.mark_delivery_expired(delivery.delivery_id, now=now),
        ) is None:
            return "store_errors"
        return "expired"

    body = event_body(wire_event(event))
    try:
        signed = webhook_signing.sign(
            destination_id=destination.destination_id,
            generations=destination.signing_generations,
            # The `webhook-id` IS the delivery id and is stable across attempts, because the
            # receiver dedupes on it. The timestamp and the signature are re-minted per attempt by
            # `sign` itself: a retry reusing the first attempt's timestamp lands outside the
            # receiver's five-minute replay window, where a CORRECT receiver drops our own retry as
            # an attack.
            webhook_id=delivery.delivery_id,
            body=body,
        )
    except Exception as exc:
        # An unconfigured signing root is not a delivery outcome and must not be recorded as one:
        # every destination would fail together, and marking each event failed would burn a
        # subscription's attempts on a missing environment variable.
        logger.warning(
            "Notification event could not be signed; leaving it pending (%s)",
            type(exc).__name__,
            exc_info=True,
            extra={"fields": {"event": "webhook_delivery", "result": "unsigned",
                              "destination_id": delivery.destination_id}},
        )
        skips.drop(delivery.destination_id)
        return "store_errors"

    attempt = sender(
        destination.callback_url,
        body=signed.body,
        headers=signed.headers,
        timeout_s=timeout_s,
        connect_timeout_s=connect_timeout_s,
        # The client's own `concurrent_deliveries`, which bounds how many requests this process has
        # in flight against that host at once (U8). Per worker REPLICA rather than per client, which
        # is what the quota's own comment says and why it is the only one here that is not a
        # property of the account alone.
        max_in_flight=notification_auth.quotas_for(delivery.client_id).concurrent_deliveries,
    )
    if attempt.ok:
        # The client HAS the event; if this write fails, only our record of it is missing.
        # Recording nothing is the right direction -- the next cycle re-sends and the receiver's own
        # inbox suppresses the duplicate, which is the contract (R12, at-least-once). Marking it
        # failed would be a lie about what happened.
        outcome = _store_step(
            "sent",
            delivery,
            lambda: notification_store.mark_delivery_sent(
                delivery.delivery_id, token=claim.token, now=now
            ),
        )
        if outcome is None:
            return "store_errors"
        if outcome.lease_lost:
            # Our lease lapsed and another worker re-claimed the delivery while this attempt was in
            # flight. The client HAS the event -- but the record is now somebody else's to resolve,
            # and writing over it would clear a hold that live holder is still using. Counted
            # separately from `already_resolved`, which means the delivery is finished.
            return "lease_lost"
        # RECOVERY, and it costs a write only when there is something to clear. The test is on the
        # pass-local CACHED record, so a healthy destination -- the ordinary case, and the one that
        # carries twenty thousand POSTs -- never reaches the store at all here.
        if (
            destination.consecutive_failures or destination.retry_not_before is not None
        ) and skips.claim_recovery(delivery.destination_id):
            try:
                notification_auth.clear_destination_backoff(delivery.destination_id, now=now)
            except Exception:
                # Never a delivery outcome: the event went out. A backoff that outlives its cause
                # costs at most one cycle's attempts against a destination that is demonstrably
                # healthy, and the next success clears it.
                logger.warning(
                    "Could not clear a recovered destination's backoff; it lapses on its own",
                    exc_info=True,
                    extra={"fields": {"event": "webhook_delivery", "result": "backoff_clear_failed",
                                      "destination_id": delivery.destination_id}},
                )
        # The write is idempotent and says so. A late arrival from a thread the previous cycle
        # abandoned found the delivery already resolved, wrote nothing, and spent no slot -- so it
        # is not a send, however successful the HTTP call it just made was. See `DeliveryReport`.
        return _resolved_kind(outcome) or "sent"

    if attempt.pause_destination:
        # R12: a 410 pauses the DESTINATION until an operator resumes it. Idempotent in
        # `notification_auth`, so a backlog against a gone endpoint cannot fill the audit collection --
        # and it is still called per attempt rather than claimed once under the lock, so that a
        # pause which FAILS can be retried by the next of this cycle's attempts instead of being
        # swallowed by a claim nobody releases.
        try:
            notification_auth.pause_destination(
                delivery.destination_id, reason=f"delivery {attempt.error or 'gone'}"
            )
            skips.pause_recorded(delivery.destination_id)
        except Exception:
            logger.warning(
                "Could not pause a destination that answered 410; it is re-attempted next cycle",
                exc_info=True,
                extra={"fields": {"event": "webhook_delivery", "result": "pause_failed",
                                  "destination_id": delivery.destination_id}},
            )
    failure = _store_step(
        "failed",
        delivery,
        lambda: notification_store.mark_delivery_failed(
            delivery.delivery_id,
            error=attempt.error or "unknown",
            retryable=attempt.retryable,
            retry_after_s=attempt.retry_after_s,
            token=claim.token,
            # `cancelled` for a gone destination, `failed` otherwise -- both are pinned as members
            # of `notification_store.DELIVERY_STATES` by U5's suite, so they pass straight through.
            terminal_state=attempt.terminal_state or "failed",
            now=now,
        ),
    )
    if failure is None:
        return "store_errors"
    # FIRST failure, and the destination is out for the rest of the cycle. What that protects is not
    # tidiness: `http.client`'s read timeout is per-recv, so N pending events against one hung
    # endpoint is N multiples of it on the poller's own thread.
    skips.drop(delivery.destination_id)
    # And, since U7, out for a stretch that OUTLIVES the pass. Only a retryable failure -- a 400 is
    # about the event we sent and says nothing about the host, and a 410 is R12's pause, which is
    # terminal and not a delay. `claim_backoff` makes this one write per destination per pass, or a
    # hung endpoint holding eight workers would double the delay eight times for one round of
    # failure and reach the cap immediately.
    if attempt.retryable and skips.claim_backoff(delivery.destination_id):
        try:
            notification_auth.back_off_destination(
                delivery.destination_id, retry_after_s=attempt.retry_after_s, now=now
            )
        except Exception:
            # Not a delivery outcome either: the per-delivery `retry_not_before` the store just
            # wrote still holds, and the pass-local skip still holds. What is lost is only the part
            # that survives the pass.
            logger.warning(
                "Could not record a destination's backoff; only this pass's skip applies",
                exc_info=True,
                extra={"fields": {"event": "webhook_delivery", "result": "backoff_write_failed",
                                  "destination_id": delivery.destination_id}},
            )
    # The endpoint really did misbehave, so the destination is out for the rest of the cycle either
    # way -- but `retryable` would be a false promise for a record another pass has already
    # resolved: nothing was written and nothing will be retried. This is the failure half of the
    # same late-arrival race as the success branch, and `_failed_body` guards against it for a
    # sharper reason: unguarded it set an already-`sent` delivery back to `pending`, so the next
    # cycle sent the client a second copy.
    if failure.lease_lost:
        return "lease_lost"
    resolved = _resolved_kind(failure)
    if resolved is not None:
        return resolved
    if attempt.pause_destination:
        # The EVENT's outcome, which is what the per-attempt counters are about: `cancelled` is the
        # state just written to its record, so the report and the store agree. How many
        # DESTINATIONS were paused is a different question and is counted off a set.
        return "cancelled"
    return "retryable" if attempt.retryable else "failed"


def _store_step[T](step: str, delivery: notification_store.Delivery, call: Callable[[], T]) -> T | None:
    """Run one store call, or log and classify its failure and answer ``None``.

    A wrapper rather than six ``try/except`` blocks, and the reason is what it does NOT do: nothing
    is written on a failure, in either classification, which is what makes a store outage cost a
    cycle rather than a subscription. ``notification_store.is_retryable`` decides only what the log
    line says -- and it is the right classifier rather than a handler keyed on
    ``google.api_core`` exceptions, because an exhausted transaction surfaces as a plain
    ``ValueError`` after five attempts, which such a handler would call terminal, mark the subject
    failed for, and never deliver.

    ``None`` is unambiguous here because no store call this module makes returns ``None`` on
    success: the two that could (``get_event``, ``destination_for_delivery``) are wrapped in a
    one-tuple by their callers, so "the read failed" and "there is no such record" stay
    distinguishable, and the rest return a dataclass.
    """
    try:
        return call()
    except Exception as exc:
        logger.warning(
            "Webhook delivery store step %s failed (%s); the delivery is re-offered next cycle",
            step,
            "retryable" if notification_store.is_retryable(exc) else "terminal",
            exc_info=True,
            extra={
                "fields": {
                    "event": "webhook_delivery",
                    "result": "store_error",
                    "step": step,
                    "destination_id": delivery.destination_id,
                }
            },
        )
        return None


class _Counts:
    """The pass's outcome tally, written by the workers as they go.

    Not the futures' return values, and that is the point: with one future per DELIVERY an
    unfinished future was one unattempted delivery, but a shard future holds a whole
    subscription's deliveries, so collecting counts at the end would discard everything a
    still-running shard had already completed when the bounded wait expired. Under a timeout that
    is exactly when the numbers are worth having.
    """

    def __init__(self, **initial: int) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = dict(initial)

    def add(self, key: str) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def get(self, key: str) -> int:
        with self._lock:
            return self._counts.get(key, 0)


def _shard_by_subscription(
    ready: Sequence[notification_store.Delivery],
) -> list[tuple[notification_store.Delivery, ...]]:
    """One shard per subscription, each keeping the store's order, shards in soonest-first order.

    **Why the unit of parallelism is a SUBSCRIPTION and not a delivery.** `claim_delivery` reserves
    R11's slot on the subscription document in the transaction that admits the delivery -- which is
    what closed the cap overspend -- so two concurrent claims for one subscription contend on one
    document. Firestore's pessimistic concurrency serialises them, and past its retries the loser
    surfaces as an exhausted transaction: measured on the emulator, 8 concurrent claims on one
    subscription admitted 0-3 and turned the rest into retryable store errors with ZERO `capped`.
    That lands on precisely the day the cap matters -- AE3's line suspension IS thirteen pending
    deliveries on one subscription -- so a busy cycle would report a Firestore fault instead of the
    suppressions the plan says to record, and delay real notifications by a pass period (~5 min).

    Sharding removes the contention by construction rather than mitigating it: no two workers ever
    touch one subscription document, because one worker owns the subscription. The pool still
    parallelises across subscriptions, which is where a 200-deep backlog's breadth actually is.
    Serialising globally was the alternative and remains wrong for the reason this module already
    gave: a claim is a store call, and sequential claims for a 200-deep backlog are what the pool
    exists to avoid.

    Two things it costs, both accepted. Soonest-first becomes exact WITHIN a subscription -- the
    grain R11 is defined at -- and per-shard across them; shards are ordered by their soonest
    delivery so the pool still starts the most urgent first. And one subscription with a deep
    backlog holds a worker for its whole shard, which the cap bounds the usefulness of and the
    deadline reservation bounds the length of.
    """
    shards: dict[str, list[notification_store.Delivery]] = {}
    for delivery in ready:
        # `ready` is already in the store's soonest-first order, so appending preserves it inside
        # each shard and the first key inserted is the soonest subscription.
        shards.setdefault(delivery.subscription_id, []).append(delivery)
    return [tuple(group) for group in shards.values()]


def _deliver_shard(
    deliveries: Sequence[notification_store.Delivery],
    *,
    counts: _Counts,
    skips: _Skips,
    budget: _DeliveryBudget,
    sender: Sender,
    timeout_s: float,
    connect_timeout_s: float,
    now: datetime,
) -> None:
    """One subscription's pending deliveries, in order, on one worker. Never raises."""
    for delivery in deliveries:
        try:
            key = _attempt(
                delivery,
                skips=skips,
                budget=budget,
                sender=sender,
                timeout_s=timeout_s,
                connect_timeout_s=connect_timeout_s,
                now=now,
            )
        except Exception:
            # `_attempt` documents itself as never raising; this is the belt, because a raise here
            # would abandon the REST of the shard rather than one delivery.
            logger.warning(
                "A webhook delivery attempt raised; it is re-offered next cycle",
                exc_info=True,
                extra={"fields": {"event": "webhook_delivery", "result": "error"}},
            )
            key = "store_errors"
        counts.add(key)


def _resolved_kind(outcome: Any) -> str | None:
    """Which counter a terminal write belongs to, or ``None`` for an ordinary fresh one.

    ``notification_store`` refuses a terminal write to a delivery that is no longer ``pending`` and
    answers ``already_resolved=True`` with the state the record actually holds. That is not an
    error -- the client did receive the event, and raising would log a store failure and read as
    an outage -- but it is not a fresh delivery either, so it gets its own counter.

    ``superseded`` is separated because the slot accounting differs: a newer revision replaced this
    delivery and spends R11's slot itself, where every other resolved state means the slot question
    was settled when the record first reached it.

    ``getattr`` rather than attribute access, deliberately: the field is additive on
    ``DeliveryOutcome`` and this keeps the module working against a store that predates it -- the
    same defensive read `CallPred` uses for the SIRI boarding activities, and for the same reason,
    which is the cycle immediately after a deploy where one side is newer than the other.
    """
    if not getattr(outcome, "already_resolved", False):
        return None
    return "superseded" if getattr(outcome, "state", "") == "superseded" else "already_resolved"


def _expire_stale(stale: tuple[str, ...], *, now: datetime, budget: _DeliveryBudget) -> tuple[int, int]:
    """Resolve the deliveries whose validity has passed. Returns (expired, store_errors).

    Handed back by ``pending_deliveries`` rather than silently dropped, because a pending document
    nothing ever resolves is a document the delivery query keeps returning for its whole seven-day
    retention. Charged against the same budget as an attempt: these are store writes on the poller's
    thread and a backlog of them is exactly as expensive as a backlog of anything else.

    **Runs on a worker, never on the calling thread** -- see :func:`deliver_pending`. The budget
    bounds the COUNT of these writes and when the last one may start; it cannot bound how long one
    takes, because ``mark_delivery_expired`` is a transactional commit and a commit carries no
    timeout the Python client can express. Only the bounded wait can, and it only reaches what is
    submitted to a pool.
    """
    expired = errors = 0
    for delivery_id in stale:
        if not budget.claim():
            break
        try:
            notification_store.mark_delivery_expired(delivery_id, now=now)
            expired += 1
        except Exception:
            logger.warning(
                "Could not mark a webhook delivery expired; retrying next cycle",
                exc_info=True,
                extra={"fields": {"event": "webhook_delivery", "result": "expire_error"}},
            )
            errors += 1
    return expired, errors


def deliver_pending(
    *,
    now: datetime | None = None,
    sender: Sender | None = None,
    deadline_s: float = _DELIVERY_DEADLINE_S,
    wait_s: float = _DELIVERY_WAIT_S,
    store_call_s: float = _STORE_CALL_S,
    timeout_s: float = webhook_sender.DEFAULT_TIMEOUT_S,
    connect_timeout_s: float = webhook_sender.CONNECT_TIMEOUT_S,
    workers: int = _WORKERS,
    max_attempts: int = _MAX_ATTEMPTS_PER_CYCLE,
) -> DeliveryReport:
    """One attempt for each pending notification event, bounded. Never raises.

    Reads the committed pending set from Firestore and takes its order unchanged -- ``ready`` is
    already sorted the way ``/varsel`` spends its own budget, and re-sorting or re-deriving it here
    would be a second definition of what is pending.

    **One thing loosens R11's "spent soonest-departure-first", and it is a limit rather than a
    detail.** The order is ``valid_until``, which is ``min(occurred_at + 60 min, departure)``:
    exact for every train departing inside the horizon -- the ones a traveller can still act on --
    and clamped to the horizon beyond it, so among trains further out it falls back to the delivery
    id. Nothing on a delivery record carries the departure, so improving that means a field the
    store does not have.

    **The daily cap does not loosen it, and this docstring claimed it did until 2026-09-09.** That
    reading described a version of the store where ``claim_delivery`` READ the allowance and
    ``mark_delivery_sent`` spent it, so up to ``workers`` claims could pass against one free slot.
    The claim now RESERVES the slot in the transaction that admits the delivery
    (``notification_store._claim_body``), and :func:`_shard_by_subscription` puts one subscription on one
    worker -- so a subscription's claims are sequential by construction and the cap is exact at any
    pool width, which is also what makes "spent on the soonest" exact rather than approximately
    true.
    """
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    send = sender if sender is not None else webhook_sender.deliver
    started = monotonic()
    budget = _DeliveryBudget(
        max_attempts, started + deadline_s, _worst_case_s(timeout_s, store_call_s)
    )
    try:
        pending = notification_store.pending_deliveries(
            limit=max_attempts,
            # The per-destination attempt ceiling is the CLIENT's quota since U7, so a client at a
            # hundred thousand subscriptions is not bounded by the number a pilot at a few hundred
            # was sized with. Resolved here because the store must not import `notification_auth`;
            # this module already does, for the destination record and R12's pause.
            per_destination_limit=lambda client_id: notification_auth.quotas_for(
                client_id
            ).deliveries_per_minute,
            now=instant,
        )
    except Exception:
        logger.warning(
            "Could not read the pending webhook deliveries this cycle; nothing was attempted",
            exc_info=True,
            extra={"fields": {"event": "webhook_delivery", "result": "pending_error"}},
        )
        return DeliveryReport(store_errors=1, ms=int((monotonic() - started) * 1000))

    # THE STALE SWEEP IS A POOL TOO, and it was not until 2026-09-09. It is a loop of store WRITES
    # on the poller's thread, which is the one class of call this module's docstring says has no
    # ceiling of its own -- and it ran BEFORE the delivery pool existed, so the
    # `wait(futures, timeout=...)` below never reached it. The budget bounded the count and the
    # start time; nothing bounded the duration, so one hung `mark_delivery_expired` commit held the
    # ingestion thread for as long as Firestore took and blew the 28 s ceiling KTD6's 30 s reversal
    # trigger is sized against. `notification_events.emit`'s shape, for the same reason and with the same
    # residual: one leaked thread per bad cycle, which Python cannot cancel and which is far cheaper
    # than a stalled poller.
    expired = expire_errors = 0
    timed_out = False
    if pending.stale:
        sweep = ThreadPoolExecutor(max_workers=1, thread_name_prefix="client-expire")
        try:
            expired, expire_errors = sweep.submit(
                _expire_stale, pending.stale, now=instant, budget=budget
            ).result(timeout=max(0.0, wait_s - (monotonic() - started)))
        except Exception:
            logger.warning(
                "The client stale-delivery sweep did not finish inside its budget; the records "
                "stay pending and the next cycle re-offers them",
                exc_info=True,
                extra={"fields": {"event": "webhook_delivery", "result": "expire_timeout"}},
            )
            timed_out = True
        finally:
            # NEVER wait, for `deliver_pending`'s own reason two blocks down.
            sweep.shutdown(wait=False)
    counts = _Counts(expired=expired, store_errors=expire_errors)
    skips = _Skips()

    # A sweep that timed out means the store is demonstrably not answering, and the wait budget it
    # spent is gone. Dialling four more claims into it would leak four more threads to arrive at the
    # same `not_done`, so this cycle stops here and the next one asks again -- which is this
    # module's retry model for everything else.
    shards: list[tuple[notification_store.Delivery, ...]] = (
        [] if timed_out else _shard_by_subscription(pending.ready)
    )
    if shards:
        pool = ThreadPoolExecutor(
            max_workers=min(workers, len(shards)), thread_name_prefix="client-deliver"
        )
        try:
            futures = [
                pool.submit(
                    _deliver_shard,
                    shard,
                    counts=counts,
                    skips=skips,
                    budget=budget,
                    sender=send,
                    timeout_s=timeout_s,
                    connect_timeout_s=connect_timeout_s,
                    now=instant,
                )
                for shard in shards
            ]
            _, not_done = wait(futures, timeout=max(0.0, wait_s - (monotonic() - started)))
            # `or`, not `=`: the sweep above can already have set it, and although the guard on
            # `shards` makes that unreachable today, an assignment here would silently un-report a
            # sweep timeout the day that guard changes.
            timed_out = timed_out or bool(not_done)
        finally:
            # NEVER wait: `__exit__` would call `shutdown(wait=True)` and block the poller until
            # whatever is hung finally returns, defeating the bound completely. An abandoned worker
            # outlives this cycle and is collected when its call returns; what it may cost is a POST
            # that landed without being recorded, which the next cycle re-sends -- at-least-once.
            pool.shutdown(wait=False)

    ms = int((monotonic() - started) * 1000)
    minted = [d.created_at for d in pending.ready if d.created_at is not None]
    report = DeliveryReport(
        ready=len(pending.ready),
        backlog_age_s=max(0.0, (instant - min(minted)).total_seconds()) if minted else 0.0,
        sent=counts.get("sent"),
        already_resolved=counts.get("already_resolved"),
        superseded=counts.get("superseded"),
        failed=counts.get("failed"),
        retryable=counts.get("retryable"),
        expired=counts.get("expired"),
        cancelled=counts.get("cancelled"),
        capped=counts.get("capped"),
        deadline=counts.get("deadline"),
        gone=counts.get("gone"),
        not_deliverable=counts.get("not_deliverable"),
        paused=skips.paused_count(),
        backed_off=counts.get("backed_off"),
        lease_lost=counts.get("lease_lost"),
        leased=counts.get("leased"),
        skipped=counts.get("skipped"),
        skipped_destinations=skips.count(),
        store_errors=counts.get("store_errors"),
        truncated=pending.truncated,
        throttled=pending.throttled,
        timed_out=timed_out,
        ms=ms,
    )
    logger.info(
        "Webhook delivery: %d sent of %d ready in %d ms (%d skipped, %d capped, %d expired)",
        report.sent,
        report.ready,
        ms,
        report.skipped,
        report.capped,
        report.expired,
        extra={
            "fields": {
                "event": "webhook_delivery_round",
                "result": "timeout" if timed_out else "ok",
                "ready": report.ready,
                "backlog_age_s": int(report.backlog_age_s),
                "sent": report.sent,
                # Beside `sent` rather than added into it: U8 publishes this record as operational
                # telemetry, so "we delivered N notifications" has to mean that.
                "already_resolved": report.already_resolved,
                "superseded": report.superseded,
                "failed": report.failed,
                "retryable": report.retryable,
                "expired": report.expired,
                "cancelled": report.cancelled,
                "capped": report.capped,
                # Apart from `capped`: R11's suppression is a product fact the store records; a
                # deadline bind is this pass running out of clock and is recorded nowhere.
                "deadline": report.deadline,
                "not_deliverable": report.not_deliverable,
                "paused": report.paused,
                "backed_off": report.backed_off,
                "lease_lost": report.lease_lost,
                "leased": report.leased,
                "skipped": report.skipped,
                "skipped_destinations": report.skipped_destinations,
                "store_errors": report.store_errors,
                "truncated": report.truncated,
                "throttled": list(report.throttled),
                "ms": ms,
            }
        },
    )
    return report


__all__ = [
    "DeliveryReport",
    "Sender",
    "deliver_pending",
    "event_body",
    "wire_event",
]
