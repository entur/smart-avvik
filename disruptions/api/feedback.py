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
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Literal
from urllib.request import Request as UrlRequest, urlopen

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
    return os.environ.get("POSTHOG_PROJECT_TOKEN")


def _secret() -> bytes | None:
    value = os.environ.get("VARSEL_FEEDBACK_RECEIPT_SECRET")
    return value.encode() if value and len(value) >= 32 else None


def enabled() -> bool:
    return bool(_token() and _secret() and feedback_contexts.enabled())


def parse(raw: bytes) -> FeedbackIn:
    if len(raw) > _MAX_BODY:
        raise ValueError("body_too_large")
    try:
        return FeedbackIn.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError("invalid") from exc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _receipt(response_id: uuid.UUID, context_id: str, expires: datetime) -> str:
    payload = f"{response_id}|{context_id}|{int(expires.timestamp())}".encode()
    return f"{_b64(payload)}.{_b64(hmac.new(_secret() or b'', payload, hashlib.sha256).digest())}"


def _verify(receipt: str | None, response_id: uuid.UUID, context_id: str) -> bool:
    if not receipt or "." not in receipt:
        return False
    try:
        left, signature = receipt.split(".", 1)
        payload = base64.urlsafe_b64decode(left + "=" * (-len(left) % 4))
        expected = hmac.new(_secret() or b"", payload, hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        rid, cid, expiry = payload.decode().split("|")
        return hmac.compare_digest(expected, actual) and rid == str(response_id) and cid == context_id and int(expiry) >= int(datetime.now(UTC).timestamp())
    except (ValueError, UnicodeError):
        return False


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
    }
    payload = json.dumps({"api_key": _token(), "event": EVENT, "properties": properties}).encode()
    request = UrlRequest("https://eu.i.posthog.com/i/v0/e/", data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=3) as response:  # noqa: S310 - fixed EU ingestion origin
        if response.status >= 300:
            raise RuntimeError(f"PostHog returned {response.status}")


def submit(body: FeedbackIn, *, environment: str) -> dict[str, str | int]:
    if not enabled():
        return {"status": "disabled"}
    if body.revision > 1 and not _verify(body.receipt, body.response_id, body.context_id):
        return {"status": "invalid_receipt"}
    snapshot = feedback_contexts.resolve(body.context_id)
    if snapshot is None:
        return {"status": "expired"}
    if not _claim(body.context_id):
        return {"status": "throttled"}
    persisted, reason = feedback_contexts.persist(body.context_id)
    if not persisted:
        return {"status": reason or "downstream"}
    submitted = datetime.now(UTC)
    try:
        _capture(body, environment=environment, context_date=submitted.date().isoformat(), submitted_at=submitted.isoformat())
    except Exception:
        logger.warning("Feedback capture failed", extra={"fields": {"event": "varsel_feedback", "result": "downstream"}}, exc_info=True)
        return {"status": "downstream"}
    result: dict[str, str | int] = {"status": "saved", "revision": body.revision}
    if body.revision == 1:
        result["receipt"] = _receipt(body.response_id, body.context_id, submitted + timedelta(hours=24))
    return result
