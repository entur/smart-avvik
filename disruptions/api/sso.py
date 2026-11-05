"""Entur-SSO gate for the internal surfaces: /historikk, /meldinger, /rushhistorikk and /demo,
pages and APIs both.

OpenID Connect against **Auth0's partner tenant** (``partner.<dev|staging|>.entur.org``), which
federates Entur staff and hired-in consultants from Entra ID through its ``entur-waad``
connection — the official route (Team Sikkerhet's Golden Path, «How to implement
authentication»), enforced in this process because nothing upstream enforces anything: the
platform ingress is a shared host router, and the kix gateway's SSO is a portal-UI property, not an
HTTP one. It replaced a Google-OIDC gate on 2026-09-10; that one needed an OAuth client in this
app's own GCP project, and the ``roles/oauthconfig.editor`` it takes is not on the platform's
assignable-roles list (tf-gcp-apps PR #724's answer was "use Auth0"). The flow is the same
authorization-code exchange either way — what moved is the endpoints, the verifier and the
access rule.

**The access rule is the organisation claim, not the tenant.** The partner tenant hosts every
partner operator's users too, so "authenticated by it" is not "works at Entur". A tenant action
(``auth0-tf-partner/actions/add-organisationID-to-tokens``) puts ``https://entur.io/organisationID``
on the id_token when ``profile`` is in scope — a JSON number, Entur's is 1 — and denies the login
outright when a user has none; ``_claims_problem`` requires exactly that value. The application is
also registered with only the ``entur-waad`` connection enabled, and ``/auth/login`` names it, so a
partner user never gets as far as the claim check; neither of those two is the lock.

What is gated is exactly :data:`PROTECTED_PREFIXES` — entries are absolute path prefixes, so a
new one MUST start with "/" (every ASGI path does; an unslashed entry matches nothing and the
surface silently stays public — shipped once, 2026-08-31, pinned by the test suite since).
Everything else — /varsel, /rushvarsel, the read-only APIs — is deliberately untouched: those are
the traveller-facing surfaces, and this gate exists so the internal ones (and the partner demo)
can stop being public without moving anybody's URL.

**Failure posture: closed.** ``SSO_REQUIRED=true`` with a missing domain, client id or client
secret serves the protected paths as 503, never as public — a deployment that loses an env var
must not quietly republish the decision log. ``SSO_REQUIRED`` unset or falsy is the documented off
switch and the rollback: everything serves exactly as before this module existed.

**Sessions** are a signed cookie (HMAC-SHA256 over ``{email, exp}``), 12 h, HttpOnly,
SameSite=Lax. There is no server-side session store and no revocation — acceptable for a gate
whose failure mode was "public". If ``SSO_SESSION_SECRET`` is unset (or under 32 chars) the key is
per-process, so every redeploy logs everyone out; the re-login is a silent redirect bounce while
the Auth0 session lives, but in-flight XHR on an open page gets 401s until reload, so deployed
environments should set the secret. Expiry behaves the same way: the SPAs' fetches start
returning 401 and a reload logs back in. ``/auth/logout`` ends the Auth0 session as well as ours,
because Auth0's SSO cookie outlives our cookie and the next ``/auth/login`` would otherwise mint a
new session without a prompt.

Operational recipe, per environment (the application is a dashboard resource the tenant owners
create by hand — a Slack request per the Golden Path, not a PR; ours was posted 2026-09-09):

1. Ask for a **Regular Web Application** in the partner tenant of that environment, callback
   ``https://<host>/auth/callback`` (smart-avvik.dev.entur.io / smart-avvik.staging.entur.io /
   smart-avvik.entur.io; ``http://localhost:8000/auth/callback`` on dev for local runs — 8000 is the
   local port, 8080 is the container's), logout URL ``https://<host>/``, only the ``entur-waad``
   connection enabled, no API/audience.
2. Wire the env: ``SSO_AUTH0_DOMAIN`` and ``SSO_AUTH0_CLIENT_ID`` (configmap — neither is a
   secret; the client id is in every login redirect), ``SSO_AUTH0_CLIENT_SECRET`` and
   ``SSO_SESSION_SECRET`` (Secret Manager, their own ExternalSecret group, and the values must
   exist BEFORE the names land in Helm — see the env values files), then flip ``SSO_REQUIRED``
   to ``"true"``. ``SSO_AUTH0_CONNECTION`` overrides the connection hint (empty disables it).

All configuration is read at call time, not import (deployment.py's rule), so tests can set the
variables around a request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# The gated surfaces. Matching is per path segment (see _is_protected), and /auth/* is public by
# construction — it is how a session comes to exist.
PROTECTED_PREFIXES = (
    "/historikk",
    "/historikk-api",
    "/rushhistorikk",
    "/rushhistorikk-api",
    "/meldinger",
    "/meldinger-api",
    "/demo",
    "/demo-api",
)

_COOKIE_NAME = "smart_avvik_sso"
_SESSION_TTL_S = 12 * 3600  # a working day; renewal is a silent bounce while the Auth0 session lives
_STATE_TTL_S = 600  # how long a login redirect may sit on the Entra screen before the state expires

# The claim the tenant's action sets and the value that means Entur (auth0-tf-partner,
# add-organisationID-to-profile: `Number.parseInt` of the connection's metadata, so a JSON number).
_ORGANISATION_CLAIM = "https://entur.io/organisationID"
_ENTUR_ORGANISATION_ID = 1
# The Entra federation for Entur staff. A hint on /authorize that skips Universal Login's
# connection chooser; the application is registered with only this connection anyway.
_DEFAULT_CONNECTION = "entur-waad"
# `profile` is what puts the organisation claim on the id_token (the action tests the raw scope
# string for the word); `email` is the session identity and the audit trail.
_SCOPE = "openid email profile"
# Asymmetric only. HS256 is on the tenant's supported list and would take the client secret as the
# key — never accept it, and never pass the secret to a verifier.
_ID_TOKEN_ALGORITHMS = ("RS256", "PS256")
_CLOCK_LEEWAY_S = 30
_JWKS_TTL_S = 3600  # how long a fetched key set is served before it is re-read; an unknown kid refetches at once

_TRUTHY = ("true", "1", "yes", "on")  # the same allow-list as llm_message._TRUTHY


def required() -> bool:
    """Whether the gate is on. Unset means off — current behaviour — and is the rollback."""
    return os.environ.get("SSO_REQUIRED", "").strip().lower() in _TRUTHY


def _domain() -> str:
    """The tenant host, bare: `partner.dev.entur.org`. A pasted `https://…/` is tolerated."""
    value = os.environ.get("SSO_AUTH0_DOMAIN", "").strip()
    for prefix in ("https://", "http://"):
        value = value.removeprefix(prefix)
    return value.strip("/")


def _client_id() -> str:
    return os.environ.get("SSO_AUTH0_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("SSO_AUTH0_CLIENT_SECRET", "").strip()


def _connection() -> str:
    return os.environ.get("SSO_AUTH0_CONNECTION", _DEFAULT_CONNECTION).strip()


def configured() -> bool:
    return bool(_domain() and _client_id() and _client_secret())


# Auth0's endpoints are fixed relative to the tenant host (confirmed against all three tenants'
# /.well-known/openid-configuration on 2026-09-10); the issuer carries a trailing slash.
def _issuer() -> str:
    return f"https://{_domain()}/"


def _authorize_url() -> str:
    return f"https://{_domain()}/authorize"


def _token_url() -> str:
    return f"https://{_domain()}/oauth/token"


def _jwks_url() -> str:
    return f"https://{_domain()}/.well-known/jwks.json"


def _logout_url() -> str:
    return f"https://{_domain()}/v2/logout"


# Fallback signing key when SSO_SESSION_SECRET is absent: per-process, so sessions die with the
# pod. Fine locally; deployed environments set the secret (see the module docstring).
_EPHEMERAL_SECRET = secrets.token_bytes(32)


def _signing_key() -> bytes:
    value = os.environ.get("SSO_SESSION_SECRET", "")
    return value.encode() if len(value) >= 32 else _EPHEMERAL_SECRET


# --- Signed tokens (session cookie and OAuth state) ---------------------------------------------
#
# base64url(JSON payload) + "." + HMAC-SHA256 over purpose + payload. The purpose tag keeps a
# state token from ever passing as a session cookie or vice versa — same key, disjoint messages.


def _pack(purpose: str, payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    body = body.rstrip(b"=")
    mac = hmac.new(_signing_key(), purpose.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"{body.decode()}.{mac}"


def _unpack(purpose: str, token: str) -> dict[str, Any] | None:
    """The payload, or None for anything invalid: bad shape, bad MAC, or past its ``exp``."""
    body_str, _, mac = token.rpartition(".")
    if not body_str or not mac:
        return None
    body = body_str.encode()
    expected = hmac.new(_signing_key(), purpose.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def _make_session(email: str) -> str:
    return _pack("session", {"email": email, "exp": int(time.time()) + _SESSION_TTL_S})


def session_email(cookie: str | None) -> str | None:
    payload = _unpack("session", cookie or "")
    return payload.get("email") if payload else None


# --- Enforcement ---------------------------------------------------------------------------------


def _is_protected(path: str) -> bool:
    # Per segment, not startswith alone: /historikk must not accidentally claim a future
    # /historikk-noe sibling (that /historikk-api happens to be protected too is the list's
    # doing, not the prefix rule's).
    return any(path == p or path.startswith(p + "/") for p in PROTECTED_PREFIXES)


def _gate(scope: Scope) -> Response | None:
    """The response that ends a request, or None to let it through to the app."""
    request = Request(scope)
    if not configured():
        # Required but unconfigured serves nothing rather than everything.
        return JSONResponse({"detail": "sso_required_but_not_configured"}, status_code=503)
    if session_email(request.cookies.get(_COOKIE_NAME)):
        return None
    # A top-level browser navigation is sent to login and comes back; anything else — the SPAs'
    # own fetches, a curl — gets a plain 401. Same discriminator kix-docs' redirect middleware
    # uses: navigations say text/html, XHR does not.
    if request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", ""):
        next_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/auth/login?{urlencode({'next': next_url})}", status_code=302)
    return JSONResponse({"detail": "sso_required"}, status_code=401)


class RequireSSO:
    """Pure ASGI middleware: a couple of string compares on the hot public paths, no response
    buffering (BaseHTTPMiddleware wraps every body; the static mounts and /impact do not need
    that on each request).

    OPTIONS is exempt: this middleware is added after CORSMiddleware and therefore runs outside
    it, so gating preflights would 401 them before CORS could answer — and a preflight carries no
    cookie and discloses nothing.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] == "OPTIONS"
            or not _is_protected(scope["path"])
            or not required()
        ):
            await self.app(scope, receive, send)
            return
        response = _gate(scope)
        if response is None:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


