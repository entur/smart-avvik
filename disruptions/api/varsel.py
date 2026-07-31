"""Avviksvarsel — registered-journey push alerts on top of the per-call prediction cache.

A traveller registers a journey (from-stop → to-stop, a recurring daily time window and a
weekday selector) from the PWA at ``/varsel``. Whenever the poller refreshes the per-call
prediction cache (``_call_pred_by_situation`` in main.py), :func:`check_and_notify` matches every
registered journey against the live situations and sends a Web Push notification for any newly
alert-worthy match. Tapping it opens the app, which lists the journeys and the disruptions
currently hitting them (served by :func:`GET /varsel-api/journeys`).

The matching itself **is** ``/impact``: :func:`match_journey` turns a registered journey into
boarding windows and asks main's ``/impact`` provider about each one — passing ``catchable_after``
so the matcher itself excludes departures the traveller can no longer make — then applies the one
policy that is genuinely varsel's: the per-operator alert gate. It used to re-implement the matcher,
which had already drifted (the two severity orders disagreed); there is now one matcher and one
severity order behind both surfaces.

  Catchability has to be *inside* candidate selection. Filtering it afterwards, on the rolled-up
  worst-per-situation result, let a departed worst-case train suppress a later train on the same line
  that was both still catchable and alert-worthy — a missed alert, found in review 2026-07-29.

Design notes
------------
* **Weekdays** are ``0=Monday … 6=Sunday`` (Python ``date.weekday()``), consistent with the PWA.
* **Windows** are non-wrapping inclusive ``[begin, end]`` in Europe/Oslo local time; the PWA
  validates ``begin < end``, so overnight windows never reach the matcher (and are ignored if
  they somehow do). :func:`journey_windows` offers today's and tomorrow's window, because the
  prediction cache carries near-future departures and late in the evening the next relevant window
  is tomorrow's.
* **Work is shared per journey definition, delivery is not.** :func:`journey_key` collapses
  registrations that describe the same trip, so two devices watching the same train drive one
  ``/impact`` call and one written message — and see identical text (``_messages``). Push dedup
  (``_notified``) stays keyed on ``journey_id``: sharing it would let the first device notified
  suppress everyone else's push.
* **The push text is written by an LLM** (:mod:`llm_message`, Claude Sonnet 5 on Vertex with a
  Gemini fallback) from the enriched ``/impact`` payload — the situation's own words, what has been
  observed on the line, and any faster alternative. It phrases the alert; it never decides it. Every
  failure path falls back to :func:`_deterministic_body`, the previous template wording.
* **Push gate** — a match is *pushed* only when the worst matching train crosses the same
  production alert threshold used by ``/demo`` and eval (``P(cancelled OR ≥10 min late) ≥ 0.65``,
  per-operator cuts in :data:`_OPERATOR_ALERT_THRESHOLD`), so trivial flags don't spam the phone.
  The gate is literally ``/demo``'s ``would_alert`` (main._demo_snapshot): the calibrated impact
  probability and nothing else — no cancellation-probability fallback (measured 2026-07-27: in
  ``call_predictions_{,bus_}today`` ``p_impact_dep`` is NULL only where ``aimed_dep`` is NULL,
  i.e. at terminals the matcher already skips, so the old fallback could never fire on a boarding).
  The in-app list shows *all* current matches regardless, flagged with ``alertworthy``.
* **What the push says** — the *alerting* quantity, in ``/demo``'s wording: P(innstilling eller
  ≥ 10 min forsinkelse), then whichever driver dominates (likely cancellation, or the
  exceedance-stack delay interval). Never the raw ``dep_delay_typ_min`` regression: for today's
  alert-worthy boardings its median is 2 min against the stack p50's 15 min (rail), so quoting it
  made a 93 %-impact alert read as "1 % sjanse for innstilling, typisk 2 min forsinket".
* **State** (devices / journeys / notified) is tiny JSON, guarded by a re-entrant lock and written
  through to a local file and — when ``VARSEL_STATE_BUCKET`` is set — a GCS blob, so it survives a
  redeploy (the container fs is ephemeral). Single always-on instance ⇒ last-write-wins is
  safe.
* **VAPID** keys come from env (``VAPID_PRIVATE_KEY`` raw base64url, ``VAPID_PUBLIC_KEY``,
  ``VAPID_SUBJECT``). When unset, push is disabled gracefully: ``/config`` reports
  ``push_enabled=false`` and :func:`check_and_notify` is a no-op, so the app still renders the
  journey list.

No import of main.py (avoids a cycle): main injects its ``/impact`` provider and a stops provider via
:func:`configure`, and passes the live per-call snapshot to :func:`check_and_notify` (used only to
prune state for situations that have ended). Everything read off the impact response is duck-typed,
so the tests can drive this with a synthetic provider.
"""

import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from alternatives import AlternativeJourney
from fastapi import APIRouter, HTTPException, Query, Request
from impact import ImpactResponse, LatestObservation, severity_key
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

# Push a match only when the worst matching train's impact probability crosses this — the same
# operating threshold as /demo and eval/impact_eval.py (>=91 % precision at every ask horizon).
_ALERT_THRESHOLD = 0.65
# A journey counts as "trouble" when cancelled or >= this many minutes late — the event the impact
# probability predicts. Public because /demo scores its realized outcomes against the same bar
# (main._DEMO_TROUBLE_DELAY_MIN), as it takes its threshold from threshold_for(): one definition of
# "trouble" behind both surfaces.
TROUBLE_DELAY_MIN = 10
# Above this cancellation probability the wording leads with "mest sannsynlig innstilt" instead of
# the delay interval: when the train probably isn't running, a predicted delay is beside the point
# (bus alerts are mostly cancellation-driven, rail alerts mostly delay-driven).
_CANCEL_LIKELY = 0.5

