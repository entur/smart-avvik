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
* **One message input per decision, built once** (:func:`_message_input`, from
  ``llm_message.build_message_input``). The model is prompted with it, :func:`_deterministic_body`
  words the fallback from it, and the decision log records it — so the row says what the writer was
  actually shown, and a figure added for one of the three is available to all of them. It is built
  for *every* matched situation, not just the alert-worthy ones, because a decision with no
  recorded input cannot be re-examined and those rows are the majority.
* **Push gate** — a match is *pushed* only when the worst matching train crosses the same
  production alert threshold used by ``/demo`` and eval (``P(cancelled OR ≥10 min late) ≥ 0.65``,
  per-operator cuts in :mod:`alert_policy`), so trivial flags don't spam the phone.
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
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from zoneinfo import ZoneInfo

# The alert gate moved out to its own module on 2026-08-06 so `impact` can reach it too (importing
# it back from here would be a cycle — see alert_policy's docstring). Bound at module level rather
# than reached through `alert_policy.`, so `varsel.threshold_for` / `varsel.TROUBLE_DELAY_MIN` keep
# resolving for the callers that already read them off this module. TROUBLE_DELAY_MIN is such a
# re-export and nothing else: it is /demo's realized-outcome bar, not something varsel reads.
from alert_policy import TROUBLE_DELAY_MIN, min_threshold, threshold_for  # noqa: F401
from alternatives import AlternativeJourney
from deployment import environment_label
from fastapi import APIRouter, HTTPException, Query, Request
from impact import ImpactResponse, LatestObservation, severity_key
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

# Above this cancellation probability the wording leads with "mest sannsynlig innstilt" instead of
# the delay interval: when the train probably isn't running, a predicted delay is beside the point
# (bus alerts are mostly cancellation-driven, rail alerts mostly delay-driven).
#
# The bar itself now lives in `llm_message._CANCEL_LIKELY`, which decides whether the message input
# carries `innstilling_prosent` at all -- and that key's presence is what _deterministic_body reads.
# Restated here, not imported, because the only caller left is _message_input_from_alert, whose whole
# job is to produce a usable input when llm_message is the thing that failed.
_CANCEL_LIKELY = 0.5

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


class ConnectivityProvider(Protocol):
    """main's "does a planned trip run between these stops today" lookup.

    A Protocol for the same reason as :class:`ImpactProvider`: it is a cross-module contract, and the
    consequence of drift is quiet. A TypeError here would be caught by the registration path's own
    guard and every journey would register unchecked, which looks exactly like a healthy deploy.

    Returns (from→to runs, to→from runs), or None when the question could not be answered — an
    outage, a timeout, the rate limit. None is not "no service".
    """

    def __call__(
        self, from_refs: Sequence[str], to_refs: Sequence[str]
    ) -> tuple[bool, bool] | None: ...


_impact_provider: ImpactProvider | None = None
_stops_provider: Callable[[], list[dict]] | None = None
_connectivity_provider: ConnectivityProvider | None = None
# Collapsed stop ref -> the primary ref for the same physical place (see set_stop_aliases). Empty
# until the first stop-list rebuild, and stays empty if the stop registry cannot be read — in which
# case every ref is its own place, exactly as before this map existed.
_stop_aliases: dict[str, str] = {}


def configure(
    *,
    impact_provider: ImpactProvider,
    stops_provider: Callable[[], list[dict]],
    connectivity_provider: ConnectivityProvider | None = None,
) -> None:
    """Wire in main.py's /impact provider, stop list and planned-service check (once, at startup).

    `connectivity_provider` is optional: without it, registrations are simply not checked, which is
    what every deploy did before the check existed.
    """
    global _impact_provider, _stops_provider, _connectivity_provider
    _impact_provider = impact_provider
    _stops_provider = stops_provider
    _connectivity_provider = connectivity_provider


def set_stop_aliases(aliases: dict[str, str]) -> None:
    """Publish "this ref is really that ref" for co-located stops, and heal what is stored.

    Called by main.refresh_varsel_stops on every stop-list rebuild with the refs it collapsed onto a
    primary. Rewriting what is stored is about identity, not about matching (:func:`place_refs` is
    what makes every ref of a place visible to the matcher): two devices that picked different refs
    for the same place must land on ONE `journey_key`, or the same physical trip earns two /impact
    calls, two written messages and two rows in the decision log.

    Replaced wholesale (never mutated in place) so readers on the request path need no lock.
    """
    global _stop_aliases
    _stop_aliases = dict(aliases)
    if aliases:
        _canonicalize_journeys()


def canonical_stop(ref: str) -> str:
    """The primary ref for `ref`'s physical place — `ref` itself unless it was collapsed."""
    return _stop_aliases.get(ref, ref)


