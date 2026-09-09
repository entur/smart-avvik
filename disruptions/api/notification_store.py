"""Durable notification state: subscriptions, alert subjects, events and delivery records.

Firestore Native, one **named** database per environment, created by this repo's own Terraform
(``terraform/firestore.tf``). Chosen over extending /varsel's GCS state blob, which is rewritten
whole with no compare-and-set: a notification API writing a subscription while the poller saves a cycle
would silently lose one of the two, and that is precisely the write pattern this surface adds.

Four collections, all **top level**, all carrying the ids that relate them:

===========================  ===============================================================
``subscriptions``    one per registration: a /varsel journey definition plus a channel
``alert_subjects``   one per (subscription, train): the revision and lifecycle phase
``events``           one per minted revision: the envelope a client receives
``deliveries``       one per event per destination: attempt state, stable delivery id
===========================  ===============================================================

**Events, deliveries and subjects are top-level collections carrying a subscription id, not
subcollections of it.** A document delete does not remove subcollections and a TTL field does not
cascade into them, so subcollections would be orphaned by the deletion path below and the delivery
query would keep returning them forever.

Three rules govern everything here, and each of them has a failure that is silent when broken.

1. **A transaction body may run more than once.** Firebase's own words: the function "might run
   more than once" and "should not directly modify application state". So every ``_*_body`` below
   takes its ``now`` and its ``revision_seed`` as ARGUMENTS captured once by the caller, performs
   all its reads before any write, returns its result rather than appending to an outer list, and
   logs nothing. A clock read inside a body makes ``event_id`` irreproducible; a log line inside
   one double-reports; an outer-list append double-counts. ``test_notification_store`` invokes each body
   twice against one fake transaction, with a clock that RAISES and a logger that raises on any
   attribute access, which is what catches all three.
2. **TTL deletes within roughly 24 hours of expiry, and expired documents stay queryable until it
   does.** Verbatim from the Firestore documentation: "Expired documents continue to appear in
   queries and lookup requests until the TTL process actually deletes them." So every read path
   tests the expiry field ITSELF -- :func:`_not_expired` plus the per-collection semantic test
   beside it -- and the ~24 h residual is the window :data:`TTL_RESIDUAL_HOURS` publishes rather
   than implies.
3. **A missing composite index is a runtime ``FailedPrecondition``, not a deploy-time error**, and
   the emulator tracks no composite indexes at all, so no runtime test can stand in for the
   declaration. Every query in this module is therefore built from a :class:`QuerySpec` in
   :data:`QUERY_SPECS` by the single :func:`_query` helper, and the suite checks each spec against
   the checked-in ``firestore.indexes.json`` in both directions -- every spec covered by an entry,
   and every entry materialised by ``terraform/firestore.tf``.

What this module deliberately does NOT do: decide when to emit (that reads the notification pass),
sign anything, or make an outbound call. It is the durable state and the transactions over it.
Nothing here imports ``varsel``, ``main`` or ``impact``: a notification event may not reach the device
registry, the VAPID keypair or the decision-log writer (R16), and the poller's load step calls
:func:`recipient_documents` through an injected provider precisely so the dependency runs one way.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

OSLO = ZoneInfo("Europe/Oslo")

# ---- Collections and the one TTL field per group ---------------------------------------------

SUBSCRIPTIONS = "subscriptions"
SUBJECTS = "alert_subjects"
EVENTS = "events"
DELIVERIES = "deliveries"

COLLECTIONS = (SUBSCRIPTIONS, SUBJECTS, EVENTS, DELIVERIES)

# Firestore allows exactly ONE TTL field per collection group, so all four groups use the same
# field name: the Terraform resource is then one repeated block rather than four bespoke ones, and
# `_not_expired` is one function every read path can call without knowing which collection it is
# looking at. Note the field's meaning is retention, never validity -- an event's usable life is
# `valid_until`, 60 minutes, and deleting on that would destroy the seven-day history.
TTL_FIELD = "expires_at"

# The published contract version. `contracts/notifications/v1/` holds the two JSON schemas, and this
# value is the `schema_version` in every envelope, so a client can refuse a shape its receiver does
# not know rather than mis-parse it (R20's fail-closed rule, on the client's side of the wire).
SCHEMA_VERSION = "1"


# ---- Retention, stated as numbers because R19 publishes them ----------------------------------

# Events, deliveries and alert subjects. Shorter than the ten-day retry window the nightly
# labelling job uses, which forecloses retrospectively scoring client alerts -- a published
# retention choice, so lengthening it later is a contract change, not a tuning edit.
HISTORY_DAYS = 7
# R5's two subscription horizons. A subscription expires at the EARLIER of them, and both are
# enforced on read rather than by TTL alone: TTL is up to ~24 h late, and a subscription that keeps
# alerting for a day past its published expiry is a broken promise rather than a slow cleanup.
SUBSCRIPTION_MAX_LIFETIME_DAYS = 180
SUBSCRIPTION_INACTIVITY_DAYS = 60
# R12's event validity horizon, quoted by U6, U8, AE12 and the client documentation. Validity is
# the EARLIER of this, the departure, and the subscription's lifetime; the retry *interval* is one
# attempt per notification pass and is deliberately not a constant -- the pass runs on SIRI
# ingestion changes, measured at a ~296 s period (2026-09-08).
EVENT_VALIDITY_MINUTES = 60
# The maximum residual window where cleanup has not completed and TTL is the fallback. Published,
# not implied: the Firestore documentation says expired documents are "typically deleted within 24
# hours after expiration".
TTL_RESIDUAL_HOURS = 24
# How long a delivered situation number stays on the subscription as a dedup mark. /varsel prunes
# `_notified` exactly -- it drops a key the moment its situation stops being live -- which a store
# cannot do without reading the live set, so this is a bound instead of an exact rule. 48 h is the
# generous side on purpose: a situation reaches 12 h from ITS onset and the poller scores ~12 h
# forward, so a mark that outlives its situation costs one map entry, while a mark that expires
# EARLY re-notifies a train the traveller has already been told about.
DELIVERED_MARK_TTL_H = 48


# ---- R21's ceilings, every one a number so it can be checked ----------------------------------

# Active subscriptions per client. A planning default rather than measured capacity: the superseded
# plan proposed 1 000 per client and 5 000 total and its own risk note said to "start with lower
# measured admission limits", so this is the lower one. Raising it needs a measurement of the pass
# at the new population, not a judgement.
MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT = 500
# What one poller cycle may READ. R21 says the per-cycle read cannot exceed the ceiling, and this is
# literally the query's `limit`. It must stay at or below `varsel._MAX_WEBHOOK_RECIPIENTS`, which
# bounds the same read one layer out; `test_the_cycle_ceiling_does_not_exceed_varsels_own` parses
# that constant out of varsel.py rather than trusting the two to be edited together.
MAX_SUBSCRIPTIONS_PER_CYCLE = 2000
# Pending events per destination. Enforced with a bounded count BEFORE the emit transaction rather
# than inside it, which is exact rather than best-effort for one reason that is nameable instead of
# hoped for: the emit path has exactly one writer (KTD6 -- one poller thread, no route invokes the
# pass, one replica under the Recreate strategy), so nothing can create a pending event between the
# count and the commit.
MAX_PENDING_EVENTS_PER_DESTINATION = 500
# Delivery attempts per cycle, in total and per destination. The per-destination one is what stops a
# single backlogged client spending the whole pass; the total is what bounds the query.
MAX_DELIVERY_ATTEMPTS_PER_CYCLE = 200
MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE = 50
# R11's cap: /varsel's `_MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY`, at /varsel's grain (per
# registration per Oslo day) because R3's parity is the whole point. A commuter who registers a
# morning and an evening journey has an allowance for each, exactly as they would in /varsel.
MAX_ALERT_DELIVERIES_PER_DAY = 4
# Bounds the two per-subscription maps so a document cannot grow without limit. Both are pruned by
# time first (see `_prune_marks` / `_prune_daily`), so hitting these means something is wrong.
MAX_DELIVERED_MARKS = 500
MAX_DAILY_COUNTERS = 14
# What the notification API may SERVE of a subscription's current alert state, and what one request may
# READ to compute it. Two numbers because they answer different questions.
#
# The served cap is `MAX_ALERT_DELIVERIES_PER_DAY`, deliberately the same number: R11 lets at most
# four notifications about a subscription reach a traveller in an Oslo day, so a fifth subject is
# not something anybody can be told about today, and a list that grew past it would be reporting
# what the cap has already suppressed. AE3 is why a cap is needed at all -- one line suspension put
# thirteen cancelled departures inside one subscription's window -- and the per-row COUNTS carry the
# rest, so nothing is hidden, only unenumerated.
#
# The read ceiling bounds the whole request rather than a row: the list route reads every
# subscription's subjects in ONE client-scoped query and groups them in Python, because a query
# per row would be a hundred queries on a hundred-row page. Past this ceiling the snapshot reports
# `truncated`, and the counts are lower bounds rather than answers -- see `client_alerts`.
MAX_ALERTS_SERVED = 4
MAX_SUBJECTS_PER_READ = 500
# Below-threshold observations IN ONE RUN before an alert is withdrawn. Two, not one: a single
# cycle's dip below the gate is noise, and a withdrawal is itself a notification. A planning
# default -- the reversal condition is a measurement of how often a withdrawn subject re-crosses
# the gate.
WITHDRAWAL_CYCLES = 2
# What closes a run of below-threshold observations. Two of them further apart than this start a
# fresh count, so a dip, a long recovery and a second dip do not withdraw between them.
#
# Sized from the measurement, not chosen round: the notification pass runs on SIRI ingestion changes
# rather than a schedule, measured at a ~296 s period on 2026-09-08, so this is ~3 periods and
# tolerates two skipped or slow cycles inside one run. `_withdraw_body` explains why a time window
# is what this can use at all, and states precisely what it does and does not observe -- it is an
# approximation of "consecutive", and every way it is inaccurate errs toward NOT withdrawing.
# Re-measure the period before changing it; the two numbers are one decision.
WITHDRAWAL_GAP_S = 900
# `Retry-After` is honoured but capped, at the lesser of the header, the time to departure and this.
RETRY_AFTER_CAP_S = 300
# HOW LONG A RESERVED CAP SLOT SURVIVES UNCONFIRMED, and this number is the whole of the scheme's
# crash safety. `claim_delivery` reserves a slot, the terminal writes confirm or release it, and a
# process that dies in between leaves a reservation nobody will ever report on. A reservation that
# outlived the day would be strictly WORSE than the overspend it prevents -- it would burn one of a
# traveller's four silently and permanently, and the plan is explicit that the slot is spent on
# success ("otherwise a dead destination burns a subscription's four for the Oslo day with nothing
# delivered").
#
# 120 s: comfortably longer than one attempt's worst case (`webhook_delivery` reserves the outbound
# timeout plus three store calls, ~11 s, under a 20 s bounded wait, and a client-retried transaction
# can stretch that), and comfortably SHORTER than the pass period measured at ~296 s. That is the
# property worth stating: a leaked reservation costs at most ONE cycle's worth of allowance, never
# the day, and the next cycle reclaims it with no sweep and no repair job.
RESERVATION_TTL_S = 120.0
# THE DEADLINE ON EVERY INDIVIDUAL FIRESTORE CALL, and KTD6 requires it of the load step and the
# emit transactions alike. It is NOT the same bound as the poller's wait: `varsel`'s own
# `_WEBHOOK_LOAD_TIMEOUT_S` (5 s) bounds how long the ingestion thread WAITS on a worker, and a
# worker parked past it is abandoned rather than killed -- Python cannot cancel a thread inside a
# socket read. An abandoned thread is not free: `concurrent.futures.thread` registers an atexit hook
# that JOINS its workers, so a store call with no deadline of its own delays pod termination by
# however long it hangs. This is the deadline that stops that, and it belongs to the call rather
# than to the caller for exactly that reason.
#
# 4 s, under `varsel._WEBHOOK_LOAD_TIMEOUT_S`, so a single hung call surfaces as this module's own
# `DeadlineExceeded` -- retryable, logged, one cycle skipped -- rather than as the caller's timeout
# with a thread still running behind it. A paged read spends it PER PAGE, which is the honest shape:
# the whole-read bound is the caller's, and R21's document ceiling is what bounds the page count.
_CALL_DEADLINE_S = 4.0

# Lifecycle phases and the three event types the contract names.
PHASE_PREDICTED = "predicted"
PHASE_CONFIRMED = "confirmed"
PHASE_WITHDRAWN = "withdrawn"
PHASES = (PHASE_PREDICTED, PHASE_CONFIRMED)

EVENT_CREATED = "journey.alert.created"
EVENT_UPDATED = "journey.alert.updated"
EVENT_WITHDRAWN = "journey.alert.withdrawn"
EVENT_TYPES = (EVENT_CREATED, EVENT_UPDATED, EVENT_WITHDRAWN)

# Delivery states, the six the contract names.
DELIVERY_STATES = ("pending", "sent", "expired", "superseded", "cancelled", "failed")

# Revisions are seeded from whole MINUTES since this instant, in UTC. Two things this gets right
# that the obvious version does not. UTC, never Oslo: a revision formatted from the Oslo decision
# timestamp goes backwards for an hour at every autumn DST fall-back, and a receiver ordering on
# `revision` would silently discard every event for those subjects, a confirmed cancellation
# included. And it is only a SEED -- the transaction clamps to `max(stored + 1, seed)` using the
# subject read it already performs, so monotonicity survives an NTP step backwards, which a
# timestamp alone does not. Seeding at all is what stops a fresh process restarting at 0.
_REVISION_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# The client raises a plain ValueError -- not a Google API error -- when a transaction exhausts its
# five attempts, so a handler keyed on `google.api_core.exceptions` misses it entirely and a
# contended subject would be marked `failed` and never delivered. The literal below is
# `google.cloud.firestore_v1.base_transaction._EXCEED_ATTEMPTS_TEMPLATE`'s prefix; the constant is
# private, so `is_retryable` prefers importing it and falls back to this, and
# `test_the_exhausted_transaction_message_matches_the_installed_client` pins the two against each
# other so a client upgrade that rewords it fails a test rather than a client's delivery.
_EXHAUSTED_TRANSACTION_PREFIX = "Failed to commit transaction in"


# ---- Errors -----------------------------------------------------------------------------------


class NotificationStoreError(Exception):
    """Base for every failure this module raises deliberately."""


class NotConfigured(NotificationStoreError):
    """No client and no database id: the channel is not wired in this deployment."""


class NotFound(NotificationStoreError):
    """The record does not exist, is tombstoned, or has passed one of its horizons."""


class Forbidden(NotificationStoreError):
    """The record exists and belongs to a different client.

    Deliberately distinct from :class:`NotFound` INSIDE the store and deliberately collapsed into
    it at the API edge (U4's job): the store's caller needs to tell a tenancy violation from a
    missing document to log the former, while a client must not be able to probe another's
    subscription ids by the difference between a 403 and a 404.
    """


class Conflict(NotificationStoreError):
    """A version mismatch, an immutable field changed, or an id reused for a different definition."""


class QuotaExceeded(NotificationStoreError):
    """A ceiling in R21 would be breached. Raised before any write."""


# ---- Configuration ----------------------------------------------------------------------------

_firestore_client_obj: Any | None = None
_database: str | None = None
_clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def configure(
    *,
    client: Any | None = None,
    database: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Wire the store: a named database (production) or a ready-made client (tests, the emulator).

    Production passes only ``database`` -- the client is built lazily on first use and takes its
    PROJECT from the ambient environment (``GOOGLE_CLOUD_PROJECT`` / ADC), never from an argument
    and never from a literal in this file. That is what R1's environment isolation rests on: each
    environment is its own GCP project with its own runtime identity, so a dev credential cannot
    name prd's data because nothing in this code path can name a project at all.

    ``clock`` is injectable because a transaction body may run more than once and must therefore
    read no clock: every public method takes its instant ONCE, here, and passes it down.
    """
    global _firestore_client_obj, _database, _clock
    _firestore_client_obj = client
    _database = database
    _clock = clock or (lambda: datetime.now(UTC))


def enabled() -> bool:
    """Is there anything to read? A client was injected, or a database id was named."""
    return _firestore_client_obj is not None or bool(_database)


def database_id() -> str | None:
    """The named database this deployment talks to, for a config endpoint and for diagnostics."""
    return _database


def reset_for_tests() -> None:
    configure(client=None, database=None, clock=None)


def _now() -> datetime:
    """The one clock read. Always aware UTC -- a naive datetime cannot be compared with what the
    Firestore client returns (``DatetimeWithNanoseconds``, always aware), and the comparison raises
    rather than reading false, which is the good direction but not one to rely on."""
    now = _clock()
    return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)