# Per-operator alert threshold overrides (default _ALERT_THRESHOLD). The pooled bus family is
# calibrated per operator, but a single 0.65 cut under-/over-alerts several operators; these
# values align every operator at ~90 % pooled precision on the serving-matched holdout
# (eval/tune_alert_thresholds.py, tuned 2026-07-07 — re-run after retrains/platt refits).
# GOA/TRO/ATB run conservative → lower cut buys recall (GOA .81→.87, ATB .65→.79) at the bar;
# RUT/KOL/FIN/NOR/MOR sit below the bar at 0.65 → higher cut restores precision (MOR .80→.96).
# SOF/SKY stay on the default (already at/near the bar); OST and GCO have too little holdout
# evidence to override (OST: ~6 alerts at any >=90 % cut). NSB is the rail family (0.65
# validated at ~91 % in eval/impact_eval.py) and is deliberately absent.
_OPERATOR_ALERT_THRESHOLD: dict[str, float] = {
    "ATB": 0.55,
    "FIN": 0.75,
    "GOA": 0.50,
    "KOL": 0.75,
    "MOR": 0.90,
    "NOR": 0.80,
    "RUT": 0.75,
    "TRO": 0.55,
}


def threshold_for(operator: str | None) -> float:
    """The alerting threshold for an operator (None -> the default). Shared by the varsel
    matcher and the /demo verdicts so both surfaces apply the same operating point."""
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
# ...and the service as a whole holds at most this many of each. The per-device cap bounds nothing
# globally: `device_id` is client-generated and `upsert_device` accepts any value, so without these
# the poller's per-cycle work is bounded only by how many people install the PWA. Sized for an
# experiment with a pilot group; raise deliberately after measuring a cycle, not by discovering it.
_MAX_DEVICES = 500
_MAX_JOURNEYS = 2000
# Written messages per refresh cycle. The matcher runs on the poller thread, which also rebuilds
# every other cache, so an unbounded fan-out of LLM calls would stall ingestion. Journeys beyond the
# cap get the deterministic wording this tick and are picked up on the next one.
_MAX_LLM_CALLS_PER_CYCLE = 8
# Give-up point for writing one journey × situation's message. Failures are payload-dependent as
# often as they are transient (a refusal, an over-long or unparseable reply), so a write that failed
# twice will very likely keep failing for this exact payload — and retrying costs a slot from the
# per-cycle cap without being able to change what any user already saw.
_MAX_MESSAGE_ATTEMPTS = 3
# Wall-clock ceiling for the whole message-writing pass. The call cap alone does not bound time:
# one capped "call" can be two backend attempts at _TIMEOUT_S each, so the count implies half the
# worst case. Journeys past the budget take the deterministic wording and are re-tried next tick.
_MAX_LLM_SECONDS_PER_CYCLE = 30.0
# Web Push sends are independent, I/O-bound, and each can block for _PUSH_TIMEOUT_S against a
# client-supplied endpoint. Sequential delivery meant one sick push service stalled the whole poller
# -- and with it every cache rebuild -- for devices x 10 s. Fan them out instead.
_PUSH_WORKERS = 8
# How long past a window's end a train from that window can still be worth alerting about. A train
# is only ever dropped by its own predicted expected departure (`catchable_after`); this bound just
# stops journey_windows from discarding the window before that check runs. Sized above the p90 delays
# the exceedance stack produces (~90 min at the top of the curve).
_WINDOW_CATCH_UP = timedelta(minutes=90)
# Per-send wall clock for a Web Push delivery (see _send_push).
_PUSH_TIMEOUT_S = 10.0
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
class ImpactProvider(Protocol):
    """main's /impact code path, injected rather than called over HTTP.

    Injected because the app is deliberately a single always-on instance with an in-process
    prediction cache: a loopback request would add a round trip and a base-URL config for no
    isolation benefit. What matters is that there is ONE matcher and one severity order behind both
    surfaces.

    Spelled as a Protocol rather than `Callable[..., Any]` because this is the only cross-module
    contract in the design, and on 2026-07-29 it broke silently: an added keyword reached a provider
    that did not accept it, every matcher call raised TypeError, check_and_notify's per-journey guard
    swallowed it, and all push alerting stopped with a green test suite (both test doubles hand-write
    their own provider, so nothing checked the real one). `...` would have accepted that drift; this
    does not.
    """

    def __call__(
        self,
        *,
        from_stop: str,
        to_stop: str,
        window_start: datetime,
        window_end: datetime,
        catchable_after: datetime | None = ...,
        with_alternative: bool = ...,
        min_impact: float = ...,
    ) -> ImpactResponse: ...


_impact_provider: ImpactProvider | None = None
_stops_provider: Callable[[], list[dict]] | None = None


def configure(
    *,
    impact_provider: ImpactProvider,
    stops_provider: Callable[[], list[dict]],
) -> None:
    """Wire in main.py's /impact provider and stop list (called once at startup)."""
    global _impact_provider, _stops_provider
    _impact_provider = impact_provider
    _stops_provider = stops_provider


def _impact(
    journey: dict,
    window_start: datetime,
    window_end: datetime,
    catchable_after: datetime | None = None,
    with_alternative: bool = False,
) -> ImpactResponse | None:
    """One /impact call for a journey's boarding window, or None when unconfigured.

    `min_impact` is the *default* alert threshold rather than a per-operator one: the operator is a
    property of the matched train, which is only known after the call. It is the floor of the
    per-operator cuts (GOA 0.50), so no alert-worthy journey is ever denied an alternative — it just
    means a few sub-threshold-for-their-operator trips still pay the lookup.
    """
    if _impact_provider is None:
        return None
    return _impact_provider(
        from_stop=journey["from_stop"],
        to_stop=journey["to_stop"],
        window_start=window_start,
        window_end=window_end,
        catchable_after=catchable_after,
        with_alternative=with_alternative,
        min_impact=min(_OPERATOR_ALERT_THRESHOLD.values(), default=_ALERT_THRESHOLD)
        if with_alternative
        else 0.0,
    )