def _canonicalize_journeys() -> None:
    """Rewrite stored registrations onto canonical stop refs.

    Runs on each stop-list rebuild because the alias map can only be built after the registry is
    read, which is long after `load_state`. Idempotent: a canonical journey is left untouched, so
    this settles after one pass and does not rewrite state on every refresh.
    """
    changed: list[tuple[str, str, str]] = []
    with _state_lock:
        for jid, rec in _journeys.items():
            new_from = canonical_stop(rec["from_stop"])
            new_to = canonical_stop(rec["to_stop"])
            if (new_from, new_to) == (rec["from_stop"], rec["to_stop"]):
                continue
            if new_from == new_to:
                # Both ends collapse to one place: the journey never had a trip to match and
                # rewriting it would store a from == to record the API itself rejects. Left as it
                # was, and said out loud, because silently keeping it is how the original bug hid.
                logger.warning(
                    "Journey %s has both ends on one physical place (%s / %s -> %s); leaving it "
                    "un-canonicalized, it cannot match a trip",
                    jid,
                    rec["from_stop"],
                    rec["to_stop"],
                    new_from,
                    extra={"fields": {"event": "varsel_stop_alias", "result": "degenerate"}},
                )
                continue
            changed.append((jid, rec["from_stop"], rec["to_stop"]))
            rec["from_stop"] = new_from
            rec["to_stop"] = new_to
        if changed:
            _save_state()
    for jid, old_from, old_to in changed:
        logger.info(
            "Rewrote journey %s onto canonical stops (%s -> %s, %s -> %s)",
            jid,
            old_from,
            canonical_stop(old_from),
            old_to,
            canonical_stop(old_to),
            extra={"fields": {"event": "varsel_stop_alias", "result": "rewritten"}},
        )


# Shown on the card of a journey no planned trip connects today. Deliberately a warning and never a
# rejection, in both shapes below: a route that runs only at weekends, or seasonally, is legitimately
# absent from today's timetable, and even "served only the other way" can be a pair whose return leg
# runs on different days. Refusing would make the app wrong about journeys the traveller knows exist,
# and an unregisterable journey is worse than a registered one carrying a caveat. What both texts do
# say is the consequence, so "no alerts ever" is never mistaken for "no disruptions".
_NO_SERVICE_WARNING = (
    "Vi fant ingen direkteavganger mellom disse stoppene i dag. Sjekk at du valgte riktige "
    "stoppesteder — varsler krever en direkteavgang."
)


def _service_warning(rec: dict) -> str | None:
    """The caveat to show for this registration, or None when there is nothing to say."""
    if not rec.get("no_direct_service"):
        return None
    if rec.get("service_reverse_only"):
        # The stops ARE connected, just not this way round — near-proof of a swap, and worth saying
        # precisely rather than hiding inside the generic wording.
        return (
            f"Vi fant ingen direkteavganger fra {rec.get('from_name') or rec['from_stop']} til "
            f"{rec.get('to_name') or rec['to_stop']} i dag, men det går avganger motsatt vei. "
            "Har fra og til byttet plass?"
        )
    return _NO_SERVICE_WARNING
# How long a "no service" verdict stands before the poller asks again. Hours, not days: a route can
# start running later the same day (a weekend-only ferry registered on a Friday evening), and until
# the verdict is refreshed the card carries a warning that has stopped being true.
_SERVICE_RECHECK = timedelta(hours=6)


def _service_check_due(rec: dict, now: datetime) -> bool:
    """Whether this journey's "no service" verdict is old enough to re-ask.

    Wall-clock, not `monotonic()`: the stamp is written into the persisted state file, and a monotonic
    reading means nothing after a restart — it would read as far in the future and suppress the
    re-check for the life of the registration. An absent or unparseable stamp is due now.
    """
    try:
        return now - datetime.fromisoformat(rec["service_checked_ts"]) >= _SERVICE_RECHECK
    except (KeyError, TypeError, ValueError):
        return True


def _service_verdict(from_stop: str, to_stop: str) -> tuple[bool, bool] | None:
    """Ask the injected provider about every ref of both places. None when unavailable."""
    if _connectivity_provider is None:
        return None
    try:
        return _connectivity_provider(place_refs(from_stop), place_refs(to_stop))
    except Exception:
        # Same rule as the provider's own failure path: an unanswerable question never blocks a
        # registration, and never becomes a warning either.
        logger.warning(
            "Planned-service check raised for %s -> %s; registering without it",
            from_stop,
            to_stop,
            exc_info=True,
            extra={"fields": {"event": "varsel_service_check", "result": "error"}},
        )
        return None


def recheck_service() -> None:
    """Re-ask about journeys flagged as having no direct service, and clear the ones that now do.

    Called from the poller's refresh cycle. Bounded by design: only flagged journeys are re-asked, and
    each at most once per :data:`_SERVICE_RECHECK_S`, so this costs nothing on a healthy list.
    """
    now = datetime.now(UTC)
    with _state_lock:
        due = [
            (jid, rec["from_stop"], rec["to_stop"])
            for jid, rec in _journeys.items()
            if rec.get("no_direct_service") and _service_check_due(rec, now)
        ]
    if not due:
        return
    cleared = 0
    for jid, from_stop, to_stop in due:
        verdict = _service_verdict(from_stop, to_stop)
        if verdict is None:
            continue
        with _state_lock:
            rec = _journeys.get(jid)
            if rec is None:  # deleted while we were asking
                continue
            rec["service_checked_ts"] = _now_iso()
            if verdict[0]:
                rec.pop("no_direct_service", None)
                rec.pop("service_reverse_only", None)
                cleared += 1
            else:
                rec["service_reverse_only"] = verdict[1]
    if cleared:
        with _state_lock:
            _save_state()
        logger.info(
            "Planned service appeared for %d journey(s); warning cleared",
            cleared,
            extra={"fields": {"event": "varsel_service_check", "result": "cleared"}},
        )


