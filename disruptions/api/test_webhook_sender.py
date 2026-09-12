"""Checks for webhook_sender.py -- KTD9's outbound hardening, R18's bounds.

**This suite opens no socket, and enforces that rather than promising it.** ``socket.getaddrinfo``
and ``socket.create_connection`` are replaced at import with functions that raise, so any check that
reaches the network fails loudly instead of dialing a client-supplied host from CI. Address safety
is therefore tested against the RESOLVER -- a stub returning the addresses a hostile or misconfigured
DNS would -- and never against a live lookup, which could not produce ``64:ff9b::a9fe:a9fe`` on
demand anyway.

The one measurement this file exists to pin: ``64:ff9b::a9fe:a9fe`` NAT64-encodes the GCE metadata
address and CPython reports it as globally routable, non-private, non-multicast, non-reserved and
outside every named range a deny-list enumerates. ``test_a_deny_list_of_named_ranges_would_admit_
the_nat64_form`` asserts that state of affairs directly, so the argument for a positive test is a
failing assertion away from being re-checked rather than a claim in a comment.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import pathlib
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

# --- Fence the network before importing the module under test -----------------------------------


def _no_socket(*args, **kwargs):
    raise AssertionError(
        "this suite must open no socket; stub `resolver`/`transport` instead "
        f"(called with {args!r})"
    )


socket.getaddrinfo = _no_socket  # type: ignore[assignment]
socket.create_connection = _no_socket  # type: ignore[assignment]

import webhook_sender as wc

MODULE_SOURCE = pathlib.Path(wc.__file__).read_text()

DEST_URL = "https://hooks.client.example/kix/notifications"

# Real globally-routable addresses. The RFC 5737/3849 documentation ranges are NOT usable here --
# 192.0.2.x, 198.51.100.x, 203.0.113.x and 2001:db8:: all report is_global=False -- which is the
# first thing a future test author reaching for an "example" address will trip over.
GOOD_V4 = "1.1.1.1"
GOOD_V6 = "2606:4700:4700::1111"


def _answers(*addresses: str) -> list[tuple]:
    """``getaddrinfo``'s tuple shape for the given addresses, per family."""
    out: list[tuple] = []
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.version == 4:
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)))
        else:
            out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0)))
    return out


def _resolver(*addresses: str):
    def resolve(host: str, port: int) -> list[tuple]:
        return _answers(*addresses)

    return resolve


class _Headers:
    """Case-insensitive ``.get``, like ``http.client.HTTPResponse.headers``."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = {k.lower(): v for k, v in (values or {}).items()}

    def get(self, name: str, default=None):
        return self._values.get(name.lower(), default)


class _Response:
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"ok",
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = _Headers(headers)
        self._body = body
        self._read_error = read_error
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body if n < 0 else self._body[:n]

    def close(self) -> None:
        self.closed = True


class _Recorder:
    """A transport stub that records what it was handed and returns (or raises) what it was told."""

    def __init__(self, response=None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple] = []

    def __call__(self, resolved, request, connect_timeout_s, deadline, max_in_flight=None):
        self.calls.append((resolved, request, connect_timeout_s, deadline))
        if self.error is not None:
            raise self.error
        return self.response if self.response is not None else _Response()


def _code() -> str:
    """The module minus its docstring and comment lines.

    The headers here name what the transport deliberately does NOT use -- `build_opener`,
    `ProxyHandler`, redirects -- so a negative grep over the raw text matches the explanation and
    passes for the wrong reason. `test_webhook_delivery` has the same helper for the same reason.
    """
    body = MODULE_SOURCE.split('"""', 2)[-1]
    return "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))


def _deliver(**kwargs):
    # A FRESH pool per call. The pool caches one validated resolution per host for
    # `_RESOLVE_TTL_S`, which is the point of it in production and is exactly wrong in a suite where
    # every test injects a different resolver for the same host -- the second `deliver` would get
    # the first test's answer and pass or fail for a reason having nothing to do with its own
    # fixture. `test_the_pool_reuses_one_resolution_for_a_window` is where the caching itself is
    # asserted, deliberately, rather than being relied on everywhere by accident.
    wc.reset_pools_for_tests()
    kwargs.setdefault("url", DEST_URL)
    kwargs.setdefault("body", b'{"event_id":"e1"}')
    kwargs.setdefault("headers", {"webhook-id": "d1"})
    url = kwargs.pop("url")
    return wc.deliver(url, **kwargs)


# --- URL validation -----------------------------------------------------------------------------


def test_only_https_is_accepted():
    destination = wc.validate_url(DEST_URL)
    assert destination.host == "hooks.client.example"
    assert destination.port == wc.ALLOWED_PORT == 443
    for bad in ("http://hooks.client.example/x", "ftp://h/x", "hooks.client.example/x", ""):
        try:
            wc.validate_url(bad)
        except wc.UnsafeDestination:
            continue
        raise AssertionError(f"{bad!r} should have been refused")


def test_userinfo_is_refused_because_it_hides_the_real_host():
    # `https://good.example@evil.example/` reads as good.example to a human and resolves
    # evil.example. That is the entire reason userinfo appears in a hostile URL.
    for bad in (
        "https://good.example@evil.example/hook",
        "https://user:pass@hooks.client.example/hook",
        "https://@hooks.client.example/hook",
    ):
        try:
            wc.validate_url(bad)
        except wc.UnsafeDestination as exc:
            assert "userinfo" in str(exc)
            continue
        raise AssertionError(f"{bad!r} should have been refused")


def test_a_fragment_is_refused():
    try:
        wc.validate_url("https://hooks.client.example/hook#frag")
    except wc.UnsafeDestination as exc:
        assert "fragment" in str(exc)
        return
    raise AssertionError("a fragment should have been refused")


def test_a_non_default_port_is_refused_and_an_explicit_443_is_not():
    assert wc.validate_url("https://hooks.client.example:443/hook").port == 443
    for bad in ("https://hooks.client.example:8443/hook", "https://hooks.client.example:80/hook"):
        try:
            wc.validate_url(bad)
        except wc.UnsafeDestination as exc:
            assert "port" in str(exc)
            continue
        raise AssertionError(f"{bad!r} should have been refused")


