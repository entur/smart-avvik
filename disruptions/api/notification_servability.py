"""R6's freshness half: webhook subscriptions whose servability answer has gone stale.

:mod:`notification_api` answers the question ONCE, at create and at replace
(``notification_api._record_servability``): does any single direct trip serve this stop pair today? This
module is the other half of AE13 -- "it is re-checked as service appears" -- and it is the client
channel's copy of ``varsel.recheck_service``, whose whole reason for existing applies here
unchanged. A weekend-only ferry registered on a Friday evening, a seasonal route registered out of
season and a pair whose timetable simply had not been published yet all answer "no direct service"
at registration and start running later. Without a re-check the caveat stands for the life of the
subscription and ``GET /subscriptions`` keeps serving it: a caveat that never clears is the refusal
R5 forbids, with extra steps.

**Four rules, and each is ``varsel.recheck_service``'s.**

1. **A ``None`` verdict is never written.** The seam returns ``None`` for "could not be established"
   -- a BigQuery outage, the 20 s timeout, the webhook channel's own per-minute ceiling on distinct
   pairs. ``varsel`` ``continue``s on it; so does this. The edge is sharper
   here because ``notification_store.set_servability`` accepts ``None`` and would then move
   ``servable_checked_at``: calling it would convert "we do not know" into a freshly-stamped
   non-answer, and one blip into a caveat U4's docstring says must never be rendered as one. So a
   failed check leaves the stored value AND its stamp exactly as they were.
2. **Per record, not per cycle.** :data:`_RECHECK_INTERVAL` bounds each subscription to one ask per
   six hours, so a healthy list costs nothing and an unservable one costs one call per interval.
3. **The stamp moves on every ESTABLISHED answer**, including a ``False`` one that changes nothing.
   That is R6's freshness half, and it is also what takes a still-unservable record out of the due
   set for six hours rather than leaving it at the head of the queue on every cycle.
4. **A record deleted while we were asking is skipped, not resurrected.** ``set_servability``
   refuses a tombstone by reading before it writes; the ``NotFound`` that comes back is an ordinary
   outcome here, exactly as ``varsel``'s "deleted while we were asking" is.

**What is deliberately NOT re-asked: ``servable is True``.** A route that stops running leaves a
stale ``true``, which is a real asymmetry rather than an oversight. Three reasons to leave it.
``varsel`` re-asks only its flagged journeys, and R3's parity claim is about matching its
behaviour. The harm is one-directional: a stale ``false`` renders a caveat about a subscription
that is working, while a stale ``true`` renders nothing at all and the answer's age is published
beside it (``servable_checked_at``), which is precisely what R6 asks for instead of a silent
all-clear. And re-asking every servable subscription is the expensive direction -- O(all
subscriptions) BigQuery questions per interval against O(the unservable ones), which at a pilot's
volume is the difference between nothing and the ceiling below binding every cycle.

**Bounded, in numbers, because each bound protects a different resource.**

* :data:`_MAX_CHECKS_PER_CYCLE` (4) bounds the BigQuery spend and this pass's share of the seam's
  per-minute limit -- since 2026-09-09 the Webhook channel's own
  (`main._NOTIFICATION_API_CONNECTIVITY_MAX_PER_MIN`, 15), not the public unauthenticated endpoint's, so a
  sweep cannot cost a real traveller their registration's no-direct-service caveat. It is the bound
  that binds in ordinary operation.
* :data:`_DEADLINE_S` (30 s) bounds the poller. It RESERVES the worst case
  (:data:`_WORST_CASE_CHECK_S`, the seam's own 20 s timeout) rather than measuring elapsed time --
  ``varsel._WriteBudget``'s shape -- so it bounds when the step ENDS and not when its last question
  starts. One hung question therefore costs 20 s and never 4 x 20 s. It reserves the QUESTION and
  not the store write that follows it, so the step ends within ``_DEADLINE_S`` **plus one
  ``set_servability``**; that constant's own comment carries the arithmetic and the reason the
  residual is left rather than reserved.
* The store read is one page of at most ``notification_store.MAX_SUBSCRIPTIONS_PER_CYCLE`` documents,
  R21's existing ceiling and the same query the notification pass already runs once per cycle. It
  needs no new composite index, which is the second reason to reuse it: a query ordering on
  ``servable_checked_at`` would have to reason about Firestore excluding documents that lack the
  field and sorting null below every timestamp, and the emulator tracks no composite indexes, so
  neither the emulator suite nor a dry run could catch getting that wrong.

When a bound binds it is LOGGED, once per cycle, under ``result`` values of the existing
``notification_servability`` event -- a ceiling nobody can see binding is not a ceiling anybody can
size. And when it binds the order is defensible rather than arbitrary: subscriptions carrying a
user-visible ``false`` caveat before ones that were never established, each group oldest answer
first, a missing stamp counting as oldest.

**One consequence worth naming rather than fixing.** Because rule 1 leaves the stamp alone, a pair
whose check keeps failing stays at the head of the due queue and is re-asked on every cycle, up to
the ceiling. That is ``varsel.recheck_service``'s behaviour exactly, and the seam's own 10-minute
answer cache does not soften it (only established verdicts are cached). The ceiling is what bounds
it; nothing here retires such a record, because retiring it would be a give-up rule that silently
converts a long outage into a permanent non-answer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from time import monotonic

import notification_store

logger = logging.getLogger(__name__)

# ---- Bounds ------------------------------------------------------------------------------------

#: How long an answer stands before it is re-asked. `varsel._SERVICE_RECHECK`'s six hours, restated
#: rather than imported: this module holds no import of `varsel` (147 KB of poller state, and the
#: seam below is injected for exactly that reason), and `test_notification_servability` pins the two
#: values equal so the two channels cannot come to disagree about what "stale" means.
_RECHECK_INTERVAL = timedelta(hours=6)

#: The seam's own ceiling on one question -- `main._CONNECTIVITY_TIMEOUT_S`, pinned equal by
#: `test_poller_refresh`, which imports `main` without entering its lifespan. Restated here because
#: a poller-side module cannot import `main`, and a reservation budget that guesses low would stop
#: being a ceiling the first time a question ran long.
_WORST_CASE_CHECK_S = 20.0

#: At most this many questions per cycle. Four is sized off the cadence rather than picked: at the
#: measured ~296 s cycle period a subscription needs four checks a day to hold rule 2's six-hour
#: interval, so four per cycle carries ~290 permanently-unservable subscriptions at full cadence --
#: two orders of magnitude above a pilot, and still only four BigQuery questions of the client
#: channel's own 15/min share of a limit this module does not own.
_MAX_CHECKS_PER_CYCLE = 4

#: The step's clock, reserved rather than measured, and running from before the store read so what
#: it bounds is the PHASE and not the loop. It has to exceed `_WORST_CASE_CHECK_S` by enough to
#: admit a normal run: at 30 s a question may START while under 10 s have elapsed, which fits
#: `_MAX_CHECKS_PER_CYCLE` questions at the ~2 s a healthy one takes and lets a single hung one end
#: the step at 20 s. Set equal to the worst case and the count ceiling would be decoration --
#: exactly one question could ever start.
#:
#: **What it does NOT reserve, stated rather than implied: the store write that follows an admitted
#: question.** So the true ceiling is this number plus one `set_servability`, whose own worst case is
#: five contended transaction attempts at `notification_store._CALL_DEADLINE_S` each with commits that
#: carry no deadline at all -- unbounded in principle, and only an outer bounded wait on a worker
#: thread could fix that (`notification_events._EMIT_WAIT_S`'s shape, and its residual is a leaked
#: thread). Reserving a guessed write cost instead would push this to ~50 s to keep the same start
#: window, and would still not be a ceiling. This step is a maintenance chore on a ~296 s cycle with
#: ~230 s of headroom, so the proportionate answer is the honest number rather than a bigger one
#: that is equally residual.
_DEADLINE_S = 30.0

# ---- The connectivity seam ---------------------------------------------------------------------
#
# `varsel._service_verdict`, injected exactly as it is into `notification_api`: it expands both stop refs
# to every ref of their place (`varsel.place_refs`) and already fails open to None, which is what
# keeps "we could not establish it" from ever reading as "no trips run". Injected rather than
# imported so this module can be exercised without varsel's module state, and so an unwired
# deployment is a cheap early return instead of a store read.
ConnectivityProvider = Callable[[str, str], "tuple[bool, bool] | None"]

_connectivity: ConnectivityProvider | None = None


def configure(*, connectivity: ConnectivityProvider | None = None) -> None:
    """Wire the seam. Called once from ``main.lifespan``."""
    global _connectivity
    _connectivity = connectivity


def reset_for_tests() -> None:
    global _connectivity
    _connectivity = None


# ---- Which subscriptions are due ---------------------------------------------------------------


def check_due(subscription: notification_store.Subscription, now: datetime) -> bool:
    """Is this subscription's servability answer stale enough to re-ask?

    ``True`` is never due (module docstring). Everything else is, once the stamp is old enough --
    and an absent or unusable stamp is due NOW, which is `varsel._service_check_due`'s rule for the
    same reason: a record whose freshness cannot be read is a record whose answer cannot be trusted,
    and treating it as fresh would hide it from the one pass that could establish it.
    """
    if subscription.servable is True:
        return False
    stamp = subscription.servable_checked_at
    try:
        return stamp is None or (now - stamp) >= _RECHECK_INTERVAL
    except TypeError:
        # A naive datetime from a document written by something other than this binary. Due now.
        return True


def _priority(subscription: notification_store.Subscription) -> tuple[int, float]:
    """The order the ceiling spends itself in: caveat-carrying first, oldest answer first.

    A ``False`` is a caveat a client is being shown right now and the only one AE13's second clause
    is about; a ``None`` was never established and is worth re-asking, but not ahead of one. Within
    a group the oldest answer goes first, and a record with no stamp at all is the oldest there is
    -- otherwise a systematic failure to establish an answer would keep re-sorting the queue and
    the same few records would be asked forever.
    """
    stamp = subscription.servable_checked_at
    try:
        age = stamp.timestamp() if stamp is not None else float("-inf")
    except (AttributeError, TypeError, ValueError, OSError):
        age = float("-inf")
    return (0 if subscription.servable is False else 1, age)


def due_subscriptions(
    subscriptions: Iterable[notification_store.Subscription], now: datetime
) -> list[notification_store.Subscription]:
    """The due set, in the order the ceiling should be spent."""
    return sorted((s for s in subscriptions if check_due(s, now)), key=_priority)


# ---- The pass ----------------------------------------------------------------------------------


def _ask(subscription: notification_store.Subscription) -> tuple[bool, bool] | None:
    """One question through the seam. Never raises: an unanswerable question is not an answer."""
    provider = _connectivity
    if provider is None:  # narrowed for the type checker; recheck() already returned
        return None
    try:
        return provider(subscription.from_stop, subscription.to_stop)
    except Exception:
        logger.warning(
            "Client servability re-check raised; the stored answer and its stamp stand",
            exc_info=True,
            extra={
                "fields": {
                    "event": "notification_servability",
                    "result": "error",
                    "client_id": subscription.client_id,
                    "subscription_id": subscription.subscription_id,
                }
            },
        )
        return None


def _record(subscription: notification_store.Subscription, servable: bool) -> bool:
    """Write an ESTABLISHED answer. ``True`` when it landed.

    Guarded per record rather than per pass (R20): a subscription deleted between the read and the
    write is an ordinary outcome, and one failing write must not cost the pass the records behind
    it.

    The version is the one the QUESTION was asked about -- the answer is about a ROUTE, and a
    replace during a 20 s check would otherwise stamp the new route with the old route's verdict,
    freshly timestamped. Same guard `notification_api._record_servability` uses, for the same race.
    """
    try:
        notification_store.set_servability(
            subscription.subscription_id,
            servable=servable,
            expected_version=subscription.version,
        )
        return True
    except notification_store.NotFound:
        # Deleted or tombstoned while we were asking. `set_servability` reads before it writes, so
        # nothing was resurrected; there is simply nothing left to stamp.
        logger.debug(
            "Webhook subscription vanished during its servability re-check; skipping it",
            extra={
                "fields": {
                    "event": "notification_servability",
                    "result": "vanished",
                    "client_id": subscription.client_id,
                    "subscription_id": subscription.subscription_id,
                }
            },
        )
        return False
    except notification_store.Conflict:
        # Replaced while we were asking. The newer definition retired the answer and its stamp on
        # purpose; a due record with nothing established is picked up on the next cadence, so this
        # needs no repair -- only a name, so the counter does not read it as a write failure.
        logger.debug(
            "A webhook subscription was replaced during its servability re-check; verdict dropped",
            extra={
                "fields": {
                    "event": "notification_servability",
                    "result": "superseded",
                    "client_id": subscription.client_id,
                    "subscription_id": subscription.subscription_id,
                }
            },
        )
        return False
    except Exception:
        logger.warning(
            "Client servability write failed; the record keeps its previous caveat",
            exc_info=True,
            extra={
                "fields": {
                    "event": "notification_servability",
                    "result": "write_failed",
                    "client_id": subscription.client_id,
                    "subscription_id": subscription.subscription_id,
                }
            },
        )
        return False


def _load(now: datetime) -> notification_store.LoadedSubscriptions | None:
    """This environment's live subscriptions, or ``None`` when the store could not be read."""
    try:
        return notification_store.active_subscriptions(now=now)
    except Exception:
        logger.warning(
            "Webhook subscriptions could not be read for the servability re-check; caveats stand",
            exc_info=True,
            extra={"fields": {"event": "notification_servability", "result": "load_failed"}},
        )
        return None