# --- Login flow ----------------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["sso"])


def _is_local(host: str) -> bool:
    return host.split(":")[0] in ("localhost", "127.0.0.1")


def _origin(request: Request) -> str:
    """This deployment's own origin as Auth0 must see it. Derived from the Host header, with the
    scheme forced to https except locally: uvicorn runs without --proxy-headers (Dockerfile CMD),
    so request.url.scheme is plain http in-cluster while the ingress terminates TLS. A forged Host
    only produces a URL Auth0 refuses as unregistered."""
    host = request.headers.get("host") or "localhost"
    scheme = "http" if _is_local(host) else "https"
    return f"{scheme}://{host}"


def _redirect_uri(request: Request) -> str:
    return f"{_origin(request)}/auth/callback"


def _safe_next(value: str) -> str:
    """Relative paths only. ``next`` rides through the IdP round trip, and an absolute (or
    scheme-relative) value here is an open redirect on a login URL — the worst place to have one."""
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


@router.get("/login", include_in_schema=False)
def login(request: Request, next: str = "/") -> Response:
    if not (required() and configured()):
        return JSONResponse({"detail": "not_found"}, status_code=404)
    target = _safe_next(next)
    if session_email(request.cookies.get(_COOKIE_NAME)):
        return RedirectResponse(target, status_code=302)
    nonce = secrets.token_urlsafe(16)
    state = _pack("state", {"next": target, "nonce": nonce, "exp": int(time.time()) + _STATE_TTL_S})
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(request),
        "scope": _SCOPE,
        "state": state,
        "nonce": nonce,
    }
    connection = _connection()
    if connection:
        # Straight to Entra instead of Universal Login's chooser. A UX hint, NOT the enforcement —
        # that is _claims_problem's organisation check on the verified token.
        params["connection"] = connection
    return RedirectResponse(f"{_authorize_url()}?{urlencode(params)}", status_code=302)