def test_a_url_with_no_host_is_refused():
    for bad in ("https:///hook", "https://"):
        try:
            wc.validate_url(bad)
        except wc.UnsafeDestination:
            continue
        raise AssertionError(f"{bad!r} should have been refused")


def test_a_query_string_survives_validation():
    # Nothing wrong with one, and a client may well route on it.
    assert wc.validate_url("https://h.example/hook?tenant=a").url.endswith("?tenant=a")


# --- Address safety (scenario 2) ----------------------------------------------------------------


def test_every_named_range_a_deny_list_would_enumerate_is_refused():
    for bad in (
        "127.0.0.1",  # loopback
        "10.0.0.1",  # private
        "192.168.0.1",  # private
        "172.16.0.1",  # private
        "169.254.169.254",  # link-local: the GCE/AWS metadata address
        "0.0.0.0",  # unspecified
        "100.64.0.1",  # CGNAT: is_global False AND is_private False
        "192.0.2.1",  # RFC 5737 documentation
        "203.0.113.5",  # RFC 5737 documentation
        "::1",  # IPv6 loopback
        "::",  # IPv6 unspecified
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 ULA
        "fd20:ce::254",  # GCP metadata over IPv6 -- ULA, NOT fe80::/10
        "fd00:ec2::254",  # AWS metadata over IPv6 -- likewise
        "2001:db8::1",  # RFC 3849 documentation
        "::ffff:192.168.0.1",  # IPv4-mapped
        "::ffff:c0a8:1",  # the exact form that smuggled past SVIXSEC-2026-0001
        "::ffff:169.254.169.254",  # IPv4-mapped metadata
    ):
        assert not wc.is_safe_address(bad), f"{bad} must be refused"


def test_nat64_encoded_metadata_is_refused():
    # THE case the positive test exists for. See the next check for why a deny-list misses it.
    assert not wc.is_safe_address("64:ff9b::a9fe:a9fe")  # 169.254.169.254
    assert not wc.is_safe_address("64:ff9b::c0a8:1")  # 192.168.0.1
    assert not wc.is_safe_address("64:ff9b::7f00:1")  # 127.0.0.1
    assert not wc.is_safe_address("64:ff9b::")  # 0.0.0.0


def test_a_deny_list_of_named_ranges_would_admit_the_nat64_form():
    """The empirical argument for KTD9's inversion, as an assertion rather than a comment."""
    ip = ipaddress.ip_address("64:ff9b::a9fe:a9fe")
    assert ip.is_global is True
    assert ip.is_private is False
    assert ip.is_multicast is False
    assert ip.is_loopback is False
    assert ip.is_link_local is False
    assert ip.ipv4_mapped is None
    for named in ("fc00::/7", "fe80::/10", "::ffff:0:0/96", "::1/128", "2002::/16"):
        assert ip not in ipaddress.IPv6Network(named)
    # ...and it is still refused, because the test is positive and decodes what it embeds.
    assert not wc.is_safe_address(ip)


def test_sixtofour_encoded_addresses_are_refused():
    assert not wc.is_safe_address("2002:a9fe:a9fe::")  # 169.254.169.254
    assert not wc.is_safe_address("2002:c0a8:1::")  # 192.168.0.1
    # 6to4 is deprecated (RFC 7526) and CPython already reports the whole /16 as non-global, so a
    # 6to4 wrapper around a PUBLIC address is refused too. That is deliberate, not a side effect.
    assert not wc.is_safe_address("2002:808:808::")  # 8.8.8.8


def test_multicast_is_refused_although_python_calls_it_globally_routable():
    for bad in ("224.0.0.1", "239.255.255.250", "ff02::1", "ff0e::1"):
        assert ipaddress.ip_address(bad).is_global is True, f"{bad}: measurement moved"
        assert ipaddress.ip_address(bad).is_multicast is True
        assert not wc.is_safe_address(bad), f"{bad} must be refused"


def test_globally_routable_unicast_is_accepted():
    for good in (GOOD_V4, "8.8.8.8", GOOD_V6, "2001:4860:4860::8888"):
        assert wc.is_safe_address(good), f"{good} must be accepted"


def test_unparseable_addresses_are_refused_rather_than_raising():
    for bad in ("", "not-an-ip", "999.1.1.1", "1.1.1", "hooks.client.example"):
        assert not wc.is_safe_address(bad)


def test_embedded_addresses_decodes_all_three_ipv4_embedding_prefixes():
    def decode(value: str) -> list[str]:
        return [str(a) for a in wc.embedded_addresses(ipaddress.ip_address(value))]

    assert decode("64:ff9b::a9fe:a9fe") == ["169.254.169.254"]
    assert decode("2002:a9fe:a9fe::") == ["169.254.169.254"]
    assert decode("::ffff:169.254.169.254") == ["169.254.169.254"]
    assert decode(GOOD_V6) == []
    assert decode(GOOD_V4) == []


# --- Resolution (scenarios 2 and 3) -------------------------------------------------------------


def test_refusal_is_against_the_resolved_address_not_the_hostname():
    # A wholesome-looking hostname whose DNS answers with loopback. Nothing about the STRING is
    # wrong, which is the whole point of validating after resolution.
    destination = wc.validate_url(DEST_URL)
    try:
        wc.resolve_and_validate(destination, resolver=_resolver("127.0.0.1"))
    except wc.UnsafeDestination as exc:
        assert "127.0.0.1" in str(exc)
        return
    raise AssertionError("a hostname resolving to loopback must be refused")


def test_one_bad_answer_among_good_ones_refuses_the_whole_destination():
    destination = wc.validate_url(DEST_URL)
    try:
        wc.resolve_and_validate(
            destination, resolver=_resolver(GOOD_V4, "64:ff9b::a9fe:a9fe", GOOD_V6)
        )
    except wc.UnsafeDestination as exc:
        assert "64:ff9b::a9fe:a9fe" in str(exc)
        return
    raise AssertionError("a mixed answer set must be refused entirely, not filtered")


