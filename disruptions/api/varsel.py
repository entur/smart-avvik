"""Avviksvarsel — registered-journey push alerts on top of the per-call prediction cache.

A traveller registers a journey (from-stop → to-stop, a recurring daily time window and a
weekday selector) from the PWA at ``/varsel``. Whenever the poller refreshes the per-call
prediction cache (``_call_pred_by_situation`` in main.py), :func:`check_and_notify` matches every
registered journey against the live situations and sends a Web Push notification for any newly
alert-worthy match. Tapping it opens the app, which lists the journeys and the disruptions
currently hitting them (served by :func:`GET /varsel-api/journeys`).

The matcher mirrors ``main._trip_cells``: a journey matches a situation when that situation flags
a train departing ``from_stop`` inside the journey's weekday+time window that later calls at
``to_stop`` (higher stop_order = same direction). The severity shown/pushed is the worst such
train's calibrated cancellation / delay / impact numbers.

Design notes
------------
* **Weekdays** are ``0=Monday … 6=Sunday`` (Python ``date.weekday()``), consistent with the PWA.
* **Windows** are non-wrapping inclusive ``[begin, end]`` in Europe/Oslo local time; the PWA
  validates ``begin < end``, so overnight windows never reach the matcher (and are ignored if
  they somehow do).
* **Push gate** — a match is *pushed* only when the worst matching train crosses the same
  production alert threshold used by ``/demo`` and eval (``P(cancelled OR ≥10 min late) ≥ 0.65``),
  so trivial flags don't spam the phone. The in-app list shows *all* current matches regardless.
* **State** (devices / journeys / notified) is tiny JSON, guarded by a re-entrant lock and written
  through to a local file and — when ``VARSEL_STATE_BUCKET`` is set — a GCS blob, so it survives a
  Cloud Run redeploy (the container fs is tmpfs). Single always-on instance ⇒ last-write-wins is
  safe.
* **VAPID** keys come from env (``VAPID_PRIVATE_KEY`` raw base64url, ``VAPID_PUBLIC_KEY``,
  ``VAPID_SUBJECT``). When unset, push is disabled gracefully: ``/config`` reports
  ``push_enabled=false`` and :func:`check_and_notify` is a no-op, so the app still renders the
  journey list.

No import of main.py (avoids a cycle): main injects a snapshot provider (the live
``_call_pred_by_situation``) and a stops provider via :func:`configure`, and passes the snapshot
directly to :func:`check_and_notify`. The matcher only reads attributes off the cached call
objects, so it duck-types the ``_CallPred`` shape.
"""

import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

# Push a match only when the worst matching train's impact probability crosses this — the same
# operating threshold as /demo and eval/impact_eval.py (>=91 % precision at every ask horizon).
_ALERT_THRESHOLD = 0.65
# A journey counts as "trouble" when cancelled or >= this many minutes late (matches the impact
# probability's definition), used for the notification wording.
_TROUBLE_DELAY_MIN = 10

# Per-operator alert threshold overrides (default _ALERT_THRESHOLD). The pooled bus family is
# calibrated per operator, but a single 0.65 cut still under-/over-alerts a few operators (e.g.
# KOL/OST — see docs/pooled-models-bus/LEARNINGS.md). Populate from eval/pooled-model-bus-eval.py.
_OPERATOR_ALERT_THRESHOLD: dict[str, float] = {}


def _threshold_for(operator: str | None) -> float:
    return _OPERATOR_ALERT_THRESHOLD.get(operator or "", _ALERT_THRESHOLD)


# Rail codespaces (operator == ref codespace). vehicleMode is NULL for most bus producers, so the
# vehicle noun is derived from the codespace, not the feed; everything not rail is labelled "Buss".
_RAIL_CODESPACES = frozenset({"VYG", "NSB", "GOA", "SJN", "FLT"})


def mode_label(operator: str | None) -> str:
    """User-facing vehicle noun for an operator: 'Tog' (rail) or 'Buss' (everything else)."""
    return "Tog" if operator in _RAIL_CODESPACES else "Buss"


