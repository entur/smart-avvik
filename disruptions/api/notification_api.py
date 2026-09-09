"""/notification-api/v1 — the authenticated webhook subscription API.

A client BFF manages its own commuter registrations here, inside quotas, under a credential this
app issued. Six routes, published as ``contracts/notifications/v1/openapi.json``, which **renders**
``subscription.schema.json`` and ``event.schema.json`` with ``$ref`` rather than restating them:
two definitions of one payload is how they drift, and U7 generates the client's TypeScript types
from the same file.

===============================================  ===============================================
``POST   /subscriptions``                        create or replace by the client's reference
``GET    /subscriptions``                        the client's own watches, paginated
``GET    /subscriptions/{subscription_id}``      one watch, with servability and freshness
``DELETE /subscriptions/{subscription_id}``      tombstone, then durable cleanup; idempotent
``GET    /destinations/current``                 readiness and signing generations, never secrets
``POST   /test-events``                          one signed synthetic event, test-only
===============================================  ===============================================

Seven things here are decisions rather than mechanics.

1. **Every route is a plain ``def``.** They touch Firestore, so they run in the anyio worker pool
   like every other route in this process. ``/meldinger``'s ``async def`` inversion is the
   precedent only for a handler that would otherwise hold one of those slots for its whole
   deadline, which none of these does.
2. **Authentication is a bearer header verified in the router's own dependency** (KTD8), never a
   cookie: this app has ONE global ``CORSMiddleware`` with ``allow_origins=["*"]``, so every new
   endpoint is reachable from every origin and a cookie would be sent from all of them. The client
   prefix is deliberately **not** in ``sso.PROTECTED_PREFIXES`` -- that gate answers an
   unconfigured deployment with 503 *and an unauthenticated browser with a 302*, and a machine
   caller following a redirect into a Google login screen gets an HTML page where it expected JSON.
3. **Ownership comes from the credential and never from the body.** ``client_id`` is read off
   :class:`notification_auth.Principal`, and the stored document id is
   ``f"{client_id}~{subscription_id}"`` -- so one client physically cannot address another's
   record, and two clients may use the same reference without colliding. ``~`` is outside the
   reference charset, which is what makes the composite collision-free rather than merely unlikely.
4. **R2's shape is reached, not restated.** The window, the weekday set and the from/to rules come
   from :meth:`notification_store.SubscriptionSpec.validate`, which is /varsel's own three rules; the
   stop refs go through /varsel's ``canonical_stop``; the servability answer comes from /varsel's
   own connectivity seam. The Pydantic models below bound *sizes and charsets* and deliberately
   state no semantics -- a fourth copy of "begin must be before end" is exactly how the two
   surfaces would drift apart, and ``test_notification_api`` greps this file to keep it that way.
5. **The test-only marker is immutable and cannot be set after creation** (R15/AE18). The store
   refuses a replace that changes it; this module refuses a synthetic event for a subscription that
   is not marked; and the marker rides inside the signed body, which is U6's to emit.
6. **``/test-events`` sends synchronously and writes NOTHING to the store.** That is not
   convenience: ``notification_store``'s pending-event ceiling is exact rather than best-effort *because
   the emit path has exactly one writer* -- one poller thread, no route invoking the pass, one
   replica under the Recreate strategy. A route that called ``commit_event`` would falsify that
   claim for every quota check in the module. So a test event is signed and dialled here and leaves
   no event, no delivery and no alert subject behind.
7. **Deletion is ordered.** The tombstone lands first, in one transactional write that stops
   routing (the poller's load query filters ``status == "active"``); the events, deliveries and
   subjects behind it are then deleted in bounded batches with an auditable completion state on the
   tombstone itself. Reversed, a partial delete leaves a routable subscription whose events are
   gone. Where cleanup has not completed, a repeated DELETE resumes it and TTL is the published
   fallback (``notification_store.TTL_RESIDUAL_HOURS``).

No import of ``main`` and no import of ``varsel`` (the package rule): ``main`` injects the store,
the credential plane, the canonicaliser and the connectivity seam through :func:`configure`, the
way it already does for ``historikk``, ``meldinger``, ``reisevalg`` and ``togvarsel_notify``.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import alert_policy
import notification_auth
import notification_store
import webhook_delivery
import webhook_sender
import webhook_signing
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

PREFIX = "/notification-api/v1"

#: Separates the client id from the client's own reference in the stored document id. Outside
#: `subscription.schema.json`'s reference charset (`[A-Za-z0-9._:-]`) and outside
#: `notification_auth.CLIENT_ID_RE`, so the split is unambiguous in both directions.
_NAMESPACE = "~"

#: How many cleanup passes one DELETE runs before reporting `pending`. Bounded because this is a
#: request path: three passes of `_CLEANUP_BATCH` is 600 documents, comfortably more than a pilot
#: subscription's seven days of events, and a subscription that somehow holds more resumes on the
#: next DELETE (which is idempotent) or falls back to TTL. Unbounded looping here would let one
#: deletion hold an anyio slot for as long as the data took to walk.
_CLEANUP_PASSES = 3
_CLEANUP_BATCH = 200

#: Default and ceiling for a list page. The store's own query is capped at
#: `MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT`, so a page is a slice of an already-bounded read.
_LIST_DEFAULT = 100
_LIST_MAX = notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT

#: `/test-events` is the one route that makes an OUTBOUND call, so it carries its own tight ceiling
#: on top of the per-client tier. Six a minute is enough to debug a receiver and far too few to
#: turn this app into a request amplifier against a client-supplied host.
_TEST_EVENTS_PER_MIN = 6
_TEST_EVENTS_WINDOW_S = 60

#: How long the route waits on the one worker its delivery runs on. `webhook_sender`'s own two
#: ceilings plus one second for the DNS resolution that precedes both of them and that neither can
#: bound -- see the comment at the call site. It has to EXCEED the attempt's worst case, or this
#: backstop would fire on an ordinary slow client and report `timeout` where their own status,
#: error or refusal is the diagnosis the endpoint exists to give.
_TEST_EVENT_WAIT_S = webhook_sender.DEFAULT_TIMEOUT_S + webhook_sender.CONNECT_TIMEOUT_S + 1.0

#: `Retry-After` on an R21 ceiling. Bounded, and deliberately longer than the limiter's 60 s: a
#: subscription-count ceiling does not clear when a window rolls over, so telling the caller to
#: retry in a minute would invite exactly the retry loop the ceiling exists to stop.
_QUOTA_RETRY_AFTER_S = notification_store.RETRY_AFTER_CAP_S

_store: Any = notification_store
_auth: Any = notification_auth
_canonicalize: Any = None
_connectivity: Any = None
_deliver: Any = None
_signer: Any = None

_lock = threading.Lock()
_test_event_calls: dict[str, deque[float]] = {}


def configure(
    *,
    store: Any | None = None,
    auth: Any | None = None,
    canonicalize: Any | None = None,
    connectivity: Any | None = None,
    deliver: Any | None = None,
    signer: Any | None = None,
) -> None:
    """Wire the seams. Called once from ``main.lifespan``; the suites pass fakes.

    ``canonicalize`` is ``varsel.canonical_stop`` and ``connectivity`` is ``varsel._service_verdict``
    -- /varsel's own two, injected rather than imported so this module does not pull 147 KB of
    poller state onto a request path, and so R2's and R5's shapes cannot drift into a second
    statement. ``connectivity`` already expands both places to every ref
    (``varsel.place_refs``) and already fails open to ``None``, which is why nothing here talks to
    BigQuery.
    """
    global _store, _auth, _canonicalize, _connectivity, _deliver, _signer
    _store = store if store is not None else notification_store
    _auth = auth if auth is not None else notification_auth
    _canonicalize = canonicalize
    _connectivity = connectivity
    _deliver = deliver
    _signer = signer
    with _lock:
        _test_event_calls.clear()


def _canonical(ref: str) -> str:
    """`ref`'s canonical stop place, or `ref` itself when no canonicaliser is wired.

    Falling through is right rather than lax: the alias map only exists after the stop registry has
    been read, and /varsel's own ``canonical_stop`` returns the input unchanged in exactly that
    state. A registration made before the map is built is rewritten by
    ``varsel._canonicalize_journeys`` on the next refresh -- for the browser side. That rewrite does
    NOT reach a Firestore document, which is a real limitation and is stated in the route below.
    """
    return _canonicalize(ref) if _canonicalize is not None else ref


# ---- Models -----------------------------------------------------------------------------------
#
# Every model is `extra="forbid"`, and on the RESPONSE side that is a drift guard rather than input
# hygiene: `subscription.schema.json` is `additionalProperties: false`, so a field added here and
# not there breaks U7's generated types silently. `test_the_response_model_matches_the_published_
# schema` compares the two field sets in both directions.


class SubscriptionIn(BaseModel):
    """What a client submits. **Bounds only** -- see the module docstring, point 4.

    Nothing here restates a rule that lives in :meth:`notification_store.SubscriptionSpec.validate`:
    ``days`` is length-bounded but its values are not checked here, and ``begin``/``end`` are
    length-bounded but their format and their ordering are not. What IS enforced is what the
    published schema says about shape, plus a ceiling on every string, because this is a public
    endpoint and an unbounded client string is not free.
    """

    model_config = ConfigDict(extra="forbid")

    subscription_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Your own reference for this watch. The plan's API table calls it the "
        "client's opaque reference and the published schema calls it subscription_id; they are "
        "the same value. Supplying it is what makes a create safely retryable.",
    )
    destination_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Which provisioned callback destination this watch's events go to.",
    )
    from_stop: str = Field(pattern=r"^NSR:StopPlace:[0-9]+$", description="Origin stop place.")
    to_stop: str = Field(pattern=r"^NSR:StopPlace:[0-9]+$", description="Destination stop place.")
    begin: str = Field(max_length=5, description='Window start "HH:MM" (Europe/Oslo).')
    end: str = Field(max_length=5, description='Window end "HH:MM" (Europe/Oslo).')
    days: list[int] = Field(max_length=7, description="Weekdays to watch: 0=Monday … 6=Sunday.")
    external_ref: str = Field(
        default="",
        max_length=256,
        description="Your opaque recipient reference, echoed unchanged in every event.",
    )
    test: bool = Field(
        default=False,
        description="Marks the watch test-only. Immutable: changing it needs delete and re-create.",
    )


class SubscriptionOut(BaseModel):
    """A stored subscription, exactly ``subscription.schema.json``'s property set.

    ``servable`` plus ``servable_checked_at`` ARE R5's caveat and R6's freshness, and the pair is
    why neither may be collapsed to a boolean: ``null`` means the answer could not be established
    -- an outage, a timeout, the 30/min ceiling on /varsel's own connectivity check -- and must
    never render as an all-clear. A subscription no direct trip can serve is stored anyway and
    carries the caveat: weekend-only and seasonal routes are why a refusal would be wrong.
    """

    model_config = ConfigDict(extra="forbid")

    subscription_id: str = Field(description="Your own reference, as you supplied it.")
    client_id: str = Field(description="Read-only: taken from the credential, never the body.")
    destination_id: str
    from_stop: str
    to_stop: str
    begin: str
    end: str
    days: list[int]
    external_ref: str
    test: bool
    version: int = Field(description="Monotonic; send it back as If-Match to condition a replace.")
    status: str = Field(description='"active" (a tombstone is never served here).')
    servable: bool | None = Field(
        description="Whether any planned direct trip runs between the two places. null means the "
        "answer could not be established and must never read as an all-clear.",
    )
    servable_checked_at: datetime | None = Field(description="How fresh that answer is.")
    alerts: AlertStateOut | None = Field(
        description="Current alert state on the two GET routes. **null** on the response to a "
        "create or replace, which does not read it -- null therefore means \"not included in this "
        "response\", never \"no standing alerts\"; that is `n_active: 0` with `truncated: false`.",
    )
    expires_at: datetime | None = Field(
        description="The earlier of a 180-day lifetime and a 60-day inactivity horizon, both "
        "enforced on read as well as by TTL.",
    )
    created_at: datetime | None
    updated_at: datetime | None


class AlertSubjectOut(BaseModel):
    """One standing alert on this watch: which train, which phase, which revision.

    The train's six fields are exactly the ones ``event.schema.json`` carries, so a card built from
    this and a notification built from an event describe the same train in the same terms.
    ``delivered_revision`` is the field that answers "did this reach the traveller": a revision with
    no delivered revision beside it is an alert we hold and they do not.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(description="The subject key your receiver deduplicates and orders on.")
    phase: str = Field(description='"predicted" or "confirmed". A withdrawn alert is not listed.')
    revision: int
    delivered_revision: int | None = Field(
        description="The highest revision about this train that was delivered to you; null if "
        "none has been.",
    )
    operator: str
    line: str
    journey_ref: str
    call_date: str = Field(description="Operating day of the boarding call (Europe/Oslo).")
    stop_place: str
    stop_order: int
    updated_at: datetime | None