def test_both_families_are_validated_and_both_are_kept():
    destination = wc.validate_url(DEST_URL)
    resolved = wc.resolve_and_validate(destination, resolver=_resolver(GOOD_V4, GOOD_V6))
    assert resolved.addresses == (GOOD_V4, GOOD_V6)
    assert resolved.connect_address == GOOD_V4
    for bad in ("fd20:ce::254", "169.254.169.254"):
        try:
            wc.resolve_and_validate(destination, resolver=_resolver(GOOD_V4, bad))
        except wc.UnsafeDestination:
            continue
        raise AssertionError(f"an answer set containing {bad} must be refused")


def test_a_host_that_resolves_to_nothing_is_refused():
    destination = wc.validate_url(DEST_URL)
    try:
        wc.resolve_and_validate(destination, resolver=lambda h, p: [])
    except wc.UnsafeDestination as exc:
        assert "no usable address" in str(exc)
        return
    raise AssertionError("an empty answer set must be refused")


def test_a_resolver_failure_is_refused_rather_than_escaping():
    destination = wc.validate_url(DEST_URL)

    def boom(host, port):
        raise socket.gaierror("nope")

    try:
        wc.resolve_and_validate(destination, resolver=boom)
    except wc.UnsafeDestination as exc:
        assert "could not resolve" in str(exc)
        return
    raise AssertionError("a resolver failure must surface as UnsafeDestination")


def test_resolution_happens_once_and_a_changed_second_answer_cannot_be_reached():
    """Scenario 3: the time-of-check/time-of-use race CVE-2026-27129 and SVIXSEC-2026-0001 were."""
    calls: list[str] = []

    def flipping(host: str, port: int) -> list[tuple]:
        calls.append(host)
        return _answers(GOOD_V4) if len(calls) == 1 else _answers("169.254.169.254")

    recorder = _Recorder(_Response(204))
    attempt = _deliver(resolver=flipping, transport=recorder)
    assert attempt.ok is True
    assert len(calls) == 1, "the host must be resolved exactly once per attempt"
    resolved = recorder.calls[0][0]
    assert resolved.connect_address == GOOD_V4
    assert "169.254.169.254" not in resolved.addresses
    assert attempt.address == GOOD_V4


# --- Pinning (scenario 3, the connection half) --------------------------------------------------


class _FakeSock:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def setsockopt(self, *args) -> None:
        pass

    def settimeout(self, value) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        pass


class _FakeContext:
    def __init__(self, sock) -> None:
        self.sock = sock
        self.server_hostname = None

    def wrap_socket(self, sock, server_hostname=None, **kwargs):
        self.server_hostname = server_hostname
        return self.sock


def test_the_connection_dials_the_validated_literal_but_keeps_the_hostname_for_host_and_sni():
    sock = _FakeSock()
    connection = wc._PinnedHTTPSConnection(
        "hooks.client.example", pinned_ip=GOOD_V6, deadline=time.monotonic() + 4.0, timeout=2.0
    )
    dialed: list[tuple] = []

    def fake_create_connection(address, timeout, source_address):
        dialed.append((address, timeout))
        return sock

    connection._create_connection = fake_create_connection
    connection._context = _FakeContext(sock)
    connection.connect()

    assert dialed == [((GOOD_V6, 443), 2.0)], "the socket must go to the pinned literal"
    # Host header and certificate verification both key on this, and neither may see the literal.
    assert connection.host == "hooks.client.example"
    assert connection._context.server_hostname == "hooks.client.example"
    # The read budget is what is left of the total after the connect.
    assert sock.timeouts and 3.0 < sock.timeouts[-1] <= 4.0


def test_the_read_timeout_floors_at_a_blocking_value():
    # settimeout(0) puts the socket in non-blocking mode, which http.client cannot drive.
    assert wc.read_timeout(100.0, now=0.0) == 100.0
    assert wc.read_timeout(0.0, now=100.0) == wc._MIN_READ_TIMEOUT_S
    assert wc.read_timeout(0.0, now=0.0) == wc._MIN_READ_TIMEOUT_S


def test_nothing_can_follow_a_redirect_use_a_proxy_or_keep_a_cookie():
    """Refusing all three is structural: since U8 there is no urllib machinery here at all.

    It used to be an `OpenerDirector` carrying exactly one handler, so the guarantee was the ABSENCE
    of `HTTPRedirectHandler`, `ProxyHandler` and `HTTPErrorProcessor` from a list -- true, and one
    `build_opener` away from being false. The pooled transport drives `http.client` directly: a 3xx
    is a response this module classifies, there is no code that reads `https_proxy`, and no cookie
    jar exists to keep anything in. Asserted as the absence of the machinery rather than of the
    handlers, and matched as CALLS so a comment naming what is not used cannot satisfy it.
    """
    for forbidden in (
        r"build_opener\s*\(",
        r"OpenerDirector\s*\(",
        r"HTTPRedirectHandler",
        r"ProxyHandler",
        r"HTTPErrorProcessor",
        r"HTTPCookieProcessor",
    ):
        assert not re.search(forbidden, _code()), forbidden
    # `urllib.request.Request` survives as a value carrier for url, data and headers -- it opens
    # nothing. What must not survive is anything that can OPEN a connection through urllib.
    assert not re.search(r"opener\.open\s*\(", _code())
    assert "connection.request(" in MODULE_SOURCE, "the transport drives http.client directly"


# --- The per-host connection pool (U8) ----------------------------------------------------------
#
# These are the only tests that drive the REAL transport, so they fake the connection class instead
# of the transport. `_require_network` is stubbed with them: the fence exists so a suite that forgot
# to stub the network fails loudly, and a test that has replaced the connection class by definition
# has not forgotten.


class _FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"{}", *, will_close: bool = False) -> None:
        self.status = status
        self.headers = {}
        self.will_close = will_close
        self._body = body
        self._read = False

    def read(self, amount: int | None = None) -> bytes:
        self._read = True
        return self._body if amount is None else self._body[:amount]

    def isclosed(self) -> bool:
        return self._read

    def close(self) -> None:
        pass


