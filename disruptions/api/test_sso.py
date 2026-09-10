"""Tests for sso.py — the Entur-SSO gate in front of /historikk, /meldinger, /rushhistorikk and
/demo.

Hermetic: the flow is exercised against a small FastAPI app carrying the same middleware and
router main installs, with the two Auth0 calls (_exchange_code, _verify_id_token) stubbed at
module level — except one test, which runs the real verifier against a key pair of its own, since
the verifier IS the lock and a stub cannot tell whether its options are right. One test imports
main to pin that the gate is actually wired there.

The properties that matter, in rough order of what a regression would cost:

* Fail CLOSED: SSO_REQUIRED without the domain, client id and secret serves 503 on the gated
  paths, never the content.
* Off by default: unset means exactly the pre-SSO behaviour, and is the rollback.
* Only the four prefix pairs are gated; /varsel and friends never see the middleware's responses.
* No session leaks in: tampered, expired and mis-purposed tokens are all refused.
* The callback refuses a verified-but-wrong identity (another organisation, no organisation,
  nonce mismatch, foreign issuer) — signature validity alone must never mint a session, because
  the partner tenant authenticates every partner operator's users too.
* The verifier refuses a wrong audience, a wrong issuer, an expired token and a symmetric
  algorithm, and never sees the client secret.
* `next` cannot become an open redirect on the login URL.
"""

from __future__ import annotations

import os
import time
import warnings
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

# --- Stub the BigQuery client BEFORE anything imports main (it builds one at module scope, and
# CI has no GCP credentials — test_static_cache.py's exact pattern; locally this suite only passed
# because a developer's ADC made the real constructor succeed). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import sso
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SESSION_SECRET = "0123456789abcdef0123456789abcdef"  # >= 32 chars, so _signing_key uses it
_DOMAIN = "partner.test.entur.org"
_ISSUER = f"https://{_DOMAIN}/"

_ENV_ON = {
    "SSO_REQUIRED": "true",
    "SSO_AUTH0_DOMAIN": _DOMAIN,
    "SSO_AUTH0_CLIENT_ID": "test-client-id",
    "SSO_AUTH0_CLIENT_SECRET": "test-client-secret",
    "SSO_SESSION_SECRET": _SESSION_SECRET,
}

_ALL_VARS = (*_ENV_ON.keys(), "SSO_AUTH0_CONNECTION")


@contextmanager
def _env(**values: str):
    """Set exactly these SSO variables for the block; every other SSO variable is unset."""
    before = {name: os.environ.pop(name, None) for name in _ALL_VARS}
    os.environ.update(values)
    try:
        yield
    finally:
        for name in values:
            os.environ.pop(name, None)
        for name, value in before.items():
            if value is not None:
                os.environ[name] = value


def _app() -> TestClient:
    app = FastAPI()
    app.add_middleware(sso.RequireSSO)
    app.include_router(sso.router)

    @app.get("/historikk")
    def historikk_page() -> dict:
        return {"page": "historikk"}

    @app.get("/historikk-api/summary")
    def historikk_api() -> dict:
        return {"api": "historikk"}

    @app.post("/meldinger-api/messages")
    def meldinger_api() -> dict:
        return {"api": "meldinger"}

    @app.get("/rushhistorikk")
    def rushhistorikk_page() -> dict:
        return {"page": "rushhistorikk"}

    @app.get("/rushhistorikk-api/days")
    def rushhistorikk_api() -> dict:
        return {"api": "rushhistorikk"}

    @app.get("/demo-api/config")
    def demo_api() -> dict:
        return {"api": "demo"}

    @app.get("/varsel-api/config")
    def public_api() -> dict:
        return {"public": True}

    return TestClient(app, follow_redirects=False)


def _session_cookie(email: str = "somebody@entur.org") -> dict[str, str]:
    return {sso._COOKIE_NAME: sso._make_session(email)}


def test_off_by_default_serves_everything():
    """Unset SSO_REQUIRED is the pre-SSO behaviour and the documented rollback."""
    client = _app()
    with _env():
        assert client.get("/historikk").status_code == 200
        assert client.get("/historikk-api/summary").status_code == 200
        assert client.post("/meldinger-api/messages").status_code == 200
        # And the login surface does not exist while the gate is off.
        assert client.get("/auth/login").status_code == 404