def _message_key(key: str, alert: "Alert") -> tuple[str, str, str]:
    """Cache key for a written message / alternative: (journey definition, situation, departure).

    A tuple, not an f-string: `journey_key` itself embeds four `|` separators, so the string form
    needed `rsplit` on one line and `split` on the next three lines apart, with nothing declaring
    which encoding a key used. Including the departure is what keeps the cached text tied to the
    train it describes (see _journey_model).
    """
    dep = alert.departure_time.isoformat() if alert.departure_time else ""
    return (key, alert.situation_number, dep)


# ---- State: devices, journeys, notified ------------------------------------------------------
# Re-entrant lock: mutations call _save_state while already holding it.
_state_lock = threading.RLock()
_devices: dict[str, dict] = {}  # device_id -> {"subscription": {...}, "created_ts": iso}
_journeys: dict[str, dict] = {}  # journey_id -> journey record (see POST /journeys)
_notified: set[str] = set()  # {"journey_id|situationNumber"} — dedup, pruned when a situation ends
# {"journey_key|situationNumber" -> written message}. Keyed on the journey *definition*, not the
# registration, so every device on the same trip gets the same text and one message is written per
# journey × situation rather than one per device. In-memory only: a redeploy re-writes messages
# rather than restoring stale advice from before the restart.
_messages: dict[tuple[str, str, str], str] = {}
# Same key, same lifecycle: the alternative the poller found for that journey x situation. Cached
# rather than recomputed because the request path deliberately does NOT call the Journey Planner
# (see match_journey) -- without this the field would be structurally null on the only endpoint that
# serves it, which is how it shipped until review on 2026-07-29.
_alternatives: dict[tuple[str, str, str], AlternativeJourney] = {}
# Same key, same lifecycle: how many times writing this message has failed, so a payload the LLM
# cannot handle is not retried on every tick for the situation's whole life.
_message_attempts: dict[tuple[str, str, str], int] = {}

_STATE_PATH = Path(os.environ.get("VARSEL_STATE_PATH", "/tmp/varsel_state.json"))
_STATE_BUCKET = os.environ.get("VARSEL_STATE_BUCKET")  # GCS bucket for durable state (optional)
_STATE_BLOB = os.environ.get("VARSEL_STATE_BLOB", "varsel_state.json")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


_gcs_client = None
_gcs_client_lock = threading.Lock()


def _gcs_blob():
    """The state blob handle when a bucket is configured, else None (import is lazy).

    The client is built once and reused. Constructing a fresh `storage.Client()` per call meant every
    device registration and every journey delete paid an ADC resolution plus a metadata-server token
    fetch before the upload — on the request path, under `_state_lock`.
    """
    global _gcs_client
    if not _STATE_BUCKET:
        return None
    from google.cloud import storage

    with _gcs_client_lock:
        if _gcs_client is None:
            _gcs_client = storage.Client()
    return _gcs_client.bucket(_STATE_BUCKET).blob(_STATE_BLOB)


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


def _typ_delay(pred: Any) -> int | None:
    """Typical delay off a StopPrediction, preferring the calibrated exceedance-stack p50 and
    falling back to the raw regression only when the stack has nothing (as main.py does)."""
    return pred.delay_p50_min if pred.delay_p50_min is not None else pred.delay_typ_min


def journey_key(journey: dict) -> str:
    """A canonical key for a journey *definition* — what makes two registrations the same trip.

    Two devices that register the same from→to, weekdays and time window get one /impact call and
    one written message between them, and therefore identical text. Deliberately excludes
    ``device_id`` and ``journey_id``: those identify a *registration*, and keying the shared work on
    them is what would duplicate it. (Delivery dedup still keys on ``journey_id`` — see
    :func:`check_and_notify`.)
    """
    return "|".join(
        [
            journey["from_stop"],
            journey["to_stop"],
            journey["begin"],
            journey["end"],
            ",".join(str(d) for d in sorted(journey["days"])),
        ]
    )


def journey_windows(journey: dict, now: datetime) -> list[tuple[datetime, datetime]]:
    """The journey's boarding windows that could still be caught, as UTC instants.

    A registered journey is a recurring Europe/Oslo ``[begin, end]`` window on selected weekdays;
    ``/impact`` matches an explicit window. Today and tomorrow are both offered because the
    prediction cache carries near-future departures, so late in the evening the next relevant window
    is tomorrow's. Windows whose end has already passed are dropped, and only the journey's own
    weekdays qualify (weekday of the *local* date, matching the PWA's 0=Monday).
    """
    days = set(journey["days"])
    begin_min = _parse_hhmm(journey["begin"])
    end_min = _parse_hhmm(journey["end"])
    local_now = now.astimezone(_OSLO)
    windows: list[tuple[datetime, datetime]] = []
    for offset in (0, 1):
        local_date = (local_now + timedelta(days=offset)).date()
        if local_date.weekday() not in days:
            continue
        midnight = datetime.combine(local_date, time(0, 0), tzinfo=_OSLO)
        start = midnight + timedelta(minutes=begin_min)
        end = midnight + timedelta(minutes=end_min)
        # Keep offering a window for a grace period after its end. Dropping it the moment `end`
        # passes discards exactly the case the alert exists for: a 07:52 train in an 07:00-08:00
        # window, predicted 45 min late, is still catchable at 08:02. `catchable_after` does the real
        # per-train filtering downstream, so this only has to be generous enough not to pre-empt it.
        if end + _WINDOW_CATCH_UP < local_now:
            continue  # even a heavily delayed train in this window has gone
        windows.append((start.astimezone(UTC), end.astimezone(UTC)))
    return windows