def recheck(*, now: datetime | None = None) -> None:
    """Re-ask about webhook subscriptions whose servability answer is stale, and clear the ones
    that now have service.

    Called from the poller's refresh cycle, in its own phase after ``notification_webhooks``. Costs one
    bounded store read on every cycle and nothing else on a healthy list. ``now`` is injectable for
    the same reason every ``notification_store`` entry point's is: staleness is the whole rule here, and
    a suite that could not pin the clock could only assert it by sleeping.
    """
    if _connectivity is None:
        # No seam wired: nothing could be established anyway, so this must not spend a store read
        # to discover that. Same shape as `notification_api._record_servability`'s own first line.
        return
    # The clock starts BEFORE the load, not at the first question. The load is a Firestore stream
    # and part of what the phase spends, so a deadline measured from after it would bound the loop
    # while the phase ran longer -- and the summary's `ms` would report a step that is not the one
    # `poller_phase` times.
    started = monotonic()
    # ONE instant for both halves, the shape `impact._impact_response` uses: the store decides
    # liveness at it and the staleness test below reads it, so an expiry and a re-check cannot
    # straddle two clocks.
    instant = now or datetime.now(UTC)
    loaded = _load(instant)
    if loaded is None:
        return
    due = due_subscriptions(loaded.subscriptions, instant)
    if not due:
        return
    _sweep(due, truncated=loaded.truncated, started=started)


