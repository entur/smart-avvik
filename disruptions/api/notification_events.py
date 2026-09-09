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
~300 cycles in a day. So one client-scoped query (:func:`notification_store.client_alerts`) answers
"which subjects stand, and in which phase" for the whole cycle, and the transactions are spent only
where a write can actually happen. That is reading the store to decide what to write -- not
re-deriving the pending set -- and it is exact rather than best-effort for the reason KTD6 names:
the emit path has exactly one writer (one poller thread, no route invokes the pass, one replica
under the Recreate strategy), so nothing can change a subject between the query and the commit.

The pre-read has one honest limit and it is handled rather than assumed. ``client_alerts`` serves
at most :data:`notification_store.MAX_ALERTS_SERVED` subjects per subscription in its LIST while counting
all of them, so a subscription holding more standing alerts than that -- AE3's line suspension,
thirteen cancelled departures inside one window -- reports a count this module cannot enumerate.
:class:`_Standing` calls that *incomplete* and falls back to asking the store per subject, which is
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
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
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
# Sized against the phase, not against this step: `webhook_delivery` reserves 20 s (KTD6's number,
# not ours to move) and the two run in one `main._phase`, whose measured `ms` is the number KTD6's
# reversal trigger reads -- "move delivery out when the webhook phase exceeds 30 s of measured phase
# time on any cycle in a week". 8 + 20 = 28 s of ceiling leaves that trigger meaning what it says.
_EMIT_DEADLINE_S = 6.0
_EMIT_WAIT_S = 8.0
# One bounded Firestore transaction, reserved up front. `commit_event` is a count aggregation plus a
# transaction the client retries up to five times, so this is a budgeting assumption and not a
# guarantee -- which is precisely why the outer wait exists as well.
_WORST_CASE_EMIT_S = 2.0

#: What one cycle may write, on top of the clock. A count as well as a clock for
#: `varsel._WriteBudget`'s reason: the two answer different questions, and a caller must not be able
#: to satisfy one and ignore the other. At the pilot's "a few hundred events a day" (KTD6) this is
#: never reached; it is the bound that makes a store bug or a matcher regression cost one cycle's
#: tail instead of the cycle.
_MAX_EMITS_PER_CYCLE = 120

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
    if subject.alertworthy:
        if notification_store._transition(stored_phase, requested_phase(subject)) is None:
            return "unchanged"
        return None
    if stored_phase in (None, notification_store.PHASE_WITHDRAWN):
        return "nothing_standing"
    return None