class Alert(BaseModel):
    """One situation currently hitting a registered journey — the worst matching train's numbers."""

    situation_number: str = Field(description="SIRI-SX situationNumber of the disruption.")
    summary: str | None = Field(description="Short Norwegian summary of the situation.")
    description: str | None = Field(
        default=None,
        description="The operator's fuller Norwegian explanation of the situation.",
    )
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
    delay_p50_min: int | None = Field(
        description="Median departure delay (min) if it runs, from the calibrated exceedance stack "
        "— the same figure as /demo's delay column, not the weak raw regression.",
    )
    delay_p90_min: int | None = Field(description="Bad-case (p90) departure delay (min).")
    ge10_probability: float | None = Field(
        default=None,
        description="Calibrated P(departure >= 10 min late | the train runs), 0-1.",
    )
    impact_probability: float | None = Field(
        default=None,
        description="Calibrated P(cancelled OR >= 10 min late) at boarding — the alerting score.",
    )
    alert_threshold: float = Field(
        description="The operator's alerting threshold applied to impact_probability here (as "
        "shown per card on /demo).",
    )
    alertworthy: bool = Field(
        description="Whether this match crosses the push threshold (a notification is/was sent).",
    )
    latest_observation: LatestObservation | None = Field(
        default=None,
        description="What has actually been observed on this line so far: {cancelled, at_stop, "
        "at_stop_name, delayed_min, observed_at}. Null when nothing has been observed yet. See "
        "/impact's LatestObservation for the full schema and its staleness bound.",
    )
    alternative_journey: AlternativeJourney | None = Field(
        default=None,
        description="A faster way from from_stop to to_stop than this train, when one exists. "
        "Found by the poller (the request path deliberately skips the Journey Planner lookup), so "
        "this is populated once a push has been produced for this journey × situation.",
    )
    message: str | None = Field(
        default=None,
        description="The written traveller message that was pushed for this journey × situation — "
        "identical for every device registered on the same journey. Null before a push has been "
        "produced, or when no message writer was available (the push then carries the "
        "deterministic wording instead).",
    )
    # Internal: the full /impact response this alert was derived from, handed to the message writer
    # so it sees the complete picture (every situation's text, every line's observation, the
    # alternative) rather than just the headline numbers. exclude=True keeps it out of the API
    # response — it is a working reference, not part of the contract.
    impact: ImpactResponse | None = Field(default=None, exclude=True, repr=False)


def match_journey(
    journey: dict, now: datetime | None = None, *, with_alternative: bool = False
) -> list[Alert]:
    """Situations currently hitting `journey`, worst-first — one :class:`Alert` per situation.

    The matching itself is ``/impact``'s: this asks the impact provider about each of the journey's
    boarding windows (``select="worst"`` — if any train in the window is alert-worthy the traveller
    needs to know) and keeps what is left of varsel's own policy on top of the result:

    * **not yet departed** — handed to the matcher as ``catchable_after=now`` rather than applied
      here, so a departed train is never a candidate in the first place (a situation stays open, and
      its scored calls stay in the cache, long after the departures it describes; without this a
      morning delay would keep alerting all day).
    * **the per-operator alert gate** — exactly ``/demo``'s ``would_alert``: the calibrated impact
      probability must exist and cross :func:`threshold_for`. No cancellation-probability fallback
      (see the module docstring).

    ``now`` is injectable for tests; it defaults to the current time.

    `with_alternative` is **off by default**: the Journey Planner lookup is a blocking external call,
    and a device with 20 registered journeys x 2 windows would fan out ~40 of them on a single
    request. The poller passes True (the push is where the advice matters); the request path leaves it
    off and the in-app list shows the cached written message, which already names the alternative.
    """
    if now is None:
        now = datetime.now(UTC)

    # Keep the worst alert per situation across every window (a journey can have a window today and
    # one tomorrow, and the same situation can reach into both).
    worst_by_situation: dict[str, Alert] = {}
    for window_start, window_end in journey_windows(journey, now):
        impact = _impact(
            journey, window_start, window_end, catchable_after=now,
            with_alternative=with_alternative,
        )
        if impact is None:
            continue
        observation_by_line = {
            line.line: line.latest_observation for line in (impact.lines or [])
        }
        for sit in impact.situations or []:
            dep = sit.departure_prediction
            threshold = threshold_for(sit.operator)
            impact_p = dep.impact_probability
            alert = Alert(
                situation_number=sit.situation_number,
                summary=sit.summary,
                description=sit.description,
                operator=sit.operator,
                mode=mode_label(sit.operator),
                line=sit.line,
                departure_time=dep.scheduled_time,
                cancel_probability=dep.cancel_probability,
                delay_p50_min=_typ_delay(dep),
                delay_p90_min=dep.delay_p90_min,
                ge10_probability=dep.ge10_probability,
                impact_probability=impact_p,
                alert_threshold=threshold,
                alertworthy=impact_p is not None and impact_p >= threshold,
                latest_observation=observation_by_line.get(sit.line),
                alternative_journey=impact.alternative_journey,
                impact=impact,
            )
            existing = worst_by_situation.get(alert.situation_number)
            if existing is None or _alert_severity(alert) > _alert_severity(existing):
                worst_by_situation[alert.situation_number] = alert

    alerts = sorted(worst_by_situation.values(), key=_alert_severity, reverse=True)
    return alerts


