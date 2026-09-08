"""One bounded attempt per pending partner event, on the poller's own thread (KTD6, R9, R12, R18).

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
3. **5 s total and 2 s connect per attempt**, ``webhook_client.deliver``'s own defaults.
4. **A 20 s deadline that RESERVES its worst case**, in ``varsel._WriteBudget``'s shape -- see
   :class:`_DeliveryBudget`.
5. **A destination is skipped for the rest of the cycle after its first failure**, in PASS-LOCAL
   state. Module-level and swept it would pause a destination permanently, which is
   indistinguishable from the intended 410 pause; pass-local, it is exactly "this endpoint is
   unwell right now" and the next cycle asks again.

**And a sixth bound the five do not cover.** ``socket.settimeout`` is per-recv, not a total
deadline, and ``http.client`` reads response headers a line at a time, so a partner drip-feeding
one byte per second can hold a worker for several multiples of the read timeout --
``webhook_client``'s docstring states that and delegates the containment here. ``partner_store``
plumbs no ``timeout=`` into the Firestore client either, so a hung store call has no ceiling of its
own. Both are contained the same way: every task is submitted to the pool and the pool is awaited
for :data:`_DELIVERY_WAIT_S`, then abandoned with ``shutdown(wait=False)``. The reservation budget
is what bounds the *ordinary* pass; the bounded wait is what bounds a pass with something hung in
it. They are different failures and neither one catches both.

**Dedup is on success, exactly as /varsel's is.** An attempt that errors leaves no mark and is
re-offered next cycle, and R11's daily cap slot is spent inside the transaction that records the
success -- otherwise a dead destination burns a subscription's four for the Oslo day with nothing
delivered. All of that lives in ``partner_store``; this module classifies and reports.

**What this module does not do.** It never re-derives or re-sorts the pending set
(``partner_store.pending_deliveries`` already separates ``ready``, ``stale`` and ``throttled`` and
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

import partner_auth
import partner_store
import webhook_client
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
#: What one cycle may attempt, on top of the clock -- `partner_store`'s own per-cycle ceiling, so
#: the query's `limit` and this budget's count cannot disagree.
_MAX_ATTEMPTS_PER_CYCLE = partner_store.MAX_DELIVERY_ATTEMPTS_PER_CYCLE


def _worst_case_s(timeout_s: float, store_call_s: float) -> float:
    """The whole attempt's worst case: the outbound call plus the three store calls around it."""
    return timeout_s + 3 * store_call_s


class Sender(Protocol):
    """``webhook_client.deliver``'s shape, narrowed to what this module passes.

    A Protocol for the same reason ``varsel.ImpactProvider`` is one: it is a cross-module contract
    whose drift is quiet, and the suites replace it wholesale. ``webhook_client.deliver`` raises
    ``NetworkBlocked`` from a test process that forgot to, so a missing stub is loud rather than a
    request to a partner-supplied host from CI.
    """

    def __call__(
        self,
        url: str,
        *,
        body: bytes,
        headers: dict[str, str],
        timeout_s: float,
        connect_timeout_s: float,
    ) -> webhook_client.DeliveryAttempt: ...


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
    write, and the pool is four wide. Nothing was broken -- `partner_auth.pause_destination` is
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

    ``sent`` counts FRESH successes only -- one notification the partner did not already have, one
    of R11's four slots spent. Two counters exist beside it because a late arrival writes nothing
    and must not be filed as a send. The pass abandons an attempt's thread when its bounded wait
    expires (it cannot kill a thread parked in a socket read); the next cycle legitimately re-sends
    the same still-pending event; and the abandoned thread then returns and asks the store to
    record a success that has already been recorded. ``partner_store`` now refuses that write and
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
    skipped: int = 0
    skipped_destinations: int = 0
    store_errors: int = 0
    truncated: bool = False
    throttled: tuple[str, ...] = ()
    timed_out: bool = False
    ms: int = 0