class AlertStateOut(BaseModel):
    """This watch's current alert state: a summary, plus the worst few subjects.

    **Bounded on purpose.** ``subjects`` carries at most your daily cap's worth of entries
    (``MAX_ALERTS_SERVED``, 4, unless your client's cap is higher), worst first -- a declared
    cancellation ahead of a forecast, then most recently changed. One line suspension can put a
    dozen cancelled departures inside a single window (AE3), and at most that many notifications
    about a watch reach a traveller in an Oslo day, so a longer list would enumerate what the cap
    has already suppressed. The COUNTS are over everything read, so nothing is hidden
    -- only unenumerated.

    ``truncated`` is the honest field and must be rendered rather than ignored: when it is true the
    counts are LOWER BOUNDS, so ``n_active: 0`` beside it means "we could not establish this", never
    "all clear". It is set when the read hit its ceiling and when the read failed, and on a list
    page it is set on **every** row whenever that page's one query was bounded -- because a watch
    whose subjects fell outside the page would otherwise read exactly like a calm one. Ask about
    that watch individually (`GET /subscriptions/{id}`) and the read is scoped to it alone.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: datetime = Field(description="When this state was read.")
    n_active: int = Field(description="Standing alerts: predicted or confirmed, withdrawn excluded.")
    n_confirmed: int = Field(description="How many of those are confirmed cancellations.")
    truncated: bool = Field(
        description="True when the read was bounded or failed. The counts are then lower bounds "
        "and zero does not mean all-clear.",
    )
    subjects: list[AlertSubjectOut] = Field(
        description="At most a handful, worst first. The counts above are the whole picture.",
    )


class SubscriptionList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    limit: int
    next_cursor: str | None = Field(
        description="Pass as `after` to continue. null when this is the last page.",
    )
    subscriptions: list[SubscriptionOut]


class CleanupState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(description='"done" or "pending"; pending falls back to TTL.')
    passes: int
    deleted_events: int
    deleted_deliveries: int
    deleted_subjects: int
    residual_hours: int = Field(
        description="The published maximum residual window where cleanup has not completed and "
        "TTL is the fallback.",
    )


class DeleteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    status: str = Field(description='Always "deleted": a repeated deletion succeeds.')
    existed: bool
    cleanup: CleanupState | None


class SigningGenerations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: int
    retiring: int | None = Field(
        description="Both are valid at once, so one can be rotated without a flag day and revoked "
        "without touching another client. null when nothing is retiring.",
    )


class DestinationOut(BaseModel):
    """Destination readiness and signing key ids. **Never a secret.**

    A signing secret is derived from a root over ``destination_id || generation`` and is stored
    nowhere, so there is nothing here to withhold by accident -- but the response is pinned by a
    test that refuses the ``whsec_`` prefix anywhere in the body, because "we do not store it" and
    "we do not serve it" are different claims.
    """

    model_config = ConfigDict(extra="forbid")

    destination_id: str
    callback_url: str
    status: str
    paused: bool = Field(
        description="Paused on a 410 from your endpoint, and lifted only by an operator.",
    )
    paused_at: datetime | None
    paused_reason: str | None
    ready: bool = Field(description="Active, not paused, and signing is configured here.")
    signing_generations: SigningGenerations


class TestEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str = Field(
        min_length=1,
        # The SAME two forms the two path routes accept, and the bound (`~` plus 128+128) is theirs
        # too. It was the reference charset alone, which rejected the namespaced id an EVENT carries
        # in its `subscription_id` -- so a client who took the id from a GET response, or from the
        # event their receiver had just handled, and posted it here got a 422 from a value this API
        # had itself just served. `_requested_id` strips only the CALLER's own prefix, so widening
        # the charset opens no path to another client's record.
        max_length=257,
        pattern=r"^[A-Za-z0-9._:~-]+$",
        description="A subscription created with test: true. Your own reference, or the "
        "subscription_id an event carries. Anything else is refused.",
    )


class TestEventOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    subscription_id: str
    destination_id: str
    delivered: bool
    status: int | None = Field(description="Your endpoint's HTTP status, when it answered.")
    error: str | None
    signed_generations: list[int]


# ---- Errors -----------------------------------------------------------------------------------


def _http(exc: Exception, *, validating: bool = False) -> HTTPException:
    """One place that turns a store or credential failure into a status code.

    Three mappings are load-bearing:

    * ``Forbidden`` -> **404**, deliberately collapsing tenancy into absence. The store keeps the
      two apart internally so a violation can be logged; a client must not be able to probe
      another's subscription ids by the difference between a 403 and a 404, and the response body
      names nothing about the other client (AE11).
    * ``Conflict`` -> **422 while validating, 409 afterwards.** ``SubscriptionSpec.validate``
      raises the same class as a version mismatch does, and the two are different answers: the
      first is "your request is malformed" and the second is "the record has moved under you".
      Collapsing them would give /varsel's own window verdicts a 409, which reads as a race.
    * ``NotConfigured`` -> **503 with ``Retry-After``**, never 401. A deployment with no pepper or
      no database cannot verify anybody, and answering 401 would tell a client their credential is
      wrong when it is ours that is missing.
    """
    if isinstance(exc, notification_auth.NotConfigured | notification_store.NotConfigured):
        return HTTPException(
            status_code=503,
            detail="channel_not_configured",
            headers={"Retry-After": str(notification_auth.RETRY_AFTER_S)},
        )
    if isinstance(exc, notification_auth.AuthSuspended):
        return HTTPException(status_code=403, detail="client_suspended")
    if isinstance(exc, notification_auth.AuthFailed):
        # The reason is logged (`notification_auth._log_refusal`) and never returned: "unknown key id"
        # and "wrong secret" are different facts about somebody's credential and telling them apart
        # over the wire is an oracle.
        return HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if isinstance(exc, notification_auth.RateLimited):
        return HTTPException(
            status_code=429,
            detail="rate_limited",
            headers={"Retry-After": str(exc.retry_after_s)},
        )
    if isinstance(exc, notification_store.QuotaExceeded):
        return HTTPException(
            status_code=429,
            detail="quota_exceeded",
            headers={"Retry-After": str(_QUOTA_RETRY_AFTER_S)},
        )
    if isinstance(exc, notification_auth.Forbidden | notification_store.Forbidden):
        return HTTPException(status_code=404, detail="not_found")
    if isinstance(exc, notification_auth.NotFound | notification_store.NotFound):
        return HTTPException(status_code=404, detail="not_found")
    if isinstance(exc, notification_auth.Conflict | notification_store.Conflict):
        return HTTPException(
            status_code=422 if validating else 409, detail=str(exc) or "conflict"
        )
    # R20's fail-closed rule at the API edge: a store outage suppresses this request and nothing
    # wider. 503 rather than 500 because it is retryable, and the traceback stays in the log.
    logger.warning("Notification API store failure", exc_info=exc)
    return HTTPException(
        status_code=503,
        detail="store_unavailable",
        headers={"Retry-After": str(notification_auth.RETRY_AFTER_S)},
    )


# ---- The credential dependency ----------------------------------------------------------------


def _authenticated(request: Request) -> notification_auth.Principal:
    """Verify the bearer credential (KTD8). The router's own dependency, once per request.

    Declared BOTH at router level and as a parameter on every route. FastAPI caches an identical
    dependency within a request, so the credential is verified once -- and the router-level copy is
    what keeps a route added later gated even if its author forgets the parameter, which is exactly
    the shape ``deployment.require_rushvarsel`` uses one surface over.
    """
    try:
        return _auth.authenticate(request.headers.get("authorization"))
    except HTTPException:
        raise
    except Exception as exc:
        raise _http(exc) from exc


_Principal = Annotated[notification_auth.Principal, Depends(_authenticated)]


def _throttled(principal: _Principal) -> notification_auth.Principal:
    """Spend one per-client slot, BEFORE any handler runs (approach 4).

    Tier two of KTD8's limiter. Tier one is spent inside :func:`notification_auth.authenticate`, ahead
    of the credential lookup it exists to bound; this one is spent after authentication, so an
    anonymous flood cannot spend an authenticated client's budget -- and it is a router-level
    DEPENDENCY rather than a call inside each route, which is what makes "before anything is
    created" a property of the wiring instead of of whoever wrote the newest handler.

    It applies to reads as well as writes, deliberately. The ceiling is on what one client may ask
    of this surface, and a read is a Firestore query like any other; exempting reads would leave the
    cheapest way to spend the store unbounded except by the global tier, which anonymous traffic
    can already exhaust.
    """
    try:
        _auth.claim_client_slot(principal.client_id, limit=principal.quotas.requests_per_minute)
    except Exception as exc:
        raise _http(exc) from exc
    return principal


def _served(principal: notification_auth.Principal) -> int:
    """How many alert subjects a response enumerates for this client: its daily cap, never fewer
    than :data:`notification_store.MAX_ALERTS_SERVED`. The list is bounded by the cap because a
    subject past it is one the cap has already suppressed; a client with a larger cap sees a
    correspondingly longer list. The COUNTS are over everything read either way."""
    return max(notification_store.MAX_ALERTS_SERVED, principal.quotas.daily_cap)


#: The tag vocabulary, shared with `contracts/notifications/v1/openapi.json` and pinned against it
#: (`test_the_openapi_tags_are_the_routers_own`). Declared per ROUTE rather than on the router, so
#: the published document and this app's own /docs group the six routes identically -- the OpenAPI
#: file is what U7 generates types from and what a client reads, and two groupings for one surface
#: is a difference somebody has to reconcile by hand. Prefixed, because these names sit among 36
#: paths here: a bare "subscriptions" tag in an app that also serves journeys says nothing.
TAG_SUBSCRIPTIONS = "notification-subscriptions"
TAG_DESTINATIONS = "notification-destinations"
TAG_TEST = "notification-test"

router = APIRouter(
    prefix=PREFIX,
    dependencies=[Depends(_authenticated), Depends(_throttled)],
)


# ---- Identity helpers -------------------------------------------------------------------------


def _document_id(client_id: str, reference: str) -> str:
    return f"{client_id}{_NAMESPACE}{reference}"


def _reference(client_id: str, document_id: str) -> str:
    prefix = f"{client_id}{_NAMESPACE}"
    return document_id.removeprefix(prefix)


def _requested_id(client_id: str, value: str) -> str:
    """The stored document id for a path parameter.

    Accepts the client's own reference and, for convenience, the namespaced id an EVENT carries in
    its ``subscription_id`` field -- so a receiver holding an event can look the watch up without
    knowing how we key documents. It cannot cross tenants: the prefix it strips is the caller's own,
    so a value naming another client is treated as a reference and simply does not exist.
    """
    return _document_id(client_id, _reference(client_id, value))


def _alerts_out(snapshot: notification_store.AlertSnapshot) -> AlertStateOut:
    return AlertStateOut(
        as_of=snapshot.as_of,
        n_active=snapshot.n_active,
        n_confirmed=snapshot.n_confirmed,
        truncated=snapshot.truncated,
        subjects=[
            AlertSubjectOut(
                alert_id=state.alert_id,
                phase=state.phase,
                revision=state.revision,
                delivered_revision=state.delivered_revision,
                operator=state.train.operator,
                line=state.train.line,
                journey_ref=state.train.journey_ref,
                call_date=state.train.call_date,
                stop_place=state.train.stop_place,
                stop_order=state.train.stop_order,
                updated_at=state.updated_at,
            )
            for state in snapshot.subjects
        ],
    )


def _unknown_alerts() -> AlertStateOut:
    """What a bounded or failed alert-state read renders as. **Never an empty all-clear.**

    R6's rule, applied to a second question: missing or stale data must not read as an all-clear.
    So a read that could not be completed comes back with `truncated: true` and zero counts, which
    a client is told to treat as "we do not know" -- and a genuinely quiet watch comes back with
    `truncated: false`, which is a claim.
    """
    return AlertStateOut(
        as_of=datetime.now(UTC), n_active=0, n_confirmed=0, truncated=True, subjects=[]
    )


def _out(
    client_id: str,
    sub: notification_store.Subscription,
    alerts: AlertStateOut | None = None,
) -> SubscriptionOut:
    return SubscriptionOut(
        subscription_id=_reference(client_id, sub.subscription_id),
        client_id=sub.client_id,
        destination_id=sub.destination_id,
        from_stop=sub.from_stop,
        to_stop=sub.to_stop,
        begin=sub.begin,
        end=sub.end,
        days=list(sub.days),
        external_ref=sub.external_ref,
        test=sub.test,
        version=sub.version,
        status=sub.status,
        servable=sub.servable,
        servable_checked_at=sub.servable_checked_at,
        expires_at=sub.expires_at,
        created_at=sub.created_at,
        updated_at=sub.updated_at,
        alerts=alerts,
    )


def _spec(principal: notification_auth.Principal, body: SubscriptionIn) -> notification_store.SubscriptionSpec:
    """Build the store's own spec, canonicalising both stop refs first.

    Canonicalisation before storage, for the reason ``varsel.create_journey`` gives: a client
    holding a stale stop list -- or posting straight to the API -- can name a ref the registry has
    collapsed onto a co-located primary, and storing it recreates exactly the failure the collapse
    exists to prevent (a watched quay the trip never calls at, reported forever as "no active
    disruptions"). The from == to check runs AFTER canonicalising, because two refs for one physical
    place look like two stops in an older list.
    """
    from_stop = _canonical(body.from_stop)
    to_stop = _canonical(body.to_stop)
    if from_stop == to_stop:
        raise HTTPException(status_code=422, detail="from_stop and to_stop must differ")
    return notification_store.SubscriptionSpec(
        subscription_id=_document_id(principal.client_id, body.subscription_id),
        client_id=principal.client_id,
        destination_id=body.destination_id,
        from_stop=from_stop,
        to_stop=to_stop,
        begin=body.begin,
        end=body.end,
        days=tuple(body.days),
        external_ref=body.external_ref,
        test=body.test,
    )


def _checked_destination(
    principal: notification_auth.Principal, destination_id: str
) -> notification_auth.Destination:
    """The destination this subscription names, or a 422 that names nothing about anyone else.

    Unknown and foreign are the SAME answer, which is the collapse AE11 asks for: a client must
    not learn another's destination ids from the difference. 422 rather than 404 because the failure
    is a field in the request body -- the route target exists -- and a 404 on a POST reads as "no
    such endpoint" to whoever is debugging.

    A PAUSED destination is accepted here on purpose. Pausing stops delivery, not registration:
    events queue behind the pause and go out when an operator resumes it, and refusing new watches
    would turn a receiver outage into lost registrations too.
    """
    try:
        destination = _auth.get_destination(destination_id, client_id=principal.client_id)
    except notification_auth.Forbidden:
        destination = None
    except Exception as exc:
        raise _http(exc) from exc
    if destination is None or destination.status != "active":
        raise HTTPException(status_code=422, detail="unknown_destination")
    return destination


def _same_definition(spec: notification_store.SubscriptionSpec, sub: notification_store.Subscription) -> bool:
    return (
        spec.destination_id == sub.destination_id
        and spec.from_stop == sub.from_stop
        and spec.to_stop == sub.to_stop
        and spec.begin == sub.begin
        and spec.end == sub.end
        and tuple(sorted(set(spec.days))) == tuple(sub.days)
        and spec.external_ref == sub.external_ref
        and bool(spec.test) == bool(sub.test)
    )


def _record_servability(
    spec: notification_store.SubscriptionSpec, *, expected_version: int
) -> notification_store.Subscription | None:
    """Ask /varsel's connectivity seam and record R5's caveat. Never fails the request.

    Two things it deliberately does not do. It does not write when the verdict could not be
    established: ``_new_subscription_doc`` and ``_replace_body`` both leave ``servable`` as ``None``
    with no ``servable_checked_at``, which is already "not established" -- so a provider timeout
    returns no caveat rather than a false one (AE13), and it costs no second transaction. And it
    does not raise: the servability answer is a caveat on a registration that is never refused for
    it, so a failure here leaves the record exactly as a timeout would and the client reads
    ``servable: null``.

    It is a second transaction after the create, which is the one seam in this flow. The store's
    ``servable`` is written by ``set_servability`` and hard-coded to ``None`` by the create, so
    folding them would mean either a wider store API or a caveat computed inside a transaction body
    -- and a transaction body may run more than once, which is the last place a 20-second BigQuery
    question belongs.

    ``expected_version`` is the version the QUESTION was asked about, and it is what keeps the
    answer about a route rather than about an id: the question takes up to
    ``main._CONNECTIVITY_TIMEOUT_S`` (20 s) while a replace is one request, so a concurrent replace
    can land in between and this would otherwise stamp the new route with the old route's verdict,
    freshly timestamped. The store enforces it inside the transaction; a conflict here means the
    definition moved and the verdict is discarded, exactly as an unestablished one is.
    """
    if _connectivity is None:
        return None
    try:
        verdict = _connectivity(spec.from_stop, spec.to_stop)
    except Exception:
        logger.warning(
            "Client servability check raised; storing no caveat",
            exc_info=True,
            extra={"fields": {"event": "notification_servability", "result": "error"}},
        )
        return None
    if verdict is None:
        return None
    try:
        return _store.set_servability(
            spec.subscription_id, servable=bool(verdict[0]), expected_version=expected_version
        )
    except notification_store.Conflict:
        # The route was replaced while we were asking. Not a failure and not an error: the newer
        # definition retired the answer AND its stamp on purpose, and `notification_servability.recheck`
        # picks a never-established record up on its own cadence.
        logger.info(
            "A client servability verdict arrived after the route was replaced; discarded",
            extra={
                "fields": {
                    "event": "notification_servability",
                    "result": "superseded",
                    "subscription_id": spec.subscription_id,
                }
            },
        )
        return None
    except Exception:
        logger.warning(
            "Client servability write failed; the record keeps its null caveat",
            exc_info=True,
            extra={"fields": {"event": "notification_servability", "result": "write_failed"}},
        )
        return None


def _expected_version(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    value = if_match.strip().strip('"').removeprefix("W/").strip('"')
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="if_match_must_be_a_version_integer") from exc


def _one_snapshot(
    document_id: str, client_id: str, *, served: int = notification_store.MAX_ALERTS_SERVED
) -> AlertStateOut:
    """One watch's alert state: ONE indexed query, and never a reason to fail the request.

    A read of what the notification pass has already committed -- ``alert_subjects`` holds
    one document per (watch, train) with its revision and phase -- so nothing here consults the
    matcher, ``/impact`` or a prediction cache. That is what makes it affordable on a request path,
    and it is why this is a read rather than a re-evaluation.

    A failure degrades to :func:`_unknown_alerts` rather than 503ing the route: the subscription
    itself was read successfully and is worth serving, and an alert state that cannot be established
    is a state the response can express.
    """
    try:
        return _alerts_out(
            _store.subscription_alerts(document_id, client_id=client_id, served=served)
        )
    except Exception:
        logger.warning(
            "Client alert-state read failed; serving it as unknown",
            exc_info=True,
            extra={"fields": {"event": "notification_alert_state", "result": "error"}},
        )
        return _unknown_alerts()


# ---- Routes -----------------------------------------------------------------------------------


@router.post(
    "/subscriptions",
    response_model=SubscriptionOut,
    tags=[TAG_SUBSCRIPTIONS],
    summary="Create or replace a watch by your own reference",
    responses={
        401: {"description": "Missing, unknown, revoked or wrong credential."},
        403: {"description": "The client is suspended."},
        409: {"description": "The reference is tombstoned, or the version moved under you."},
        # TWO shapes at this status, and the authored contract now says so -- this string is
        # the copy FastAPI renders into the app's own /docs, and it named only the first.
        # `Error` with a string `detail` is a refusal this application made; the framework's
        # own array shape reaches a caller before any handler here runs.
        422: {"description": "Malformed window, weekdays, stops or destination (Error), or "
                             "a schema refusal the framework made first (ValidationError)."},
        429: {"description": "A limiter tier or an R21 ceiling. Retry-After is bounded."},
        503: {"description": "The channel is not configured here, or the store is unavailable."},
    },
)
def create_or_replace_subscription(
    principal: _Principal,
    body: SubscriptionIn,
    response: Response,
    if_match: Annotated[
        str | None,
        Header(
            alias="If-Match",
            description="The version you last read. A mismatch is 409 rather than a silent "
            "overwrite; omit it to replace unconditionally.",
        ),
    ] = None,
) -> SubscriptionOut:
    """Register or amend one commuter's recurring journey. Idempotent on (reference, definition).

    An identical retry writes nothing but the inactivity horizon: the store's create path
    deliberately performs NO write for a same-definition retry (so a retry cannot bump a version,
    reset a horizon or clear a spent cap slot), and this route then calls ``touch_subscription``,
    because a client re-POSTing its watch IS the "still interested" signal R5's inactivity horizon
    is measured from. A GET does not touch, for the same reason: reading is not interest.

    A tombstoned reference is **not** resurrected -- the id is spent, its record is the audit trail,
    and a re-registration is a new subscription. That is a 409, and the window in which it matters
    is the one between the tombstone and the cleanup completing, where a same-definition re-POST
    would otherwise look exactly like a safe retry.

    One limitation worth stating rather than discovering: /varsel rewrites stored registrations onto
    canonical stop refs when the registry rebuilds (``varsel._canonicalize_journeys``), and that
    rewrite reaches the GCS state blob, not Firestore. A webhook subscription is canonicalised on
    the way IN and never afterwards, so a ref that is collapsed later keeps whatever it was stored
    with. Re-POSTing the same reference re-canonicalises it.
    """
    _checked_destination(principal, body.destination_id)
    spec = _spec(principal, body)
    try:
        # /varsel's own three window rules, reached rather than restated (module docstring, point
        # 4). A `Conflict` from HERE is a malformed request (422); a `Conflict` from a store call
        # below is a race or a spent id (409).
        spec.validate()
    except Exception as exc:
        raise _http(exc, validating=True) from exc

    expected = _expected_version(if_match)
    try:
        existing = _store.get_subscription(spec.subscription_id, client_id=principal.client_id)
    except Exception as exc:
        raise _http(exc) from exc

    if existing is not None:
        if expected is not None and existing.version != expected:
            raise HTTPException(status_code=409, detail="version_conflict")
        if _same_definition(spec, existing):
            try:
                touched = _store.touch_subscription(
                    spec.subscription_id,
                    client_id=principal.client_id,
                    daily_cap=principal.quotas.daily_cap,
                )
            except Exception as exc:
                raise _http(exc) from exc
            return _out(principal.client_id, touched)
        try:
            replaced = _store.replace_subscription(
                spec, expected_version=expected, daily_cap=principal.quotas.daily_cap
            )
        except Exception as exc:
            raise _http(exc) from exc
        # A changed route is a different servability question, and the store has already retired the
        # old answer, so this re-asks rather than carrying a stale verdict forward (R6).
        return _out(
            principal.client_id,
            _record_servability(spec, expected_version=replaced.version) or replaced,
        )

    if expected is not None:
        # An If-Match on a reference that does not exist cannot be satisfied, and creating anyway
        # would silently ignore a precondition the caller asked for.
        raise HTTPException(status_code=409, detail="version_conflict")
    try:
        created = _store.create_subscription(
            spec,
            max_active=principal.quotas.max_active_subscriptions,
            daily_cap=principal.quotas.daily_cap,
        )
    except notification_store.QuotaExceeded as exc:
        # R21's "a breach is visible to an operator". The store logs its pending-events ceiling and
        # its delivery ceilings itself; the active-subscription one is refused inside a transaction
        # body, which may run more than once and therefore logs NOTHING by design -- so this is the
        # only place the breach can be recorded once per request. Deliberately not done for a
        # `RateLimited`: a limiter that logs every rejection floods on exactly the traffic it is
        # rejecting, which is why `feedback._claim` does not either.
        logger.warning(
            "Client %s is at the active-subscription ceiling (%d): %s",
            principal.client_id,
            principal.quotas.max_active_subscriptions,
            exc,
            extra={
                "fields": {
                    "event": "notification_quota",
                    "result": "subscriptions",
                    "client_id": principal.client_id,
                    "key_id": principal.key_id,
                    "n": principal.quotas.max_active_subscriptions,
                }
            },
        )
        raise _http(exc) from exc
    except Exception as exc:
        raise _http(exc) from exc
    response.status_code = 201
    return _out(
        principal.client_id,
        _record_servability(spec, expected_version=created.version) or created,
    )


@router.get(
    "/subscriptions",
    response_model=SubscriptionList,
    tags=[TAG_SUBSCRIPTIONS],
    summary="Your own watches, with servability and freshness",
)
def list_subscriptions(
    principal: _Principal,
    limit: Annotated[int, Query(ge=1, le=_LIST_MAX, description="Page size.")] = _LIST_DEFAULT,
    after: Annotated[
        str | None,
        Query(max_length=128, description="A previous page's next_cursor."),
    ] = None,
) -> SubscriptionList:
    """Every live watch this credential owns, oldest first.

    **The caller's page size reaches the query**, through a real Firestore cursor on the ordering
    ``subscriptions`` already declares. Until 2026-09-09 it did not: the store streamed up
    to ``MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT`` (500) subscription documents and this function
    sliced afterwards, so `?limit=1` -- the natural shape for a paginated dashboard -- cost exactly
    what `?limit=500` did. Against ``notification_auth.PER_CLIENT_PER_MIN`` (120) that is a read
    amplification a client drives with their own polling and cannot see.

    **What one page costs now, as a number**: one document to resolve ``after`` (none on the first
    page), ``limit + 1`` subscription documents -- the extra one is what detects ``next_cursor``
    without a second query -- and up to ``MAX_SUBJECTS_PER_READ`` (500) alert-subject documents from
    the one client-scoped alert-state query below. That last read is deliberately NOT narrowed to
    the page: one query for every row's state is the trade recorded beside it, and narrowing it
    would buy back the documents at the price of N+1 queries.

    A cursor the current set does not contain is a 400 rather than an empty page, for
    ``historikk._checked_choice``'s reason: "that watch is gone" and "nothing matched" are different
    answers and a client that cannot tell them apart will show the second when it means the first.
    The store answers that with ``NotFound``, which is why it is caught by name here -- through
    ``_http`` it would become a 404 about the listing rather than a 400 about the cursor.
    """
    try:
        # `limit + 1`: one row past the page is what says there IS a next page, and it costs one
        # document rather than a second query. `_LIST_MAX` is the store's own ceiling, so the `+ 1`
        # cannot push the read past what R21 already allows.
        subs = _store.list_subscriptions(
            client_id=principal.client_id,
            limit=min(limit + 1, _LIST_MAX),
            after=_requested_id(principal.client_id, after) if after is not None else None,
        )
    except notification_store.NotFound as exc:
        raise HTTPException(status_code=400, detail="unknown_cursor") from exc
    except Exception as exc:
        raise _http(exc) from exc
    # ONE query for every row's alert state, never one per row: a page can hold hundreds of
    # watches, and a per-row read would be hundreds of queries. A bounded or failed read marks
    # EVERY row truncated -- including the rows whose own subjects fitted -- because a watch crowded
    # out of the page would otherwise be indistinguishable from a calm one.
    try:
        loaded = _store.client_alerts(principal.client_id, served=_served(principal))
        snapshots, alerts_truncated = loaded.snapshots, loaded.truncated
    except Exception:
        logger.warning(
            "Client alert-state read failed for a list page; serving every row as unknown",
            exc_info=True,
            extra={"fields": {"event": "notification_alert_state", "result": "error"}},
        )
        snapshots, alerts_truncated = {}, True
    rows = [
        _out(
            principal.client_id,
            s,
            _alerts_out(snapshots[s.subscription_id])
            if s.subscription_id in snapshots
            else (
                _unknown_alerts()
                if alerts_truncated
                else _alerts_out(notification_store.empty_alert_snapshot(s.subscription_id))
            ),
        )
        for s in subs
    ]
    page = rows[:limit]
    return SubscriptionList(
        count=len(page),
        limit=limit,
        next_cursor=page[-1].subscription_id if len(rows) > len(page) and page else None,
        subscriptions=page,
    )


@router.get(
    "/subscriptions/{subscription_id}",
    response_model=SubscriptionOut,
    tags=[TAG_SUBSCRIPTIONS],
    summary="One watch",
    responses={404: {"description": "No such watch for this credential."}},
)
def get_subscription(
    principal: _Principal,
    subscription_id: Annotated[str, Path(max_length=257)],
) -> SubscriptionOut:
    """One watch. Missing, tombstoned, expired and belonging to another client are all 404.

    Deliberately one answer for four states. A tombstone is not served because it carries no live
    definition; a foreign record is not distinguished because the difference between a 403 and a 404
    is how a client would enumerate another's references (AE11).
    """
    try:
        sub = _store.get_subscription(
            _requested_id(principal.client_id, subscription_id),
            client_id=principal.client_id,
        )
    except Exception as exc:
        raise _http(exc) from exc
    if sub is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _out(
        principal.client_id,
        sub,
        _one_snapshot(sub.subscription_id, principal.client_id, served=_served(principal)),
    )


@router.delete(
    "/subscriptions/{subscription_id}",
    response_model=DeleteResult,
    tags=[TAG_SUBSCRIPTIONS],
    summary="Stop a watch: tombstone, then durable cleanup",
)
def delete_subscription(
    principal: _Principal,
    subscription_id: Annotated[str, Path(max_length=257)],
) -> DeleteResult:
    """Ordered and durable (R13). Repeated deletion succeeds, and so does deleting nothing.

    The tombstone lands first, in one transactional write that stops routing -- the poller's load
    query filters ``status == "active"`` -- and only then are the events, deliveries and alert
    subjects deleted. Reversed, a partial failure leaves a ROUTABLE subscription whose events are
    already gone, which is the failure this order exists to prevent.

    Cleanup runs in bounded batches with an auditable completion state on the tombstone itself
    (``cleanup.state``, ``cleanup.attempts``, ``cleanup.completed_at``), and the tombstone's route,
    window, weekdays and client reference are redacted the moment it completes. Where it has not
    completed this reports ``pending``: a repeated DELETE resumes it -- which is what makes it a
    retryable task rather than a best-effort one -- and TTL is the fallback, with
    ``residual_hours`` publishing the maximum window rather than implying it.

    Cleanup deliberately does NOT reach the in-process wording caches: they key on the journey
    definition, which under KTD3 is exactly what this subscription shares with any browser
    registration on the same route, so removing one would remove the other's. Those entries expire
    with their situation.
    """
    document_id = _requested_id(principal.client_id, subscription_id)
    try:
        tombstone = _store.delete_subscription(document_id, client_id=principal.client_id)
    except notification_store.Forbidden:
        # Collapsed into "there was nothing here", exactly as the reads are: a delete that answered
        # 403 for a foreign id would be an enumeration oracle with a side effect attached.
        tombstone = None
    except Exception as exc:
        raise _http(exc) from exc
    if tombstone is None:
        return DeleteResult(
            subscription_id=_reference(principal.client_id, document_id),
            status="deleted",
            existed=False,
            cleanup=None,
        )
    deleted = {"events": 0, "deliveries": 0, "subjects": 0}
    passes = 0
    done = False
    for _ in range(_CLEANUP_PASSES):
        try:
            result = _store.cleanup_subscription(document_id, batch=_CLEANUP_BATCH)
        except Exception:
            # Never fails the DELETE: the tombstone is what stops routing and it is already
            # committed, so a cleanup failure is a retention question rather than a routing one --
            # and it is reported as `pending` with the residual window beside it.
            logger.warning(
                "Client cleanup pass failed; the tombstone stands and TTL is the fallback",
                exc_info=True,
                extra={"fields": {"event": "notification_cleanup", "result": "error"}},
            )
            break
        passes += 1
        deleted["events"] += result.deleted_events
        deleted["deliveries"] += result.deleted_deliveries
        deleted["subjects"] += result.deleted_subjects
        if result.done:
            done = True
            break
    return DeleteResult(
        subscription_id=_reference(principal.client_id, document_id),
        status="deleted",
        existed=True,
        cleanup=CleanupState(
            state="done" if done else "pending",
            passes=passes,
            deleted_events=deleted["events"],
            deleted_deliveries=deleted["deliveries"],
            deleted_subjects=deleted["subjects"],
            residual_hours=notification_store.TTL_RESIDUAL_HOURS,
        ),
    )


@router.get(
    "/destinations/current",
    response_model=DestinationOut,
    tags=[TAG_DESTINATIONS],
    summary="Destination readiness and signing key ids",
    responses={404: {"description": "No destination is provisioned for this client."}},
)
def current_destination(principal: _Principal) -> DestinationOut:
    """Your provisioned callback, its pause state, and which signing generations are live.

    Generation NUMBERS only. A secret is derived from a root over ``destination_id || generation``
    and is stored nowhere, so this endpoint has nothing to leak -- and it never calls the derivation
    either, which is what keeps "the API cannot produce a signing secret" a property of the code
    rather than of the response shape.
    """
    try:
        destination = _auth.current_destination(principal.client_id)
    except Exception as exc:
        raise _http(exc) from exc
    if destination is None:
        raise HTTPException(status_code=404, detail="no_destination")
    return DestinationOut(
        destination_id=destination.destination_id,
        callback_url=destination.callback_url,
        status=destination.status,
        paused=destination.paused,
        paused_at=destination.paused_at,
        paused_reason=destination.paused_reason,
        ready=destination.deliverable and webhook_signing.configured(),
        signing_generations=SigningGenerations(
            active=destination.active_generation,
            retiring=destination.retiring_generation,
        ),
    )


def _claim_test_event(client_id: str) -> None:
    now = time.monotonic()
    with _lock:
        bucket = _test_event_calls.setdefault(client_id, deque())
        # `notification_auth`'s window, not a third copy of it: this route's limiter is the same trailing
        # window as the two credential tiers and had been written out again here.
        if not notification_auth.claim_window(
            bucket, _TEST_EVENTS_PER_MIN, now, window_s=_TEST_EVENTS_WINDOW_S
        ):
            raise HTTPException(
                status_code=429,
                detail="test_events_rate_limited",
                headers={"Retry-After": str(_TEST_EVENTS_WINDOW_S)},
            )
        while len(_test_event_calls) > 1_000:
            _test_event_calls.pop(next(iter(_test_event_calls)))


def _synthetic_event(
    sub: notification_store.Subscription, *, now: datetime, nonce: str
) -> dict[str, Any]:
    """One synthetic event, in the shape the store writes and the delivery pass projects.

    ``test: true`` rides INSIDE the body rather than in a header, which is the whole of R15: a
    header can be stripped by a proxy, while the marker inside the signed bytes is covered by the
    signature and a receiver can refuse it before native dispatch.

    **The payload is the one `varsel._webhook_payload` builds, key for key**, and that is the point
    of this function rather than a detail of it. A client writes their receiver against what a test
    event contains -- it is the only body they can see before a real disruption -- so a synthetic
    payload with its own key names teaches a shape that breaks on the first real event, in
    production, at go-live. ``test_the_synthetic_payload_has_the_same_shape_as_a_real_one`` compares
    the two producers directly, in both directions, because the schema alone cannot catch this:
    ``payload`` is deliberately open (its sub-objects carry no property list), so a differently
    named key is schema-VALID and wrong.

    The event is a coherent **confirmed cancellation**, which is what the wording says. Not a
    detail either: the schema's own rule is that both probabilities are 1.0 and the delay figures
    null when ``phase`` is ``confirmed``, and a synthetic event that stated 1.0 under
    ``predicted`` with a null delay would be teaching a combination no real event produces.

    ``alert_id`` and ``event_id`` are RANDOM here, unlike a real event's, which are deterministic
    over the subject and the revision. Deliberately: the supplied receiver policy drops a duplicate
    event id and a revision not strictly newer than the highest applied for that train, so two test
    events derived from the same synthetic train would give the client exactly one notification
    however many times they asked. The train is a ``KIX:Test:`` journey ref, so it can never collide
    with a real subject.
    """
    situation_number = f"KIX:TestEvent:{nonce}"
    aimed = now + timedelta(minutes=45)
    return {
        "schema_version": notification_store.SCHEMA_VERSION,
        "event_id": f"test-{nonce}",
        "type": notification_store.EVENT_CREATED,
        "phase": notification_store.PHASE_CONFIRMED,
        "alert_id": f"test-{nonce}",
        "revision": notification_store.revision_seed(now),
        "occurred_at": now.isoformat(),
        "valid_until": notification_store.event_validity(
            occurred_at=now, departure_at=aimed, subscription_expires_at=sub.expires_at
        ).isoformat(),
        "subscription_id": sub.subscription_id,
        "subscription_version": max(1, sub.version),
        "external_ref": sub.external_ref,
        "test": True,
        "operator": "VYG",
        "line": "VYG:Line:L1",
        "journey_ref": f"KIX:Test:{nonce}",
        "call_date": aimed.astimezone(notification_store.OSLO).date().isoformat(),
        "stop_place": sub.from_stop,
        "stop_order": 1,
        # Strings at the top level, rich objects inside `payload.situations` -- the store's own
        # split, not two ways of saying one thing.
        "situations": [situation_number],
        "payload": {
            "notification": {
                # Norwegian, like every traveller-facing string in this repo, and marked as a test
                # in the text itself -- the same choice `/togmelding`'s test round makes, so a
                # notification that reaches a real lock screen during integration says so.
                "title": "Testvarsel: Tog L1 er innstilt",
                "body": (
                    f"{sub.from_stop} → {sub.to_stop}\n"
                    "Dette er et testvarsel fra Entur. Ingen virkelige avganger er berørt."
                ),
                "action": None,
            },
            "call": {
                "stop_place": sub.from_stop,
                "stop_order": 1,
                "aimed_departure_time": aimed.isoformat(),
                "departs_tomorrow": False,
            },
            # A declared cancellation reads (1.0, 1.0, NULL) by construction, and the threshold is
            # carried so a client can see WHY it cleared the bar rather than inferring one.
            #
            # `delay_ge10` is the null, and it stated 1.0 until 2026-09-09. `impact
            # ._cancelled_prediction` serves `ge10_probability=None` for every declared-cancelled
            # call, because every delay figure meant "if it runs" -- so a real confirmed event
            # carries null there and this taught a combination no real event produces, in the one
            # body a client types their receiver against before a real disruption.
            #
            # `has_situation=False`, matching the situation below, and the pair is the whole point:
            # a real event's `alert_threshold` is `alert.alert_threshold`, which is
            # `alert_policy.gate(..., has_situation=sit.has_situation)`'s -- so an unnamed train
            # carries the no-SX cut (0.85 for VYG) and never the published one (0.65). Stating 0.65
            # beside `has_situation: false` would be a combination no real event produces, which is
            # exactly what this function was fixed to stop teaching.
            "probabilities": {
                "impact": 1.0,
                "cancellation": 1.0,
                "delay_ge10": None,
                "alert_threshold": alert_policy.threshold_for("VYG", has_situation=False),
            },
            # Null, because every delay figure meant "if it runs".
            "delay": None,
            "effect": "cancelled",
            "situations": [
                {
                    "situation_number": situation_number,
                    "summary": "Testvarsel fra Entur",
                    "description": (
                        "Syntetisk hendelse sendt fra /notification-api/v1/test-events. "
                        "Ingen virkelige avganger er berørt."
                    ),
                    # False, and truthfully: no SX message named this train, so `summary` is text
                    # this codebase wrote rather than an operator's. A receiver presenting it as an
                    # operator statement would be making the 2026-08-20 mistake one layer out --
                    # and a test event is exactly where that habit would be learned.
                    "has_situation": False,
                }
            ],
            "alternative_journey": None,
        },
    }


@router.post(
    "/test-events",
    response_model=TestEventOut,
    tags=[TAG_TEST],
    summary="Send one signed synthetic event to your callback",
    responses={
        409: {"description": "The watch is not test-only, or the destination cannot be dialled."},
        429: {"description": "The per-client test-event ceiling."},
        503: {"description": "Signing is not configured in this deployment."},
    },
)
def send_test_event(principal: _Principal, body: TestEventIn) -> TestEventOut:
    """A signed synthetic event, restricted to a subscription created with ``test: true`` (R15).

    **Refused for a live subscription** (AE18): the marker is set at creation and immutable, so the
    only way to a test event is a watch that was always one. That refusal is the point of the
    marker -- a subscription that could be flipped live would turn every previously-signed test
    event into a real notification.

    Sent synchronously and **nothing is stored**. Not for convenience: ``notification_store``'s
    pending-event ceiling is exact rather than best-effort *because the emit path has exactly one
    writer* -- one poller thread, no route invoking the pass, one replica under the Recreate
    strategy -- so a route that called ``commit_event`` would falsify that claim for every quota
    check in that module. No event, no delivery and no alert subject is written here, and the daily
    cap is untouched.

    Answers 200 with the outcome rather than mapping a client's own failure onto our status code:
    this is a diagnostic endpoint and the classification (their status, their error, which
    generations signed it) is the diagnosis. A refusal on OUR side -- not a test subscription, a
    paused destination, no signing root -- is a 4xx/503 as usual.
    """
    _claim_test_event(principal.client_id)
    try:
        sub = _store.get_subscription(
            _requested_id(principal.client_id, body.subscription_id),
            client_id=principal.client_id,
        )
    except notification_store.Forbidden:
        sub = None
    except Exception as exc:
        raise _http(exc) from exc
    if sub is None:
        raise HTTPException(status_code=404, detail="not_found")
    if not sub.test:
        raise HTTPException(status_code=409, detail="not_a_test_subscription")
    try:
        destination = _auth.get_destination(sub.destination_id, client_id=principal.client_id)
    except notification_auth.Forbidden:
        destination = None
    except Exception as exc:
        raise _http(exc) from exc
    if destination is None:
        raise HTTPException(status_code=409, detail="destination_missing")
    if not destination.deliverable:
        raise HTTPException(status_code=409, detail="destination_paused")
    if not webhook_signing.configured():
        raise HTTPException(
            status_code=503,
            detail="signing_not_configured",
            headers={"Retry-After": str(notification_auth.RETRY_AFTER_S)},
        )

    now = datetime.now(UTC)
    nonce = secrets.token_hex(12)
    event = _synthetic_event(sub, now=now, nonce=nonce)
    # Projected and serialized by the DELIVERY PASS's own two functions, not by a second copy here.
    # `wire_event` is the allow-list that decides what leaves this process -- so a field added to
    # the store's event, or a property added to the schema, changes both paths at once instead of
    # leaving the test route shipping last month's envelope. `event_body` is the one serializer:
    # sorted keys, UTF-8, timestamps rendered one way, and the bytes built ONCE and signed
    # unmodified (KTD7 -- a signature over re-serialised JSON covers a document the receiver did
    # not get, which is the canonical webhook bug every provider's documentation warns about).
    payload = webhook_delivery.event_body(webhook_delivery.wire_event(event))
    signer = _signer or webhook_signing.sign
    signed = signer(
        destination_id=destination.destination_id,
        generations=destination.signing_generations,
        webhook_id=event["event_id"],
        body=payload,
    )
    # ON A ONE-WIDE POOL, NOT INLINE, and the reason is DNS. `webhook_sender`'s two ceilings are
    # `socket.settimeout` values on a connection that already exists, and `_default_resolver` calls
    # `socket.getaddrinfo` before either applies -- a blocking C call with no deadline the Python
    # layer can express and no way to interrupt it. The delivery PASS contains that inside its own
    # bounded wait; this route is a plain `def` sharing the ~40-slot anyio worker pool with
    # `/varsel-api`'s registration writes and `/impact`, so a stalled resolver held one of those
    # workers for as long as the resolver took, and `_TEST_EVENTS_PER_MIN` bounds REQUESTS rather
    # than workers held. `notification_events.emit`'s shape, with the same honest residual: the thread is
    # abandoned rather than killed, because Python cannot cancel one parked in a syscall -- and here
    # the per-client ceiling is also what bounds how many can be leaked.
    #
    # The wait is the attempt's own worst case plus a small margin for the resolution that precedes
    # it, so an ordinary slow client still reports THEIR failure (a 500, a timeout, a refusal) and
    # only a hang this module cannot otherwise see reaches the backstop.
    deliver = _deliver or webhook_sender.deliver
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="client-test-event")
    try:
        attempt = pool.submit(
            deliver,
            destination.callback_url,
            body=signed.body,
            headers={**signed.headers, "Content-Type": "application/json"},
        ).result(timeout=_TEST_EVENT_WAIT_S)
    except FuturesTimeout:
        # A 200 carrying the outcome, like every other delivery failure on this route: the request
        # succeeded and the diagnosis is that their endpoint did not answer. A 504 here would read
        # as OUR failure, which is the opposite of what this endpoint is for.
        logger.warning(
            "A client test event did not return inside its wait; the worker was abandoned",
            extra={
                "fields": {
                    "event": "notification_test_event",
                    "result": "timeout",
                    "client_id": principal.client_id,
                    "key_id": principal.key_id,
                    "destination_id": destination.destination_id,
                }
            },
        )
        attempt = webhook_sender.DeliveryAttempt(ok=False, retryable=True, error="timeout")
    finally:
        # NEVER wait: `shutdown(wait=True)` would block this request until the stalled call returned
        # and defeat the bound completely.
        pool.shutdown(wait=False)
    logger.info(
        "Client test event to %s: ok=%s status=%s",
        destination.destination_id,
        attempt.ok,
        attempt.status,
        extra={
            "fields": {
                "event": "notification_test_event",
                "result": "ok" if attempt.ok else "failed",
                "client_id": principal.client_id,
                "key_id": principal.key_id,
                "destination_id": destination.destination_id,
                "status": attempt.status,
            }
        },
    )
    return TestEventOut(
        event_id=str(event["event_id"]),
        subscription_id=_reference(principal.client_id, sub.subscription_id),
        destination_id=destination.destination_id,
        delivered=bool(attempt.ok),
        status=attempt.status,
        error=attempt.error,
        signed_generations=list(destination.signing_generations),
    )


__all__ = [
    "PREFIX",
    "TAG_DESTINATIONS",
    "TAG_SUBSCRIPTIONS",
    "TAG_TEST",
    "AlertStateOut",
    "AlertSubjectOut",
    "CleanupState",
    "DeleteResult",
    "DestinationOut",
    "SigningGenerations",
    "SubscriptionIn",
    "SubscriptionList",
    "SubscriptionOut",
    "TestEventIn",
    "TestEventOut",
    "configure",
    "router",
]