def _alert_severity(alert: Alert) -> tuple[float, int, float]:
    """The shared severity order, applied to an Alert.

    Delegates to `impact.severity_key` rather than restating it. Keeping a second copy in sync by
    comment is what let the two orders drift apart in the first place (main's was cancellation-first
    while this one was impact-first), and `Alert` carries the same three field names as
    `StopPrediction`, so the one function serves both.
    """
    return severity_key(alert)


# ---- Notify ----------------------------------------------------------------------------------
def _delay_phrase(alert: Alert) -> str | None:
    """"typisk 15–27 min forsinket" from the exceedance-stack interval (p50–p90, /demo's delay
    column); collapsed to one number when they coincide, None when neither is known."""
    p50, p90 = alert.delay_p50_min, alert.delay_p90_min
    if not p50 and not p90:
        return None
    if p50 and p90 and p90 > p50:
        return f"typisk {p50}–{p90} min forsinket"
    return f"typisk {p90 or p50} min forsinket"


def _deterministic_body(alert: Alert) -> str:
    """The template wording: what the push says when no written message is available.

    Leads with the number the push gate actually fired on — P(innstilling eller ≥ 10 min
    forsinkelse), worded as on ``/demo`` — then the dominant driver: a likely cancellation, else the
    exceedance-stack delay interval. Cancellation probability alone plus the raw typical-delay
    regression (what this said until 2026-07-27) understates a delay-driven alert badly.
    """
    # alertworthy alerts always carry an impact probability (the gate requires it); the fallback
    # only matters if this is ever called for a listed-but-not-pushed match.
    score = (
        alert.impact_probability if alert.impact_probability is not None else alert.cancel_probability
    )
    lead = (
        f"{round(score * 100)} % sjanse for innstilling eller "
        f"≥ {TROUBLE_DELAY_MIN} min forsinkelse"
    )
    detail = (
        "mest sannsynlig innstilt"
        if alert.cancel_probability >= _CANCEL_LIKELY
        else _delay_phrase(alert)
    )
    if detail:
        lead = f"{lead} — {detail}"
    if alert.departure_time is not None:
        hhmm = alert.departure_time.astimezone(_OSLO).strftime("%H:%M")
        lead = f"{alert.mode} kl {hhmm}: {lead}"
    return f"{alert.summary}\n{lead}" if alert.summary else lead