def test_required_but_unconfigured_fails_closed():
    """A deployment that flips the flag without the credentials must serve NOTHING gated —
    503, not the content — while the public surfaces stay up. All three of domain, client id and
    secret are needed: the Secret Manager pair arrives through a separate ExternalSecret from the
    configmap values, so "two of three" is a real deployment state, not a typo."""
    client = _app()
    with _env(SSO_REQUIRED="true"):
        assert client.get("/historikk").status_code == 503
        assert client.get("/historikk-api/summary").status_code == 503
        assert client.get("/varsel-api/config").status_code == 200
        assert client.get("/auth/login").status_code == 404
    with _env(SSO_REQUIRED="true", SSO_AUTH0_DOMAIN=_DOMAIN, SSO_AUTH0_CLIENT_ID="id"):
        assert client.get("/historikk").status_code == 503
    with _env(SSO_REQUIRED="true", SSO_AUTH0_CLIENT_ID="id", SSO_AUTH0_CLIENT_SECRET="s"):
        assert client.get("/historikk").status_code == 503


def test_public_paths_never_gated():
    client = _app()
    with _env(**_ENV_ON):
        assert client.get("/varsel-api/config").status_code == 200


def test_browser_navigation_redirects_to_login():
    """A document request without a session bounces to /auth/login carrying where it was going."""
    client = _app()
    with _env(**_ENV_ON):
        response = client.get("/historikk?dag=2026-08-30", headers={"accept": "text/html"})
        assert response.status_code == 302
        target = urlsplit(response.headers["location"])
        assert target.path == "/auth/login"
        assert parse_qs(target.query)["next"] == ["/historikk?dag=2026-08-30"]


def test_demo_is_gated():
    """/demo joined the gated surfaces on 2026-08-31 (partner demos are driven by an Entur
    person's logged-in browser, so SSO does not lock the demo out of its own showings)."""
    client = _app()
    with _env(**_ENV_ON):
        response = client.get("/demo", headers={"accept": "text/html"})
        assert response.status_code == 302
        assert response.headers["location"].startswith("/auth/login")
        assert client.get("/demo-api/config").status_code == 401
        response = client.get("/demo-api/config", cookies=_session_cookie())
        assert response.status_code == 200


def test_prefixes_are_absolute_paths():
    """Every ASGI path starts with "/", so an unslashed prefix matches NOTHING and the surface
    silently stays public while looking gated — exactly how /demo first shipped ("demo",
    "demo-api"). Also pins the per-segment rule: /demo must not claim /demolition."""
    assert all(prefix.startswith("/") for prefix in sso.PROTECTED_PREFIXES)
    assert sso._is_protected("/demo")
    assert sso._is_protected("/demo/index.html")
    assert not sso._is_protected("/demolition")
    assert not sso._is_protected("demo")


def test_api_fetch_gets_401():
    """The SPAs' own fetches (and curl) get a plain 401, not a redirect into an HTML login."""
    client = _app()
    with _env(**_ENV_ON):
        response = client.get("/historikk-api/summary", headers={"accept": "application/json"})
        assert response.status_code == 401
        # Non-GET is never redirected, whatever it accepts.
        response = client.post("/meldinger-api/messages", headers={"accept": "text/html"})
        assert response.status_code == 401


def test_options_is_exempt():
    """The middleware runs outside CORSMiddleware, so gating preflights would 401 them before
    CORS could answer. No route serves OPTIONS here, so pass-through reads as 405, not 401/503."""
    client = _app()
    with _env(**_ENV_ON):
        assert client.options("/historikk-api/summary").status_code == 405


def test_valid_session_passes():
    client = _app()
    with _env(**_ENV_ON):
        response = client.get("/historikk-api/summary", cookies=_session_cookie())
        assert response.status_code == 200


