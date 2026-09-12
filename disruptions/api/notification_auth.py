"""Client identity: accounts, API-key credentials, callback destinations and the operator audit.

The **identity plane** for the webhook channel. ``notification_store`` is the data plane --
subscriptions, alert subjects, events, deliveries -- and deliberately knows nothing about who is
allowed to write one. This module answers three questions the data plane must not:

===========================  ===============================================================
``clients``         one per client: a name and a suspension switch
``credentials``      one per ``key-id``: the peppered secret hash, active or revoked
``destinations``     one per callback: the URL, the live signing generations, PAUSED
``admin_audit``      append-only: every operator action, refusals included (R22)
===========================  ===============================================================

Four things here are decisions rather than mechanics, and each has a failure that is silent when
it is got backwards.

1. **The secret is hashed with HMAC-SHA256 under a pepper, never bcrypt or Argon2** (KTD8). Those
   are sized for low-entropy human passwords, and a measured ~100 ms per comparison would be paid
   on *every* client request for no gain at all against a 256-bit random secret. The lookup is by
   ``key-id``, so the secret never appears in a query; the comparison is
   :func:`hmac.compare_digest`; the log carries the ``key-id`` and never the secret.
2. **The limiter is two-tier, because verifying a credential is itself store work.** A global
   pre-lookup ceiling plus a short-TTL negative cache of unknown ``key-id``s means a repeated bad
   credential costs no Firestore read at all; a per-client ceiling then applies *after*
   authentication, so anonymous traffic cannot spend an authenticated client's budget. An
   anonymous flood can still exhaust the global tier, which is the accepted trade ``/impact``'s
   limiter already makes.
3. **The destination record carries a ``paused`` flag, and that flag is the whole reason it
   exists.** ``webhook_sender`` classifies a 410 as ``pause_destination=True`` and
   ``notification_store.mark_delivery_failed`` records only what happened to the *delivery* -- R12's "a
   destination is paused on 410 until an operator resumes it" has nowhere else to live. U6 reads
   :func:`destination_for_delivery` before attempting a destination and calls
   :func:`pause_destination` when the attempt says so.
4. **The audit is append-only and no client credential can read it.** Enforced structurally rather
   than by a rule: ``notification_api`` never names :data:`ADMIN_AUDIT`, so no request path can project
   it however the code above drifts -- the same argument ``meldinger_decisions.sql`` makes about
   ``message_input``, and ``test_notification_auth`` greps for it in both directions. Firestore has no
   per-collection IAM (KTD5 records the limit), so a projection argument is the only one available.

**One Firestore client per process, and it is ``notification_store``'s.** These four collections live in
the same named database as the subscriptions, so :func:`notification_store.firestore_client` is called rather than
a second client built here: two clients means two gRPC channels and two connection pools in a
single-replica deployment whose poller already shares the thread, for no gain at all. It also keeps
**one place deciding which database is opened** -- this module takes no database argument, so
``notification_store.configure(database=...)`` is the only such decision in the process. Still lazy:
nothing imports the library or needs a credential until a request arrives.

Everything reachable from a route goes through :class:`_Docs`, which is what makes the whole surface
testable with no emulator and no credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import notification_store
import webhook_sender

logger = logging.getLogger(__name__)

# ---- Collections ------------------------------------------------------------------------------

ACCOUNTS = "clients"
CREDENTIALS = "credentials"
DESTINATIONS = "destinations"
ADMIN_AUDIT = "admin_audit"

COLLECTIONS = (ACCOUNTS, CREDENTIALS, DESTINATIONS, ADMIN_AUDIT)

# Every query in this module is a single equality filter on one field, which Firestore's automatic
# single-field indexes serve. Deliberately: a composite index is a runtime `FailedPrecondition` the
# emulator cannot catch, it has to be declared in `firestore.indexes.json` AND materialised in
# `terraform/firestore.tf`, and nothing this module lists is large enough to need one -- a client
# holds one destination and a handful of credentials. Ordering and secondary filtering happen in
# Python, over sets bounded by `_MAX_*` below.
MAX_CREDENTIALS_PER_CLIENT = 8
MAX_DESTINATIONS_PER_CLIENT = 4

# ---- Credential shape -------------------------------------------------------------------------

#: The `key-id` prefix. Greppable on purpose, exactly as PostHog's `phc_` is in `feedback.py`: a
#: value that turns up in a log, a ticket or a config file should be identifiable as a kix client
#: key id at a glance, and the prefix is also what lets `_parse_bearer` refuse a malformed header
#: before spending a store read on it.
KEY_ID_PREFIX = "kixn_"
_KEY_ID_BYTES = 10
KEY_ID_RE = re.compile(r"^kixn_[0-9a-f]{20}$")
#: 32 bytes, urlsafe-base64 without padding: 43 characters, 256 bits. The number KTD8's argument
#: against bcrypt rests on -- against this much entropy a work factor buys nothing.
_SECRET_BYTES = 32
#: The credential a client sends, as one string: `<key-id>.<secret>`. One token to copy, and the
#: separator cannot occur in either half (the id is `kixn_` plus hex, the secret is base64url).
_BEARER_SEPARATOR = "."

PEPPER_ENV = "NOTIFICATION_KEY_PEPPER"
#: A pepper shorter than one HMAC block's worth of entropy is a provisioning mistake rather than a
#: policy choice, and refusing it is the only moment anybody can notice. Same bar, same reasoning
#: and same failure direction as `webhook_signing.MIN_ROOT_BYTES`.
MIN_PEPPER_BYTES = 32
_HASH_PURPOSE = "kix.client.key.v1"

_ACCOUNT_ACTIVE = "active"
_ACCOUNT_SUSPENDED = "suspended"
_CREDENTIAL_ACTIVE = "active"
_CREDENTIAL_REVOKED = "revoked"
_DESTINATION_ACTIVE = "active"
#: The two a destination can be in. `retired` exists for a destination an operator has taken out of
#: service without deleting it -- the record is the audit trail for every delivery signed under its
#: generations -- and is deliberately distinct from `paused`, which is automatic and reversible.
DESTINATION_STATUSES = (_DESTINATION_ACTIVE, "retired")

CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
#: Matches `subscription.schema.json`'s `subscription_id` pattern, and the same charset serves a
#: destination id. Neither may contain `~`, which is what makes `notification_api`'s composite document
#: id (`<client>~<subscription>`) collision-free rather than merely unlikely.
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# ---- Limiter numbers, every one a number so it can be checked ---------------------------------

_WINDOW_S = 60
#: Tier one, spent BEFORE the credential lookup. Sized well above any pilot client's real traffic
#: (a BFF registers a commuter once and then polls rarely) and well below what would make a
#: Firestore read storm out of an anonymous flood. Exhausting it is the accepted trade named in the
#: module docstring: anonymous traffic can deny the whole surface, which is what `/impact`'s
#: limiter already accepts, and the alternative -- no pre-lookup tier -- makes every unknown key id
#: a billable read.
GLOBAL_PRELOOKUP_PER_MIN = 300
#: Tier two, spent AFTER authentication and before any write. This is what stops one client (or
#: one leaked credential) spending the surface, and it is per client rather than per key id
#: deliberately: rotation issues a second live credential (R1/KTD8), and a per-key budget would
#: silently double a client's allowance for the length of every cutover.
PER_CLIENT_PER_MIN = 120
#: How long an unknown or revoked `key-id` is remembered as bad. Short, because it is a negative
#: cache on a security decision: 60 s bounds how long a freshly issued credential can be refused by
#: a stale entry, which is the one way this can be wrong in the client's direction. A WRONG SECRET
#: for a known key id is deliberately NOT cached -- caching it would let one bad attempt mask the
#: right secret for a minute.
_UNKNOWN_TTL_S = 60
_UNKNOWN_MAX = 10_000
#: Accounts are read on every request, so the verdict is cached for one window. Suspension is
#: therefore effective within a minute rather than instantly, which is the right trade for a
#: control an operator uses deliberately; a revoked CREDENTIAL is not cached this way and takes
#: effect on the next request, because that is the lever used in a hurry.
_ACCOUNT_TTL_S = 60
_ACCOUNT_MAX = 1_000
#: `Retry-After` on a limiter rejection. Bounded, and bounded small: the window is 60 s, so telling
#: a caller to come back later than that would be a longer outage than the limiter causes.
RETRY_AFTER_S = 60

# ---- Per-client quotas ------------------------------------------------------------------------
#
# Three ceilings that were module constants until 2026-09-09 and are now DEFAULTS: every client
# account carries its own values, read on each authenticated request through the same cached
# account lookup that answers "is this client suspended?", so a raised quota costs no extra read.
# The first consumer is the Entur app and the second a trial operator, one number cannot fit
# both, and a constant published in the contract becomes the number a client builds around. The
# bounds are why the fields are not simply "any positive integer":
#
#   max_active_subscriptions  bounded at 200 000 since U7, which is the number that work was
#                             waiting on rather than a new judgement. It was 10 000 because
#                             `notification_store._create_body` COUNTED the client's live
#                             subscriptions by READING them inside the create transaction, up to
#                             the ceiling -- a bounded read at 500 and a 10 000-document read under
#                             pessimistic locks on every create at the bound, which is why the
#                             comment here said raising it without the aggregation "only moves the
#                             cost onto every create the client makes". The count is now a
#                             `count()` over the `expires_at` inequality the `subscription_count`
#                             spec declares, billed at roughly one read per thousand index entries
#                             and taking no per-document lock, so the reason for the old bound is
#                             gone. 200 000 is R1's population with room above it, not a capacity
#                             measurement: what one client may actually hold is still decided by
#                             the pass measurements this plan's harness takes.
#   requests_per_minute       bounded at 10 000. Tier two of KTD8's limiter, spent after
#                             authentication; tier one (`GLOBAL_PRELOOKUP_PER_MIN`) is unchanged and
#                             is what bounds the credential lookup an anonymous flood can reach.
#   daily_cap                 bounded at 1 000, which is "effectively uncapped" for a client that
#                             registers one subscription per ROUTE and enforces per-user caps on
#                             its own side. It is copied onto each subscription document at
#                             create, replace and touch (`notification_store.cap_remaining` reads
#                             it there, so the delivery pass needs no second lookup), which means a
#                             change reaches a subscription the next time the client writes it --
#                             the idempotent re-PUT included.
QUOTA_BOUNDS: dict[str, tuple[int, int]] = {
    "max_active_subscriptions": (1, 200_000),
    "requests_per_minute": (1, 10_000),
    "daily_cap": (1, 1_000),
}


@dataclass(frozen=True)
class Quotas:
    """One client's ceilings. The defaults are the constants the pilot was sized with."""

    max_active_subscriptions: int = notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT
    requests_per_minute: int = PER_CLIENT_PER_MIN
    daily_cap: int = notification_store.MAX_ALERT_DELIVERIES_PER_DAY

    def validate(self) -> None:
        for name, (low, high) in QUOTA_BOUNDS.items():
            value = getattr(self, name)
            if not _is_int(value) or not low <= value <= high:
                raise Conflict(f"{name} must be an integer between {low} and {high}")

    def as_fields(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in QUOTA_BOUNDS}