def _notification_payload(journey: dict, alert: Alert, message: str | None = None) -> dict:
    """Norwegian Web Push payload for one journey×situation match.

    `message` is the LLM-written text when one was produced; the deterministic template is used
    otherwise, so a missing or failing message writer costs wording, never the notification. The
    title, url and situation_number are unchanged either way — the notification stays recognisable
    and the service worker's click-through keeps working.
    """
    from_name = journey.get("from_name") or journey["from_stop"]
    to_name = journey.get("to_name") or journey["to_stop"]
    return {
        "title": f"Avvik: {from_name} → {to_name}",
        "body": message or _deterministic_body(alert),
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
            # pywebpush passes this to requests; without it the default is None -- block forever. The
            # endpoint URL comes from the client, so an unresponsive host would hang the poller
            # thread and stop ingestion for every other feature (review 2026-07-29).
            timeout=_PUSH_TIMEOUT_S,
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


def _log_push(
    kind: str,
    result: str,
    device_id: str,
    payload: dict,
    *,
    journey_id: str | None = None,
    alert: Alert | None = None,
) -> None:
    """One structured log line per push attempt, message text included — the audit trail of what
    was actually sent to whom (``jsonPayload.event = "push"`` in Cloud Logging)."""
    fields = {
        "event": "push",
        "push_kind": kind,  # "alert" (journey×situation match) or "test"
        "result": result,  # "sent", "gone" or "error"
        "device_id": device_id,
        "journey_id": journey_id,
        "title": payload.get("title"),
        "body": payload.get("body"),
    }
    if alert is not None:
        fields.update(
            situation_number=alert.situation_number,
            operator=alert.operator,
            line=alert.line,
            departure_time=alert.departure_time,
            cancel_probability=alert.cancel_probability,
            ge10_probability=alert.ge10_probability,
            delay_p50_min=alert.delay_p50_min,
            delay_p90_min=alert.delay_p90_min,
            impact_probability=alert.impact_probability,
            alert_threshold=alert.alert_threshold,
        )
    logger.info(
        "Push %s (%s) to device %s: %s | %s",
        result,
        kind,
        device_id,
        payload.get("title"),
        str(payload.get("body", "")).replace("\n", " / "),
        extra={"fields": fields},
    )


def _write_message(journey: dict, alert: Alert, *, journey_key: str) -> str | None:
    """The LLM-written body for this journey × situation, or None to use the template wording.

    Wraps :mod:`llm_message` so that nothing it can do — an unconfigured project, an unavailable
    model, a timeout, an unexpected exception — reaches the notification path. The import is local
    so a deploy without the Vertex dependencies still serves pushes.
    """
    try:
        import llm_message

        if not llm_message.llm_enabled():
            return None
        payload = llm_message.build_payload(journey, alert.impact, alert)
        return llm_message.write_message(
            payload, journey_key=journey_key, situation_number=alert.situation_number
        )
    except Exception:
        logger.warning(
            "Written-message step failed; falling back to the deterministic wording",
            exc_info=True,
            extra={"fields": {"event": "llm_message", "result": "error"}},
        )
        return None


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
    return datetime.now(UTC) - created < _SUBSCRIPTION_GRACE


def check_and_notify(by_situation: dict[str, list]) -> None:
    """Send a push for every registered journey × newly alert-worthy situation.

    Called at the end of main's ingestion-change rebuild with the freshly-refreshed per-call
    snapshot (used here only to prune ``_notified``; the matching itself goes through ``/impact``).

    Two loops, and the split between them is load-bearing:

    1. **Compute, keyed by journey definition.** Journeys are grouped by :func:`journey_key`, so two
       devices registered on the same trip drive one ``/impact`` call and one written message
       between them — and therefore receive identical text.
    2. **Deliver, keyed by registration.** ``_notified`` stays keyed on ``journey_id`` so each
       device is notified once per situation. Collapsing dedup onto the journey key would mean the
       first device notified silently suppresses the push for everyone else on the same trip.

    Written messages are capped per cycle (:data:`_MAX_LLM_CALLS_PER_CYCLE`); journeys beyond the cap
    get the deterministic wording this tick rather than delaying the poller thread. No-op when push
    is disabled.
    """
    if not push_enabled():
        return
    with _state_lock:
        journeys = list(_journeys.items())
        # Prune notified entries and cached messages whose situation is no longer live. In-place
        # updates (not `-=`) so the module globals stay globals rather than becoming function-locals.
        live = set(by_situation.keys())
        stale = {k for k in _notified if k.split("|", 1)[1] not in live}
        _notified.difference_update(stale)
        for key in [k for k in _messages if k[1] not in live]:
            _messages.pop(key, None)
        for key in [k for k in _alternatives if k[1] not in live]:
            _alternatives.pop(key, None)
        for key in [k for k in _message_attempts if k[1] not in live]:
            _message_attempts.pop(key, None)
        changed = bool(stale)

    # ---- Loop 1: one impact call and one written message per distinct journey definition --------
    by_journey_key: dict[str, dict] = {}
    for _jid, journey in journeys:
        by_journey_key.setdefault(journey_key(journey), journey)

    alerts_by_key: dict[str, list[Alert]] = {}
    llm_calls = 0
    # Wall-clock budget, not just a call count. _MAX_LLM_CALLS_PER_CYCLE caps journeys, but one
    # "call" can be two backend attempts (a refusal or an unparseable reply falls through to the
    # other backend), so 8 journeys x 2 x _TIMEOUT_S is ~128 s rather than the 64 s the count
    # suggests. This is the ingestion thread: every second here is a second /disruptions, /journey,
    # /demo and /reisevalg serve staler data, and loop 2's pushes wait for loop 1 to finish.
    llm_deadline = monotonic() + _MAX_LLM_SECONDS_PER_CYCLE
    for key, journey in by_journey_key.items():
        try:
            alerts = match_journey(journey, with_alternative=True)
        except Exception:
            # One journey's failure must not stop the remaining journeys being scored or any push
            # going out. Empty alerts for this key; the next tick retries.
            logger.warning(
                "Scoring failed for journey %s; skipping it this cycle",
                key,
                exc_info=True,
                extra={"fields": {"event": "varsel_match_error"}},
            )
            alerts_by_key[key] = []
            continue
        alerts_by_key[key] = alerts
        worst = next((a for a in alerts if a.alertworthy), None)
        if worst is None:
            continue
        message_key = _message_key(key, worst)
        # Record the alternative even if no message gets written (cap reached, LLM disabled): the
        # in-app list wants it either way.
        with _state_lock:
            if worst.alternative_journey is not None:
                _alternatives[message_key] = worst.alternative_journey
            already_written = message_key in _messages
            attempts = _message_attempts.get(message_key, 0)
        if already_written:
            continue
        if attempts >= _MAX_MESSAGE_ATTEMPTS:
            # Without this the retry never stops. A write that fails for a payload-dependent reason
            # (a refusal, an over-long reply, an unparseable one) fails again on every 60 s tick for
            # the situation's whole life -- ~720 Vertex calls that can change nothing, because the
            # push already went out with the deterministic wording, each one burning a slot from
            # _MAX_LLM_CALLS_PER_CYCLE that a journey which WOULD have succeeded now cannot use.
            continue
        if llm_calls >= _MAX_LLM_CALLS_PER_CYCLE or monotonic() >= llm_deadline:
            logger.info(
                "Written-message budget spent this cycle (%d calls / %.0fs); %s gets the "
                "deterministic wording",
                llm_calls,
                _MAX_LLM_SECONDS_PER_CYCLE,
                key,
                extra={"fields": {"event": "llm_message", "result": "capped"}},
            )
            continue
        llm_calls += 1
        # _write_message catches Exception itself, so this is belt and braces for a future edit that
        # lets one escape. It records an attempt rather than `continue`-ing past the counter, so a
        # raiser is subject to the same give-up rule as a None. Delivery is unaffected either way:
        # loop 2 pushes independently and falls back to the deterministic wording.
        try:
            message = _write_message(journey, worst, journey_key=key)
        except Exception:
            logger.warning(
                "Written-message step raised for %s; using the deterministic wording",
                key,
                exc_info=True,
                extra={"fields": {"event": "llm_message", "result": "error"}},
            )
            message = None
        with _state_lock:
            if message:
                _messages[message_key] = message
                _message_attempts.pop(message_key, None)
            else:
                _message_attempts[message_key] = attempts + 1

    # ---- Loop 2: deliver per registration, deduped per device ---------------------------------
    # Collect first, then fan out. One broad situation produces one send per registration in a single
    # cycle, and each send can block for _PUSH_TIMEOUT_S against a client-supplied endpoint (FCM,
    # Mozilla, Apple). Sequentially that is devices x 10 s of a stalled poller in the bad case --
    # minutes during which no cache is rebuilt and nobody else is alerted.
    # Parallel ACROSS devices, sequential WITHIN one: a "gone" result must still stop that device's
    # remaining alerts for the tick, which only holds if its own sends stay ordered.
    per_device: dict[str, list[tuple[str, dict, dict, Alert, str]]] = {}
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
        key = journey_key(journey)
        for alert in alerts_by_key.get(key, []):
            if not alert.alertworthy:
                continue
            with _state_lock:
                if f"{jid}|{alert.situation_number}" in _notified:
                    continue
            per_device.setdefault(journey["device_id"], []).append(
                (jid, device, journey, alert, key)
            )

    def _deliver_device(items: list[tuple[str, dict, dict, Alert, str]]) -> list[tuple]:
        """One device's pending alerts, in order, stopping at a dead subscription."""
        out = []
        for jid, device, journey, alert, key in items:
            with _state_lock:
                message = _messages.get(_message_key(key, alert))
            payload = _notification_payload(journey, alert, message)
            result = _send_push(device, payload)
            out.append((jid, journey, alert, result, payload))
            if result == "gone":
                break  # stop this device's remaining alerts this tick
        return out

    if per_device:
        with ThreadPoolExecutor(max_workers=min(_PUSH_WORKERS, len(per_device))) as pool:
            batches = list(pool.map(_deliver_device, per_device.values()))
        for jid, journey, alert, result, payload in [r for batch in batches for r in batch]:
            _log_push("alert", result, journey["device_id"], payload, journey_id=jid, alert=alert)
            if result == "sent":
                with _state_lock:
                    _notified.add(f"{jid}|{alert.situation_number}")
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
    """A stop offered in the from/to pickers."""

    stop_place: str = Field(description="NSR StopPlace ref.")
    stop_name: str | None = Field(description="Human-readable stop name.")
    mode: str = Field(description='Families serving the stop today: "Tog", "Buss" or "Tog/Buss".')
    n_calls: int = Field(description="Scheduled calls at the stop today; pickers rank matches by it.")


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


class ClientLogIn(BaseModel):
    """A browser-side error the PWA forwards into the server log (see api.ts reportClientError)."""

    context: str = Field(min_length=1, max_length=80, description='Where it happened: "fetch", "http", "window", …')
    message: str = Field(min_length=1, max_length=1000)
    device_id: str | None = Field(default=None, max_length=100)
    stack: str | None = Field(default=None, max_length=4000)
    url: str | None = Field(default=None, max_length=300, description="Client page path.")


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


@router.get("/stops", summary="Stops for the journey pickers")
async def stops() -> list[StopItem]:
    """All stops in today's planned timetable (rail and bus), for the from/to pickers."""
    rows = _stops_provider() if _stops_provider is not None else []
    return [
        StopItem(
            stop_place=r["stop_place"],
            stop_name=r.get("stop_name"),
            mode=r.get("mode", "Tog"),
            n_calls=r.get("n_calls", 0),
        )
        for r in rows
    ]


@router.post("/devices", summary="Register or refresh a device's push subscription")
def upsert_device(body: DeviceIn) -> dict:
    """Store the browser's PushSubscription for this device_id (idempotent upsert).

    Plain `def`, not `async def`: `_save_state` constructs a GCS client and uploads the blob, which
    is blocking network I/O. On `async def` that runs on the single-worker event loop and freezes
    every other route, `/health` included. Same rule as `list_journeys`/`create_journey`/`/impact`.
    """
    with _state_lock:
        existing = _devices.get(body.device_id)
        if existing is None and len(_devices) >= _MAX_DEVICES:
            logger.warning(
                "Global device cap (%d) reached; refusing a new registration",
                _MAX_DEVICES,
                extra={"fields": {"event": "varsel_capacity", "kind": "devices"}},
            )
            raise HTTPException(status_code=503, detail="Tjenesten er full. Prøv igjen senere.")
        _devices[body.device_id] = {
            "subscription": body.subscription,
            "created_ts": existing["created_ts"] if existing else _now_iso(),
        }
        _save_state()
    return {"status": "ok"}


@router.delete("/devices/{device_id}", summary="Forget a device's push subscription")
def delete_device(device_id: str) -> dict:
    """Remove a device's subscription (e.g. the user turned notifications off). Journeys are kept.

    Plain `def`: `_save_state` blocks on GCS. See `upsert_device`.
    """
    with _state_lock:
        _devices.pop(device_id, None)
        _save_state()
    return {"status": "ok"}


def _journey_model(jid: str, rec: dict, *, alerts: list[Alert] | None = None) -> Journey:
    """A journey plus its live alerts, each annotated with the message that was pushed for it.

    The message is read from :data:`_messages` rather than written here: the list endpoint is a
    request path, and writing one per request would mean an LLM call per page view. Reading the
    cached text also means the in-app list says exactly what the push said.

    `alerts` lets the caller supply an already-scored result, so a device with several registrations
    of the same journey definition runs the matcher once rather than once per registration.
    """
    key = journey_key(rec)
    alerts = match_journey(rec) if alerts is None else alerts
    with _state_lock:
        now = datetime.now(UTC)
        for alert in alerts:
            # The cache key carries the DEPARTURE, so text only ever appears above the train it was
            # written about. Both caches live as long as the situation (up to its 12 h window) while
            # the alert underneath is re-scored every tick and `catchable_after` walks departed
            # trains out of the result — so keyed on (journey, situation) alone, a message written
            # at 06:05 about the 07:52 train still headed the card at 08:40, above a freshly
            # computed line reading 40 % for the 09:12 one.
            cache_key = _message_key(key, alert)
            alert.message = _messages.get(cache_key)
            alt = _alternatives.get(cache_key)
            # An alternative that has itself departed is worse than none: it advises catching a
            # train that has already left.
            if alt is not None and (alt.departure_time is None or alt.departure_time >= now):
                alert.alternative_journey = alt
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
        alerts=alerts,
    )