def test_expired_session_is_refused():
    client = _app()
    with _env(**_ENV_ON):
        stale = sso._pack("session", {"email": "somebody@entur.org", "exp": int(time.time()) - 1})
        response = client.get(
            "/historikk",
            headers={"accept": "text/html"},
            cookies={sso._COOKIE_NAME: stale},
        )
        assert response.status_code == 302


def test_tampered_session_is_refused():
    client = _app()
    with _env(**_ENV_ON):
        token = sso._make_session("somebody@entur.org")
        flipped = token[:-1] + ("0" if token[-1] != "0" else "1")
        response = client.get(
            "/historikk-api/summary",
            headers={"accept": "application/json"},
            cookies={sso._COOKIE_NAME: flipped},
        )
        assert response.status_code == 401


def test_state_token_is_not_a_session():
    """The purpose tag in _pack keeps the two token kinds disjoint under the one signing key."""
    with _env(**_ENV_ON):
        state = sso._pack("state", {"next": "/", "nonce": "n", "exp": int(time.time()) + 60})
        assert sso.session_email(state) is None


def test_login_redirects_to_auth0():
    client = _app()
    with _env(**_ENV_ON):
        response = client.get("/auth/login?next=/meldinger")
        assert response.status_code == 302
        target = urlsplit(response.headers["location"])
        assert (target.scheme, target.netloc, target.path) == ("https", _DOMAIN, "/authorize")
        params = parse_qs(target.query)
        assert params["client_id"] == ["test-client-id"]
        assert params["response_type"] == ["code"]
        # `profile` is load-bearing: without it the tenant's action leaves the organisation claim
        # off the id_token and every login would be refused as no_organisation.
        assert params["scope"] == ["openid email profile"]
        # Straight to the Entra federation, not Universal Login's chooser.
        assert params["connection"] == ["entur-waad"]
        # TestClient's host is "testserver" — not local, so the callback must be https.
        assert params["redirect_uri"] == ["https://testserver/auth/callback"]
        # The state round-trips next and the nonce, signed.
        state = sso._unpack("state", params["state"][0])
        assert state is not None and state["next"] == "/meldinger"
        assert state["nonce"] == params["nonce"][0]
        # Nothing secret rides in the redirect.
        assert "test-client-secret" not in response.headers["location"]


def test_connection_hint_is_configurable():
    """An empty SSO_AUTH0_CONNECTION drops the hint (Universal Login then offers whatever the
    application has enabled); a pasted `https://…/` domain is tolerated."""
    client = _app()
    with _env(**{**_ENV_ON, "SSO_AUTH0_CONNECTION": ""}):
        params = parse_qs(urlsplit(client.get("/auth/login").headers["location"]).query)
        assert "connection" not in params
    with _env(**{**_ENV_ON, "SSO_AUTH0_DOMAIN": f"https://{_DOMAIN}/"}):
        target = urlsplit(client.get("/auth/login").headers["location"])
        assert (target.netloc, target.path) == (_DOMAIN, "/authorize")
        assert sso._issuer() == _ISSUER


def test_login_with_live_session_skips_auth0():
    client = _app()
    with _env(**_ENV_ON):
        response = client.get("/auth/login?next=/historikk", cookies=_session_cookie())
        assert response.status_code == 302
        assert response.headers["location"] == "/historikk"


def test_next_is_relative_only():
    """`next` rides through the IdP round trip; an absolute or scheme-relative value would be an
    open redirect on a login URL."""
    assert sso._safe_next("/historikk?x=1") == "/historikk?x=1"
    assert sso._safe_next("https://evil.example/") == "/"
    assert sso._safe_next("//evil.example/") == "/"
    assert sso._safe_next("/\\evil") == "/"
    assert sso._safe_next("") == "/"


@contextmanager
def _stubbed_auth0(claims_or_exc):
    """Stub the two Auth0 seams; the exchange returns an opaque JWT, the verifier the claims."""
    before = (sso._exchange_code, sso._verify_id_token)

    def fake_exchange(code: str, redirect_uri: str) -> str:
        return "opaque-jwt"

    def fake_verify(id_token_jwt: str) -> dict:
        assert id_token_jwt == "opaque-jwt"
        if isinstance(claims_or_exc, Exception):
            raise claims_or_exc
        return claims_or_exc

    sso._exchange_code, sso._verify_id_token = fake_exchange, fake_verify
    try:
        yield
    finally:
        sso._exchange_code, sso._verify_id_token = before


