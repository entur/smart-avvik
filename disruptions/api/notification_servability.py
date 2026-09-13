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

**ONE QUESTION PER CYCLE SINCE U10, and the bound moved with it.** Until then the pass asked the
seam once per subscription, which made a BigQuery job the unit of work: four per cycle was the
ceiling, four per ~296 s cycle is ~1 200 records a day, and a population meant to reach 100 000
would never have finished a first pass. The seam now takes every pending stop pair at once
(`varsel._service_verdicts` -> `main._varsel_connectivity_batch`), so the question costs one job
however many records are waiting -- and the pass is bounded by what it WRITES instead.

**Bounded, in numbers, because each bound protects a different resource.**

* :data:`_MAX_WRITES_PER_CYCLE` (2 000) bounds the store. It bounds the question too, since the
  pairs asked about are derived from the admitted subscriptions and never from the whole due set --
  asking about a pair whose subscriptions will not be written this cycle would spend the answer on
  nothing and leave them due anyway. It is spent in :func:`_priority` order, so a caveat a client is
  being shown right now goes first.
* :data:`_WRITE_WORKERS` (32) is that bound divided by the time it may take: 2 000 transactions at
  the ~250 ms round trip U9 measured is ~16 s at this width, and a 500 ms one still fits inside
  :data:`_WRITE_WAIT_S`. One transaction at a time is ~4 writes a second however many are waiting,
  which is U9's arithmetic one module over -- a bound of 2 000 and a serial writer would simply
  mean the deadline binding every cycle.
* :data:`_WRITE_WAIT_S` (30 s) bounds the COLLECTION of those writes, and it has to: a transactional
  commit carries no client-side deadline, so an unbounded wait on a hung one would hold the poller's
  thread. Stragglers are abandoned and counted; an abandoned write that later commits is a stamped
  record, and one that never commits leaves its subscription due for the next cycle.
