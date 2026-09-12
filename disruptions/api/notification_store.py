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
import secrets
import threading
from collections.abc import Callable, Mapping
from contextlib import nullcontext
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
# forward, so a mark that outlives its train's day costs one map entry, while a mark that expires
# EARLY re-notifies a train the traveller has already been told about. The key is the `alert_id`
# -- one mark per delivered (subscription, train) -- since 2026-09-10; see `_sent_body`.
DELIVERED_MARK_TTL_H = 48


# ---- R21's ceilings, every one a number so it can be checked ----------------------------------

# Active subscriptions per client. A planning default rather than measured capacity: the superseded
# plan proposed 1 000 per client and 5 000 total and its own risk note said to "start with lower
# measured admission limits", so this is the lower one. Raising it needs a measurement of the pass
# at the new population, not a judgement.
MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT = 500
# Subscription documents ONE listing page may read while filling itself. The listing tests liveness
# in Python (a query cannot express both horizons), so a page of `limit` documents can come back
# with fewer live rows and it reads again -- bounded, until U7, by the client's own ceiling. That
# was the same number as the bound on `max_active_subscriptions`, which this plan raises to
# 200 000: a client whose collection is dominated by rows TTL has not caught up with would then
# turn one `?limit=1` request into a 200 000-document read. This is the old bound, kept as the scan
# it always was, so the listing's worst case is exactly what it was before the quota moved.
MAX_SUBSCRIPTION_LIST_SCAN = 10_000
#: The field the subscription mirror's watermark is read from (U6, KTD5).
#:
#: **Server-stamped, never `now`.** Every body here already takes `now` as an argument for
#: `updated_at`, and reusing it would be the obvious thing to do and would break the mirror
#: quietly: the watermark is compared against values Firestore wrote, so a value this process
#: wrote is being compared against a different clock. A pod whose clock runs a few seconds fast
#: would advance the watermark past documents the store stamped later, and they would never be
#: synced -- no error, just a subscription that stops updating.
#:
#: Two properties the sync depends on, both consequences of it being a FIELD rather than a
#: timestamp on the document itself. A Firestore `order_by` **excludes documents that lack the
#: field**, exactly as an equality does (U5's lesson one layer along), so the startup read cannot
#: order by this one -- every document written before it existed would be invisible to the read
#: that is supposed to seed the mirror. And a document nothing writes is never re-read, which is
#: the point: a `sent` mark must not cost a sync.
MIRROR_FIELD = "mirror_updated_at"
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
# Pending documents `pending_deliveries` may READ in one cycle while filling its attempt set. Larger
# than the attempt ceiling for one reason: the per-destination ceiling is applied to what a page
# holds, and a page is chosen by retry order, so a destination with more pending rows than the page
# (`MAX_PENDING_EVENTS_PER_DESTINATION` is 500 against a page of 200) whose rows never advance --
# paused after a 410, or skipped for the rest of a pass after one failure -- owned the whole page and
# every other client's deliveries never entered it. The read pages past throttled rows up to this
# bound, five pages of the attempt ceiling: enough for two such destinations to be paged past with
# room to spare, and small enough that the read stays a bounded, predictable cost per cycle.
MAX_DELIVERY_SCAN_PER_CYCLE = MAX_DELIVERY_ATTEMPTS_PER_CYCLE * 5
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
# HOW LONG A CLAIM HOLDS A DELIVERY AGAINST OTHER WORKERS (U8, KTD14), and the arithmetic is the
# whole of it. After `_claim_body` commits, one attempt still has to: read the event (one Firestore
# call at `_CALL_DEADLINE_S`, 4 s), POST it (`webhook_sender.DEFAULT_TIMEOUT_S`, 5 s, of which 2 s
# is the connect), and write the terminal state (4 s again). 13 s worst case, so the plan's "~15 s"
# leaves two seconds of margin for signing, scheduling and a slow GC pause -- 20 s is the same
# number with room, and what it costs is the only thing the lease delays: a delivery whose holder
# DIED is re-offered 20 s later instead of 13. Against an event validity of 60 minutes and a pass
# period of ~296 s that is free.
#
# Distinct from `RESERVATION_TTL_S` below and not to be conflated with it: that one is a slot on the
# SUBSCRIPTION protecting R11's daily cap, this one is a hold on the DELIVERY protecting against two
# workers POSTing the same event. They have different owners, different documents and different
# failure modes.
DELIVERY_LEASE_S = 20.0
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
#
# **REWRITTEN FOR A WORKER WITH NO PASS PERIOD (U8, step 5), and the number did not move.** The
# upper half of that argument was "shorter than the pass period", which is a property of the poller
# and stops holding the moment delivery moves to its own Deployment (U11): a worker draining a queue
# has no period at all, so there is no cycle for a leaked reservation to cost. What replaces it is
# the lower half plus `DELIVERY_LEASE_S` above. A reservation is taken by a claim, and a claim now
# holds a 20 s lease; a holder that dies is re-claimed as soon as that lapses, and the re-claim
# either confirms the slot (`_sent_body`) or releases it (`_failed_body`). So the interval a leaked
# reservation can survive is bounded by the LEASE and the retry behind it, not by the pass -- 120 s
# is six leases, which is the margin, and the ceiling that actually matters is still that it must be
# far below an Oslo day. Do not read this number as "one cycle" once U11 lands; read it as "long
# enough that no live attempt loses its own slot, short enough that a dead one cannot keep it".
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

#: The channel a subscription is delivered on, written on every subscription document. Only one
#: exists today; the second is the value /varsel's registrations take when they move into this
#: store (decided 2026-09-09), named now so that move is a new value rather than a backfill of
#: every document written before it. A document without the field reads as ``webhook``.
CHANNEL_WEBHOOK = "webhook"
CHANNEL_WEB_PUSH = "web_push"
CHANNELS = (CHANNEL_WEBHOOK, CHANNEL_WEB_PUSH)

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
#: The tracing seam (R16). See :func:`configure`.
_span_provider: Callable[..., Any] | None = None


def _span(name: str, **attributes: Any):
    """One span around one Firestore call, or a no-op when nothing was injected.

    **Measured, and mute about its exception.** `measure=True` makes the span carry its own
    `wall_ms` and `cpu_ms`: KTD2 sizes the widened emit pass in interpreter ms per transaction, and
    a Firestore transaction is not pure socket wait — the body, the protobuf and the commit are all
    bytecode under one GIL. `record_exception=False` is the redaction clause: a Firestore error
    message names the document path, and a `subscription_id` embeds the client's own reference, so
    neither the span event nor the status description may carry it (both do, which is why the
    seam sets them together).

    Attributes are counts only, from `tracing_setup.SPAN_ATTRIBUTE_NAMES`; the filter lives there.

    A broken provider degrades to no span rather than failing the store call it observes.
    """
    provider = _span_provider
    if provider is None:
        return nullcontext()
    try:
        return provider(name, attributes, record_exception=False, measure=True)
    except Exception:  # noqa: BLE001 - instrumentation must never fail the call it observes
        return nullcontext()


def _mirror_stamp() -> Any:
    """``SERVER_TIMESTAMP``, resolved by Firestore at commit -- see :data:`MIRROR_FIELD`.

    A function rather than a module constant because the library is imported lazily throughout this
    file: a deployment with the channel switched off imports no Firestore library at all.
    """
    from google.cloud import firestore

    return firestore.SERVER_TIMESTAMP


#: The transaction bodies that stamp :data:`MIRROR_FIELD`, and the ones that deliberately do not.
#:
#: The rule is "every body that changes a field the mirror CONSUMES", which is not the same as
#: "every body that touches a subscription". `_touch_body` is in the list and is the one most
#: easily left out: it moves `expires_at` through `_horizons`, the mirror tests liveness against
#: its own stored copy, and the idempotent re-PUT is the write a client makes most often -- so an
#: unstamped touch means the mirror keeps the stale horizon and DROPS a live subscription when it
#: passes.
#:
#: `_sent_body` is the one most easily left in, and leaving it in would defeat the whole mechanism:
#: it writes the `delivered` marks, so every delivery would make its subscription due for re-read
#: and the watermark would sync the busiest documents on every cycle. The cost of leaving it out is
#: real and bounded: the mirror's `delivered` set can lag, so the pass can word a train already
#: delivered. It cannot DUPLICATE one -- `_emit_body` reads the real subject document inside its
#: transaction and mints nothing for an unchanged subject -- and the standing-subject mirror is
#: what closes the wasted wording. Same reasoning for the claim and reservation-release bodies.
#:
#: `_finish_cleanup_body` is out because it only ever runs on a subscription `_delete_body` has
#: already tombstoned, which the mirror has already dropped on that stamp.
MIRROR_STAMPING_BODIES = (
    "_create_body",
    "_replace_body",
    "_delete_body",
    "_touch_body",
    "_servability_body",
)


def _stream(query: Any) -> list[Any]:
    """One page of a query, read under the module's call deadline and inside one span.

    The single place a streamed read happens, so "one span per Firestore call" is a property of
    this file rather than of whoever writes the next query: a bare
    ``list(query.stream(timeout=_CALL_DEADLINE_S))`` would be an untraced read, and nothing about
    it would look wrong — the store would simply stop accounting for one of its own calls.
    Transparent by construction: same list, same order, same timeout.
    """
    with _span("firestore_stream"):
        return list(query.stream(timeout=_CALL_DEADLINE_S))


def configure(
    *,
    client: Any | None = None,
    database: str | None = None,
    clock: Callable[[], datetime] | None = None,
    span: Callable[..., Any] | None = None,
) -> None:
    """Wire the store: a named database (production) or a ready-made client (tests, the emulator).

    Production passes only ``database`` -- the client is built lazily on first use and takes its
    PROJECT from the ambient environment (``GOOGLE_CLOUD_PROJECT`` / ADC), never from an argument
    and never from a literal in this file. That is what R1's environment isolation rests on: each
    environment is its own GCP project with its own runtime identity, so a dev credential cannot
    name prd's data because nothing in this code path can name a project at all.

    ``clock`` is injectable because a transaction body may run more than once and must therefore
    read no clock: every public method takes its instant ONCE, here, and passes it down.

    ``span`` is main's ``tracing_setup.span`` (R16). A CALLABLE and not an import, like every other
    seam here: this module must stay importable with nothing configured, and importing
    ``tracing_setup`` would run that module's import-time ``setup()`` in every suite that touches
    the store. Absent, :func:`_span` is a no-op and the store is byte-for-byte what it was.
    """
    global _firestore_client_obj, _database, _clock, _span_provider
    _firestore_client_obj = client
    _database = database
    _clock = clock or (lambda: datetime.now(UTC))
    _span_provider = span