def _state(nonce: str = "n1", next_url: str = "/historikk") -> str:
    return sso._pack("state", {"next": next_url, "nonce": nonce, "exp": int(time.time()) + 60})


def _claims(**overrides) -> dict:
    """What the partner tenant puts on an id_token for an entur-waad login with
    `openid email profile` in scope. The organisation claim is a JSON NUMBER (the tenant action
    parses it with Number.parseInt)."""
    claims = {
        "iss": _ISSUER,
        "sub": "waad|abc123",
        "aud": "test-client-id",
        "nonce": "n1",
        "email": "somebody@entur.org",
        "email_verified": True,
        "name": "Somebody",
        sso._ORGANISATION_CLAIM: 1,
    }
    for key, value in overrides.items():
        if value is ...:
            claims.pop(key)
        else:
            claims[key] = value
    return claims


def test_callback_happy_path_sets_a_working_session():
    client = _app()
    with _env(**_ENV_ON), _stubbed_auth0(_claims()):
        response = client.get(f"/auth/callback?code=c&state={_state()}")
        assert response.status_code == 302
        assert response.headers["location"] == "/historikk"
        cookie = response.cookies[sso._COOKIE_NAME]
        assert sso.session_email(cookie) == "somebody@entur.org"
        # And the minted session actually opens the gate.
        follow = client.get("/historikk-api/summary", cookies={sso._COOKIE_NAME: cookie})
        assert follow.status_code == 200


def test_callback_accepts_a_string_organisation_id():
    """The action writes a number, but app_metadata set by another path (the permission store
    sync, a hand edit) can carry "1"; the rule is about the value, not its JSON type."""
    client = _app()
    with _env(**_ENV_ON), _stubbed_auth0(_claims(**{sso._ORGANISATION_CLAIM: "1"})):
        response = client.get(f"/auth/callback?code=c&state={_state()}")
        assert response.status_code == 302
        assert sso._COOKIE_NAME in response.cookies


def test_callback_rejects_bad_state():
    client = _app()
    with _env(**_ENV_ON):
        assert client.get("/auth/callback?code=c&state=garbage").status_code == 400
        assert client.get("/auth/callback?code=c").status_code == 400


