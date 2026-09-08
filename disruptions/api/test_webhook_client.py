"""Checks for webhook_client.py -- KTD9's outbound hardening, R18's bounds.

**This suite opens no socket, and enforces that rather than promising it.** ``socket.getaddrinfo``
and ``socket.create_connection`` are replaced at import with functions that raise, so any check that
reaches the network fails loudly instead of dialing a partner-supplied host from CI. Address safety
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
import time
import urllib.error
import urllib.request

# --- Fence the network before importing the module under test -----------------------------------


def _no_socket(*args, **kwargs):
    raise AssertionError(
        "this suite must open no socket; stub `resolver`/`transport` instead "
        f"(called with {args!r})"
    )


socket.getaddrinfo = _no_socket  # type: ignore[assignment]
socket.create_connection = _no_socket  # type: ignore[assignment]

import webhook_client as wc

MODULE_SOURCE = pathlib.Path(wc.__file__).read_text()

DEST_URL = "https://hooks.partner.example/kix/notifications"

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

    def __call__(self, resolved, request, connect_timeout_s, deadline):
        self.calls.append((resolved, request, connect_timeout_s, deadline))
        if self.error is not None:
            raise self.error
        return self.response if self.response is not None else _Response()


def _deliver(**kwargs):
    kwargs.setdefault("url", DEST_URL)
    kwargs.setdefault("body", b'{"event_id":"e1"}')
    kwargs.setdefault("headers", {"webhook-id": "d1"})
    url = kwargs.pop("url")
    return wc.deliver(url, **kwargs)


# --- URL validation -----------------------------------------------------------------------------


def test_only_https_is_accepted():
    destination = wc.validate_url(DEST_URL)
    assert destination.host == "hooks.partner.example"
    assert destination.port == wc.ALLOWED_PORT == 443
    for bad in ("http://hooks.partner.example/x", "ftp://h/x", "hooks.partner.example/x", ""):
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
        "https://user:pass@hooks.partner.example/hook",
        "https://@hooks.partner.example/hook",
    ):
        try:
            wc.validate_url(bad)
        except wc.UnsafeDestination as exc:
            assert "userinfo" in str(exc)
            continue
        raise AssertionError(f"{bad!r} should have been refused")


def test_a_fragment_is_refused():
    try:
        wc.validate_url("https://hooks.partner.example/hook#frag")
    except wc.UnsafeDestination as exc:
        assert "fragment" in str(exc)
        return
    raise AssertionError("a fragment should have been refused")


def test_a_non_default_port_is_refused_and_an_explicit_443_is_not():
    assert wc.validate_url("https://hooks.partner.example:443/hook").port == 443
    for bad in ("https://hooks.partner.example:8443/hook", "https://hooks.partner.example:80/hook"):
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
    # Nothing wrong with one, and a partner may well route on it.
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
    for bad in ("", "not-an-ip", "999.1.1.1", "1.1.1", "hooks.partner.example"):
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
        "hooks.partner.example", pinned_ip=GOOD_V6, deadline=time.monotonic() + 4.0, timeout=2.0
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
    assert connection.host == "hooks.partner.example"
    assert connection._context.server_hostname == "hooks.partner.example"
    # The read budget is what is left of the total after the connect.
    assert sock.timeouts and 3.0 < sock.timeouts[-1] <= 4.0


def test_the_read_timeout_floors_at_a_blocking_value():
    # settimeout(0) puts the socket in non-blocking mode, which http.client cannot drive.
    assert wc.read_timeout(100.0, now=0.0) == 100.0
    assert wc.read_timeout(0.0, now=100.0) == wc._MIN_READ_TIMEOUT_S
    assert wc.read_timeout(0.0, now=0.0) == wc._MIN_READ_TIMEOUT_S


def test_the_opener_carries_no_redirect_proxy_error_or_cookie_handler():
    """Refusing redirects is structural here: there is no handler that could follow one."""
    opener = urllib.request.OpenerDirector()
    opener.add_handler(wc._PinnedHTTPSHandler(pinned_ip=GOOD_V4, deadline=time.monotonic() + 1))
    names = {type(h).__name__ for h in opener.handlers}
    assert names == {"_PinnedHTTPSHandler"}, names
    for forbidden in ("HTTPRedirectHandler", "ProxyHandler", "HTTPErrorProcessor"):
        assert forbidden not in names
    # And `_send` builds it exactly that way -- one handler, and never `build_opener`, whose
    # default handler set includes both ProxyHandler and HTTPRedirectHandler. Matched as a CALL, so
    # the docstring naming it as the thing not used does not satisfy the check.
    assert not re.search(r"build_opener\s*\(", MODULE_SOURCE)
    assert "OpenerDirector()" in MODULE_SOURCE


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
    from partner_store import RETRY_AFTER_CAP_S

    assert wc.parse_retry_after("30") == 30.0
    assert wc.parse_retry_after(" 30 ") == 30.0
    # RFC 9110 puts no ceiling on the header, so a partner can legally send this.
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
    from partner_store import RETRY_AFTER_CAP_S

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
    # The partner accepted the event; a byte count nobody reads must not turn that into a retry
    # every cycle for the event's whole 60-minute validity.
    assert attempt.ok is True and attempt.retryable is False
    small = _Response(200, body=b"ok")
    assert _deliver(resolver=_resolver(GOOD_V4), transport=_Recorder(small)).response_truncated is (
        False
    )


def test_an_unsafe_destination_is_terminal_and_pauses_nothing():
    # Pausing is the 410 signal -- "the partner told us to stop". A bad URL in a destination record
    # is an operator's to correct.
    attempt = _deliver(url="http://hooks.partner.example/hook")
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
        destination_id="dst_partner_a_prod",
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
            secret=ws.derive_secret("dst_partner_a_prod", generation, root=root),
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
    """Scenario 6. Unstubbed, this call would dial a partner-supplied host from CI."""
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
    from partner_store import DELIVERY_STATES

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
    allowed_local = {"partner_store", "__future__"}
    for module in imports - allowed_local:
        root = module.split(".")[0]
        assert root in set(__import__("sys").stdlib_module_names), f"{module} is not stdlib"


def test_the_module_documents_the_measurement_behind_the_positive_test():
    # The rationale is what a future reader needs to not "simplify" this back into a deny-list.
    assert "64:ff9b::a9fe:a9fe" in MODULE_SOURCE
    assert "is_multicast" in MODULE_SOURCE
    assert "CVE-2026-27129" in MODULE_SOURCE
    assert "SVIXSEC-2026-0001" in MODULE_SOURCE


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