def _close_run(
    alert_id: str,
    known: _Standing,
    *,
    now: datetime,
    budget: _EmitBudget,
    counts: dict[str, int],
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
        counts["run_stale"] = counts.get("run_stale", 0) + 1
        return
    if not budget.claim():
        counts["run_clear_capped"] = counts.get("run_clear_capped", 0) + 1
        return
    try:
        if notification_store.clear_below_threshold(alert_id, now=now):
            counts["run_cleared"] = counts.get("run_cleared", 0) + 1
    except Exception:
        # Never fatal and never re-raised: what a failed clear costs is the exactness of ONE
        # subject's withdrawal count, and the store's time window is still underneath it.
        counts["run_clear_error"] = counts.get("run_clear_error", 0) + 1
        logger.warning(
            "Could not close a withdrawal run; the store's time window still applies",
            exc_info=True,
            extra={"fields": {"event": "notification_emit", "result": "run_clear_error"}},
        )


@dataclass(frozen=True)
class _Standing:
    """What stands per subscription, and whether that is a claim or a guess.

    ``complete`` is the load-bearing half. ``client_alerts`` counts every live subject and
    enumerates at most :data:`notification_store.MAX_ALERTS_SERVED` of them, so past that ceiling the
    phases below are a subset and "absent" no longer means "nothing stands". Treating a subset as
    complete would skip a withdrawal that was owed and, worse, skip a ``confirmed`` mint for a train
    ET has cancelled -- so an incomplete answer falls back to asking the store per subject, which is
    the slow path and the only correct one.
    """

    phases: Mapping[str, str]
    complete: bool
    #: alert_id -> `below_threshold_at` for this client's subjects with an OPEN withdrawal run,
    #: straight off `ClientAlerts.open_below_threshold`. Carried even when `complete` is False,
    #: because it is derived from the whole live page rather than from the four-row display list --
    #: so a truncated snapshot still resets runs exactly, and only a FAILED read loses them.
    open_runs: Mapping[str, datetime] = field(default_factory=dict)


_INCOMPLETE = _Standing(phases={}, complete=False)


def _standing(subjects: Sequence[WebhookSubject], now: datetime) -> dict[str, _Standing]:
    """One client-scoped query per client: which subjects stand, in which phase.

    Fails to :data:`_INCOMPLETE` for every subscription of a client whose read raised (R20 -- a
    store outage suppresses that client's *optimisation*, never its correctness), which costs
    transactions rather than events.
    """
    out: dict[str, _Standing] = {}
    for client_id in sorted({s.client_id for s in subjects if s.client_id}):
        try:
            alerts = notification_store.client_alerts(client_id, now=now)
        except Exception:
            logger.warning(
                "Could not read standing client alerts; asking the store per subject this cycle",
                exc_info=True,
                extra={"fields": {"event": "notification_emit", "result": "standing_error",
                                  "client_id": client_id}},
            )
            for subject in subjects:
                if subject.client_id == client_id:
                    out[subject.subscription_id] = _INCOMPLETE
            continue
        for subject in subjects:
            if subject.client_id != client_id:
                continue
            snapshot = alerts.snapshots.get(subject.subscription_id)
            if snapshot is None:
                # Absent from a bounded read is "crowded out", not "calm" -- `client_alerts`
                # documents exactly that, which is why the wrapper carries its own `truncated`.
                out[subject.subscription_id] = _Standing(
                    phases={},
                    complete=not alerts.truncated,
                    open_runs=alerts.open_below_threshold,
                )
                continue
            out[subject.subscription_id] = _Standing(
                phases={state.alert_id: state.phase for state in snapshot.subjects},
                complete=not snapshot.truncated and snapshot.n_active <= len(snapshot.subjects),
                open_runs=alerts.open_below_threshold,
            )
    return out


def _sort_key(subject: WebhookSubject) -> tuple:
    """Alert-worthy first, then soonest departure, then a stable tie-break.

    Soonest-departure-first across subscriptions is ``/varsel``'s own rule for spending a bounded
    budget (`_MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY`, and the write queue since 2026-08-21), and the
    reason is the same here: when the budget binds it must be spent on the trains a traveller can
    still act on. Alert-worthy ahead of below-threshold so a squeezed cycle never spends its last
    transaction retracting a warning instead of raising one.

    ``.timestamp()`` rather than the datetime, because the key must be total and comparing an aware
    datetime with a naive one raises; a subject with no departure sorts last and keeps its relative
    order, which :func:`train_ref` will refuse anyway.
    """
    return (
        not subject.alertworthy,
        subject.departure_time is None,
        subject.departure_time.timestamp() if subject.departure_time else 0.0,
        subject.subscription_id,
        subject.journey_ref or "",
        subject.stop_order if subject.stop_order is not None else -1,
    )


def _emit_one(
    subject: WebhookSubject,
    *,
    train: notification_store.TrainRef,
    phase: str,
    now: datetime,
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
        return notification_store.commit_event(request, now=now)
    return notification_store.record_below_threshold(request, now=now)


def _run(
    subjects: Sequence[WebhookSubject],
    *,
    now: datetime,
    deadline_s: float,
    worst_case_s: float,
    max_calls: int,
) -> EmitReport:
    """The emit loop itself, on a worker thread so the caller's wait can bound a hung store call."""
    counts: dict[str, int] = {}
    budget = _EmitBudget(max_calls, monotonic() + deadline_s, worst_case_s)
    standing = _standing(subjects, now)
    # Pass-local, and pass-local for the reason KTD6 gives about the delivery skip: a destination at
    # its pending-event ceiling refuses every further emit with the same answer, so asking again for
    # each of a hundred subjects is a hundred wasted aggregation queries -- but a ceiling remembered
    # across cycles would keep refusing a destination that has since drained, which is
    # indistinguishable from the intended 410 pause.
    over_quota: set[str] = set()

    for subject in sorted(subjects, key=_sort_key):
        train = train_ref(subject)
        if train is None:
            counts["unidentified"] = counts.get("unidentified", 0) + 1
            continue
        if subject.destination_id in over_quota:
            counts["quota"] = counts.get("quota", 0) + 1
            continue
        known = standing.get(subject.subscription_id, _INCOMPLETE)
        # Hoisted out of the `complete` branch: the withdrawal-run reset below needs it on both
        # paths, and it is a pure hash of two strings.
        alert_id = notification_store.alert_subject_id(subject.subscription_id, train)
        if known.complete:
            reason = skip_reason(known.phases.get(alert_id), subject)
            if reason is not None:
                if reason == "unchanged":
                    # THE recovery case: alert-worthy, standing in the same phase, minting nothing
                    # -- so the store is about to hear nothing about a cycle that proves any open
                    # withdrawal run is broken. This is the one place that can tell it.
                    _close_run(alert_id, known, now=now, budget=budget, counts=counts)
                counts[reason] = counts.get(reason, 0) + 1
                continue
        if not budget.claim():
            counts["capped"] = counts.get("capped", 0) + 1
            continue
        try:
            commit = _emit_one(subject, train=train, phase=requested_phase(subject), now=now)
        except notification_store.QuotaExceeded:
            # R21, and visible rather than silent: the store has already logged the breach with its
            # own count. Skipping the rest of this destination's subjects is a cost decision, not a
            # policy one -- they are re-offered on the next cycle.
            over_quota.add(subject.destination_id)
            counts["quota"] = counts.get("quota", 0) + 1
            continue
        except Exception as exc:
            # `notification_store.is_retryable` and NOT a handler keyed on `google.api_core` exceptions:
            # an exhausted transaction surfaces as a plain `ValueError` after five attempts, and
            # treating that as terminal would mark a contended subject failed and never deliver it.
            bucket = "retryable" if notification_store.is_retryable(exc) else "failed"
            counts[bucket] = counts.get(bucket, 0) + 1
            logger.warning(
                "Notification event could not be committed (%s); it is re-offered next cycle",
                bucket,
                exc_info=True,
                extra={"fields": {"event": "notification_emit", "result": bucket,
                                  "client_id": subject.client_id}},
            )
            continue
        if commit.status == "created" and commit.event_type == notification_store.EVENT_WITHDRAWN:
            counts["withdrawn"] = counts.get("withdrawn", 0) + 1
        else:
            if commit.status == "unchanged":
                # Same recovery, reached the long way: the pre-read was truncated or missing, so
                # the store was asked and said "no transition". A mint would have closed the run
                # itself (`_mint` writes both counter fields), which is why only `unchanged` needs
                # this -- on either path.
                _close_run(alert_id, known, now=now, budget=budget, counts=counts)
            counts[commit.status] = counts.get(commit.status, 0) + 1
    return EmitReport(
        created=counts.get("created", 0),
        unchanged=counts.get("unchanged", 0),
        withdrawn=counts.get("withdrawn", 0),
        counting=counts.get("counting", 0),
        obsolete=counts.get("obsolete", 0),
        unidentified=counts.get("unidentified", 0),
        nothing_standing=counts.get("nothing_standing", 0),
        quota=counts.get("quota", 0),
        capped=counts.get("capped", 0),
        retryable=counts.get("retryable", 0),
        failed=counts.get("failed", 0),
        run_cleared=counts.get("run_cleared", 0),
        run_stale=counts.get("run_stale", 0),
        run_clear_capped=counts.get("run_clear_capped", 0),
        run_clear_error=counts.get("run_clear_error", 0),
    )


def emit(
    subjects: Sequence[WebhookSubject],
    *,
    now: datetime | None = None,
    deadline_s: float = _EMIT_DEADLINE_S,
    wait_s: float = _EMIT_WAIT_S,
    worst_case_s: float = _WORST_CASE_EMIT_S,
    max_calls: int = _MAX_EMITS_PER_CYCLE,
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
    """
    if not subjects:
        return EmitReport()
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    started = monotonic()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="client-emit")
    try:
        future = pool.submit(
            _run,
            subjects,
            now=instant,
            deadline_s=deadline_s,
            worst_case_s=worst_case_s,
            max_calls=max_calls,
        )
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
                "n": len(subjects),
                "ms": ms,
            }
        },
    )
    return report


__all__ = [
    "EmitReport",
    "WebhookSubject",
    "emit",
    "requested_phase",
    "skip_reason",
    "train_ref",
]