class _FakeConnection:
    """Stands in for `_PinnedHTTPSConnection`, recording what the transport did with it."""

    opened: ClassVar[list[_FakeConnection]] = []

    def __init__(self, host, *, port, pinned_ip, deadline, timeout, context) -> None:
        self.host = host
        self.port = port
        self.pinned_ip = pinned_ip
        self.timeout = timeout
        self.sock = _FakeSocket()
        self.requests: list[tuple[str, str, dict]] = []
        self.armed: list[float] = []
        self.closed = False
        self.next_response: object = _FakeResponse()
        self.raise_on_request: BaseException | None = None
        _FakeConnection.opened.append(self)

    def arm(self, deadline: float) -> None:
        self.armed.append(deadline)
        self.sock.settimeout(wc.read_timeout(deadline))

    def request(self, method, target, body=None, headers=None) -> None:
        if self.raise_on_request is not None:
            raise self.raise_on_request
        self.requests.append((method, target, dict(headers or {})))

    def getresponse(self) -> object:
        return self.next_response

    def close(self) -> None:
        self.closed = True


def _pooled(**kwargs):
    """Drive the REAL transport with a faked connection class, and clean up after."""
    saved_connection = wc._PinnedHTTPSConnection
    saved_fence = wc._require_network
    _FakeConnection.opened = []
    wc._PinnedHTTPSConnection = _FakeConnection  # type: ignore[misc]
    wc._require_network = lambda: None  # type: ignore[assignment]
    try:
        kwargs.setdefault("url", DEST_URL)
        kwargs.setdefault("body", b'{"event_id":"e1"}')
        kwargs.setdefault("headers", {"webhook-id": "d1"})
        kwargs.setdefault("resolver", _resolver(GOOD_V4))
        url = kwargs.pop("url")
        return wc.deliver(url, **kwargs)
    finally:
        wc._PinnedHTTPSConnection = saved_connection  # type: ignore[misc]
        wc._require_network = saved_fence  # type: ignore[assignment]


def test_a_second_request_to_one_host_reuses_the_connection():
    """20 000 POSTs used to be 20 000 TLS handshakes.

    A handshake is one to two round trips plus an asymmetric operation on both sides, inside the 5 s
    per-attempt ceiling `webhook_delivery` treats as scarce -- so the CONNECTION, not the request,
    was the cost of a bad morning.
    """
    wc.reset_pools_for_tests()
    saved_connection, saved_fence = wc._PinnedHTTPSConnection, wc._require_network
    _FakeConnection.opened = []
    wc._PinnedHTTPSConnection = _FakeConnection  # type: ignore[misc]
    wc._require_network = lambda: None  # type: ignore[assignment]
    try:
        for _ in range(3):
            attempt = wc.deliver(
                DEST_URL, body=b"{}", headers={"webhook-id": "d1"}, resolver=_resolver(GOOD_V4)
            )
            assert attempt.ok, attempt
        assert len(_FakeConnection.opened) == 1, (
            f"three requests opened {len(_FakeConnection.opened)} connections"
        )
        connection = _FakeConnection.opened[0]
        assert len(connection.requests) == 3
        # Every request carries the HOSTNAME, never the pinned literal: the Host header and the
        # certificate both key on it, and neither may see the address.
        assert connection.host == "hooks.client.example"
        assert connection.pinned_ip == GOOD_V4
        # And the origin-form target, never the absolute URL -- `http.client` writes the request
        # line verbatim and an absolute URL is legal only to a proxy.
        assert {target for _m, target, _h in connection.requests} == {"/kix/notifications"}
    finally:
        wc._PinnedHTTPSConnection = saved_connection  # type: ignore[misc]
        wc._require_network = saved_fence  # type: ignore[assignment]


def test_the_socket_deadline_is_re_armed_for_every_request_on_a_pooled_connection():
    """The bug pooling introduces, and it does not look like a pooling bug.

    The socket timeout used to be set once inside `connect()`, which was right while every attempt
    opened its own connection. Reused, a connection carries the FIRST request's deadline into the
    second -- and that deadline is already in the past, so `read_timeout` floors it at 50 ms and
    every later request times out. The symptom reads as a receiver that suddenly went slow.
    """
    wc.reset_pools_for_tests()
    saved_connection, saved_fence = wc._PinnedHTTPSConnection, wc._require_network
    _FakeConnection.opened = []
    wc._PinnedHTTPSConnection = _FakeConnection  # type: ignore[misc]
    wc._require_network = lambda: None  # type: ignore[assignment]
    try:
        for _ in range(2):
            wc.deliver(DEST_URL, body=b"{}", headers={}, resolver=_resolver(GOOD_V4), timeout_s=5.0)
        connection = _FakeConnection.opened[0]
        assert len(connection.armed) == 2, "armed once per REQUEST, not once per connect"
        assert connection.armed[1] > connection.armed[0], "the second deadline is its own"
        # And what actually reached the socket is a real read budget, not the 50 ms floor.
        assert all(t > wc._MIN_READ_TIMEOUT_S for t in connection.sock.timeouts), (
            f"a reused connection got the floored timeout: {connection.sock.timeouts}"
        )
    finally:
        wc._PinnedHTTPSConnection = saved_connection  # type: ignore[misc]
        wc._require_network = saved_fence  # type: ignore[assignment]


def test_a_body_that_was_not_drained_is_never_put_back():
    """Unread bytes on a socket become the NEXT request's reply.

    An over-long response is read to the cap and no further (`MAX_RESPONSE_BYTES`), so the rest is
    still on the wire. Reusing that connection would hand the leftovers to whatever asks next, which
    is a wrong status on a delivery that has nothing to do with it.
    """
    wc.reset_pools_for_tests()
    saved_connection, saved_fence = wc._PinnedHTTPSConnection, wc._require_network
    _FakeConnection.opened = []
    wc._PinnedHTTPSConnection = _FakeConnection  # type: ignore[misc]
    wc._require_network = lambda: None  # type: ignore[assignment]
    try:
        class _Undrained(_FakeResponse):
            def isclosed(self) -> bool:
                return False

        original_init = _FakeConnection.__init__

        def _init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.next_response = _Undrained()

        _FakeConnection.__init__ = _init  # type: ignore[method-assign]
        try:
            wc.deliver(DEST_URL, body=b"{}", headers={}, resolver=_resolver(GOOD_V4))
            wc.deliver(DEST_URL, body=b"{}", headers={}, resolver=_resolver(GOOD_V4))
        finally:
            _FakeConnection.__init__ = original_init  # type: ignore[method-assign]
        assert len(_FakeConnection.opened) == 2, "an undrained connection must not be reused"
        assert _FakeConnection.opened[0].closed is True
    finally:
        wc._PinnedHTTPSConnection = saved_connection  # type: ignore[misc]
        wc._require_network = saved_fence  # type: ignore[assignment]