def enabled() -> bool:
    """Is there anything to read? A client was injected, or a database id was named."""
    return _firestore_client_obj is not None or bool(_database)


def database_id() -> str | None:
    """The named database this deployment talks to, for a config endpoint and for diagnostics."""
    return _database


def reset_for_tests() -> None:
    configure(client=None, database=None, clock=None)
    forget_recent_load()


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


def daily_cap_of(doc: Mapping[str, Any]) -> int:
    """The subscription's own daily cap, or :data:`MAX_ALERT_DELIVERIES_PER_DAY` without one.

    Per client since 2026-09-09 (``notification_auth.Quotas.daily_cap``), copied onto the document
    at create, replace and touch so the delivery pass reads it from the document it already holds.
    A missing or unreadable field is the default, never zero: a document written before the field
    existed must keep /varsel's four rather than lose its allowance.
    """
    raw = doc.get("daily_cap")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return MAX_ALERT_DELIVERIES_PER_DAY


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
    cap = daily_cap_of(doc)
    counters = doc.get("daily_deliveries") or {}
    spent = counters.get(oslo_day(now), 0) if isinstance(counters, Mapping) else 0
    try:
        spent_n = int(spent)
    except (TypeError, ValueError):
        spent_n = cap  # unreadable counter = no allowance (fail closed)
    return max(0, cap - spent_n - len(_live_reservations(doc, now)))


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
        name="mirror_seed",
        collection=SUBSCRIPTIONS,
        # NO filters, and ordered by `created_at` rather than by the watermark field. Both are
        # deliberate. No filters because the mirror holds every channel and applies `status`,
        # `channel` and liveness in Python -- a `status == "active"` equality here would mean a
        # tombstone written during the seed is never learned about. And `created_at` because a
        # Firestore `order_by` EXCLUDES documents that lack the field, so ordering by
        # `mirror_updated_at` would skip every document written before it existed, which on the
        # deploy that introduces it is all of them.
        order_by=(("created_at", "ASCENDING"),),
        why="the mirror's one full read, at startup",
    ),
    QuerySpec(
        name="mirror_seed_subjects",
        collection=SUBJECTS,
        # `created_at` for `mirror_seed`'s reason: an `order_by` excludes documents that lack the
        # field, and every subject document carries this one.
        order_by=(("created_at", "ASCENDING"),),
        why="the standing-subject mirror's one full read, at startup",
    ),
    QuerySpec(
        name="mirror_sync",
        collection=SUBSCRIPTIONS,
        inequality=((MIRROR_FIELD, ">"),),
        order_by=((MIRROR_FIELD, "ASCENDING"),),
        why="everything written since the mirror's watermark",
    ),
    QuerySpec(
        name="subscriptions",
        collection=SUBSCRIPTIONS,
        equality=("client_id", "status"),
        order_by=(("created_at", "ASCENDING"),),
        why="a client lists its own subscriptions",
    ),
    QuerySpec(
        name="subscription_count",
        collection=SUBSCRIPTIONS,
        # `channel` since U5: the ceiling this counts for is the client's WEBHOOK ceiling, and a
        # traveller's `web_push` document is not a client's subscription at all. Counted together,
        # /varsel's own registrations moving into this collection (U12-U14) would exhaust every
        # client's ceiling on a collection they do not own.
        equality=("client_id", "status", "channel"),
        # `expires_at > now` since U7, which is what turns this from a bounded PAGE into a `count()`
        # aggregation: expiry is now asked of the index instead of tested in Python over the rows it
        # returned. The ordering is unchanged and so is `index_fields` -- the range field is already
        # the first ordering, which is Firestore's classic rule -- so this query needs no new index
        # and the terraform plan is empty (`test_the_ceiling_count_needs_no_new_index` pins that).
        #
        # Why the ordering was DESCENDING, kept because the fallback reasoning still has to hold if
        # anyone ever pages this again: the count came from a page bounded AT the ceiling and
        # liveness was tested in Python, so the page had to shed dead rows before live ones. Read
        # through the listing's `created_at` ordering until 2026-09-10, the page filled with the
        # OLDEST rows first -- exactly where rows TTL has not caught up with sit -- so the count
        # under-read and a create past the ceiling was admitted (R21).
        inequality=(("expires_at", ">"),),
        order_by=(("expires_at", "DESCENDING"),),
        why="the create-time ceiling count",
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
# Three one-line helpers so a fake transaction has exactly three read shapes to implement, and so
# the reads-before-writes rule is visible at every call site rather than inferred from the client's
# API. It was two until U7 moved the create-time ceiling onto a `count()` aggregation, which the
# Python client does NOT reach through `Transaction.get` -- that method accepts a `DocumentReference`
# or a `Query` and raises `ValueError` on anything else, so the aggregation carries the transaction
# itself.


def _read(transaction: Any, ref: Any) -> Any:
    return ref.get(transaction=transaction, timeout=_CALL_DEADLINE_S)


def _read_query(transaction: Any, query: Any) -> list[Any]:
    return list(transaction.get(query, timeout=_CALL_DEADLINE_S))


def _read_count(transaction: Any, query: Any) -> int:
    """How many documents the query matches, read INSIDE the transaction.

    **No stream fallback here, unlike :func:`_count_destination_deliveries`.** That one keeps the
    stream because the two are the same answer at a different price and a build without aggregation
    support should degrade in cost rather than refuse to emit. This one bounds a client's create at
    `notification_auth.QUOTA_BOUNDS["max_active_subscriptions"]`, which is 200 000 -- so a silent
    fallback would put a 200 000-document read under pessimistic locks on every create the client
    makes. A `FailedPrecondition` nobody can miss is the better failure.
    """
    result = query.count(alias="n").get(transaction=transaction, timeout=_CALL_DEADLINE_S)
    return int(result[0][0].value)


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

    ONE span covers all five of the client's internal retries, deliberately: what the pass is
    charged is what the whole call cost it, and a per-attempt span would report the cheapest
    attempt as the transaction's cost.
    """
    from google.cloud import firestore

    client = _firestore_client()
    transaction = client.transaction()

    @firestore.transactional
    def _wrapped(txn: Any) -> Any:
        return body(txn)

    with _span("firestore_transaction"):
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
    #: Defaulted rather than required, because every caller today is the webhook API and a required
    #: field would make this a breaking change to a shape three modules build. U12 gives /varsel's
    #: own registrations `web_push` through the same builder.
    channel: str = CHANNEL_WEBHOOK

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
        # Validated against the vocabulary rather than left free, because it is now a QUERY key:
        # a typo'd channel writes a document that no read path's equality filter matches, which is
        # a subscription that exists, is billed for, and is never loaded or counted by anything.
        if self.channel not in CHANNELS:
            raise Conflict(f"channel must be one of {', '.join(CHANNELS)}")
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
    channel: str = CHANNEL_WEBHOOK
    daily_cap: int = MAX_ALERT_DELIVERIES_PER_DAY

    def as_recipient_document(self, now: datetime) -> dict[str, Any]:
        """The raw document ``varsel.WebhookRecipientProvider`` is contracted to yield.

        Built by :func:`_recipient_document`, which :class:`MirrorEntry` also calls: the mirror
        serves this same shape from memory, and two builders would be two shapes the day one of
        them gained a field.
        """
        return _recipient_document(self, now)


def _recipient_document(record: Any, now: datetime) -> dict[str, Any]:
    """The ONE statement of the shape ``varsel.WebhookRecipientProvider`` yields.

    Exactly the keys ``varsel._parse_webhook_recipient`` reads, plus the ids needed to route a match
    back to a destination. The extra keys are safe by that function's own design: it validates the
    fields the pass reads and is indifferent to the rest, so a store that adds a field does not
    start failing every document in the collection.

    ``delivered`` is Firestore's counterpart of ``varsel._notified`` for this channel, at the grain
    events are minted at: the ``alert_id`` of every (subscription, train) already delivered to THIS
    subscription. Not the browser ledger's per-situation grain -- a second train under a delivered
    situation is a new event here and must read as pending. Marks whose window has passed are
    dropped here rather than stored forever.

    Takes any record carrying the attributes rather than a :class:`Subscription` specifically, so
    the mirror's slimmer entry can serve the identical document without a second builder.
    """
    return {
        "recipient_id": record.subscription_id,
        "subscription_id": record.subscription_id,
        "client_id": record.client_id,
        # Carried so the READER can refuse what it cannot route (R20's rule: this module hands over
        # the shape, `varsel._parse_webhook_recipient` decides what it can read). The store's query
        # already filters on it, and the mirror filters in PYTHON -- so from U6 this field is not a
        # second statement of the same thing, it is the only one the pass sees.
        "channel": record.channel,
        "destination_id": record.destination_id,
        "external_ref": record.external_ref,
        "version": record.version,
        "test": record.test,
        "from_stop": record.from_stop,
        "to_stop": record.to_stop,
        "begin": record.begin,
        "end": record.end,
        "days": list(record.days),
        "delivered": sorted(_prune_marks(record.delivered, now)),
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
        channel=str(doc.get("channel") or CHANNEL_WEBHOOK),
        daily_cap=daily_cap_of(doc),
    )


def _new_subscription_doc(
    spec: SubscriptionSpec, now: datetime, *, daily_cap: int | None = None
) -> dict[str, Any]:
    return {
        "subscription_id": spec.subscription_id,
        "client_id": spec.client_id,
        "destination_id": spec.destination_id,
        "channel": spec.channel,
        "from_stop": spec.from_stop,
        "to_stop": spec.to_stop,
        "begin": spec.begin,
        "end": spec.end,
        "days": [int(d) for d in sorted(set(spec.days))],
        "external_ref": spec.external_ref,
        "test": bool(spec.test),
        "version": 1,
        "status": "active",
        "daily_cap": MAX_ALERT_DELIVERIES_PER_DAY if daily_cap is None else int(daily_cap),
        "definition_digest": _definition_digest(spec),
        "created_at": now,
        "updated_at": now,
        MIRROR_FIELD: _mirror_stamp(),
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
    max_active: int | None = None,
    daily_cap: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Create one subscription, refusing past the ceiling BEFORE any write.

    ``max_active`` and ``daily_cap`` are the CLIENT's (``notification_auth.Quotas``); ``None``
    means the module constants, resolved here at call time so a test that moves the constant moves
    the ceiling.

    The ceiling is counted inside the transaction from the client's own subscriptions rather than
    from a counter document, deliberately. A counter over-counts and drifts: a subscription past one
    of R5's horizons is still ``status == "active"`` until TTL deletes it up to ~24 h later, so a
    client would be locked out by dead rows, and a crash between the tombstone and the decrement
    would leave the counter permanently wrong with nothing to reconcile it against.

    **Counted by an aggregation since U7, not by reading the rows.** It read them until then --
    up to ``max_active`` read locks under Standard edition's pessimistic concurrency, bounded and
    paid on a create, which a commuter does once. That was the argument at a ceiling of 500 and it
    is what put the 10 000 bound on ``QUOTA_BOUNDS``; at the 200 000 this plan sizes for, "bounded"
    stops meaning "affordable" and every create the client makes pays it. A ``count()`` bills
    roughly one read per thousand index entries and takes no per-document lock.

    **What the aggregation counts is a SUPERSET of live, never a subset**, which is the property
    R21 rests on. :func:`subscription_is_live` tests four things; the query tests three of them
    (``status == "active"``, the client, the channel) plus ``expires_at > now``. That field is
    written as the MINIMUM of the two horizons, so a document inside both of them is necessarily
    inside it -- every live row is counted. The converse does not hold: a document whose stored
    ``expires_at`` is somehow ahead of a horizon it should have been derived from is counted while
    :func:`subscription_is_live` would reject it. So the count can only ever be too HIGH, and too
    high refuses a create that a stricter count would have admitted. Refusing is the direction a
    ceiling must fail in; admitting past it is the one R21 forbids.
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
    ceiling = MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT if max_active is None else max_active
    # The query is bounded at the ceiling by the helper that built it, so this is
    # `min(actual, ceiling)` rather than the true count -- which is all the comparison below needs,
    # and what keeps the aggregation's scan bounded by the same number R21 states.
    live = _read_count(transaction, count_query)
    if live >= ceiling:
        raise QuotaExceeded(
            f"client already holds {live} active subscriptions (ceiling {ceiling})"
        )
    doc = _new_subscription_doc(spec, now, daily_cap=daily_cap)
    transaction.set(refs.subscription(spec.subscription_id), doc)
    return "created", doc


def create_subscription(
    spec: SubscriptionSpec,
    *,
    max_active: int | None = None,
    daily_cap: int | None = None,
    now: datetime | None = None,
) -> Subscription:
    """Register a subscription. Idempotent on (id, definition); refused past the ceiling.

    The ceiling and the daily cap are the client's own when the API passes them, and the module
    constants otherwise (resolved here, not in the signature, so a test may move the constant).
    """
    spec.validate()
    instant = now or _now()
    ceiling = MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT if max_active is None else max_active
    refs = _Refs(_firestore_client())
    count_query = _query(
        "subscription_count",
        # The client's own ceiling, on the channel it is creating for. A client's webhook ceiling is
        # not spent by a traveller's `web_push` document in the same collection (U12-U14).
        equality={"client_id": spec.client_id, "status": "active", "channel": spec.channel},
        # The instant is the caller's, taken once outside the transaction like every other clock
        # here -- a transaction body may run more than once and must read no clock of its own, and
        # a query built inside it would take a different `now` on the retry.
        inequality={"expires_at": instant},
        limit=ceiling,
    )
    outcome, doc = _run_transaction(
        lambda txn: _create_body(
            txn, refs, count_query, spec=spec, now=instant, max_active=ceiling, daily_cap=daily_cap
        )
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
    daily_cap: int | None = None,
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
    if daily_cap is not None:
        patch["daily_cap"] = int(daily_cap)
    patch[MIRROR_FIELD] = _mirror_stamp()
    transaction.update(refs.subscription(spec.subscription_id), patch)
    return {**stored, **patch}


def replace_subscription(
    spec: SubscriptionSpec,
    *,
    expected_version: int | None = None,
    daily_cap: int | None = None,
    now: datetime | None = None,
) -> Subscription:
    spec.validate()
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc = _run_transaction(
        lambda txn: _replace_body(
            txn,
            refs,
            spec=spec,
            expected_version=expected_version,
            now=instant,
            daily_cap=daily_cap,
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
        # The mirror learns of a revocation HERE and nowhere else: the cleanup that follows only
        # redacts a document the mirror has already dropped, and TTL's eventual hard delete writes
        # nothing at all.
        MIRROR_FIELD: _mirror_stamp(),
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
    # Nothing may stand for a tombstoned subscription: `_subscription_obsolete` refuses every later
    # emit for it anyway, and the poller's subscription mirror stops matching it -- so the standing
    # set would otherwise hold entries that can never move again until their week of TTL ran out.
    # The subject DOCUMENTS survive until `cleanup_subscription` deletes them, which is what R19
    # wants: the tombstone is the moment the subscription stops being routable, not the moment its
    # history stops existing.
    _subject_mirror.drop_subscription(subscription_id)
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
    transaction: Any,
    refs: _Refs,
    *,
    subscription_id: str,
    client_id: str,
    now: datetime,
    daily_cap: int | None = None,
) -> dict[str, Any]:
    """Push the inactivity horizon out. Reads first, so it cannot resurrect a tombstone.

    ``daily_cap`` rides along when the API passes it: the idempotent re-PUT is the write a client
    makes most often, so it is how a changed client cap reaches subscriptions that already exist.
    """
    snapshot = _read(transaction, refs.subscription(subscription_id))
    stored = _doc(snapshot)
    if not stored:
        raise NotFound("no such subscription")
    if stored.get("client_id") != client_id:
        raise Forbidden("subscription belongs to another client")
    if not subscription_is_live(stored, now):
        raise NotFound("subscription is deleted or past one of its horizons")
    created = _as_dt(stored.get("created_at")) or now
    # `_horizons` moves `expires_at`, which the mirror tests liveness against from its OWN copy --
    # so an unstamped touch leaves the mirror holding the stale horizon and dropping a subscription
    # the client has just told us it still wants. This is the write a client makes most often.
    patch: dict[str, Any] = {
        "last_seen_at": now,
        "updated_at": now,
        MIRROR_FIELD: _mirror_stamp(),
        **_horizons(created, now),
    }
    if daily_cap is not None:
        patch["daily_cap"] = int(daily_cap)
    transaction.update(refs.subscription(subscription_id), patch)
    return {**stored, **patch}


def touch_subscription(
    subscription_id: str,
    *,
    client_id: str,
    daily_cap: int | None = None,
    now: datetime | None = None,
) -> Subscription:
    """Record that the client is still interested -- R5's inactivity horizon moves with this."""
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc = _run_transaction(
        lambda txn: _touch_body(
            txn,
            refs,
            subscription_id=subscription_id,
            client_id=client_id,
            now=instant,
            daily_cap=daily_cap,
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
    patch = {
        "servable": servable,
        "servable_checked_at": now,
        "updated_at": now,
        MIRROR_FIELD: _mirror_stamp(),
    }
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
    max_active: int | None = None,
) -> list[Subscription]:
    """A client's own live subscriptions, oldest first, starting after ``after``.

    ``max_active`` is the CLIENT's ceiling (``notification_auth.Quotas``), the module constant when
    ``None``. It bounds both the page and the scan below, and it has to be the client's own: bounded
    by the constant, a client whose quota was raised past 500 could never page past its first 500
    live rows -- the page equalled the read, so no cursor was ever emitted -- and 500 expired rows
    ahead of its live ones would end the scan on an empty page.

    **The scan is additionally capped at ``MAX_SUBSCRIPTION_LIST_SCAN``** since U7. The client's
    ceiling alone was a sound scan bound while it was bounded at 10 000; at 200 000 a listing over
    a collection full of rows TTL has not deleted yet becomes a 200 000-document read driven by a
    `?limit=1` request. The cap is that old bound, so nothing about the listing's worst case
    changed when the quota did.

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
    bound = min(
        MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT if max_active is None else max_active,
        MAX_SUBSCRIPTION_LIST_SCAN,
    )
    ceiling = min(limit, bound)
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
    while len(out) < ceiling and scanned < bound:
        requested = min(ceiling - len(out), bound - scanned)
        page = _stream(
            _query(
                "subscriptions",
                equality={"client_id": client_id, "status": "active"},
                limit=requested,
                start_after=cursor,
            )
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
    #: Either :class:`Subscription` records, from a direct read, or :class:`MirrorEntry` ones from
    #: the mirror. The one reader of this beyond the pass -- `notification_servability` -- uses the
    #: fields both carry (`subscription_id`, the pair, `servable`, `servable_checked_at`,
    #: `version`), so it is indifferent to which, and typing it to the union rather than converting
    #: keeps the mirror from paying a copy per subscription per cycle.
    subscriptions: tuple[Subscription | MirrorEntry, ...]
    truncated: bool
    skipped: int
    expired: int


# The notification pass's read of the collection, kept for the servability re-check that runs later
# in the SAME cycle (`notification_servability.recheck`). Both walk every live subscription, and
# until 2026-09-10 each read the collection for itself -- two full reads per cycle, the second for
# a question that is due on a handful of documents every six hours. This is a stamped snapshot with
# an age bound, not a ledger: nothing writes through it, and the re-check reads for itself when it
# is stale or absent, so a cycle whose pass raised still re-checks. Under a lock because the pass
# reads on a worker thread (`varsel._load_webhook_recipients`) and the re-check on the poller's.
_recent_load_lock = threading.Lock()
_recent_load: tuple[datetime, LoadedSubscriptions] | None = None


def _remember_load(loaded_at: datetime, loaded: LoadedSubscriptions) -> None:
    global _recent_load
    with _recent_load_lock:
        _recent_load = (loaded_at, loaded)


def recent_load(*, now: datetime, max_age_s: float) -> LoadedSubscriptions | None:
    """The pass's read of the live subscriptions if it is at most ``max_age_s`` old, else ``None``.

    Strictly non-negative age: a clock that has gone backwards (an injected test clock, a
    corrected node) makes the snapshot unusable rather than "very fresh".
    """
    with _recent_load_lock:
        if _recent_load is None:
            return None
        loaded_at, loaded = _recent_load
    age = (now - loaded_at).total_seconds()
    return loaded if 0 <= age <= max_age_s else None


def forget_recent_load() -> None:
    global _recent_load
    with _recent_load_lock:
        _recent_load = None


# ---- The subscription mirror (U6, KTD5) ---------------------------------------------------------
#
# Why a mirror at all. Until U6 the pass read every live subscription from Firestore on every cycle,
# bounded at 2 000 documents. At 100 000 that read is the cycle: R3 sizes it at ~$9-18 a day in
# document reads alone, and the 2 000 ceiling truncates SILENTLY -- the load simply returns a page
# and the subscriptions past it are not matched, with nothing in the log that reads as data loss.
#
# What replaces it is not a cache with a TTL. A TTL cache answers "is this old?" and the question
# that matters is "what has CHANGED?", which is what the watermark answers: one query per cycle for
# the documents whose `mirror_updated_at` is newer than the last one seen. A subscription nothing
# writes to is read once, at startup, and never again.

#: How far BEHIND the watermark each sync re-reads. Firestore commits are not ordered by the
#: instant a transaction started, so a transaction that began before the last sync can commit with
#: a server stamp EARLIER than a document that sync already saw -- and a strict `> watermark` would
#: step over it forever. The overlap costs re-reading whatever was written in the last two minutes
#: and is applied on top of a dedupe by document id, so an entry re-read is simply re-applied.
MIRROR_SYNC_OVERLAP_S = 120.0
#: Documents per page, for both the startup read and each cycle's sync. Bounds one Firestore call
#: rather than the whole read: the startup read pages with a cursor until the collection is
#: exhausted, beating the poller's heartbeat between pages.
MIRROR_PAGE_SIZE = 1000
#: How many pages ONE cycle's sync may take before it stops and carries its cursor to the next.
#: A sync that had to read 100 000 changed documents would otherwise hold `varsel_notify` for the
#: whole read; bounded, it catches up over several cycles and the mirror is merely behind, which is
#: the state `mirror_age` publishes.
MIRROR_MAX_PAGES_PER_SYNC = 20
#: When the watermark gets this old, the mirror stops trusting incremental sync and re-seeds.
#:
#: **This is a correctness bound, not a freshness one, and it is why the number is what it is.** A
#: revocation reaches the mirror as a tombstone (`_delete_body`), and that tombstone is itself a
#: document with a TTL: Firestore hard-deletes it roughly a day later. If sync is broken for longer
#: than that, the tombstone is gone before the mirror ever sees it, the removal is lost for good,
#: and the poller keeps alerting a recipient whose client revoked it -- which no later sync can
#: repair, because there is nothing left to read. Six hours is well inside that window and well
#: above three median cycle periods, so an ordinary slow patch never trips it.
MIRROR_MAX_AGE_S = 6 * 60 * 60.0


@dataclass(frozen=True, slots=True)
class MirrorEntry:
    """One mirrored subscription: what the pass reads, and nothing else.

    Slotted and deliberately slimmer than :class:`Subscription`, which carries `daily_deliveries`,
    `daily_reservations`, `cleanup` and four more timestamps -- all of them spent inside store
    transactions and none of them read by the matching pass. At 100 000 subscriptions that
    difference is the difference between a mirror that fits the pod's memory limit and one that
    does not.

    ``liveness`` is the four fields :func:`subscription_is_live` reads, built ONCE here rather than
    assembled per check: the liveness pass asks about every entry on every cycle, and there is
    exactly one implementation of the question because the alternative is two that disagree about
    an expired subscription.
    """

    subscription_id: str
    client_id: str
    destination_id: str
    external_ref: str
    version: int
    test: bool
    channel: str
    from_stop: str
    to_stop: str
    begin: str
    end: str
    days: tuple[int, ...]
    delivered: Mapping[str, datetime]
    liveness: Mapping[str, Any]
    mirror_updated_at: datetime | None
    # Not read by the matching pass at all -- these are `notification_servability`'s, which runs
    # later in the SAME cycle over the same population and reuses this read through
    # `recent_load` rather than walking the collection a second time. Carrying two more fields is
    # what lets that arrangement survive the mirror; without them the re-check would go back to a
    # full read per cycle, which is most of what U6 exists to remove.
    servable: bool | None = None
    servable_checked_at: datetime | None = None

    def is_live(self, now: datetime) -> bool:
        return subscription_is_live(self.liveness, now)

    def as_recipient_document(self, now: datetime) -> dict[str, Any]:
        return _recipient_document(self, now)


def _mirror_entry(doc: Mapping[str, Any]) -> MirrorEntry:
    """One stored document to a mirror entry, raising on anything this binary cannot read.

    Goes through :func:`_subscription_from_doc` rather than reading the document a second way: that
    function is where a document's field-by-field meaning lives, including every permissive default
    a newer binary's document depends on, and a second parser here would be a second set of
    defaults to keep in step.
    """
    sub = _subscription_from_doc(doc)
    return MirrorEntry(
        subscription_id=sub.subscription_id,
        client_id=sub.client_id,
        destination_id=sub.destination_id,
        external_ref=sub.external_ref,
        version=sub.version,
        test=sub.test,
        channel=sub.channel,
        from_stop=sub.from_stop,
        to_stop=sub.to_stop,
        begin=sub.begin,
        end=sub.end,
        days=tuple(sub.days),
        delivered=dict(sub.delivered),
        liveness={
            "status": sub.status,
            TTL_FIELD: sub.expires_at,
            "lifetime_expires_at": sub.lifetime_expires_at,
            "inactivity_expires_at": sub.inactivity_expires_at,
        },
        mirror_updated_at=_as_dt(doc.get(MIRROR_FIELD)),
        servable=sub.servable,
        servable_checked_at=sub.servable_checked_at,
    )


@dataclass(frozen=True, slots=True)
class MirrorSync:
    """What one sync did, for the phase record. Every field is a number an operator can act on."""

    synced: int = 0
    removed: int = 0
    pages: int = 0
    size: int = 0
    reseeded: bool = False
    truncated: bool = False
    watermark_age_s: float | None = None
    error: str | None = None


class SubscriptionMirror:
    """Every live subscription, held in memory and kept current by a watermark query.

    Three properties the pass depends on, none of which a TTL cache has:

    * **A failed sync keeps the previous mirror.** The alternative -- an empty mirror on a transient
      Firestore error -- is indistinguishable to every downstream reader from "every client
      unsubscribed", and it would silently retire an entire channel for one bad minute.
    * **A document nothing writes to is read once.** That is the whole economy of the design and is
      why `_sent_body` must not stamp: the busiest documents would otherwise be the ones re-read
      every cycle.
    * **Removal is applied, not inferred.** A tombstone arrives as a document whose status is no
      longer active, and the entry is dropped. Expiry is the one removal nothing writes, so it is
      applied locally on every read, from the entry's own horizons.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, MirrorEntry] = {}
        self._watermark: datetime | None = None
        self._seeded = False
        self._seeded_at: datetime | None = None
        self._cursor: Any = None

    # -- state a reader can ask about ------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Has the startup read completed? Until it has, this mirror answers for nobody.

        Read rather than inferred from emptiness: an empty collection is a legitimate steady state
        (no client onboarded) and must not read as "still loading", or a deployment with no clients
        would hold its emit pass forever.
        """
        with self._lock:
            return self._seeded

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def watermark_age_s(self, now: datetime) -> float | None:
        with self._lock:
            mark = self._watermark or self._seeded_at
        if mark is None:
            return None
        return max(0.0, (now - mark).total_seconds())

    def reset(self) -> None:
        """Drop everything. For tests and for a forced re-seed."""
        with self._lock:
            self._entries = {}
            self._watermark = None
            self._seeded = False
            self._seeded_at = None
            self._cursor = None

    # -- reads -----------------------------------------------------------------------------------

    def recipients(self, now: datetime, *, channel: str = CHANNEL_WEBHOOK) -> list[MirrorEntry]:
        """The live entries on one channel, liveness applied here rather than at sync time.

        Applied on the READ because expiry is the one state change nothing writes a document for:
        a subscription whose inactivity horizon passes at 03:00 is not touched by anybody, so no
        sync can carry the news. Testing it here means the pass never matches an expired
        subscription even if the mirror has not been synced since.
        """
        with self._lock:
            entries = list(self._entries.values())
        return [e for e in entries if e.channel == channel and e.is_live(now)]

    def documents(self, now: datetime, *, channel: str = CHANNEL_WEBHOOK) -> list[dict[str, Any]]:
        """:func:`recipient_documents`' shape, served from memory."""
        return [e.as_recipient_document(now) for e in self.recipients(now, channel=channel)]

    # -- writes ----------------------------------------------------------------------------------

    def seed(self, *, now: datetime, beat: Callable[[], None] | None = None) -> MirrorSync:
        """Page the whole collection into the mirror. Runs on the poller thread, after the port binds.

        **Not in the lifespan.** The app serves nothing until the lifespan yields, so a 100 000
        document read there would hold the port closed for the whole of it -- and the readiness
        probe would fail a deployment that is doing exactly what it was told to.

        **Paged with a cursor rather than streamed.** One `stream()` over 100 000 documents is one
        call under one deadline; a cursor makes each page its own bounded call, which is what lets
        `beat` move the poller's liveness clock between pages. Without that, `/health` reads "no
        step has finished in 600 s" and restarts the pod mid-read, forever.

        **Ordered by `created_at`, never by the watermark field.** A Firestore `order_by` excludes
        documents that LACK the field, so ordering by `mirror_updated_at` here would silently skip
        every document written before that field existed -- which, on the deploy that introduces
        it, is all of them.
        """
        entries: dict[str, MirrorEntry] = {}
        watermark: datetime | None = None
        cursor: Any = None
        pages = skipped = 0
        while True:
            query = _query("mirror_seed", limit=MIRROR_PAGE_SIZE, start_after=cursor)
            page = _stream(query)
            pages += 1
            for snapshot in page:
                stored = _doc(snapshot)
                try:
                    entry = _mirror_entry(stored)
                except Exception:
                    skipped += 1
                    logger.debug("Unreadable subscription document; not mirrored", exc_info=True)
                    continue
                entries[entry.subscription_id] = entry
                if entry.mirror_updated_at is not None and (
                    watermark is None or entry.mirror_updated_at > watermark
                ):
                    watermark = entry.mirror_updated_at
            if len(page) < MIRROR_PAGE_SIZE:
                break
            cursor = page[-1]
            if beat is not None:
                beat()
        with self._lock:
            self._entries = entries
            self._watermark = watermark
            self._seeded = True
            self._seeded_at = now
            self._cursor = None
        return MirrorSync(synced=len(entries), pages=pages, size=len(entries), reseeded=True)

    def sync(self, *, now: datetime, beat: Callable[[], None] | None = None) -> MirrorSync:
        """Apply everything written since the watermark. One bounded read per cycle.

        Re-seeds instead when the watermark is older than :data:`MIRROR_MAX_AGE_S` -- see that
        constant for why the bound is a correctness one.
        """
        if not self.ready:
            return self.seed(now=now, beat=beat)
        age = self.watermark_age_s(now)
        if age is not None and age > MIRROR_MAX_AGE_S:
            logger.warning(
                "Subscription mirror watermark is %.0f s old; re-seeding rather than syncing",
                age,
                extra={
                    "fields": {
                        "event": "notification_store_load",
                        "result": "mirror_reseed",
                        "watermark_age_s": int(age),
                    }
                },
            )
            return self.seed(now=now, beat=beat)

        with self._lock:
            watermark = self._watermark
            cursor = self._cursor
        # The overlap, and the reason a sync is applied by document id: the window deliberately
        # re-reads documents this mirror already holds, so applying twice must be a no-op.
        since = (
            watermark - timedelta(seconds=MIRROR_SYNC_OVERLAP_S)
            if watermark is not None
            else _REVISION_EPOCH
        )
        synced = removed = pages = 0
        newest = watermark
        truncated = False
        while pages < MIRROR_MAX_PAGES_PER_SYNC:
            query = _query(
                "mirror_sync",
                inequality={MIRROR_FIELD: since},
                limit=MIRROR_PAGE_SIZE,
                start_after=cursor,
            )
            page = _stream(query)
            pages += 1
            for snapshot in page:
                stored = _doc(snapshot)
                try:
                    entry = _mirror_entry(stored)
                except Exception:
                    logger.debug("Unreadable subscription document; not mirrored", exc_info=True)
                    continue
                # A tombstone is a document whose status is no longer active. It is REMOVED here
                # rather than left to the liveness pass, because `_delete_body` also pulls both
                # horizons back to `now` -- so the two agree, and this is the path that carries a
                # revocation the instant it is written rather than at the next expiry check.
                if entry.liveness.get("status") != "active":
                    with self._lock:
                        if self._entries.pop(entry.subscription_id, None) is not None:
                            removed += 1
                else:
                    with self._lock:
                        self._entries[entry.subscription_id] = entry
                    synced += 1
                if entry.mirror_updated_at is not None and (
                    newest is None or entry.mirror_updated_at > newest
                ):
                    newest = entry.mirror_updated_at
            if len(page) < MIRROR_PAGE_SIZE:
                cursor = None
                break
            cursor = page[-1]
            if beat is not None:
                beat()
        else:
            # The page budget bound before the collection was drained. The cursor is CARRIED so the
            # next cycle continues where this one stopped rather than re-reading from the overlap,
            # and the watermark is not advanced past what was applied.
            truncated = True

        with self._lock:
            self._watermark = newest
            self._cursor = cursor
            size = len(self._entries)
        return MirrorSync(
            synced=synced,
            removed=removed,
            pages=pages,
            size=size,
            truncated=truncated,
            watermark_age_s=self.watermark_age_s(now),
        )


# ---- The standing-subject mirror (U6 step 3, KTD2) ----------------------------------------------
#
# The emit pass asks one question per subject -- "does this already stand, and in which phase?" --
# and today it answers with one `client_alerts` query per client, bounded at MAX_SUBJECTS_PER_READ.
# Past that bound the answer is INCOMPLETE, and incomplete is not a slightly worse answer: the pass
# falls back to a store transaction per subject, because treating a truncated read as complete would
# skip a withdrawal that was owed and, worse, a `confirmed` mint for a train ET has cancelled. At one
# client with 100 000 subscriptions the read is truncated on every cycle, so the fallback IS the
# steady state and the emit pass costs a transaction per subject forever.
#
# This mirror answers the same question completely, from memory.
#
# **Kept current by the poller's own commits, not by a watermark.** The subscription mirror needs a
# watermark because clients write subscriptions through the API at any time. Subject documents have
# exactly one writer -- KTD6's single writer: one poller thread, no route invokes the pass, one
# replica under Recreate -- so every change to a subject passes through this process, and applying
# it in memory is both cheaper and stronger than re-reading it. That property is worth re-checking
# rather than assuming when U11 moves delivery to its own Deployment: what the worker writes is
# `delivered_revision` and `pending_delivery_id` on the subject, and neither is in the standing set,
# which carries the phase and the below-threshold run only.


@dataclass(frozen=True, slots=True)
class StandingSubject:
    """One standing subject, as the emit pass needs it -- the phase and the withdrawal run."""

    alert_id: str
    subscription_id: str
    phase: str
    below_threshold_at: datetime | None
    expires_at: datetime | None
    #: How far the open withdrawal run has counted. Carried because it is free -- the document is
    #: being read anyway -- and NOT because anything outside this module may decide a withdrawal
    #: from it: `_withdraw_body` stays the one statement of that rule, run closure included.
    below_threshold_cycles: int = 0


def _standing_subject(doc: Mapping[str, Any]) -> StandingSubject:
    return StandingSubject(
        alert_id=str(doc["alert_id"]),
        subscription_id=str(doc["subscription_id"]),
        phase=str(doc["phase"]),
        below_threshold_at=_as_dt(doc.get("below_threshold_at")),
        expires_at=_as_dt(doc.get(TTL_FIELD)),
        below_threshold_cycles=int(doc.get("below_threshold_cycles") or 0),
    )


class SubjectMirror:
    """Which subjects stand, in which phase -- complete, in memory, for the whole environment.

    Indexed BY SUBSCRIPTION as well as by alert id, and that is not a convenience: the emit pass
    asks per subscription, and a flat map would make each of 100 000 questions a scan of 100 000
    entries. The secondary index is what keeps the pass linear.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_subscription: dict[str, dict[str, StandingSubject]] = {}
        self._seeded = False

    @property
    def ready(self) -> bool:
        """Whether the standing set is COMPLETE. An incomplete one must not be trusted.

        Same reasoning as `_Standing.complete` in `notification_events`, which this replaces: a
        partial standing set that reads as complete skips a withdrawal that was owed and a
        `confirmed` mint for a train ET has cancelled. Until the seed finishes, the pass falls back
        to asking the store, exactly as it does today.
        """
        with self._lock:
            return self._seeded

    def size(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._by_subscription.values())

    def reset(self) -> None:
        with self._lock:
            self._by_subscription = {}
            self._seeded = False

    def seed(self, *, now: datetime, beat: Callable[[], None] | None = None) -> MirrorSync:
        """Page `alert_subjects` into the standing set. The only full read it ever does."""
        by_subscription: dict[str, dict[str, StandingSubject]] = {}
        cursor: Any = None
        pages = held = 0
        while True:
            query = _query("mirror_seed_subjects", limit=MIRROR_PAGE_SIZE, start_after=cursor)
            page = _stream(query)
            pages += 1
            for snapshot in page:
                stored = _doc(snapshot)
                try:
                    subject = _standing_subject(stored)
                except Exception:
                    logger.debug("Unreadable alert subject; not mirrored", exc_info=True)
                    continue
                # Expiry is applied at seed time AND on every read, for the subscription mirror's
                # reason: TTL runs up to ~24 h late, so an expired subject is still returned by the
                # query, and nothing writes a document when its own TTL passes.
                if not _not_expired(stored, now):
                    continue
                by_subscription.setdefault(subject.subscription_id, {})[subject.alert_id] = subject
                held += 1
            if len(page) < MIRROR_PAGE_SIZE:
                break
            cursor = page[-1]
            if beat is not None:
                beat()
        with self._lock:
            self._by_subscription = by_subscription
            self._seeded = True
        return MirrorSync(synced=held, pages=pages, size=held, reseeded=True)

    def apply(self, doc: Mapping[str, Any]) -> None:
        """Record a subject document this process has just committed.

        Silently ignores a document it cannot read, for the same reason every other per-document
        path here does (R20): the commit has already happened, and raising here would unwind a pass
        over a mirror that is only ever an optimisation.
        """
        try:
            subject = _standing_subject(doc)
        except Exception:
            logger.debug("Committed subject could not be mirrored", exc_info=True)
            return
        with self._lock:
            self._by_subscription.setdefault(subject.subscription_id, {})[subject.alert_id] = (
                subject
            )

    def drop(self, subscription_id: str, alert_id: str) -> None:
        with self._lock:
            subjects = self._by_subscription.get(subscription_id)
            if subjects is None:
                return
            subjects.pop(alert_id, None)
            if not subjects:
                self._by_subscription.pop(subscription_id, None)

    def drop_subscription(self, subscription_id: str) -> None:
        """Everything a revoked subscription was standing on. Called when the mirror drops it."""
        with self._lock:
            self._by_subscription.pop(subscription_id, None)

    def phases_for(self, subscription_id: str, now: datetime) -> dict[str, str]:
        """``alert_id -> phase`` for one subscription, expired subjects excluded."""
        with self._lock:
            subjects = list(self._by_subscription.get(subscription_id, {}).values())
        return {
            s.alert_id: s.phase
            for s in subjects
            if s.expires_at is not None and s.expires_at > now
        }

    def prune(self, now: datetime) -> int:
        """Drop expired subjects, and answer how many. One step of the poller's sync.

        Correctness never needed it -- :meth:`phases_for` and :meth:`open_runs` both test
        ``expires_at`` on the read, for the reason the seed does: TTL runs up to ~24 h late, so a
        document the store still returns can be long dead. This is about MEMORY, and the arithmetic
        is worth stating because it is the one number this mirror grows in. A subject document
        lives :data:`HISTORY_DAYS` (7), so without a prune a pod holds every train every
        subscription was ever warned about for a week: at 100 000 subscriptions and a handful of
        alert-worthy trains each that is high hundreds of thousands of entries, and the pod's
        memory limit equals its request. Pruned, the set is what is still live.
        """
        with self._lock:
            dropped = 0
            for subscription_id in list(self._by_subscription):
                subjects = self._by_subscription[subscription_id]
                for alert_id in [
                    a
                    for a, s in subjects.items()
                    if s.expires_at is None or s.expires_at <= now
                ]:
                    subjects.pop(alert_id, None)
                    dropped += 1
                if not subjects:
                    self._by_subscription.pop(subscription_id, None)
            return dropped

    def open_runs(self, now: datetime) -> dict[str, datetime]:
        """``alert_id -> below_threshold_at`` for every subject with an open withdrawal run.

        Whole-environment rather than per client, which is what `ClientAlerts.open_below_threshold`
        is too: the run is a property of the subject, and the caller looks up the one alert id it
        is about.
        """
        with self._lock:
            everything = [s for subjects in self._by_subscription.values() for s in subjects.values()]
        return {
            s.alert_id: s.below_threshold_at
            for s in everything
            if s.below_threshold_at is not None
            and s.expires_at is not None
            and s.expires_at > now
        }


#: The process's two mirrors. Module-level for `_recent_load`'s reason -- the pass reads them from a
#: worker thread and the poller syncs them from its own -- and both are internally locked.
_subscription_mirror = SubscriptionMirror()
_subject_mirror = SubjectMirror()


def subscription_mirror() -> SubscriptionMirror:
    return _subscription_mirror


def subject_mirror() -> SubjectMirror:
    return _subject_mirror


def reset_mirrors_for_tests() -> None:
    _subscription_mirror.reset()
    _subject_mirror.reset()


def sync_mirrors(
    *, now: datetime | None = None, beat: Callable[[], None] | None = None
) -> MirrorSync:
    """Bring both mirrors up to date. One step of the poller's cycle, before the pass reads them.

    Seeds whichever is not ready yet -- which on a fresh pod is both, and is why this runs on the
    POLLER thread rather than in the lifespan: the app serves nothing until the lifespan yields, so
    a 100 000-document read there would hold the port closed for the whole of it and fail the
    readiness probe of a deployment doing exactly what it was told to.

    Raises what the store raises. The caller decides, and the mirrors keep what they had -- an empty
    mirror on a transient error is indistinguishable from "every client unsubscribed".
    """
    instant = now or _now()
    report = _subscription_mirror.sync(now=instant, beat=beat)
    if not _subject_mirror.ready:
        _subject_mirror.seed(now=instant, beat=beat)
    else:
        # Memory only -- both readers already test expiry -- and here rather than on the read
        # because a prune walks the whole set while a read walks one subscription. See
        # `SubjectMirror.prune` for the arithmetic it bounds.
        _subject_mirror.prune(instant)
    return report


#: The transaction bodies that write a subject document AND must be seen by the standing-subject
#: mirror, declared as a SET for the reason `MIRROR_STAMPING_BODIES` is: what has to hold is a
#: property of the set, and a body added next year is the case no behavioural test over today's
#: four can see. `test_notification_store` pins it against the source both ways.
#:
#: Each of these moves something the emit pass READS BACK -- the phase, or the open withdrawal run
#: -- so an unmirrored one leaves the next cycle answering from a stale standing set: a subject
#: minted in cycle N reads as absent in cycle N+1, `skip_reason` answers `nothing_standing` for the
#: below-threshold observation that follows, and the withdrawal is never counted.
SUBJECT_MIRRORING_BODIES = frozenset({"_mint", "_withdraw_body", "_clear_below_threshold_body"})

#: The one body that writes a subject document and deliberately does NOT mirror it.
#: `_sent_body` writes `delivered_revision`, `delivered_at` and the pending pointer. None of the
#: three is in the standing set, the emit pass never asks about them, and mirroring them would put
#: the busiest documents in the hottest loop for no reader -- the same economy that keeps this body
#: out of `MIRROR_STAMPING_BODIES`. **Re-check this sentence when U11 moves delivery to its own
#: Deployment**: what a worker there writes is exactly those three fields, and the moment one of
#: them enters the standing set this process stops being their only writer.
SUBJECT_WRITERS_OUTSIDE_THE_MIRROR = frozenset({"_sent_body"})


def _mirror_subject(state: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Record a committed subject in the standing set, and hand its document back.

    **The one place a subject reaches the mirror**, so "which writes the mirror sees" is a property
    of this module rather than of whoever remembered; :data:`SUBJECT_MIRRORING_BODIES` names the
    bodies whose writes get here and the suite pins that set against the source.

    Called on the RESULT of a transaction and never from inside a body: a body may run more than
    once, so an apply from inside would record a state a later attempt then overwrote and the
    mirror would answer the next cycle with a phase Firestore never held. Returns its argument so a
    call site reads as one expression.
    """
    if state is not None:
        _subject_mirror.apply(state)
    return state


def recipient_documents() -> list[dict[str, Any]]:
    """``varsel.WebhookRecipientProvider``: the raw documents this cycle should match.

    Served from the mirror since U6, and the per-cycle full read is gone with it. What has NOT
    changed is the shape: `_recipient_document` is the one statement of it, and the mirror and the
    store's own reader both call it, so this returns byte-identical documents to the ones the query
    produced.

    Raw documents rather than parsed records, because `WebhookRecipientProvider` is explicit about
    why: R20 wants an unreadable document skipped rather than failing its collection, and only the
    READER can decide what "unreadable" means for the schema the running binary knows.

    **An unseeded mirror yields nothing, and says so.** Returning a partial set would be worse than
    returning none: the pass cannot tell "this client has no live subscriptions" from "the mirror
    has not finished loading", and a half-loaded mirror would silently retire whatever it had not
    reached yet. `sync_mirrors` runs as its own step before the pass, so a ready mirror is the
    ordinary state and this is the restart window only.
    """
    instant = _now()
    mirror = subscription_mirror()
    if not mirror.ready:
        logger.warning(
            "The subscription mirror has not finished loading; no webhook recipient is matched on "
            "this cycle",
            extra={"fields": {"event": "notification_store_load", "result": "mirror_not_ready"}},
        )
        return []
    entries = mirror.recipients(instant)
    # The servability re-check runs later in the SAME cycle and walks every live subscription too.
    # Stamping the mirror's own read here is what lets it reuse this one instead of reading for
    # itself -- the arrangement that existed before the mirror, kept, because the re-check still
    # wants a `LoadedSubscriptions` and does not care where it came from.
    _remember_load(
        instant,
        LoadedSubscriptions(
            subscriptions=tuple(entries), truncated=False, skipped=0, expired=0
        ),
    )
    logger.debug(
        "Webhook subscription load from the mirror: %d live of %d held",
        len(entries),
        mirror.size(),
        extra={
            "fields": {
                "event": "notification_store_load",
                "result": "mirror",
                "n": len(entries),
                "mirror_size": mirror.size(),
            }
        },
    )
    return [e.as_recipient_document(instant) for e in entries]


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
        page = _stream(query)
        if page:
            writer = client.batch()
            for snapshot in page:
                writer.delete(snapshot.reference)
            writer.commit()
        deleted[key] = len(page)
        full_page = full_page or len(page) >= batch

    # Again after the deletes, and deliberately not only here: `delete_subscription` already
    # dropped it, but cleanup is the retryable path R19 requires and can run long after a restart
    # that re-seeded the mirror from documents this call is now deleting.
    _subject_mirror.drop_subscription(subscription_id)

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
    #: The subject document as it stands AFTER this commit, for the standing-subject mirror, or
    #: ``None`` when nothing about the subject moved.
    #:
    #: **Carried out on the result rather than applied inside the body, and that is not a style
    #: choice.** A transaction body may run more than once (the module header's rule, and the suite
    #: runs every body twice against one fake transaction): an `apply` from inside would record a
    #: state a later attempt then overwrote, and the mirror would answer the next cycle with a
    #: phase Firestore never held. Only `_run_transaction`'s return value is the committed one.
    subject_state: Mapping[str, Any] | None = None


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
        subject_state=subject_doc,
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


def commit_event(
    request: EmitRequest, *, max_pending: int | None = None, now: datetime | None = None
) -> EventCommit:
    """Record one observation about one train, minting a revision only on a lifecycle transition.

    The pending set is read from Firestore, never re-derived from a cycle's decisions: a
    re-derivation would mint a fresh revision and therefore a fresh ``event_id`` on every cycle,
    which the receiver cannot recognise as a duplicate and would turn into a second notification
    about the same train.

    The ceiling on pending events per destination is checked BEFORE the transaction. Exact rather
    than best effort, and the reason is nameable instead of hoped for: the emit path has exactly one
    writer (one poller thread, no route invokes the pass, one replica under the Recreate strategy),
    so nothing can create a pending event between the count and the commit.

    ``max_pending`` is the CLIENT's (``notification_auth.Quotas.max_pending_events``); ``None``
    means the module constant, resolved here at call time exactly as ``max_active`` is on the create
    path. It is the caller's to look up rather than this module's, because the store must not import
    ``notification_auth`` -- that edge already runs the other way.
    """
    if request.phase not in PHASES:
        raise Conflict(f"phase must be one of {PHASES}, not {request.phase!r}")
    instant = now or _now()
    seed = revision_seed(instant)
    refs = _Refs(_firestore_client())
    ceiling = MAX_PENDING_EVENTS_PER_DESTINATION if max_pending is None else max_pending
    pending = _count_destination_deliveries(
        request.destination_id, state="pending", ceiling=ceiling
    )
    if pending >= ceiling:
        logger.warning(
            "Destination %s holds %d pending events; refusing to emit (ceiling %d)",
            request.destination_id,
            pending,
            ceiling,
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
            f"destination holds {pending} pending events (ceiling {ceiling})"
        )
    commit = _run_transaction(
        lambda txn: _emit_body(txn, refs, request=request, now=instant, seed=seed)
    )
    _mirror_subject(commit.subject_state)
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
        patch = {
            "below_threshold_cycles": count,
            "below_threshold_at": now,
            "updated_at": now,
        }
        transaction.update(refs.subject(request.alert_id), patch)
        return EventCommit(
            status="counting",
            alert_id=request.alert_id,
            below_threshold_cycles=count,
            revision=int(subject.get("revision") or 0),
            # The document as the update leaves it, built from the copy this body READ rather than
            # from a fresh one: a patch alone would tell the mirror nothing about the phase, and a
            # re-read would be a second round trip for a document already in hand.
            subject_state={**subject, **patch},
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
    commit = _run_transaction(
        lambda txn: _withdraw_body(txn, refs, request=request, now=instant, seed=seed)
    )
    _mirror_subject(commit.subject_state)
    return commit


def _clear_below_threshold_body(
    transaction: Any, refs: _Refs, *, alert_id: str, now: datetime
) -> dict[str, Any] | None:
    """The subject as the clear leaves it, or ``None`` when there was nothing to clear.

    The document rather than a bool since U9, so the caller can hand it to the standing-subject
    mirror; the public :func:`clear_below_threshold` still answers whether it wrote.
    """
    subject = _doc(_read(transaction, refs.subject(alert_id)))
    # NEVER creates a subject: this says "the run that was open is closed", which is meaningless
    # for a subject that does not exist, and a `set` here would mint a phase-less document the
    # snapshot readers would then have to skip.
    if not subject or not _not_expired(subject, now):
        return None
    if _as_dt(subject.get("below_threshold_at")) is None:
        # No run open, so no write. This is what makes U6's "zero writes on an ordinary standing
        # cycle" affordable: the overwhelmingly common case costs one read and nothing else, and
        # with the mirror's `open_runs` in hand the caller does not even pay that.
        return None
    patch = {"below_threshold_cycles": 0, "below_threshold_at": None, "updated_at": now}
    transaction.update(refs.subject(alert_id), patch)
    return {**subject, **patch}


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
    cleared = _mirror_subject(
        _run_transaction(
            lambda txn: _clear_below_threshold_body(txn, refs, alert_id=alert_id, now=now)
        )
    )
    return cleared is not None


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
    served: int = MAX_ALERTS_SERVED,
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
        subjects=tuple(states[: max(served, MAX_ALERTS_SERVED)]),
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
    served: int = MAX_ALERTS_SERVED,
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
    page = _stream(query)
    rows, _expired = _live_subject_rows(page, instant)
    if client_id is not None:
        for row in rows:
            if str(row.get("client_id") or "") != client_id:
                raise Forbidden("alert subject belongs to another client")
    return _snapshot(
        subscription_id,
        rows,
        now=instant,
        truncated=len(page) >= min(limit, MAX_SUBJECTS_PER_READ),
        served=served,
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
    client_id: str,
    *,
    limit: int = MAX_SUBJECTS_PER_READ,
    served: int = MAX_ALERTS_SERVED,
    now: datetime | None = None,
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
    page = _stream(query)
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
                subscription_id, subject_rows, now=instant, truncated=truncated, served=served
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


def _count_destination_deliveries(destination_id: str, *, state: str, ceiling: int) -> int:
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
        limit=ceiling + 1,
    )
    try:
        result = query.count(alias="n").get(timeout=_CALL_DEADLINE_S)
        return int(result[0][0].value)
    except Exception:
        logger.debug("count() aggregation unavailable; falling back to a stream", exc_info=True)
        # Not `_stream`: this counts without materialising the page, which at the ceiling is the
        # whole point of the fallback. It opens the same span so the call is still accounted for.
        with _span("firestore_stream"):
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
    #: U8's lease. ``None`` on every delivery written before it existed, which reads as unheld --
    #: the direction that keeps a backlog deliverable across the deploy that introduces the field.
    claimed_until: datetime | None
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
        claimed_until=_as_dt(doc.get("claimed_until")),
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
    ``truncated`` says the read stopped before the collection was drained -- the attempt ceiling
    was reached with a full page still in hand, or the scan bound (``MAX_DELIVERY_SCAN_PER_CYCLE``)
    was hit -- so deliverable rows may remain that this cycle never saw. Both are visible to an
    operator (R21) instead of looking like a quiet day.
    """

    ready: tuple[Delivery, ...]
    stale: tuple[str, ...]
    throttled: tuple[str, ...]
    truncated: bool


def pending_deliveries(
    *,
    limit: int = MAX_DELIVERY_ATTEMPTS_PER_CYCLE,
    per_destination_limit: Callable[[str], int] | None = None,
    now: datetime | None = None,
) -> PendingDeliveries:
    """What to attempt this cycle, soonest-departure-first.

    Two orderings, and both are deliberate. The QUERY orders by ``retry_not_before`` because
    Firestore's classic rule requires the first ordering to be on the range-filtered field -- and
    the range filter is what makes a persisted ``Retry-After`` backoff survive a cycle boundary and
    a restart. The RESULT is then re-sorted by validity in Python, because /varsel spends its budget
    soonest-departure-first across registrations and this must too: insertion order handed the whole
    budget to the earliest-registered travellers every busy morning. The consequence to know is that
    at the ceiling the PAGES are chosen by retry order and only their contents are ordered by
    departure.

    **The read pages past a throttled destination** (2026-09-10). The per-destination ceiling is
    applied to what a page holds, and a paused or skipped destination's rows never advance their
    ``retry_not_before`` -- so with more pending rows than one page they sorted first on every cycle,
    filled it, and every other client's deliveries waited behind rows that were never attempted.
    So when the page is full and the attempt set is not, the next page is read from a cursor,
    bounded by ``MAX_DELIVERY_SCAN_PER_CYCLE``. The per-destination ceiling is spent within each
    page soonest-departure-first, which is what the single-page version did.

    ``per_destination_limit`` answers that ceiling from the delivery's own CLIENT_ID
    (``notification_auth.Quotas.deliveries_per_minute``); ``None`` means the module constant for
    every destination. Resolved by the caller rather than here for `commit_event`'s reason -- the
    store must not import ``notification_auth`` -- and keyed on the client rather than the
    destination because a quota is a property of an account and a client may hold several
    destinations, each of which still spends its own ceiling.
    """
    instant = now or _now()
    ceiling = min(limit, MAX_DELIVERY_ATTEMPTS_PER_CYCLE)
    per_client: dict[str, int] = {}

    def _destination_ceiling(client_id: str) -> int:
        if per_destination_limit is None:
            return MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE
        # Memoised for the pass: a page can hold hundreds of deliveries for one client, and the
        # lookup behind this is a cached account read rather than a free one.
        if client_id not in per_client:
            per_client[client_id] = per_destination_limit(client_id)
        return per_client[client_id]

    ready: list[Delivery] = []
    stale: list[str] = []
    per_destination: dict[str, int] = {}
    throttled: set[str] = set()
    seen: set[str] = set()
    scanned = 0
    cursor: Any = None
    exhausted = False
    while len(ready) < ceiling and scanned < MAX_DELIVERY_SCAN_PER_CYCLE:
        requested = min(ceiling, MAX_DELIVERY_SCAN_PER_CYCLE - scanned)
        query = _query(
            "pending_deliveries",
            equality={"state": "pending"},
            inequality={"retry_not_before": instant},
            limit=requested,
            start_after=cursor,
        )
        page = _stream(query)
        scanned += len(page)
        candidates: list[Delivery] = []
        for snapshot in page:
            stored = _doc(snapshot)
            try:
                delivery = _delivery_from_doc(stored)
            except Exception:
                logger.debug("Unreadable webhook delivery document; skipping it", exc_info=True)
                continue
            # A cursor is exact on the real client; the guard is against a page source that is not
            # (a fake that cannot page), where the same row would otherwise be attempted twice.
            if delivery.delivery_id in seen:
                continue
            seen.add(delivery.delivery_id)
            # Every read path tests the expiry field itself: a delivery TTL has not caught up with is
            # not claimable, and one past its validity is expired rather than deliverable.
            if not _not_expired(stored, instant):
                continue
            if delivery.valid_until is None or delivery.valid_until <= instant:
                stale.append(delivery.delivery_id)
                continue
            if delivery.retry_not_before is not None and delivery.retry_not_before > instant:
                continue
            # LEASED rows are filtered in PYTHON, not in the query (U8). A second inequality would
            # need its own composite index and would exclude every document that lacks the field --
            # which, on the deploy that introduces it, is all of them. The cost of filtering here is
            # that a leased row still occupies a slot in the scan; the cost of filtering there is
            # that no delivery written before this release is ever offered again.
            if delivery.claimed_until is not None and delivery.claimed_until > instant:
                continue
            candidates.append(delivery)
        candidates.sort(key=lambda d: (d.valid_until, d.delivery_id))
        for delivery in candidates:
            if len(ready) >= ceiling:
                break
            spent = per_destination.get(delivery.destination_id, 0)
            if spent >= _destination_ceiling(delivery.client_id):
                throttled.add(delivery.destination_id)
                continue
            per_destination[delivery.destination_id] = spent + 1
            ready.append(delivery)
        if len(page) < requested:
            exhausted = True  # the collection is drained: another page would read nothing
            break
        cursor = page[-1]

    ready.sort(key=lambda d: (d.valid_until, d.delivery_id))
    truncated = not exhausted
    if throttled or truncated:
        logger.warning(
            "Webhook delivery set bounded: %d ready, %d throttled destination(s), truncated=%s",
            len(ready),
            len(throttled),
            truncated,
            extra={
                "fields": {
                    "event": "notification_quota",
                    "result": "delivery_attempts",
                    "n": len(ready),
                    "throttled": sorted(throttled),
                    "truncated": truncated,
                    "scanned": scanned,
                }
            },
        )
    return PendingDeliveries(
        ready=tuple(ready),
        stale=tuple(stale),
        throttled=tuple(sorted(throttled)),
        truncated=truncated,
    )


@dataclass(frozen=True)
class DeliveryClaim:
    """The pre-dispatch eligibility recheck.

    ``status``: ``ready`` (dispatch it), ``cancelled`` (the subscription is gone or obsolete -- R13,
    written durably so nothing re-offers it), ``expired`` (validity passed), ``capped`` (R11's
    allowance is spent; recorded and left pending so it is re-offered next cycle), ``leased``
    (another worker holds it mid-attempt -- nothing is written, and it is re-offered when the lease
    lapses) or ``gone`` (no such delivery, or it is no longer pending).
    """

    status: str
    delivery: Delivery | None = None
    subscription: Subscription | None = None
    #: The lease this claim took, present only on ``ready``. Carried back to the terminal write so a
    #: holder whose lease has lapsed and been re-claimed cannot resolve a delivery it no longer
    #: holds. Never persisted anywhere but the delivery document, and never logged: it is the only
    #: thing that distinguishes two workers, so it is a capability for exactly one write.
    token: str | None = None
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
    transaction: Any,
    refs: _Refs,
    *,
    delivery_id: str,
    token: str,
    lease_s: float,
    now: datetime,
) -> tuple[str, dict[str, Any], dict[str, Any], int, str | None]:
    """Recheck eligibility and take the lease, or refuse because somebody else holds it.

    ``token`` is minted by the CALLER, outside the transaction, for the reason every clock here is
    read outside one: a body may run more than once, and a token minted inside would differ between
    attempts, so the retry would write a lease its own caller cannot later prove it holds.

    **The lease is what makes a second worker safe** (KTD14). Until U11 there is exactly one --
    ``claimed_at`` was written and read by nothing -- so this is unreachable in production today and
    is pinned against the fake anyway: a check that arrives with the second replica arrives after the
    duplicate notifications it exists to prevent.
    """
    delivery = _doc(_read(transaction, refs.delivery(delivery_id)))
    if not delivery or delivery.get("state") != "pending" or not _not_expired(delivery, now):
        return "gone", delivery, {}, 0, None
    held_until = _as_dt(delivery.get("claimed_until"))
    if held_until is not None and held_until > now:
        # Somebody else is mid-attempt. NOT an error and NOT a terminal state: the delivery is
        # still `pending` and the holder is about to resolve it, or its lease lapses and the next
        # pass takes it. Writing anything here would be this reader's opinion about another
        # worker's in-flight attempt.
        return "leased", delivery, {}, 0, "lease"
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
    transaction.update(
        refs.delivery(delivery_id),
        # `claimed_until` and `claim_token` REPLACE the write-only `claimed_at`, which nothing read.
        # The token is what the terminal writes compare against, so a holder whose lease lapsed
        # cannot resolve a delivery another worker has since re-claimed.
        {
            "claimed_at": now,
            "claimed_until": now + timedelta(seconds=lease_s),
            "claim_token": token,
            "updated_at": now,
        },
    )
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


def claim_delivery(
    delivery_id: str, *, lease_s: float = DELIVERY_LEASE_S, now: datetime | None = None
) -> DeliveryClaim:
    """Recheck eligibility immediately before dispatch, durably (R13).

    Called right before the outbound attempt, never at the top of the pass: the pending set was read
    at the start of the cycle and a delete, an expiry or a version bump can land in between. A
    claim that finds one writes the terminal state itself, so nothing re-offers the delivery.
    """
    instant = now or _now()
    refs = _Refs(_firestore_client())
    # Minted HERE, outside the body, exactly as `now` is: a body may run more than once and a token
    # minted inside would differ between attempts. `token_hex` and not a counter or a hash of the
    # id -- two workers must not be able to derive each other's token, or the compare proves
    # nothing.
    token = secrets.token_hex(8)
    status, delivery, subscription, remaining, reason = _run_transaction(
        lambda txn: _claim_body(
            txn, refs, delivery_id=delivery_id, token=token, lease_s=lease_s, now=instant
        )
    )
    return DeliveryClaim(
        status=status,
        delivery=_delivery_from_doc(delivery) if delivery else None,
        subscription=_subscription_from_doc(subscription) if subscription else None,
        cap_remaining=remaining,
        reason=reason,
        token=token if status == "ready" else None,
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
    #: The delivery is still ``pending`` but is held by a DIFFERENT claim than the one this write
    #: belongs to, so nothing was written (U8). A third outcome rather than a flavour of
    #: ``already_resolved``: the delivery is not resolved, it is somebody else's, and the caller
    #: must not count it as a send or a failure. Unreachable while there is one writer; it is what
    #: makes a second replica safe rather than what reports on one.
    lease_lost: bool = False


def _sent_body(
    transaction: Any, refs: _Refs, *, delivery_id: str, token: str | None, now: datetime
) -> tuple[dict[str, Any], bool, bool, bool]:
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

    **And conditional on the LEASE since U8** (KTD14). A delivery can be `pending` and still not be
    this caller's to resolve: its lease lapsed, another worker re-claimed it, and this write would
    otherwise mark sent an attempt whose outcome nobody knows -- spending R11's slot for a
    notification that may never have arrived, and clearing a pending delivery the live holder is
    still working on. ``token`` is the one the claim returned; ``None`` means the caller is not
    using leases and the compare is skipped, which is what keeps every existing suite's direct call
    meaningful. The lease is cleared here on success, so a resolved delivery holds no stale hold.
    """
    delivery = _doc(_read(transaction, refs.delivery(delivery_id)))
    if not delivery:
        raise NotFound("no such delivery")
    if delivery.get("state") != "pending":
        return delivery, False, True, False
    if token is not None and str(delivery.get("claim_token") or "") != token:
        return delivery, False, False, True
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
        # The hold goes with the resolution. Left behind, a `sent` record would carry a lease that
        # means nothing and an operator reading the document could not tell it from a live one.
        "claimed_until": None,
        "claim_token": None,
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
        # PER TRAIN, keyed on the `alert_id`, never per situation number (2026-09-10). Events are
        # minted per (subscription, train), so the question `varsel._pending_pushes` asks of these
        # marks -- "is a notification about this train still coming?" -- is a per-train question: a
        # second departure under an already-delivered situation is a NEW event, and a per-situation
        # mark admitted it to the wording pass never, so its event carried the template while the
        # first train's carried the model's prose.
        if alert_id:
            marks[alert_id] = now + timedelta(hours=DELIVERED_MARK_TTL_H)
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
        # NOT MIRRORED, and `_mirror_subject` carries the reasoning: none of these three fields is
        # in the standing set the emit pass reads, so recording them would put the busiest
        # documents in the hottest loop for no reader. The phase is untouched here by construction
        # -- a delivery is not a lifecycle transition.
        transaction.update(refs.subject(alert_id), subject_patch)
    return {**delivery, **delivery_patch}, spent, False, False


def mark_delivery_sent(
    delivery_id: str, *, token: str | None = None, now: datetime | None = None
) -> DeliveryOutcome:
    """Record that the client received this event. Idempotent, and it spends R11's slot once.

    ``state`` is read back from the document rather than asserted to be ``"sent"``: a delivery some
    other pass already resolved keeps the state it holds, and claiming ``"sent"`` for a
    ``superseded`` or ``cancelled`` record would misreport it to whoever reads the outcome.
    """
    instant = now or _now()
    refs = _Refs(_firestore_client())
    doc, spent, already_resolved, lease_lost = _run_transaction(
        lambda txn: _sent_body(txn, refs, delivery_id=delivery_id, token=token, now=instant)
    )
    return DeliveryOutcome(
        delivery_id=delivery_id,
        state=str(doc.get("state") or ""),
        attempts=int(doc.get("attempts") or 0),
        slot_spent=spent,
        already_resolved=already_resolved,
        lease_lost=lease_lost,
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
    token: str | None,
    now: datetime,
) -> tuple[dict[str, Any], bool, bool]:
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
        return delivery, True, False
    # And conditional on the LEASE, for `_sent_body`'s reason mirrored: a lapsed holder writing a
    # failure here would set `retry_not_before` on a delivery the live holder is mid-POST with, and
    # release a cap slot that claim is still using.
    if token is not None and str(delivery.get("claim_token") or "") != token:
        return delivery, False, True
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
        # Released with the outcome, on both branches: a retryable failure leaves the delivery
        # `pending` for the next cycle, and a lease outliving the attempt that took it would make
        # the next cycle refuse its own retry.
        "claimed_until": None,
        "claim_token": None,
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
    return {**delivery, **patch}, False, False


def mark_delivery_failed(
    delivery_id: str,
    *,
    error: str,
    retryable: bool,
    retry_after_s: float | None = None,
    terminal_state: str = "failed",
    token: str | None = None,
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
    doc, already_resolved, lease_lost = _run_transaction(
        lambda txn: _failed_body(
            txn,
            refs,
            delivery_id=delivery_id,
            error=error,
            retryable=retryable,
            retry_after_s=retry_after_s,
            terminal_state=terminal_state,
            token=token,
            now=instant,
        )
    )
    return DeliveryOutcome(
        delivery_id=delivery_id,
        state=str(doc.get("state") or ""),
        attempts=int(doc.get("attempts") or 0),
        retry_not_before=_as_dt(doc.get("retry_not_before")),
        already_resolved=already_resolved,
        lease_lost=lease_lost,
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
    "CHANNELS",
    "CHANNEL_WEBHOOK",
    "CHANNEL_WEB_PUSH",
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
    "MirrorEntry",
    "MirrorSync",
    "NotConfigured",
    "NotFound",
    "NotificationStoreError",
    "PendingDeliveries",
    "QuerySpec",
    "QuotaExceeded",
    "StandingSubject",
    "SubjectMirror",
    "Subscription",
    "SubscriptionMirror",
    "SubscriptionSpec",
    "TrainRef",
    "alert_subject_id",
    "cap_remaining",
    "claim_delivery",
    "cleanup_subscription",
    "clear_below_threshold",
    "client_alerts",
    "commit_event",
    "configure",
    "create_subscription",
    "daily_cap_of",
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
