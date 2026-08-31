"""Entur-SSO gate for the internal surfaces: /historikk and /meldinger, pages and APIs both.

Google OIDC against the entur.org Workspace — the same flow ki.entur.no and Kompass use — enforced
in this process, because nothing upstream enforces anything: the platform ingress is a shared
host router, and the kix gateway's SSO is a portal-UI property, not an HTTP one (its own host
serves ``/kix/*`` to anyone). The choice of Google over Entra ID is practical, not doctrinal: an
OAuth client in this app's own GCP project is self-service (consent screen "Internal" restricts it
to the entur.org organisation), while an Entra app registration is a request to another team. The
flow and this module are issuer-agnostic enough that swapping later is a config-plus-verifier
change, not a redesign.

What is gated is exactly :data:`PROTECTED_PREFIXES` — entries are absolute path prefixes, so a
new one MUST start with "/" (every ASGI path does; an unslashed entry matches nothing and the
surface silently stays public — shipped once, 2026-08-31, pinned by the test suite since).
Everything else — /varsel, /togmelding, the read-only APIs — is deliberately untouched: those are
the traveller-facing surfaces, and this gate exists so the internal ones (and the partner demo)
can stop being public without moving anybody's URL.

**Failure posture: closed.** ``SSO_REQUIRED=true`` with a missing client id/secret serves the
protected paths as 503, never as public — a deployment that loses an env var must not quietly
republish the decision log. ``SSO_REQUIRED`` unset or falsy is the documented off switch and the
rollback: everything serves exactly as before this module existed.

**Sessions** are a signed cookie (HMAC-SHA256 over ``{email, exp}``), 12 h, HttpOnly,
SameSite=Lax. There is no server-side session store and no revocation — acceptable for a gate
whose failure mode was "public". If ``SSO_SESSION_SECRET`` is unset (or under 32 chars) the key is
per-process, so every redeploy logs everyone out; the re-login is a silent redirect bounce while
the browser's Google session lives, but in-flight XHR on an open page gets 401s until reload, so
deployed environments should set the secret. Expiry behaves the same way: the SPAs' fetches start
returning 401 and a reload logs back in.

Operational recipe, per environment (the OAuth client cannot be Terraformed — it is a console
resource, like the VAPID keypair):

1. In ``ent-kixavvik-<env>``: APIs & Services → OAuth consent screen → User type **Internal**.
2. Credentials → Create credentials → OAuth client ID → **Web application**, authorized redirect
   URI ``https://<host>/auth/callback`` (kix-avvik.dev.entur.io / kix-avvik.staging.entur.io /
   kix-avvik.entur.io; add ``http://localhost:8080/auth/callback`` for local runs if wanted).
3. Wire the env: ``SSO_GOOGLE_CLIENT_ID`` (configmap — it is not a secret),
   ``SSO_GOOGLE_CLIENT_SECRET`` and ``SSO_SESSION_SECRET`` (Secret Manager, like the VAPID keys),
   then flip ``SSO_REQUIRED`` to ``"true"``.

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
    "/meldinger",
    "/meldinger-api",
    "/demo",
    "/demo-api",
)

_COOKIE_NAME = "kix_sso"
_SESSION_TTL_S = 12 * 3600  # a working day; renewal is a silent bounce while the Google session lives
_STATE_TTL_S = 600  # how long a login redirect may sit on the Google screen before the state expires

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

_TRUTHY = ("true", "1", "yes", "on")  # the same allow-list as llm_message._TRUTHY


def required() -> bool:
    """Whether the gate is on. Unset means off — current behaviour — and is the rollback."""
    return os.environ.get("SSO_REQUIRED", "").strip().lower() in _TRUTHY


def _client_id() -> str:
    return os.environ.get("SSO_GOOGLE_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("SSO_GOOGLE_CLIENT_SECRET", "").strip()


def configured() -> bool:
    return bool(_client_id() and _client_secret())


def _allowed_domain() -> str:
    return os.environ.get("SSO_ALLOWED_DOMAIN", "entur.org").strip().lower()


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


def _redirect_uri(request: Request) -> str:
    """The callback URL as Google must see it. Derived from the Host header, with the scheme
    forced to https except locally: uvicorn runs without --proxy-headers (Dockerfile CMD), so
    request.url.scheme is plain http in-cluster while the ingress terminates TLS. A forged Host
    only produces a redirect_uri Google refuses as unregistered."""
    host = request.headers.get("host") or "localhost"
    scheme = "http" if _is_local(host) else "https"
    return f"{scheme}://{host}/auth/callback"


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
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        # A UX hint that preselects the entur.org account on Google's chooser. It is NOT the
        # enforcement — that is _claims_problem's hd check on the verified token.
        "hd": _allowed_domain(),
    }
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)


def _exchange_code(code: str, redirect_uri: str) -> str:
    """Trade the authorization code for tokens; returns the raw ID-token JWT.

    Module-level seam so tests stub it (the alternatives.fetch_candidates shape). ``requests``
    arrives with pywebpush and is imported lazily like the other optional deps.
    """
    import requests

    resp = requests.post(
        _GOOGLE_TOKEN_URL,
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


def _verify_id_token(id_token_jwt: str) -> dict[str, Any]:
    """Signature (against Google's published certs), issuer, audience and expiry — google-auth is
    already in the image via the BigQuery client. Also a test seam."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(
        id_token_jwt, google_requests.Request(), audience=_client_id()
    )


def _claims_problem(claims: dict[str, Any], expected_nonce: str) -> str | None:
    """Why these verified claims still must not become a session, or None when they may.

    The hd check is the actual access rule — "an entur.org Workspace identity" — and the email
    suffix check restates it from a second claim on purpose. verify_oauth2_token already refuses
    a non-Google issuer; the check here is belt and braces, not the lock.
    """
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        return "wrong_issuer"
    if claims.get("nonce") != expected_nonce:
        return "nonce_mismatch"
    if not claims.get("email_verified"):
        return "email_not_verified"
    domain = _allowed_domain()
    if claims.get("hd") != domain:
        return "wrong_hosted_domain"
    email = (claims.get("email") or "").lower()
    if not email.endswith("@" + domain):
        return "wrong_email_domain"
    return None


@router.get("/callback", include_in_schema=False)
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    if not (required() and configured()):
        return JSONResponse({"detail": "not_found"}, status_code=404)
    state_payload = _unpack("state", state or "")
    if state_payload is None:
        return JSONResponse({"detail": "invalid_state"}, status_code=400)
    if error or not code:
        # The user (or Google) declined; nothing to verify. The error slug is Google's, not ours.
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
        # The reason, never the token. hd/email of a refused login are logged for the audit trail.
        logger.warning(
            "SSO login rejected: %s",
            problem,
            extra={
                "fields": {
                    "event": "sso_login",
                    "result": "rejected",
                    "reason": problem,
                    "hd": claims.get("hd"),
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
def logout() -> Response:
    response: Response = RedirectResponse("/", status_code=302)
    response.delete_cookie(_COOKIE_NAME, path="/")
    return response