def test_a_pooled_connection_the_peer_closed_is_retried_once_on_a_fresh_one():
    """Receivers close idle keep-alives on their own schedule.

    A socket the peer has already closed fails the next request, and that failure is about the POOL
    rather than about the receiver -- so it costs one fresh connection rather than one failed
    delivery. Exactly one retry: a genuinely broken host must still cost one attempt per cycle, not
    two.
    """
    wc.reset_pools_for_tests()
    saved_connection, saved_fence = wc._PinnedHTTPSConnection, wc._require_network
    _FakeConnection.opened = []
    wc._PinnedHTTPSConnection = _FakeConnection  # type: ignore[misc]
    wc._require_network = lambda: None  # type: ignore[assignment]
    try:
        first = wc.deliver(DEST_URL, body=b"{}", headers={}, resolver=_resolver(GOOD_V4))
        assert first.ok and len(_FakeConnection.opened) == 1
        _FakeConnection.opened[0].raise_on_request = ConnectionResetError("peer closed")
        second = wc.deliver(DEST_URL, body=b"{}", headers={}, resolver=_resolver(GOOD_V4))
        assert second.ok, "a dead pooled connection must not cost a delivery"
        assert len(_FakeConnection.opened) == 2
        assert _FakeConnection.opened[0].closed is True, "and the dead one is discarded"

        # A host that really is broken fails, once, with a retryable classification. The pool is
        # emptied first so the attempt starts on a FRESH connection -- otherwise it would take the
        # healthy one the retry above put back and prove nothing.
        wc.reset_pools_for_tests()
        original_init = _FakeConnection.__init__

        def _init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.raise_on_request = ConnectionResetError("still broken")

        _FakeConnection.__init__ = _init  # type: ignore[method-assign]
        try:
            broken = wc.deliver(DEST_URL, body=b"{}", headers={}, resolver=_resolver(GOOD_V4))
        finally:
            _FakeConnection.__init__ = original_init  # type: ignore[method-assign]
        assert broken.ok is False and broken.retryable is True, broken
    finally:
        wc._PinnedHTTPSConnection = saved_connection  # type: ignore[misc]
        wc._require_network = saved_fence  # type: ignore[assignment]


def test_one_validated_resolution_serves_a_window_and_a_rebind_is_refused_at_the_next_one():
    """KTD9 one layer along: the pool must RE-validate, not trust the first answer forever.

    A process that resolved once at startup and dialled that literal for a week would be safe
    against a rebind and useless against a client moving their endpoint. A pool that re-resolved per
    request would pay a DNS round trip inside the per-attempt ceiling. One validated answer per
    window is the trade, and the window is what bounds how long a rebind is still being dialled.
    """
    wc.reset_pools_for_tests()
    saved_connection, saved_fence = wc._PinnedHTTPSConnection, wc._require_network
    _FakeConnection.opened = []
    wc._PinnedHTTPSConnection = _FakeConnection  # type: ignore[misc]
    wc._require_network = lambda: None  # type: ignore[assignment]
    resolves: list[int] = []
    try:
        def counting(host, port):
            resolves.append(1)
            return _answers(GOOD_V4)

        for _ in range(3):
            wc.deliver(DEST_URL, body=b"{}", headers={}, resolver=counting)
        assert resolves == [1], f"one resolution per window, not {len(resolves)}"

        # Past the window, with DNS now answering the metadata service: refused, terminally, and
        # the idle connection goes with it -- it was dialled under an answer nothing trusts now.
        pool = wc._pool_for(wc.validate_url(DEST_URL))
        assert pool.idle_count() == 1
        pool._resolved_at -= wc._RESOLVE_TTL_S + 1
        attempt = wc.deliver(
            DEST_URL, body=b"{}", headers={}, resolver=_resolver("169.254.169.254")
        )
        assert attempt.ok is False and attempt.retryable is False, attempt
        assert "unsafe_destination" in (attempt.error or "")
        assert pool.idle_count() == 0, "the idle connections go with the refused resolution"
        assert len(_FakeConnection.opened) == 1, "and nothing was dialled"
    finally:
        wc._PinnedHTTPSConnection = saved_connection  # type: ignore[misc]
        wc._require_network = saved_fence  # type: ignore[assignment]


def test_concurrency_to_one_host_is_bounded_by_the_clients_quota():
    """What stops a wide worker opening a connection per thread against one receiver.

    That shape is what a receiver experiences as an attack rather than as traffic, and it is the one
    thing about delivery that gets worse rather than better as the worker gets wider. Forced with a
    barrier: `max_in_flight` requests arrive together and the next one waits.
    """
    wc.reset_pools_for_tests()
    saved_connection, saved_fence = wc._PinnedHTTPSConnection, wc._require_network
    _FakeConnection.opened = []
    wc._PinnedHTTPSConnection = _FakeConnection  # type: ignore[misc]
    wc._require_network = lambda: None  # type: ignore[assignment]
    inside = threading.Semaphore(0)
    release = threading.Event()
    peak = [0]
    live = [0]
    lock = threading.Lock()
    try:
        original_request = _FakeConnection.request

        def blocking(self, method, target, body=None, headers=None):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            inside.release()
            release.wait(10)
            with lock:
                live[0] -= 1
            return original_request(self, method, target, body=body, headers=headers)

        _FakeConnection.request = blocking  # type: ignore[method-assign]
        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [
                    pool.submit(
                        wc.deliver,
                        DEST_URL,
                        body=b"{}",
                        headers={},
                        resolver=_resolver(GOOD_V4),
                        max_in_flight=2,
                        connect_timeout_s=10.0,
                    )
                    for _ in range(5)
                ]
                # Wait for the limit to be reached, then let everybody through.
                for _ in range(2):
                    assert inside.acquire(timeout=10), "the first two requests never arrived"
                release.set()
                assert all(f.result(timeout=15).ok for f in futures)
        finally:
            _FakeConnection.request = original_request  # type: ignore[method-assign]
            release.set()
        assert peak[0] <= 2, f"{peak[0]} requests were in flight against a limit of 2"
    finally:
        wc._PinnedHTTPSConnection = saved_connection  # type: ignore[misc]
        wc._require_network = saved_fence  # type: ignore[assignment]