DEFAULT_QUOTAS = Quotas()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _quotas_from_doc(doc: Mapping[str, Any]) -> Quotas:
    """A stored account's quotas. A missing, unreadable or out-of-bounds field is the DEFAULT.

    Default rather than refuse, in both directions: accounts written before the fields existed
    carry none, and a hand-repaired document with a bad value must not lock its client out of a
    surface the default would have served. Out-of-bounds reads as the default too, because the
    bounds exist to protect the create transaction (see :data:`QUOTA_BOUNDS`) and a value that
    bypassed :meth:`Quotas.validate` must not bypass the reason for it.
    """
    values: dict[str, int] = {}
    for name, default in DEFAULT_QUOTAS.as_fields().items():
        raw = doc.get(name)
        low, high = QUOTA_BOUNDS[name]
        values[name] = int(raw) if _is_int(raw) and low <= raw <= high else default
    return Quotas(**values)


# ---- Errors -----------------------------------------------------------------------------------


class ClientAuthError(Exception):
    """Base for every failure this module raises deliberately."""


class NotConfigured(ClientAuthError):
    """No store, or no usable pepper: this deployment cannot verify a credential at all.

    Distinct from :class:`AuthFailed` because the honest answer to a machine caller is 503, not
    401. A 401 would tell a client their key is wrong when it is the deployment that is.
    """