class _DeliveryBudget:
    """This pass's ceiling on attempts, claimable from four threads at once.

    **The clock is RESERVED, not checked**, and the reservation covers the attempt *plus the durable
    write that follows it*. ``varsel._WriteBudget``'s docstring records why: a naive
    ``monotonic() >= deadline`` bounds when the last attempt *starts*, not when the pass *ends*, and
    it overshot a documented 30 s ceiling by 16 s in exactly that shape. Concurrency does not weaken
    it -- every worker reserves its own worst case before starting, so whatever is in flight when
    the deadline nears was already paid for.

    **Claimed inside the worker as its task starts, never at submit time.** Submitting is what
    queues the work; if the claim ran there, every task would be admitted before the deadline was
    consulted and the bound would guarantee nothing. That is ``/meldinger``'s recorded lesson, and
    it is the one line of this class that is easy to move and fatal to move.
    """

    def __init__(self, max_calls: int, deadline: float, worst_case_s: float) -> None:
        self._lock = threading.Lock()
        self._max_calls = max_calls
        self._deadline = deadline
        self._worst_case = worst_case_s
        self.claimed = 0
        self.refused = 0

    def claim(self) -> bool:
        with self._lock:
            if self.claimed >= self._max_calls or monotonic() + self._worst_case >= self._deadline:
                self.refused += 1
                return False
            self.claimed += 1
            return True


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
        self._records: dict[str, partner_auth.Destination | None] = {}

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

    def destination(self, destination_id: str) -> partner_auth.Destination | None:
        """The destination record, read at most once per pass.

        Cached because R12's pause and the signing generations are read per attempt and a
        destination holds many pending events on a bad morning -- one read per destination per cycle
        rather than one per event. The value is cached even when it is ``None``, so a destination
        record that has gone missing costs one read rather than one per pending event.
        """
        with self._lock:
            if destination_id in self._records:
                return self._records[destination_id]
        record = partner_auth.destination_for_delivery(destination_id)
        with self._lock:
            self._records[destination_id] = record
        return record


