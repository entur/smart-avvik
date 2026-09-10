"""The hardened outbound client for webhook delivery (KTD9, R18).

Built on :mod:`urllib.request`, this repo's outbound client (`alternatives._post_json`,
`reisevalg._post_json`) -- no new dependency, and none is needed. What is different here is the
destination: the Journey Planner is an Entur host in a constant, while a client callback URL is a
value somebody typed into a record. Everything below follows from that one difference.

**The address test is POSITIVE, and that is the whole point.** An address is accepted only when it
is globally routable and not multicast; then the IPv4-embedding prefixes are decoded and the same
test is re-run on what they carry. A deny-list is the wrong shape for the claim, and this is
measurable rather than arguable -- against CPython 3.13.13 on 2026-09-08:

  * ``64:ff9b::a9fe:a9fe``, the NAT64 encoding of the GCE metadata address ``169.254.169.254``,
    reports ``is_global=True, is_private=False, is_multicast=False``. It therefore passes every
    named range a deny-list enumerates -- private, loopback, link-local, ULA ``fc00::/7``,
    IPv4-mapped ``::ffff:0:0/96`` -- and OWASP's own SSRF table omits three of those anyway. It is
    the one case where the embedded decode below does real work.
  * ``224.0.0.1``, ``239.255.255.250``, ``ff02::1`` and ``ff0e::1`` all report ``is_global=True``.
    So ``not is_multicast`` is load-bearing, not decoration.
  * ``100.64.0.1`` (CGNAT) reports ``is_global=False`` **and** ``is_private=False``: `is_private` is
    not the complement of `is_global`, so a deny-list written against `is_private` lets it through.
  * ``2002:a9fe:a9fe::`` (6to4) and ``::ffff:c0a8:1`` (the IPv4-mapped form that smuggled past
    SVIXSEC-2026-0001's range checks) already report ``is_global=False`` here. Both are decoded
    anyway: the IPv4-mapped delegation is a recent CPython behaviour, and a guarantee this module
    makes itself does not move when the stdlib does.

The named ranges are kept above as documented rationale. They are deliberately NOT the check.

**Resolve once, then connect to a validated literal while still sending the original SNI and Host.**
Validating and then re-requesting by hostname is a time-of-check/time-of-use race, and it is not
folklore: CVE-2026-27129 (Craft CMS, 2026-02) and SVIXSEC-2026-0001 (2026-07) are both that bug this
year, the second at a company whose entire product is sending webhooks. So the resolution happens
exactly once, every returned address in both families must pass, and the connection is made to one
of those literals -- with ``self.host`` left as the hostname, so the ``Host`` header and the TLS
``server_hostname`` (and therefore certificate verification) are unchanged. A host with one bad
answer among good ones is refused **entirely**, which is what defeats a multi-answer rebind.

**Redirects are refused by construction, not by a handler.** The opener carries no
``HTTPRedirectHandler`` and no ``HTTPErrorProcessor``, so a 3xx arrives as an ordinary response this
module classifies as terminal and never follows. It also carries no ``ProxyHandler``: a proxy read
out of ``https_proxy`` would send the request to an address nothing validated, which would undo the
pinning silently.

**The test-network fence raises before any socket opens.** `alternatives._network_allowed`'s shape,
and the reason is sharper here: a suite that forgets to stub the sender would dial a
*client-supplied host* from CI. The check sits immediately before ``socket.getaddrinfo`` and again
inside the real transport, so stubbing only one of the two seams still trips it.

**What this module does not do.** It looks nothing up, writes nothing durable and knows no
destination record. It returns a classified :class:`DeliveryAttempt`; the caller (U6) persists the
outcome through ``notification_store``. The delivery id and the ``webhook-id`` are the caller's to hold
across attempts -- see :mod:`webhook_signing`.

**Known bound, stated rather than fixed.** ``socket.settimeout`` is per-recv, not a total deadline,
and ``http.client`` reads response headers a line at a time -- so a client drip-feeding one byte
per second can hold the calling thread for several multiples of the read timeout. Closing that
properly means a non-blocking read loop, which is a rewrite of ``http.client``'s response parsing.
What contains it instead is one layer up: KTD6's delivery pass reserves its worst case against a
20 s budget, runs 4-wide, and skips a destination for the rest of the cycle after its first failure.
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from notification_store import RETRY_AFTER_CAP_S

logger = logging.getLogger(__name__)

# ---- Bounds ------------------------------------------------------------------------------------
#
# 5 s total and 2 s connect, per KTD6/KTD9. The pass that calls this is the poller's last phase and
# runs 4-wide against a 20 s budget, so a per-attempt ceiling is what keeps a dead client from
# turning into cycle time.

DEFAULT_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 2.0

#: A response body is read only to be discarded -- nothing here parses it. The cap is ordinary
#: resource-exhaustion hygiene rather than a webhook-specific practice (no provider documents one),
#: so it TRUNCATES and logs instead of failing the attempt: the status line already told us the
#: client accepted the event, and turning that into a retry would re-send it every cycle for the
#: event's whole 60-minute validity because of a byte count nobody reads.
MAX_RESPONSE_BYTES = 64 * 1024

#: A read timeout can never be zero -- `socket.settimeout(0)` puts the socket in non-blocking mode,
#: which `http.client` cannot drive -- so an already-spent deadline gets the smallest real timeout.
_MIN_READ_TIMEOUT_S = 0.05

ALLOWED_SCHEME = "https"

#: Only 443. A callback on another port is not refused because the port is dangerous; it is refused
#: because a destination record naming one is more likely a mistake or an internal service than a
#: client's public endpoint, and R18's blast radius is worth more than that flexibility.
ALLOWED_PORT = 443

USER_AGENT = "kix-avvik-notifications/1"

# ---- The test-network fence --------------------------------------------------------------------
#
# `alternatives._ALLOW_NETWORK_ENV`'s shape and semantics, with its own variable:
#   * truthy -> always allowed (a deliberate live check against a real endpoint)
#   * falsy  -> always blocked
#   * unset  -> allowed only outside a test process.
# The last line is what keeps the deployment safe: requiring the flag in prd would mean a forgotten
# Helm value silently costs every webhook delivery, forever, trading a loud test failure for a
# silent production one. Detection covers both ways the suites run -- `python test_x.py` (CI) and
# pytest (a developer).
ALLOW_NETWORK_ENV = "KIX_ALLOW_WEBHOOK_NETWORK"


class UnsafeDestination(ValueError):
    """The URL or one of its resolved addresses is refused. Terminal: retrying gains nothing.

    Deliberately a ``ValueError``, so U4 can call :func:`validate_url` at *provisioning* time and
    reject a bad destination when a human is present to fix it, rather than discovering it on the
    first delivery.
    """


class NetworkBlocked(RuntimeError):
    """A test process tried to make a real request. Raised before any socket opens."""


def _in_a_test_process() -> bool:
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return True
    argv0 = os.path.basename(sys.argv[0] if sys.argv else "")
    return argv0.startswith("test_") and argv0.endswith(".py")


def network_allowed() -> bool:
    """Whether this process may dial a client endpoint at all. See the note above."""
    flag = os.environ.get(ALLOW_NETWORK_ENV)
    if flag is not None:
        return flag.strip().lower() in ("1", "true", "yes", "on")
    return not _in_a_test_process()


def _require_network() -> None:
    if not network_allowed():
        raise NetworkBlocked(
            "Refusing a live webhook request from a test process: stub "
            f"webhook_sender.deliver's `transport` and `resolver` (or set {ALLOW_NETWORK_ENV}=1 "
            "to allow it). Unstubbed, this would dial a client-supplied host from CI."
        )


# ---- Address safety ----------------------------------------------------------------------------

#: RFC 6052 NAT64. The embedded IPv4 address is the low 32 bits.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
#: RFC 3056 6to4. The embedded IPv4 address is bytes 2..6.
_SIXTOFOUR = ipaddress.IPv6Network("2002::/16")
#: RFC 4291 IPv4-mapped. `IPv6Address.ipv4_mapped` decodes it.
_V4MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def embedded_addresses(ip: IPAddress) -> tuple[ipaddress.IPv4Address, ...]:
    """Every IPv4 address an IPv6 address encodes, for the prefixes that encode one.

    Returned as a tuple rather than an optional single value because the prefixes are disjoint today
    and a future one would simply add an entry, and because the caller's rule -- re-run the same
    test on each -- is the same however many there are.
    """
    if not isinstance(ip, ipaddress.IPv6Address):
        return ()
    found: list[ipaddress.IPv4Address] = []
    if ip in _V4MAPPED and ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip in _SIXTOFOUR and ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip in _NAT64:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return tuple(found)


def _routable(ip: IPAddress) -> bool:
    """The positive test itself: globally routable and not multicast."""
    return bool(ip.is_global) and not ip.is_multicast


def is_safe_address(value: str | IPAddress) -> bool:
    """Whether we may open a connection to this address.

    See the module docstring for what each clause is answering and the measurements behind it. Note
    that the RFC 5737/3849 documentation ranges (``192.0.2.0/24``, ``198.51.100.0/24``,
    ``203.0.113.0/24``, ``2001:db8::/32``) are all refused, as is CGNAT ``100.64.0.0/10`` -- a test
    reaching for an "example" address needs a real global one (``1.1.1.1``, ``8.8.8.8``,
    ``2606:4700:4700::1111``).
    """
    try:
        ip = (
            value
            if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address))
            else ipaddress.ip_address(value)
        )
    except ValueError:
        return False
    if not _routable(ip):
        return False
    # The second pass, and the reason the first is not enough. Measured: the naive check accepts
    # `64:ff9b::a9fe:a9fe` -- it is global, non-private, non-multicast and in none of the named
    # ranges -- while the address it carries is the metadata service. Re-running the SAME test on
    # what a prefix embeds is what makes the guarantee about the reachable host rather than about
    # the notation somebody wrote it in.
    return all(_routable(embedded) for embedded in embedded_addresses(ip))


# ---- URL and resolution ------------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """A callback URL that passed the syntactic checks. Not yet resolved."""

    url: str
    host: str
    port: int


def validate_url(url: str) -> Destination:
    """Check a callback URL's syntax, or raise :class:`UnsafeDestination`.

    Pure and network-free, so U4 calls it when a destination is provisioned or amended. Each refusal
    has its own reason string, because "which of these is wrong" is the whole question an operator
    reading the error has.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeDestination("empty callback URL")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise UnsafeDestination(f"unparseable callback URL: {exc}") from exc
    if parts.scheme != ALLOWED_SCHEME:
        raise UnsafeDestination(f"scheme must be {ALLOWED_SCHEME}, not {parts.scheme!r}")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        # Userinfo is refused rather than stripped: `https://good.example@evil.example/` reads as
        # `good.example` to a human and resolves `evil.example`, which is the point of putting it
        # there. Nothing legitimate needs it -- the signature is the authentication.
        raise UnsafeDestination("a callback URL must carry no userinfo")
    if parts.fragment:
        # A fragment is never transmitted, so one in a stored URL means the record is wrong.
        raise UnsafeDestination("a callback URL must carry no fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeDestination(f"unreadable port: {exc}") from exc
    if port is not None and port != ALLOWED_PORT:
        raise UnsafeDestination(f"port must be {ALLOWED_PORT}, not {port}")
    host = parts.hostname
    if not host:
        raise UnsafeDestination("a callback URL must name a host")
    return Destination(url=url, host=host, port=ALLOWED_PORT)


#: ``socket.getaddrinfo``'s shape, narrowed to what this module passes and reads.
Resolver = Callable[[str, int], Sequence[tuple]]


def _default_resolver(host: str, port: int) -> Sequence[tuple]:
    # The fence sits here rather than in `deliver`, because THIS is the first line that opens a
    # socket (DNS is UDP). A test that stubs the resolver has by definition not forgotten to.
    _require_network()
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


@dataclass(frozen=True)
class ResolvedDestination:
    """One resolution, validated. Every address in ``addresses`` passed :func:`is_safe_address`."""

    destination: Destination
    addresses: tuple[str, ...]

    @property
    def connect_address(self) -> str:
        """The literal to dial.

        The first validated answer, in the resolver's own order -- which on a dual-stack host is the
        system's RFC 6724 preference. Only the first is tried: one attempt per cycle is this repo's
        retry model everywhere, and walking every answer would multiply the per-attempt ceiling by
        the number of A/AAAA records a client happens to publish.
        """
        return self.addresses[0]


def _answer_addresses(answers: Iterable[tuple]) -> list[str]:
    """The address strings out of ``getaddrinfo`` tuples, ignoring anything unrecognizable."""
    found: list[str] = []
    for answer in answers:
        try:
            sockaddr = answer[4]
            address = str(sockaddr[0])
        except (IndexError, TypeError):
            continue
        if address and address not in found:
            found.append(address)
    return found


def resolve_and_validate(
    destination: Destination, *, resolver: Resolver | None = None
) -> ResolvedDestination:
    """Resolve once, validate every answer in both families, or refuse the destination entirely.

    All-or-nothing on purpose. Picking the good answers out of a mixed set would accept a host whose
    DNS is already answering with an internal address, which is exactly the rebind this guards
    against -- and it costs nothing, because a legitimate client endpoint has no private answers.
    """
    resolve = resolver or _default_resolver
    try:
        answers = resolve(destination.host, destination.port)
    except OSError as exc:
        raise UnsafeDestination(f"could not resolve {destination.host!r}: {exc}") from exc
    addresses = _answer_addresses(answers)
    if not addresses:
        raise UnsafeDestination(f"{destination.host!r} resolved to no usable address")
    refused = [address for address in addresses if not is_safe_address(address)]
    if refused:
        raise UnsafeDestination(
            f"{destination.host!r} resolves to {len(refused)} address(es) that are not globally "
            f"routable unicast: {', '.join(refused)}"
        )
    return ResolvedDestination(destination=destination, addresses=tuple(addresses))


# ---- Pinned transport --------------------------------------------------------------------------


def read_timeout(deadline: float, *, now: float | None = None) -> float:
    """What is left of the per-attempt budget, floored so the socket stays blocking."""
    instant = time.monotonic() if now is None else now
    return max(_MIN_READ_TIMEOUT_S, deadline - instant)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to a validated literal; ``Host`` and SNI still carry the hostname.

    ``self.host`` is deliberately left as the hostname, which is what ``putrequest`` writes into the
    ``Host`` header and what ``HTTPSConnection.connect`` passes as ``server_hostname`` -- so the
    certificate is verified against the name, not the pinned address. Only the socket's destination
    is overridden, by swapping the ``_create_connection`` attribute that ``HTTPConnection.__init__``
    installs.
    """

    def __init__(self, host: str, *, pinned_ip: str, deadline: float, **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip
        self._deadline = deadline

    def connect(self) -> None:
        original = self._create_connection

        def _pinned(address, timeout, source_address):
            return original((self._pinned_ip, address[1]), timeout, source_address)

        self._create_connection = _pinned  # type: ignore[method-assign]
        try:
            super().connect()
        finally:
            self._create_connection = original  # type: ignore[method-assign]
        # The connection was opened under the CONNECT timeout; the rest of the attempt shares what
        # is left of the total. See the module docstring on why this is a per-recv bound.
        if self.sock is not None:
            self.sock.settimeout(read_timeout(self._deadline))


#: ONE trust store for the process, not one per delivery attempt.
#: ``ssl.create_default_context()`` loads and parses the platform CA bundle every time it is
#: called, and it was called once per attempt -- inside the 5 s per-attempt ceiling that
#: ``webhook_delivery`` treats as scarce, for a bundle that is identical for every destination and
#: changes only on a redeploy. An ``SSLContext`` is safe to share across threads and across
#: connections, which is what makes this a memoization rather than a behaviour change: the same
#: default verification, the same pinning path, the same everything except the parse.
#:
#: Built lazily rather than at import so a module import in an environment with no readable trust
#: store fails when a delivery is attempted, where the error is diagnosable, rather than at import
#: time on the poller thread.
_default_context: ssl.SSLContext | None = None
_context_lock = threading.Lock()


def _shared_context() -> ssl.SSLContext:
    global _default_context
    with _context_lock:
        if _default_context is None:
            _default_context = ssl.create_default_context()
        return _default_context


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """``HTTPSHandler`` that builds :class:`_PinnedHTTPSConnection` instead of the default."""

    def __init__(self, *, pinned_ip: str, deadline: float, context: ssl.SSLContext | None = None):
        super().__init__(context=context or _shared_context())
        self._pinned_ip = pinned_ip
        self._deadline = deadline

    def https_open(self, req):
        def factory(host, **kwargs):
            return _PinnedHTTPSConnection(
                host, pinned_ip=self._pinned_ip, deadline=self._deadline, **kwargs
            )

        return self.do_open(factory, req, context=self._context)


#: The seam a suite replaces. Called as ``transport(resolved, request, connect_timeout_s, deadline)``
#: and returns something with ``status``, ``headers`` and ``read(n)`` -- i.e. an
#: ``http.client.HTTPResponse``, or a fake.
Transport = Callable[[ResolvedDestination, urllib.request.Request, float, float], object]


def _send(
    resolved: ResolvedDestination,
    request: urllib.request.Request,
    connect_timeout_s: float,
    deadline: float,
) -> object:
    """The real transport: one request, pinned, no redirects, no proxy, no cookies.

    The opener is built by hand rather than with ``build_opener`` because what it must NOT contain
    is the point. ``HTTPRedirectHandler`` absent means a 3xx is returned as a response instead of
    followed; ``HTTPErrorProcessor`` absent means every status arrives the same way and this module
    does the classifying; ``ProxyHandler`` absent means ``https_proxy`` cannot redirect the request
    to an address nothing validated; ``HTTPCookieProcessor`` was never there and stays away.
    """
    _require_network()
    opener = urllib.request.OpenerDirector()
    opener.add_handler(
        _PinnedHTTPSHandler(pinned_ip=resolved.connect_address, deadline=deadline)
    )
    return opener.open(request, timeout=connect_timeout_s)


# ---- Classification ----------------------------------------------------------------------------
#
# The Event contract's vocabulary, and it is contested in the wild rather than settled, so this
# states a choice: 410 pauses the destination, other 4xx are terminal for that event, and network
# errors, timeouts, 408, 429 and 5xx are retryable. (Stripe retries all 4xx; the Standard Webhooks
# spec does not; Svix's own docs disagree with each other. Do not average them.)
#
# 3xx is the one status the plan does not name. It is terminal: a redirect is a client
# configuration fault, the next attempt gets the same redirect, and following it is what the whole
# pinning exercise exists to prevent.

_RETRYABLE_STATUSES = frozenset({408, 429})


@dataclass(frozen=True)
class DeliveryAttempt:
    """One attempt's outcome, in the terms the caller persists.

    ``retryable`` and ``terminal_state`` are the two the store reads: a retryable attempt goes back
    to ``pending`` with ``retry_not_before``, and a terminal one is parked in ``terminal_state``
    (``cancelled`` for a gone destination, ``failed`` otherwise). ``pause_destination`` is the 410
    signal and is about the DESTINATION record rather than this delivery, so only U4/U6 can act on
    it.

    ``retry_after_s`` is already capped at :data:`notification_store.RETRY_AFTER_CAP_S`; the store caps it
    again against the time to departure. RFC 9110 puts no ceiling on the header, so a client can
    legally answer 999999 and the cap is purely our convention.
    """

    ok: bool
    status: int | None = None
    retryable: bool = False
    terminal_state: str | None = None
    pause_destination: bool = False
    retry_after_s: float | None = None
    error: str | None = None
    duration_ms: int = 0
    response_bytes: int = 0
    response_truncated: bool = False
    address: str | None = None


def classify_status(status: int) -> tuple[bool, bool, str | None, bool]:
    """``(ok, retryable, terminal_state, pause_destination)`` for one response status."""
    if 200 <= status < 300:
        return True, False, None, False
    if 300 <= status < 400:
        return False, False, "failed", False
    if status == 410:
        # RFC 9110's semantic, and the one delivery state that is not about this event: the endpoint
        # is gone. Mailchimp changed behaviour in 2021 to honour it; the Standard Webhooks spec says
        # disable. The event is `cancelled` rather than `failed` because nothing was wrong with it.
        return False, False, "cancelled", True
    if status in _RETRYABLE_STATUSES:
        return False, True, None, False
    if 400 <= status < 500:
        return False, False, "failed", False
    return False, True, None, False


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """``Retry-After`` as bounded seconds, or ``None`` when absent or unreadable.

    Both RFC 9110 forms: delta-seconds and an HTTP-date. Capped at
    :data:`notification_store.RETRY_AFTER_CAP_S`, and never negative -- a date already in the past means
    "now", not "go back in time".
    """
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(int(text))
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        instant = time.time() if now is None else now
        seconds = when.timestamp() - instant
    return max(0.0, min(seconds, float(RETRY_AFTER_CAP_S)))


def _response_header(response: object, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    return str(value) if value is not None else None


# ---- The one public call -----------------------------------------------------------------------


def deliver(
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    timeout_s: float = DEFAULT_TIMEOUT_S,
    connect_timeout_s: float = CONNECT_TIMEOUT_S,
    resolver: Resolver | None = None,
    transport: Transport | None = None,
) -> DeliveryAttempt:
    """POST ``body`` to a client callback, once, safely, and classify what happened.

    Never raises for a delivery outcome: every failure the client or the network can cause comes
    back as a :class:`DeliveryAttempt`, because the caller must record it and a raise on the poller
    thread is a hole in the pass. The two exceptions are deliberate and are both OUR faults rather
    than theirs -- :class:`NetworkBlocked` (a suite forgot to stub this) and a ``ValueError`` from
    signing headers that do not exist.

    ``body`` is sent byte for byte as given; it is the object :func:`webhook_signing.sign` signed.
    ``resolver`` and ``transport`` are the test seams -- see the module docstring on where the fence
    sits relative to each.
    """
    started = time.monotonic()
    deadline = started + timeout_s

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        destination = validate_url(url)
        resolved = resolve_and_validate(destination, resolver=resolver)
    except UnsafeDestination as exc:
        # Terminal, and NOT a reason to pause the destination: pausing is the 410 signal and means
        # "the client told us to stop". A destination record with a bad URL is an operator's to
        # correct, and it is visible as a failed delivery with this reason on it.
        logger.warning(
            "Refusing a client callback URL: %s",
            exc,
            extra={"fields": {"event": "webhook_delivery_refused", "reason": str(exc)}},
        )
        return DeliveryAttempt(
            ok=False,
            retryable=False,
            terminal_state="failed",
            error=f"unsafe_destination: {exc}",
            duration_ms=elapsed_ms(),
        )

    request = urllib.request.Request(
        destination.url,
        data=bytes(body),
        headers={
            # Explicit, because `AbstractHTTPHandler.do_request_` otherwise defaults an unset
            # Content-Type to `application/x-www-form-urlencoded` -- which a receiver framework may
            # then try to form-parse, leaving the raw bytes the signature covers unavailable.
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            **dict(headers),
        },
        method="POST",
    )
    send = transport or _send
    try:
        response = send(resolved, request, connect_timeout_s, deadline)
    except NetworkBlocked:
        raise
    except (urllib.error.URLError, http.client.HTTPException, ssl.SSLError, OSError) as exc:
        # `http.client.HTTPException` is in that tuple because `AbstractHTTPHandler.do_open` wraps
        # `h.request()` in a URLError but NOT `h.getresponse()` -- so a garbage status line
        # (`BadStatusLine`, `RemoteDisconnected`, `IncompleteRead`) propagates as an HTTPException,
        # which is not an OSError and would otherwise escape onto the poller thread.
        return DeliveryAttempt(
            ok=False,
            retryable=True,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=elapsed_ms(),
            address=resolved.connect_address,
        )

    try:
        status = int(getattr(response, "status", 0) or 0)
        retry_after = parse_retry_after(_response_header(response, "Retry-After"))
        raw = response.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    except (urllib.error.URLError, http.client.HTTPException, ssl.SSLError, OSError) as exc:
        return DeliveryAttempt(
            ok=False,
            status=None,
            retryable=True,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=elapsed_ms(),
            address=resolved.connect_address,
        )
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    truncated = len(raw) > MAX_RESPONSE_BYTES
    if truncated:
        logger.warning(
            "Client response body exceeded %d bytes and was truncated",
            MAX_RESPONSE_BYTES,
            extra={
                "fields": {
                    "event": "webhook_delivery_response_truncated",
                    "limit": MAX_RESPONSE_BYTES,
                }
            },
        )
    ok, retryable, terminal_state, pause = classify_status(status)
    return DeliveryAttempt(
        ok=ok,
        status=status,
        retryable=retryable,
        terminal_state=terminal_state,
        pause_destination=pause,
        retry_after_s=retry_after if retryable else None,
        # No client-supplied response text on the record, ever: R16 keeps client prose out of our
        # surfaces, and the status is what an operator needs to tell a 500 from a 403.
        error=None if ok else f"http_{status}",
        duration_ms=elapsed_ms(),
        response_bytes=len(raw),
        response_truncated=truncated,
        address=resolved.connect_address,
    )


__all__ = [
    "ALLOWED_PORT",
    "ALLOWED_SCHEME",
    "ALLOW_NETWORK_ENV",
    "CONNECT_TIMEOUT_S",
    "DEFAULT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "USER_AGENT",
    "DeliveryAttempt",
    "Destination",
    "NetworkBlocked",
    "ResolvedDestination",
    "Resolver",
    "Transport",
    "UnsafeDestination",
    "classify_status",
    "deliver",
    "embedded_addresses",
    "is_safe_address",
    "network_allowed",
    "parse_retry_after",
    "read_timeout",
    "resolve_and_validate",
    "validate_url",
]