def test_the_default_in_flight_limit_is_the_delivery_quotas_default():
    """Two constants, one number, and neither module may import the other for it.

    `notification_auth` imports THIS module for `validate_url`, so the edge runs one way and the
    default is written out on both sides. Pinned here rather than left to agree by habit.
    """
    import notification_auth

    assert wc.DEFAULT_MAX_IN_FLIGHT == notification_auth.DEFAULT_CONCURRENT_DELIVERIES


# --- Classification (the Event contract's vocabulary) -------------------------------------------


def test_a_2xx_is_a_successful_delivery():
    for status in (200, 201, 202, 204, 299):
        attempt = _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(_Response(status)))
        assert attempt.ok is True and attempt.status == status
        assert attempt.retryable is False
        assert attempt.terminal_state is None
        assert attempt.pause_destination is False
        assert attempt.error is None


def test_a_410_pauses_the_destination_and_cancels_the_event():
    attempt = _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(_Response(410)))
    assert attempt.ok is False
    assert attempt.retryable is False
    assert attempt.terminal_state == "cancelled"
    assert attempt.pause_destination is True


def test_other_4xx_are_terminal_for_the_event_and_pause_nothing():
    for status in (400, 401, 403, 404, 415, 422):
        attempt = _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(_Response(status)))
        assert attempt.retryable is False, status
        assert attempt.terminal_state == "failed", status
        assert attempt.pause_destination is False, status


def test_408_429_and_every_5xx_are_retryable():
    for status in (408, 429, 500, 502, 503, 504):
        attempt = _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(_Response(status)))
        assert attempt.retryable is True, status
        assert attempt.terminal_state is None, status
        assert attempt.pause_destination is False, status


def test_a_redirect_is_terminal_and_never_followed():
    recorder = _Recorder(_Response(301, {"Location": "https://elsewhere.example/"}))
    attempt = _deliver(resolver=_resolver(GOOD_V4), transport=recorder)
    assert attempt.ok is False
    assert attempt.retryable is False
    assert attempt.terminal_state == "failed"
    assert len(recorder.calls) == 1, "one request, and no second one to the Location"
    for status in (302, 303, 307, 308):
        assert wc.classify_status(status) == (False, False, "failed", False)


def test_retry_after_is_read_in_both_rfc_forms_and_bounded():
    from notification_store import RETRY_AFTER_CAP_S

    assert wc.parse_retry_after("30") == 30.0
    assert wc.parse_retry_after(" 30 ") == 30.0
    # RFC 9110 puts no ceiling on the header, so a client can legally send this.
    assert wc.parse_retry_after("999999") == float(RETRY_AFTER_CAP_S)
    assert wc.parse_retry_after("-5") == 0.0
    assert wc.parse_retry_after(None) is None
    assert wc.parse_retry_after("") is None
    assert wc.parse_retry_after("soon") is None
    # HTTP-date form, resolved against an injected now. 1788912000 is 2026-09-09T00:00:00Z.
    assert wc.parse_retry_after("Wed, 09 Sep 2026 00:01:00 GMT", now=1788912000.0) == 60.0
    # A date already in the past means "now", never "go back in time".
    assert wc.parse_retry_after("Wed, 09 Sep 2026 00:00:00 GMT", now=1788912060.0) == 0.0
    # ...and a far-future date is capped like a huge delta-seconds.
    assert wc.parse_retry_after("Fri, 09 Oct 2026 00:00:00 GMT", now=1788912000.0) == float(
        RETRY_AFTER_CAP_S
    )


def test_retry_after_rides_on_a_retryable_attempt_only():
    attempt = _deliver(
        resolver=_resolver(GOOD_V4), transport=_Recorder(_Response(429, {"Retry-After": "42"}))
    )
    assert attempt.retryable is True and attempt.retry_after_s == 42.0
    # A terminal status's Retry-After is meaningless: nothing will retry.
    attempt = _deliver(
        resolver=_resolver(GOOD_V4), transport=_Recorder(_Response(403, {"Retry-After": "42"}))
    )
    assert attempt.retryable is False and attempt.retry_after_s is None


def test_the_stores_retry_after_cap_is_the_one_this_module_applies():
    from notification_store import RETRY_AFTER_CAP_S

    assert RETRY_AFTER_CAP_S == 300
    assert wc.parse_retry_after(str(RETRY_AFTER_CAP_S * 10)) == float(RETRY_AFTER_CAP_S)


# --- Failure modes (scenario 4) -----------------------------------------------------------------


def test_a_network_error_is_retryable_and_never_escapes():
    for error in (
        urllib.error.URLError("connection refused"),
        ConnectionResetError("peer went away"),
        ssl.SSLError("handshake failed"),
        socket.gaierror("temporary failure in name resolution"),
    ):
        attempt = _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(error=error))
        assert attempt.ok is False and attempt.retryable is True
        assert attempt.terminal_state is None
        assert type(error).__name__ in (attempt.error or "")


def test_an_http_client_exception_is_retryable_because_do_open_does_not_wrap_getresponse():
    # `AbstractHTTPHandler.do_open` wraps h.request() in a URLError but NOT h.getresponse(), so a
    # garbage status line arrives as an HTTPException -- which is not an OSError, and would escape
    # onto the poller thread if the catch tuple named only OSError.
    for error in (
        http.client.BadStatusLine("garbage"),
        http.client.RemoteDisconnected("closed without a response"),
        http.client.IncompleteRead(b"half"),
    ):
        assert not isinstance(error, OSError) or isinstance(
            error, http.client.RemoteDisconnected
        )
        attempt = _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(error=error))
        assert attempt.retryable is True, type(error).__name__


def test_a_timeout_is_retryable_and_reported_inside_the_attempt_ceiling():
    attempt = _deliver(
        resolver=_resolver(GOOD_V4), transport=_Recorder(error=TimeoutError("timed out"))
    )
    assert attempt.retryable is True
    assert "TimeoutError" in (attempt.error or "")
    # The transport is handed the connect ceiling and the total deadline, and both are the plan's.
    assert wc.DEFAULT_TIMEOUT_S == 5.0
    assert wc.CONNECT_TIMEOUT_S == 2.0