def place_refs(ref: str) -> tuple[str, ...]:
    """Every stop ref standing for `ref`'s physical place — the canonical one first.

    The picker offers one row per place, but the call sources still key each call on the ref its own
    feed used: rail calls at Lysaker are recorded on the station ref and bus calls on the bus-side
    ref. Matching only the canonical ref would therefore hide one side's traffic behind a merged row
    — the same silent miss the merge exists to remove, moved one layer down.
    """
    primary = canonical_stop(ref)
    # Filtered against the PRIMARY, not against `ref`: filtering against `ref` dropped the caller's
    # own ref whenever it was one of the collapsed ones (a registration made before the rewrite ran,
    # or one the rewrite deliberately left alone), asking about every ref of the place except the one
    # the traveller actually picked. The alias map never holds the primary as a key, so nothing here
    # can duplicate it.
    return (primary, *sorted(a for a, p in _stop_aliases.items() if p == primary))


def _impact(
    journey: dict,
    window_start: datetime,
    window_end: datetime,
    catchable_after: datetime | None = None,
    with_alternative: bool = False,
    from_stop: str | None = None,
    to_stop: str | None = None,
) -> ImpactResponse | None:
    """One /impact call for a journey's boarding window, or None when unconfigured.

    `from_stop`/`to_stop` override the journey's own refs so :func:`match_journey` can ask about the
    other refs of the same physical place (see :func:`place_refs`).

    `min_impact` is :func:`alert_policy.min_threshold` rather than a per-operator cut: the operator
    is a property of the matched train, which is only known after the call. It is the floor of the
    per-operator cuts (GOA 0.50), so no alert-worthy journey is ever denied an alternative — it just
    means a few sub-threshold-for-their-operator trips still pay the lookup.
    """
    if _impact_provider is None:
        return None
    return _impact_provider(
        from_stop=from_stop or journey["from_stop"],
        to_stop=to_stop or journey["to_stop"],
        window_start=window_start,
        window_end=window_end,
        catchable_after=catchable_after,
        with_alternative=with_alternative,
        min_impact=min_threshold() if with_alternative else 0.0,
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
# Same key, same lifecycle: how this journey x situation's message came about —
# {"status": ..., "provider": ..., "model": ...}. Written here rather than derived at record time
# because only this loop can tell a per-cycle cap from an exhausted retry from a model that refused,
# and once the deterministic wording is stored all three look identical. See decisions.build_row.
_message_meta: dict[tuple[str, str, str], dict[str, str | None]] = {}

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
    # Identity of the matched train, carried so a recorded decision can be joined back to what
    # actually happened. The grain that works for BOTH families is (operator, jkey, call_date,
    # stop_order): rail refs are date-scoped and unique on their own, but bus ServiceJourney jkeys
    # repeat every operating day (demo_journeys.sql:11-15). Without these an alert can only be
    # matched to ET on (operator, line, departure_time), which is a guess, not a key.
    jkey: str | None = Field(
        default=None,
        description="Journey key of the worst affected train — the SIRI-ET "
        "`datedVehicleJourneyRef` minus its codespace prefix. Identifies the DEPARTURE side.",
    )
    stop_place: str | None = Field(
        default=None,
        description="StopPlace ref of the boarding call the numbers describe.",
    )
    stop_order: int | None = Field(
        default=None,
        description="Position of that boarding call on the journey — the rest of the ET join key.",
    )
    decided_at: datetime | None = Field(
        default=None,
        description="When this match was evaluated (UTC). With `departure_time` it gives the lead "
        "time the decision was made at, which is the axis alerting quality varies along most.",
    )
    cancelled: bool = Field(
        default=False,
        description="ET has ALREADY declared this boarding call cancelled — the train is not "
        "running, as a fact rather than a prediction. The wording states it ('Innstilt') instead of "
        "quoting `cancel_probability`, which is then 1.0 by construction.",
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
    # Ref pairs, not one pair: a stop the registry groups with others is asked about under each of
    # its refs, because each feed records its calls on its own (see place_refs). Almost every place
    # has a single ref, so this is one pass in the ordinary case. The alternative-journey lookup runs
    # only for the canonical pair — it is a blocking external call and the answer ("a faster way from
    # here to there") does not vary by which ref of one place the question named.
    ref_pairs = [
        (f, t)
        for f in place_refs(journey["from_stop"])
        for t in place_refs(journey["to_stop"])
    ]
    alternative = None
    for window_start, window_end in journey_windows(journey, now):
        for pair_index, (from_ref, to_ref) in enumerate(ref_pairs):
            impact = _impact(
                journey, window_start, window_end, catchable_after=now,
                with_alternative=with_alternative and pair_index == 0,
                from_stop=from_ref, to_stop=to_ref,
            )
            if impact is None:
                continue
            alternative = alternative or impact.alternative_journey
            _collect_alerts(impact, worst_by_situation, now=now)

    # The alternative is a property of the trip, not of the situation that won: an alert whose worst
    # reading came from a non-canonical ref (asked without the lookup) still gets the advice.
    for alert in worst_by_situation.values():
        if alert.alternative_journey is None:
            alert.alternative_journey = alternative

    alerts = sorted(worst_by_situation.values(), key=_alert_rank, reverse=True)
    return alerts


def _collect_alerts(
    impact: ImpactResponse, worst_by_situation: dict[str, "Alert"], *, now: datetime
) -> None:
    """Fold one /impact response into the worst-per-situation accumulator.

    Extracted from :func:`match_journey` when that grew a second loop (one pass per stop ref of the
    journey's endpoints): the accumulator is what makes the extra passes safe, since two refs of one
    place can return the same situation and only the worst reading of it survives.
    """
    observation_by_line = {line.line: line.latest_observation for line in (impact.lines or [])}
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
            jkey=sit.jkey,
            stop_place=dep.stop_place,
            stop_order=dep.stop_order,
            decided_at=now,
            cancelled=dep.cancelled,
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


def _alert_severity(alert: Alert) -> tuple[float, int, float]:
    """The shared severity order, applied to an Alert.

    Delegates to `impact.severity_key` rather than restating it. Keeping a second copy in sync by
    comment is what let the two orders drift apart in the first place (main's was cancellation-first
    while this one was impact-first), and `Alert` carries the same three field names as
    `StopPrediction`, so the one function serves both.
    """
    return severity_key(alert)


def _alert_rank(alert: Alert) -> tuple:
    """The full worst-first order: severity, then situation number as a deterministic tie-break.

    The tie-break stopped being a formality on 2026-08-05. A call ET has declared cancelled scores
    exactly (1.0, 0, 1.0), so every situation about one cancelled train ties on all three
    components, and which of them headed the card — and therefore which situation the written
    message was cached under — came down to dict iteration order and could flip between refreshes.
    """
    return (_alert_severity(alert), alert.situation_number)


def _train_key(alert: Alert) -> tuple | None:
    """What makes two alerts the same departure in the traveller's eyes.

    The boarding call's ET join key when the match carried one — `(operator, jkey, stop_place,
    stop_order)` is the grain the decision log and the nightly labelling job already agree on —
    falling back to `(operator, line, departure_time)` when there is no journey ref. `None` means
    "cannot tell which train this is", and an alert that cannot be identified is never merged with
    another: showing one card too many is a much smaller error than hiding an unrelated disruption.
    """
    if alert.departure_time is None:
        return None
    if alert.jkey:
        return (alert.operator, alert.jkey, alert.stop_place, alert.stop_order)
    if alert.line:
        return (alert.operator, alert.line, alert.departure_time)
    return None


def one_alert_per_train(alerts: list[Alert]) -> list[Alert]:
    """Collapse alerts about the SAME departure to the most severe one, order preserved.

    ``/impact`` already rolls its `lines` up so a line is never listed once per situation; this is
    that same rule one level out, where the unit is the traveller's train rather than the line.

    Operators routinely publish several SX messages about one disruption. On 2026-08-05 Vy sent both
    "Ta neste tog" and "Innstilt Drammen–Skøyen" for the same cancelled 15:03 from Asker, and the
    card showed two boxes quoting **two different percentages for one train** (91 % and 87 %),
    because each situation scores the same call independently. `JourneyCard` already refuses to
    print two percentages inside one box; nothing stopped two boxes. The lock screen had the same
    defect one worse: two pushes about a single departure.

    **Traveller-facing only.** `check_and_notify` hands `_record_decisions` the uncollapsed list, so
    a decision row is still written per situation — those rows are the lead-time series, and a
    situation suppressed on the card is still a decision that was made about it.

    The survivor is the most severe by :func:`_alert_rank`, which is also the order `match_journey`
    returns, so the alert that survives is the one loop 1 wrote the message for and cached under.
    """
    winner_at: dict[tuple, int] = {}
    out: list[Alert] = []
    for alert in alerts:
        key = _train_key(alert)
        if key is None:
            out.append(alert)
            continue
        pos = winner_at.get(key)
        if pos is None:
            winner_at[key] = len(out)
            out.append(alert)
        elif _alert_rank(alert) > _alert_rank(out[pos]):
            # Keep the first-seen POSITION: the incoming list is already worst-first, so replacing
            # in place preserves that ordering while the tie-break decides which situation shows.
            out[pos] = alert
    return out


# ---- Notify ----------------------------------------------------------------------------------
def _delay_phrase(message_input: dict) -> str | None:
    """"typisk 15–27 min forsinket" from the exceedance-stack interval (p50–p90, /demo's delay
    column); collapsed to one number when they coincide, None when neither is known."""
    delay = message_input.get("forsinkelse_min") or {}
    p50, p90 = delay.get("typisk"), delay.get("verstefall")
    if not p50 and not p90:
        return None
    if p50 and p90 and p90 > p50:
        return f"Typisk {p50}–{p90} min forsinket"
    return f"Typisk {p90 or p50} min forsinket"


def _deterministic_body(message_input: dict) -> str:
    """The template wording: what the push says when no written message is available.

    Written from the same message input the model is prompted with (llm_message.build_message_input)
    rather than from the Alert directly, so the two wordings are always built from one set of facts.
    Adding a figure to the input is what makes it sayable by both; before this, enriching the
    template meant reaching past the document the model had already been handed.

    Every part is optional, so the body is assembled from the ones that exist rather than
    interpolated blind. A pushed alert is gated on `impact_probability`, which comes off the
    exceedance head and not off these columns: `p_cancel` can sit under the cancellation bar (so the
    input carries no `innstilling_prosent`) while both delay percentiles are NULL, and `sammendrag`
    is nullable too. Interpolating either unguarded put the literal string "None" in a notification
    body. Dropping the part is the honest fallback -- with a departure time present (the matcher
    needs one, so in practice there always is) the reader still gets which train, and the payload
    title still names the journey.
    """
    situations = message_input.get("situasjoner") or []
    summary = situations[0].get("sammendrag") if situations else None
    # `innstilling_prosent` is in the input only when cancellation is the most likely outcome, which
    # is exactly when the wording should lead with it -- see llm_message.build_message_input.
    cancel_pct = message_input.get("innstilling_prosent")
    if message_input.get("innstilt"):
        # ET has already declared this departure cancelled. State it. A probability here would be
        # both wrong and unhelpful: the train is not in the journey planner any more, so "91 %
        # sjanse for innstilling" sends the traveller looking for a departure that does not exist
        # (2026-08-05). The input carries no delay figures in this case either -- they meant
        # "if it runs".
        detail = "Innstilt"
    elif cancel_pct is not None:
        detail = f"{cancel_pct} % sjanse for innstilling"
    else:
        detail = _delay_phrase(message_input)
    hhmm = message_input.get("avgang_kl")
    if hhmm:
        train = f"{message_input.get('transportmiddel')} kl {hhmm}"
        detail = f"{train}: {detail}" if detail else train
    return "\n".join(part for part in (summary, detail) if part)


def _tag_environment(title: str) -> str:
    """Prefix a notification title with its environment on non-prod deployments, e.g. "[dev] ".

    The OS names the sending app from the manifest, which is identical in every environment, so a
    dev push and a prd push are indistinguishable in the tray unless the title says which. Leading,
    not trailing: phones truncate the title on the right, exactly where a suffix would be lost.
    Nothing is added in prd (see :func:`deployment.environment_label`), so real travellers see the
    plain title.
    """
    env = environment_label()
    return f"[{env}] {title}" if env else title


def _notification_payload(
    journey: dict, alert: Alert, message: str | None = None, *, message_input: dict | None = None
) -> dict:
    """Norwegian Web Push payload for one journey×situation match.

    `message` is the LLM-written text when one was produced; the deterministic template is used
    otherwise, so a missing or failing message writer costs wording, never the notification. The
    title, url and situation_number are unchanged either way — the notification stays recognisable
    and the service worker's click-through keeps working.

    `message_input` is this decision's message input (see :func:`_message_input`); `None` means it
    could not be built, and the template falls back to reconstructing the little of it the wording
    needs from the alert — a push must not be lost to a payload problem.
    """
    from_name = journey.get("from_name") or journey["from_stop"]
    to_name = journey.get("to_name") or journey["to_stop"]
    return {
        "title": _tag_environment(f"Avvik: {from_name} → {to_name}"),
        "body": message or _deterministic_body(message_input or _message_input_from_alert(alert)),
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


def _remember_message_meta(
    message_key: tuple[str, str, str],
    *,
    status: str,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """Note how this journey × situation's message came about, for the decision log.

    `provider`/`model` are only overwritten when the caller knows them, so a later cycle that merely
    reuses a cached message (status ``ok_cached``) does not erase which backend actually wrote it.
    """
    with _state_lock:
        meta = _message_meta.setdefault(message_key, {})
        meta["status"] = status
        if provider is not None:
            meta["provider"] = provider
        if model is not None:
            meta["model"] = model


def _write_message(alert: Alert, *, journey_key: str, message_input: dict | None) -> str | None:
    """The LLM-written body for this journey × situation, or None to use the template wording.

    Takes the message input this cycle already built rather than building its own, so what the model
    is prompted with is the same object the decision log records and the template wording is
    assembled from. `None` means it could not be built — there is nothing to prompt with, and the
    caller falls back the same way it does for every other failure here.

    Wraps :mod:`llm_message` so that nothing it can do — an unconfigured project, an unavailable
    model, a timeout, an unexpected exception — reaches the notification path. The import is local
    so a deploy without the Vertex dependencies still serves pushes.

    Also records the attempt's provenance (which backend answered, and why it did not) under the
    message key: it is knowable only here, and the stored message cannot be reverse-engineered into
    it — every failure mode ends up as the same deterministic wording.
    """
    if message_input is None:
        return None  # _message_input already logged why
    try:
        import llm_message

        if not llm_message.llm_enabled():
            return None
        result = llm_message.write_message_detailed(
            message_input, journey_key=journey_key, situation_number=alert.situation_number
        )
        _remember_message_meta(
            _message_key(journey_key, alert),
            status=result.status,
            provider=result.provider,
            model=result.model,
        )
        return result.text
    except Exception:
        logger.warning(
            "Written-message step failed; falling back to the deterministic wording",
            exc_info=True,
            extra={"fields": {"event": "llm_message", "result": "error"}},
        )
        return None


def _message_input(journey: dict, alert: Alert) -> dict | None:
    """Everything a message about this decision is written from — see build_message_input.

    Built for **every** decision, including the sub-threshold ones the writer is never asked about:
    "what did the model see?" is unanswerable for a row that has no input, and those rows are the
    majority. Cheap enough to do unconditionally — `build_message_input` is pure apart from an
    in-memory stop-name lookup, and the alert already carries the `/impact` response it reads.

    Guarded on its own rather than inside the caller's blanket try: one journey with an awkward
    payload must cost that journey's input, not the whole cycle's decision log. `None` is recorded
    as a NULL `message_input`, because a row must not claim the writer was shown a document that was
    never built.
    """
    try:
        import llm_message

        return llm_message.build_message_input(journey, alert.impact, alert)
    except Exception:
        logger.warning(
            "Could not build the message input for a decision; recording it without one",
            exc_info=True,
            extra={"fields": {"event": "varsel_decision_write", "result": "no_message_input"}},
        )
        return None


def _message_input_from_alert(alert: Alert) -> dict:
    """The alert's own figures in the message-input shape — a last resort for the *wording* only.

    The message input now backs the push itself and not just the prompt and the log, so "could not
    build it" may no longer mean "no wording". This restates the handful of keys
    :func:`_deterministic_body` reads, straight off the Alert, using nothing from
    :mod:`llm_message` — which is the module that has just failed when this is reached.

    Never recorded and never prompted with: it was not what any writer was shown, and a decision row
    claiming otherwise would be worse than one with a NULL input.
    """
    body: dict[str, Any] = {
        "transportmiddel": alert.mode,
        "avgang_kl": (
            alert.departure_time.astimezone(_OSLO).strftime("%H:%M")
            if alert.departure_time is not None
            else None
        ),
        "situasjoner": [{"sammendrag": alert.summary}],
    }
    # Same three-way split as build_message_input, for the same reason: a declared cancellation is
    # not a probability, and the conditional figures below it do not apply once the train is gone.
    if alert.cancelled:
        body["innstilt"] = True
        return body
    body["forsinkelse_min"] = {"typisk": alert.delay_p50_min, "verstefall": alert.delay_p90_min}
    if alert.cancel_probability >= _CANCEL_LIKELY:
        body["innstilling_prosent"] = round(alert.cancel_probability * 100)
    return body


def _message_input_json(message_input: dict | None) -> str | None:
    """This decision's message input as the exact bytes the model was (or would have been) sent.

    The same serializer the writer uses, so the recorded column is byte-identical to the prompt
    rather than a document that merely resembles it. Guarded for the same reason
    :func:`_message_input` is: a value that will not serialize costs its own column, not the cycle.
    """
    if message_input is None:
        return None
    try:
        import llm_message

        return llm_message.message_input_json(message_input)
    except Exception:
        logger.warning(
            "Could not serialize the message input for a decision; recording it without one",
            exc_info=True,
            extra={"fields": {"event": "varsel_decision_write", "result": "no_message_input"}},
        )
        return None


def _record_decisions(
    by_journey_key: dict[str, dict],
    alerts_by_key: dict[str, list[Alert]],
    journeys: list[tuple[str, dict]],
    pushed_keys: set[tuple[str, str]],
    message_inputs: dict[tuple[str, str, str], dict | None],
    *,
    push_on: bool,
) -> None:
    """Append this cycle's alert decisions to the decision log.

    Records **every** matched situation, not just the alert-worthy ones: a journey we decided not
    to alert on is exactly the evidence a miss is made of, and it exists nowhere else.

    Runs after delivery so `pushed` reflects what actually reached a device rather than what was
    queued. Like every other dependency in this cycle it fails open — the import is local so a
    deploy without the module still pushes, and :func:`decisions.record` never raises.
    """
    try:
        import decisions
        import llm_message
        from llm_message import key_digest

        if not decisions.enabled():
            return
        llm_on = llm_message.llm_enabled()
        device_counts: dict[str, set[str]] = {}
        for _jid, journey in journeys:
            device_counts.setdefault(journey_key(journey), set()).add(journey["device_id"])

        rows = []
        for key, journey in by_journey_key.items():
            digest = key_digest(key)
            n_devices = len(device_counts.get(key, ()))
            for alert in alerts_by_key.get(key, []):
                message_key = _message_key(key, alert)
                with _state_lock:
                    written = _messages.get(message_key)
                    meta = dict(_message_meta.get(message_key) or {})
                # No meta means the writer was never reached for this alert, and why is knowable
                # from the state that decided it: below the threshold, push (and with it the writer)
                # switched off, the LLM switched off — or alert-worthy but not the worst situation
                # for this journey, which is the one the message is written about.
                status = meta.get("status")
                if status is None:
                    if not alert.alertworthy:
                        status = "not_alertworthy"
                    elif not push_on:
                        status = "push_disabled"
                    elif not llm_on:
                        status = "llm_disabled"
                    else:
                        status = "not_selected"
                # Record what was actually SENT, not just what the LLM wrote. `_messages` only ever
                # holds LLM output, so storing it raw left every deterministic push with a NULL
                # message — and prd runs with VARSEL_LLM_ENABLED=false, so /historikk there would
                # have shown no message text at all. Fall back to the same deterministic body
                # _notification_payload puts in the push -- from the same input, so the two agree by
                # construction rather than by two template copies -- and say which one it was.
                message_input = message_inputs.get(message_key)
                message = written or _deterministic_body(
                    message_input or _message_input_from_alert(alert)
                )
                rows.append(
                    decisions.build_row(
                        journey,
                        alert,
                        journey_digest=digest,
                        n_devices=n_devices,
                        pushed=(key, alert.situation_number) in pushed_keys,
                        message=message,
                        message_source="llm" if written else "deterministic",
                        message_input=_message_input_json(message_input),
                        llm_provider=meta.get("provider"),
                        llm_model=meta.get("model"),
                        message_status=status,
                        prompt_version=llm_message.PROMPT_VERSION,
                    )
                )
        decisions.record(rows)
    except Exception:
        logger.warning("Could not record alert decisions this cycle", exc_info=True)


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
    get the deterministic wording this tick rather than delaying the poller thread.

    **Scoring and recording run even when push is disabled**; only delivery is gated. The decision
    log's guarantee is "every decision, not every push", and an early return here quietly broke it:
    an environment without VAPID keys recorded nothing at all, so /historikk looked like a broken
    deployment rather than an un-provisioned one. The LLM stays gated on push, though — writing
    traveller-facing prose nobody can receive is spend with no reader, and the deterministic body
    already captures what the decision was.
    """
    push_on = push_enabled()
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
        for key in [k for k in _message_meta if k[1] not in live]:
            _message_meta.pop(key, None)
        changed = bool(stale)

    # ---- Loop 1: one impact call and one written message per distinct journey definition --------
    by_journey_key: dict[str, dict] = {}
    for _jid, journey in journeys:
        by_journey_key.setdefault(journey_key(journey), journey)

    alerts_by_key: dict[str, list[Alert]] = {}
    # What every message about this cycle's decisions is written from, one per journey definition x
    # situation. Built here, before any wording exists, so the prompt, the deterministic fallback and
    # the recorded row all read the same document rather than three separate builds of it. Local to
    # the cycle: unlike `_messages` it describes this tick's numbers and must not outlive them.
    message_inputs: dict[tuple[str, str, str], dict | None] = {}
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
        for alert in alerts:
            message_inputs[_message_key(key, alert)] = _message_input(journey, alert)
        worst = next((a for a in alerts if a.alertworthy), None)
        if worst is None or not push_on:
            # No message writing without push: the text exists to be delivered, and the decision is
            # recorded with the deterministic wording either way.
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
            # Reused from an earlier cycle: keep who wrote it, but say that this cycle's row carries
            # text written against an earlier cycle's payload, not the one recorded beside it.
            _remember_message_meta(message_key, status="ok_cached")
            continue
        if attempts >= _MAX_MESSAGE_ATTEMPTS:
            # Without this the retry never stops. A write that fails for a payload-dependent reason
            # (a refusal, an over-long reply, an unparseable one) fails again on every 60 s tick for
            # the situation's whole life -- ~720 Vertex calls that can change nothing, because the
            # push already went out with the deterministic wording, each one burning a slot from
            # _MAX_LLM_CALLS_PER_CYCLE that a journey which WOULD have succeeded now cannot use.
            _remember_message_meta(message_key, status="attempts_exhausted")
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
            _remember_message_meta(message_key, status="capped")
            continue
        llm_calls += 1
        # _write_message catches Exception itself, so this is belt and braces for a future edit that
        # lets one escape. It records an attempt rather than `continue`-ing past the counter, so a
        # raiser is subject to the same give-up rule as a None. Delivery is unaffected either way:
        # loop 2 pushes independently and falls back to the deterministic wording.
        try:
            message = _write_message(
                worst, journey_key=key, message_input=message_inputs.get(message_key)
            )
        except Exception:
            logger.warning(
                "Written-message step raised for %s; using the deterministic wording",
                key,
                exc_info=True,
                extra={"fields": {"event": "llm_message", "result": "error"}},
            )
            message = None
            _remember_message_meta(message_key, status="error")
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
    for jid, journey in journeys if push_on else []:
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
        # One push per train, not per SX message. Two situations about one cancelled departure used
        # to mean two notifications a minute apart, saying the same thing with different percentages
        # (2026-08-05). `_notified` still keys on the surviving situation_number, so the suppressed
        # one simply never notifies -- and `_record_decisions` below still sees both.
        for alert in one_alert_per_train(alerts_by_key.get(key, [])):
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
            message_key = _message_key(key, alert)
            with _state_lock:
                message = _messages.get(message_key)
            payload = _notification_payload(
                journey, alert, message, message_input=message_inputs.get(message_key)
            )
            result = _send_push(device, payload)
            out.append((jid, journey, alert, result, payload))
            if result == "gone":
                break  # stop this device's remaining alerts this tick
        return out

    pushed_keys: set[tuple[str, str]] = set()
    if per_device:
        with ThreadPoolExecutor(max_workers=min(_PUSH_WORKERS, len(per_device))) as pool:
            batches = list(pool.map(_deliver_device, per_device.values()))
        for jid, journey, alert, result, payload in [r for batch in batches for r in batch]:
            _log_push("alert", result, journey["device_id"], payload, journey_id=jid, alert=alert)
            if result == "sent":
                with _state_lock:
                    _notified.add(f"{jid}|{alert.situation_number}")
                pushed_keys.add((journey_key(journey), alert.situation_number))
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

    _record_decisions(
        by_journey_key, alerts_by_key, journeys, pushed_keys, message_inputs, push_on=push_on
    )

    if changed:
        _save_state()


# ---- API models ------------------------------------------------------------------------------
class ConfigResponse(BaseModel):
    """What the PWA needs to decide whether/how to subscribe to push."""

    push_enabled: bool = Field(description="Whether the server can send Web Push (VAPID configured).")
    vapid_public_key: str | None = Field(
        description="VAPID application server key (base64url) for pushManager.subscribe; null if disabled.",
    )
    environment: str | None = Field(
        description='Non-prod environment name ("dev", "tst") for the PWA to show in its title; null in prd.',
    )


class StopItem(BaseModel):
    """A stop offered in the from/to pickers.

    One row per physical place: refs the registry groups under the same parent place, under the same
    name, are collapsed by main.refresh_varsel_stops and only the busiest one is offered here.
    """

    stop_place: str = Field(description="NSR StopPlace ref.")
    stop_name: str | None = Field(description="Human-readable stop name.")
    mode: str = Field(description='Families serving the stop today: "Tog", "Buss" or "Tog/Buss".')
    n_calls: int = Field(description="Scheduled calls at the stop today; pickers rank matches by it.")
    place_id: str = Field(
        description=(
            "The physical place this row stands for (NSR parent StopPlace, else the ref itself). "
            "Clients may de-duplicate on it; the server has already done so."
        )
    )
    region: str | None = Field(
        default=None,
        description=(
            "County, from the registry's municipality number. Pickers append it when a name is "
            "offered by more than one place; NULL when the registry does not place the stop in a "
            "Norwegian county."
        ),
    )


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
    warning: str | None = Field(
        default=None,
        description=(
            "Something about the registration itself that the traveller should see — currently only "
            "'no planned trip connects these stops today', which means this journey cannot produce "
            "an alert. NULL when there is nothing to say."
        ),
    )


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
    to ``pushManager.subscribe`` -- plus which environment is answering."""
    return ConfigResponse(
        push_enabled=push_enabled(),
        vapid_public_key=_VAPID_PUBLIC_KEY if push_enabled() else None,
        environment=environment_label(),
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
            place_id=r.get("place_id") or r["stop_place"],
            region=r.get("region"),
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
    # One card per train, not per SX message -- see one_alert_per_train. Collapsed here rather than
    # in match_journey because the matcher also feeds the decision log, which must keep every
    # situation. `alerts` may be shared across several registrations of the same definition, so this
    # builds a new list and never mutates the caller's.
    alerts = one_alert_per_train(match_journey(rec) if alerts is None else alerts)
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
        # Suppressed while something is actually matching: a card cannot credibly say "we found no
        # departures between these stops" directly above a disruption on one of them.
        warning=None if alerts else _service_warning(rec),
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

    Stop refs are canonicalized first: a client holding a stale stop list — or posting straight to
    the API — can still name a ref that was collapsed onto a co-located primary, and storing it
    would recreate exactly the failure the collapse exists to prevent (a watched quay the trip never
    calls at, reported as "ingen aktive avvik akkurat nå").

    Plain ``def`` for the same reason as :func:`list_journeys`."""
    from_stop = canonical_stop(body.from_stop)
    to_stop = canonical_stop(body.to_stop)
    if from_stop == to_stop:
        # Reachable without the client doing anything odd: two refs for one physical place look like
        # two stops in an older stop list. JourneyIn's from != to check ran before canonicalization.
        raise HTTPException(
            status_code=422, detail="Fra- og til-stoppet er samme stoppested."
        )
    # Can any trip today carry this traveller from A to B at all? varsel only alerts on a DIRECT trip
    # (`to_stop` narrowing keeps only trains that reach the alighting stop), so a pair no single trip
    # connects cannot produce an alert however bad the day gets — and saying so is the whole point:
    # what this replaces is a card reading "ingen aktive avvik akkurat nå" for the journey's whole
    # life. The verdict never blocks the registration; it is carried as a warning (_service_warning).
    verdict = _service_verdict(from_stop, to_stop)
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
        # Annotated: inferred from the literal alone the value type is `list[int] | str | None`, and
        # the service-check fields added below (a bool, a timestamp) then read as type errors.
        rec: dict[str, Any] = {
            "device_id": body.device_id,
            "from_stop": from_stop,
            "from_name": body.from_name,
            "to_stop": to_stop,
            "to_name": body.to_name,
            "begin": body.begin,
            "end": body.end,
            "days": body.days,
            "created_ts": _now_iso(),
        }
        if verdict is not None:
            rec["service_checked_ts"] = _now_iso()
            if not verdict[0]:
                rec["no_direct_service"] = True
                rec["service_reverse_only"] = verdict[1]
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
        "title": _tag_environment("Varsler er aktivert"),
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