* :data:`_DEADLINE_S` (55 s) bounds the phase. It RESERVES the worst case
  (:data:`_WORST_CASE_CHECK_S`, the seam's own 20 s timeout) rather than measuring elapsed time --
  ``varsel._WriteBudget``'s shape -- so it bounds when the step ENDS and not when its question
  starts. Unlike the per-record version this no longer leaves the store write outside the ceiling:
  the writes have their own bounded wait, so the phase's worst case is the sum of the two plus the
  load, which is what this number is.
* The store read is one page of at most ``notification_store.MAX_SUBSCRIPTIONS_PER_CYCLE`` documents,
  R21's existing ceiling and the same query the notification pass already runs once per cycle. It
  needs no new composite index, which is the second reason to reuse it: a query ordering on
  ``servable_checked_at`` would have to reason about Firestore excluding documents that lack the
  field and sorting null below every timestamp, and the emulator tracks no composite indexes, so
  neither the emulator suite nor a dry run could catch getting that wrong.

The phase does not beat ``/health``'s heartbeat and does not need to: its whole ceiling is 55 s
against a 600 s staleness rule, where U9's emit pass needed one at 110 s.

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
converts a long outage into a permanent non-answer. It costs one place in a job that is issued
anyway, rather than one job of four, which is what makes it cheap enough to leave alone.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
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

#: At most this many records are written per cycle, and therefore at most this many stop pairs are
#: asked about. 2 000 at the measured ~296 s cycle period is ~580 000 records a day at full cadence,
#: against rule 2's requirement of four checks a day per record -- so it carries ~145 000
#: permanently-unservable subscriptions, which is above the population this unit sizes for. It
#: replaced `_MAX_CHECKS_PER_CYCLE` (4) in U10, when the BigQuery question stopped being per record.
#:
#: It is also the bound on ONE JOB's pairs: `main._CONNECTIVITY_MAX_PAIRS_PER_JOB` is the same
#: number from the other side, and `test_poller_refresh` pins this one at or under it, or the
#: overflow would be answered as unknown by a job this pass had already decided to spend.
#:
#: **What a full cycle costs the mirror, stated rather than discovered.** `_servability_body` is in
#: `notification_store.MIRROR_STAMPING_BODIES` -- it must be, or the mirror's copy of `servable`
#: never moves, the record reads as due on the next cycle and the bound is spent re-asking the same
#: pairs forever. So 2 000 writes here are 2 000 documents the NEXT `notification_mirror` phase
#: re-reads, which is deliberate and is against the read-once-ever economy U6 was built for. At the
#: full bound on every cycle that is ~580 000 extra document reads a day, on the order of $0.35;
#: a `synced` count spiking to 2 000 on that phase is this pass working as designed, not a churn
#: problem. It is self-limiting in practice, because a record stamped now is not due for six hours.
_MAX_WRITES_PER_CYCLE = 2000

#: How many of those writes are in flight at once. Derived, not picked: `notification_store`'s
#: transactions are ~250 ms of round trip (U9's measurement, same store and same client), so 2 000
#: of them at this width is ~16 s and at 500 ms each is ~31 s -- the first inside `_WRITE_WAIT_S`
#: and the second only just outside it, which is the margin this number buys. Serial, the pass
#: would write ~4 records a second and the bound above would be decoration.
_WRITE_WORKERS = 32

#: The bounded collection of the write pass. A transactional COMMIT carries no client-side deadline
#: -- `notification_store` plumbs one into every read, a transaction's reads included, and there is
#: nowhere to put one on the commit -- so waiting on all of them would park the poller's thread on a
#: hung store. Stragglers are abandoned and counted; an abandoned write that later commits is a
#: stamped record and one that never commits leaves its subscription due, so the residual is a
#: thread and never a wrong answer.
_WRITE_WAIT_S = 30.0

#: The step's clock, reserved rather than measured, and running from before the store read so what
#: it bounds is the PHASE and not the loop. The sum of what the phase may spend: one question at
#: `_WORST_CASE_CHECK_S` (20 s), one write pass at `_WRITE_WAIT_S` (30 s), and ~5 s for the load and
#: the coordinator between them.
#:
#: **It no longer leaves the store write outside the ceiling.** Until U10 this was 30 s and bounded
#: the QUESTIONS only, so the true worst case was that number plus one `set_servability` -- five
#: contended transaction attempts at `notification_store._CALL_DEADLINE_S` each, with commits that
#: carry no deadline at all, and therefore unbounded in principle. The writes have their own bounded
#: wait now, so this number is the phase's ceiling rather than most of it.
_DEADLINE_S = 55.0

# ---- The connectivity seam ---------------------------------------------------------------------
#
# `varsel._service_verdicts`: it expands both stop refs of every pair to every ref of their place
# (`varsel.place_refs`) and already fails open to None, which is what keeps "we could not establish
# it" from ever reading as "no trips run". Injected rather than imported so this module can be
# exercised without varsel's module state, and so an unwired deployment is a cheap early return
# instead of a store read.
#
# **It takes PAIRS and answers positionally** (U10). The single-pair shape it replaced made a
# BigQuery job the unit of work; the parameter is named `verdicts` rather than kept as
# `connectivity` so a deployment still wiring the old callable fails loudly at `main.lifespan`
# instead of handing a list of pairs to something expecting two stop refs.
BatchConnectivityProvider = Callable[
    [Sequence["tuple[str, str]"]], "Sequence[tuple[bool, bool] | None]"
]

_verdicts: BatchConnectivityProvider | None = None


def configure(*, verdicts: BatchConnectivityProvider | None = None) -> None:
    """Wire the seam. Called once from ``main.lifespan``."""
    global _verdicts
    _verdicts = verdicts


def reset_for_tests() -> None:
    global _verdicts
    _verdicts = None


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


class _Counts:
    """The write pass's tally, written by its workers as they go.

    `notification_events._Counts`' shape and for its reason: a still-running worker's completed work
    must not be discarded when the bounded wait expires, which is exactly when the numbers are worth
    having. A plain dict is a lost update at thirty-two threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def add(self, key: str, n: int = 1) -> int:
        """Increment, and answer what the value was BEFORE this call."""
        with self._lock:
            before = self._counts.get(key, 0)
            self._counts[key] = before + n
            return before

    def get(self, key: str) -> int:
        with self._lock:
            return self._counts.get(key, 0)


def _ask(pairs: Sequence[tuple[str, str]]) -> list[tuple[bool, bool] | None]:
    """One question about every pair, through the seam. Never raises.

    An unanswerable question is not an answer, so every failure -- the seam being unwired, a raise,
    a short or long answer list -- is ``None`` for every pair it touched, and rule 1 then leaves
    each record's stored value and stamp exactly as they were.

    The length check is not defensive decoration: the answers are POSITIONAL, so a provider that
    returned a different number of them would silently pair each verdict with the wrong stop pair
    and stamp routes with other routes' answers.
    """
    provider = _verdicts
    if provider is None:  # narrowed for the type checker; recheck() already returned
        return [None] * len(pairs)
    try:
        answers = list(provider(pairs))
    except Exception:
        logger.warning(
            "Client servability re-check raised; every stored answer and stamp stands",
            exc_info=True,
            extra={
                "fields": {
                    "event": "notification_servability",
                    "result": "error",
                    "pairs": len(pairs),
                }
            },
        )
        return [None] * len(pairs)
    if len(answers) != len(pairs):
        logger.warning(
            "The connectivity seam answered %d of %d stop pairs; the whole batch is discarded "
            "rather than paired up by position",
            len(answers),
            len(pairs),
            extra={
                "fields": {
                    "event": "notification_servability",
                    "result": "misaligned",
                    "pairs": len(pairs),
                    "answers": len(answers),
                }
            },
        )
        return [None] * len(pairs)
    return answers


def _record(
    subscription: notification_store.Subscription, servable: bool, counts: _Counts
) -> None:
    """Write one ESTABLISHED answer, on a worker thread. Never raises.

    Guarded per record rather than per pass (R20): a subscription deleted between the read and the
    write is an ordinary outcome, and one failing write must not cost the pass the records behind
    it -- which at thirty-two threads means it must not reach the pool either.

    The version is the one the QUESTION was asked about -- the answer is about a ROUTE, and a
    replace during the check would otherwise stamp the new route with the old route's verdict,
    freshly timestamped. Enforced INSIDE `set_servability`'s transaction, which is why this pass
    writes through it one record at a time rather than as a Firestore `WriteBatch`: a batch's only
    preconditions are `exists` and `last_update_time`, neither of which can say "and the route has
    not changed", and `last_update_time` read off the mirror is as old as the last sync -- so the
    busiest subscriptions, the ones a delivery has written since, would fail it forever.

    **Outcomes are COUNTED, not logged one by one.** Two thousand records a cycle is two thousand
    lines, and the one line worth having -- the traceback of an unexpected failure -- is written
    once per pass by the worker that saw the first one.
    """
    try:
        notification_store.set_servability(
            subscription.subscription_id,
            servable=servable,
            expected_version=subscription.version,
        )
    except notification_store.NotFound:
        # Deleted or tombstoned while we were asking. `set_servability` reads before it writes, so
        # nothing was resurrected; there is simply nothing left to stamp.
        counts.add("vanished")
    except notification_store.Conflict:
        # Replaced while we were asking. The newer definition retired the answer and its stamp on
        # purpose; a due record with nothing established is picked up on the next cadence, so this
        # needs no repair -- only a name, so the counter does not read it as a write failure.
        counts.add("superseded")
    except Exception:
        if counts.add("write_failed") == 0:
            logger.warning(
                "Client servability write failed; the record keeps its previous caveat. Further "
                "failures this cycle are counted rather than logged",
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
    else:
        counts.add("cleared" if servable else "unservable")


#: How old the notification pass's read of `subscriptions` may be for this re-check to reuse it
#: rather than read the collection again. One pass sits between the two in a healthy cycle (~296 s
#: measured 2026-09-08, and the pass is bounded well under that), so ten minutes reuses every
#: same-cycle read and never one from the cycle before. Past it -- or when the pass raised before
#: reading, or the channel's flag was off for the pass -- the re-check reads for itself.
_LOAD_REUSE_S = 600.0


def _load(now: datetime) -> notification_store.LoadedSubscriptions | None:
    """This environment's live subscriptions, or ``None`` when they could not be read.

    The notification pass's own read is reused when it is fresh (:data:`_LOAD_REUSE_S`): both walk
    every live subscription, and two reads per cycle bought nothing but a second stream.

    **The fallback is the mirror, not a query** (U6). It used to be `active_subscriptions`, a
    bounded read of the collection; that function and its ceiling are gone, and reading the
    collection here would reintroduce per-cycle the one full read the mirror exists to remove --
    on the path least able to justify it, since a healthy list has nothing due. An unseeded mirror
    yields ``None``, which is this function's existing "could not be read" and leaves every caveat
    standing, exactly as a failed query did.
    """
    reused = notification_store.recent_load(now=now, max_age_s=_LOAD_REUSE_S)
    if reused is not None:
        return reused
    try:
        mirror = notification_store.subscription_mirror()
        if not mirror.ready:
            logger.warning(
                "The subscription mirror is not loaded; the servability re-check is skipped this "
                "cycle and every caveat stands",
                extra={
                    "fields": {"event": "notification_servability", "result": "mirror_not_ready"}
                },
            )
            return None
        return notification_store.LoadedSubscriptions(
            subscriptions=tuple(mirror.recipients(now)),
            truncated=False,
            skipped=0,
            expired=0,
        )
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

    Called from the poller's refresh cycle, in its own phase after ``notification_webhooks``. Costs
    nothing on a healthy list beyond reusing the notification pass's read of the same collection --
    one bounded store read of its own only when that read is stale or absent (:data:`_LOAD_REUSE_S`). ``now`` is injectable for
    the same reason every ``notification_store`` entry point's is: staleness is the whole rule here, and
    a suite that could not pin the clock could only assert it by sleeping.
    """
    if _verdicts is None:
        # No seam wired: nothing could be established anyway, so this must not spend a store read
        # to discover that.
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
    """One question about every pending pair, then the answers written across a bounded pool."""
    deadline = started + _DEADLINE_S
    # The bound is on SUBSCRIPTIONS and the pairs are derived from the admitted ones, never from
    # the whole due set: a pair whose subscriptions will not be written this cycle would spend an
    # answer on nothing and stay due regardless. `due` is already in `_priority` order, so what the
    # bound spends itself on is the caveats a client is being shown right now, oldest answer first.
    admitted = list(due[:_MAX_WRITES_PER_CYCLE])
    capped = len(due) - len(admitted)
    # Deduped and in first-seen order: two subscriptions on one corridor are one question, and the
    # answer fans back out to both.
    pairs = list(dict.fromkeys((s.from_stop, s.to_stop) for s in admitted))
    counts = _Counts()
    answers: list[tuple[bool, bool] | None] = []
    out_of_time = monotonic() + _WORST_CASE_CHECK_S > deadline
    if not out_of_time:
        # RESERVED, not measured, so this bounds when the pass ENDS rather than when its question
        # starts. What it reserves is the SEAM's worst case; the writes below have their own.
        answers = _ask(pairs)
    # Only the forward direction decides the caveat. `answer[1]` is the reverse-service reading
    # /varsel words a likely from/to swap from, and the client record has nowhere to carry it:
    # `set_servability` takes one tri-state, and inventing a second field here would publish a hint
    # U4 never contracted.
    verdicts = {pair: answer for pair, answer in zip(pairs, answers) if answer is not None}
    established = [
        (subscription, bool(verdicts[(subscription.from_stop, subscription.to_stop)][0]))
        for subscription in admitted
        if (subscription.from_stop, subscription.to_stop) in verdicts
    ]
    if established:
        pool = ThreadPoolExecutor(max_workers=_WRITE_WORKERS, thread_name_prefix="servability")
        try:
            futures = [
                pool.submit(_record, subscription, servable, counts)
                for subscription, servable in established
            ]
            # Bounded by whichever of the two ceilings comes first, and bounded at all because a
            # transactional commit carries no client-side deadline. Stragglers are abandoned: the
            # record they were writing stays due, which is the same state it was already in.
            until = min(monotonic() + _WRITE_WAIT_S, deadline)
            _, unfinished = wait(futures, timeout=max(0.0, until - monotonic()))
            counts.add("abandoned", len(unfinished))
        finally:
            pool.shutdown(wait=False)
    abandoned = counts.get("abandoned")
    cleared = counts.get("cleared")
    if out_of_time:
        bound: str | None = "deadline"
    elif abandoned:
        bound = "abandoned"
    elif capped:
        bound = "capped"
    else:
        bound = None
    ms = int((monotonic() - started) * 1000)
    fields = {
        "event": "notification_servability",
        "result": bound or "swept",
        "due": len(due),
        "asked": len(pairs),
        "established": len(verdicts),
        "cleared": cleared,
        "unservable": counts.get("unservable"),
        "vanished": counts.get("vanished"),
        "superseded": counts.get("superseded"),
        "write_failed": counts.get("write_failed"),
        "abandoned": abandoned,
        "capped": capped,
        # R21's ceiling bound the page this pass read, so the due set may be incomplete. The
        # notification pass warns about it once a cycle already (`notification_store.recipient_documents`),
        # so this only carries the flag rather than repeating the warning.
        "truncated": truncated,
        "ms": ms,
    }
    if bound is not None:
        logger.warning(
            "Client servability re-check hit its %s bound: %d of %d due records written in %d ms",
            bound,
            cleared + counts.get("unservable"),
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
            "Client servability re-check: %d of %d due records written in %d ms",
            cleared + counts.get("unservable"),
            len(due),
            ms,
            extra={"fields": fields},
        )