def test_a_failure_while_reading_the_body_is_retryable():
    response = _Response(200, read_error=TimeoutError("read timed out"))
    attempt = _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(response))
    assert attempt.ok is False and attempt.retryable is True
    assert attempt.status is None
    assert response.closed is True, "the response must be closed even when the read fails"


def test_an_oversized_response_body_is_truncated_and_the_status_still_decides():
    big = b"x" * (wc.MAX_RESPONSE_BYTES + 4096)
    response = _Response(200, body=big)
    attempt = _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(response))
    assert attempt.response_truncated is True
    assert attempt.response_bytes == wc.MAX_RESPONSE_BYTES + 1, "read is capped at limit + 1"
    # The client accepted the event; a byte count nobody reads must not turn that into a retry
    # every cycle for the event's whole 60-minute validity.
    assert attempt.ok is True and attempt.retryable is False
    small = _Response(200, body=b"ok")
    assert _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(small)).response_truncated is (
        False
    )


def test_an_unsafe_destination_is_terminal_and_pauses_nothing():
    # Pausing is the 410 signal -- "the client told us to stop". A bad URL in a destination record
    # is an operator's to correct.
    attempt = _deliver(url="http://hooks.client.example/hook")
    assert attempt.ok is False
    assert attempt.retryable is False
    assert attempt.terminal_state == "failed"
    assert attempt.pause_destination is False
    assert (attempt.error or "").startswith("unsafe_destination")
    assert attempt.status is None


def test_a_hostile_resolution_is_terminal_without_a_transport_call():
    recorder = _Recorder(_Response(200))
    attempt = _deliver(resolver=_resolver("169.254.169.254"), transport=recorder)
    assert attempt.terminal_state == "failed"
    assert recorder.calls == [], "nothing may be sent to an address that failed validation"


# --- The request itself -------------------------------------------------------------------------


def test_the_body_is_sent_byte_for_byte_and_the_content_type_is_json():
    # Non-ASCII on purpose: the bytes signed and the bytes sent must be the same object, and a
    # multibyte body is where a re-encode would show up.
    body = '{"melding":"Tog L1 07:18 er innstilt \u2014 ta L1 07:33 fra \u00c5s"}'.encode()
    recorder = _Recorder(_Response(200))
    _deliver(
        body=body,
        headers={"webhook-id": "d1", "webhook-timestamp": "1789", "webhook-signature": "v1,x"},
        resolver=_resolver(GOOD_V4),
        transport=recorder,
    )
    request = recorder.calls[0][1]
    assert request.data == body, "the bytes signed are the bytes sent"
    assert request.get_method() == "POST"
    # Explicit, or `do_request_` defaults it to application/x-www-form-urlencoded and a receiver
    # framework may form-parse the raw bytes the signature covers.
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("User-agent") == wc.USER_AGENT
    for name in ("Webhook-id", "Webhook-timestamp", "Webhook-signature"):
        assert request.get_header(name) is not None
    assert "Content-Type" in MODULE_SOURCE


def test_a_signed_request_survives_delivery_and_verifies_at_the_receiver():
    """The one place the two modules meet, asserted as the receiver would see it.

    This is the guarantee U6 depends on and the one a refactor is most likely to break silently: if
    anything between :func:`webhook_signing.sign` and the socket re-serialized, re-encoded or
    re-wrapped the body, the signature would cover a document the receiver did not get -- and
    nothing on our side would fail. The only detector is verifying the bytes that were SENT.
    """
    import webhook_signing as ws

    root = b"kix-test-signing-root-not-used-in-any-deployment"
    # Norwegian, and therefore multibyte: a re-encode anywhere in the path would show up here and
    # nowhere else.
    body = (
        '{"alert_id":"c3d4","notification":'
        '{"body":"Toget kl. 07:18 er innstilt — ta L1 07:33 fra Ås"}}'
    ).encode()
    signed = ws.sign(
        destination_id="dst_client_a_prod",
        generations=[3, 2],
        webhook_id="dlv_01J9Z4KQ7X",
        body=body,
        timestamp=1789197300,
        root=root,
    )
    recorder = _Recorder(_Response(202))
    attempt = _deliver(
        body=signed.body,
        headers=signed.headers,
        resolver=_resolver(GOOD_V4),
        transport=recorder,
    )
    assert attempt.ok is True
    request = recorder.calls[0][1]
    for generation in (3, 2):
        assert ws.verify(
            secret=ws.derive_secret("dst_client_a_prod", generation, root=root),
            webhook_id=request.get_header("Webhook-id"),
            timestamp=request.get_header("Webhook-timestamp"),
            body=request.data,
            header=request.get_header("Webhook-signature"),
            now=1789197300,
        ).ok is True, generation


def test_caller_headers_win_over_the_defaults():
    recorder = _Recorder(_Response(200))
    _deliver(
        headers={"Content-Type": "application/cloudevents+json"},
        resolver=_resolver(GOOD_V4),
        transport=recorder,
    )
    assert recorder.calls[0][1].get_header("Content-type") == "application/cloudevents+json"


def test_the_transport_receives_the_connect_ceiling_and_a_monotonic_deadline():
    recorder = _Recorder(_Response(200))
    before = time.monotonic()
    _deliver(resolver=_resolver(GOOD_V4), transport=recorder)
    _, _, connect_timeout, deadline = recorder.calls[0]
    assert connect_timeout == wc.CONNECT_TIMEOUT_S
    assert before + wc.DEFAULT_TIMEOUT_S - 0.5 <= deadline <= time.monotonic() + (
        wc.DEFAULT_TIMEOUT_S
    )


# --- The test-network fence (scenario 6) --------------------------------------------------------


def test_the_network_fence_raises_under_a_test_runner():
    """Scenario 6. Unstubbed, this call would dial a client-supplied host from CI."""
    assert wc.network_allowed() is False
    try:
        _deliver()
    except wc.NetworkBlocked as exc:
        assert wc.ALLOW_NETWORK_ENV in str(exc)
        return
    raise AssertionError("deliver must refuse to dial from a test process")