class AuthFailed(ClientAuthError):
    """The credential is missing, malformed, unknown, revoked, expired or wrong. 401.

    Carries a ``reason`` for the log and never for the response body: "unknown key id" and "wrong
    secret" are different facts about somebody else's credential, and telling them apart over the
    wire is an oracle.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AuthSuspended(ClientAuthError):
    """The credential is good and the client is suspended. 403, deliberately not 401.

    A client must be able to tell "my key is wrong" from "we have been switched off": the first is
    theirs to fix by rotating, the second is a conversation with an operator, and one status code
    for both sends every suspended client into a credential hunt.
    """


class RateLimited(ClientAuthError):
    """A limiter tier rejected the request. 429 with a bounded ``Retry-After``."""

    def __init__(self, tier: str, retry_after_s: int = RETRY_AFTER_S) -> None:
        super().__init__(tier)
        self.tier = tier
        self.retry_after_s = retry_after_s


class Forbidden(ClientAuthError):
    """The record exists and belongs to another client.

    Distinct from "missing" INSIDE this module and collapsed into it at the API edge, exactly as
    ``notification_store.Forbidden`` is: the caller needs to tell a tenancy violation from a typo in
    order to log the former, while a client must not be able to probe another's destination ids by
    the difference between a 403 and a 404.
    """


class Conflict(ClientAuthError):
    """A validation failure, a duplicate id, or a state the transition does not allow."""


class NotFound(ClientAuthError):
    """No such account, credential or destination."""


# ---- The document seam ------------------------------------------------------------------------


class _Docs(Protocol):
    """The four operations this module needs from a document store.

    A seam rather than a Firestore client, for two reasons that are both about what can be checked.
    Every route-reachable path goes through it, so ``test_notification_api`` runs the real router against
    an in-memory implementation with no emulator, no socket and no credentials -- and the emulator
    is explicit that it "uses a simple locking approach" and mirrors neither production concurrency
    nor its indexes, so an emulator test would not have been the stronger check it looks like. And
    the four operations are deliberately the *only* four: there is no ``delete``, because this
    module's audit collection is append-only and its records are never removed, and there is no
    range filter or ordering, because every query here is one equality on one field (see
    :data:`COLLECTIONS`).
    """

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...

    def set(self, collection: str, doc_id: str, doc: Mapping[str, Any]) -> None: ...

    def update(self, collection: str, doc_id: str, patch: Mapping[str, Any]) -> None: ...

    def list_by(
        self, collection: str, field: str, value: Any, *, limit: int
    ) -> list[dict[str, Any]]: ...


class _FirestoreDocs:
    """:class:`_Docs` over the process's one Firestore client.

    The client is ``notification_store``'s (:func:`notification_store.firestore_client`), built lazily on first use --
    so a deployment with the channel off neither imports the library nor holds a credential for it,
    and there is exactly one gRPC channel to the one database. The PROJECT is never an argument: it
    comes from the ambient environment, which is what R1's environment isolation rests on.

    Stateless, so one module-level instance serves every request; the client behind it is resolved
    per call rather than captured, which is what lets a test reconfigure ``notification_store`` between
    checks without leaving a stale channel behind.

    **Every one of the four carries the data plane's own per-call deadline**
    (``notification_store._CALL_DEADLINE_S``), and none of them did until 2026-09-09. That is not
    symmetry for its own sake -- it is a sharper requirement here than in ``notification_store``, whose
    calls run on the poller thread inside an outer bounded wait. These run on a REQUEST thread: the
    lookup in :meth:`get` is the credential read :func:`authenticate` performs on every
    authenticated request, from a plain ``def`` route sharing the ~40-slot anyio worker pool with
    ``/varsel-api``'s registration writes and ``/impact``. With no deadline, a Firestore stall parks
    one of those shared workers per in-flight client request until gRPC gives up on its own, and
    the limiters here cap requests per window rather than workers held -- so a traveller's
    registration queues behind a client's stalled credential lookup. There is no outer wait on this
    plane to fall back on, which is exactly why the bound has to belong to the call.
    """

    def _c(self) -> Any:
        return notification_store.firestore_client()

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snapshot = (
            self._c()
            .collection(collection)
            .document(doc_id)
            .get(timeout=notification_store._CALL_DEADLINE_S)
        )
        if not getattr(snapshot, "exists", False):
            return None
        return dict(snapshot.to_dict() or {})

    def set(self, collection: str, doc_id: str, doc: Mapping[str, Any]) -> None:
        self._c().collection(collection).document(doc_id).set(
            dict(doc), timeout=notification_store._CALL_DEADLINE_S
        )

    def update(self, collection: str, doc_id: str, patch: Mapping[str, Any]) -> None:
        self._c().collection(collection).document(doc_id).update(
            dict(patch), timeout=notification_store._CALL_DEADLINE_S
        )

    def list_by(
        self, collection: str, field: str, value: Any, *, limit: int
    ) -> list[dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = (
            self._c()
            .collection(collection)
            .where(filter=FieldFilter(field, "==", value))
            .limit(limit)
        )
        return [
            dict(s.to_dict() or {})
            for s in query.stream(timeout=notification_store._CALL_DEADLINE_S)
        ]


# ---- Configuration ---------------------------------------------------------------------------

_docs: _Docs | None = None
_clock: Callable[[], datetime] = lambda: datetime.now(UTC)
_lock = threading.Lock()
_global_calls: deque[float] = deque()
_client_calls: dict[str, deque[float]] = {}
_unknown: dict[str, float] = {}
_accounts: dict[str, tuple[float, str | None, Quotas]] = {}
_WHITESPACE_REPORTED: set[str] = set()


#: The production seam: stateless, so one instance is enough, and it resolves the shared client per
#: call. Used whenever no seam has been injected and ``notification_store`` has been configured.
_SHARED_DOCS = _FirestoreDocs()


def configure(
    *,
    docs: _Docs | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Wire the identity plane. Production passes nothing; the suites pass a ready-made seam.

    **No database argument, deliberately.** These collections live in the database
    ``notification_store.configure(database=...)`` opened, so naming it twice would be two places to get
    wrong -- and a mismatch is invisible in the worst way, because the request path is guarded and
    would simply answer 503 while looking exactly like a healthy deployment with no client
    provisioned.

    ``clock`` is injectable for the same reason ``notification_store``'s is -- every write takes its
    instant once, at the top, so two records written by one action cannot disagree about when it
    happened.
    """
    global _docs, _clock
    _docs = docs
    _clock = clock or (lambda: datetime.now(UTC))
    reset_limiters()


def reset_limiters() -> None:
    """Forget every in-process ledger. Called by :func:`configure` and by the suites.

    A leaked half-spent budget or a stale negative-cache entry is indistinguishable from a bug in
    the thing it bounds, which is why ``varsel``'s own caches are registered in two places; here
    there is one function and both callers use it.
    """
    with _lock:
        _global_calls.clear()
        _client_calls.clear()
        _unknown.clear()
        _accounts.clear()


def reset_for_tests() -> None:
    configure(docs=None, clock=None)
    _WHITESPACE_REPORTED.clear()


def store_ready() -> bool:
    """Is there a store to read? An injected seam, or a configured ``notification_store``."""
    return _docs is not None or notification_store.enabled()


def _store() -> _Docs:
    if _docs is not None:
        return _docs
    if not notification_store.enabled():
        raise NotConfigured(
            "notification_store.configure() was never called with a database id, so the client "
            "identity plane has nothing to read"
        )
    return _SHARED_DOCS


def _now() -> datetime:
    now = _clock()
    return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)


def _env(name: str) -> str:
    """The variable, stripped, warning once per process when stripping changed it.

    ``feedback._env``'s shape and ``feedback._env``'s reason, restated here because the failure is
    worse: a trailing newline on the pepper re-hashes every credential, so every client's key
    stops verifying at once and nothing on our side reports a configuration fault -- just a wall of
    401s that reads exactly like a client integration bug.
    """
    raw = os.environ.get(name) or ""
    value = raw.strip()
    if value != raw and name not in _WHITESPACE_REPORTED:
        _WHITESPACE_REPORTED.add(name)
        logger.warning(
            "%s carried surrounding whitespace and was stripped before use; fix it at the source "
            "(a Secret Manager payload written with `echo` rather than `printf '%%s'` is the usual "
            "cause). Unstripped, it re-hashes every credential and every client's key stops "
            "verifying at once.",
            name,
            extra={"fields": {"event": "notification_auth_config", "variable": name}},
        )
    return value


def pepper_ready() -> bool:
    """Is there a usable pepper? A cheap probe that never raises and never logs the value."""
    return len(_env(PEPPER_ENV).encode()) >= MIN_PEPPER_BYTES


def _pepper() -> bytes:
    value = _env(PEPPER_ENV).encode()
    if len(value) < MIN_PEPPER_BYTES:
        raise NotConfigured(
            f"{PEPPER_ENV} is unset or shorter than {MIN_PEPPER_BYTES} bytes; client credentials "
            "cannot be verified. Provision it in Secret Manager and uncomment the chart's "
            "`notifications:` secret group."
        )
    return value


def available() -> bool:
    """Can this deployment verify a credential at all? Store AND pepper, read at call time."""
    return store_ready() and pepper_ready()


# ---- Hashing ----------------------------------------------------------------------------------


def secret_hash(key_id: str, secret: str) -> str:
    """HMAC-SHA256 of the secret under the pepper, purpose-tagged and bound to its ``key-id``.

    Binding the id into the preimage is not decoration: without it a hash lifted from one
    credential document verifies under any other ``key-id`` it is pasted into, so a single stolen
    row would authenticate as every client whose row it replaced. Purpose-tagged for
    ``sso._pack``'s reason -- one pepper, disjoint messages.
    """
    material = f"{_HASH_PURPOSE}|{key_id}|{secret}".encode()
    return hmac.new(_pepper(), material, hashlib.sha256).hexdigest()