def _exchange_code(code: str, redirect_uri: str) -> str:
    """Trade the authorization code for tokens; returns the raw ID-token JWT.

    Module-level seam so tests stub it (the alternatives.fetch_candidates shape). ``requests``
    arrives with pywebpush and is imported lazily like the other optional deps.
    """
    import requests

    resp = requests.post(
        _token_url(),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": redirect_uri,
        },
        timeout=10,
    )
    resp.raise_for_status()
    id_token_jwt = resp.json().get("id_token")
    if not id_token_jwt:
        raise ValueError("token response carried no id_token")
    return id_token_jwt


# One key-set client per JWKS URL (one per deployment in practice), so the tenant's keys are
# fetched once per _JWKS_TTL_S rather than once per login. PyJWT refetches on an unknown kid, which
# is how a tenant key rotation is picked up without waiting out the TTL.
_jwks_clients: dict[str, Any] = {}
_jwks_lock = threading.Lock()


def _jwks_client(url: str) -> Any:
    import jwt

    with _jwks_lock:
        client = _jwks_clients.get(url)
        if client is None:
            client = jwt.PyJWKClient(url, cache_keys=True, lifespan=_JWKS_TTL_S, timeout=10)
            _jwks_clients[url] = client
        return client


def _verify_id_token(id_token_jwt: str) -> dict[str, Any]:
    """Signature (against the tenant's published JWKS), issuer, audience and expiry. Also a test
    seam; the suite exercises the real thing once with a key of its own."""
    import jwt

    signing_key = _jwks_client(_jwks_url()).get_signing_key_from_jwt(id_token_jwt)
    return jwt.decode(
        id_token_jwt,
        signing_key.key,
        algorithms=list(_ID_TOKEN_ALGORITHMS),
        audience=_client_id(),
        issuer=_issuer(),
        leeway=_CLOCK_LEEWAY_S,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )


def _organisation_id(claims: dict[str, Any]) -> int | None:
    """The organisation claim as an int, or None when absent or unreadable. The action writes a
    number; a string copy of one is tolerated, a bool (which int() would read as 1) is not."""
    value = claims.get(_ORGANISATION_CLAIM)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _claims_problem(claims: dict[str, Any], expected_nonce: str) -> str | None:
    """Why these verified claims still must not become a session, or None when they may.

    The organisation check is the actual access rule — "an Entur identity", whichever partner
    tenant connection produced it. jwt.decode already refuses another issuer; the check here is
    belt and braces, not the lock.
    """
    if claims.get("iss") != _issuer():
        return "wrong_issuer"
    if claims.get("nonce") != expected_nonce:
        return "nonce_mismatch"
    organisation = _organisation_id(claims)
    if organisation is None:
        return "no_organisation"
    if organisation != _ENTUR_ORGANISATION_ID:
        return "wrong_organisation"
    if not claims.get("email"):
        return "no_email"
    return None


@router.get("/callback", include_in_schema=False)
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> Response:
    if not (required() and configured()):
        return JSONResponse({"detail": "not_found"}, status_code=404)
    state_payload = _unpack("state", state or "")
    if state_payload is None:
        return JSONResponse({"detail": "invalid_state"}, status_code=400)
    if error or not code:
        # The user, Entra or the tenant declined; nothing to verify. The slug is Auth0's, not ours
        # (`access_denied` is what its organisation action answers with), and the description is
        # logged rather than echoed.
        logger.info(
            "SSO login refused upstream: %s",
            error or "no_code",
            extra={
                "fields": {
                    "event": "sso_login",
                    "result": "refused",
                    "error": error or "no_code",
                    "description": error_description,
                }
            },
        )
        return JSONResponse({"detail": f"login_refused:{error or 'no_code'}"}, status_code=403)
    try:
        claims = _verify_id_token(_exchange_code(code, _redirect_uri(request)))
    except Exception:
        logger.warning(
            "SSO token exchange or verification failed",
            exc_info=True,
            extra={"fields": {"event": "sso_login", "result": "error"}},
        )
        return JSONResponse({"detail": "token_exchange_failed"}, status_code=502)
    problem = _claims_problem(claims, state_payload.get("nonce", ""))
    if problem:
        # The reason, never the token. Organisation, subject and email of a refused login are
        # logged for the audit trail.
        logger.warning(
            "SSO login rejected: %s",
            problem,
            extra={
                "fields": {
                    "event": "sso_login",
                    "result": "rejected",
                    "reason": problem,
                    "organisation_id": claims.get(_ORGANISATION_CLAIM),
                    "sub": claims.get("sub"),
                    "email": claims.get("email"),
                }
            },
        )
        return JSONResponse({"detail": problem}, status_code=403)
    email = claims["email"]
    logger.info(
        "SSO login: %s",
        email,
        extra={"fields": {"event": "sso_login", "result": "ok", "email": email}},
    )
    response: Response = RedirectResponse(state_payload.get("next") or "/", status_code=302)
    response.set_cookie(
        _COOKIE_NAME,
        _make_session(email),
        max_age=_SESSION_TTL_S,
        httponly=True,
        samesite="lax",
        secure=not _is_local(request.headers.get("host") or "localhost"),
        path="/",
    )
    return response


@router.get("/logout", include_in_schema=False)
def logout(request: Request) -> Response:
    """Drop our cookie and end the Auth0 session, landing back on this host's root — which is
    the application's registered logout URL, per environment. Not `federated`: that would sign
    the person out of Entra and every Microsoft 365 tab with it. With the gate off there is no
    Auth0 session to end, so the redirect is just home."""
    if required() and configured():
        params = {"client_id": _client_id(), "returnTo": f"{_origin(request)}/"}
        target = f"{_logout_url()}?{urlencode(params)}"
    else:
        target = "/"
    response: Response = RedirectResponse(target, status_code=302)
    response.delete_cookie(_COOKIE_NAME, path="/")
    return response