def test_callback_relays_an_upstream_refusal_without_a_session():
    """Auth0 answers `error=access_denied` when its action denies the login (a user with no
    organisation) or the person cancels at Entra. Nothing to verify, nothing minted."""
    client = _app()
    with _env(**_ENV_ON), _stubbed_auth0(AssertionError("must not be called")):
        response = client.get(
            f"/auth/callback?error=access_denied&error_description=User+is+not+part+of+organisation"
            f"&state={_state()}"
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "login_refused:access_denied"
        assert sso._COOKIE_NAME not in response.cookies


def _assert_callback_refused(claims, expected_reason: str):
    client = _app()
    with _env(**_ENV_ON), _stubbed_auth0(claims):
        response = client.get(f"/auth/callback?code=c&state={_state()}")
        assert response.status_code == 403, expected_reason
        assert response.json()["detail"] == expected_reason
        assert sso._COOKIE_NAME not in response.cookies


def test_callback_rejects_wrong_identity():
    """A cryptographically valid token is not enough: the claims decide, one refusal per rule.
    The partner tenant logs in every partner operator's staff — a Vy employee (organisation 57)
    holds a perfectly valid token from the same issuer."""
    org = sso._ORGANISATION_CLAIM
    _assert_callback_refused(_claims(**{org: 57}), "wrong_organisation")
    _assert_callback_refused(_claims(**{org: "57"}), "wrong_organisation")
    _assert_callback_refused(_claims(**{org: ...}), "no_organisation")
    _assert_callback_refused(_claims(**{org: None}), "no_organisation")
    _assert_callback_refused(_claims(**{org: "entur"}), "no_organisation")
    _assert_callback_refused(_claims(**{org: True}), "no_organisation")
    _assert_callback_refused(_claims(nonce="other"), "nonce_mismatch")
    _assert_callback_refused(_claims(iss="https://evil.example/"), "wrong_issuer")
    _assert_callback_refused(_claims(iss=_ISSUER.rstrip("/")), "wrong_issuer")
    _assert_callback_refused(_claims(email=...), "no_email")
    _assert_callback_refused(_claims(email=""), "no_email")


def test_callback_verification_failure_is_502_not_a_session():
    client = _app()
    with _env(**_ENV_ON), _stubbed_auth0(ValueError("boom")):
        response = client.get(f"/auth/callback?code=c&state={_state()}")
        assert response.status_code == 502
        assert sso._COOKIE_NAME not in response.cookies


def _rsa_key_pair():
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@contextmanager
def _published_key(public_key, kid: str = "kid-1"):
    """Stand in for the tenant's JWKS: whatever kid a token names, this is the key served."""

    class _Key:
        key = public_key

    class _Client:
        def get_signing_key_from_jwt(self, token: str):
            import jwt

            assert jwt.get_unverified_header(token).get("kid") == kid
            return _Key()

    before = sso._jwks_client
    sso._jwks_client = lambda url: _Client()
    try:
        yield
    finally:
        sso._jwks_client = before


def test_the_real_verifier_checks_signature_issuer_audience_and_expiry():
    """The verifier is the lock, so this one runs PyJWT for real against a key pair of the
    suite's own. Each refusal is a separate wrong token: another key, another audience, another
    issuer, expired, and HS256 under the client secret — the algorithm confusion a verifier that
    accepted the tenant's whole supported list would fall to."""
    import jwt

    private, public = _rsa_key_pair()
    other_private, _ = _rsa_key_pair()
    now = int(time.time())

    def token(key=private, alg="RS256", **overrides) -> str:
        claims = {
            "iss": _ISSUER,
            "sub": "waad|abc123",
            "aud": "test-client-id",
            "iat": now,
            "exp": now + 300,
            "nonce": "n1",
            "email": "somebody@entur.org",
            sso._ORGANISATION_CLAIM: 1,
        }
        claims.update(overrides)
        with warnings.catch_warnings():
            # The HS256 arm signs under the 18-byte test secret on purpose; PyJWT's key-length
            # warning about it is noise here.
            warnings.simplefilter("ignore")
            return jwt.encode(claims, key, algorithm=alg, headers={"kid": "kid-1"})

    with _env(**_ENV_ON), _published_key(public):
        claims = sso._verify_id_token(token())
        assert claims["email"] == "somebody@entur.org"
        assert claims[sso._ORGANISATION_CLAIM] == 1
        # A PS256 signature from the same key is fine too — asymmetric either way.
        assert sso._verify_id_token(token(alg="PS256"))["sub"] == "waad|abc123"

        for label, bad in (
            ("other key", token(key=other_private)),
            ("wrong audience", token(aud="somebody-elses-client")),
            ("wrong issuer", token(iss="https://partner.entur.org/")),
            ("issuer without slash", token(iss=_ISSUER.rstrip("/"))),
            ("expired", token(exp=now - 120)),
            ("no exp", token(exp=None)),
            ("HS256 under the client secret", token(key="test-client-secret", alg="HS256")),
        ):
            try:
                sso._verify_id_token(bad)
            except jwt.exceptions.PyJWTError:
                pass
            else:
                raise AssertionError(f"verifier accepted a token with {label}")


def test_logout_clears_the_cookie_and_ends_the_auth0_session():
    """Auth0's own SSO cookie outlives ours, so a logout that only dropped the cookie would be
    undone by the next /auth/login without a prompt. returnTo is this host's root — the
    application's registered logout URL — and never `federated`, which would sign the person out
    of Entra and every Microsoft 365 tab."""
    client = _app()
    with _env(**_ENV_ON):
        response = client.get("/auth/logout", cookies=_session_cookie())
        assert response.status_code == 302
        target = urlsplit(response.headers["location"])
        assert (target.scheme, target.netloc, target.path) == ("https", _DOMAIN, "/v2/logout")
        params = parse_qs(target.query)
        assert params["client_id"] == ["test-client-id"]
        assert params["returnTo"] == ["https://testserver/"]
        assert "federated" not in params
        set_cookie = response.headers.get("set-cookie", "")
        assert sso._COOKIE_NAME in set_cookie and "Max-Age=0" in set_cookie
    # With the gate off there is no Auth0 session to end; logout is just home.
    with _env():
        response = client.get("/auth/logout", cookies=_session_cookie())
        assert response.status_code == 302
        assert response.headers["location"] == "/"


def test_main_wires_the_gate():
    """The gate is exercised on the REAL app — a gate that only exists in this suite's toy app
    protects nothing. Functional rather than introspective (this FastAPI keeps included routers
    lazy in app.routes), and no lifespan: neither request reaches BigQuery."""
    import main

    assert any(m.cls is sso.RequireSSO for m in main.app.user_middleware)
    client = TestClient(main.app, follow_redirects=False)
    with _env(**_ENV_ON):
        response = client.get("/auth/login")
        assert response.status_code == 302
        assert response.headers["location"].startswith(f"https://{_DOMAIN}/authorize?")
        # The middleware answers before any route or mount, so this holds whether or not the
        # gitignored historikk_static build exists where the suite runs.
        response = client.get("/historikk", headers={"accept": "text/html"})
        assert response.status_code == 302
        assert response.headers["location"].startswith("/auth/login")
        response = client.get("/meldinger-api/messages", headers={"accept": "application/json"})
        assert response.status_code == 401
        # /rushhistorikk is its own prefix pair: matching is per path segment, so "/historikk"
        # does not claim it and "/rushhistorikk" does not claim "/rushhistorikk-api".
        response = client.get("/rushhistorikk", headers={"accept": "text/html"})
        assert response.status_code == 302
        response = client.get("/rushhistorikk-api/days", headers={"accept": "application/json"})
        assert response.status_code == 401
    # The eight surfaces, exactly: a ninth must be added here deliberately, and removing one
    # un-gates it.
    assert sso.PROTECTED_PREFIXES == (
        "/historikk",
        "/historikk-api",
        "/rushhistorikk",
        "/rushhistorikk-api",
        "/meldinger",
        "/meldinger-api",
        "/demo",
        "/demo-api",
    )


def test_the_notification_api_is_deliberately_not_behind_this_gate():
    """U4 scenario 9. The client surface is authenticated, and NOT by this middleware.

    Deliberate rather than forgotten, and the reason is the two answers this gate gives. Required
    but unconfigured serves 503, which is right for a browser and wrong as a statement about a
    machine caller's credential; and an unauthenticated navigation gets a 302 into an Entra login
    screen, so a client BFF following it receives an HTML page where it expected JSON — and the
    redirect discriminator is `text/html` in `accept`, which nothing stops a client sending.
    KTD8 therefore puts the check in the router's own dependency
    (`notification_auth.authenticate`, bearer only, never a cookie) and leaves this tuple alone.

    Pinned as a NEGATIVE beside the exact tuple above rather than instead of it: this asserts that
    the client prefixes are absent and that nothing claims them by segment, so adding
    "/notification-api" here later fails both checks and has to be an argued change.
    """
    assert not any(prefix.startswith("/notification") for prefix in sso.PROTECTED_PREFIXES)
    for path in (
        "/notification-api",
        "/notification-api/v1",
        "/notification-api/v1/subscriptions",
        "/notification-api/v1/destinations/current",
        "/notification-api/v1/test-events",
    ):
        assert not sso._is_protected(path), path
    # And the gate really does let them through, rather than merely not listing them: with SSO
    # required and configured, an unauthenticated client request reaches the app (404 here,
    # because this suite's app mounts no client router) instead of being answered 302 or 401.
    client = _app()
    with _env(**_ENV_ON):
        response = client.get(
            "/notification-api/v1/subscriptions", headers={"accept": "text/html"}
        )
        assert response.status_code == 404, response.status_code


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} sso checks passed.")


if __name__ == "__main__":
    _run()
