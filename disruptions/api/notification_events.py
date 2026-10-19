"""The client alert lifecycle: this cycle's observations turned into committed events.

Between :mod:`varsel` (which decides *what is true about a train* and what a traveller would be
told) and :mod:`notification_store` (which owns the durable revision, the event and its delivery
record). It holds the ordering, the bounds and the "is there anything to write?" question, and
deliberately holds neither the transition rule nor any prose:

* **The transition rule is `notification_store._transition`, called and never restated.** A new revision
  is minted only on the lifecycle diagram's transitions -- none→phase, predicted→confirmed,
  withdrawn→{predicted, confirmed} -- and a changed probability, delay figure or wording mints
  nothing, at any size. There is no materiality threshold to tune: minting on a changed figure
  would turn every poller cycle into a fresh revision, and therefore a second notification about
  the same train. Restating that here is exactly the duplication the plan forbids, so the private
  name is used on purpose and :func:`skip_reason` is pinned against it by the suite.
* **Every user-facing string arrives on the subject**, built by ``varsel`` through the one assembly
  the Web Push body also goes through, so an event's ``notification`` is byte-identical to what
  ``/varsel`` would have pushed for the same journey (R7). Nothing here composes Norwegian.

**The pending set is read from Firestore, never re-derived from a cycle's decisions.** This module
writes events; :mod:`webhook_delivery` reads the committed delivery records. A re-derivation would
mint a fresh revision and therefore a fresh ``event_id`` every cycle, which the receiver cannot
recognise as a duplicate and would turn into a second notification about the same train.

**Why there is a pre-read at all.** ``commit_event`` and ``record_below_threshold`` are Firestore
transactions, and the population this runs over is mostly *nothing to do*: about 83 % of matched
alerts are sub-threshold (`varsel_decisions`' own split), and an alert-worthy train whose phase has
not changed is a transaction that reads two documents and returns ``unchanged`` on every one of the
~300 cycles in a day. So "which subjects stand, and in which phase" is answered before the
transactions are spent, and they are spent only where a write can actually happen. That is reading
the store to decide what to write -- not re-deriving the pending set -- and it is exact rather than
best-effort for the reason KTD6 names: the emit path has exactly one writer (one poller thread, no
route invokes the pass, and only the pod holding the poller lease runs the cycle), so nothing can
change a subject between the answer and the commit. Since the zero-downtime work that is one LEADER
per leadership term, not one replica: the exactness holds per term. At a handoff at most
`_EMIT_WORKERS` abandoned commits of the old leader's pass can land after the new leader seeded,
each idempotent; shutdown and lease loss wait for this module's pass lock first
(:func:`wait_until_idle`), bounded by the shutdown deadline.

**Where that answer comes from moved in U9, and the reason is a ceiling rather than a cost.** It
was one client-scoped query per client (``notification_store.client_alerts``), which serves at most
:data:`notification_store.MAX_ALERTS_SERVED` subjects per subscription in its LIST while counting
all of them -- so a subscription holding more standing alerts than that (AE3's line suspension,
thirteen cancelled departures inside one window) reported a count this module could not enumerate,
and an incomplete answer means a store transaction PER SUBJECT. At one client with 100 000
subscriptions that fallback is the steady state, which is to say the optimisation stops existing
exactly where it is needed. :class:`notification_store.SubjectMirror` answers the same question
completely, from memory, kept current by this process's own commits -- subject documents have
exactly one writer, so every change passes through here. The fallback survives unchanged and is now
the restart window only: an unseeded mirror is :data:`_INCOMPLETE` for every subscription, which is
the slow path and the correct one.

**Bounded twice, because the two failures are different.** A reservation budget
(:class:`_EmitBudget`, ``varsel._WriteBudget``'s shape) makes :data:`_EMIT_DEADLINE_S` a ceiling on
when the step *ends* under bounded slowness; an outer bounded wait on a worker thread
(:data:`_EMIT_WAIT_S`) is the backstop for a store call that hangs, which the reservation cannot
see: ``notification_store`` plumbs :data:`~notification_store._CALL_DEADLINE_S` into every READ, a transaction's
reads included, but a transactional COMMIT carries no timeout at all. Both numbers are sized so this step plus
:mod:`webhook_delivery`'s stay under KTD6's own 30 s reversal trigger for the phase.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import notification_store
import reserved_budget

logger = logging.getLogger(__name__)

OSLO = ZoneInfo("Europe/Oslo")

# ---- Bounds ------------------------------------------------------------------------------------
#
# KTD6: "The load step and the emit transactions each carry a per-call Firestore deadline and a
# whole-step budget reserved the way the delivery deadline is." The per-call half is now real for
# READS -- `notification_store._CALL_DEADLINE_S` (4.0 s, under this module's own `_EMIT_WAIT_S` so a hung
# read surfaces as the store's own retryable `DeadlineExceeded` rather than as our timeout with a
# thread still running) -- and is still unexpressible for the COMMIT, which the Python client's
# `@firestore.transactional` offers no way to bound. So the outer bounded wait below still stands in
# for it, in the shape `varsel._load_webhook_recipients` already uses for the load step, and the
# residual is a leaked thread rather than a stalled poller.
#
# **WIDENED IN U9, AND THE DEADLINE HAD TO MOVE WITH THE WIDTH.** It was 6 s against an 8 s wait,
# sized so this step plus `webhook_delivery`'s 20 s stayed under KTD6's 30 s reversal trigger for
# the phase -- a trigger stated in the phase's measured `ms`. That arithmetic was right for a
# one-wide loop at the pilot's "a few hundred events a day" and is the wrong shape for R2's target:
# a bad morning's lifecycle transitions have to be emitted INSIDE one cycle, and one transaction at
# a time against a ~200 ms round trip is ~5 emits a second however many are waiting. A width
# without a deadline to spend changes nothing -- at 6 s the pass stops after about four seconds of
# claims whether it is one-wide or sixty-four-wide -- so the two move together or neither does.
#
# 80 s is emit's share of a cycle measured at a ~296 s period (2026-09-08), and the number it is
# sized against is R2's 250 emits per second: 80 x 250 = 20 000, which is `_MAX_EMITS_PER_CYCLE`
# below. What it gives up is stated rather than hidden and is dealt with in the operations
# document: the phase's ceiling is now 90 + 20 = 110 s, so KTD6's "exceeds 30 s of measured phase
# time" can no longer be read as a wall-clock ceiling -- the widened pass exceeds it BY DESIGN, and
# the trigger is restated there as emit's share being exceeded (the pass ran to its deadline with
# work left over) rather than as a number of seconds.
_EMIT_DEADLINE_S = 80.0
_EMIT_WAIT_S = 90.0
#: How many store transactions run at once. **KTD2's derivation, not a guess**: the target rate
#: times the measured transaction latency, 250/s x ~250 ms = 63, taken to 64. That latency is dev's
#: first reading and it INCLUDED the per-mint count aggregation this unit removes, so the true
#: figure is lower and this width is on the safe side of it -- re-measure with the harness before
#: moving it, and move `_EMIT_DEADLINE_S` in the same breath if the rate is what changed.
#:
#: Two concurrent emits cannot contend on one document: `_mint` writes the subject, the event and
#: the delivery, and READS the subscription without writing it, so the only shared document in a
#: transaction is one nothing in this pass modifies.
_EMIT_WORKERS = 64
# One bounded Firestore transaction, reserved up front. `commit_event` is a transaction the client
# retries up to five times, so this is a budgeting assumption and not a guarantee -- which is
# precisely why the outer wait exists as well.
_WORST_CASE_EMIT_S = 2.0
#: How often the pass beats the poller's heartbeat while it submits. `/health`'s staleness rule
#: means "no STEP has finished in `HEALTH_STALE_AFTER_S`" (600 s), and a step that may now run for
#: 80 s is a long way inside that -- but it is a long way further inside it than 6 s was, and the
#: cheapest insurance against the next widening is to beat already. `varsel._HEARTBEAT_EVERY`'s
#: number and shape.
_HEARTBEAT_EVERY = 64

#: What one cycle may write, on top of the clock. A count as well as a clock for
#: `varsel._WriteBudget`'s reason: the two answer different questions, and a caller must not be able
#: to satisfy one and ignore the other.
#:
#: 20 000 since U9 -- `_EMIT_DEADLINE_S` times R2's target rate, so the two bounds bind at the same
#: moment on a healthy store and the count is what binds first when the store is faster than
#: expected. It was 120, sized for "a few hundred events a day"; anything above it is re-offered on
#: the next cycle and counted as `capped`, which is what the plan calls `deferred`.
_MAX_EMITS_PER_CYCLE = 20_000

#: The least validity an observation the matcher admitted gets, in seconds past the emit instant.
#: The match (pass 1a) and this emit are the same cycle but not the same instant -- the wording
#: pass, the browser deliveries and two durable writes sit between them -- and the delivery step
#: that classifies the event sits after this emit again. So a train the matcher judged catchable
#: can have an expected departure a few seconds ahead, or already behind, by the time it is
#: minted, and either way the event would be stale before its first attempt while the browser push
#: about the same train went out. The floor applies to every expected departure LESS THAN two
#: minutes ahead of the emit instant, the already-past ones included: `valid_until` is never
#: earlier than `now + DEPARTURE_GRACE_S`. Two minutes covers the gap and is well under one pass
#: period (~296 s measured 2026-09-08), so it cannot carry a departed train into a retry next cycle.
DEPARTURE_GRACE_S = 120


@dataclass(frozen=True)
class WebhookSubject:
    """One (subscription, train) this cycle observed, with everything a client would receive.

    Built by ``varsel`` at the end of the notification pass and handed to this module through
    ``check_and_notify``'s return value -- deliberately a value rather than a module-level snapshot,
    because a leaked half-cycle snapshot reads exactly like the give-up bug every other ledger in
    that file is swept to avoid.

    ``title``, ``body`` and ``payload`` are the notification for **this** observation: the alert
    wording when :attr:`alertworthy`, the retraction wording when it is not. ``varsel`` builds them
    through the one assembly the Web Push body also goes through, so R7's "identical to what
    /varsel would have pushed" is true by construction rather than by a parallel builder.

    ``situations`` carries **every** situation contributing to this train, not just the one
    ``one_alert_per_train`` kept: R8 calls them "the contributing situations", and the event names
    them all. The delivered mark ``notification_store._sent_body`` writes is keyed on the TRAIN --
    the ``alert_id`` -- and not on any of these numbers (2026-09-10), so which situation wins the
    collapse in a later cycle cannot re-pend a train already delivered, and a second departure under
    an already-delivered situation is what it is: a new train, still owed its notification.
    """

    # Routing and the version the match was computed against. The version is the LOAD snapshot's,
    # never a fresh read: `notification_store._subscription_obsolete` compares it against what the
    # subscription holds at commit time, which is how "a slow pass cannot emit obsolete work" is
    # enforced. Re-reading it here would make that check compare a value with itself and emit an
    # alert about a train matched for a route the client has since changed.
    subscription_id: str
    client_id: str
    destination_id: str
    subscription_version: int
    external_ref: str = ""
    test: bool = False

    # The observation.
    alertworthy: bool = False
    cancelled: bool = False
    situations: tuple[str, ...] = ()

    # The train, flat rather than a `TrainRef`, so `varsel` needs no `notification_store` type and one
    # function (:func:`train_ref`) owns "can this observation be a stable subject at all".
    operator: str | None = None
    line: str | None = None
    journey_ref: str | None = None
    stop_place: str | None = None
    stop_order: int | None = None
    departure_time: datetime | None = None

    # What a client receives.
    title: str = ""
    body: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    # When the train is expected to actually leave: the aimed time plus the delay predicted for it,
    # which is the matcher's own definition of "already gone" (`impact._expected_departure`). It is
    # R12's departure term. The AIMED time is not: a train 25 minutes late is still catchable after
    # its aimed time has passed -- the matcher keeps it and the browser pushes it -- and an event
    # whose validity ended at the aimed time was stale at mint and never attempted. Appended last
    # so positional constructors keep working; `None` falls back to `departure_time`.
    expected_departure_time: datetime | None = None


@dataclass(frozen=True)
class Recipient:
    """One subscription a shared observation is owed to. Everything routing needs and nothing else.

    Split out of :class:`WebhookSubject` in U9 so the wording, the payload and the train identity
    are held ONCE per (definition, train) instead of once per recipient -- see
    :class:`WebhookObservation` for the arithmetic that forces it.
    """

    subscription_id: str
    client_id: str
    destination_id: str
    subscription_version: int
    external_ref: str = ""
    test: bool = False


@dataclass(frozen=True)
class WebhookObservation:
    """One (definition, train) this cycle observed, and every subscription watching it.

    **What the pass hands over since U9, and the change is about memory rather than tidiness.** It
    used to be one :class:`WebhookSubject` per (recipient, train), built in `varsel` before this
    module saw any of them -- so a corridor with 100 000 subscriptions and AE3's thirteen cancelled
    departures materialised 1.3 million dataclasses in one burst on the ingestion thread, every one
    of them carrying the routing of a subscription whose subject may not even stand. The payload
    was already shared by reference; the objects around it were not, and the pod's memory limit
    equals its request.

    Held this way, the wording and the train are one object per train and the per-recipient cost is
    six strings. :meth:`subject` materialises the rest, and :func:`_run` calls it only where the
    standing set says a store call can actually happen -- which for ~83 % of matched alerts (the
    sub-threshold ones, `varsel_decisions`' own split) is never.

    Every field below means exactly what the same-named field on :class:`WebhookSubject` means; the
    docstring there is the one to read.
    """

    recipients: tuple[Recipient, ...] = ()

    alertworthy: bool = False
    cancelled: bool = False
    situations: tuple[str, ...] = ()

    operator: str | None = None
    line: str | None = None
    journey_ref: str | None = None
    stop_place: str | None = None
    stop_order: int | None = None
    departure_time: datetime | None = None

    title: str = ""
    body: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    expected_departure_time: datetime | None = None

    #: The decision log's own subject, (journey definition, situation, departure) -- `varsel.
    #: _decision_key`'s tuple, opaque here. U16's seam, and it is on the OBSERVATION rather than
    #: derived from the alert id because the two grains do not convert: an alert id is per
    #: (subscription, train) and a decision row is per (definition, situation, departure), so a
    #: corridor with a hundred thousand subscriptions mints a hundred thousand alert ids for the
    #: one row this names. Carried rather than recomputed for the same reason `train_ref` is: the
    #: collapse that picked the winning situation happened in `varsel`, and restating it here would
    #: be a second definition of which situation a train's notification is about.
    #:
    #: None where the caller did not supply one; `emit` then simply reports nothing for that
    #: observation, which is what keeps every existing test and the `/test-events` route working.
    decision_key: tuple[str, str, str] | None = None

    def subject(self, recipient: Recipient) -> WebhookSubject:
        """This observation as one recipient's subject. The payload is SHARED, not copied.

        `_emit_one` copies it into the request it builds, which is the one place a mutation could
        matter; copying here instead would put a dict copy per recipient back into the hot loop
        this class exists to empty.
        """
        return WebhookSubject(
            subscription_id=recipient.subscription_id,
            client_id=recipient.client_id,
            destination_id=recipient.destination_id,
            subscription_version=recipient.subscription_version,
            external_ref=recipient.external_ref,
            test=recipient.test,
            alertworthy=self.alertworthy,
            cancelled=self.cancelled,
            situations=self.situations,
            operator=self.operator,
            line=self.line,
            journey_ref=self.journey_ref,
            stop_place=self.stop_place,
            stop_order=self.stop_order,
            departure_time=self.departure_time,
            title=self.title,
            body=self.body,
            payload=self.payload,
            expected_departure_time=self.expected_departure_time,
        )


@dataclass(frozen=True)
class EmitReport:
    """What one emit step did, as counts an operator can read off one log line.

    Counts rather than a list of ids: at the pilot's volume the interesting question is always
    "did anything get written, and what stopped the rest", and a per-subject line would put a
    client's traffic shape into routine logs at cycle rate.
    """

    created: int = 0
    unchanged: int = 0
    withdrawn: int = 0
    counting: int = 0
    obsolete: int = 0
    unidentified: int = 0
    nothing_standing: int = 0
    quota: int = 0
    capped: int = 0
    retryable: int = 0
    failed: int = 0
    #: Withdrawal runs closed because the train cleared the gate again -- one write per genuine
    #: recovery. `run_stale` counts runs left alone as already past `WITHDRAWAL_GAP_S`, where the
    #: store starts a fresh run anyway; `run_clear_capped` counts resets deferred to the next cycle
    #: by the budget. Apart from every other counter here because none of them is a notification
    #: decision: these three say only how exact the NEXT withdrawal count will be.
    run_cleared: int = 0
    run_stale: int = 0
    run_clear_capped: int = 0
    run_clear_error: int = 0
    timed_out: bool = False
    #: The alert ids this pass MINTED a revision for, in completion order. U9 step 5's seam for
    #: U16, which puts the webhook channel's own work into the decision log.
    #:
    #: **On the report and never on a module global**, which is `varsel.PassOutcome`'s rule and for
    #: its reason: a leaked half-cycle ledger reads exactly like the give-up bug every other pass
    #: in this repository is swept to avoid, and it would need registering in two stale sweeps to
    #: be safe.
    #:
    #: The ORDER is which transaction finished first, not which was submitted, so it carries no
    #: meaning beyond membership.
    minted: tuple[str, ...] = ()
    #: The same mints at the DECISION LOG's grain and deduplicated: one entry per (definition,
    #: situation, departure) any of whose recipients minted a revision. U16 reads it; nothing else
    #: does. Empty where the caller supplied no `decision_key` on its observations.
    #:
    #: **U9 left two questions here and U16 answered both, so the answers live here rather than in
    #: a commit message.** The grain is the first: it is carried on the observation
    #: (:attr:`WebhookObservation.decision_key`) rather than derived, because an alert id embeds a
    #: subscription and a decision key embeds a situation and neither converts to the other. The
    #: ORDERING is the second, and it is the one that shaped the design: this pass runs AFTER
    #: `_record_decisions` -- KTD6 puts every client step after the cycle's two durable writes,
    #: deliberately -- so a cycle's mints cannot reach the rows that cycle already wrote. U16 took
    #: the other branch U9 named, "the row has to be written somewhere else": `varsel.
    #: record_minted_decisions` writes ONE more row per newly-notified subject, right here in the
    #: `notification_webhooks` phase, copied from the ledger's own row with `pushed` flipped. What
    #: that costs is stated where it is paid, on that function.
    minted_decisions: tuple[tuple[str, str, str], ...] = ()
    #: Transactions still in flight when the collection's deadline passed, walked away from rather
    #: than waited on -- a transactional commit carries no client-side deadline, so waiting would
    #: hold the pass lock on a hung store and refuse every later cycle. An abandoned transaction
    #: that later commits is a committed event nothing delivered this cycle; the next cycle's
    #: `pending_deliveries` finds it, because the pending set is the store's rather than a cycle's.
    #: This is the number behind "at most one pool's width in flight across a cycle boundary", and
    #: it is the field to read beside `capped`: a big `capped` with no `abandoned` is a pass that
    #: was simply larger than its budget, and one with `abandoned` is a slow store.
    abandoned: int = 0
    #: The previous cycle's pass had not finished draining, so this one did not start. A refusal
    #: rather than a second pass, because the emit path's exactness rests on there being exactly
    #: one writer: two overlapping passes would each hold their own pending-event count and admit
    #: past a destination's ceiling together.
    overlap_refused: bool = False
    ms: int = 0


class _EmitBudget(reserved_budget.ReservedBudget):
    """This step's ceiling on store transactions.

    The mechanism -- a count and a clock RESERVED rather than checked -- is
    :class:`reserved_budget.ReservedBudget`'s, and its docstring carries why and the measurement
    behind it. What is this step's own is the number: :data:`_WORST_CASE_EMIT_S` is what one commit
    can cost, so reserving it up front makes :data:`_EMIT_DEADLINE_S` a ceiling on when the STEP
    ends rather than on when its last transaction starts.
    """


def train_ref_of(
    *,
    operator: object,
    line: object,
    journey_ref: object,
    stop_place: object,
    stop_order: object,
    departure_time: datetime | None,
) -> notification_store.TrainRef | None:
    """The train these six fields name, or ``None`` when they cannot name one stably.

    ``alert_id`` is derived from these six fields, and the receiver's supplied policy deduplicates
    and orders on nothing else -- so an observation missing any of them cannot be a *stable* subject
    and must not become one: two different trains would share an id and the second commuter's
    notification would be suppressed as a stale revision.

    ``departure_time`` is part of that set rather than a nice-to-have, because ``call_date`` is
    derived from it and is the operating day the whole ET join key is built on. ``line`` is the one
    field allowed to be absent: ``journey_ref`` already identifies the dated train, and a line
    nobody published is a display gap rather than an identity one.

    **The one derivation for both sides of the delivered mark.** :func:`train_ref` feeds the emit
    step's ``alert_id`` from a :class:`WebhookSubject`; :func:`alert_id_for` feeds
    ``varsel._pending_pushes``' admission test from an ``Alert``. If those two ever derived the id
    differently -- ``line=""`` on one side and ``None`` on the other, say -- every delivered train
    would stay pending forever and the wording budget would burn on it silently, which is why there
    is one function here rather than one per caller.
    """
    if departure_time is None:
        return None
    if not operator or not journey_ref or not stop_place:
        return None
    if stop_order is None:
        return None
    return notification_store.TrainRef(
        operator=str(operator),
        line=str(line or ""),
        journey_ref=str(journey_ref),
        call_date=departure_time.astimezone(OSLO).date().isoformat(),
        stop_place=str(stop_place),
        stop_order=int(stop_order),
    )


def train_ref(subject: WebhookSubject) -> notification_store.TrainRef | None:
    """This observation's train, or ``None`` when it cannot be one -- see :func:`train_ref_of`."""
    return train_ref_of(
        operator=subject.operator,
        line=subject.line,
        journey_ref=subject.journey_ref,
        stop_place=subject.stop_place,
        stop_order=subject.stop_order,
        departure_time=subject.departure_time,
    )


def alert_id_for(
    subscription_id: str,
    *,
    operator: object,
    line: object,
    journey_ref: object,
    stop_place: object,
    stop_order: object,
    departure_time: datetime | None,
) -> str | None:
    """The ``alert_id`` the store would mint for this (subscription, train), or ``None``.

    What ``varsel._pending_pushes`` tests a recipient's delivered marks against: the store keys those
    marks on the ``alert_id`` (per train), and this is the same derivation the emit step's
    :func:`train_ref` goes through, so a mark the store wrote is a mark the pass can read.
    """
    ref = train_ref_of(
        operator=operator,
        line=line,
        journey_ref=journey_ref,
        stop_place=stop_place,
        stop_order=stop_order,
        departure_time=departure_time,
    )
    return None if ref is None else notification_store.alert_subject_id(subscription_id, ref)


def requested_phase(subject: WebhookSubject) -> str:
    """``confirmed`` when ET has declared the call cancelled, otherwise ``predicted``.

    A declared cancellation is a fact rather than a forecast -- ``impact._cancelled_prediction``
    serves 1.0 and null delay figures for exactly that reason -- and the phase is what carries the
    difference onto the wire. Confirmed is terminal for advice: a prediction never reinstates a
    cancelled call, which ``notification_store._transition`` enforces a layer earlier than the receiver.
    """
    return notification_store.PHASE_CONFIRMED if subject.cancelled else notification_store.PHASE_PREDICTED


def skip_reason(stored_phase: str | None, subject: WebhookSubject) -> str | None:
    """Why this subject needs no store call at all, or ``None`` when it does.

    Two answers, and both come from the store's own rule rather than from a copy of it:

    * an alert-worthy subject whose ``notification_store._transition`` is ``None`` would mint nothing --
      the same phase again, or a ``predicted`` for a train already ``confirmed``;
    * a below-threshold subject with no standing subject has nothing to withdraw, which
      ``notification_store._withdraw_body`` answers with ``nothing_standing`` and no write.

    The private ``_transition`` is called deliberately. Restating the transition table here is what
    the plan forbids ("call it, do not restate it"), and the failure of a restatement is silent: a
    copy that drifted would mint a revision on a changed probability and put a second notification
    about one train on a traveller's phone. ``test_notification_events`` pins this function against
    ``_transition`` over the whole phase cross-product, so a client-side skip can never be looser
    than the store's own decision.
    """
    return _skip_reason_for(
        stored_phase, alertworthy=subject.alertworthy, phase=requested_phase(subject)
    )


def _skip_reason_for(
    stored_phase: str | None, *, alertworthy: bool, phase: str
) -> str | None:
    """:func:`skip_reason` without a subject, so the hot loop needs none.

    The rule is stated once, here; :func:`skip_reason` is the wrapper the source-inspection test
    and every other caller use, and it is what keeps "a subject decides this" true as a statement
    about the module even though the pass no longer builds one to ask.
    """
    if alertworthy:
        if notification_store._transition(stored_phase, phase) is None:
            return "unchanged"
        return None
    if stored_phase in (None, notification_store.PHASE_WITHDRAWN):
        return "nothing_standing"
    return None


class _Ledger:
    """What a pass minted, in two grains, appended by the workers as they commit.

    Its own structure rather than a key on :class:`_Counts` because neither is a count: it is the
    pass's own record of what it wrote, and `EmitReport.minted` / `.minted_decisions` are where the
    two go.

    TWO GRAINS because the two readers ask different questions. An operator reading a pass asks
    which alert subjects moved, which is per (subscription, train). The decision log asks which
    journey definitions were notified about, which is per (definition, situation, departure) -- and
    on a shared corridor one of those covers every subscription on it, so the second list is
    deduplicated and is orders of magnitude shorter than the first. Deriving either from the other
    is not possible in this module: an alert id embeds a subscription and the decision key embeds a
    situation, and neither carries the other.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._minted: list[str] = []
        self._decisions: dict[tuple[str, str, str], None] = {}

    def add(self, alert_id: str, decision_key: tuple[str, str, str] | None = None) -> None:
        with self._lock:
            self._minted.append(alert_id)
            if decision_key is not None:
                # A dict rather than a set, so the order is first-mint order and therefore stable
                # between runs -- the same reason `minted` is a list. Membership is what the caller
                # reads; the order is what makes a failure reproducible.
                self._decisions.setdefault(decision_key, None)

    def frozen(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._minted)

    def frozen_decisions(self) -> tuple[tuple[str, str, str], ...]:
        with self._lock:
            return tuple(self._decisions)


class _Counts:
    """The pass's outcome tally, written by the workers as they go.

    `webhook_delivery._Counts`' shape and for its reason: a still-running worker's completed work
    must not be discarded when the bounded wait expires, which is exactly when the numbers are
    worth having. A plain dict was right while the pass was one-wide and is a lost update at
    sixty-four.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def add(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + n

    def get(self, key: str) -> int:
        with self._lock:
            return self._counts.get(key, 0)


class _PendingLedger:
    """How many pending events each destination holds, counted ONCE per pass and kept in step.

    **The aggregation this replaces was the transaction latency.** `commit_event` counts a
    destination's pending deliveries before every mint, and dev's first reading of ~200-250 ms per
    emit included it -- so at sixty-four workers it would be sixty-four concurrent aggregation
    queries against one collection, per mint, to re-derive a figure this process is the only writer
    of.

    **Charged on the coordinator, refunded by the worker, and the refund is what makes it exact.**
    A mint that SUPERSEDES a pending delivery is net zero on the count: `_mint` flips the old
    delivery to `superseded` in the same transaction that creates the new one. A ledger that only
    counted up would refuse at half the ceiling on exactly the morning that matters -- one where
    many mints are a `confirmed` replacing a `predicted`. So anything that did not leave a NEW
    pending delivery behind gives its charge back: a superseding mint, an `unchanged`, a
    `counting`, an `obsolete`, a refused budget claim, a raise.

    Exact rather than best-effort for the reason KTD6 names -- the emit path has exactly one writer
    -- with one change U9 makes to that sentence: the writer is now sixty-four threads of one pass
    rather than one thread, so the ceiling is enforced by this lock instead of by the shape of the
    loop. `emit` refuses to start a second pass over the same ledger, which is the other half.

    When U11 moves delivery to its own Deployment the figure becomes stale HIGH inside a pass -- the
    worker drains pending deliveries this ledger does not hear about -- so it refuses earlier than
    it needs to. That is the conservative direction and it needs no change; what would need one is
    a second EMITTER.
    """

    def __init__(self, count: Callable[[str, int], int]) -> None:
        self._lock = threading.Lock()
        self._count = count
        self._pending: dict[str, int] = {}

    def charge(self, destination_id: str, ceiling: int) -> int | None:
        """Reserve one pending slot, answering how many were held BEFORE it, or ``None`` to refuse.

        The pre-charge figure rather than a bare ``True`` so the caller can hand the store a real
        number: `notification_store.commit_event` takes it as `pending` and spends no aggregation
        of its own.
        """
        with self._lock:
            if destination_id not in self._pending:
                # INSIDE the lock, and the obvious tidy-up of counting outside it would be wrong:
                # a count released and re-taken lets the same destination be counted twice, and the
                # second answer overwrites the first one's charges.
                self._pending[destination_id] = self._count(destination_id, ceiling)
            held = self._pending[destination_id]
            if held >= ceiling:
                return None
            self._pending[destination_id] = held + 1
            return held

    def refund(self, destination_id: str) -> None:
        with self._lock:
            if destination_id in self._pending:
                self._pending[destination_id] = max(0, self._pending[destination_id] - 1)

    def held(self, destination_id: str) -> int:
        with self._lock:
            return self._pending.get(destination_id, 0)


def _close_run(
    alert_id: str,
    known: _Standing,
    *,
    now: datetime,
    budget: _EmitBudget,
    counts: _Counts,
) -> None:
    """This train cleared the gate again: close any open withdrawal run. Usually writes nothing.

    **What makes the withdrawal count truly CONSECUTIVE, and why it lives here rather than in the
    store.** `notification_store._withdraw_body` cannot see an alert-worthy cycle at all -- `skip_reason`
    skips the store call on one, deliberately, to save a transaction per standing subject per cycle
    -- so the store closes runs by TIME (`WITHDRAWAL_GAP_S`), and that approximation still withdraws
    on a dip / one recovered cycle / dip inside the window. A wrong withdrawal is a real
    notification («varselet er trukket tilbake»), it is cap-exempt so nothing suppresses it, and the
    next cycle re-alerts -- notification flapping, which this repo has rejected before.

    The cost contract, because the cheap version of this is a write per standing cycle and that is
    what the whole `skip_reason` optimisation exists to avoid:

    * **an ordinary standing cycle writes nothing** -- `open_runs` is consulted first and is empty
      for a subject with no open run, which is nearly all of them;
    * **one write per genuine recovery**, not per cycle;
    * **no per-subject read, ever** -- `open_runs` rides on the `client_alerts` query `_standing`
      already spends, so a failed pre-read simply loses the resets and falls back to the store's
      time window (R20: an outage costs the optimisation, never correctness);
    * a run already older than the gap is left alone, since the store would start a fresh run for
      the next dip anyway and the write would buy nothing.

    Budgeted like a mint, because it IS a store transaction and the deadline is a reservation
    rather than a check. Its own counter, so a deferred clear never reads as a suppressed mint.
    """
    at = known.open_runs.get(alert_id)
    if at is None:
        return
    if (now - at).total_seconds() > notification_store.WITHDRAWAL_GAP_S:
        counts.add("run_stale")
        return
    if not budget.claim():
        counts.add("run_clear_capped")
        return
    try:
        if notification_store.clear_below_threshold(alert_id, now=now):
            counts.add("run_cleared")
    except Exception:
        # Never fatal and never re-raised: what a failed clear costs is the exactness of ONE
        # subject's withdrawal count, and the store's time window is still underneath it.
        counts.add("run_clear_error")
        logger.warning(
            "Could not close a withdrawal run; the store's time window still applies",
            exc_info=True,
            extra={"fields": {"event": "notification_emit", "result": "run_clear_error"}},
        )


@dataclass(frozen=True)
class _Standing:
    """What stands per subscription, and whether that is a claim or a guess.

    ``complete`` is the load-bearing half: "absent" may only mean "nothing stands" when the answer
    enumerates everything. Treating a subset as complete would skip a withdrawal that was owed and,
    worse, skip a ``confirmed`` mint for a train ET has cancelled -- so an incomplete answer falls
    back to asking the store per subject, which is the slow path and the only correct one.

    Since U9 the answer comes from :class:`notification_store.SubjectMirror`, which is complete by
    construction, so ``complete`` is False in exactly one state: the restart window before the
    mirror has finished seeding. It is KEPT rather than dropped because that window is real -- a
    pod comes up cold on every deploy -- and because a flag whose only value is True is a flag
    nobody notices has stopped being checked.
    """

    phases: Mapping[str, str]
    complete: bool
    #: alert_id -> `below_threshold_at` for every subject with an OPEN withdrawal run, from the
    #: whole standing set rather than per client: the run is a property of the SUBJECT and the
    #: caller looks up the one alert id it is about. Read once per pass, not once per subject.
    open_runs: Mapping[str, datetime] = field(default_factory=dict)


_INCOMPLETE = _Standing(phases={}, complete=False)


def _standing(subscription_ids: Iterable[str], now: datetime) -> dict[str, _Standing]:
    """Which subjects stand, in which phase -- off the mirror, one lookup per SUBSCRIPTION.

    No store read at all since U9, and therefore no per-client failure mode: the whole
    client-scoped query it replaces is gone, along with the truncation that made its answer
    incomplete for exactly the clients this has to scale for. What survives is the fallback, and
    the one state that reaches it is an unseeded mirror -- ``sync_mirrors`` runs as its own poller
    step before the pass, so that is the restart window and nothing else.

    **Per subscription and not per recipient**, which is why it takes ids rather than recipients: a
    subscription watching thirteen disrupted departures appears thirteen times in the fan-out, and
    ``phases_for`` is a lock, a list copy and a dict build every time -- so asking per recipient
    would spend twelve of those thirteen answers on a dict comprehension that overwrites them. At
    100 000 subscriptions on one corridor that is the difference between 100 000 lookups and 1.3
    million.

    ``open_runs`` is asked ONCE and shared for the same reason, one step further: it walks the
    whole standing set, so per subscription it would be quadratic in a structure built to keep the
    pass linear.
    """
    mirror = notification_store.subject_mirror()
    ids = sorted(set(subscription_ids))
    if not mirror.ready:
        return dict.fromkeys(ids, _INCOMPLETE)
    runs = mirror.open_runs(now)
    return {
        subscription_id: _Standing(
            phases=mirror.phases_for(subscription_id, now),
            complete=True,
            open_runs=runs,
        )
        for subscription_id in ids
    }


def _sort_key(observation: WebhookObservation) -> tuple:
    """Alert-worthy first, then soonest departure, then a stable tie-break.

    Soonest-departure-first is ``/varsel``'s own rule for spending a bounded budget
    (`_MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY`, and the write queue since 2026-08-21), and the reason
    is the same here: when the budget binds it must be spent on the trains a traveller can still
    act on. Alert-worthy ahead of below-threshold so a squeezed cycle never spends its last
    transaction retracting a warning instead of raising one.

    **It orders TRAINS since U9, where it used to order (recipient, train) pairs**, and the
    subscription id has left the key with them. That is a sharper statement of the same rule rather
    than a weakening of it: urgency is a property of the departure, so within one train every
    recipient is equally urgent and the order among them is whatever the standing set hands over --
    deterministic, and carrying no meaning it would be wrong to read into it. What the old key did
    with `subscription_id` in third place was group one subscription's trains together inside an
    identical departure instant, which nothing depended on.

    ``.timestamp()`` rather than the datetime, because the key must be total and comparing an aware
    datetime with a naive one raises; an observation with no departure sorts last and keeps its
    relative order, which :func:`train_ref_of` will refuse anyway.
    """
    return (
        not observation.alertworthy,
        observation.departure_time is None,
        observation.departure_time.timestamp() if observation.departure_time else 0.0,
        observation.journey_ref or "",
        observation.stop_order if observation.stop_order is not None else -1,
        observation.operator or "",
    )


def _emit_one(
    subject: WebhookSubject,
    *,
    train: notification_store.TrainRef,
    phase: str,
    now: datetime,
    max_pending: int | None = None,
    pending: int | None = None,
) -> notification_store.EventCommit:
    """One observation, committed. The valid_until note is the only judgement here.

    R12's validity is "the earlier of a 60-minute horizon, the departure, and the subscription's
    lifetime", and only the first two are available: ``Subscription.as_recipient_document`` does not
    carry the subscription's own expiry, so the pass never sees it. The third term is enforced
    DURABLY instead, and strictly more tightly than a snapshot could -- ``claim_delivery`` re-reads
    the live subscription immediately before dispatch and cancels a delivery whose subscription has
    expired (R13). So the horizon on the wire can be up to the subscription's remaining life too
    long, and nothing is ever *sent* past it. Stated rather than hidden because U8 publishes the
    horizon as a number.

    The departure term is the EXPECTED departure (:attr:`WebhookSubject.expected_departure_time`,
    the aimed time when none is carried), and never earlier than :data:`DEPARTURE_GRACE_S` past
    ``now`` -- see both for why. :func:`notification_store.event_validity` itself is untouched: it
    takes the terms it is given, and the synthetic test-event route gives it others.
    """
    departure = subject.expected_departure_time or subject.departure_time
    if departure is not None:
        if departure.tzinfo is None:
            departure = departure.replace(tzinfo=UTC)
        floor = now + timedelta(seconds=DEPARTURE_GRACE_S)
        departure = max(departure, floor)
    request = notification_store.EmitRequest(
        subscription_id=subject.subscription_id,
        client_id=subject.client_id,
        destination_id=subject.destination_id,
        subscription_version=int(subject.subscription_version),
        train=train,
        phase=phase,
        situations=tuple(subject.situations),
        payload=dict(subject.payload),
        valid_until=notification_store.event_validity(
            occurred_at=now,
            departure_at=departure,
            subscription_expires_at=None,
        ),
        external_ref=subject.external_ref,
        test=bool(subject.test),
    )
    if subject.alertworthy:
        return notification_store.commit_event(
            request, max_pending=max_pending, pending=pending, now=now
        )
    return notification_store.record_below_threshold(request, now=now)


def _run(
    observations: Sequence[WebhookObservation],
    *,
    now: datetime,
    deadline_s: float,
    worst_case_s: float,
    max_calls: int,
    workers: int = _EMIT_WORKERS,
    pending_quota: Callable[[str], int] | None = None,
    beat: Callable[[], None] | None = None,
    release: Callable[[], None] | None = None,
) -> EmitReport:
    """The emit loop itself: decide serially, commit concurrently, bounded twice.

    **The split is the whole of U9 step 3.** Everything that decides whether a store call is owed
    -- the sort, the train identity, the standing set, the skip rule, the destination's pending
    ceiling -- happens on THIS thread, in microseconds per recipient, with no lock needed because
    there is one of it. Only the transaction is submitted. The alternative shapes are both wrong in
    a way that is easy to miss: a whole observation as one task makes a 100 000-recipient corridor
    a single task and defeats the pool entirely, and deciding inside the workers would put the
    standing set and the ledger behind a lock that every one of them takes.

    Submission order IS the sorted order, so soonest-departure-first survives as admission order
    even though completion order does not.

    **The budget is claimed inside the worker as its task starts, never at submit time.** Claimed
    at submit, every task is admitted before the deadline is consulted and the bound guarantees
    nothing -- `/meldinger`'s recorded lesson, and the one line here that is easy to move and fatal
    to move.

    ``release`` is the pass lock's, released HERE and not in ``emit``'s own ``finally``: the caller
    may have given up on this thread long ago, and releasing there would let the next cycle start a
    second pass over the store while this one's abandoned workers are still committing.
    """
    try:
        return _run_pass(
            observations,
            now=now,
            deadline_s=deadline_s,
            worst_case_s=worst_case_s,
            max_calls=max_calls,
            workers=workers,
            pending_quota=pending_quota,
            beat=beat,
        )
    finally:
        # OUTSIDE everything, because everything is inside: `_standing`, the ledger's construction
        # and the pool's own are all above the loop's `try`, and a raise in any of them would
        # otherwise leave the module lock held for the life of the pod -- every later cycle
        # answering `overlap_refused` with nothing draining.
        if release is not None:
            release()


def _run_pass(
    observations: Sequence[WebhookObservation],
    *,
    now: datetime,
    deadline_s: float,
    worst_case_s: float,
    max_calls: int,
    workers: int,
    pending_quota: Callable[[str], int] | None,
    beat: Callable[[], None] | None,
) -> EmitReport:
    """:func:`_run` without the lock, so the lock's release cannot depend on any of this."""
    counts = _Counts()
    ledger_of_mints = _Ledger()
    deadline = monotonic() + deadline_s
    budget = _EmitBudget(max_calls, deadline, worst_case_s)
    standing = _standing(
        (r.subscription_id for observation in observations for r in observation.recipients), now
    )
    # One lookup per CLIENT per pass, not one per subject. The resolver behind it is a cached
    # account read, but a pass touches tens of thousands of subjects across a handful of clients and
    # a cache hit is still a lock acquisition and a dict walk apiece.
    quotas: dict[str, int] = {}

    def _pending_ceiling(client_id: str) -> int:
        if client_id not in quotas:
            quotas[client_id] = (
                notification_store.MAX_PENDING_EVENTS_PER_DESTINATION
                if pending_quota is None
                else int(pending_quota(client_id))
            )
        return quotas[client_id]

    ledger = _PendingLedger(
        lambda destination_id, ceiling: notification_store.count_pending_events(
            destination_id, ceiling=ceiling
        )
    )
    pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="client-emit")
    futures: list[Any] = []
    #: Destinations whose pending-event ceiling could not be established this pass. Pass-local for
    #: the reason KTD6 gives about the delivery skip: the answer is the same for every recipient on
    #: the destination right now, and a ceiling remembered across cycles would keep refusing one
    #: whose store has since recovered.
    uncountable: set[str] = set()

    def _commit(
        observation: WebhookObservation,
        recipient: Recipient,
        *,
        train: notification_store.TrainRef,
        phase: str,
        known: _Standing,
        alert_id: str,
        ceiling: int,
        held: int,
    ) -> None:
        if not budget.claim():
            ledger.refund(recipient.destination_id)
            counts.add("capped")
            return
        # MATERIALISED HERE AND NOWHERE EARLIER. Everything on the coordinator is decided from the
        # observation and six strings; this is the first line that builds a per-recipient object,
        # and it is reached only where a store call is actually going to happen.
        subject = observation.subject(recipient)
        try:
            commit = _emit_one(
                subject, train=train, phase=phase, now=now, max_pending=ceiling, pending=held
            )
        except notification_store.QuotaExceeded:
            # **UNREACHABLE FROM THIS PASS, and kept anyway.** `_emit_one` hands the store the
            # ledger's pre-charge figure, and `charge` only returned it because it was under the
            # ceiling -- so `commit_event`'s own comparison is a restatement of the one made two
            # lines above and cannot fail. What makes it worth keeping is that `commit_event` is
            # public: a caller passing `pending=None` gets the aggregation and the real refusal,
            # and this is the branch that would carry it. Do not read it as a second, independent
            # check on the ledger; the ledger IS the check here.
            ledger.refund(recipient.destination_id)
            counts.add("quota")
            return
        except Exception as exc:
            # `notification_store.is_retryable` and NOT a handler keyed on `google.api_core`
            # exceptions: an exhausted transaction surfaces as a plain `ValueError` after five
            # attempts, and treating that as terminal would mark a contended subject failed and
            # never deliver it.
            ledger.refund(recipient.destination_id)
            bucket = "retryable" if notification_store.is_retryable(exc) else "failed"
            counts.add(bucket)
            logger.warning(
                "Notification event could not be committed (%s); it is re-offered next cycle",
                bucket,
                exc_info=True,
                extra={"fields": {"event": "notification_emit", "result": bucket,
                                  "client_id": recipient.client_id}},
            )
            return
        # THE REFUND, and it is what keeps the ceiling exact rather than merely safe: only a mint
        # that left a NEW pending delivery behind spends a slot. A superseding mint is net zero --
        # `_mint` flips the old delivery to `superseded` in the same transaction -- and every other
        # status wrote no delivery at all.
        if commit.status != "created" or commit.superseded_delivery_id:
            ledger.refund(recipient.destination_id)
        if commit.status == "created":
            ledger_of_mints.add(commit.alert_id, observation.decision_key)
        if commit.status == "created" and commit.event_type == notification_store.EVENT_WITHDRAWN:
            counts.add("withdrawn")
            return
        if commit.status == "unchanged":
            # Same recovery as on the coordinator, reached the long way: the standing set was not
            # ready, so the store was asked and said "no transition". A mint would have closed the
            # run itself (`_mint` writes both counter fields), which is why only `unchanged` needs
            # this -- on either path. Inline rather than submitted, because this worker is already
            # on the pool and the budget bounds it either way.
            _close_run(alert_id, known, now=now, budget=budget, counts=counts)
        counts.add(commit.status)

    try:
        submitted = 0
        for observation in sorted(observations, key=_sort_key):
            # ONCE PER TRAIN, not once per recipient: the train identity, its `TrainRef` and the
            # phase it asks for are properties of the observation, and at 100 000 subscriptions on
            # one corridor the difference is four function calls against four hundred thousand.
            train = train_ref_of(
                operator=observation.operator,
                line=observation.line,
                journey_ref=observation.journey_ref,
                stop_place=observation.stop_place,
                stop_order=observation.stop_order,
                departure_time=observation.departure_time,
            )
            if train is None:
                counts.add("unidentified", len(observation.recipients))
                continue
            phase = (
                notification_store.PHASE_CONFIRMED
                if observation.cancelled
                else notification_store.PHASE_PREDICTED
            )
            for recipient in observation.recipients:
                known = standing.get(recipient.subscription_id, _INCOMPLETE)
                # Hoisted out of the `complete` branch: the withdrawal-run reset below needs it on
                # both paths, and it is a pure hash of two strings.
                alert_id = notification_store.alert_subject_id(recipient.subscription_id, train)
                if known.complete:
                    reason = _skip_reason_for(
                        known.phases.get(alert_id),
                        alertworthy=observation.alertworthy,
                        phase=phase,
                    )
                    if reason is not None:
                        if reason == "unchanged":
                            # THE recovery case: alert-worthy, standing in the same phase, minting
                            # nothing -- so the store is about to hear nothing about a cycle that
                            # proves any open withdrawal run is broken. This is the one place that
                            # can tell it. Submitted, because it IS a store transaction.
                            futures.append(
                                pool.submit(
                                    _close_run,
                                    alert_id,
                                    known,
                                    now=now,
                                    budget=budget,
                                    counts=counts,
                                )
                            )
                        counts.add(reason)
                        continue
                if recipient.destination_id in uncountable:
                    counts.add("retryable")
                    continue
                try:
                    ceiling = _pending_ceiling(recipient.client_id)
                    held = ledger.charge(recipient.destination_id, ceiling)
                except Exception:
                    # **GUARDED, because both of these moved onto the coordinator in U9 and were
                    # inside the per-subject `try` before it.** The quota resolver is a cached
                    # account read and the ledger's first sight of a destination is a Firestore
                    # aggregation with a 4 s deadline -- and a 100 000-subscription morning is
                    # exactly when that deadline gets hit. Unguarded, one blip raises out of this
                    # loop, through `_run`, into `emit`'s handler, which reports the whole cycle as
                    # a timeout and discards the counts of every transaction already committed.
                    #
                    # Once per DESTINATION, in the shape the pass-local skip set already had: the
                    # answer is the same for every recipient on it, so the rest are counted as
                    # retryable without asking again, and the next cycle is the retry.
                    uncountable.add(recipient.destination_id)
                    counts.add("retryable")
                    logger.warning(
                        "Could not establish a destination's pending-event ceiling; its "
                        "observations are re-offered on the next cycle",
                        exc_info=True,
                        extra={"fields": {"event": "notification_emit", "result": "ceiling_error",
                                          "client_id": recipient.client_id}},
                    )
                    continue
                if held is None:
                    # R21's ceiling, refused before a transaction is spent discovering it. Charged
                    # on the coordinator, where there is one thread and so no contention for the
                    # decision; refunded by the worker for everything that did not leave a pending
                    # delivery behind.
                    counts.add("quota")
                    continue
                futures.append(
                    pool.submit(
                        _commit,
                        observation,
                        recipient,
                        train=train,
                        phase=phase,
                        known=known,
                        alert_id=alert_id,
                        ceiling=ceiling,
                        held=held,
                    )
                )
                submitted += 1
                if beat is not None and submitted % _HEARTBEAT_EVERY == 0:
                    _beat_quietly(beat)
        # THE COLLECTION IS BOUNDED TOO, and it has to be: `notification_store` plumbs a deadline
        # into every READ, a transaction's reads included, but a transactional COMMIT carries none
        # -- so a worker parked in one would hold an unbounded wait here forever, and with it the
        # module lock that makes the next cycle refuse as `overlap_refused`. Stragglers are
        # abandoned, exactly as the outer wait already abandons this thread: an abandoned
        # transaction that later commits is a committed event nothing delivered this cycle, and the
        # next cycle's `pending_deliveries` finds it. At most `workers` of them can be in flight
        # across a cycle boundary, which is what the refusal above bounds to one cycle's worth.
        _, unfinished = wait(futures, timeout=max(0.0, deadline - monotonic()))
        counts.add("abandoned", len(unfinished))
    finally:
        pool.shutdown(wait=False)
        if beat is not None:
            _beat_quietly(beat)
    return EmitReport(
        created=counts.get("created"),
        unchanged=counts.get("unchanged"),
        withdrawn=counts.get("withdrawn"),
        counting=counts.get("counting"),
        obsolete=counts.get("obsolete"),
        unidentified=counts.get("unidentified"),
        nothing_standing=counts.get("nothing_standing"),
        quota=counts.get("quota"),
        capped=counts.get("capped"),
        retryable=counts.get("retryable"),
        failed=counts.get("failed"),
        run_cleared=counts.get("run_cleared"),
        run_stale=counts.get("run_stale"),
        run_clear_capped=counts.get("run_clear_capped"),
        run_clear_error=counts.get("run_clear_error"),
        abandoned=counts.get("abandoned"),
        minted=ledger_of_mints.frozen(),
        minted_decisions=ledger_of_mints.frozen_decisions(),
    )


def _beat_quietly(beat: Callable[[], None]) -> None:
    """Beat the poller's heartbeat, and never let the seam cost the pass.

    `varsel._heartbeat`'s rule: the heartbeat is an availability signal, so a provider that raises
    must not unwind a pass that is otherwise healthy.
    """
    try:
        beat()
    except Exception:
        logger.warning("Heartbeat seam failed; continuing the emit pass", exc_info=True)


#: Held for the whole of one pass, including the part of it this thread has walked away from.
#: See :func:`emit` for what a second pass over the same store would cost.
_pass_lock = threading.Lock()


def wait_until_idle(timeout_s: float) -> bool:
    """Wait up to `timeout_s` for any pass still draining -- abandoned workers included -- to end.

    For the poller lease's shutdown and loss (zero-downtime U2, KTD7): the subject mirror learns only
    from this process's own commits, so the lease is handed on only after this process has stopped
    committing, or after the bound. Taken and released at once; True when the pass lock was free.
    """
    if not _pass_lock.acquire(timeout=max(0.0, timeout_s)):
        return False
    _pass_lock.release()
    return True


def emit(
    observations: Sequence[WebhookObservation],
    *,
    now: datetime | None = None,
    deadline_s: float = _EMIT_DEADLINE_S,
    wait_s: float = _EMIT_WAIT_S,
    worst_case_s: float = _WORST_CASE_EMIT_S,
    max_calls: int = _MAX_EMITS_PER_CYCLE,
    workers: int = _EMIT_WORKERS,
    pending_quota: Callable[[str], int] | None = None,
    beat: Callable[[], None] | None = None,
) -> EmitReport:
    """Commit this cycle's notification events. Never raises for a store problem.

    ``now`` is captured ONCE, in **UTC**, by the caller of every transaction body -- which is what
    makes ``revision`` monotonic structurally rather than as a property of the clock. Three things
    that depend on it, and each fails silently if it is got wrong. An Oslo-formatted revision goes
    backwards for an hour at every autumn DST fall-back, and a receiver ordering on ``revision``
    would then discard every event for those subjects, a confirmed cancellation included. An NTP
    step backwards does the same for an unbounded stretch, which is why the seed is only a seed and
    the store clamps to ``max(stored + 1, seed)`` inside the transaction. And a clock read *inside*
    a body that may run more than once makes ``event_id`` irreproducible, so a retried body would
    mint a second event the receiver cannot recognise as the same one.

    The whole loop runs on a one-wide worker whose result is awaited for ``wait_s``. The executor is
    deliberately not a context manager -- ``__exit__`` calls ``shutdown(wait=True)``, which would
    block the poller until the slow call finally returned and defeat the timeout entirely.
    ``shutdown(wait=False)`` abandons the thread, which is the honest trade: Python cannot cancel a
    thread parked in a socket read, and one leaked thread per bad cycle is far cheaper than a
    stalled ingestion thread. An abandoned transaction that later commits is a committed event
    nothing delivered this cycle; the next cycle's ``pending_deliveries`` finds it, because the
    pending set is the store's rather than this cycle's.

    ``pending_quota`` answers "how many pending events may this client's destination hold" from a
    ``client_id``. A CALLABLE handed in rather than a lookup made here, and the reason is the import
    set this suite pins exactly: the answer lives in ``notification_auth``, which imports
    ``notification_store`` as this module does, and the edge would make every process that emits
    also carry the credential machinery. ``None`` keeps the module constant for every client, which
    is what every test that does not care about quotas gets.

    **A second pass cannot start while the first is still draining** (U9). The store's
    pending-event ceiling is exact because one pass holds the whole count
    (:class:`_PendingLedger`); two overlapping passes would each hold their own and admit past a
    destination's ceiling together, and the abandoned workers of the first are exactly the ones
    still writing. So the module lock is taken here without blocking and released by ``_run``'s
    own ``finally`` -- not by this thread, which may have walked away from ``_run`` long before it
    finishes. A refused cycle is reported as ``overlap_refused`` and its work is re-offered on the
    next one, which is what every other bound here does too.
    """
    if not observations:
        return EmitReport()
    if not _pass_lock.acquire(blocking=False):
        logger.warning(
            "The previous notification emit pass is still draining; this cycle's observations are "
            "re-offered on the next one",
            extra={"fields": {"event": "notification_emit", "result": "overlap_refused"}},
        )
        return EmitReport(overlap_refused=True)
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    started = monotonic()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="client-emit-run")
    released = False
    try:
        future = pool.submit(
            _run,
            observations,
            now=instant,
            deadline_s=deadline_s,
            worst_case_s=worst_case_s,
            max_calls=max_calls,
            workers=workers,
            pending_quota=pending_quota,
            beat=beat,
            release=_pass_lock.release,
        )
        released = True
        report = future.result(timeout=wait_s)
        timed_out = False
    except Exception:
        logger.warning(
            "Notification event emission did not finish inside its budget; this cycle's remaining "
            "observations are re-offered on the next one",
            exc_info=True,
            extra={"fields": {"event": "notification_emit", "result": "timeout"}},
        )
        report = EmitReport()
        timed_out = True
    finally:
        pool.shutdown(wait=False)
        if not released:
            # The submit itself raised, so `_run` never started and nothing will release for us.
            _pass_lock.release()

    ms = int((monotonic() - started) * 1000)
    report = replace(report, timed_out=timed_out, ms=ms)
    logger.info(
        "Client emit: %d created, %d withdrawn, %d unchanged, %d skipped in %d ms",
        report.created,
        report.withdrawn,
        report.unchanged,
        report.nothing_standing + report.unidentified,
        ms,
        extra={
            "fields": {
                "event": "notification_emit",
                "result": "timeout" if timed_out else "ok",
                "created": report.created,
                "withdrawn": report.withdrawn,
                "unchanged": report.unchanged,
                "counting": report.counting,
                "obsolete": report.obsolete,
                "nothing_standing": report.nothing_standing,
                "unidentified": report.unidentified,
                "quota": report.quota,
                "capped": report.capped,
                # Not notification decisions: these say how exact the next withdrawal count is.
                "run_cleared": report.run_cleared,
                "run_stale": report.run_stale,
                "run_clear_capped": report.run_clear_capped,
                "run_clear_error": report.run_clear_error,
                "retryable": report.retryable,
                "failed": report.failed,
                "abandoned": report.abandoned,
                "overlap_refused": report.overlap_refused,
                # `n` keeps its meaning -- the (recipient, train) observations this cycle offered
                # -- so a rate computed across U9 still measures one thing. `trains` is what the
                # fan-out is over, and the ratio between them is the corridor's sharing factor.
                "n": sum(len(o.recipients) for o in observations),
                "trains": len(observations),
                "ms": ms,
            }
        },
    )
    return report


__all__ = [
    "EmitReport",
    "Recipient",
    "WebhookObservation",
    "WebhookSubject",
    "emit",
    "requested_phase",
    "skip_reason",
    "train_ref",
]
