"""Strict, personless server-side capture for per-alert usefulness feedback."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import uuid
from collections import defaultdict, deque
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from typing import Literal
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import feedback_contexts
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)
EVENT = "varsel_alert_feedback"
_MAX_BODY = 2_048
_WINDOW_S = 60
_GLOBAL_PER_MIN = 60
_PER_CONTEXT_PER_MIN = 12
_DAILY_BUDGET = 2_000
_lock = threading.Lock()
_global: deque[float] = deque()
_per_context: dict[str, deque[float]] = defaultdict(deque)
_day = datetime.now(UTC).date()
_daily = 0
_inflight = threading.BoundedSemaphore(4)


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    response_id: uuid.UUID
    event_id: uuid.UUID
    revision: int = Field(ge=1, le=10_000)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=500)
    receipt: str | None = Field(default=None, max_length=1024)

    @field_validator("response_id", "event_id")
    @classmethod
    def uuid4_only(cls, value: uuid.UUID) -> uuid.UUID:
        if value.version != 4:
            raise ValueError("must be UUIDv4")
        return value


def _token() -> str | None:
    # The shape is checked here because nothing downstream can check the VALUE. Measured against
    # eu.i.posthog.com on 2026-08-28: /i/v0/e/ is fire-and-forget, so EVERY non-empty api_key —
    # a placeholder, a well-formed token for someone else's project, random noise — returns
    # HTTP 200 {"status":"Ok"} and is only validated later, asynchronously, at ingestion. Only an
    # *empty* key 401s. So _capture's `response.status >= 300` check cannot tell a wrong token from
    # a right one, and a bad one discards every traveller's feedback in complete silence while
    # submit() still reports "saved" and still writes the route-bearing context row to BigQuery.
    # A "phc_" project write token is what belongs here. Rejecting anything else catches the two
    # mistakes that actually happen — a placeholder secret version, and a personal API key ("phx_")
    # pasted where the project token goes — and it fails SAFE rather than silent: enabled() reads
    # False, so no context is persisted either and /varsel-api/config reports capture off, which is
    # a one-request diagnosis. What it cannot catch is a well-formed token for the WRONG project;
    # the only detector for that is an export returning zero responses beside a growing
    # varsel_feedback_contexts, which is why data/feedback/README.md names that check.
    value = os.environ.get("POSTHOG_PROJECT_TOKEN") or ""
    return value if value.startswith("phc_") and len(value) >= 32 else None


def _secret() -> bytes | None:
    value = os.environ.get("VARSEL_FEEDBACK_RECEIPT_SECRET")
    return value.encode() if value and len(value) >= 32 else None


def enabled() -> bool:
    return (
        os.environ.get("VARSEL_FEEDBACK_ENABLED", "").lower() == "true"
        and os.environ.get("POSTHOG_HOST") == "https://eu.i.posthog.com"
        and bool(_token() and _secret() and feedback_contexts.enabled())
    )


def parse(raw: bytes) -> FeedbackIn:
    if len(raw) > _MAX_BODY:
        raise ValueError("body_too_large")
    try:
        return FeedbackIn.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError("invalid") from exc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _receipt(
    response_id: uuid.UUID, context_id: str, context_date: str, expires: datetime
) -> str:
    payload = f"{response_id}|{context_id}|{context_date}|{int(expires.timestamp())}".encode()
    return f"{_b64(payload)}.{_b64(hmac.new(_secret() or b'', payload, hashlib.sha256).digest())}"


def _verify(receipt: str | None, response_id: uuid.UUID, context_id: str) -> str | None:
    if not receipt or "." not in receipt:
        return None
    try:
        left, signature = receipt.split(".", 1)
        payload = base64.urlsafe_b64decode(left + "=" * (-len(left) % 4))
        expected = hmac.new(_secret() or b"", payload, hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        rid, cid, context_date, expiry = payload.decode().split("|")
        valid = (
            hmac.compare_digest(expected, actual)
            and rid == str(response_id)
            and cid == context_id
            and int(expiry) >= int(datetime.now(UTC).timestamp())
        )
        date.fromisoformat(context_date)
        return context_date if valid else None
    except (ValueError, UnicodeError):
        return None


def _claim(context_id: str) -> bool:
    global _day, _daily
    now = monotonic()
    today = datetime.now(UTC).date()
    with _lock:
        if today != _day:
            _day, _daily = today, 0
        while _global and now - _global[0] >= _WINDOW_S:
            _global.popleft()
        local = _per_context[context_id]
        while local and now - local[0] >= _WINDOW_S:
            local.popleft()
        if len(_global) >= _GLOBAL_PER_MIN or len(local) >= _PER_CONTEXT_PER_MIN or _daily >= _DAILY_BUDGET:
            return False
        _global.append(now)
        local.append(now)
        _daily += 1
        while len(_per_context) > 10_000:
            _per_context.pop(next(iter(_per_context)))
        return True


def _capture(body: FeedbackIn, *, environment: str, context_date: str, submitted_at: str) -> None:
    # Scope PostHog's mandatory distinct_id to this exact response and display context. Even a
    # hostile client reusing its response UUID cannot create a cross-alert/cross-environment person.
    distinct_id = "fb_" + hashlib.sha256(
        f"{environment}|{body.context_id}|{body.response_id}".encode()
    ).hexdigest()[:32]
    properties = {
        "distinct_id": distinct_id,
        "context_id": body.context_id,
        "response_id": str(body.response_id),
        "event_id": str(body.event_id),
        "revision": body.revision,
        "rating": body.rating,
        "comment": body.comment,
        "context_date": context_date,
        "environment": environment,
        "submitted_at": submitted_at,
        "$insert_id": str(body.event_id),
        "$process_person_profile": False,
        # PostHog stamps $ip from the socket unless the payload supplies a VALID one, so the only way
        # to keep an address out of the store is to occupy the field. Measured against the real
        # project 2026-08-28, five probes: absent, null and "" all recorded the caller's real
        # address — null and empty are read as "not supplied" — while "0.0.0.0" survived intact (as
        # did an X-Forwarded-For header, not used here: relying on PostHog's proxy trust is a weaker
        # contract than occupying the property). The project's "discard client IP data" setting did
        # not suppress it either, which is the other reason this belongs in code: a privacy
        # guarantee that depends on a console toggle is one click from silently ending.
        # Nothing here wants an IP. Capture is server-side, so the address would be this pod's
        # egress — Entur infrastructure, not the traveller, whose browser never contacts PostHog —
        # and a developer running data/feedback's synthetic round-trip from a laptop would otherwise
        # write their own residential address into a store with 30-day retention and no per-event
        # deletion path.
        "$ip": "0.0.0.0",
    }
    payload = json.dumps({"api_key": _token(), "event": EVENT, "properties": properties}).encode()
    request = UrlRequest(
        f"{os.environ['POSTHOG_HOST']}/i/v0/e/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        if response.status >= 300:
            raise RuntimeError(f"PostHog returned {response.status}")


def submit(body: FeedbackIn, *, environment: str) -> dict[str, str | int]:
    if not enabled():
        return {"status": "disabled"}
    context_date = (
        _verify(body.receipt, body.response_id, body.context_id)
        if body.revision > 1
        else None
    )
    if body.revision > 1 and context_date is None:
        return {"status": "invalid_receipt"}
    if body.revision == 1 and feedback_contexts.resolve(body.context_id) is None:
        return {"status": "expired"}
    if not _claim(body.context_id):
        return {"status": "throttled"}
    if not _inflight.acquire(blocking=False):
        return {"status": "throttled"}
    try:
        submitted = datetime.now(UTC)
        if body.revision == 1:
            context_date = submitted.date().isoformat()
            persisted, reason = feedback_contexts.persist(
                body.context_id, context_date=context_date
            )
            if not persisted:
                return {"status": reason or "downstream"}
        try:
            _capture(
                body,
                environment=environment,
                context_date=context_date or submitted.date().isoformat(),
                submitted_at=submitted.isoformat(),
            )
        except Exception:
            logger.warning("Feedback capture failed", extra={"fields": {"event": "varsel_feedback", "result": "downstream"}}, exc_info=True)
            return {"status": "downstream"}
    finally:
        _inflight.release()
    result: dict[str, str | int] = {"status": "saved", "revision": body.revision}
    if body.revision == 1:
        result["receipt"] = _receipt(
            body.response_id,
            body.context_id,
            context_date or submitted.date().isoformat(),
            submitted + timedelta(hours=24),
        )
    return result