@router.get("/journeys", summary="A device's journeys with the disruptions currently hitting them")
def list_journeys(
    device_id: str = Query(description="Client-generated device UUID."),
) -> JourneysResponse:
    """Every journey registered by this device, each annotated with the situations currently
    affecting it (the same matcher used for push, but with no dedup — this is the live picture).

    Plain ``def`` (NOT ``async def``), as in ``reisevalg.recommendation``: the impact provider can
    make a blocking Journey Planner call, which must run in the anyio threadpool rather than on the
    single-worker event loop. The alternative lookup is additionally skipped on this request path —
    see :func:`match_journey`."""
    with _state_lock:
        mine = [(jid, rec) for jid, rec in _journeys.items() if rec["device_id"] == device_id]
    # Score each distinct journey DEFINITION once, exactly as check_and_notify's loop 1 does. A
    # device at the 20-journey cap with overlapping registrations would otherwise run the full
    # matcher up to 20 times per page view, and the PWA polls this every 60 s.
    by_key: dict[str, list[Alert]] = {}
    journeys = []
    for jid, rec in mine:
        key = journey_key(rec)
        if key not in by_key:
            by_key[key] = match_journey(rec)
        journeys.append(_journey_model(jid, rec, alerts=by_key[key]))
    journeys.sort(key=lambda j: j.created_ts)
    return JourneysResponse(journeys=journeys)