def _firestore_client() -> Any:
    global _firestore_client_obj
    if _firestore_client_obj is not None:
        return _firestore_client_obj
    if not _database:
        raise NotConfigured("notification_store.configure() was never called with a database id")
    # Lazy, like llm_message's Vertex clients and for the same reason: a deployment with the channel
    # switched off must neither import the library nor need credentials for it, and the import is
    # ~200 ms of gRPC machinery nothing else in this process wants.
    from google.cloud import firestore

    _firestore_client_obj = firestore.Client(database=_database)
    return _firestore_client_obj


def firestore_client() -> Any:
    """The one Firestore client this process holds, built on first use.

    Public so ``notification_auth`` -- the identity plane, which stores its accounts, credentials,
    destinations and operator audit in the same named database -- does not open a second one. Two
    clients means two gRPC channels and two connection pools in a single-replica deployment whose
    poller already shares the thread, for no gain: there is one database and one process.

    It is also what keeps **one place deciding which database is opened**. The project is never an
    argument -- it comes from the ambient environment (``GOOGLE_CLOUD_PROJECT`` / ADC) -- and that
    is what R1's environment isolation rests on: each environment is its own GCP project with its
    own runtime identity, so a dev credential cannot name prd's data because nothing in this code
    path can name a project at all.

    Still lazy: a deployment with the channel switched off imports no library and needs no
    credential, and nothing here runs at module import.
    """
    return _firestore_client()


# ---- Time, expiry and the two per-subscription maps -------------------------------------------


def _as_dt(value: Any) -> datetime | None:
    """Coerce a stored timestamp to aware UTC, or ``None`` if it cannot be read.

    Handles what the three writers of these documents actually produce: the Firestore client's
    ``DatetimeWithNanoseconds`` (aware), a plain ``datetime`` (a fake, or a naive value from an
    older binary -- read as UTC), and an ISO-8601 string (a hand-repaired document, a backfill).
    """
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            # 3.11+ accepts a trailing "Z" directly, so no normalisation is needed -- and adding one
            # is what ruff's FURB162 flags. `.python-version` pins 3.13.
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _not_expired(doc: Mapping[str, Any], now: datetime) -> bool:
    """Is this document's TTL field still in the future?

    The test every read path owes, in every collection, because TTL deletion runs up to ~24 h late
    and an expired document is returned by queries and lookups until it does. An UNREADABLE or
    MISSING ``expires_at`` reads as expired, which is the direction a retention promise has to fail
    in: R19 publishes what the code does, and a document that outlives its published window because
    its timestamp could not be parsed makes the published text false. The cost of the strict
    direction is a dropped document, which is visible; the cost of the lax one is not.
    """
    expires = _as_dt(doc.get(TTL_FIELD))
    return expires is not None and expires > now


def subscription_is_live(doc: Mapping[str, Any], now: datetime) -> bool:
    """Active, not tombstoned, and inside BOTH of R5's horizons.

    Public because U4's resolution endpoint and U6's pass ask the same question and must not answer
    it two ways. The horizons are tested from their own stored fields rather than from the derived
    ``expires_at`` alone: that field is the minimum of the two, written by whichever binary created
    the document, and a stale or absent one would otherwise decide expiry on its own.
    """
    if doc.get("status") != "active":
        return False
    if not _not_expired(doc, now):
        return False
    for horizon in ("lifetime_expires_at", "inactivity_expires_at"):
        at = _as_dt(doc.get(horizon))
        if at is None or at <= now:
            return False
    return True


def _horizons(created_at: datetime, last_seen_at: datetime) -> dict[str, datetime]:
    """R5's two horizons and the TTL field derived as their minimum."""
    lifetime = created_at + timedelta(days=SUBSCRIPTION_MAX_LIFETIME_DAYS)
    inactivity = last_seen_at + timedelta(days=SUBSCRIPTION_INACTIVITY_DAYS)
    return {
        "lifetime_expires_at": lifetime,
        "inactivity_expires_at": inactivity,
        TTL_FIELD: min(lifetime, inactivity),
    }


def oslo_day(now: datetime) -> str:
    """The Oslo calendar day R11's cap is keyed on. Oslo, not UTC: a cap on "today's" notifications
    is a claim about the traveller's day, and /varsel keys `_daily_pushes` the same way."""
    return now.astimezone(OSLO).date().isoformat()


