"""Tests for sso.py — the Entur-SSO gate in front of /historikk and /meldinger.

Hermetic: the flow is exercised against a small FastAPI app carrying the same middleware and
router main installs, with the two Google calls (_exchange_code, _verify_id_token) stubbed at
module level. One test imports main to pin that the gate is actually wired there.

The properties that matter, in rough order of what a regression would cost:

* Fail CLOSED: SSO_REQUIRED without credentials serves 503 on the gated paths, never the content.
* Off by default: unset means exactly the pre-SSO behaviour, and is the rollback.
* Only the four prefixes are gated; /varsel and friends never see the middleware's responses.
* No session leaks in: tampered, expired and mis-purposed tokens are all refused.
* The callback refuses a verified-but-wrong identity (foreign hd, unverified email, nonce
  mismatch) — signature validity alone must never mint a session.
* `next` cannot become an open redirect on the login URL.
"""

from __future__ import annotations

import os
import time
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

_ENV_ON = {
    "SSO_REQUIRED": "true",
    "SSO_GOOGLE_CLIENT_ID": "test-client-id",
    "SSO_GOOGLE_CLIENT_SECRET": "test-client-secret",
    "SSO_SESSION_SECRET": _SESSION_SECRET,
}

_ALL_VARS = (*_ENV_ON.keys(), "SSO_ALLOWED_DOMAIN")


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
    503, not the content — while the public surfaces stay up."""
    client = _app()
    with _env(SSO_REQUIRED="true"):
        assert client.get("/historikk").status_code == 503
        assert client.get("/historikk-api/summary").status_code == 503
        assert client.get("/varsel-api/config").status_code == 200
        assert client.get("/auth/login").status_code == 404


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


def test_login_redirects_to_google():
    client = _app()
    with _env(**_ENV_ON):
        response = client.get("/auth/login?next=/meldinger")
        assert response.status_code == 302
        target = urlsplit(response.headers["location"])
        assert (target.scheme, target.netloc) == ("https", "accounts.google.com")
        params = parse_qs(target.query)
        assert params["client_id"] == ["test-client-id"]
        assert params["scope"] == ["openid email"]
        assert params["hd"] == ["entur.org"]
        # TestClient's host is "testserver" — not local, so the callback must be https.
        assert params["redirect_uri"] == ["https://testserver/auth/callback"]
        # The state round-trips next and the nonce, signed.
        state = sso._unpack("state", params["state"][0])
        assert state is not None and state["next"] == "/meldinger"
        assert state["nonce"] == params["nonce"][0]


def test_login_with_live_session_skips_google():
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
def _stubbed_google(claims_or_exc):
    """Stub the two Google seams; the exchange returns an opaque JWT, the verifier the claims."""
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
    claims = {
        "iss": "https://accounts.google.com",
        "nonce": "n1",
        "email": "somebody@entur.org",
        "email_verified": True,
        "hd": "entur.org",
    }
    claims.update(overrides)
    return claims


def test_callback_happy_path_sets_a_working_session():
    client = _app()
    with _env(**_ENV_ON), _stubbed_google(_claims()):
        response = client.get(f"/auth/callback?code=c&state={_state()}")
        assert response.status_code == 302
        assert response.headers["location"] == "/historikk"
        cookie = response.cookies[sso._COOKIE_NAME]
        assert sso.session_email(cookie) == "somebody@entur.org"
        # And the minted session actually opens the gate.
        follow = client.get("/historikk-api/summary", cookies={sso._COOKIE_NAME: cookie})
        assert follow.status_code == 200


def test_callback_rejects_bad_state():
    client = _app()
    with _env(**_ENV_ON):
        assert client.get("/auth/callback?code=c&state=garbage").status_code == 400
        assert client.get("/auth/callback?code=c").status_code == 400


def _assert_callback_refused(claims, expected_reason: str):
    client = _app()
    with _env(**_ENV_ON), _stubbed_google(claims):
        response = client.get(f"/auth/callback?code=c&state={_state()}")
        assert response.status_code == 403, expected_reason
        assert response.json()["detail"] == expected_reason
        assert sso._COOKIE_NAME not in response.cookies


def test_callback_rejects_wrong_identity():
    """A cryptographically valid token is not enough: the claims decide, one refusal per rule."""
    _assert_callback_refused(_claims(hd="gmail.com"), "wrong_hosted_domain")
    _assert_callback_refused(_claims(nonce="other"), "nonce_mismatch")
    _assert_callback_refused(_claims(email_verified=False), "email_not_verified")
    _assert_callback_refused(_claims(iss="https://evil.example"), "wrong_issuer")
    _assert_callback_refused(
        _claims(email="somebody@gmail.com"), "wrong_email_domain"
    )


def test_callback_verification_failure_is_502_not_a_session():
    client = _app()
    with _env(**_ENV_ON), _stubbed_google(ValueError("boom")):
        response = client.get(f"/auth/callback?code=c&state={_state()}")
        assert response.status_code == 502
        assert sso._COOKIE_NAME not in response.cookies


def test_logout_clears_the_cookie():
    client = _app()
    with _env(**_ENV_ON):
        response = client.get("/auth/logout", cookies=_session_cookie())
        assert response.status_code == 302
        set_cookie = response.headers.get("set-cookie", "")
        assert sso._COOKIE_NAME in set_cookie and 'Max-Age=0' in set_cookie


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
        assert response.headers["location"].startswith("https://accounts.google.com/")
        # The middleware answers before any route or mount, so this holds whether or not the
        # gitignored historikk_static build exists where the suite runs.
        response = client.get("/historikk", headers={"accept": "text/html"})
        assert response.status_code == 302
        assert response.headers["location"].startswith("/auth/login")
        response = client.get("/meldinger-api/messages", headers={"accept": "application/json"})
        assert response.status_code == 401
    # The four surfaces, exactly: a fifth internal surface must be added here deliberately, and
    # removing one un-gates it.
    assert sso.PROTECTED_PREFIXES == ("/historikk", "/historikk-api", "/meldinger", "/meldinger-api")


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