@router.post("/journeys", summary="Register a journey for alerts")
def create_journey(body: JourneyIn) -> Journey:
    """Register a from→to journey with a recurring weekday+time window. Capped per device.

    Plain ``def`` for the same reason as :func:`list_journeys`."""
    with _state_lock:
        mine = sum(1 for rec in _journeys.values() if rec["device_id"] == body.device_id)
        if mine >= _MAX_JOURNEYS_PER_DEVICE:
            raise HTTPException(
                status_code=409,
                detail=f"Maks {_MAX_JOURNEYS_PER_DEVICE} reiser per enhet.",
            )
        if len(_journeys) >= _MAX_JOURNEYS:
            logger.warning(
                "Global journey cap (%d) reached; refusing a new registration",
                _MAX_JOURNEYS,
                extra={"fields": {"event": "varsel_capacity", "kind": "journeys"}},
            )
            raise HTTPException(status_code=503, detail="Tjenesten er full. Prøv igjen senere.")
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
    try:
        return _journey_model(jid, rec)
    except Exception:
        # The registration is already committed, so a failure rendering the response would otherwise
        # burn one of the 20 per-device slots on every retry while the UI shows nothing (review
        # 2026-07-29). Roll it back and let the client retry cleanly.
        with _state_lock:
            _journeys.pop(jid, None)
            _save_state()
        logger.exception("Could not render the new journey; rolled the registration back")
        raise HTTPException(
            status_code=503, detail="Kunne ikke opprette reisen akkurat nå. Prøv igjen."
        ) from None


@router.delete("/journeys/{journey_id}", summary="Delete a registered journey")
def delete_journey(
    journey_id: str,
    device_id: str = Query(description="The owning device; a journey is only deleted by its owner."),
) -> dict:
    """Delete a journey. 404 if it doesn't exist or isn't owned by this device.

    Plain `def`: `_save_state` blocks on GCS. See `upsert_device`.
    """
    with _state_lock:
        rec = _journeys.get(journey_id)
        if rec is None or rec["device_id"] != device_id:
            raise HTTPException(status_code=404, detail="Reise ikke funnet.")
        del _journeys[journey_id]
        _save_state()
    return {"status": "ok"}


# Open, unauthenticated endpoint — a global per-minute cap keeps a misbehaving (or malicious)
# client from flooding the server log. Beyond the cap reports are dropped, not errored, so the
# client never retries harder because of the throttle itself.
_CLIENT_LOG_MAX_PER_MIN = 60
_client_log_lock = threading.Lock()
_client_log_window_start: datetime | None = None
_client_log_count = 0


@router.post("/client-log", summary="Forward a client-side error into the server log")
async def client_log(body: ClientLogIn, request: Request) -> dict:
    """Log a browser-side error (failed fetch, HTTP error, uncaught exception) server-side, so
    client failures are visible next to the server logs in Cloud Logging
    (``jsonPayload.event = "client_error"``)."""
    global _client_log_window_start, _client_log_count
    now = datetime.now(UTC)
    with _client_log_lock:
        if _client_log_window_start is None or now - _client_log_window_start >= timedelta(minutes=1):
            _client_log_window_start, _client_log_count = now, 0
        _client_log_count += 1
        if _client_log_count > _CLIENT_LOG_MAX_PER_MIN:
            return {"status": "throttled"}
    logger.warning(
        "Client error [%s] %s",
        body.context,
        body.message,
        extra={
            "fields": {
                "event": "client_error",
                "client_context": body.context,
                "device_id": body.device_id,
                "client_url": body.url,
                "stack": body.stack,
                "user_agent": request.headers.get("user-agent"),
            }
        },
    )
    return {"status": "ok"}


@router.post("/test", summary="Send a test push to verify delivery")
def test_push(body: TestIn) -> dict:
    """Send a "Varsler er aktivert" push to this device — proves the subscription works.

    Plain `def`: `_send_push` blocks for up to `_PUSH_TIMEOUT_S` (10 s) against a client-supplied
    push endpoint. On `async def` a slow endpoint freezes the whole event loop for that long, which
    is enough to fail a liveness probe. See `upsert_device`.
    """
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
    _log_push("test", result, body.device_id, payload)
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