# FOLLOW-UP (needs its own eval before enabling): MOR/ATB/OST/GCO publish the SX *after* departure,
# so match_journey's pre-departure boarding requirement means those journeys rarely match. Serving
# them well needs an "alert at message arrival" policy (arrival-side impact, relaxed future-boarding
# requirement); deferred until validated. See docs/pooled-models-bus/LEARNINGS.md (Lead time).
_AFTER_DEPARTURE_OPERATORS = frozenset({"MOR", "ATB", "OST", "GCO"})
# Keep state bounded: a client-generated device may register at most this many journeys.
_MAX_JOURNEYS_PER_DEVICE = 20
# A freshly-created push subscription can 404/410 at the push service for a short window before
# it has propagated (seen especially with FCM/Android). Within this grace period we treat such a
# response as transient and do NOT drop the device, so an immediate test-push or first alert
# doesn't silently unsubscribe the user.
_SUBSCRIPTION_GRACE = timedelta(minutes=10)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---- VAPID / Web Push config ----------------------------------------------------------------
# Loaded once at import from env. Push is disabled (all-None) when the private key is absent, so
# local runs and un-provisioned deploys degrade to "list only" instead of erroring.
_VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
_VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
_VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:kixavvik@entur.org")

_vapid: Any | None = None
if _VAPID_PRIVATE_KEY:
    try:
        from py_vapid import Vapid02

        _vapid = Vapid02.from_raw(_VAPID_PRIVATE_KEY.encode("utf-8"))
        logger.info("VAPID configured; Web Push enabled")
    except Exception:
        logger.exception("VAPID private key present but could not be loaded; push disabled")
        _vapid = None
else:
    logger.info("No VAPID_PRIVATE_KEY set; Web Push disabled (journey list still served)")


def push_enabled() -> bool:
    """True when VAPID keys are configured and a public key can be served to the client."""
    return _vapid is not None and bool(_VAPID_PUBLIC_KEY)


# ---- Injected providers (set by main.configure to avoid an import cycle) ---------------------
_snapshot_provider: Callable[[], dict[str, list]] | None = None
_stops_provider: Callable[[], list[dict]] | None = None


def configure(
    *,
    snapshot_provider: Callable[[], dict[str, list]],
    stops_provider: Callable[[], list[dict]],
) -> None:
    """Wire in main.py's live per-call snapshot and rail-stop list (called once at startup)."""
    global _snapshot_provider, _stops_provider
    _snapshot_provider = snapshot_provider
    _stops_provider = stops_provider


def _snapshot() -> dict[str, list]:
    return _snapshot_provider() if _snapshot_provider is not None else {}


# ---- State: devices, journeys, notified ------------------------------------------------------
# Re-entrant lock: mutations call _save_state while already holding it.
_state_lock = threading.RLock()
_devices: dict[str, dict] = {}  # device_id -> {"subscription": {...}, "created_ts": iso}
_journeys: dict[str, dict] = {}  # journey_id -> journey record (see POST /journeys)
_notified: set[str] = set()  # {"journey_id|situationNumber"} — dedup, pruned when a situation ends