def mint_credential() -> tuple[str, str]:
    """A fresh ``(key_id, secret)``. The secret is returned once and never stored in the clear."""
    return (
        KEY_ID_PREFIX + secrets.token_bytes(_KEY_ID_BYTES).hex(),
        secrets.token_urlsafe(_SECRET_BYTES),
    )


def bearer_credential(key_id: str, secret: str) -> str:
    """The one string a client configures: ``<key-id>.<secret>``."""
    return f"{key_id}{_BEARER_SEPARATOR}{secret}"


def _parse_bearer(header: str | None) -> tuple[str, str]:
    """``(key_id, secret)`` from an ``Authorization`` header, or raise :class:`AuthFailed`.

    Deliberately the cheapest thing in the whole path and deliberately BEFORE the global limiter
    tier: a flood of garbage headers then costs a regex and nothing else, so it cannot spend the
    budget that protects the credential lookup. Only a well-formed unknown ``key-id`` reaches the
    tier it is designed to bound.
    """
    if not header:
        raise AuthFailed("missing")
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer" or not rest.strip():
        raise AuthFailed("malformed")
    key_id, sep, secret = rest.strip().partition(_BEARER_SEPARATOR)
    if not sep or not secret or not KEY_ID_RE.match(key_id):
        raise AuthFailed("malformed")
    return key_id, secret


# ---- The limiter ------------------------------------------------------------------------------


def claim_window(
    bucket: deque[float], ceiling: int, now: float, *, window_s: float = _WINDOW_S
) -> bool:
    """Spend one slot in a trailing window, or refuse. The caller owns the bucket and the lock.

    Public and window-parameterised because ``notification_api._claim_test_event`` wrote the same three
    lines again for the same feature -- a sliding window is exactly the kind of small algorithm that
    is easy to write and easy to write *slightly* differently, and the difference between ``>`` and
    ``>=`` here is a limit that admits one extra call per window forever.

    ``window_s`` defaults to this module's own minute so both credential tiers keep reading as they
    did; the test-event route passes its own, which happens to be the same number under a different
    name and must stay free to change.
    """
    while bucket and now - bucket[0] >= window_s:
        bucket.popleft()
    if len(bucket) >= ceiling:
        return False
    bucket.append(now)
    return True


def _claim_global(now: float) -> None:
    with _lock:
        if not claim_window(_global_calls, GLOBAL_PRELOOKUP_PER_MIN, now):
            raise RateLimited("global")


def claim_client_slot(
    client_id: str, *, limit: int | None = None, now: float | None = None
) -> None:
    """Tier two: spend one of this client's per-minute slots, or raise :class:`RateLimited`.

    Public because ``notification_api`` spends it AFTER authentication and BEFORE any store write, which
    is what approach 4's "reject excess before creating anything" means. Keyed on the client, not
    the key id -- see :data:`PER_CLIENT_PER_MIN`, which is the default ``limit``; the router passes
    the client's own (:attr:`Principal.quotas`).
    """
    instant = _monotonic() if now is None else now
    ceiling = PER_CLIENT_PER_MIN if limit is None else limit
    with _lock:
        bucket = _client_calls.setdefault(client_id, deque())
        if not claim_window(bucket, ceiling, instant):
            raise RateLimited("client")
        # Bounded for the reason `feedback._claim` bounds its own per-context map: the key is
        # client-controlled only in the sense that an operator creates it, but a pod that never
        # restarts must not accumulate dead buckets. Drop the emptiest first is not worth the scan;
        # dropping insertion-oldest is what `feedback` does and is enough.
        while len(_client_calls) > 1_000:
            _client_calls.pop(next(iter(_client_calls)))


def _monotonic() -> float:
    # Its own function so the suites can freeze it without touching `time` globally. Monotonic and
    # never wall clock: a limiter window measured against a clock that steps backwards over NTP
    # would hand out a fresh allowance.
    import time

    return time.monotonic()


def _remember_unknown(key_id: str, now: float) -> None:
    with _lock:
        _unknown[key_id] = now
        while len(_unknown) > _UNKNOWN_MAX:
            _unknown.pop(next(iter(_unknown)))


def _is_known_bad(key_id: str, now: float) -> bool:
    with _lock:
        at = _unknown.get(key_id)
        if at is None:
            return False
        if now - at >= _UNKNOWN_TTL_S:
            _unknown.pop(key_id, None)
            return False
        return True