def _sweep(
    due: Sequence[notification_store.Subscription], *, truncated: bool, started: float
) -> None:
    deadline = started + _DEADLINE_S
    checked = cleared = still_unservable = 0
    bound: str | None = None
    for subscription in due:
        if checked >= _MAX_CHECKS_PER_CYCLE:
            bound = "capped"
            break
        if monotonic() + _WORST_CASE_CHECK_S > deadline:
            # RESERVED, not measured, so this bounds when the pass ENDS rather than when its last
            # question starts. What it reserves is the SEAM's worst case and not the write that
            # follows it -- see `_DEADLINE_S` for the residual that leaves and why it is not
            # reserved too.
            bound = "deadline"
            break
        checked += 1
        verdict = _ask(subscription)
        if verdict is None:
            continue  # rule 1: the stored value and its stamp are left exactly as they were
        # Only the forward direction decides the caveat. `verdict[1]` is the reverse-service
        # reading /varsel words a likely from/to swap from, and the client record has nowhere to
        # carry it: `set_servability` takes one tri-state, and inventing a second field here would
        # publish a hint U4 never contracted.
        servable = bool(verdict[0])
        if not _record(subscription, servable):
            continue
        if servable:
            cleared += 1
        else:
            still_unservable += 1
    ms = int((monotonic() - started) * 1000)
    fields = {
        "event": "notification_servability",
        "result": bound or "swept",
        "due": len(due),
        "checked": checked,
        "cleared": cleared,
        "unservable": still_unservable,
        # R21's ceiling bound the page this pass read, so the due set may be incomplete. The
        # notification pass warns about it once a cycle already (`notification_store.recipient_documents`),
        # so this only carries the flag rather than repeating the warning.
        "truncated": truncated,
        "ms": ms,
    }
    if bound is not None:
        logger.warning(
            "Client servability re-check hit its %s bound: %d of %d due checked in %d ms",
            bound,
            checked,
            len(due),
            ms,
            extra={"fields": fields},
        )
    elif cleared:
        logger.info(
            "Planned service appeared for %d webhook subscription(s); caveat cleared",
            cleared,
            extra={"fields": fields},
        )
    else:
        logger.debug(
            "Client servability re-check: %d of %d due checked in %d ms",
            checked,
            len(due),
            ms,
            extra={"fields": fields},
        )