_STATE_PATH = Path(os.environ.get("VARSEL_STATE_PATH", "/tmp/varsel_state.json"))
_STATE_BUCKET = os.environ.get("VARSEL_STATE_BUCKET")  # GCS bucket for durable state (optional)
_STATE_BLOB = os.environ.get("VARSEL_STATE_BLOB", "varsel_state.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_state() -> str:
    with _state_lock:
        return json.dumps(
            {"devices": _devices, "journeys": _journeys, "notified": sorted(_notified)}
        )


def _apply_state(raw: str) -> None:
    data = json.loads(raw)
    with _state_lock:
        global _devices, _journeys, _notified
        _devices = dict(data.get("devices", {}))
        _journeys = dict(data.get("journeys", {}))
        _notified = set(data.get("notified", []))


def _gcs_blob():
    """The state blob handle when a bucket is configured, else None (import is lazy)."""
    if not _STATE_BUCKET:
        return None
    from google.cloud import storage

    return storage.Client().bucket(_STATE_BUCKET).blob(_STATE_BLOB)


def load_state() -> None:
    """Load persisted state at startup — from GCS when configured, else the local file.

    Tolerant: a missing/corrupt store just starts empty (logged), so a first-ever run or a fresh
    bucket doesn't block serving."""
    try:
        blob = _gcs_blob()
        if blob is not None:
            if blob.exists():
                _apply_state(blob.download_as_text())
                logger.info(
                    "Loaded varsel state from gs://%s/%s: %d devices, %d journeys",
                    _STATE_BUCKET,
                    _STATE_BLOB,
                    len(_devices),
                    len(_journeys),
                )
            else:
                logger.info("No varsel state blob yet at gs://%s/%s", _STATE_BUCKET, _STATE_BLOB)
            return
        if _STATE_PATH.exists():
            _apply_state(_STATE_PATH.read_text())
            logger.info(
                "Loaded varsel state from %s: %d devices, %d journeys",
                _STATE_PATH,
                len(_devices),
                len(_journeys),
            )
    except Exception:
        logger.exception("Could not load varsel state; starting empty")


def _save_state() -> None:
    """Write state through to the local file and (when configured) the GCS blob. Best-effort:
    a persistence failure is logged, not raised — the in-memory state is still authoritative."""
    payload = _serialize_state()
    try:
        _STATE_PATH.write_text(payload)
    except Exception:
        logger.exception("Could not write varsel state to %s", _STATE_PATH)
    try:
        blob = _gcs_blob()
        if blob is not None:
            blob.upload_from_string(payload, content_type="application/json")
    except Exception:
        logger.exception("Could not upload varsel state to gs://%s/%s", _STATE_BUCKET, _STATE_BLOB)


# ---- Matcher ---------------------------------------------------------------------------------
def _parse_hhmm(value: str) -> int:
    """"HH:MM" -> minutes since midnight."""
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def _dep_p90(call: Any) -> int | None:
    """Boarding-side p90 delay, preferring the calibrated exceedance stack (as main.py does)."""
    return call.dep_delay_p90_stack_min if call.dep_delay_p90_stack_min is not None else call.dep_delay_p90_min


class Alert(BaseModel):
    """One situation currently hitting a registered journey — the worst matching train's numbers."""

    situation_number: str = Field(description="SIRI-SX situationNumber of the disruption.")
    summary: str | None = Field(description="Short Norwegian summary of the situation.")
    operator: str | None = Field(
        default=None,
        description="Ref codespace of the worst affected journey (VYG, RUT, KOL, ...).",
    )
    mode: str = Field(default="Tog", description="Vehicle noun for the UI: 'Tog' or 'Buss'.")
    line: str | None = Field(description="Line ref of the worst affected vehicle, e.g. 'VYG:Line:R31'.")
    departure_time: datetime | None = Field(
        description="Aimed departure from the boarding stop of the worst affected train (UTC).",
    )
    cancel_probability: float = Field(description="Calibrated P(the train is cancelled), 0-1.")
    delay_typ_min: int | None = Field(description="Typical departure delay (min) if it runs.")
    delay_p90_min: int | None = Field(description="Bad-case (p90) departure delay (min).")
    impact_probability: float | None = Field(
        default=None,
        description="Calibrated P(cancelled OR >= 10 min late) at boarding — the alerting score.",
    )
    alertworthy: bool = Field(
        description="Whether this match crosses the push threshold (a notification is/was sent).",
    )


def _severity(call: Any) -> tuple[float, int]:
    """Order matching trains most-severe first: cancellation prob, then bad-case delay."""
    return (call.p_cancel, _dep_p90(call) or 0)


def match_journey(journey: dict, by_situation: dict[str, list]) -> list[Alert]:
    """Situations currently hitting `journey`, worst-first — one :class:`Alert` per situation.

    For each situation, group its scored calls by dated journey (jkey). A train matches when it has
    a boarding call at ``from_stop`` whose aimed departure (in Europe/Oslo) falls on one of the
    journey's weekdays and inside its ``[begin, end]`` window, AND the same train later calls at
    ``to_stop`` (a call with higher stop_order — the travel direction). Per situation the worst such
    train (highest cancellation prob, then p90 delay) supplies the alert's numbers.
    """
    days = set(journey["days"])
    begin_min = _parse_hhmm(journey["begin"])
    end_min = _parse_hhmm(journey["end"])
    from_stop = journey["from_stop"]
    to_stop = journey["to_stop"]

    alerts: list[Alert] = []
    for sit_num, calls in by_situation.items():
        # Group by (operator, jkey): bus ServiceJourney jkeys are not date-scoped, so a single
        # situation (e.g. a GCO notice fanning across county codespaces) can carry the same jkey for
        # two different operators' journeys — keying on jkey alone would merge them.
        by_key: dict[tuple[str, str], list] = {}
        for c in calls:
            by_key.setdefault((c.operator, c.jkey), []).append(c)

        matching_boardings: list[Any] = []
        for group in by_key.values():
            board = next(
                (
                    c
                    for c in group
                    if c.stop_place == from_stop and c.aimed_dep is not None
                ),
                None,
            )
            if board is None:
                continue
            local = board.aimed_dep.astimezone(_OSLO)
            tod = local.hour * 60 + local.minute
            if local.weekday() not in days or not (begin_min <= tod <= end_min):
                continue
            # Direction: the same train must call at to_stop further along the line.
            if not any(
                c.stop_place == to_stop and c.stop_order > board.stop_order for c in group
            ):
                continue
            matching_boardings.append(board)

        if not matching_boardings:
            continue
        worst = max(matching_boardings, key=_severity)
        impact = worst.p_impact_dep
        threshold = _threshold_for(worst.operator)
        alertworthy = (
            impact >= threshold
            if impact is not None
            else worst.p_cancel >= threshold
        )
        alerts.append(
            Alert(
                situation_number=sit_num,
                summary=worst.summary,
                operator=worst.operator,
                mode=mode_label(worst.operator),
                line=worst.line,
                departure_time=worst.aimed_dep,
                cancel_probability=worst.p_cancel,
                delay_typ_min=worst.dep_delay_typ_min,
                delay_p90_min=_dep_p90(worst),
                impact_probability=impact,
                alertworthy=alertworthy,
            )
        )
    alerts.sort(key=lambda a: (a.cancel_probability, a.delay_p90_min or 0), reverse=True)
    return alerts


# ---- Notify ----------------------------------------------------------------------------------
def _notification_payload(journey: dict, alert: Alert) -> dict:
    """Norwegian Web Push payload for one journey×situation match."""
    from_name = journey.get("from_name") or journey["from_stop"]
    to_name = journey.get("to_name") or journey["to_stop"]
    pct = round(alert.cancel_probability * 100)
    parts = []
    if alert.departure_time is not None:
        hhmm = alert.departure_time.astimezone(_OSLO).strftime("%H:%M")
        parts.append(f"{alert.mode} kl {hhmm}:")
    parts.append(f"{pct} % sjanse for innstilling")
    if alert.delay_typ_min:
        parts.append(f"typisk {alert.delay_typ_min} min forsinket")
    detail = ", ".join(parts[1:]) if len(parts) > 1 else parts[0]
    lead = (parts[0] + " " + detail) if alert.departure_time is not None else detail
    body = f"{alert.summary}\n{lead}" if alert.summary else lead
    return {
        "title": f"Avvik: {from_name} → {to_name}",
        "body": body,
        "url": "/varsel/",
        "situation_number": alert.situation_number,
    }


def _send_push(subscription: dict, payload: dict) -> str:
    """Send one Web Push. Returns 'sent', 'gone' (404/410 — drop the device) or 'error' (retry)."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=_vapid,
            vapid_claims={"sub": _VAPID_SUBJECT},
            ttl=3600,
        )
        return "sent"
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        body = (getattr(exc.response, "text", "") or "")[:400]
        endpoint = str(subscription.get("endpoint", ""))
        host = endpoint.split("/", 3)[2] if "://" in endpoint else endpoint[:40]
        logger.warning(
            "Web push failed: status=%s host=%s body=%r exc=%s", status, host, body, exc
        )
        if status in (404, 410):
            return "gone"
        return "error"
    except Exception:
        logger.exception("Web push raised unexpectedly")
        return "error"


def _drop_device(device_id: str) -> None:
    """Forget a device whose push subscription is gone (404/410). Journeys are kept — the client
    re-attaches its subscription (same device_id) if the user re-enables notifications."""
    with _state_lock:
        if _devices.pop(device_id, None) is not None:
            logger.info("Dropped expired push subscription for device %s", device_id)


def _device_is_fresh(device_id: str) -> bool:
    """True while a device's subscription is younger than the propagation grace window. A 404/410
    for such a subscription is likely a transient push-service delay, not a dead endpoint, so the
    caller should retry later rather than drop it."""
    with _state_lock:
        dev = _devices.get(device_id)
    if dev is None:
        return False
    try:
        created = datetime.fromisoformat(dev["created_ts"])
    except (KeyError, ValueError):
        return False
    return datetime.now(timezone.utc) - created < _SUBSCRIPTION_GRACE


def check_and_notify(by_situation: dict[str, list]) -> None:
    """Send a push for every registered journey × newly alert-worthy situation.

    Called at the end of main's ingestion-change rebuild with the freshly-refreshed per-call
    snapshot. Deduped via ``_notified`` (one push per journey×situation until that situation leaves
    the cache); entries for ended situations are pruned so a returning situation re-alerts and the
    set stays bounded. No-op when push is disabled.
    """
    if not push_enabled():
        return
    with _state_lock:
        journeys = list(_journeys.items())
        # Prune notified entries whose situation is no longer live. In-place update (not `-=`)
        # so `_notified` stays the module global rather than becoming a function-local.
        live = set(by_situation.keys())
        stale = {k for k in _notified if k.split("|", 1)[1] not in live}
        _notified.difference_update(stale)
        changed = bool(stale)

    for jid, journey in journeys:
        device = None
        with _state_lock:
            if jid not in _journeys:  # deleted while iterating
                continue
            dev = _devices.get(journey["device_id"])
            if dev is not None:
                device = dev.get("subscription")
        if device is None:
            continue
        for alert in match_journey(journey, by_situation):
            if not alert.alertworthy:
                continue
            key = f"{jid}|{alert.situation_number}"
            with _state_lock:
                if key in _notified:
                    continue
            result = _send_push(device, _notification_payload(journey, alert))
            if result == "sent":
                with _state_lock:
                    _notified.add(key)
                changed = True
            elif result == "gone":
                if _device_is_fresh(journey["device_id"]):
                    # Likely transient (subscription still propagating): keep it, retry next tick.
                    logger.info(
                        "Transient push failure for fresh device %s; keeping subscription",
                        journey["device_id"],
                    )
                else:
                    _drop_device(journey["device_id"])
                    changed = True
                break  # stop this device's remaining alerts this tick either way

    if changed:
        _save_state()


# ---- API models ------------------------------------------------------------------------------
class ConfigResponse(BaseModel):
    """What the PWA needs to decide whether/how to subscribe to push."""

    push_enabled: bool = Field(description="Whether the server can send Web Push (VAPID configured).")
    vapid_public_key: str | None = Field(
        description="VAPID application server key (base64url) for pushManager.subscribe; null if disabled.",
    )


class StopItem(BaseModel):
    """A rail stop offered in the from/to pickers."""

    stop_place: str = Field(description="NSR StopPlace ref.")
    stop_name: str | None = Field(description="Human-readable stop name.")


class DeviceIn(BaseModel):
    """A device registering (or refreshing) its Web Push subscription."""

    device_id: str = Field(min_length=8, max_length=100, description="Client-generated UUID.")
    subscription: dict = Field(description="The PushSubscription JSON (endpoint + keys).")

    @field_validator("subscription")
    @classmethod
    def _has_endpoint(cls, v: dict) -> dict:
        if not isinstance(v, dict) or not v.get("endpoint"):
            raise ValueError("subscription must include an endpoint")
        return v


class JourneyIn(BaseModel):
    """A journey to register for alerts."""

    device_id: str = Field(min_length=8, max_length=100)
    from_stop: str = Field(min_length=1, max_length=80)
    from_name: str | None = Field(default=None, max_length=120)
    to_stop: str = Field(min_length=1, max_length=80)
    to_name: str | None = Field(default=None, max_length=120)
    begin: str = Field(description='Window start "HH:MM" (Europe/Oslo).')
    end: str = Field(description='Window end "HH:MM" (Europe/Oslo); must be after begin.')
    days: list[int] = Field(description="Weekdays to watch: 0=Monday … 6=Sunday.")

    @field_validator("begin", "end")
    @classmethod
    def _valid_hhmm(cls, v: str) -> str:
        if not _HHMM_RE.match(v):
            raise ValueError("time must be HH:MM (24h)")
        return v

    @field_validator("days")
    @classmethod
    def _valid_days(cls, v: list[int]) -> list[int]:
        uniq = sorted(set(v))
        if not uniq or any(d < 0 or d > 6 for d in uniq):
            raise ValueError("days must be a non-empty subset of 0..6 (0=Monday)")
        return uniq

    @model_validator(mode="after")
    def _checks(self) -> "JourneyIn":
        if self.from_stop == self.to_stop:
            raise ValueError("from_stop and to_stop must differ")
        if _parse_hhmm(self.begin) >= _parse_hhmm(self.end):
            raise ValueError("begin must be before end (overnight windows are not supported)")
        return self


class Journey(BaseModel):
    """A registered journey plus the situations currently hitting it."""

    journey_id: str
    from_stop: str
    from_name: str | None
    to_stop: str
    to_name: str | None
    begin: str
    end: str
    days: list[int] = Field(description="0=Monday … 6=Sunday.")
    created_ts: str
    alerts: list[Alert] = Field(description="Situations currently affecting this journey, worst first.")


class JourneysResponse(BaseModel):
    journeys: list[Journey]


class TestIn(BaseModel):
    """Request a test push to verify the pipe end to end."""

    device_id: str = Field(min_length=8, max_length=100)


# ---- Router ----------------------------------------------------------------------------------
router = APIRouter(prefix="/varsel-api", tags=["varsel"])


@router.get("/config", summary="Push capability + VAPID public key")
async def config() -> ConfigResponse:
    """Whether the server can send Web Push, and (if so) the application server key the PWA passes
    to ``pushManager.subscribe``."""
    return ConfigResponse(
        push_enabled=push_enabled(),
        vapid_public_key=_VAPID_PUBLIC_KEY if push_enabled() else None,
    )


@router.get("/stops", summary="Rail stops for the journey pickers")
async def stops() -> list[StopItem]:
    """The Vy rail stops currently in the planned timetable, for the from/to dropdowns."""
    rows = _stops_provider() if _stops_provider is not None else []
    return [StopItem(stop_place=r["stop_place"], stop_name=r.get("stop_name")) for r in rows]


@router.post("/devices", summary="Register or refresh a device's push subscription")
async def upsert_device(body: DeviceIn) -> dict:
    """Store the browser's PushSubscription for this device_id (idempotent upsert)."""
    with _state_lock:
        existing = _devices.get(body.device_id)
        _devices[body.device_id] = {
            "subscription": body.subscription,
            "created_ts": existing["created_ts"] if existing else _now_iso(),
        }
        _save_state()
    return {"status": "ok"}


@router.delete("/devices/{device_id}", summary="Forget a device's push subscription")
async def delete_device(device_id: str) -> dict:
    """Remove a device's subscription (e.g. the user turned notifications off). Journeys are kept."""
    with _state_lock:
        _devices.pop(device_id, None)
        _save_state()
    return {"status": "ok"}


def _journey_model(jid: str, rec: dict, by_situation: dict[str, list]) -> Journey:
    return Journey(
        journey_id=jid,
        from_stop=rec["from_stop"],
        from_name=rec.get("from_name"),
        to_stop=rec["to_stop"],
        to_name=rec.get("to_name"),
        begin=rec["begin"],
        end=rec["end"],
        days=rec["days"],
        created_ts=rec["created_ts"],
        alerts=match_journey(rec, by_situation),
    )


@router.get("/journeys", summary="A device's journeys with the disruptions currently hitting them")
async def list_journeys(
    device_id: str = Query(description="Client-generated device UUID."),
) -> JourneysResponse:
    """Every journey registered by this device, each annotated with the situations currently
    affecting it (the same matcher used for push, but with no dedup — this is the live picture)."""
    by_situation = _snapshot()
    with _state_lock:
        mine = [(jid, rec) for jid, rec in _journeys.items() if rec["device_id"] == device_id]
    journeys = [_journey_model(jid, rec, by_situation) for jid, rec in mine]
    journeys.sort(key=lambda j: j.created_ts)
    return JourneysResponse(journeys=journeys)


@router.post("/journeys", summary="Register a journey for alerts")
async def create_journey(body: JourneyIn) -> Journey:
    """Register a from→to journey with a recurring weekday+time window. Capped per device."""
    with _state_lock:
        mine = sum(1 for rec in _journeys.values() if rec["device_id"] == body.device_id)
        if mine >= _MAX_JOURNEYS_PER_DEVICE:
            raise HTTPException(
                status_code=409,
                detail=f"Maks {_MAX_JOURNEYS_PER_DEVICE} reiser per enhet.",
            )
        jid = uuid.uuid4().hex
        rec = {
            "device_id": body.device_id,
            "from_stop": body.from_stop,
            "from_name": body.from_name,
            "to_stop": body.to_stop,
            "to_name": body.to_name,
            "begin": body.begin,
            "end": body.end,
            "days": body.days,
            "created_ts": _now_iso(),
        }
        _journeys[jid] = rec
        _save_state()
    return _journey_model(jid, rec, _snapshot())


@router.delete("/journeys/{journey_id}", summary="Delete a registered journey")
async def delete_journey(
    journey_id: str,
    device_id: str = Query(description="The owning device; a journey is only deleted by its owner."),
) -> dict:
    """Delete a journey. 404 if it doesn't exist or isn't owned by this device."""
    with _state_lock:
        rec = _journeys.get(journey_id)
        if rec is None or rec["device_id"] != device_id:
            raise HTTPException(status_code=404, detail="Reise ikke funnet.")
        del _journeys[journey_id]
        _save_state()
    return {"status": "ok"}


@router.post("/test", summary="Send a test push to verify delivery")
async def test_push(body: TestIn) -> dict:
    """Send a "Varsler er aktivert" push to this device — proves the subscription works."""
    if not push_enabled():
        raise HTTPException(status_code=503, detail="Push er ikke konfigurert på tjeneren.")
    with _state_lock:
        dev = _devices.get(body.device_id)
        subscription = dev.get("subscription") if dev else None
    if subscription is None:
        raise HTTPException(status_code=404, detail="Enheten har ingen aktiv varsel-abonnement.")
    payload = {
        "title": "Varsler er aktivert",
        "body": "Du vil nå få beskjed når et avvik kan ramme en av dine reiser.",
        "url": "/varsel/",
    }
    result = _send_push(subscription, payload)
    if result == "gone":
        if _device_is_fresh(body.device_id):
            # A brand-new subscription can briefly 404/410 before it propagates — don't drop it.
            raise HTTPException(
                status_code=425,
                detail="Varselet er nettopp slått på. Vent et lite øyeblikk og prøv testvarsel igjen.",
            )
        _drop_device(body.device_id)
        _save_state()
        raise HTTPException(status_code=410, detail="Abonnementet er utløpt; slå på varsler på nytt.")
    if result == "error":
        raise HTTPException(status_code=502, detail="Kunne ikke sende testvarsel.")
    return {"status": "sent"}