def test_the_fence_guards_the_transport_too_when_only_the_resolver_is_stubbed():
    # Two seams, two checks: stubbing one is not enough to reach the network by accident.
    try:
        _deliver(resolver=_resolver(GOOD_V4))
    except wc.NetworkBlocked:
        return
    raise AssertionError("_send must refuse to dial from a test process")


def test_the_fence_is_a_three_state_env_var_like_the_journey_planners():
    previous = os.environ.get(wc.ALLOW_NETWORK_ENV)
    try:
        for truthy in ("1", "true", "YES", "on"):
            os.environ[wc.ALLOW_NETWORK_ENV] = truthy
            assert wc.network_allowed() is True, truthy
        for falsy in ("0", "false", "no", "", "maybe"):
            os.environ[wc.ALLOW_NETWORK_ENV] = falsy
            assert wc.network_allowed() is False, falsy
        os.environ.pop(wc.ALLOW_NETWORK_ENV, None)
        # Unset means "allowed outside a test process" -- which keeps a forgotten Helm value from
        # silently costing every delivery in prd.
        assert wc.network_allowed() is False
    finally:
        os.environ.pop(wc.ALLOW_NETWORK_ENV, None)
        if previous is not None:
            os.environ[wc.ALLOW_NETWORK_ENV] = previous


# --- Structure ----------------------------------------------------------------------------------


def test_every_terminal_state_this_module_can_report_is_one_the_store_accepts():
    # `mark_delivery_failed` raises Conflict on a terminal_state outside DELIVERY_STATES, so a
    # vocabulary drift here would surface as a raise on the poller thread rather than as a bad
    # record -- but only for the status that drifted, on the cycle it first occurred.
    from notification_store import DELIVERY_STATES

    reported = {
        wc.classify_status(status)[2]
        for status in (200, 204, 301, 302, 400, 403, 404, 408, 410, 429, 500, 503, 0)
    }
    reported.add(_deliver(url="http://h.example/x").terminal_state)
    for state in reported - {None}:
        assert state in DELIVERY_STATES, state
    assert reported == {None, "failed", "cancelled"}


def test_no_new_dependency_is_introduced():
    imports = set(re.findall(r"^(?:import|from)\s+([\w.]+)", MODULE_SOURCE, re.MULTILINE))
    allowed_local = {"notification_store", "__future__"}
    for module in imports - allowed_local:
        root = module.split(".")[0]
        assert root in set(__import__("sys").stdlib_module_names), f"{module} is not stdlib"


def test_the_module_documents_the_measurement_behind_the_positive_test():
    # The rationale is what a future reader needs to not "simplify" this back into a deny-list.
    assert "64:ff9b::a9fe:a9fe" in MODULE_SOURCE
    assert "is_multicast" in MODULE_SOURCE
    assert "CVE-2026-27129" in MODULE_SOURCE
    assert "SVIXSEC-2026-0001" in MODULE_SOURCE


# ---- The load harness's receiver gets no exemption (U1, 2026-09-12) ----------------------------


def test_no_flag_or_environment_variable_bypasses_the_address_validation():
    """The load harness needs a receiver, and the tempting shortcut is a bypass switch.

    `scripts/loadgen_subscriptions.py` points a client's destination at a receiver that has to
    answer thousands of POSTs, and the obvious way to run one is `https://localhost:8443/hook` —
    which this module refuses, correctly, on three counts (scheme, port, address). The obvious fix
    is a `--allow-local` flag or a `KIX_...` variable that skips the check, and that switch would
    then exist in the code prd runs, one environment variable away from turning the delivery path
    into an SSRF primitive against the metadata service. So there is no such switch: the harness
    README requires a public `https` : 443 hostname (a tunnel), and this check is what keeps the
    shortcut from being added later.

    **The one environment variable this module reads is not it.** `KIX_ALLOW_WEBHOOK_NETWORK`
    decides whether a socket may be opened from a TEST process at all — it is a fence in front of
    the network, and it sits BEHIND the address checks rather than in front of them. Setting it,
    or anything plausibly named like a bypass, changes no verdict below.

    Two halves, because either alone is weak. Behaviourally: with every candidate variable set
    truthy, a loopback, a link-local, a private and a metadata address are still refused, and so
    are http and a non-443 port. Structurally: the address-safety functions read no environment at
    all, so there is nothing to set.
    """
    import inspect

    candidates = (
        wc.ALLOW_NETWORK_ENV,
        "KIX_ALLOW_LOCAL_WEBHOOK",
        "KIX_WEBHOOK_ALLOW_INSECURE",
        "KIX_LOADTEST",
        "KIX_ALLOW_PRIVATE_DESTINATIONS",
    )
    before = {name: os.environ.get(name) for name in candidates}
    for name in candidates:
        os.environ[name] = "1"
    try:
        for address in ("127.0.0.1", "::1", "10.0.0.5", "169.254.169.254", "64:ff9b::a9fe:a9fe"):
            assert not wc.is_safe_address(address), address
        for url in (
            "http://receiver.example/hook",
            "https://receiver.example:8443/hook",
            "https://receiver.example:443@evil.example/hook",
        ):
            try:
                wc.validate_url(url)
            except wc.UnsafeDestination:
                continue
            raise AssertionError(f"{url} was accepted")
        # And the resolved-address half, which is where a `localhost` receiver actually dies:
        # `validate_url` is syntactic, so the refusal has to come from resolution.
        local = wc.validate_url("https://receiver.example/hook")
        try:
            wc.resolve_and_validate(
                local, resolver=lambda host, port: [(socket.AF_INET, 0, 0, "", ("127.0.0.1", 443))]
            )
        except wc.UnsafeDestination:
            pass
        else:
            raise AssertionError("a host resolving to loopback was accepted")
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    reads_env = [
        fn.__name__
        for fn in (wc.is_safe_address, wc._routable, wc.validate_url, wc.resolve_and_validate)
        if "os.environ" in inspect.getsource(fn) or "getenv" in inspect.getsource(fn)
    ]
    assert not reads_env, (
        "the address checks must read no environment variable — a configurable one is a bypass "
        f"whatever it is called: {reads_env}"
    )


def _run() -> None:
    checks = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in checks:
        os.environ.pop(wc.ALLOW_NETWORK_ENV, None)
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(checks)} webhook-client checks passed.")


if __name__ == "__main__":
    _run()