# ---- Authentication ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Who is calling. The ONLY source of ownership for every record this API touches.

    ``client_id`` is taken from here and never from a request body: a client may manage only its
    own subscriptions, and accepting the field from the caller would make that a request-shaped
    choice rather than a property of the credential.
    """

    client_id: str
    key_id: str
    quotas: Quotas = DEFAULT_QUOTAS


def authenticate(header: str | None) -> Principal:
    """Verify a bearer credential. The router's own dependency calls this once per request (KTD8).

    The order of the seven steps is the design, not an accident, and each step is placed where it
    is because of what the step after it costs:

    1. **Availability** -- no store or no pepper is a 503 about this deployment, never a 401 about
       the caller's key.
    2. **Parse** -- free, so a malformed header spends no budget.
    3. **Negative cache** -- an unknown or revoked ``key-id`` seen in the last minute costs no
       Firestore read.
    4. **Global tier** -- spent before the lookup, which is the read it exists to bound.
    5. **Lookup by ``key-id``** -- so the secret never appears in a query.
    6. **Compare** -- ``hmac.compare_digest``, and a wrong secret is NOT cached (that would let one
       bad attempt mask the right secret for a minute).
    7. **Account** -- a suspended client is 403, not 401, so they can tell the two apart.

    The per-client tier is deliberately NOT spent here: it belongs after authentication and
    immediately before a write, which is `notification_api`'s :func:`claim_client_slot` call.
    """
    if not available():
        raise NotConfigured(
            "the webhook channel is not configured in this deployment"
        )
    key_id, secret = _parse_bearer(header)
    now = _monotonic()
    if _is_known_bad(key_id, now):
        raise AuthFailed("unknown_cached")
    _claim_global(now)
    stored = _store().get(CREDENTIALS, key_id)
    if not stored or stored.get("status") != _CREDENTIAL_ACTIVE:
        _remember_unknown(key_id, now)
        _log_refusal(key_id, "unknown" if not stored else "revoked")
        raise AuthFailed("unknown" if not stored else "revoked")
    expected = str(stored.get("secret_hash") or "")
    if not expected or not hmac.compare_digest(expected, secret_hash(key_id, secret)):
        _log_refusal(key_id, "secret")
        raise AuthFailed("secret")
    expires = _as_dt(stored.get("expires_at"))
    if expires is not None and expires <= _now():
        _log_refusal(key_id, "expired")
        raise AuthFailed("expired")
    client_id = str(stored.get("client_id") or "")
    status, quotas = _account_state(client_id, now)
    if status is None:
        _log_refusal(key_id, "no_account")
        raise AuthFailed("no_account")
    if status != _ACCOUNT_ACTIVE:
        _log_refusal(key_id, "suspended")
        raise AuthSuspended(client_id)
    return Principal(client_id=client_id, key_id=key_id, quotas=quotas)


def _log_refusal(key_id: str, reason: str) -> None:
    """One structured line per refusal, carrying the ``key-id`` and never the secret (KTD8/R16).

    ``key_id`` is an identifier an operator can act on -- revoke it, ask whose it is -- and it is
    the only half of the credential that is safe to write down. The secret is not logged anywhere
    in this module, at any level, and ``test_notification_auth`` captures logging around a failed
    authentication to pin exactly that.
    """
    logger.warning(
        "Client credential refused: %s",
        reason,
        extra={
            "fields": {
                "event": "notification_auth",
                "result": "refused",
                "reason": reason,
                "key_id": key_id,
            }
        },
    )


def _account_state(client_id: str, now: float) -> tuple[str | None, Quotas]:
    """The account's status and quotas, cached together for one limiter window.

    Status ``None`` when there is no account. One cache entry carries both, so a client's quotas
    ride on every :class:`Principal` for the price of the status read this already paid.
    """
    if not client_id:
        return None, DEFAULT_QUOTAS
    with _lock:
        hit = _accounts.get(client_id)
        if hit is not None and now - hit[0] < _ACCOUNT_TTL_S:
            return hit[1], hit[2]
    stored = _store().get(ACCOUNTS, client_id)
    status = str(stored.get("status") or "") if stored else None
    quotas = _quotas_from_doc(stored) if stored else DEFAULT_QUOTAS
    with _lock:
        while len(_accounts) >= _ACCOUNT_MAX:
            _accounts.pop(next(iter(_accounts)))
        _accounts[client_id] = (now, status, quotas)
    return status, quotas


# The store's own coercion, not a copy of it: both modules read documents the same three writers
# produce (the client's ``DatetimeWithNanoseconds``, a plain possibly-naive ``datetime``, an ISO-8601
# string from a hand-repaired document), and a second implementation was a second place for those
# cases to drift apart.
_as_dt = notification_store._as_dt


# ---- The audit record -------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRecord:
    audit_id: str
    actor: str
    action: str
    result: str
    at: datetime
    client_id: str | None = None
    destination_id: str | None = None
    key_id: str | None = None
    reason: str | None = None


def record_admin_action(
    *,
    actor: str,
    action: str,
    result: str,
    client_id: str | None = None,
    destination_id: str | None = None,
    key_id: str | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> AuditRecord:
    """Append one operator-action record. R22's "no client credential can read it" is structural.

    Written for a REFUSAL as well as for a success -- scenario 10 is explicit that an action taken
    without the operator group is refused *and* audited -- which is why this is a plain append with
    no authorization of its own. The consequence is worth naming rather than hiding: anyone who can
    write this collection can append to it, so the record proves what was attempted and by whom it
    claimed to be, not that the claim was verified. What makes it useful anyway is that a refusal is
    exactly the row an operator wants to see, and that the collection is append-only: nothing in
    this repo updates or deletes a row here, so a later action cannot erase an earlier one.

    The document id is the millisecond timestamp followed by random bytes, so ids sort
    chronologically (Firestore lists a collection by id) and two actions in the same millisecond
    cannot collide.
    """
    instant = now or _now()
    audit_id = f"{int(instant.timestamp() * 1000):013d}-{secrets.token_hex(6)}"
    doc: dict[str, Any] = {
        "audit_id": audit_id,
        "actor": actor,
        "action": action,
        "result": result,
        "at": instant,
        "client_id": client_id,
        "destination_id": destination_id,
        "key_id": key_id,
        "reason": reason,
    }
    _store().set(ADMIN_AUDIT, audit_id, doc)
    logger.info(
        "Client admin: %s %s by %s",
        action,
        result,
        actor,
        extra={
            "fields": {
                "event": "notification_admin",
                "result": result,
                "action": action,
                "actor": actor,
                "client_id": client_id,
                "destination_id": destination_id,
                "key_id": key_id,
            }
        },
    )
    return AuditRecord(
        audit_id=audit_id,
        actor=actor,
        action=action,
        result=result,
        at=instant,
        client_id=client_id,
        destination_id=destination_id,
        key_id=key_id,
        reason=reason,
    )


# ---- Accounts ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    client_id: str
    name: str
    status: str
    created_at: datetime | None
    updated_at: datetime | None
    suspended_at: datetime | None = None
    suspended_reason: str | None = None
    quotas: Quotas = DEFAULT_QUOTAS

    @property
    def active(self) -> bool:
        return self.status == _ACCOUNT_ACTIVE


def _account_from_doc(doc: Mapping[str, Any]) -> Account:
    return Account(
        client_id=str(doc.get("client_id") or ""),
        name=str(doc.get("name") or ""),
        status=str(doc.get("status") or ""),
        created_at=_as_dt(doc.get("created_at")),
        updated_at=_as_dt(doc.get("updated_at")),
        suspended_at=_as_dt(doc.get("suspended_at")),
        suspended_reason=doc.get("suspended_reason") or None,
        quotas=_quotas_from_doc(doc),
    )


def get_account(client_id: str) -> Account | None:
    stored = _store().get(ACCOUNTS, client_id)
    return _account_from_doc(stored) if stored else None


def create_client(
    client_id: str,
    *,
    name: str,
    actor: str,
    quotas: Quotas | None = None,
    now: datetime | None = None,
) -> Account:
    """Provision a client. Idempotent on (id, name); a different name under a live id is refused.

    ``quotas`` applies to a NEW account only. An existing account keeps what it has -- the
    idempotency key is (id, name), and a quota change is :func:`set_client_quotas`, which is
    audited as one.
    """
    if not CLIENT_ID_RE.match(client_id or ""):
        raise Conflict("client_id must be lowercase alphanumeric with hyphens, 2-63 characters")
    if not name.strip():
        raise Conflict("a client needs a name")
    effective = quotas or DEFAULT_QUOTAS
    effective.validate()
    instant = now or _now()
    existing = _store().get(ACCOUNTS, client_id)
    if existing:
        if str(existing.get("name") or "") != name.strip():
            raise Conflict("client id already exists with a different name")
        record_admin_action(
            actor=actor,
            action="create_client",
            result="unchanged",
            client_id=client_id,
            now=instant,
        )
        return _account_from_doc(existing)
    doc = {
        "client_id": client_id,
        "name": name.strip(),
        "status": _ACCOUNT_ACTIVE,
        "created_at": instant,
        "updated_at": instant,
        "suspended_at": None,
        "suspended_reason": None,
        **effective.as_fields(),
    }
    _store().set(ACCOUNTS, client_id, doc)
    record_admin_action(
        actor=actor,
        action="create_client",
        result="created",
        client_id=client_id,
        now=instant,
    )
    return _account_from_doc(doc)


def set_client_suspended(
    client_id: str,
    *,
    suspended: bool,
    actor: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> Account:
    """Suspend or resume a client. Suspension takes effect within :data:`_ACCOUNT_TTL_S`.

    The account cache is cleared for this client rather than left to expire, so an operator who
    suspends and then checks sees the effect on the pod they are talking to. It is a single-replica
    deployment (``deployment.forceReplicas: 1``), so that is every pod -- and even if it were not,
    the TTL is the bound and the clear is the courtesy.
    """
    instant = now or _now()
    stored = _store().get(ACCOUNTS, client_id)
    if not stored:
        record_admin_action(
            actor=actor,
            action="suspend_client" if suspended else "resume_client",
            result="not_found",
            client_id=client_id,
            now=instant,
        )
        raise NotFound("no such client")
    patch: dict[str, Any] = {
        "status": _ACCOUNT_SUSPENDED if suspended else _ACCOUNT_ACTIVE,
        "updated_at": instant,
        "suspended_at": instant if suspended else None,
        "suspended_reason": (reason or None) if suspended else None,
    }
    _store().update(ACCOUNTS, client_id, patch)
    with _lock:
        _accounts.pop(client_id, None)
    record_admin_action(
        actor=actor,
        action="suspend_client" if suspended else "resume_client",
        result="ok",
        client_id=client_id,
        reason=reason,
        now=instant,
    )
    return _account_from_doc({**stored, **patch})


def set_client_quotas(
    client_id: str,
    *,
    actor: str,
    max_active_subscriptions: int | None = None,
    requests_per_minute: int | None = None,
    daily_cap: int | None = None,
    now: datetime | None = None,
) -> Account:
    """Raise or lower one client's ceilings; unnamed ones keep their value.

    The rate and the subscription ceiling take effect within :data:`_ACCOUNT_TTL_S` (the cache
    entry is cleared here, as a suspension clears it). The daily cap reaches each subscription the
    next time the client WRITES it -- create, replace or the idempotent re-PUT -- because it is
    stored on the subscription document, where the delivery pass reads it without a second lookup.
    Audited as ``set_client_quotas`` with the requested values in ``reason``; they carry no
    personal data.
    """
    instant = now or _now()
    stored = _store().get(ACCOUNTS, client_id)
    if not stored:
        record_admin_action(
            actor=actor,
            action="set_client_quotas",
            result="not_found",
            client_id=client_id,
            now=instant,
        )
        raise NotFound("no such client")
    requested = {
        name: value
        for name, value in (
            ("max_active_subscriptions", max_active_subscriptions),
            ("requests_per_minute", requests_per_minute),
            ("daily_cap", daily_cap),
        )
        if value is not None
    }
    if not requested:
        raise Conflict("name at least one quota to change")
    updated = Quotas(**{**_quotas_from_doc(stored).as_fields(), **requested})
    updated.validate()
    patch: dict[str, Any] = {**updated.as_fields(), "updated_at": instant}
    _store().update(ACCOUNTS, client_id, patch)
    with _lock:
        _accounts.pop(client_id, None)
    record_admin_action(
        actor=actor,
        action="set_client_quotas",
        result="ok",
        client_id=client_id,
        reason=", ".join(f"{name}={value}" for name, value in sorted(requested.items())),
        now=instant,
    )
    return _account_from_doc({**stored, **patch})


# ---- Credentials ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    """A stored credential as an operator sees it. **Never carries the secret or its hash.**"""

    key_id: str
    client_id: str
    label: str
    status: str
    created_at: datetime | None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    expires_at: datetime | None = None


def _credential_from_doc(doc: Mapping[str, Any]) -> Credential:
    return Credential(
        key_id=str(doc.get("key_id") or ""),
        client_id=str(doc.get("client_id") or ""),
        label=str(doc.get("label") or ""),
        status=str(doc.get("status") or ""),
        created_at=_as_dt(doc.get("created_at")),
        revoked_at=_as_dt(doc.get("revoked_at")),
        revoked_reason=doc.get("revoked_reason") or None,
        expires_at=_as_dt(doc.get("expires_at")),
    )


def issue_credential(
    client_id: str,
    *,
    actor: str,
    label: str = "",
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[Credential, str]:
    """Mint a credential. Returns the record and the bearer string, which is shown ONCE.

    **Several credentials live at once**, which is the whole of KTD8's rotation story: issuing a new
    ``key-id`` does not disturb the old one, so a client cuts over at their own pace and the
    retiring credential is revoked afterwards. Taking a client down to rotate a key is the failure
    this replaces, and it is why the ceiling below is a handful rather than one.
    """
    instant = now or _now()
    account = get_account(client_id)
    if account is None:
        raise NotFound("no such client")
    # Live means what `authenticate` means by it: `active` AND not past `expires_at`. An expired
    # credential is refused on every request already, so counting it here would make an operator
    # revoke credentials that can no longer be used before minting the replacement -- exactly the
    # rotation step this ceiling is sized to avoid.
    live = [
        c
        for c in list_credentials(client_id)
        if c.status == _CREDENTIAL_ACTIVE and (c.expires_at is None or c.expires_at > instant)
    ]
    if len(live) >= MAX_CREDENTIALS_PER_CLIENT:
        raise Conflict(
            f"client already holds {len(live)} live credentials "
            f"(ceiling {MAX_CREDENTIALS_PER_CLIENT}); revoke one first"
        )
    key_id, secret = mint_credential()
    doc = {
        "key_id": key_id,
        "client_id": client_id,
        "label": label.strip(),
        "status": _CREDENTIAL_ACTIVE,
        # The peppered hash and nothing else. There is no reversible copy of the secret anywhere:
        # a lost credential is re-issued, never recovered, which is what makes "shown once" true
        # rather than a convention.
        "secret_hash": secret_hash(key_id, secret),
        "created_at": instant,
        "revoked_at": None,
        "revoked_reason": None,
        "expires_at": expires_at,
    }
    _store().set(CREDENTIALS, key_id, doc)
    record_admin_action(
        actor=actor,
        action="issue_credential",
        result="created",
        client_id=client_id,
        key_id=key_id,
        now=instant,
    )
    return _credential_from_doc(doc), bearer_credential(key_id, secret)


def revoke_credential(
    key_id: str, *, actor: str, reason: str | None = None, now: datetime | None = None
) -> Credential:
    """Revoke one ``key-id``. Terminal: a revoked credential is never reactivated.

    Effective on the caller's next request rather than within the account cache's window, because
    revocation is the lever used in a hurry -- the credential document itself is read on every
    request and the negative cache only ever *adds* a refusal. The one bound worth knowing is the
    other direction: a key revoked while a request is already authenticated completes that request.
    """
    instant = now or _now()
    stored = _store().get(CREDENTIALS, key_id)
    if not stored:
        record_admin_action(
            actor=actor, action="revoke_credential", result="not_found", key_id=key_id, now=instant
        )
        raise NotFound("no such credential")
    patch = {
        "status": _CREDENTIAL_REVOKED,
        "revoked_at": instant,
        "revoked_reason": reason or None,
    }
    _store().update(CREDENTIALS, key_id, patch)
    record_admin_action(
        actor=actor,
        action="revoke_credential",
        result="ok",
        client_id=str(stored.get("client_id") or "") or None,
        key_id=key_id,
        reason=reason,
        now=instant,
    )
    return _credential_from_doc({**stored, **patch})


def list_credentials(client_id: str) -> list[Credential]:
    """A client's credentials, newest first. Never carries a secret or a hash."""
    rows = _store().list_by(
        CREDENTIALS, "client_id", client_id, limit=MAX_CREDENTIALS_PER_CLIENT * 4
    )
    out = [_credential_from_doc(r) for r in rows]
    out.sort(key=lambda c: (c.created_at or datetime.min.replace(tzinfo=UTC), c.key_id), reverse=True)
    return out