def _event_body(event: dict[str, Any]) -> bytes:
    """The event as the exact bytes that are signed and sent.

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
# `partner_id` and `destination_id` are our own routing, recorded so a delivery can be traced back
# to the destination it was authorised by; `expires_at` is the store's TTL field, a retention
# decision and never validity, which on the wire would invite a receiver to read it as one. Sending
# any of the three costs every event for every partner running the verifier U7 ships. The second
# reason is direction of failure: a deny-list fails OPEN, so the next field added to
# `partner_store._mint` would reach every partner unreviewed, and KTD10's boundary would rest on
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
    "partner_ref",
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


def _wire_event(event: dict[str, Any]) -> dict[str, Any]:
    """The stored event projected onto the published envelope -- see ``_WIRE_FIELDS`` above.

    A key the store has not written is left out rather than sent as null: every optional property is
    optional in the schema too, and a null where a receiver expects a string is a parse failure it
    cannot distinguish from corruption.
    """
    return {key: event[key] for key in _WIRE_FIELDS if key in event}


def _attempt(
    delivery: partner_store.Delivery,
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
    destination = skips.destination(delivery.destination_id)
    if destination is None or not destination.deliverable:
        # R12's pause, read before the attempt rather than rediscovered by it. The delivery stays
        # `pending` and expires at its own validity: a paused destination is an operator's to
        # resume, and writing a terminal state here would throw the event away on their behalf.
        skips.drop(delivery.destination_id)
        return "not_deliverable"
    claim = _store_step(
        "claim", delivery, lambda: partner_store.claim_delivery(delivery.delivery_id, now=now)
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
        lambda: (partner_store.get_event(claim.delivery.event_id, now=now),),
    )
    if read is None:
        return "store_errors"
    event = read[0]
    if event is None or not partner_store.event_is_valid(event, now):
        # R9: past its validity a delivery is EXPIRED, not failed. "We ran out of time to tell you"
        # and "your endpoint rejected us" are different operator problems, and collapsing them
        # would hide a dead destination inside ordinary expiry.
        if _store_step(
            "expire",
            delivery,
            lambda: partner_store.mark_delivery_expired(delivery.delivery_id, now=now),
        ) is None:
            return "store_errors"
        return "expired"

    body = _event_body(_wire_event(event))
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
            "Partner event could not be signed; leaving it pending (%s)",
            type(exc).__name__,
            exc_info=True,
            extra={"fields": {"event": "partner_delivery", "result": "unsigned",
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
    )
    if attempt.ok:
        # The partner HAS the event; if this write fails, only our record of it is missing.
        # Recording nothing is the right direction -- the next cycle re-sends and the receiver's own
        # inbox suppresses the duplicate, which is the contract (R12, at-least-once). Marking it
        # failed would be a lie about what happened.
        outcome = _store_step(
            "sent",
            delivery,
            lambda: partner_store.mark_delivery_sent(delivery.delivery_id, now=now),
        )
        if outcome is None:
            return "store_errors"
        # The write is idempotent and says so. A late arrival from a thread the previous cycle
        # abandoned found the delivery already resolved, wrote nothing, and spent no slot -- so it
        # is not a send, however successful the HTTP call it just made was. See `DeliveryReport`.
        return _resolved_kind(outcome) or "sent"

    if attempt.pause_destination:
        # R12: a 410 pauses the DESTINATION until an operator resumes it. Idempotent in
        # `partner_auth`, so a backlog against a gone endpoint cannot fill the audit collection --
        # and it is still called per attempt rather than claimed once under the lock, so that a
        # pause which FAILS can be retried by the next of this cycle's attempts instead of being
        # swallowed by a claim nobody releases.
        try:
            partner_auth.pause_destination(
                delivery.destination_id, reason=f"delivery {attempt.error or 'gone'}"
            )
            skips.pause_recorded(delivery.destination_id)
        except Exception:
            logger.warning(
                "Could not pause a destination that answered 410; it is re-attempted next cycle",
                exc_info=True,
                extra={"fields": {"event": "partner_delivery", "result": "pause_failed",
                                  "destination_id": delivery.destination_id}},
            )
    failure = _store_step(
        "failed",
        delivery,
        lambda: partner_store.mark_delivery_failed(
            delivery.delivery_id,
            error=attempt.error or "unknown",
            retryable=attempt.retryable,
            retry_after_s=attempt.retry_after_s,
            # `cancelled` for a gone destination, `failed` otherwise -- both are pinned as members
            # of `partner_store.DELIVERY_STATES` by U5's suite, so they pass straight through.
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
    # The endpoint really did misbehave, so the destination is out for the rest of the cycle either
    # way -- but `retryable` would be a false promise for a record another pass has already
    # resolved: nothing was written and nothing will be retried. This is the failure half of the
    # same late-arrival race as the success branch, and `_failed_body` guards against it for a
    # sharper reason: unguarded it set an already-`sent` delivery back to `pending`, so the next
    # cycle sent the partner a second copy.
    resolved = _resolved_kind(failure)
    if resolved is not None:
        return resolved
    if attempt.pause_destination:
        # The EVENT's outcome, which is what the per-attempt counters are about: `cancelled` is the
        # state just written to its record, so the report and the store agree. How many
        # DESTINATIONS were paused is a different question and is counted off a set.
        return "cancelled"
    return "retryable" if attempt.retryable else "failed"


def _store_step[T](step: str, delivery: partner_store.Delivery, call: Callable[[], T]) -> T | None:
    """Run one store call, or log and classify its failure and answer ``None``.

    A wrapper rather than six ``try/except`` blocks, and the reason is what it does NOT do: nothing
    is written on a failure, in either classification, which is what makes a store outage cost a
    cycle rather than a subscription. ``partner_store.is_retryable`` decides only what the log
    line says -- and it is the right classifier rather than a handler keyed on
    ``google.api_core`` exceptions, because an exhausted transaction surfaces as a plain
    ``ValueError`` after five attempts, which such a handler would call terminal, mark the subject
    failed for, and never deliver.

    ``None`` is unambiguous here because no store call this module makes returns ``None`` on
    success: the two that could (``get_event``, ``destination_for_delivery``) are handled by their
    own callers, and the rest return a dataclass.
    """
    try:
        return call()
    except Exception as exc:
        logger.warning(
            "Partner delivery store step %s failed (%s); the delivery is re-offered next cycle",
            step,
            "retryable" if partner_store.is_retryable(exc) else "terminal",
            exc_info=True,
            extra={
                "fields": {
                    "event": "partner_delivery",
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
    ready: Sequence[partner_store.Delivery],
) -> list[tuple[partner_store.Delivery, ...]]:
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
    shards: dict[str, list[partner_store.Delivery]] = {}
    for delivery in ready:
        # `ready` is already in the store's soonest-first order, so appending preserves it inside
        # each shard and the first key inserted is the soonest subscription.
        shards.setdefault(delivery.subscription_id, []).append(delivery)
    return [tuple(group) for group in shards.values()]


def _deliver_shard(
    deliveries: Sequence[partner_store.Delivery],
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
                "A partner delivery attempt raised; it is re-offered next cycle",
                exc_info=True,
                extra={"fields": {"event": "partner_delivery", "result": "error"}},
            )
            key = "store_errors"
        counts.add(key)


def _resolved_kind(outcome: Any) -> str | None:
    """Which counter a terminal write belongs to, or ``None`` for an ordinary fresh one.

    ``partner_store`` refuses a terminal write to a delivery that is no longer ``pending`` and
    answers ``already_resolved=True`` with the state the record actually holds. That is not an
    error -- the partner did receive the event, and raising would log a store failure and read as
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
    """
    expired = errors = 0
    for delivery_id in stale:
        if not budget.claim():
            break
        try:
            partner_store.mark_delivery_expired(delivery_id, now=now)
            expired += 1
        except Exception:
            logger.warning(
                "Could not mark a partner delivery expired; retrying next cycle",
                exc_info=True,
                extra={"fields": {"event": "partner_delivery", "result": "expire_error"}},
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
    timeout_s: float = webhook_client.DEFAULT_TIMEOUT_S,
    connect_timeout_s: float = webhook_client.CONNECT_TIMEOUT_S,
    workers: int = _WORKERS,
    max_attempts: int = _MAX_ATTEMPTS_PER_CYCLE,
) -> DeliveryReport:
    """One attempt for each pending partner event, bounded. Never raises.

    Reads the committed pending set from Firestore and takes its order unchanged -- ``ready`` is
    already sorted the way ``/varsel`` spends its own budget, and re-sorting or re-deriving it here
    would be a second definition of what is pending.

    **Two things loosen R11's "spent soonest-departure-first", and both are limits rather than
    details.** The order is ``valid_until``, which is ``min(occurred_at + 60 min, departure)``:
    exact for every train departing inside the horizon -- the ones a traveller can still act on --
    and clamped to the horizon beyond it, so among trains further out it falls back to the delivery
    id. Nothing on a delivery record carries the departure, so improving that means a field the
    store does not have. And the daily cap is READ by ``claim_delivery`` and SPENT by
    ``mark_delivery_sent``, so up to ``workers`` claims can pass the check before any increment
    lands: measured on AE3's shape (thirteen departures, cap four) the delivered four were the 1st,
    2nd, 3rd and 6th soonest. The COUNT is exact either way -- Firestore's counter is authoritative,
    and the store records every suppression -- and the loosening is bounded by the pool width.
    Tightening it would mean claiming sequentially, and a claim is a store call: sequential claims
    for a two-hundred-deep backlog are exactly what the pool exists to avoid.
    """
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    send = sender if sender is not None else webhook_client.deliver
    started = monotonic()
    budget = _DeliveryBudget(
        max_attempts, started + deadline_s, _worst_case_s(timeout_s, store_call_s)
    )
    try:
        pending = partner_store.pending_deliveries(limit=max_attempts, now=instant)
    except Exception:
        logger.warning(
            "Could not read the pending partner deliveries this cycle; nothing was attempted",
            exc_info=True,
            extra={"fields": {"event": "partner_delivery", "result": "pending_error"}},
        )
        return DeliveryReport(store_errors=1, ms=int((monotonic() - started) * 1000))

    expired, expire_errors = _expire_stale(pending.stale, now=instant, budget=budget)
    counts = _Counts(expired=expired, store_errors=expire_errors)
    skips = _Skips()
    timed_out = False

    shards = _shard_by_subscription(pending.ready)
    if shards:
        pool = ThreadPoolExecutor(
            max_workers=min(workers, len(shards)), thread_name_prefix="partner-deliver"
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
            timed_out = bool(not_done)
        finally:
            # NEVER wait: `__exit__` would call `shutdown(wait=True)` and block the poller until
            # whatever is hung finally returns, defeating the bound completely. An abandoned worker
            # outlives this cycle and is collected when its call returns; what it may cost is a POST
            # that landed without being recorded, which the next cycle re-sends -- at-least-once.
            pool.shutdown(wait=False)

    ms = int((monotonic() - started) * 1000)
    report = DeliveryReport(
        ready=len(pending.ready),
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
        skipped=counts.get("skipped"),
        skipped_destinations=skips.count(),
        store_errors=counts.get("store_errors"),
        truncated=pending.truncated,
        throttled=pending.throttled,
        timed_out=timed_out,
        ms=ms,
    )
    logger.info(
        "Partner delivery: %d sent of %d ready in %d ms (%d skipped, %d capped, %d expired)",
        report.sent,
        report.ready,
        ms,
        report.skipped,
        report.capped,
        report.expired,
        extra={
            "fields": {
                "event": "partner_delivery_round",
                "result": "timeout" if timed_out else "ok",
                "ready": report.ready,
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
]