def _prune_daily(counters: Mapping[str, Any], now: datetime) -> dict[str, int]:
    """Keep the most recent Oslo days only.

    ISO dates sort lexicographically, so "most recent" is a reverse sort on the key and the trim is
    deterministic -- which matters because a transaction body may run more than once and two runs
    must write the same map. A non-numeric value is dropped rather than coerced: an unreadable
    counter must not become a zero, which would hand a subscription a fresh allowance.

    ``now`` is unused today and is taken anyway, so a future change that wants to keep "the last N
    days ending today" rather than "the N highest keys" does not have to change every call site.
    """
    del now  # see the docstring
    keep = {
        str(k): int(v)
        for k, v in counters.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    if len(keep) <= MAX_DAILY_COUNTERS:
        return keep
    for day in sorted(keep, reverse=True)[MAX_DAILY_COUNTERS:]:
        keep.pop(day, None)
    return keep


def _prune_marks(marks: Mapping[str, Any], now: datetime) -> dict[str, datetime]:
    """Drop delivered marks whose window has passed, then bound what remains by count."""
    live: dict[str, datetime] = {}
    for key, value in marks.items():
        at = _as_dt(value)
        if at is not None and at > now:
            live[str(key)] = at
    if len(live) <= MAX_DELIVERED_MARKS:
        return live
    newest = sorted(live.items(), key=lambda kv: kv[1], reverse=True)[:MAX_DELIVERED_MARKS]
    return dict(newest)


def _live_reservations(doc: Mapping[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
    """The delivery ids holding a slot on THIS Oslo day whose reservation has not lapsed.

    Keyed by delivery id, which is what makes the whole scheme idempotent: re-reserving the same
    delivery replaces its own entry instead of consuming a second slot, so a retried claim -- or a
    claim racing the abandoned thread of its own previous attempt -- cannot overspend.

    A reservation older than :data:`RESERVATION_TTL_S` is ignored here and pruned by the next write.
    That is the crash-safety property, and it is why an expiry exists at all: see
    :func:`_reserve_slot`.
    """
    reservations = doc.get("daily_reservations")
    if not isinstance(reservations, Mapping):
        return {}
    today = oslo_day(now)
    live: dict[str, dict[str, Any]] = {}
    for delivery_id, entry in reservations.items():
        if not isinstance(entry, Mapping):
            continue
        at = _as_dt(entry.get("at"))
        if at is None or (now - at).total_seconds() > RESERVATION_TTL_S:
            continue
        if str(entry.get("day") or "") != today:
            continue
        live[str(delivery_id)] = dict(entry)
    return live


def cap_remaining(doc: Mapping[str, Any], now: datetime) -> int:
    """R11's remaining allowance for this subscription on this Oslo day.

    **Confirmed sends PLUS live reservations**, and counting only the first is the defect this
    replaced. The allowance was READ here by ``claim_delivery`` and SPENT later by
    ``mark_delivery_sent``, so with a delivery pool wider than the cap the first wave of claims all
    read "slots left" before any of them spent one, and up to
    ``MAX_ALERT_DELIVERIES_PER_DAY + workers - 1`` notifications could go out for one subscription
    in one Oslo day. It did not bite only because ``webhook_delivery._WORKERS`` (4) happens to equal
    the cap (4) -- a coincidence, pinned by an assertion in that suite rather than relied on.
    ``varsel._WriteBudget.claim`` is the precedent for the fix and its docstring records the same
    read-then-act shape overshooting a 30 s ceiling by 16 s.

    R11's four-per-registration grain is the whole of this plan's parity claim with /varsel, so an
    overspend is not cosmetic.
    """
    counters = doc.get("daily_deliveries") or {}
    spent = counters.get(oslo_day(now), 0) if isinstance(counters, Mapping) else 0
    try:
        spent_n = int(spent)
    except (TypeError, ValueError):
        spent_n = MAX_ALERT_DELIVERIES_PER_DAY  # unreadable counter = no allowance (fail closed)
    return max(0, MAX_ALERT_DELIVERIES_PER_DAY - spent_n - len(_live_reservations(doc, now)))


# ---- Identity: alert subject, event id, revision seed -----------------------------------------


@dataclass(frozen=True)
class TrainRef:
    """The affected train, as the store keys and displays it.

    ``stop_order`` is part of the identity because one physical train can be a distinct SUBJECT for
    two subscriptions boarding it at different stops -- and because /varsel's own
    `one_alert_per_train` collapses on the boarding call, so this is that key.
    """

    operator: str
    line: str
    journey_ref: str
    call_date: str
    stop_place: str
    stop_order: int


def alert_subject_id(subscription_id: str, train: TrainRef) -> str:
    """The ``alert_id``: the canonical per-train subject key, per SUBSCRIPTION.

    **The grain is (subscription, train), not train alone**, and the plan does not state it, so
    here is the reasoning it rests on. `alert_id` is the only key the supplied receiver policy
    deduplicates and orders on. A client serves many commuters through one receiver; if two
    subscriptions on the same disrupted train shared an `alert_id`, the receiver's own "drop a
    revision not strictly newer than the highest applied for that train" rule would suppress the
    SECOND commuter's notification entirely. Per subscription, the ordering claim stays exactly as
    strong (revisions about one train reaching one recipient are strictly ordered) and no recipient
    is silently starved.

    Opaque and derived, and it must never become a lookup capability or a bearer credential -- the
    determinism exists so a receiver can dedupe, and for nothing else.
    """
    material = "|".join(
        (
            "kix.alert.v1",
            subscription_id,
            train.operator,
            train.line,
            train.journey_ref,
            train.call_date,
            train.stop_place,
            str(train.stop_order),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()[:40]


def event_id_for(alert_id: str, revision: int) -> str:
    """``event_id``, deterministic over the subject and the revision.

    Determinism is what makes a re-run of the same transaction body mint the same id, and what lets
    the receiver's inbox recognise a duplicate. It is emphatically NOT a capability: the same
    residual-review finding that named this pattern also recorded that an opaque derived id "must
    never become a lookup capability or a bearer credential".
    """
    return hashlib.sha256(f"kix.event.v1|{alert_id}|{revision}".encode()).hexdigest()[:40]


def revision_seed(now: datetime) -> int:
    """Whole minutes since :data:`_REVISION_EPOCH`, in UTC. Captured OUTSIDE any transaction body."""
    delta = now.astimezone(UTC) - _REVISION_EPOCH
    return max(1, int(delta.total_seconds() // 60))


def _definition_digest(spec: SubscriptionSpec) -> str:
    """What makes a create idempotent: the same id plus the same definition is the same request.

    Covers exactly the fields a client submits, so a retry of an unacknowledged POST returns the
    stored record instead of a conflict, and a genuinely different definition under a reused id is
    refused rather than silently overwriting a live subscription.
    """
    material = "|".join(
        (
            spec.client_id,
            spec.destination_id,
            spec.from_stop,
            spec.to_stop,
            spec.begin,
            spec.end,
            ",".join(str(d) for d in sorted(set(spec.days))),
            spec.external_ref,
            "test" if spec.test else "live",
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


# ---- Query specs: the declaration every index check reads -------------------------------------


@dataclass(frozen=True)
class QuerySpec:
    """One query this module runs, declared so it can be checked against a checked-in index.

    Declared rather than written inline because a missing composite index is a RUNTIME
    ``FailedPrecondition`` with a console link, the emulator tracks no composite indexes (so a
    query missing its production index simply succeeds there), and nothing in this repo executes a
    query against a real database in CI. The only thing that can catch it is a static check, and a
    static check needs something to read.
    """

    name: str
    collection: str
    equality: tuple[str, ...] = ()
    # (field, operator). Firestore's classic rule: the first ordering must be on the range field.
    inequality: tuple[tuple[str, str], ...] = ()
    order_by: tuple[tuple[str, str], ...] = ()
    why: str = ""

    def __post_init__(self) -> None:
        for field_path, _op in self.inequality:
            if not self.order_by or self.order_by[0][0] != field_path:
                raise ValueError(
                    f"{self.name}: a range filter on {field_path!r} requires it to be the first "
                    "order_by field"
                )

    @property
    def index_fields(self) -> tuple[tuple[str, str], ...]:
        """The composite index this query needs, as ordered (fieldPath, order) pairs.

        Equality fields first (any order works in the query; the index must simply contain them),
        then the explicit orderings with their directions. Empty when the query needs at most one
        field, which Firestore's automatic single-field indexes already serve.
        """
        fields: list[tuple[str, str]] = [(f, "ASCENDING") for f in self.equality]
        for field_path, direction in self.order_by:
            if field_path not in [f for f, _ in fields]:
                fields.append((field_path, direction))
        return tuple(fields) if len(fields) > 1 else ()


QUERY_SPECS: tuple[QuerySpec, ...] = (
    QuerySpec(
        name="load_active_subscriptions",
        collection=SUBSCRIPTIONS,
        equality=("status",),
        # `expires_at` DESCENDING, and the direction is load-bearing. Expiry is tested in PYTHON
        # (see `subscription_is_live`), so an expired-but-not-yet-deleted document is still returned
        # by this query and discarded after the fact. Ordered ascending, a page truncated at the
        # per-cycle ceiling would keep exactly the rows closest to expiry -- the dead ones -- and
        # starve live subscriptions. Descending puts the furthest-from-expiry first, so truncation
        # sheds dead rows before live ones.
        order_by=(("expires_at", "DESCENDING"),),
        why="the poller's one client read per cycle",
    ),
    QuerySpec(
        name="subscriptions",
        collection=SUBSCRIPTIONS,
        equality=("client_id", "status"),
        order_by=(("created_at", "ASCENDING"),),
        why="a client lists its own subscriptions, and the create-time ceiling count",
    ),
    QuerySpec(
        name="subscription_alerts",
        collection=SUBJECTS,
        equality=("subscription_id",),
        # `updated_at` DESCENDING, and the direction is the same argument
        # `load_active_subscriptions` makes for its own: the page is truncated at a ceiling, so
        # whichever end the ordering puts first is what survives. Most-recently-written first is
        # what the current state is made of -- a subject document is rewritten on every lifecycle
        # transition -- and it sheds subjects nothing has happened to before ones that just changed.
        order_by=(("updated_at", "DESCENDING"),),
        why="one subscription's current alert state, for the notification API",
    ),
    QuerySpec(
        name="client_alerts",
        collection=SUBJECTS,
        equality=("client_id",),
        order_by=(("updated_at", "DESCENDING"),),
        why="every subscription's alert state in ONE query, for the notification API's list page",
    ),
    QuerySpec(
        name="pending_deliveries",
        collection=DELIVERIES,
        equality=("state",),
        # `Retry-After` is persisted as `retry_not_before` and the QUERY excludes records before
        # that time, so a backoff survives a cycle boundary and a restart instead of lasting only
        # the current pass.
        inequality=(("retry_not_before", "<="),),
        order_by=(("retry_not_before", "ASCENDING"),),
        why="this cycle's deliverable set",
    ),
    QuerySpec(
        name="destination_deliveries_in_state",
        collection=DELIVERIES,
        equality=("destination_id", "state"),
        order_by=(("created_at", "ASCENDING"),),
        why="R21's pending-events-per-destination ceiling",
    ),
    # The three cleanup queries filter on `subscription_id` alone, which Firestore's automatic
    # single-field indexes serve -- hence an empty `index_fields` and no entry in
    # firestore.indexes.json. Declared anyway, so "every query in the module is a spec" stays a
    # property of the module rather than of whoever remembered.
    QuerySpec(
        name="cleanup_events",
        collection=EVENTS,
        equality=("subscription_id",),
        why="ordered deletion after a tombstone",
    ),
    QuerySpec(
        name="cleanup_deliveries",
        collection=DELIVERIES,
        equality=("subscription_id",),
        why="ordered deletion after a tombstone",
    ),
    QuerySpec(
        name="cleanup_subjects",
        collection=SUBJECTS,
        equality=("subscription_id",),
        why="ordered deletion after a tombstone",
    ),
)

_SPECS_BY_NAME = {spec.name: spec for spec in QUERY_SPECS}


def _query(
    name: str,
    *,
    equality: Mapping[str, Any] | None = None,
    inequality: Mapping[str, Any] | None = None,
    limit: int,
    start_after: Any = None,
) -> Any:
    """Build the query named by a :class:`QuerySpec`. **The only place this module filters.**

    ``test_every_filter_goes_through_the_query_helper`` asserts that ``.where(`` and ``.order_by(``
    appear nowhere else in this file, so a query added later cannot dodge the spec table -- and
    therefore cannot ship without an index declaration.

    ``start_after`` is a document SNAPSHOT, never a field value: Firestore derives the cursor from
    the ordering fields plus ``__name__``, so a page boundary that falls on two documents sharing a
    ``created_at`` is still cut in one place. It needs no index of its own -- the cursor is
    positional within an ordering the spec already declares.
    """
    spec = _SPECS_BY_NAME[name]
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = _firestore_client().collection(spec.collection)
    for field_path in spec.equality:
        query = query.where(filter=FieldFilter(field_path, "==", (equality or {})[field_path]))
    for field_path, op in spec.inequality:
        query = query.where(filter=FieldFilter(field_path, op, (inequality or {})[field_path]))
    for field_path, direction in spec.order_by:
        query = query.order_by(field_path, direction=direction)
    if start_after is not None:
        query = query.start_after(start_after)
    return query.limit(limit)


# ---- Reads and writes inside a transaction ----------------------------------------------------
#
# Two one-line helpers so a fake transaction has exactly two read shapes to implement, and so the
# reads-before-writes rule is visible at every call site rather than inferred from the client's API.


def _read(transaction: Any, ref: Any) -> Any:
    return ref.get(transaction=transaction, timeout=_CALL_DEADLINE_S)


def _read_query(transaction: Any, query: Any) -> list[Any]:
    return list(transaction.get(query, timeout=_CALL_DEADLINE_S))


def _doc(snapshot: Any) -> dict[str, Any]:
    return dict(snapshot.to_dict() or {}) if getattr(snapshot, "exists", False) else {}


class _Refs:
    """Document references by id, so a transaction body never touches the client.

    The bodies below take one of these. In production it wraps the Firestore client; in the
    emulator-free half of the suite it is a fake holding dicts, which is what makes scenario 1 --
    invoke a body twice and compare its writes -- possible at all.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def subscription(self, subscription_id: str) -> Any:
        return self._client.collection(SUBSCRIPTIONS).document(subscription_id)

    def subject(self, alert_id: str) -> Any:
        return self._client.collection(SUBJECTS).document(alert_id)

    def event(self, event_id: str) -> Any:
        return self._client.collection(EVENTS).document(event_id)

    def delivery(self, delivery_id: str) -> Any:
        return self._client.collection(DELIVERIES).document(delivery_id)


def _run_transaction(body: Callable[[Any], Any]) -> Any:
    """Run ``body`` under a real Firestore transaction.

    ``@firestore.transactional`` is the only form the Python client offers -- there is no
    context-manager shape on ``Transaction`` -- and it retries the body up to five times, then
    raises a plain ``ValueError``. Nothing is caught here: the classification a caller needs is
    :func:`is_retryable`, and swallowing at this level would turn a contended subject into a
    silently dropped one.
    """
    from google.cloud import firestore

    client = _firestore_client()
    transaction = client.transaction()

    @firestore.transactional
    def _wrapped(txn: Any) -> Any:
        return body(txn)

    return _wrapped(transaction)


def is_retryable(exc: BaseException) -> bool:
    """Should this store failure be retried on a later cycle rather than treated as terminal?

    The case that matters and that a natural implementation misses: an **exhausted transaction**.
    The client retries a contended body five times and then raises a plain ``ValueError``, not a
    ``google.api_core`` error -- so a handler keyed on API exceptions classifies it as terminal, the
    subject is marked ``failed``, and a train nobody could commit an event for is never delivered.

    Any OTHER ``ValueError`` is terminal, because every other ``ValueError`` reachable from this
    module is its own validation.
    """
    if isinstance(exc, ValueError):
        return str(exc).startswith(_exhausted_transaction_prefix())
    if isinstance(exc, (NotFound, Forbidden, Conflict, QuotaExceeded, NotConfigured)):
        return False
    try:
        from google.api_core import exceptions as api_exceptions
    except ImportError:  # pragma: no cover - the library is a hard dependency of the channel
        return False
    return isinstance(
        exc,
        (
            api_exceptions.Aborted,
            api_exceptions.Cancelled,
            api_exceptions.DeadlineExceeded,
            api_exceptions.InternalServerError,
            api_exceptions.ResourceExhausted,
            api_exceptions.ServiceUnavailable,
            api_exceptions.TooManyRequests,
            api_exceptions.Unknown,
        ),
    )


def _exhausted_transaction_prefix() -> str:
    """Prefer the client's own constant; fall back to the literal it currently holds."""
    try:
        from google.cloud.firestore_v1.base_transaction import (
            _EXCEED_ATTEMPTS_TEMPLATE,
        )
    except ImportError:  # pragma: no cover
        return _EXHAUSTED_TRANSACTION_PREFIX
    prefix = _EXCEED_ATTEMPTS_TEMPLATE.split("{", 1)[0].strip()
    return prefix or _EXHAUSTED_TRANSACTION_PREFIX


# ---- Subscriptions ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SubscriptionSpec:
    """What a client submits. /varsel's registration shape plus a channel.

    ``subscription_id`` is caller-supplied so a create is safely retryable (R5): the same id with
    the same definition returns the stored record and writes nothing, while the same id with a
    different definition is a :class:`Conflict` rather than a silent overwrite.

    ``test`` is IMMUTABLE (R15) -- changing it requires delete and re-create -- because it rides
    inside the signed event body and a receiver refuses a test event before native dispatch. A
    subscription that could be flipped live would turn every previously-signed test event into a
    real notification.
    """

    subscription_id: str
    client_id: str
    destination_id: str
    from_stop: str
    to_stop: str
    begin: str
    end: str
    days: tuple[int, ...]
    external_ref: str = ""
    test: bool = False

    def validate(self) -> None:
        """/varsel's own three window rules, restated rather than imported.

        ``varsel.JourneyIn`` requires a ``device_id``, which a webhook subscription by definition
        does not have, and ``varsel._parse_webhook_recipient`` restates the same three rules on the
        read side for its own reason (it validates documents written by a possibly newer binary).
        This is the third statement and the suite pins all three against each other: overnight
        windows are refused here exactly as they are in /varsel, or a client could register a
        window /varsel would not and R3's parity claim would be quietly false.
        """
        if not self.subscription_id or not self.client_id or not self.destination_id:
            raise Conflict("subscription_id, client_id and destination_id are required")
        if not self.from_stop or not self.to_stop:
            raise Conflict("from_stop and to_stop must be non-empty")
        if self.from_stop == self.to_stop:
            raise Conflict("from_stop and to_stop must differ")
        if not _HHMM_RE.match(self.begin) or not _HHMM_RE.match(self.end):
            raise Conflict("begin/end must be HH:MM (24h)")
        if self.begin >= self.end:
            raise Conflict("begin must be before end (overnight windows are not supported)")
        days = sorted(set(self.days))
        if not days or any(d < 0 or d > 6 for d in days):
            raise Conflict("days must be a non-empty subset of 0..6 (0=Monday)")


@dataclass(frozen=True)
class Subscription:
    """A stored subscription as every reader sees it."""

    subscription_id: str
    client_id: str
    destination_id: str
    from_stop: str
    to_stop: str
    begin: str
    end: str
    days: tuple[int, ...]
    external_ref: str
    test: bool
    version: int
    status: str
    created_at: datetime | None
    updated_at: datetime | None
    last_seen_at: datetime | None
    expires_at: datetime | None
    lifetime_expires_at: datetime | None
    inactivity_expires_at: datetime | None
    # R6: whether a direct trip can serve this pair, and how fresh that answer is. `None` means
    # "not established" and never reads as an all-clear -- U4 renders the difference.
    servable: bool | None = None
    servable_checked_at: datetime | None = None
    daily_deliveries: Mapping[str, int] = field(default_factory=dict)
    #: ``delivery_id`` -> ``{"day", "at"}`` for slots RESERVED and not yet confirmed or released.
    #: Kept separate from ``daily_deliveries`` on purpose: that map stays the count of confirmed
    #: sends, which is what an operator reads and what the delivery suite asserts on, while this one
    #: is admission control with a lapse. :func:`cap_remaining` is the only thing that adds them.
    daily_reservations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    delivered: Mapping[str, datetime] = field(default_factory=dict)
    cleanup: Mapping[str, Any] = field(default_factory=dict)

    def as_recipient_document(self, now: datetime) -> dict[str, Any]:
        """The raw document ``varsel.WebhookRecipientProvider`` is contracted to yield.

        Exactly the keys ``varsel._parse_webhook_recipient`` reads, plus the ids U6 needs to route
        a match back to a destination. The extra keys are safe by that function's own design: it
        validates the fields the pass reads and is indifferent to the rest, so a store that adds a
        field does not start failing every document in the collection.

        ``delivered`` is Firestore's copy of ``varsel._notified`` at the same grain -- situation
        numbers already delivered to THIS subscription -- and the marks whose window has passed are
        dropped here rather than stored forever.
        """
        return {
            "recipient_id": self.subscription_id,
            "subscription_id": self.subscription_id,
            "client_id": self.client_id,
            "destination_id": self.destination_id,
            "external_ref": self.external_ref,
            "version": self.version,
            "test": self.test,
            "from_stop": self.from_stop,
            "to_stop": self.to_stop,
            "begin": self.begin,
            "end": self.end,
            "days": list(self.days),
            "delivered": sorted(_prune_marks(self.delivered, now)),
        }


def _subscription_from_doc(doc: Mapping[str, Any]) -> Subscription:
    """Parse one stored document, raising on anything the running binary cannot read.

    Raises rather than returning ``None``: the caller's per-document guard is what turns a bad
    document into a skipped document (R20), and a sentinel return would let a later edit forget to
    check it.
    """
    days = tuple(sorted({int(d) for d in doc["days"]}))
    # Non-empty only while the subscription is ACTIVE. A completed cleanup deliberately redacts the
    # tombstone -- route, window, weekdays, client reference -- so requiring days here made the
    # SECOND delete of a fully cleaned-up subscription raise instead of being idempotent, which is a
    # 500 on a retry rather than the "already gone, nothing to do" the caller is owed.
    if not days and doc.get("status") == "active":
        raise ValueError("an active subscription must name at least one weekday")
    return Subscription(
        subscription_id=str(doc["subscription_id"]),
        client_id=str(doc["client_id"]),
        destination_id=str(doc["destination_id"]),
        from_stop=str(doc["from_stop"]),
        to_stop=str(doc["to_stop"]),
        begin=str(doc["begin"]),
        end=str(doc["end"]),
        days=days,
        external_ref=str(doc.get("external_ref") or ""),
        test=bool(doc.get("test")),
        version=int(doc.get("version") or 0),
        status=str(doc.get("status") or ""),
        created_at=_as_dt(doc.get("created_at")),
        updated_at=_as_dt(doc.get("updated_at")),
        last_seen_at=_as_dt(doc.get("last_seen_at")),
        expires_at=_as_dt(doc.get(TTL_FIELD)),
        lifetime_expires_at=_as_dt(doc.get("lifetime_expires_at")),
        inactivity_expires_at=_as_dt(doc.get("inactivity_expires_at")),
        servable=doc.get("servable") if isinstance(doc.get("servable"), bool) else None,
        servable_checked_at=_as_dt(doc.get("servable_checked_at")),
        daily_deliveries=dict(doc.get("daily_deliveries") or {}),
        daily_reservations={
            str(k): dict(v)
            for k, v in (doc.get("daily_reservations") or {}).items()
            if isinstance(v, Mapping)
        },
        delivered={k: v for k, v in (doc.get("delivered") or {}).items()},
        cleanup=dict(doc.get("cleanup") or {}),
    )


def _new_subscription_doc(spec: SubscriptionSpec, now: datetime) -> dict[str, Any]:
    return {
        "subscription_id": spec.subscription_id,
        "client_id": spec.client_id,
        "destination_id": spec.destination_id,
        "from_stop": spec.from_stop,
        "to_stop": spec.to_stop,
        "begin": spec.begin,
        "end": spec.end,
        "days": [int(d) for d in sorted(set(spec.days))],
        "external_ref": spec.external_ref,
        "test": bool(spec.test),
        "version": 1,
        "status": "active",
        "definition_digest": _definition_digest(spec),
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "servable": None,
        "servable_checked_at": None,
        "daily_deliveries": {},
        "daily_reservations": {},
        "delivered": {},
        "cleanup": {},
        **_horizons(now, now),
    }


def _create_body(
    transaction: Any,
    refs: _Refs,
    count_query: Any,
    *,
    spec: SubscriptionSpec,
    now: datetime,
) -> tuple[str, dict[str, Any]]:
    """Create one subscription, refusing past the ceiling BEFORE any write.

    The ceiling is counted inside the transaction from the client's own subscriptions rather than
    from a counter document, deliberately. A counter over-counts and drifts: a subscription past one
    of R5's horizons is still ``status == "active"`` until TTL deletes it up to ~24 h later, so a
    client would be locked out by dead rows, and a crash between the tombstone and the decrement
    would leave the counter permanently wrong with nothing to reconcile it against. Counting costs
    up to ``MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT`` read locks under Standard edition's pessimistic
    concurrency -- bounded, and paid on a create, which a commuter does once.

    R21's "the read cannot exceed the ceiling" is literally the query's ``limit``.
    """
    existing = _read(transaction, refs.subscription(spec.subscription_id))
    stored = _doc(existing)
    if stored:
        if stored.get("client_id") != spec.client_id:
            raise Forbidden("subscription id belongs to another client")
        # LIVENESS BEFORE THE DIGEST, or a tombstoned subscription is handed back through a 2xx as
        # an "unchanged" success -- with `status: "deleted"` on it. `_delete_body` does not clear
        # `definition_digest` (only the cleanup's redaction does), so the window where a delete and
        # a same-definition re-POST look like a safe retry is exactly the window between the
        # tombstone and the cleanup completing, which is where nobody is looking.
        #
        # An expired-but-not-deleted document lands here too, and refusing is right for the same
        # reason: the id is spent, its record is the audit trail, and a re-registration is a new
        # subscription rather than a resurrection.
        if not subscription_is_live(stored, now):
            raise Conflict(
                "subscription id was deleted or has passed one of its horizons; use a new id"
            )
        if stored.get("definition_digest") == _definition_digest(spec):
            # A safely retried create (R5). No write at all, so a retry cannot bump a version, reset
            # a horizon or clear a spent cap slot.
            return "unchanged", stored
        raise Conflict("subscription id already exists with a different definition")
    live = 0
    for snapshot in _read_query(transaction, count_query):
        if subscription_is_live(_doc(snapshot), now):
            live += 1
    if live >= MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT:
        raise QuotaExceeded(
            f"client already holds {live} active subscriptions "
            f"(ceiling {MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT})"
        )
    doc = _new_subscription_doc(spec, now)
    transaction.set(refs.subscription(spec.subscription_id), doc)
    return "created", doc


def create_subscription(spec: SubscriptionSpec, *, now: datetime | None = None) -> Subscription:
    """Register a subscription. Idempotent on (id, definition); refused past the ceiling."""
    spec.validate()
    instant = now or _now()
    refs = _Refs(_firestore_client())
    count_query = _query(
        "subscriptions",
        equality={"client_id": spec.client_id, "status": "active"},
        limit=MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT,
    )
    outcome, doc = _run_transaction(
        lambda txn: _create_body(txn, refs, count_query, spec=spec, now=instant)
    )
    logger.info(
        "Webhook subscription %s (%s)",
        spec.subscription_id,
        outcome,
        extra={
            "fields": {
                "event": "notification_subscription",
                "result": outcome,
                "client_id": spec.client_id,
                "subscription_id": spec.subscription_id,
                "test": bool(spec.test),
            }
        },
    )
    return _subscription_from_doc(doc)


def _replace_body(
    transaction: Any,
    refs: _Refs,
    *,
    spec: SubscriptionSpec,
    expected_version: int | None,
    now: datetime,
) -> dict[str, Any]:
    """Amend a subscription, refusing an obsolete version and an immutable-field change.

    ``expected_version`` is the optimistic-concurrency check R13 and the event contract both need:
    an emission that read version N must not commit against a subscription some concurrent request
    has already moved to N+1.
    """
    snapshot = _read(transaction, refs.subscription(spec.subscription_id))
    stored = _doc(snapshot)
    if not stored:
        raise NotFound("no such subscription")
    if stored.get("client_id") != spec.client_id:
        raise Forbidden("subscription belongs to another client")
    if not subscription_is_live(stored, now):
        raise NotFound("subscription is deleted or past one of its horizons")
    if expected_version is not None and int(stored.get("version") or 0) != expected_version:
        raise Conflict(
            f"version is {stored.get('version')}, not {expected_version}"
        )
    if bool(stored.get("test")) != bool(spec.test):
        raise Conflict("the test marker is immutable; delete and re-create instead")
    created = _as_dt(stored.get("created_at")) or now
    patch: dict[str, Any] = {
        "destination_id": spec.destination_id,
        "from_stop": spec.from_stop,
        "to_stop": spec.to_stop,
        "begin": spec.begin,
        "end": spec.end,
        "days": [int(d) for d in sorted(set(spec.days))],
        "external_ref": spec.external_ref,
        "definition_digest": _definition_digest(spec),
        "version": int(stored.get("version") or 0) + 1,
        "updated_at": now,
        "last_seen_at": now,
        # A changed route is a different servability question, so the stored answer is retired
        # rather than carried over: R6 says a stale answer must never read as an all-clear.
        "servable": None,
        "servable_checked_at": None,
        **_horizons(created, now),
    }
    # Two things an amendment deliberately does NOT reset. `daily_deliveries`, because R11's cap is
    # per subscription per Oslo day and letting an amendment clear it would be a cap bypass anyone
    # could drive from the API. And `delivered`, because those marks are per situation number: a
    # mark for a train the new route does not ride simply never matches again and expires on its
    # own, while clearing them would re-notify every train already delivered today.
    transaction.update(refs.subscription(spec.subscription_id), patch)
    return {**stored, **patch}


def replace_subscription(
    spec: SubscriptionSpec,
    *,
    expected_version: int | None = None,
    now: datetime | None = None,
) -> Subscription:
    spec.validate()
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc = _run_transaction(
        lambda txn: _replace_body(
            txn, refs, spec=spec, expected_version=expected_version, now=instant
        )
    )
    return _subscription_from_doc(doc)


def _delete_body(
    transaction: Any, refs: _Refs, *, subscription_id: str, client_id: str, now: datetime
) -> dict[str, Any] | None:
    """Tombstone first, in one transactional write that stops routing.

    Order matters and the reverse is the bug: delete the events first and a partial failure leaves a
    ROUTABLE subscription whose events are gone. The tombstone stops routing (the load query filters
    ``status == "active"``) and hands the rest to :func:`cleanup_subscription`, which is retryable
    and carries an auditable completion state.

    Idempotent in both directions: an unknown id returns ``None`` and an already-tombstoned one
    returns its record with no write, so a retried DELETE cannot bump a version forever.
    """
    snapshot = _read(transaction, refs.subscription(subscription_id))
    stored = _doc(snapshot)
    if not stored:
        return None
    if stored.get("client_id") != client_id:
        raise Forbidden("subscription belongs to another client")
    if stored.get("status") == "deleted":
        return stored
    patch = {
        "status": "deleted",
        "deleted_at": now,
        "version": int(stored.get("version") or 0) + 1,
        "updated_at": now,
        # The tombstone is history: it keeps the delete idempotent and the cleanup auditable, so it
        # lives on the history window rather than the subscription's own horizons. Its route is
        # redacted the moment cleanup completes -- see `_finish_cleanup_body`.
        TTL_FIELD: now + timedelta(days=HISTORY_DAYS),
        "lifetime_expires_at": now,
        "inactivity_expires_at": now,
        "cleanup": {"state": "pending", "attempts": 0, "requested_at": now},
    }
    transaction.update(refs.subscription(subscription_id), patch)
    return {**stored, **patch}


def delete_subscription(
    subscription_id: str, *, client_id: str, now: datetime | None = None
) -> Subscription | None:
    """Tombstone a subscription. Returns ``None`` if it never existed."""
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc = _run_transaction(
        lambda txn: _delete_body(
            txn, refs, subscription_id=subscription_id, client_id=client_id, now=instant
        )
    )
    if doc is None:
        return None
    logger.info(
        "Webhook subscription %s tombstoned",
        subscription_id,
        extra={
            "fields": {
                "event": "notification_subscription",
                "result": "deleted",
                "client_id": client_id,
                "subscription_id": subscription_id,
            }
        },
    )
    return _subscription_from_doc(doc)


def _touch_body(
    transaction: Any, refs: _Refs, *, subscription_id: str, client_id: str, now: datetime
) -> dict[str, Any]:
    """Push the inactivity horizon out. Reads first, so it cannot resurrect a tombstone."""
    snapshot = _read(transaction, refs.subscription(subscription_id))
    stored = _doc(snapshot)
    if not stored:
        raise NotFound("no such subscription")
    if stored.get("client_id") != client_id:
        raise Forbidden("subscription belongs to another client")
    if not subscription_is_live(stored, now):
        raise NotFound("subscription is deleted or past one of its horizons")
    created = _as_dt(stored.get("created_at")) or now
    patch = {"last_seen_at": now, "updated_at": now, **_horizons(created, now)}
    transaction.update(refs.subscription(subscription_id), patch)
    return {**stored, **patch}


def touch_subscription(
    subscription_id: str, *, client_id: str, now: datetime | None = None
) -> Subscription:
    """Record that the client is still interested -- R5's inactivity horizon moves with this."""
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc = _run_transaction(
        lambda txn: _touch_body(
            txn, refs, subscription_id=subscription_id, client_id=client_id, now=instant
        )
    )
    return _subscription_from_doc(doc)


def _servability_body(
    transaction: Any,
    refs: _Refs,
    *,
    subscription_id: str,
    servable: bool | None,
    expected_version: int,
    now: datetime,
) -> dict[str, Any]:
    snapshot = _read(transaction, refs.subscription(subscription_id))
    stored = _doc(snapshot)
    if not stored:
        raise NotFound("no such subscription")
    if stored.get("status") != "active":
        raise NotFound("subscription is deleted")
    if int(stored.get("version") or 0) != expected_version:
        raise Conflict(
            f"version is {stored.get('version')}, not {expected_version}; the route was replaced "
            "while this servability check was in flight"
        )
    patch = {"servable": servable, "servable_checked_at": now, "updated_at": now}
    transaction.update(refs.subscription(subscription_id), patch)
    return {**stored, **patch}


def set_servability(
    subscription_id: str,
    *,
    servable: bool | None,
    expected_version: int,
    now: datetime | None = None,
) -> Subscription:
    """Record R6's servability answer and its freshness, for the route that was ASKED about.

    ``None`` means "could not be established" -- an outage, a timeout -- and is stored as such
    rather than as ``False``: a subscription is NEVER refused for being unservable (R5), the caveat
    simply rides on the record, and "we do not know" must not render as "no trips run".

    ``expected_version`` is what makes the answer about a route rather than about an id. The check
    is a ~20 s BigQuery question (`main._CONNECTIVITY_TIMEOUT_S`) and a replace is one request, so
    they interleave: ask about Oslo S -> Drammen, the client replaces the watch with Oslo S -> Ski,
    the answer lands and stamps the CURRENT record with a verdict about the old route -- and nothing
    in the document then says so, because `servable` reads as an answer and `servable_checked_at`
    reads as fresh, which is the pair R6 publishes so a caveat's age can be judged.
    ``_replace_subscription`` retires both fields for exactly this reason; a late writer must not
    restore them.

    REQUIRED, never ``None``-defaulted, and the version rather than the definition digest: a replace
    bumps the version and a ``touch`` does not, so a keep-alive during a slow check is correctly not
    a conflict while every route change is. Enforced INSIDE the transaction -- a read-then-compare
    outside it is the same race one layer up.
    """
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc = _run_transaction(
        lambda txn: _servability_body(
            txn,
            refs,
            subscription_id=subscription_id,
            servable=servable,
            expected_version=expected_version,
            now=instant,
        )
    )
    return _subscription_from_doc(doc)


def get_subscription(
    subscription_id: str, *, client_id: str, now: datetime | None = None
) -> Subscription | None:
    """One subscription, or ``None`` when it is missing, tombstoned or past a horizon.

    Tenancy is checked here and not left to the caller: every subscription-scoped method in this
    module takes ``client_id`` and refuses a mismatch, so one client cannot read or affect
    another's records through any repository method.
    """
    instant = now or _now()
    snapshot = _Refs(_firestore_client()).subscription(subscription_id).get(timeout=_CALL_DEADLINE_S)
    stored = _doc(snapshot)
    if not stored:
        return None
    if stored.get("client_id") != client_id:
        raise Forbidden("subscription belongs to another client")
    if not subscription_is_live(stored, instant):
        return None
    return _subscription_from_doc(stored)


def list_subscriptions(
    *,
    client_id: str,
    limit: int = MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT,
    after: str | None = None,
    now: datetime | None = None,
) -> list[Subscription]:
    """A client's own live subscriptions, oldest first, starting after ``after``.

    **``limit`` reaches the QUERY**, and did not until 2026-09-09: this streamed the whole bounded
    set and the route sliced in Python, so a `?limit=1` page cost the same up-to-500-document read
    as the whole listing -- against a per-credential ceiling of
    ``notification_auth.PER_CLIENT_PER_MIN`` (120) that is a read amplification driven by a client's
    polling shape rather than by what they asked for. ``after`` is the document id of the last row
    of the previous page; it is resolved to a snapshot (one document read) and handed to the query
    as a cursor, so paging costs the page rather than the collection.

    ``NotFound`` for a cursor that is missing, another client's, or no longer live -- the route
    turns that into a 400. "That watch is gone" and "nothing matched" are different answers and a
    client that cannot tell them apart shows the second when it means the first.

    **The loop is what keeps the page FULL**, and it is not decoration. Expiry is tested in Python
    (``subscription_is_live``) because a query cannot express both horizons, and Firestore deletes an
    expired document only "typically within 24 hours" -- so a page of ``limit`` documents can come
    back with fewer live rows, and returning them alone would tell a client the listing had ended
    while live rows remained. It reads ``limit`` at a time and stops at the collection's own
    ceiling, so a healthy list still costs exactly one round.
    """
    instant = now or _now()
    ceiling = min(limit, MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT)
    cursor: Any = None
    if after is not None:
        snapshot = _Refs(_firestore_client()).subscription(after).get(timeout=_CALL_DEADLINE_S)
        stored = _doc(snapshot)
        if (
            not stored
            or stored.get("client_id") != client_id
            or not subscription_is_live(stored, instant)
        ):
            raise NotFound("no such subscription to page from")
        cursor = snapshot
    out: list[Subscription] = []
    scanned = 0
    while len(out) < ceiling and scanned < MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT:
        requested = min(ceiling - len(out), MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT - scanned)
        page = list(
            _query(
                "subscriptions",
                equality={"client_id": client_id, "status": "active"},
                limit=requested,
                start_after=cursor,
            ).stream(timeout=_CALL_DEADLINE_S)
        )
        scanned += len(page)
        for snapshot in page:
            stored = _doc(snapshot)
            if not subscription_is_live(stored, instant):
                continue
            try:
                out.append(_subscription_from_doc(stored))
            except Exception:
                logger.debug("Unreadable webhook subscription document; skipping it", exc_info=True)
        # A short page means the collection is exhausted, so another round would read nothing --
        # and "fewer subscriptions than the page size" is the ORDINARY case, so a speculative second
        # query would add one to every list request rather than to the rare one that needs it.
        if len(page) < requested:
            break
        cursor = page[-1]
    return out[:ceiling]


@dataclass(frozen=True)
class LoadedSubscriptions:
    subscriptions: tuple[Subscription, ...]
    truncated: bool
    skipped: int
    expired: int


def active_subscriptions(
    *, limit: int = MAX_SUBSCRIPTIONS_PER_CYCLE, now: datetime | None = None
) -> LoadedSubscriptions:
    """Every live subscription in this environment, bounded by R21's per-cycle ceiling.

    The single read the notification pass depends on. Three things it reports rather than hides:
    ``truncated`` (the ceiling bound the page, which at a pilot's volume is a store bug or a quota
    breach rather than growth), ``expired`` (documents TTL has not caught up with -- the cost of
    testing expiry in Python instead of in the query, and the reason the query orders by
    ``expires_at`` DESCENDING) and ``skipped`` (documents this binary cannot read, skipped one at a
    time so one bad document never fails its collection -- R20).
    """
    instant = now or _now()
    ceiling = min(limit, MAX_SUBSCRIPTIONS_PER_CYCLE)
    query = _query("load_active_subscriptions", equality={"status": "active"}, limit=ceiling)
    page = list(query.stream(timeout=_CALL_DEADLINE_S))
    out: list[Subscription] = []
    skipped = expired = 0
    for snapshot in page:
        stored = _doc(snapshot)
        if not subscription_is_live(stored, instant):
            expired += 1
            continue
        try:
            out.append(_subscription_from_doc(stored))
        except Exception:
            skipped += 1
            logger.debug("Unreadable webhook subscription document; skipping it", exc_info=True)
    return LoadedSubscriptions(
        subscriptions=tuple(out),
        truncated=len(page) >= ceiling,
        skipped=skipped,
        expired=expired,
    )


def recipient_documents() -> list[dict[str, Any]]:
    """``varsel.WebhookRecipientProvider``: the raw documents this cycle should match.

    Raw documents rather than parsed records, because that Protocol is explicit about why: R20 wants
    an unreadable document skipped rather than failing its collection, and only the READER can
    decide what "unreadable" means for the schema the running binary knows. So this hands over the
    shape and ``varsel._parse_webhook_recipient`` validates it.
    """
    instant = _now()
    loaded = active_subscriptions(now=instant)
    # `expired` IS NOT A WARNING, and this used to warn on it. TTL deletes an expired document
    # "typically within 24 hours", so during that documented lag a perfectly healthy deployment logs
    # a warning on EVERY cycle -- and `terraform/firestore.tf`'s own header makes the argument
    # against that better than a comment here can: a standing warning is how a real one stops being
    # read. The two conditions worth waking someone for stay warnings: `truncated` is R21's ceiling
    # binding, which at a pilot's volume is a store bug or a quota breach rather than growth, and
    # `skipped` is a document this binary cannot read, which is a schema or deploy problem. The
    # ordinary counts still go out on one DEBUG record under the same event name, so "is TTL keeping
    # up?" is still answerable -- just not by a warning.
    fields = {
        "event": "notification_store_load",
        "n": len(loaded.subscriptions),
        "skipped": loaded.skipped,
        "expired": loaded.expired,
        "truncated": loaded.truncated,
    }
    if loaded.truncated or loaded.skipped:
        logger.warning(
            "Webhook subscription load bounded: %d loaded, %d skipped, truncated=%s",
            len(loaded.subscriptions),
            loaded.skipped,
            loaded.truncated,
            extra={"fields": fields},
        )
    else:
        logger.debug(
            "Webhook subscription load: %d loaded, %d expired but not yet TTL-deleted",
            len(loaded.subscriptions),
            loaded.expired,
            extra={"fields": fields},
        )
    return [s.as_recipient_document(instant) for s in loaded.subscriptions]


# ---- Ordered, durable deletion ----------------------------------------------------------------


@dataclass(frozen=True)
class CleanupResult:
    subscription_id: str
    done: bool
    deleted_events: int
    deleted_deliveries: int
    deleted_subjects: int
    attempts: int


def _finish_cleanup_body(
    transaction: Any,
    refs: _Refs,
    *,
    subscription_id: str,
    now: datetime,
    done: bool,
    deleted: Mapping[str, int],
) -> dict[str, Any]:
    """Record the cleanup's auditable state, and redact the tombstone once it has completed.

    Redaction rather than deletion of the tombstone: the residual record is what makes a repeated
    DELETE idempotent and the completion auditable, and once the events are gone it need carry no
    route at all. So the route, window, weekdays, client reference and both per-subscription maps
    are dropped and the ids and timestamps stay -- which is also what keeps the published retention
    text true, since after this write nothing client-identifying but the ids survives the TTL
    window.
    """
    snapshot = _read(transaction, refs.subscription(subscription_id))
    stored = _doc(snapshot)
    if not stored:
        raise NotFound("no such subscription")
    cleanup = dict(stored.get("cleanup") or {})
    attempts = int(cleanup.get("attempts") or 0) + 1
    patch: dict[str, Any] = {
        "cleanup": {
            **cleanup,
            "state": "done" if done else "pending",
            "attempts": attempts,
            "last_attempt_at": now,
            "deleted_events": int(cleanup.get("deleted_events") or 0) + deleted["events"],
            "deleted_deliveries": int(cleanup.get("deleted_deliveries") or 0)
            + deleted["deliveries"],
            "deleted_subjects": int(cleanup.get("deleted_subjects") or 0) + deleted["subjects"],
            **({"completed_at": now} if done else {}),
        },
        "updated_at": now,
    }
    if done:
        patch.update(
            {
                "from_stop": "",
                "to_stop": "",
                "begin": "",
                "end": "",
                "days": [],
                "external_ref": "",
                "definition_digest": "",
                "delivered": {},
                "daily_deliveries": {},
                "daily_reservations": {},
                "redacted_at": now,
            }
        )
    transaction.update(refs.subscription(subscription_id), patch)
    return {**stored, **patch}


def cleanup_subscription(
    subscription_id: str, *, batch: int = 200, now: datetime | None = None
) -> CleanupResult:
    """Delete a tombstoned subscription's events, deliveries and subjects, in batches.

    Retryable with an auditable completion state (R19): ``done=False`` means there was a full page
    left, so the caller re-runs; ``done=True`` records ``cleanup.completed_at`` and redacts the
    tombstone. Best effort alone would leave client data queryable while the published text said
    otherwise, which is the exact failure R19 exists to prevent -- so where this has not completed,
    TTL is the fallback and :data:`TTL_RESIDUAL_HOURS` is the published residual.

    Not one transaction: a subscription can hold more documents than a transaction may touch, and a
    partial batch delete is safe to repeat because a delete is idempotent. The tombstone is already
    in place, so nothing routes to the subscription while this runs.
    """
    instant = now or _now()
    client = _firestore_client()
    refs = _Refs(client)
    tombstone = _doc(refs.subscription(subscription_id).get(timeout=_CALL_DEADLINE_S))
    if not tombstone:
        raise NotFound("no such subscription")
    if tombstone.get("status") != "deleted":
        raise Conflict("cleanup runs after the tombstone, never before it")

    deleted = {"events": 0, "deliveries": 0, "subjects": 0}
    full_page = False
    for key, spec_name in (
        ("events", "cleanup_events"),
        ("deliveries", "cleanup_deliveries"),
        ("subjects", "cleanup_subjects"),
    ):
        query = _query(spec_name, equality={"subscription_id": subscription_id}, limit=batch)
        page = list(query.stream(timeout=_CALL_DEADLINE_S))
        if page:
            writer = client.batch()
            for snapshot in page:
                writer.delete(snapshot.reference)
            writer.commit()
        deleted[key] = len(page)
        full_page = full_page or len(page) >= batch

    done = not full_page
    doc = _run_transaction(
        lambda txn: _finish_cleanup_body(
            txn, refs, subscription_id=subscription_id, now=instant, done=done, deleted=deleted
        )
    )
    state = dict(doc.get("cleanup") or {})
    logger.info(
        "Client cleanup for %s: %s",
        subscription_id,
        "done" if done else "more to do",
        extra={
            "fields": {
                "event": "notification_cleanup",
                "result": "done" if done else "pending",
                "subscription_id": subscription_id,
                "deleted_events": deleted["events"],
                "deleted_deliveries": deleted["deliveries"],
                "deleted_subjects": deleted["subjects"],
            }
        },
    )
    return CleanupResult(
        subscription_id=subscription_id,
        done=done,
        deleted_events=deleted["events"],
        deleted_deliveries=deleted["deliveries"],
        deleted_subjects=deleted["subjects"],
        attempts=int(state.get("attempts") or 0),
    )


# ---- Alert subjects and events -----------------------------------------------------------------


def _transition(stored_phase: str | None, requested_phase: str) -> str | None:
    """Which event type, if any, this observation mints. ``None`` means nothing is minted.

    **A new revision is minted only on a lifecycle transition.** A changed probability, delay figure
    or wording mints nothing, at any size: there is no materiality threshold to tune, because
    minting on a changed figure would turn every poller cycle into a fresh revision and therefore a
    second notification about the same train. This is also why the store owns the decision instead
    of taking a caller-supplied digest of the facts -- a payload digest changes every cycle, and
    nothing downstream would notice.
    """
    if stored_phase is None:
        return EVENT_CREATED
    if stored_phase == requested_phase:
        return None
    if stored_phase == PHASE_CONFIRMED and requested_phase == PHASE_PREDICTED:
        # R14's third suppression rule, applied a layer earlier so the event never exists: never
        # dispatch a `predicted` revision for a train already `confirmed`, whatever its revision.
        return None
    return EVENT_UPDATED


@dataclass(frozen=True)
class AlertSubject:
    alert_id: str
    subscription_id: str
    client_id: str
    destination_id: str
    revision: int
    phase: str | None
    below_threshold_cycles: int
    # When the run the count belongs to was last extended. `None` means no run is open, which is
    # what a mint leaves behind and what a fresh subject starts as.
    below_threshold_at: datetime | None
    last_event_id: str | None
    pending_delivery_id: str | None
    delivered_revision: int | None
    updated_at: datetime | None
    expires_at: datetime | None


def _subject_from_doc(doc: Mapping[str, Any]) -> AlertSubject:
    return AlertSubject(
        alert_id=str(doc.get("alert_id") or ""),
        subscription_id=str(doc.get("subscription_id") or ""),
        client_id=str(doc.get("client_id") or ""),
        destination_id=str(doc.get("destination_id") or ""),
        revision=int(doc.get("revision") or 0),
        phase=doc.get("phase") or None,
        below_threshold_cycles=int(doc.get("below_threshold_cycles") or 0),
        below_threshold_at=_as_dt(doc.get("below_threshold_at")),
        last_event_id=doc.get("last_event_id") or None,
        pending_delivery_id=doc.get("pending_delivery_id") or None,
        delivered_revision=(
            int(doc["delivered_revision"]) if doc.get("delivered_revision") is not None else None
        ),
        updated_at=_as_dt(doc.get("updated_at")),
        expires_at=_as_dt(doc.get(TTL_FIELD)),
    )


@dataclass(frozen=True)
class EmitRequest:
    """One observation about one train for one subscription.

    ``payload`` is the event body's payload as U6 built it -- the affected call, probabilities,
    delay figures, contributing situations and the Norwegian ``notification`` title and body. The
    store stores it verbatim and never inspects it: R7's "normal receipt needs no second request to
    us" is a property of what the payload carries, and the store minting on a payload CHANGE would
    undo the transition rule above.
    """

    subscription_id: str
    client_id: str
    destination_id: str
    subscription_version: int
    train: TrainRef
    phase: str
    situations: tuple[str, ...]
    payload: Mapping[str, Any]
    valid_until: datetime
    external_ref: str = ""
    test: bool = False

    @property
    def alert_id(self) -> str:
        return alert_subject_id(self.subscription_id, self.train)


@dataclass(frozen=True)
class EventCommit:
    """What an emit attempt did. ``status`` is the whole outcome vocabulary.

    ``created``    a revision was minted; ``event_id`` and ``delivery_id`` name it.
    ``unchanged``  no lifecycle transition, so no new revision (the common case, every cycle).
    ``counting``   a below-threshold observation was recorded but has not reached the withdrawal
                   threshold yet.
    ``obsolete``   the subscription is gone, tombstoned, expired, or at a different version than
                   the caller read; NOTHING was written. ``reason`` says which.
    """

    status: str
    alert_id: str
    event_id: str | None = None
    delivery_id: str | None = None
    revision: int = 0
    event_type: str | None = None
    superseded_delivery_id: str | None = None
    below_threshold_cycles: int = 0
    cap_exempt: bool = False
    reason: str | None = None


def _subscription_obsolete(
    stored: Mapping[str, Any], *, request: EmitRequest, now: datetime
) -> str | None:
    """Why this emission is obsolete, or ``None`` if the subscription still authorises it.

    R13's "deletion or expiry stops later claims for that subscription", enforced at the moment of
    the write rather than only at the read that produced the match -- the pass matched against a
    snapshot taken before pass 1a, and a delete can land in between.
    """
    if not stored:
        return "missing"
    if stored.get("client_id") != request.client_id:
        return "foreign"
    if stored.get("status") != "active":
        return "deleted"
    if not subscription_is_live(stored, now):
        return "expired"
    if int(stored.get("version") or 0) != int(request.subscription_version):
        return "version"
    return None


def _mint(
    transaction: Any,
    refs: _Refs,
    *,
    request: EmitRequest,
    subject: Mapping[str, Any],
    event_type: str,
    phase: str,
    now: datetime,
    seed: int,
) -> EventCommit:
    """Write one revision: the event, its pending delivery, and the subject that orders them.

    Starts with the ONE read left to do -- the subject's currently pending delivery -- so the
    reads-before-writes rule holds however this is reached. That read exists because minting a new
    revision must **supersede** the pending delivery of the old one: without it a predicted and a
    confirmed about the same train both go out, and the receiver's revision rule saves the client
    only when the two arrive in order.
    """
    pending_id = subject.get("pending_delivery_id") or None
    pending_doc: dict[str, Any] = {}
    if pending_id:
        pending_doc = _doc(_read(transaction, refs.delivery(str(pending_id))))

    revision = max(int(subject.get("revision") or 0) + 1, seed)
    event_id = event_id_for(request.alert_id, revision)
    # R11's exemption. /varsel's own rule is that a REPLACEMENT is exempt, and R11 names the
    # confirmation case; both are the same shape -- an event that replaces one the traveller has
    # already received about this train. Read that way it also exempts a WITHDRAWAL, deliberately:
    # a retraction the cap suppressed would leave the traveller holding a warning we have withdrawn,
    # which is worse than the notification it saves.
    cap_exempt = subject.get("delivered_revision") is not None

    ttl = now + timedelta(days=HISTORY_DAYS)
    event_doc = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "alert_id": request.alert_id,
        "type": event_type,
        "phase": phase,
        "revision": revision,
        "occurred_at": now,
        "valid_until": request.valid_until,
        "subscription_id": request.subscription_id,
        "subscription_version": int(request.subscription_version),
        "client_id": request.client_id,
        "destination_id": request.destination_id,
        "external_ref": request.external_ref,
        "test": bool(request.test),
        "operator": request.train.operator,
        "line": request.train.line,
        "journey_ref": request.train.journey_ref,
        "call_date": request.train.call_date,
        "stop_place": request.train.stop_place,
        "stop_order": int(request.train.stop_order),
        "situations": list(request.situations),
        "payload": dict(request.payload),
        TTL_FIELD: ttl,
    }
    delivery_doc = {
        # Stable across attempts, because a receiver dedupes on it and because the Standard Webhooks
        # `webhook-id` is this value: the timestamp and signature are fresh per attempt, the id is
        # not. One event reaches one destination, so the two ids coincide by construction; the field
        # is kept explicit because U5 signs THIS one.
        "delivery_id": event_id,
        "event_id": event_id,
        "alert_id": request.alert_id,
        "subscription_id": request.subscription_id,
        "subscription_version": int(request.subscription_version),
        "client_id": request.client_id,
        "destination_id": request.destination_id,
        # The event's own revision, carried here so `_sent_body` can advance the subject's
        # `delivered_revision` to THIS delivery's revision rather than to whatever the subject has
        # reached by the time an attempt returns -- see the note there.
        "revision": revision,
        "state": "pending",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
        # Always a timestamp, never absent and never null. A Firestore range filter excludes
        # documents that are missing the field, and a null does not match `<= <timestamp>` under
        # Firestore's type ordering -- either way `pending_deliveries` would silently never return a
        # fresh delivery, and nothing would look wrong.
        "retry_not_before": now,
        "valid_until": request.valid_until,
        "situations": list(request.situations),
        "cap_exempt": cap_exempt,
        "test": bool(request.test),
        TTL_FIELD: ttl,
    }
    subject_doc = {
        "alert_id": request.alert_id,
        "subscription_id": request.subscription_id,
        "client_id": request.client_id,
        "destination_id": request.destination_id,
        "revision": revision,
        "phase": phase,
        # A mint closes any run of below-threshold observations: the subject has just changed phase,
        # so whatever was counting toward a withdrawal is history. Both fields, not just the count --
        # a stale `below_threshold_at` left behind would make the NEXT dip look like the
        # continuation of a run that ended here.
        "below_threshold_cycles": 0,
        "below_threshold_at": None,
        "last_event_id": event_id,
        "last_event_type": event_type,
        "pending_delivery_id": event_id,
        "delivered_revision": subject.get("delivered_revision"),
        "operator": request.train.operator,
        "line": request.train.line,
        "journey_ref": request.train.journey_ref,
        "call_date": request.train.call_date,
        "stop_place": request.train.stop_place,
        "stop_order": int(request.train.stop_order),
        "created_at": subject.get("created_at") or now,
        "updated_at": now,
        TTL_FIELD: ttl,
    }

    superseded: str | None = None
    if pending_id and pending_doc.get("state") == "pending":
        superseded = str(pending_id)
        transaction.update(
            refs.delivery(superseded),
            {
                "state": "superseded",
                "superseded_by": event_id,
                "superseded_at": now,
                "updated_at": now,
            },
        )
    transaction.set(refs.event(event_id), event_doc)
    transaction.set(refs.delivery(event_id), delivery_doc)
    transaction.set(refs.subject(request.alert_id), subject_doc)
    return EventCommit(
        status="created",
        alert_id=request.alert_id,
        event_id=event_id,
        delivery_id=event_id,
        revision=revision,
        event_type=event_type,
        superseded_delivery_id=superseded,
        cap_exempt=cap_exempt,
    )


def _emit_body(
    transaction: Any, refs: _Refs, *, request: EmitRequest, now: datetime, seed: int
) -> EventCommit:
    subject_snapshot = _read(transaction, refs.subject(request.alert_id))
    subscription = _doc(_read(transaction, refs.subscription(request.subscription_id)))
    subject = _doc(subject_snapshot)
    # An expired subject is a subject that no longer exists: TTL is up to ~24 h late, and reading a
    # stale one would clamp the revision to a stale value and, worse, read a stale PHASE -- a
    # `confirmed` from three days ago suppressing today's forecast about the same dated train.
    if subject and not _not_expired(subject, now):
        subject = {}

    reason = _subscription_obsolete(subscription, request=request, now=now)
    if reason is not None:
        return EventCommit(status="obsolete", alert_id=request.alert_id, reason=reason)

    event_type = _transition(subject.get("phase"), request.phase)
    if event_type is None:
        return EventCommit(
            status="unchanged",
            alert_id=request.alert_id,
            event_id=subject.get("last_event_id") or None,
            delivery_id=subject.get("pending_delivery_id") or None,
            revision=int(subject.get("revision") or 0),
            below_threshold_cycles=int(subject.get("below_threshold_cycles") or 0),
        )
    return _mint(
        transaction,
        refs,
        request=request,
        subject=subject,
        event_type=event_type,
        phase=request.phase,
        now=now,
        seed=seed,
    )


def commit_event(request: EmitRequest, *, now: datetime | None = None) -> EventCommit:
    """Record one observation about one train, minting a revision only on a lifecycle transition.

    The pending set is read from Firestore, never re-derived from a cycle's decisions: a
    re-derivation would mint a fresh revision and therefore a fresh ``event_id`` on every cycle,
    which the receiver cannot recognise as a duplicate and would turn into a second notification
    about the same train.

    The ceiling on pending events per destination is checked BEFORE the transaction. Exact rather
    than best effort, and the reason is nameable instead of hoped for: the emit path has exactly one
    writer (one poller thread, no route invokes the pass, one replica under the Recreate strategy),
    so nothing can create a pending event between the count and the commit.
    """
    if request.phase not in PHASES:
        raise Conflict(f"phase must be one of {PHASES}, not {request.phase!r}")
    instant = now or _now()
    seed = revision_seed(instant)
    refs = _Refs(_firestore_client())
    pending = _count_destination_deliveries(request.destination_id, state="pending")
    if pending >= MAX_PENDING_EVENTS_PER_DESTINATION:
        logger.warning(
            "Destination %s holds %d pending events; refusing to emit (ceiling %d)",
            request.destination_id,
            pending,
            MAX_PENDING_EVENTS_PER_DESTINATION,
            extra={
                "fields": {
                    "event": "notification_quota",
                    "result": "pending_events",
                    "destination_id": request.destination_id,
                    "n": pending,
                }
            },
        )
        raise QuotaExceeded(
            f"destination holds {pending} pending events "
            f"(ceiling {MAX_PENDING_EVENTS_PER_DESTINATION})"
        )
    commit = _run_transaction(
        lambda txn: _emit_body(txn, refs, request=request, now=instant, seed=seed)
    )
    if commit.status == "created":
        logger.info(
            "Notification event %s revision %d (%s)",
            commit.event_id,
            commit.revision,
            commit.event_type,
            extra={
                "fields": {
                    "event": "notification_event",
                    "result": commit.event_type,
                    "client_id": request.client_id,
                    "destination_id": request.destination_id,
                    "alert_id": commit.alert_id,
                    "revision": commit.revision,
                    "superseded": bool(commit.superseded_delivery_id),
                }
            },
        )
    return commit


def _withdraw_body(
    transaction: Any, refs: _Refs, *, request: EmitRequest, now: datetime, seed: int
) -> EventCommit:
    subject = _doc(_read(transaction, refs.subject(request.alert_id)))
    subscription = _doc(_read(transaction, refs.subscription(request.subscription_id)))
    if subject and not _not_expired(subject, now):
        subject = {}
    if not subject or subject.get("phase") in (None, PHASE_WITHDRAWN):
        # Nothing stands, so nothing to withdraw. No write: a below-threshold observation about a
        # train nobody was warned about is not a fact worth a document.
        return EventCommit(status="unchanged", alert_id=request.alert_id, reason="nothing_standing")
    reason = _subscription_obsolete(subscription, request=request, now=now)
    if reason is not None:
        return EventCommit(status="obsolete", alert_id=request.alert_id, reason=reason)
    # THE COUNT IS A RUN, NOT A TOTAL, and getting that wrong made a single dip withdraw.
    #
    # The bug: an alert-worthy cycle that mints nothing (the same phase again -- the overwhelming
    # majority of cycles for a standing alert) reaches no store call at all, because
    # `notification_events.skip_reason` deliberately skips it to save a transaction per standing subject
    # per cycle. So nothing reset the counter, and a dip, an hour of recovery and a second dip
    # withdrew the alert -- which is a single-dip withdrawal in effect, exactly what
    # `WITHDRAWAL_CYCLES` exists to prevent.
    #
    # The fix cannot be "reset it on an alert-worthy cycle": the store never sees that cycle. The
    # two ways to make it see one are (a) have the pass call the store on every unchanged cycle,
    # which is one transaction per standing subject per cycle and undoes precisely the optimisation
    # `skip_reason` was written for -- AE3's line suspension is thirteen standing subjects on one
    # subscription, so ~4 000 transactions a day for it alone -- or (b) a batched
    # "these are still standing" write, one RPC per client per cycle, which is cheap but is a
    # CALLER change and so not this module's to make unilaterally.
    #
    # So the run is closed by TIME instead: two below-threshold observations belong to the same run
    # when they are less than `WITHDRAWAL_GAP_S` apart. Be exact about what that is and is not. It
    # does NOT observe consecutiveness -- an alert-worthy cycle inside the window is invisible, so a
    # dip, one recovered cycle, and a dip still withdraws. It DOES deliver both ends the rule is for:
    # a genuine run of dips one pass period apart withdraws on the second, and a recovery longer
    # than the window starts a fresh run. Every inaccuracy is in the direction of NOT withdrawing,
    # which is the safe one -- a withdrawal is itself a notification.
    #
    # A subject with no `below_threshold_at` -- a fresh one, or a row written before this field
    # existed -- starts a new run, so a subject mid-run across the deploy restarts rather than
    # withdrawing early.
    previous_at = _as_dt(subject.get("below_threshold_at"))
    same_run = (
        previous_at is not None and (now - previous_at).total_seconds() <= WITHDRAWAL_GAP_S
    )
    count = int(subject.get("below_threshold_cycles") or 0) + 1 if same_run else 1
    if count < WITHDRAWAL_CYCLES:
        transaction.update(
            refs.subject(request.alert_id),
            {
                "below_threshold_cycles": count,
                "below_threshold_at": now,
                "updated_at": now,
            },
        )
        return EventCommit(
            status="counting",
            alert_id=request.alert_id,
            below_threshold_cycles=count,
            revision=int(subject.get("revision") or 0),
        )
    return _mint(
        transaction,
        refs,
        request=request,
        subject=subject,
        event_type=EVENT_WITHDRAWN,
        phase=PHASE_WITHDRAWN,
        now=now,
        seed=seed,
    )


def record_below_threshold(request: EmitRequest, *, now: datetime | None = None) -> EventCommit:
    """One cycle in which this train no longer clears the gate; withdraw after enough of them.

    Withdrawal means the forecast changed, not that normal service is guaranteed -- which is what
    the wording in ``request.payload`` has to say.

    **The count is a RUN, closed by time, and this docstring used to claim "consecutive" while the
    code counted a lifetime total.** A run is two or more below-threshold observations less than
    :data:`WITHDRAWAL_GAP_S` apart; a mint of any phase closes it, and so does a gap longer than
    that. It is an approximation of "consecutive" and not the thing itself, because an alert-worthy
    cycle that mints nothing never reaches this module at all -- ``notification_events.skip_reason``
    skips the call to save a transaction per standing subject per cycle. ``_withdraw_body`` carries
    the full reasoning, the exact-but-costlier alternatives, and what the approximation gets wrong
    (a dip, ONE recovered cycle, and a dip inside the window still withdraws). Every inaccuracy is
    in the direction of not withdrawing.

    Takes the withdrawal payload on EVERY call even though it is used only on the last one. The
    alternative -- returning "you would withdraw now, ask again with a payload" -- is two round
    trips on the ingestion thread for a payload that is pure and cheap to build.
    """
    instant = now or _now()
    seed = revision_seed(instant)
    refs = _Refs(_firestore_client())
    return _run_transaction(
        lambda txn: _withdraw_body(txn, refs, request=request, now=instant, seed=seed)
    )


def _clear_below_threshold_body(
    transaction: Any, refs: _Refs, *, alert_id: str, now: datetime
) -> bool:
    subject = _doc(_read(transaction, refs.subject(alert_id)))
    # NEVER creates a subject: this says "the run that was open is closed", which is meaningless
    # for a subject that does not exist, and a `set` here would mint a phase-less document the
    # snapshot readers would then have to skip.
    if not subject or not _not_expired(subject, now):
        return False
    if _as_dt(subject.get("below_threshold_at")) is None:
        # No run open, so no write. This is what makes U6's "zero writes on an ordinary standing
        # cycle" affordable: the overwhelmingly common case costs one read and nothing else, and
        # with `ClientAlerts.open_below_threshold` in hand the caller does not even pay that.
        return False
    transaction.update(
        refs.subject(alert_id),
        {"below_threshold_cycles": 0, "below_threshold_at": None, "updated_at": now},
    )
    return True


def clear_below_threshold(alert_id: str, *, now: datetime) -> bool:
    """Close an open withdrawal run: this train cleared the gate again. Returns whether it wrote.

    The other half of making the withdrawal count truly CONSECUTIVE. `_withdraw_body` cannot see an
    alert-worthy cycle -- `notification_events.skip_reason` deliberately skips the store call on one, to
    save a transaction per standing subject per cycle -- so it closes runs by time instead, and that
    approximation withdraws on a dip / one recovered cycle / dip inside
    :data:`WITHDRAWAL_GAP_S`. Calling this on a recovery removes the approximation for the case that
    matters, at one write per GENUINE recovery rather than one per standing cycle.

    Takes an **alert_id** and not an `EmitRequest`, because that is all the caller has at the point
    it decides nothing needs minting -- it has already skipped building a request.

    Touches no phase and no revision: a recovery is not a lifecycle transition and must not mint an
    event or move the subject's revision. Returns ``False`` for a missing, expired, or
    already-closed subject, so a caller can count real recoveries without a second read.
    """
    if not alert_id:
        return False
    refs = _Refs(_firestore_client())
    return bool(
        _run_transaction(
            lambda txn: _clear_below_threshold_body(txn, refs, alert_id=alert_id, now=now)
        )
    )


def get_subject(alert_id: str, *, now: datetime | None = None) -> AlertSubject | None:
    instant = now or _now()
    stored = _doc(_Refs(_firestore_client()).subject(alert_id).get(timeout=_CALL_DEADLINE_S))
    if not stored or not _not_expired(stored, instant):
        return None
    return _subject_from_doc(stored)


# ---- Current alert state, as the notification API serves it -----------------------------------------


@dataclass(frozen=True)
class AlertState:
    """One standing alert on one subscription: which train, which phase, which revision.

    The display half of an :class:`AlertSubject`, with the train reassembled into a
    :class:`TrainRef` so the API and an event describe a train the same way -- the six fields below
    are exactly what ``event.schema.json`` carries, so a client joining a card to an event is
    comparing like with like.

    ``delivered_revision`` is what the cap exemption reads at the next mint, and it is served
    because it is the one field that answers "did this reach the traveller": a subject with a
    revision and no ``delivered_revision`` is an alert we hold and they do not.
    """

    alert_id: str
    subscription_id: str
    phase: str
    revision: int
    delivered_revision: int | None
    train: TrainRef
    updated_at: datetime | None


@dataclass(frozen=True)
class AlertSnapshot:
    """One subscription's current alert state, bounded.

    ``truncated`` is the honest half and must be rendered rather than ignored: past
    :data:`MAX_SUBJECTS_PER_READ` the counts are LOWER BOUNDS, so ``n_active == 0`` beside
    ``truncated`` means "we could not establish this", never "all clear". Same rule R6 applies to
    servability, applied to a second question -- and the reason the list route marks every row in a
    page truncated when its one query was, rather than letting a quiet row read as calm.
    """

    subscription_id: str
    as_of: datetime
    n_active: int
    n_confirmed: int
    truncated: bool
    subjects: tuple[AlertState, ...]


#: Worst first: a declared cancellation outranks a forecast. `withdrawn` is listed for completeness
#: and never reaches a snapshot -- see `_alert_state_from_doc`'s caller.
_PHASE_RANK = {PHASE_CONFIRMED: 0, PHASE_PREDICTED: 1, PHASE_WITHDRAWN: 2}


def _alert_state_from_doc(doc: Mapping[str, Any]) -> AlertState:
    return AlertState(
        alert_id=str(doc["alert_id"]),
        subscription_id=str(doc["subscription_id"]),
        phase=str(doc["phase"]),
        revision=int(doc.get("revision") or 0),
        delivered_revision=(
            int(doc["delivered_revision"]) if doc.get("delivered_revision") is not None else None
        ),
        train=TrainRef(
            operator=str(doc.get("operator") or ""),
            line=str(doc.get("line") or ""),
            journey_ref=str(doc.get("journey_ref") or ""),
            call_date=str(doc.get("call_date") or ""),
            stop_place=str(doc.get("stop_place") or ""),
            stop_order=int(doc.get("stop_order") or 0),
        ),
        updated_at=_as_dt(doc.get("updated_at")),
    )


def _snapshot(
    subscription_id: str,
    rows: list[Mapping[str, Any]],
    *,
    now: datetime,
    truncated: bool,
) -> AlertSnapshot:
    """Rank and bound one subscription's subject documents.

    Three things happen here and each is a decision.

    **A withdrawn subject is not served at all.** The question is what alerts STAND, and a
    withdrawal is the retraction of one -- the client's receiver already got the withdrawal event,
    and re-offering it here would invite a card for a train we have said is fine.

    **Ranking is phase first, then recency.** So a declared cancellation is never displaced by a
    forecast that happened to change more recently, which is the one ordering mistake that would
    make the cap hide the thing that matters. Ties break on ``alert_id`` so the list cannot flip
    between two requests about unchanged data.

    **The COUNTS are over everything read, the LIST over the cap.** That split is what makes the
    cap safe: nothing is hidden, only unenumerated.
    """
    states: list[AlertState] = []
    for doc in rows:
        try:
            state = _alert_state_from_doc(doc)
        except Exception:
            # One unreadable document is skipped rather than failing its collection (R20) -- the
            # same per-document guard `active_subscriptions` applies.
            logger.debug("Unreadable client alert subject; skipping it", exc_info=True)
            continue
        if state.phase not in PHASES:
            continue
        states.append(state)
    states.sort(
        key=lambda s: (
            _PHASE_RANK.get(s.phase, len(_PHASE_RANK)),
            -(s.updated_at or _REVISION_EPOCH).timestamp(),
            s.alert_id,
        )
    )
    return AlertSnapshot(
        subscription_id=subscription_id,
        as_of=now,
        n_active=len(states),
        n_confirmed=sum(1 for s in states if s.phase == PHASE_CONFIRMED),
        truncated=truncated,
        subjects=tuple(states[:MAX_ALERTS_SERVED]),
    )


def _live_subject_rows(page: list[Any], now: datetime) -> tuple[list[dict[str, Any]], int]:
    """The unexpired documents in a page, and how many were dropped.

    Every read path tests the expiry field ITSELF, because TTL deletion runs up to ~24 h late and
    an expired document is returned by queries and lookups until it does. Here that is not merely
    hygiene: a subject document outlives the dated train it describes by up to a week
    (:data:`HISTORY_DAYS`), so trusting TTL would show a client last Tuesday's cancellation as a
    standing alert.
    """
    rows: list[dict[str, Any]] = []
    expired = 0
    for snapshot in page:
        stored = _doc(snapshot)
        if not stored:
            continue
        if not _not_expired(stored, now):
            expired += 1
            continue
        rows.append(stored)
    return rows, expired


def subscription_alerts(
    subscription_id: str,
    *,
    client_id: str | None = None,
    limit: int = MAX_SUBJECTS_PER_READ,
    now: datetime | None = None,
) -> AlertSnapshot:
    """One subscription's current alert state, in ONE query.

    A read of what the emit path has already committed, never a re-evaluation: ``commit_event``
    writes one document per (subscription, train) carrying its revision and lifecycle phase, and
    that *is* the current state. Nothing here consults the matcher, ``/impact`` or a prediction
    cache -- which is what makes it affordable on a request path at all.

    Tenancy is checked here rather than left to the caller, like every other subscription-scoped
    method in this module: a subject carries its own ``client_id``, so a mismatch is
    :class:`Forbidden` and one client cannot read another's alert state through this.
    """
    instant = now or _now()
    query = _query(
        "subscription_alerts",
        equality={"subscription_id": subscription_id},
        limit=min(limit, MAX_SUBJECTS_PER_READ),
    )
    page = list(query.stream(timeout=_CALL_DEADLINE_S))
    rows, _expired = _live_subject_rows(page, instant)
    if client_id is not None:
        for row in rows:
            if str(row.get("client_id") or "") != client_id:
                raise Forbidden("alert subject belongs to another client")
    return _snapshot(
        subscription_id, rows, now=instant, truncated=len(page) >= min(limit, MAX_SUBJECTS_PER_READ)
    )


@dataclass(frozen=True)
class ClientAlerts:
    """Every snapshot one client-scoped read produced, plus whether the read itself was bounded.

    ``truncated`` is on the WRAPPER as well as on each snapshot, and that is not redundancy: a
    subscription whose subjects fell outside the page is ABSENT from ``snapshots``, so without a
    flag the caller could not tell "this one has no standing alert" from "this one was crowded
    out". The API renders an absent row as unknown rather than calm whenever this is set.
    """

    snapshots: dict[str, AlertSnapshot]
    truncated: bool
    #: ``alert_id`` -> ``below_threshold_at`` for every live subject in the page that has one: the
    #: subjects with a withdrawal run OPEN against them. Exists so a caller can make the withdrawal
    #: count truly consecutive -- the lifecycle diagram says "two FRESH below-threshold cycles", and
    #: the time window `_withdraw_body` falls back to has exactly one failure mode (a dip, ONE
    #: recovered cycle, and a dip inside `WITHDRAWAL_GAP_S`), which withdraws when it should not and
    #: therefore sends a real «varselet er trukket tilbake» and then re-alerts.
    #:
    #: **UNCAPPED, and derived before `MAX_ALERTS_SERVED` truncates the display list.** A field on
    #: `AlertState` would not do: the display cap of 4 would leave AE3's thirteen-subject line
    #: suspension -- the case `_withdraw_body`'s own comment cites -- falling through to the time
    #: window for nine of them. It is bounded by the page (`MAX_SUBJECTS_PER_READ`) and not by the
    #: display, and it adds no RPC: the query is one `client_alerts` already spends.
    #:
    #: A mapping rather than a set, so the caller can skip a clear whose run is already older than
    #: :data:`WITHDRAWAL_GAP_S` -- the store would start a fresh run anyway, and the write would buy
    #: nothing.
    open_below_threshold: Mapping[str, datetime] = field(default_factory=dict)


def client_alerts(
    client_id: str, *, limit: int = MAX_SUBJECTS_PER_READ, now: datetime | None = None
) -> ClientAlerts:
    """Every one of a client's subscriptions' alert state, in ONE query, keyed by subscription id.

    **One query per request, not one per row.** The list page can hold hundreds of subscriptions,
    and a per-row read would be a query and up to :data:`MAX_SUBJECTS_PER_READ` document reads
    apiece -- which is the reason this is client-scoped and grouped in Python rather than called in
    a loop.

    What that buys costs one thing, and it is reported rather than hidden: the ceiling is on the
    whole request, so a client with more live subjects than the ceiling gets a page in which
    **every** snapshot is marked ``truncated``, including the rows whose own subjects happened to
    fit. Marking only the rows that were cut would be worse and is the mistake to avoid: a
    subscription whose subjects fell outside the page would then read ``n_active: 0``, which is
    indistinguishable from calm. A caller seeing ``truncated`` asks about the subscription it cares
    about through :func:`subscription_alerts`, which is scoped to one and cannot be crowded out.

    A subscription with no live subjects is simply absent from the mapping; the API renders that as
    an empty snapshot, which is the one case where "nothing" really does mean "no standing alert".
    """
    instant = now or _now()
    ceiling = min(limit, MAX_SUBJECTS_PER_READ)
    query = _query("client_alerts", equality={"client_id": client_id}, limit=ceiling)
    page = list(query.stream(timeout=_CALL_DEADLINE_S))
    rows, expired = _live_subject_rows(page, instant)
    truncated = len(page) >= ceiling
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("subscription_id") or ""), []).append(row)
    # `expired` IS NOT A WARNING -- see `recipient_documents` for the argument, which applies with
    # more force here: the notification pass calls this once per client per cycle, so during TTL's
    # documented ~24 h delete lag every cycle logged `notification_quota` for an entirely expected
    # condition. `truncated` keeps the warning, because that one changes the CALLER's behaviour
    # (every snapshot is marked incomplete and the pass falls back to asking per subject), which is
    # exactly the kind of thing an operator should see.
    fields = {
        "event": "notification_quota",
        "result": "alert_state",
        "client_id": client_id,
        "n": len(rows),
        "expired": expired,
        "truncated": truncated,
    }
    if truncated:
        logger.warning(
            "Client alert-state read hit its ceiling: %d live, %d expired",
            len(rows),
            expired,
            extra={"fields": fields},
        )
    elif expired:
        logger.debug(
            "Client alert-state read skipped %d expired subject(s) not yet TTL-deleted",
            expired,
            extra={"fields": fields},
        )
    # Over `rows`, which is the whole live page -- deliberately not over the grouped snapshots,
    # whose `subjects` tuples are already cut to `MAX_ALERTS_SERVED`.
    open_below_threshold: dict[str, datetime] = {}
    for row in rows:
        at = _as_dt(row.get("below_threshold_at"))
        alert_id = str(row.get("alert_id") or "")
        if at is not None and alert_id:
            open_below_threshold[alert_id] = at
    return ClientAlerts(
        snapshots={
            subscription_id: _snapshot(
                subscription_id, subject_rows, now=instant, truncated=truncated
            )
            for subscription_id, subject_rows in grouped.items()
            if subscription_id
        },
        truncated=truncated,
        open_below_threshold=open_below_threshold,
    )


def empty_alert_snapshot(subscription_id: str, *, now: datetime | None = None) -> AlertSnapshot:
    """The snapshot for a subscription with no live subjects. NOT the same as a failed read.

    Its own function so the API cannot express "no standing alerts" and "we could not establish
    this" with the same value: this one is ``truncated=False``, which is a claim, while a bounded or
    failed read carries ``truncated=True`` and no claim at all.
    """
    return AlertSnapshot(
        subscription_id=subscription_id,
        as_of=now or _now(),
        n_active=0,
        n_confirmed=0,
        truncated=False,
        subjects=(),
    )


def _count_destination_deliveries(destination_id: str, *, state: str) -> int:
    """How many deliveries this destination holds in ``state``, bounded by the ceiling + 1.

    Bounded because the question is only ever "are we at the ceiling", so one document past it
    answers it and the whole collection would not answer it better.

    **Aggregated, not streamed**, and the difference is a real bill rather than tidiness. This runs
    once per emit, and pending deliveries accumulate across every subscription on the destination
    for a full 60-minute event validity -- so on a bad morning a hundred subjects each streaming a
    hundred pending documents is ten thousand reads per cycle, several million a day. A ``count()``
    aggregation bills roughly one read per thousand index entries scanned.

    The stream is kept as a FALLBACK rather than deleted: it is the same answer at a worse price, so
    a Firestore or emulator build without aggregation support degrades in cost instead of refusing
    to emit. Verified against the emulator (2026-09-08) that the aggregation path is the one taken.
    """
    query = _query(
        "destination_deliveries_in_state",
        equality={"destination_id": destination_id, "state": state},
        limit=MAX_PENDING_EVENTS_PER_DESTINATION + 1,
    )
    try:
        result = query.count(alias="n").get(timeout=_CALL_DEADLINE_S)
        return int(result[0][0].value)
    except Exception:
        logger.debug("count() aggregation unavailable; falling back to a stream", exc_info=True)
        return sum(1 for _ in query.stream(timeout=_CALL_DEADLINE_S))


# ---- Deliveries --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    event_id: str
    alert_id: str
    subscription_id: str
    subscription_version: int
    client_id: str
    destination_id: str
    state: str
    attempts: int
    revision: int
    valid_until: datetime | None
    retry_not_before: datetime | None
    expires_at: datetime | None
    situations: tuple[str, ...]
    cap_exempt: bool
    test: bool


def _delivery_from_doc(doc: Mapping[str, Any]) -> Delivery:
    return Delivery(
        delivery_id=str(doc["delivery_id"]),
        event_id=str(doc["event_id"]),
        alert_id=str(doc.get("alert_id") or ""),
        subscription_id=str(doc["subscription_id"]),
        subscription_version=int(doc.get("subscription_version") or 0),
        client_id=str(doc.get("client_id") or ""),
        destination_id=str(doc["destination_id"]),
        state=str(doc.get("state") or ""),
        attempts=int(doc.get("attempts") or 0),
        revision=int(doc.get("revision") or 0),
        valid_until=_as_dt(doc.get("valid_until")),
        retry_not_before=_as_dt(doc.get("retry_not_before")),
        expires_at=_as_dt(doc.get(TTL_FIELD)),
        situations=tuple(str(s) for s in (doc.get("situations") or ())),
        cap_exempt=bool(doc.get("cap_exempt")),
        test=bool(doc.get("test")),
    )


@dataclass(frozen=True)
class PendingDeliveries:
    """This cycle's deliverable set, plus what it could not offer and why.

    ``stale`` is the set whose validity has passed. R9 is explicit that delivery is re-attempted
    while the departure is still ahead and then marked **expired rather than failed** -- so these
    are handed back for :func:`mark_delivery_expired` rather than silently dropped, which would
    leave a pending document nothing ever resolves.

    ``throttled`` names destinations whose per-cycle attempt ceiling bound the set, and
    ``truncated`` says the query's own ceiling did. Both are visible to an operator (R21) instead of
    looking like a quiet day.
    """

    ready: tuple[Delivery, ...]
    stale: tuple[str, ...]
    throttled: tuple[str, ...]
    truncated: bool


def pending_deliveries(
    *, limit: int = MAX_DELIVERY_ATTEMPTS_PER_CYCLE, now: datetime | None = None
) -> PendingDeliveries:
    """What to attempt this cycle, soonest-departure-first.

    Two orderings, and both are deliberate. The QUERY orders by ``retry_not_before`` because
    Firestore's classic rule requires the first ordering to be on the range-filtered field -- and
    the range filter is what makes a persisted ``Retry-After`` backoff survive a cycle boundary and
    a restart. The RESULT is then re-sorted by validity in Python, because /varsel spends its budget
    soonest-departure-first across registrations and this must too: insertion order handed the whole
    budget to the earliest-registered travellers every busy morning. The consequence to know is that
    at the ceiling the PAGE is chosen by retry order and only its contents are ordered by departure.
    """
    instant = now or _now()
    ceiling = min(limit, MAX_DELIVERY_ATTEMPTS_PER_CYCLE)
    query = _query(
        "pending_deliveries",
        equality={"state": "pending"},
        inequality={"retry_not_before": instant},
        limit=ceiling,
    )
    page = list(query.stream(timeout=_CALL_DEADLINE_S))
    ready: list[Delivery] = []
    stale: list[str] = []
    for snapshot in page:
        stored = _doc(snapshot)
        try:
            delivery = _delivery_from_doc(stored)
        except Exception:
            logger.debug("Unreadable webhook delivery document; skipping it", exc_info=True)
            continue
        # Every read path tests the expiry field itself: a delivery TTL has not caught up with is
        # not claimable, and one past its validity is expired rather than deliverable.
        if not _not_expired(stored, instant):
            continue
        if delivery.valid_until is None or delivery.valid_until <= instant:
            stale.append(delivery.delivery_id)
            continue
        if delivery.retry_not_before is not None and delivery.retry_not_before > instant:
            continue
        ready.append(delivery)

    ready.sort(key=lambda d: (d.valid_until, d.delivery_id))
    per_destination: dict[str, int] = {}
    kept: list[Delivery] = []
    throttled: set[str] = set()
    for delivery in ready:
        spent = per_destination.get(delivery.destination_id, 0)
        if spent >= MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE:
            throttled.add(delivery.destination_id)
            continue
        per_destination[delivery.destination_id] = spent + 1
        kept.append(delivery)
    if throttled or len(page) >= ceiling:
        logger.warning(
            "Webhook delivery set bounded: %d ready, %d throttled destination(s), truncated=%s",
            len(kept),
            len(throttled),
            len(page) >= ceiling,
            extra={
                "fields": {
                    "event": "notification_quota",
                    "result": "delivery_attempts",
                    "n": len(kept),
                    "throttled": sorted(throttled),
                    "truncated": len(page) >= ceiling,
                }
            },
        )
    return PendingDeliveries(
        ready=tuple(kept),
        stale=tuple(stale),
        throttled=tuple(sorted(throttled)),
        truncated=len(page) >= ceiling,
    )


@dataclass(frozen=True)
class DeliveryClaim:
    """The pre-dispatch eligibility recheck.

    ``status``: ``ready`` (dispatch it), ``cancelled`` (the subscription is gone or obsolete -- R13,
    written durably so nothing re-offers it), ``expired`` (validity passed), ``capped`` (R11's
    allowance is spent; recorded and left pending so it is re-offered next cycle) or ``gone`` (no
    such delivery, or it is no longer pending).
    """

    status: str
    delivery: Delivery | None = None
    subscription: Subscription | None = None
    #: The allowance left AFTER this claim's own reservation, not before it. A `ready` claim has
    #: already taken a slot -- that is the whole point of reserving inside the claim transaction --
    #: so reporting the pre-claim figure would tell a caller a slot is available that it has itself
    #: just consumed. A cap-exempt claim reserves nothing and this is unchanged by it.
    cap_remaining: int = 0
    reason: str | None = None


def _reserve_slot(
    subscription: Mapping[str, Any], *, delivery_id: str, now: datetime
) -> dict[str, Any]:
    """The subscription patch that reserves one of R11's slots for ``delivery_id``.

    Keyed by delivery id so it is idempotent, and pruned to the live set on every write so a lapsed
    reservation cannot accumulate. Never touches ``daily_deliveries``: that map stays the count of
    CONFIRMED sends, which is what an operator reads and what the delivery suite asserts.
    """
    reservations = _live_reservations(subscription, now)
    reservations[delivery_id] = {"day": oslo_day(now), "at": now}
    return {"daily_reservations": reservations, "updated_at": now}


def _release_slot(
    subscription: Mapping[str, Any], *, delivery_id: str, now: datetime
) -> dict[str, Any] | None:
    """The patch that gives a reserved slot back, or ``None`` when there is nothing to give back.

    ``None`` rather than an empty patch, so a terminal write for a delivery that never reserved
    anything -- a cap-exempt one, or one whose reservation already lapsed -- costs no write to the
    subscription document at all.
    """
    reservations = _live_reservations(subscription, now)
    if delivery_id not in reservations:
        return None
    reservations.pop(delivery_id, None)
    return {"daily_reservations": reservations, "updated_at": now}


def _claim_body(
    transaction: Any, refs: _Refs, *, delivery_id: str, now: datetime
) -> tuple[str, dict[str, Any], dict[str, Any], int, str | None]:
    delivery = _doc(_read(transaction, refs.delivery(delivery_id)))
    if not delivery or delivery.get("state") != "pending" or not _not_expired(delivery, now):
        return "gone", delivery, {}, 0, None
    subscription = _doc(_read(transaction, refs.subscription(str(delivery["subscription_id"]))))
    valid_until = _as_dt(delivery.get("valid_until"))
    if valid_until is None or valid_until <= now:
        transaction.update(
            refs.delivery(delivery_id),
            {"state": "expired", "expired_at": now, "updated_at": now},
        )
        return "expired", delivery, subscription, 0, "validity"
    # R13 is about DELETION OR EXPIRY stopping later claims, and deliberately not about amendment.
    # A version compare here would silently lose the pending alert on every amendment: emit at v1
    # leaves D1 pending, the client amends to v2, the next cycle's emission at v2 finds no
    # lifecycle transition and mints nothing, and this claim would then cancel D1 -- so the
    # traveller is never told about a train that is still cancelled, and nothing logs as wrong. The
    # version check belongs at EMIT (`_subscription_obsolete`), where an emission that read a stale
    # subscription must not commit against a newer one.
    #
    # What an amendment CAN invalidate is the destination: a pending delivery signed for and
    # addressed to a callback the client has since moved must not go to the old one.
    obsolete = (
        "missing"
        if not subscription
        else "deleted"
        if subscription.get("status") != "active"
        else "expired"
        if not subscription_is_live(subscription, now)
        else "destination"
        if subscription.get("destination_id") != delivery.get("destination_id")
        else None
    )
    if obsolete is not None:
        transaction.update(
            refs.delivery(delivery_id),
            {
                "state": "cancelled",
                "cancelled_at": now,
                "cancelled_reason": obsolete,
                "updated_at": now,
            },
        )
        return "cancelled", delivery, subscription, 0, obsolete
    remaining = cap_remaining(subscription, now)
    exempt = bool(delivery.get("cap_exempt"))
    if remaining <= 0 and not exempt:
        # R11: a suppressed alert is RECORDED and re-offered. The state stays `pending` on purpose
        # -- the cap is a per-day allowance, not a verdict on this event -- so the next cycle inside
        # the event's validity offers it again, and the record is what makes the suppression visible
        # rather than a delivery that quietly never happened.
        transaction.update(
            refs.delivery(delivery_id),
            {
                "capped_at": now,
                "capped_count": int(delivery.get("capped_count") or 0) + 1,
                "updated_at": now,
            },
        )
        return "capped", delivery, subscription, remaining, "daily_cap"
    transaction.update(refs.delivery(delivery_id), {"claimed_at": now, "updated_at": now})
    if not exempt:
        # RESERVE, in the same transaction that returns `ready`. That is the fix and the placement
        # is the whole of it: two claims for one subscription both write this document, so Standard
        # edition's pessimistic concurrency serialises them and the second reads the first's
        # reservation instead of the same free slot. A reservation written anywhere later -- or read
        # here and written by the caller -- is the read-then-act bug again with more steps.
        transaction.update(
            refs.subscription(str(delivery["subscription_id"])),
            _reserve_slot(subscription, delivery_id=delivery_id, now=now),
        )
    return "ready", delivery, subscription, remaining - (0 if exempt else 1), None


def claim_delivery(delivery_id: str, *, now: datetime | None = None) -> DeliveryClaim:
    """Recheck eligibility immediately before dispatch, durably (R13).

    Called right before the outbound attempt, never at the top of the pass: the pending set was read
    at the start of the cycle and a delete, an expiry or a version bump can land in between. A
    claim that finds one writes the terminal state itself, so nothing re-offers the delivery.
    """
    instant = now or _now()
    refs = _Refs(_firestore_client())
    status, delivery, subscription, remaining, reason = _run_transaction(
        lambda txn: _claim_body(txn, refs, delivery_id=delivery_id, now=instant)
    )
    return DeliveryClaim(
        status=status,
        delivery=_delivery_from_doc(delivery) if delivery else None,
        subscription=_subscription_from_doc(subscription) if subscription else None,
        cap_remaining=remaining,
        reason=reason,
    )


@dataclass(frozen=True)
class DeliveryOutcome:
    """What a terminal write did.

    ``already_resolved`` says the delivery was NOT ``pending`` when the write arrived, so nothing was
    written and ``state`` is the state it already held. Additive and defaulted, so a caller that
    ignores it behaves exactly as before -- but a caller counting fresh successes should read it: an
    abandoned attempt thread completing a cycle late is an ordinary event on this path, not an error.
    """

    delivery_id: str
    state: str
    attempts: int
    slot_spent: bool = False
    retry_not_before: datetime | None = None
    already_resolved: bool = False


def _sent_body(
    transaction: Any, refs: _Refs, *, delivery_id: str, now: datetime
) -> tuple[dict[str, Any], bool, bool]:
    """Record a successful attempt, and spend R11's slot in the SAME transaction.

    Three states change together and must not be splittable: the delivery becomes ``sent``, the
    subscription's Oslo-day counter and dedup marks advance, and the subject records that a revision
    about this train has been delivered (which is what the cap exemption reads on the next one).
    Split across two writes they disagree after a restart, and the exemption failing open would
    suppress a confirmed cancellation.

    Dedup is on SUCCESS, exactly as /varsel's is: an attempt that errors leaves no mark and is
    re-offered next cycle, and the cap slot is spent here for the same reason -- otherwise a dead
    destination burns a subscription's four for the Oslo day with nothing delivered.

    **THE TRANSITION IS CONDITIONAL ON `pending`, and leaving that out double-spent R11's cap.**
    Observed live in U6's measurement harness as a stray `NotFound` from an abandoned thread, and
    the sequence is ordinary rather than exotic: the delivery pass abandons an attempt's thread when
    its bounded wait expires (it cannot kill a thread parked in a socket read), the next cycle
    legitimately re-sends the same still-`pending` event, and the abandoned thread then returns and
    marks the same delivery sent. Unguarded, `attempts` double-increments -- cosmetic -- and the
    subscription's Oslo-day counter is spent TWICE for one notification, so a traveller silently
    loses one of their four. R11's grain is the whole of this plan's parity claim with /varsel, so
    that is not cosmetic.

    A non-`pending` delivery therefore writes NOTHING and comes back
    ``already_resolved=True, slot_spent=False``, carrying the state it actually holds. It is not an
    error -- the client did receive the event, and raising would log a store failure and read as an
    outage -- but it must not read as a fresh success either, which is why the flag exists rather
    than returning a plain `sent`. Note the guard is what `_terminal_body` has had all along; its
    absence here and in `_failed_body` was the oversight, not the presence of it there.
    """
    delivery = _doc(_read(transaction, refs.delivery(delivery_id)))
    if not delivery:
        raise NotFound("no such delivery")
    if delivery.get("state") != "pending":
        return delivery, False, True
    subscription_id = str(delivery["subscription_id"])
    subscription = _doc(_read(transaction, refs.subscription(subscription_id)))
    alert_id = str(delivery.get("alert_id") or "")
    subject = _doc(_read(transaction, refs.subject(alert_id))) if alert_id else {}

    attempts = int(delivery.get("attempts") or 0) + 1
    delivery_patch = {
        "state": "sent",
        "sent_at": now,
        "attempts": attempts,
        "last_error": None,
        "updated_at": now,
    }
    transaction.update(refs.delivery(delivery_id), delivery_patch)

    spent = False
    if subscription:
        counters = _prune_daily(subscription.get("daily_deliveries") or {}, now)
        marks = _prune_marks(subscription.get("delivered") or {}, now)
        # CONFIRM the reservation: drop this delivery's entry and add one to the day's confirmed
        # count. Dropping it is what makes the count and the reservations disjoint, so
        # `cap_remaining`'s sum over both cannot double-count the slot it is about to charge.
        reservations = _live_reservations(subscription, now)
        reservations.pop(delivery_id, None)
        if not delivery.get("cap_exempt"):
            # The increment does NOT require a reservation to have survived. A slot is spent on
            # SUCCESS -- the plan says so, and for the reason it gives: otherwise a dead destination
            # burns a subscription's four with nothing delivered. So a delivery whose reservation
            # lapsed (a slow attempt, a restart) still charges its slot here, and the reservation is
            # only ever an admission control on top. That combination is what makes the scheme
            # self-correcting rather than a counter that can drift in either direction, and the
            # `pending` guard above is what stops it charging twice.
            day = oslo_day(now)
            counters[day] = int(counters.get(day, 0)) + 1
            spent = True
        for situation in delivery.get("situations") or ():
            marks[str(situation)] = now + timedelta(hours=DELIVERED_MARK_TTL_H)
        transaction.update(
            refs.subscription(subscription_id),
            {
                "daily_deliveries": counters,
                "daily_reservations": reservations,
                "delivered": _prune_marks(marks, now),
                "last_delivery_at": now,
                "updated_at": now,
            },
        )
    if subject:
        # What the NEXT revision's cap exemption reads: a revision about this train has reached the
        # traveller, so its replacement is a replacement rather than a first warning.
        #
        # BOTH FIELDS ARE ABOUT *THIS* DELIVERY, NOT ABOUT WHERE THE SUBJECT HAS GOT TO, because an
        # attempt takes seconds and a newer revision can be minted inside them. So the revision is
        # read off the delivery and clamped forward (never backwards), and the pending pointer is
        # cleared only while it still names this delivery -- clearing it unconditionally would drop
        # the subject's pointer to a NEWER pending delivery, and the next mint would then fail to
        # supersede it and put two boxes about one train on a lock screen.
        delivered_revision = max(
            int(subject.get("delivered_revision") or 0), int(delivery.get("revision") or 0)
        )
        subject_patch: dict[str, Any] = {
            "delivered_revision": delivered_revision,
            "delivered_at": now,
            "updated_at": now,
        }
        if subject.get("pending_delivery_id") == delivery_id:
            subject_patch["pending_delivery_id"] = None
        transaction.update(refs.subject(alert_id), subject_patch)
    return {**delivery, **delivery_patch}, spent, False


def mark_delivery_sent(delivery_id: str, *, now: datetime | None = None) -> DeliveryOutcome:
    """Record that the client received this event. Idempotent, and it spends R11's slot once.

    ``state`` is read back from the document rather than asserted to be ``"sent"``: a delivery some
    other pass already resolved keeps the state it holds, and claiming ``"sent"`` for a
    ``superseded`` or ``cancelled`` record would misreport it to whoever reads the outcome.
    """
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc, spent, already_resolved = _run_transaction(
        lambda txn: _sent_body(txn, refs, delivery_id=delivery_id, now=instant)
    )
    return DeliveryOutcome(
        delivery_id=delivery_id,
        state=str(doc.get("state") or ""),
        attempts=int(doc.get("attempts") or 0),
        slot_spent=spent,
        already_resolved=already_resolved,
    )


def _failed_body(
    transaction: Any,
    refs: _Refs,
    *,
    delivery_id: str,
    error: str,
    retryable: bool,
    retry_after_s: float | None,
    terminal_state: str,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    # Conditional on `pending` for the same reason as `_sent_body`, and here the unguarded version
    # is the WORSE of the two: an abandoned attempt thread from the previous cycle, returning after
    # this cycle has already marked the delivery `sent`, would set the state back to `pending` with
    # a `retry_not_before` -- resurrecting a delivery the client has already received, so the next
    # cycle sends it a second time. The receiver's inbox suppresses the duplicate (R12 is
    # at-least-once), which is exactly why this would never have been noticed.
    delivery = _doc(_read(transaction, refs.delivery(delivery_id)))
    if not delivery:
        raise NotFound("no such delivery")
    if delivery.get("state") != "pending":
        return delivery, True
    # RELEASE, on the retryable branch as much as the terminal one. A retryable failure leaves the
    # delivery `pending` for the next cycle, and holding its slot until the reservation lapses would
    # withhold the allowance from a delivery that could use it NOW -- the cap is spent on success,
    # and a failed attempt is not one.
    subscription = _doc(_read(transaction, refs.subscription(str(delivery["subscription_id"]))))
    release = _release_slot(subscription, delivery_id=delivery_id, now=now)
    attempts = int(delivery.get("attempts") or 0) + 1
    patch: dict[str, Any] = {
        "attempts": attempts,
        # Truncated: an error string is an upstream-supplied value and this document is read by an
        # operator, not parsed. R16 keeps client prose out of routine logs; a bounded excerpt on
        # the record itself is what an operator needs to tell a TLS failure from a 500.
        "last_error": str(error)[:500],
        "last_attempt_at": now,
        "updated_at": now,
    }
    if retryable:
        valid_until = _as_dt(delivery.get("valid_until"))
        # `Retry-After` is honoured but capped at the lesser of the header, the time to departure and
        # RETRY_AFTER_CAP_S. Persisted, so the backoff survives a cycle boundary and a restart rather
        # than lasting only the current pass -- the delivery query filters on this field.
        delay = min(float(retry_after_s or 0.0), float(RETRY_AFTER_CAP_S))
        not_before = now + timedelta(seconds=max(0.0, delay))
        if valid_until is not None and not_before > valid_until:
            not_before = valid_until
        patch["state"] = "pending"
        patch["retry_not_before"] = not_before
    else:
        patch["state"] = terminal_state
        patch["failed_at"] = now
    transaction.update(refs.delivery(delivery_id), patch)
    if release is not None:
        transaction.update(refs.subscription(str(delivery["subscription_id"])), release)
    return {**delivery, **patch}, False


def mark_delivery_failed(
    delivery_id: str,
    *,
    error: str,
    retryable: bool,
    retry_after_s: float | None = None,
    terminal_state: str = "failed",
    now: datetime | None = None,
) -> DeliveryOutcome:
    """Record a failed attempt. Retryable keeps it ``pending``; terminal parks it.

    ``terminal_state`` exists for the 410 case: the event is ``cancelled`` rather than ``failed``,
    because the destination is gone rather than the delivery being wrong. Pausing the DESTINATION
    until an operator resumes it is a field on the destination record, which U4 owns -- this
    function records what happened to the delivery and nothing wider.
    """
    if terminal_state not in DELIVERY_STATES:
        raise Conflict(f"terminal_state must be one of {DELIVERY_STATES}")
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc, already_resolved = _run_transaction(
        lambda txn: _failed_body(
            txn,
            refs,
            delivery_id=delivery_id,
            error=error,
            retryable=retryable,
            retry_after_s=retry_after_s,
            terminal_state=terminal_state,
            now=instant,
        )
    )
    return DeliveryOutcome(
        delivery_id=delivery_id,
        state=str(doc.get("state") or ""),
        attempts=int(doc.get("attempts") or 0),
        retry_not_before=_as_dt(doc.get("retry_not_before")),
        already_resolved=already_resolved,
    )


def _terminal_body(
    transaction: Any, refs: _Refs, *, delivery_id: str, state: str, reason: str, now: datetime
) -> dict[str, Any]:
    delivery = _doc(_read(transaction, refs.delivery(delivery_id)))
    if not delivery:
        raise NotFound("no such delivery")
    if delivery.get("state") != "pending":
        return delivery
    # RELEASE. Every path that ends an attempt without a delivery gives the reserved slot back, or
    # the reservation is just the overspend traded for an underspend -- and an underspend is the
    # worse trade, because it suppresses a notification the traveller was owed rather than sending
    # one they were not. Read before write, as everywhere in this module.
    subscription = _doc(_read(transaction, refs.subscription(str(delivery["subscription_id"]))))
    release = _release_slot(subscription, delivery_id=delivery_id, now=now)
    patch = {
        "state": state,
        "updated_at": now,
        f"{state}_at": now,
        f"{state}_reason": reason,
    }
    transaction.update(refs.delivery(delivery_id), patch)
    if release is not None:
        transaction.update(refs.subscription(str(delivery["subscription_id"])), release)
    return {**delivery, **patch}


def mark_delivery_expired(
    delivery_id: str, *, reason: str = "validity", now: datetime | None = None
) -> DeliveryOutcome:
    """R9: past its validity a delivery is **expired**, not failed.

    The distinction is the whole point of the state: "we ran out of time to tell you" and "your
    endpoint rejected us" are different operator problems, and collapsing them would hide a dead
    destination inside ordinary expiry.
    """
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc = _run_transaction(
        lambda txn: _terminal_body(
            txn, refs, delivery_id=delivery_id, state="expired", reason=reason, now=instant
        )
    )
    return DeliveryOutcome(
        delivery_id=delivery_id,
        state=str(doc.get("state") or ""),
        attempts=int(doc.get("attempts") or 0),
    )


def mark_delivery_cancelled(
    delivery_id: str, *, reason: str, now: datetime | None = None
) -> DeliveryOutcome:
    """R13: the subscription is gone, so this delivery is cancelled and never re-offered."""
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc = _run_transaction(
        lambda txn: _terminal_body(
            txn, refs, delivery_id=delivery_id, state="cancelled", reason=reason, now=instant
        )
    )
    return DeliveryOutcome(
        delivery_id=delivery_id,
        state=str(doc.get("state") or ""),
        attempts=int(doc.get("attempts") or 0),
    )


def get_delivery(delivery_id: str, *, now: datetime | None = None) -> Delivery | None:
    instant = now or _now()
    stored = _doc(_Refs(_firestore_client()).delivery(delivery_id).get(timeout=_CALL_DEADLINE_S))
    if not stored or not _not_expired(stored, instant):
        return None
    return _delivery_from_doc(stored)


def get_event(event_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """One stored event, or ``None`` when it is missing or past its retention window."""
    instant = now or _now()
    stored = _doc(_Refs(_firestore_client()).event(event_id).get(timeout=_CALL_DEADLINE_S))
    if not stored or not _not_expired(stored, instant):
        return None
    return stored


def event_is_valid(doc: Mapping[str, Any], now: datetime) -> bool:
    """R12's validity test, as a function so U6 and U8 cannot state it two ways."""
    if not _not_expired(doc, now):
        return False
    valid_until = _as_dt(doc.get("valid_until"))
    return valid_until is not None and valid_until > now


def event_validity(
    *, occurred_at: datetime, departure_at: datetime | None, subscription_expires_at: datetime | None
) -> datetime:
    """The earlier of the 60-minute horizon, the departure, and the subscription's lifetime.

    **The third term is optional here, and on the notification pass it is not supplied**, because
    the pass does not have it: a matched recipient carries the five journey fields plus its routing
    ids, not the subscription's own expiry. What that leaves is a published `valid_until` that can
    outlive the subscription by up to the horizon -- so the contract states where each term is
    enforced rather than implying all three land here (``contracts/notifications/v1/
    event.schema.json``, and ``test_the_published_validity_wording_matches_where_each_term_is_
    enforced`` holds the two together).

    Carrying it in would not retire that wording anyway, which is the reason for choosing the
    wording over the field: the value is computed at emit, so it could only ever be a SNAPSHOT of
    the expiry taken when the pass loaded its recipients, already stale by the length of the pass.
    :func:`claim_delivery` re-reads the LIVE subscription immediately before dispatch and cancels a
    delivery whose subscription has ended (R13), which is strictly tighter than any snapshot and is
    what actually guarantees nothing is *sent* past expiry. The parameter stays for the callers that
    do hold a live subscription -- the notification API's own synthetic test-event path is one.
    """
    candidates = [occurred_at + timedelta(minutes=EVENT_VALIDITY_MINUTES)]
    for candidate in (departure_at, subscription_expires_at):
        if candidate is not None:
            candidates.append(candidate.astimezone(UTC) if candidate.tzinfo else candidate.replace(tzinfo=UTC))
    return min(candidates)


__all__ = [
    "MAX_ALERTS_SERVED",
    "MAX_SUBJECTS_PER_READ",
    "QUERY_SPECS",
    "AlertSnapshot",
    "AlertState",
    "AlertSubject",
    "CleanupResult",
    "ClientAlerts",
    "Conflict",
    "Delivery",
    "DeliveryClaim",
    "DeliveryOutcome",
    "EmitRequest",
    "EventCommit",
    "Forbidden",
    "LoadedSubscriptions",
    "NotConfigured",
    "NotFound",
    "NotificationStoreError",
    "PendingDeliveries",
    "QuerySpec",
    "QuotaExceeded",
    "Subscription",
    "SubscriptionSpec",
    "TrainRef",
    "active_subscriptions",
    "alert_subject_id",
    "cap_remaining",
    "claim_delivery",
    "cleanup_subscription",
    "clear_below_threshold",
    "client_alerts",
    "commit_event",
    "configure",
    "create_subscription",
    "database_id",
    "delete_subscription",
    "empty_alert_snapshot",
    "enabled",
    "event_id_for",
    "event_is_valid",
    "event_validity",
    "firestore_client",
    "get_delivery",
    "get_event",
    "get_subject",
    "get_subscription",
    "is_retryable",
    "list_subscriptions",
    "mark_delivery_cancelled",
    "mark_delivery_expired",
    "mark_delivery_failed",
    "mark_delivery_sent",
    "oslo_day",
    "pending_deliveries",
    "recipient_documents",
    "record_below_threshold",
    "replace_subscription",
    "reset_for_tests",
    "set_servability",
    "subscription_alerts",
    "subscription_is_live",
    "touch_subscription",
]