# ---- Destinations -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """A provisioned callback, and the record R12's pause lives on.

    ``generations`` holds the live signing generations and **no signing material**: a secret is
    derived from the root over ``destination_id || generation``
    (:func:`webhook_signing.derive_secret`), so the store holds nothing to leak and one client's
    compromised receiver secret forges nothing for another. Two are live at once -- ``active`` and
    ``retiring`` -- which is what lets a rotation happen without a flag day and lets either be
    revoked alone.
    """

    destination_id: str
    client_id: str
    callback_url: str
    status: str
    paused: bool
    active_generation: int
    retiring_generation: int | None
    created_at: datetime | None
    updated_at: datetime | None
    paused_at: datetime | None = None
    paused_reason: str | None = None

    @property
    def signing_generations(self) -> tuple[int, ...]:
        """Active first, then retiring: the order :func:`webhook_signing.sign` documents."""
        if self.retiring_generation is None:
            return (self.active_generation,)
        return (self.active_generation, self.retiring_generation)

    @property
    def deliverable(self) -> bool:
        """Whether U6 may attempt this destination at all: active, and not paused."""
        return self.status == _DESTINATION_ACTIVE and not self.paused


def _destination_from_doc(doc: Mapping[str, Any]) -> Destination:
    generations = doc.get("generations") or {}
    retiring = generations.get("retiring") if isinstance(generations, Mapping) else None
    return Destination(
        destination_id=str(doc.get("destination_id") or ""),
        client_id=str(doc.get("client_id") or ""),
        callback_url=str(doc.get("callback_url") or ""),
        status=str(doc.get("status") or ""),
        # Absent reads as NOT paused, which is the safe direction here and is worth stating because
        # `notification_store._not_expired` fails the other way for its own field. A missing `paused` key
        # means a document written before the flag existed, and reading that as paused would take
        # every pre-existing destination out of service on the deploy that added it.
        paused=bool(doc.get("paused")),
        active_generation=int(
            (generations.get("active") if isinstance(generations, Mapping) else None) or 1
        ),
        retiring_generation=int(retiring) if retiring is not None else None,
        created_at=_as_dt(doc.get("created_at")),
        updated_at=_as_dt(doc.get("updated_at")),
        paused_at=_as_dt(doc.get("paused_at")),
        paused_reason=doc.get("paused_reason") or None,
    )


def get_destination(destination_id: str, *, client_id: str | None = None) -> Destination | None:
    """One destination. Raises :class:`Forbidden` when ``client_id`` is given and does not own it.

    Tenancy is checked here rather than left to the caller, exactly as every subscription-scoped
    method in ``notification_store`` does it: one client cannot read or affect another's destination
    through any function in this module.
    """
    stored = _store().get(DESTINATIONS, destination_id)
    if not stored:
        return None
    if client_id is not None and str(stored.get("client_id") or "") != client_id:
        raise Forbidden("destination belongs to another client")
    return _destination_from_doc(stored)


def destination_for_delivery(destination_id: str) -> Destination | None:
    """What U6 reads before attempting a destination.

    Its own name rather than a bare :func:`get_destination` call, so the delivery path states which
    question it is asking: not "does this destination exist" but "may we dial it this cycle". A
    paused or retired destination comes back and reads ``deliverable == False``, so the caller can
    record *why* nothing was attempted instead of logging a missing record.
    """
    return get_destination(destination_id)


def current_destination(client_id: str) -> Destination | None:
    """The client's active destination -- one per environment in the pilot.

    Newest-created wins if somehow there are several, and the tie-break is the id so the answer
    cannot flip between requests. A paused one is still *current*: pausing stops delivery, not
    ownership, and a client whose destination has been paused most needs to see it.
    """
    rows = _store().list_by(
        DESTINATIONS, "client_id", client_id, limit=MAX_DESTINATIONS_PER_CLIENT * 4
    )
    live = [_destination_from_doc(r) for r in rows if r.get("status") == _DESTINATION_ACTIVE]
    if not live:
        return None
    live.sort(
        key=lambda d: (d.created_at or datetime.min.replace(tzinfo=UTC), d.destination_id),
        reverse=True,
    )
    return live[0]


def _checked_url(url: str) -> str:
    """KTD9's gate, as far as it can be taken without a network. Refusal is a :class:`Conflict`.

    Two halves, and the split is the whole point. ``webhook_sender.validate_url`` is pure and
    network-free -- HTTPS only, no userinfo, no fragment, default port -- which is exactly why it
    belongs here: a bad callback URL is caught while a human is present to fix it rather than
    discovered on the first delivery.

    The second half is the address test, and it applies here **only when the host is an IP
    literal**, where it needs no DNS at all. That is not a corner case: ``https://169.254.169.254/``
    is the URL an operator most plausibly pastes by mistake, and it passes every syntactic check.
    Running :func:`webhook_sender.is_safe_address` on a literal is pure, so provisioning refuses it
    -- including the NAT64 and 6to4 forms, which report as global and non-private while encoding a
    metadata address.

    A HOSTNAME's addresses are deliberately NOT resolved here. That is
    ``webhook_sender.resolve_and_validate``, it opens a socket (DNS is UDP, and the module refuses
    to from a test process), and it must run **per attempt** regardless: validating once at
    provisioning and trusting it later is a time-of-check race, which is how CVE-2026-27129 and
    SVIXSEC-2026-0001 both happened. So a hostname that resolves to a private address is refused at
    delivery, with the reason on the delivery record, not here.
    """
    try:
        destination = webhook_sender.validate_url(url)
    except webhook_sender.UnsafeDestination as exc:
        raise Conflict(f"unsafe callback URL: {exc}") from exc
    host = destination.host.strip("[]")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return destination.url  # a hostname: resolved and validated per attempt, never here
    if not webhook_sender.is_safe_address(literal):
        raise Conflict(
            f"unsafe callback URL: {host} is not a globally routable address"
        )
    return destination.url


def create_destination(
    destination_id: str,
    *,
    client_id: str,
    callback_url: str,
    actor: str,
    now: datetime | None = None,
) -> Destination:
    """Provision a callback destination. The URL is gated by KTD9's address test first.

    A refusal is audited (scenario 8): the attempt, the actor and the reason land in the audit
    collection, because "the operator tried to point a client at 169.254.169.254 and we refused"
    is exactly the row somebody will want later, and a refusal that leaves no trace is
    indistinguishable from an operator who never tried.
    """
    instant = now or _now()
    if not IDENTIFIER_RE.match(destination_id or ""):
        raise Conflict("destination_id must be 1-128 characters of [A-Za-z0-9._:-]")
    if get_account(client_id) is None:
        raise NotFound("no such client")
    try:
        url = _checked_url(callback_url)
    except Conflict as exc:
        record_admin_action(
            actor=actor,
            action="create_destination",
            result="refused",
            client_id=client_id,
            destination_id=destination_id,
            reason=str(exc),
            now=instant,
        )
        raise
    existing = _store().get(DESTINATIONS, destination_id)
    if existing:
        if str(existing.get("client_id") or "") != client_id:
            raise Forbidden("destination belongs to another client")
        if str(existing.get("callback_url") or "") != url:
            raise Conflict("destination id already exists with a different URL; amend it instead")
        record_admin_action(
            actor=actor,
            action="create_destination",
            result="unchanged",
            client_id=client_id,
            destination_id=destination_id,
            now=instant,
        )
        return _destination_from_doc(existing)
    live = _store().list_by(
        DESTINATIONS, "client_id", client_id, limit=MAX_DESTINATIONS_PER_CLIENT * 4
    )
    if sum(1 for r in live if r.get("status") == _DESTINATION_ACTIVE) >= (
        MAX_DESTINATIONS_PER_CLIENT
    ):
        raise Conflict(
            f"client already holds {MAX_DESTINATIONS_PER_CLIENT} active destinations"
        )
    doc = {
        "destination_id": destination_id,
        "client_id": client_id,
        "callback_url": url,
        "status": _DESTINATION_ACTIVE,
        "paused": False,
        "paused_at": None,
        "paused_reason": None,
        # Generation 1 active, nothing retiring. A rotation moves active to retiring and mints the
        # next one, so the numbers only ever increase and a signature entry names which generation
        # verified it.
        "generations": {"active": 1, "retiring": None},
        "created_at": instant,
        "updated_at": instant,
    }
    _store().set(DESTINATIONS, destination_id, doc)
    record_admin_action(
        actor=actor,
        action="create_destination",
        result="created",
        client_id=client_id,
        destination_id=destination_id,
        now=instant,
    )
    return _destination_from_doc(doc)


def amend_destination(
    destination_id: str, *, callback_url: str, actor: str, now: datetime | None = None
) -> Destination:
    """Point a destination at a new URL. Gated and audited exactly as creation is.

    Note what an amendment does to work already in flight, which is ``notification_store``'s rule rather
    than this module's: ``claim_delivery`` cancels a pending delivery whose subscription now names a
    different destination, because a delivery signed for and addressed to a callback the client has
    since moved must not go to the old one. Moving the URL *under* one destination id is the case
    that rule does not cover, and it is the reason a URL change bumps ``updated_at`` and nothing
    else: the destination is the same destination, and its pending deliveries are still for it.
    """
    instant = now or _now()
    stored = _store().get(DESTINATIONS, destination_id)
    if not stored:
        raise NotFound("no such destination")
    try:
        url = _checked_url(callback_url)
    except Conflict as exc:
        record_admin_action(
            actor=actor,
            action="amend_destination",
            result="refused",
            client_id=str(stored.get("client_id") or "") or None,
            destination_id=destination_id,
            reason=str(exc),
            now=instant,
        )
        raise
    patch = {"callback_url": url, "updated_at": instant}
    _store().update(DESTINATIONS, destination_id, patch)
    record_admin_action(
        actor=actor,
        action="amend_destination",
        result="ok",
        client_id=str(stored.get("client_id") or "") or None,
        destination_id=destination_id,
        now=instant,
    )
    return _destination_from_doc({**stored, **patch})


def pause_destination(
    destination_id: str,
    *,
    reason: str,
    actor: str = "system:delivery",
    now: datetime | None = None,
) -> Destination:
    """R12: a destination is paused until an operator resumes it.

    Called by U6 on a 410 (``webhook_sender.DeliveryAttempt.pause_destination``), which is why the
    actor defaults to the system rather than being required: the pause is automatic and the *resume*
    is the operator action. It is still audited under that actor, because an operator arriving at a
    silent destination needs to see when and why it stopped -- and ``mark_delivery_failed`` records
    only what happened to the one delivery.

    Idempotent: pausing an already-paused destination writes nothing and audits nothing, so a
    backlog of pending deliveries against a gone endpoint cannot fill the audit collection.
    """
    instant = now or _now()
    stored = _store().get(DESTINATIONS, destination_id)
    if not stored:
        raise NotFound("no such destination")
    if bool(stored.get("paused")):
        return _destination_from_doc(stored)
    patch = {
        "paused": True,
        "paused_at": instant,
        "paused_reason": str(reason)[:200],
        "updated_at": instant,
    }
    _store().update(DESTINATIONS, destination_id, patch)
    record_admin_action(
        actor=actor,
        action="pause_destination",
        result="ok",
        client_id=str(stored.get("client_id") or "") or None,
        destination_id=destination_id,
        reason=str(reason)[:200],
        now=instant,
    )
    return _destination_from_doc({**stored, **patch})


def resume_destination(
    destination_id: str, *, actor: str, now: datetime | None = None
) -> Destination:
    """Lift a pause. An operator action, and the only way out of one."""
    instant = now or _now()
    stored = _store().get(DESTINATIONS, destination_id)
    if not stored:
        raise NotFound("no such destination")
    patch = {"paused": False, "paused_at": None, "paused_reason": None, "updated_at": instant}
    _store().update(DESTINATIONS, destination_id, patch)
    record_admin_action(
        actor=actor,
        action="resume_destination",
        result="ok",
        client_id=str(stored.get("client_id") or "") or None,
        destination_id=destination_id,
        now=instant,
    )
    return _destination_from_doc({**stored, **patch})


def rotate_signing_generation(
    destination_id: str, *, actor: str, now: datetime | None = None
) -> Destination:
    """Mint the next signing generation and retire the current one.

    Two live at once, active first: every attempt carries a ``v1,<sig>`` entry per generation
    (:func:`webhook_signing.signature_header`), so a receiver that has only the old secret still
    verifies while it installs the new one. :func:`retire_signing_generation` is the second half --
    dropping the old entry once the client confirms cutover, which is what makes each generation
    revocable alone.
    """
    instant = now or _now()
    stored = _store().get(DESTINATIONS, destination_id)
    if not stored:
        raise NotFound("no such destination")
    current = _destination_from_doc(stored)
    generations = {
        "active": current.active_generation + 1,
        "retiring": current.active_generation,
    }
    patch = {"generations": generations, "updated_at": instant}
    _store().update(DESTINATIONS, destination_id, patch)
    record_admin_action(
        actor=actor,
        action="rotate_signing_generation",
        result="ok",
        client_id=current.client_id or None,
        destination_id=destination_id,
        reason=f"active={generations['active']} retiring={generations['retiring']}",
        now=instant,
    )
    return _destination_from_doc({**stored, **patch})


def retire_signing_generation(
    destination_id: str, *, actor: str, now: datetime | None = None
) -> Destination:
    """Drop the retiring generation, so only the active secret verifies from now on."""
    instant = now or _now()
    stored = _store().get(DESTINATIONS, destination_id)
    if not stored:
        raise NotFound("no such destination")
    current = _destination_from_doc(stored)
    if current.retiring_generation is None:
        return current
    patch = {
        "generations": {"active": current.active_generation, "retiring": None},
        "updated_at": instant,
    }
    _store().update(DESTINATIONS, destination_id, patch)
    record_admin_action(
        actor=actor,
        action="retire_signing_generation",
        result="ok",
        client_id=current.client_id or None,
        destination_id=destination_id,
        reason=f"retired={current.retiring_generation}",
        now=instant,
    )
    return _destination_from_doc({**stored, **patch})


__all__ = [
    "ACCOUNTS",
    "ADMIN_AUDIT",
    "CREDENTIALS",
    "DEFAULT_QUOTAS",
    "DESTINATIONS",
    "GLOBAL_PRELOOKUP_PER_MIN",
    "MAX_CREDENTIALS_PER_CLIENT",
    "MAX_DESTINATIONS_PER_CLIENT",
    "PEPPER_ENV",
    "PER_CLIENT_PER_MIN",
    "QUOTA_BOUNDS",
    "RETRY_AFTER_S",
    "Account",
    "AuditRecord",
    "AuthFailed",
    "AuthSuspended",
    "ClientAuthError",
    "Conflict",
    "Credential",
    "Destination",
    "Forbidden",
    "NotConfigured",
    "NotFound",
    "Principal",
    "Quotas",
    "RateLimited",
    "amend_destination",
    "authenticate",
    "available",
    "bearer_credential",
    "claim_client_slot",
    "claim_window",
    "configure",
    "create_client",
    "create_destination",
    "current_destination",
    "destination_for_delivery",
    "get_account",
    "get_destination",
    "issue_credential",
    "list_credentials",
    "mint_credential",
    "pause_destination",
    "pepper_ready",
    "record_admin_action",
    "reset_for_tests",
    "reset_limiters",
    "resume_destination",
    "retire_signing_generation",
    "revoke_credential",
    "rotate_signing_generation",
    "secret_hash",
    "set_client_quotas",
    "set_client_suspended",
    "store_ready",
]
