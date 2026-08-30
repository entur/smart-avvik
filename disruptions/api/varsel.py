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
* **Writing that text and sending it are two switches** (2026-08-07). A wording is written whenever
  a backend can be tried (``llm_message.generation_enabled``); ``VARSEL_LLM_ENABLED``
  (``llm_message.delivery_enabled``, read through :func:`_deliverable_message`) decides only which
  of the two already-written wordings the traveller receives — on the push body and the in-app card
  alike. prd delivers the deterministic one, so it now writes the model's version too and records
  both: the only way the two can be compared on the traffic that actually matters.
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
* **What the push says** — which train (``_train_label``: "Tog L1 09:24", the line included
  because a journey matches several of them and a time alone does not say which), then the
  *alerting* quantity in ``/demo``'s wording: P(innstilling eller ≥ 10 min forsinkelse), then
  whichever driver dominates (likely cancellation, or the exceedance-stack delay interval). Never
  the raw ``dep_delay_typ_min`` regression: for today's alert-worthy boardings its median is 2 min
  against the stack p50's 15 min (rail), so quoting it made a 93 %-impact alert read as "1 % sjanse
  for innstilling, typisk 2 min forsinket". An alternative, when there is one, closes the body with
  the same label over its FIRST transit leg ("Alternativ tog R11 08:24") — the departure the
  traveller has to catch, as opposed to a connection further along it.
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
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import feedback
import feedback_contexts

# The alert gate moved out to its own module on 2026-08-06 so `impact` can reach it too (importing
# it back from here would be a cycle — see alert_policy's docstring). Bound at module level rather
# than reached through `alert_policy.`, so `varsel.threshold_for` / `varsel.TROUBLE_DELAY_MIN` keep
# resolving for the callers that already read them off this module. TROUBLE_DELAY_MIN is such a
# re-export and nothing else: it is /demo's realized-outcome bar, not something varsel reads.
from alert_policy import TROUBLE_DELAY_MIN, min_threshold, threshold_for  # noqa: F401
from alternatives import AlternativeJourney
from deployment import environment_label
from fastapi import APIRouter, HTTPException, Query, Request
from impact import AlternativeOutcome, ImpactResponse, LatestObservation, severity_key
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

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
# the poller's per-cycle work is bounded only by how many people install the PWA.
#
# Raised 10x on 2026-08-21 for a pilot an order of magnitude larger, against a measured cycle rather
# than a guess -- which is what the old comment here asked for and nobody had done. Measured in prd
# that evening over three consecutive cycles: the rebuild runs 50-63 s, of which `check_and_notify`
# -- 44 journey definitions matched, decisions built, 113 rows inserted -- is 1.3-1.7 s, and the
# cycle PERIOD is ~177 s (p50), set by how often the upstream SIRI-SX ingestion timestamp moves and
# not by anything here. So the varsel path owns ~3 % of a cycle with ~120 s of headroom, and ten
# times the journeys is ~13 s of matching, which the period still absorbs. BigQuery owns the other
# 97 % and does not scale with registrations at all.
#
# What does not absorb it linearly is the blocking I/O on this thread -- Journey Planner lookups and
# LLM writes -- and that is precisely why this cap can move: each is bounded by its OWN per-cycle
# ceiling (`alternatives._FETCH_MAX_PER_MIN`, `_MAX_LLM_CALLS_PER_CYCLE`), independent of how many
# people are registered. Raising those is a separate decision from raising this one; do not read a
# bigger pilot as permission to widen them by the same factor.
#
# The old 500 was also the wrong SHAPE of limit for what it protected: it is a hard 503 out of
# `upsert_device` ("Tjenesten er full"), so it turns away a signup rather than degrading a cycle,
# while the per-cycle cost it stood in for is bounded elsewhere anyway. Keep it enforced -- an
# unbounded client-generated key still needs a ceiling -- and keep raising it deliberately after
# measuring a cycle, not by discovering it in the logs.
_MAX_DEVICES = 5000
_MAX_JOURNEYS = 20000
# Written messages per refresh cycle. The matcher runs on the poller thread, which also rebuilds
# every other cache, so an unbounded fan-out of LLM calls would stall ingestion. Trains beyond the
# cap get the deterministic wording this tick and are picked up on the next one.
#
# 8 -> 64 on 2026-08-21, when the writing pass stopped being sequential (see `_LLM_WORKERS`). The
# old number was a wall-clock budget wearing a count's clothing: 8 calls at ~2.2 s each is ~18 s of
# a 30 s ceiling, so what it really encoded was "about as many as fit end to end". Concurrency
# breaks that identity -- 64 calls across 16 workers is ~9 s -- so the count is now a policy choice
# about how much of a cycle's demand deserves prose, and `_MAX_LLM_SECONDS_PER_CYCLE` is what still
# bounds the thread.
#
# Sized on measured demand, not on what the pool can do. Reconstructing the per-train writing rule
# over 14 days of prd decisions: distinct trains pushed per cycle, capped at 3 per journey, runs
# p50 2 / p90 6 / p99 19 / max 22, and only 13 of 250 pushing cycles exceed 8 at all. At 10x
# registrations that p99 is ~200, which no affordable cap covers -- so 64 is chosen to clear the
# projected p90 rather than the p99, and the tail deliberately takes the template. Note the cost of
# raising it is close to zero in aggregate: demand is bursty, not sustained (prd wants ~50 writes a
# DAY), so this only ever binds on the handful of cycles it exists for.
_MAX_LLM_CALLS_PER_CYCLE = 64
# Give-up point for writing one journey × situation's message when the failure is PAYLOAD-dependent
# — the model was asked and what came back was unusable for this exact input (a refusal, an
# over-long or unparseable reply, an invented figure). Such a write will very likely keep failing
# for this payload, and retrying costs a slot from the per-cycle cap without being able to change
# what any user already saw.
#
# This counter used to be charged for EVERY `None`, which is the defect fixed on 2026-08-22. Several
# of `_write_message`'s paths return `None` without asking a backend at all, and a transient backend
# failure says nothing whatsoever about the payload — so three bad minutes retired a subject's
# wording for its whole 12 h life. Measured in prd over the 14 days to 2026-08-21: of the subjects
# that ever reached `attempts_exhausted`, the statuses that preceded it were **290 `error` and zero
# `rejected` or `ungrounded`** — the budget was being spent, in prd, exclusively on the one class of
# failure it was never meant to bound. See :data:`_MAX_TRANSIENT_ATTEMPTS`.
_MAX_MESSAGE_ATTEMPTS = 3
# ...and the separate, larger give-up point for TRANSIENT failures: the backend was reached and did
# not answer (a timeout, a 429, a 5xx). A bound is still needed -- each one costs a real call and a
# slot from `_MAX_LLM_CALLS_PER_CYCLE`, and a subject that fails every cycle for 12 h would spend
# ~240 of them -- but it must be far looser than the payload one, because a retry here genuinely
# works: of prd's 270 subjects that hit at least one `error` in those 14 days, **149 (55 %) went on
# to succeed**, while 99 were retired at three. Those 99 are wordings that a retry would have
# produced and the cap threw away.
#
# 12, sized above the worst run actually observed (p50 2 errors per subject, max 9) so that every
# case measured would now get through, while still bounding the pathological one. It is a ceiling on
# waste, not a target -- unlike the payload counter, reaching it says the environment is unwell, and
# `transient_exhausted` on the decision row is what makes that visible instead of blaming the model.
_MAX_TRANSIENT_ATTEMPTS = 12
# What a failed write says about the PAYLOAD versus about the environment. The three-way split is
# the whole fix: a status naming neither is one where no backend was asked at all (`disabled`,
# `unavailable`, an input that could not be built), which costs nothing and teaches nothing, so it
# must charge neither counter -- that is the path that produced prd's 753 `llm_disabled` rows beside
# 693 `attempts_exhausted` across the same 11 subjects on 2026-08-07.
_PAYLOAD_FAILURES = frozenset({"rejected", "ungrounded"})
_TRANSIENT_FAILURES = frozenset({"error", "truncated"})
# Written messages per JOURNEY per cycle. The cycle cap above bounds the poller thread; this one
# stops a single journey spending all of it. A journey routinely has more disrupted trains than
# either budget can cover -- measured on dev over 4 099 journey-cycles: p50 3 alert-worthy trains,
# p90 12, max 22 -- but the trains that matter are the ones about to be PUSHED, and those are few
# (p90 1, max 18; 88 % of cycles push nothing). Sized like _MAX_ALTERNATIVE_LOOKUPS and for the same
# reason: three covers the overwhelming majority (39 of 489 pushing cycles push more than three) at
# a bounded worst case. Trains past it take the deterministic wording and are recorded
# `not_selected`, so what the cap costs stays countable rather than being inferred from a log line.
_MAX_MESSAGES_PER_JOURNEY = 3
# Wall-clock ceiling for the whole message-writing pass. The call cap alone does not bound time:
# one capped "call" can be two backend attempts at _TIMEOUT_S each, so the count implies half the
# worst case. Trains past the budget take the deterministic wording and are re-tried next tick.
#
# Unchanged at 30 s across the 2026-08-21 fan-out, and that is the point of it: with the calls
# concurrent, the COUNT no longer implies a duration at all, so this is the only thing left that
# bounds how long the ingestion thread spends here. The reservation below is still per call and
# still taken before each one starts (now under `_WriteBudget`'s lock rather than in a single-threaded
# loop), so the guarantee is the same one it always was -- the pass ENDS by this deadline, it does
# not merely stop starting calls at it.
_MAX_LLM_SECONDS_PER_CYCLE = 30.0
# How many journeys are matched at once, and how many wordings are written at once. Both passes are
# blocking I/O on the poller thread and nothing else: a match spends its time inside up to
# `impact._MAX_ALTERNATIVE_LOOKUPS` Journey Planner calls at ~3 s, a write inside a Vertex call at
# ~2.2 s. So the width buys wall clock almost linearly and costs almost no CPU -- every one of these
# threads is parked in a socket read.
#
# 16 each, sized against the ceilings they drain rather than against the machine: 90 lookups
# (`alternatives._POLLER_FETCH_MAX_PER_MIN`) is ~17 s at this width instead of ~270 s sequential, and
# 64 writes is ~9 s instead of ~140 s. Raising a ceiling without raising the matching width just
# makes the poller block longer, which is the trap the old 8-call LLM cap was quietly protecting
# against.
_MATCH_WORKERS = 16
_LLM_WORKERS = 16
# What one more "call" can cost before it returns: two backend attempts at llm_message._TIMEOUT_S.
# Restated here rather than imported, like _CANCEL_LIKELY above and for the same reason -- this is
# consulted on the poller thread before llm_message is necessarily importable, and the value has to
# bound the budget even when reading that module is what failed.
#
# It exists because the deadline used to be checked but not RESERVED: `monotonic() >= llm_deadline`
# is a pre-check, so a call could start with a millisecond left and still run its full worst case.
# The reachable overshoot was ~46 s against a documented 30 s ceiling -- 53 % over, on the single
# poller thread that also feeds /disruptions, /journey, /demo and /reisevalg, and ahead of the
# delivery loop that pushes to travellers.
_WORST_CASE_CALL_S = 2 * 8.0
# Web Push sends are independent, I/O-bound, and each can block for _PUSH_TIMEOUT_S against a
# client-supplied endpoint. Sequential delivery meant one sick push service stalled the whole poller
# -- and with it every cache rebuild -- for devices x 10 s. Fan them out instead.
#
# Sized against the WORST case, not the normal one, because the normal one is invisible at any
# width: a healthy send is ~100-300 ms, so even several hundred devices finish in seconds. What this
# number actually decides is how long a DEGRADED push service holds the ingestion thread. The pool
# runs one task per device with pending pushes and each task's sends stay sequential (see
# `deliver_to_devices` -- a "gone" result must stop that device's remaining items), so the ceiling is
# ceil(devices / _PUSH_WORKERS) x _PUSH_TIMEOUT_S. At 8 workers a 10x pilot whose endpoints all hang
# costs ~9 minutes of stalled poller; at 32 it is ~2. The threads are nearly free -- every one is
# parked in a socket read, not computing -- so this tracks the device cap rather than the machine,
# and the two were raised together (2026-08-21).
_PUSH_WORKERS = 32
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


def _still_catchable(alt: AlternativeJourney | None, now: datetime) -> bool:
    """Whether a cached alternative can still be boarded at `now` — one rule, both readers.

    Strictly ``>``, which is :func:`alternatives.select_best`'s own pool rule ("a departure exactly
    at `now` is not offered"). :func:`_journey_model` applied ``>=`` instead, so for one second the
    card could offer a departure the selection rule had already refused to choose. Two operators for
    one boundary is how the two drift.

    An alternative with no readable departure time is kept: the Journey Planner can answer without
    one, and "we cannot tell whether it has left" is not "it has left".
    """
    if alt is None:
        return False
    return alt.departure_time is None or alt.departure_time > now


def _alternative_identity(alt: AlternativeJourney) -> tuple:
    """What makes two alternatives the same advice: everything a wording actually states.

    Not the whole model — both `*_prediction` blocks carry continuous probabilities re-scored
    against the live prediction cache on every cycle, so comparing those would invalidate the
    written message on nearly every tick, turning ``ok_cached`` into a per-cycle LLM call. That is
    exactly what :data:`_MAX_LLM_CALLS_PER_CYCLE` and :data:`_MAX_LLM_SECONDS_PER_CYCLE` exist to
    prevent.

    The two BOOLEANS are here, though, and the two times are not the whole story (2026-08-06).
    `at_risk` and `arrives_before_original` are each stated out loud — "(kan også bli rammet)",
    and the caveat that the traveller arrives later than they planned — so an alternative whose
    times round the same but whose `at_risk` has flipped false → true is NOT the same advice. It
    was treated as such: `_reconcile_caches` stored the rescored journey and kept the message
    written when the flag was false, so the cached text promised a clean escape that had stopped
    existing. They cost nothing to include: a boolean flips at most once or twice in a situation's
    life, where a probability moves every cycle, so this cannot reintroduce the flapping the
    minute-rounding below exists to stop.

    The times are compared AT THE MINUTE, for that same reason. Both are Journey Planner *expected*
    times carried at second precision, while every wording renders ``%H:%M``: a realtime nudge from
    08:24:12 to 08:24:41 changed the identity, dropped the message and spent an LLM call to produce
    byte-identical prose — roughly every other cycle per alerting journey, out of the budget of 8.
    The resolution of the comparison has to be the resolution of the advice.
    """
    def minute(ts: datetime | None) -> datetime | None:
        return ts.replace(second=0, microsecond=0) if ts is not None else None

    return (
        minute(alt.departure_time),
        minute(alt.arrival_time),
        alt.at_risk,
        alt.arrives_before_original,
    )


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
# Same key: the alternative the poller found for that journey x situation. Cached rather than
# recomputed because the request path deliberately does NOT call the Journey Planner (see
# match_journey) -- without this the field would be structurally null on the only endpoint that
# serves it, which is how it shipped until review on 2026-07-29.
#
# The situation's end is the OUTER bound of both these caches' lives, not their whole lifecycle:
# _reconcile_caches drops an entry as soon as this cycle's match supersedes it (see its docstring).
# A 12 h-long situation would otherwise keep advising an escape route chosen against numbers from
# hours ago -- including one the selection rule has since decided against.
_alternatives: dict[tuple[str, str, str], AlternativeJourney] = {}
# Same key, same lifecycle: how many times writing this message has failed FOR REASONS TO DO WITH
# THE PAYLOAD, so an input the LLM cannot handle is not retried on every tick for the situation's
# whole life. See :data:`_MAX_MESSAGE_ATTEMPTS`.
_message_attempts: dict[tuple[str, str, str], int] = {}
# Same key, same lifecycle: how many times the backend was reached and failed TRANSIENTLY. Counted
# apart from `_message_attempts` because the two answer different questions -- "is this payload
# hopeless?" and "is Vertex having a bad morning?" -- and one counter answering both meant a bad
# morning permanently retired a perfectly writable payload. See :data:`_MAX_TRANSIENT_ATTEMPTS`.
_message_errors: dict[tuple[str, str, str], int] = {}
# Same key, same lifecycle: how this journey x situation's message came about —
# {"status": ..., "provider": ..., "model": ...}. Written here rather than derived at record time
# because only this loop can tell a per-cycle cap from an exhausted retry from a model that refused,
# and once the deterministic wording is stored all three look identical. See decisions.build_row.
_message_meta: dict[tuple[str, str, str], dict[str, Any]] = {}

# Togvarsel push round markers, {"<YYYY-MM-DD Oslo>": {"<issue>": {"level": str, "pushed": bool}}} —
# togvarsel_notify's single state primitive (dedup + the follow-up baseline in one). Persisted in
# the state blob so a completed round never re-fires across restarts. Exactly one date is kept:
# store_togvarsel_rounds prunes to the date it is given, which is how day rollover forgets
# yesterday. Only the accessor pair below touches this — togvarsel_notify gets those injected and
# never reads varsel globals.
_togvarsel_rounds: dict[str, dict[str, Any]] = {}
# Unknown top-level keys in the loaded blob, carried through apply→serialize verbatim (rollback
# insurance): a key written by a future feature must survive THIS build rewriting the blob, so a
# rollback-and-forward does not silently erase that feature's state.
_state_extra: dict[str, Any] = {}
_STATE_KEYS = frozenset({"devices", "journeys", "notified", "togvarsel_rounds"})

_STATE_PATH = Path(os.environ.get("VARSEL_STATE_PATH", "/tmp/varsel_state.json"))
_STATE_BUCKET = os.environ.get("VARSEL_STATE_BUCKET")  # GCS bucket for durable state (optional)
_STATE_BLOB = os.environ.get("VARSEL_STATE_BLOB", "varsel_state.json")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _serialize_state() -> str:
    with _state_lock:
        return json.dumps(
            {
                **_state_extra,  # unknown keys ride along verbatim (rollback insurance)
                "devices": _devices,
                "journeys": _journeys,
                "notified": sorted(_notified),
                "togvarsel_rounds": _togvarsel_rounds,
            }
        )


def _apply_state(raw: str) -> None:
    data = json.loads(raw)
    with _state_lock:
        global _devices, _journeys, _notified, _togvarsel_rounds, _state_extra
        _devices = dict(data.get("devices", {}))
        _journeys = dict(data.get("journeys", {}))
        _notified = set(data.get("notified", []))
        _togvarsel_rounds = dict(data.get("togvarsel_rounds", {}))
        _state_extra = {k: v for k, v in data.items() if k not in _STATE_KEYS}


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


# ---- Togvarsel round markers + journey snapshot (injected into togvarsel_notify) --------------
# That module imports neither varsel nor main (the package rule), so main hands it these
# callables. Both marker accessors deal in detached copies: togvarsel_notify mutates its working
# map freely and hands it back whole through the store — it never reaches varsel globals.


def togvarsel_rounds_for(date_str: str) -> dict[str, Any]:
    """A copy of the togvarsel round markers for one Oslo date ({} when none exist)."""
    with _state_lock:
        return {k: dict(v) for k, v in _togvarsel_rounds.get(str(date_str), {}).items()}


def store_togvarsel_rounds(date_str: str, markers: Mapping[str, Any]) -> None:
    """Replace the round markers for `date_str` — the current Oslo date — and persist.

    Every OTHER date is pruned: markers dedup rounds and seed follow-up baselines for one day
    only, so the first write after midnight is what forgets yesterday. Persistence is
    :func:`_save_state`'s best-effort write-through — a failure there is logged, never raised, so
    the in-memory markers still dedup this process and the cost of a lost write is bounded by the
    next restart re-evaluating inside the timeliness window."""
    with _state_lock:
        global _togvarsel_rounds
        _togvarsel_rounds = {str(date_str): {k: dict(v) for k, v in markers.items()}}
        _save_state()


def journeys_snapshot() -> dict[str, dict]:
    """A shallow-copied snapshot of the registered journeys, for togvarsel_notify's matcher (it
    only reads scalar fields, and one take of the lock keeps a concurrent delete from tearing a
    record mid-iteration)."""
    with _state_lock:
        return {jid: dict(rec) for jid, rec in _journeys.items()}


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
    one written message PER DISRUPTED TRAIN between them, and therefore identical text. Deliberately excludes
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
    has_situation: bool = Field(
        default=True,
        description="False when no SX message named this train and the numbers are a prediction "
        "from real-time data alone. It changes three things and each matters: which threshold the "
        "alert was gated at, what the wording may claim (there is no operator text to quote), and "
        "which segment the decision lands in on /historikk.",
    )
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
        "produced, when no message writer was available, or when this deployment does not deliver "
        "written wordings (VARSEL_LLM_ENABLED=false — the ordinary production case, not a failure: "
        "a message may well have been written and recorded). In every one of those cases the push "
        "carries the deterministic wording, and this field says so by being null rather than by "
        "repeating it: the card shows exactly what the notification showed.",
    )
    feedback_context_id: str | None = Field(
        default=None,
        description="Opaque, short-lived handle for the exact alert rendering; null when disabled.",
    )
    feedback_enabled: bool = Field(
        default=False,
        description="Whether this server currently accepts isolated per-alert feedback.",
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
    # Every lookup this pass completed, keyed by the TRAIN it was about. See the back-fill below.
    by_train: dict[tuple[str | None, str], AlternativeOutcome] = {}
    for window_start, window_end in journey_windows(journey, now):
        for pair_index, (from_ref, to_ref) in enumerate(ref_pairs):
            impact = _impact(
                journey, window_start, window_end, catchable_after=now,
                with_alternative=with_alternative and pair_index == 0,
                from_stop=from_ref, to_stop=to_ref,
            )
            if impact is None:
                continue
            for sit in impact.situations or []:
                # getattr, like every other read off the impact response in this module: it is
                # duck-typed, and a synthetic provider need not carry the newest fields.
                subject = getattr(sit, "alternative_subject", None)
                if subject is not None:
                    by_train.setdefault(
                        subject,
                        AlternativeOutcome(
                            getattr(sit, "alternative_journey", None),
                            bool(getattr(sit, "original_is_best", False)),
                            subject,
                        ),
                    )
            _collect_alerts(impact, worst_by_situation, now=now)

    # **An alternative belongs to a train, and is back-filled onto alerts about THAT train only**
    # (2026-08-12). It used to be shared across the whole trip, on the reading that "a faster way
    # from here to there" is a property of the pair — but `select_best`'s in-time tier picks the
    # latest departure that still arrives before *the disrupted train's* aimed arrival, so the answer
    # is chosen against one train's deadline and is a claim about that train. Shared across a window
    # it strands people: in prd on 2026-08-12 one journey's 00:31 cycle alerted on thirteen cancelled
    # L1 departures between 05:59 and 11:59 and offered every one of them the 05:45.
    #
    # What the back-fill still exists for is narrower and unchanged: the lookup runs on the canonical
    # ref pair only (`pair_index == 0`), so a situation whose worst reading arrives from a collapsed
    # ref — a station's bus side, a ferry berth's second registration — carries a response that was
    # asked with `with_alternative=False`. When that reading is about a train the canonical pass DID
    # look up, the whole outcome transfers: same train, same deadline, same answer. When it is about
    # a different train (which is the reason the collapsed ref is asked separately at all), nothing
    # transfers, and the honest "we did not establish that" stays.
    for alert in worst_by_situation.values():
        if alert.jkey is None or getattr(alert.impact, "alternative_subject", None) is not None:
            continue  # no train to key on, or this alert's own pass already answered for it
        known = by_train.get((alert.operator, alert.jkey))
        if known is None:
            continue
        alert.alternative_journey = known.journey
        if alert.impact is not None:
            # A copy, not an in-place write: one response object is shared by every alert it
            # produced, and `alert.impact` is what `decisions._alternative_status` and
            # `_reconcile_caches` read this train's verdict off.
            alert.impact = alert.impact.model_copy(
                update={
                    "alternative_journey": known.journey,
                    "original_is_best": known.original_is_best,
                    "alternative_subject": known.subject,
                }
            )

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
        threshold = threshold_for(sit.operator, has_situation=sit.has_situation)
        impact_p = dep.impact_probability
        alert = Alert(
            situation_number=sit.situation_number,
            summary=sit.summary,
            description=sit.description,
            operator=sit.operator,
            has_situation=sit.has_situation,
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
            alternative_journey=getattr(sit, "alternative_journey", None),
            # This situation's own lookup, projected onto the response the alert carries: the three
            # alternative fields are the only ones that differ per situation, and every reader
            # downstream (`decisions._alternative_status`, `_reconcile_caches`,
            # `llm_message.build_message_input`) reaches for them through `alert.impact`. Projecting
            # here rather than leaving them to read the response's headline is what keeps a milder
            # situation from inheriting the worst train's advice — the whole 2026-08-12 fix.
            impact=impact.model_copy(
                update={
                    "alternative_journey": getattr(sit, "alternative_journey", None),
                    "original_is_best": bool(getattr(sit, "original_is_best", False)),
                    "alternative_subject": getattr(sit, "alternative_subject", None),
                }
            ),
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


def _pending_pushes(jids, alerts: list[Alert]) -> list[Alert]:
    """The alert-worthy trains a push is still coming for, worst-first.

    This is what the writing budget is spent on.

    One entry per TRAIN (`one_alert_per_train`, the same collapse loop 2 delivers through), so the
    keys these are written under are exactly the keys the delivery loop reads back.

    "Still coming" is per REGISTRATION, because `_notified` is: two devices on one trip are notified
    separately, so a train the first device already heard about is still pending for the second
    and its wording can still change what somebody sees. A journey definition nobody is registered
    on any more yields nothing, which is correct — there is no push to write for.

    Deliberately approximate in one direction only. It does not check that each registration
    still has a live subscription (loop 2 does, and drops the device if the endpoint is gone), so
    a journey whose device has vanished can still attract a write. That over-writes at worst; the
    alternative — reading device state here — would let a stale subscription silently withhold
    the wording from a push that does go out, and a missing wording is the failure this whole
    path exists to prevent.
    """
    with _state_lock:
        notified = set(_notified)
    return [
        alert
        for alert in one_alert_per_train(alerts)
        if alert.alertworthy
        and any(f"{jid}|{alert.situation_number}" not in notified for jid in jids)
    ]


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
    returns. Loop 1 writes its wordings for the output of this same collapse
    (:func:`_pending_pushes`), so the alert that survives here is the one a message was written for
    and cached under — for every train, not just the journey's worst, which is what it was until
    2026-08-21.
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


def _train_label(mode: str | None, line: str | None, hhmm: str) -> str:
    """Which departure this is, as a departure board names it: "Tog L1 09:24".

    The line is what makes the label identify *their* train rather than *a* train: a registered
    journey routinely matches several lines at once, so "Tog kl 09:24" leaves the reader to work out
    which of them left at 09:24 — on a lock screen, in the minutes before they walk to the platform.

    One label for both departures a body can name — the traveller's own train and the alternative
    offered instead of it — so the two read alike and a reader comparing them is comparing like with
    like. That is why it takes its parts rather than reading them off the message input: they come
    from the top level for the first and from `alternativ_reise.reisevei[0]` for the second.

    `mode` is passed in RATHER THAN LOOKED UP, and its case comes with it: the first label heads its
    own line ("Tog R10 08:00: …") while the second sits mid-clause after "Alternativ", where bokmål
    wants a lower-case common noun. This function cannot know which, so `_LEG_MODE_NOUN` — used only
    for the second — carries the lower-case form and the alert's own `mode` stays as it is.

    Every part is optional, and each missing one costs only itself: `linje` is absent when the ref
    was unknown, and a leg can carry a mode this codebase has no noun for. Inventing either would be
    the one failure worse than saying too little. Note the line-less form keeps "kl" and the line
    form drops it — "Tog 09:24" alone reads as a quantity, "Tog L1 09:24" as a line and a time.
    """
    if line:
        return f"{mode} {line} {hhmm}" if mode else f"{line} {hhmm}"
    return f"{mode} kl {hhmm}" if mode else f"kl {hhmm}"


# The Journey Planner's transport modes in Norwegian. A closed set: `alternatives._TRIP_QUERY`
# asks for exactly these six (foot legs never reach a wording -- build_message_input drops them),
# so this is a complete translation rather than a best effort. Anything outside it yields no noun
# at all: "Alternativ R11 08:24" says less than it could, while a guessed noun would put the wrong
# vehicle on a lock screen, and the line and time still identify the departure without one.
#
# LOWER CASE, unlike the `mode` on the alert (2026-08-20). These are ordinary bokmål nouns, and this
# label is the only one that lands mid-clause -- always after "Alternativ", never at the start of a
# line -- so "Alternativ tog R11 08:24" is what Norwegian orthography asks for and "Alternativ Tog
# R11 08:24" reads as a proper noun. The alert's own label keeps its capital because it heads its
# line. Case therefore belongs HERE, at the one call site whose position is fixed, and not in
# `_train_label`, which cannot know where its caller will put the result. "T-bane" keeps its capital
# T in every position: it is an abbreviation, not a common noun.
_LEG_MODE_NOUN = {
    "rail": "tog",
    "bus": "buss",
    "coach": "buss",
    "tram": "trikk",
    "metro": "T-bane",
    "water": "båt",
}


def _first_leg_label(alternative: dict, hhmm: str) -> str:
    """The alternative's first transit leg, labelled like any other departure: "tog R11 08:24".

    "Alternativ kl 08:24" tells a traveller when to be on a platform but not what to board, which on
    a station with several departures in the same minute is the half of the advice they cannot
    supply themselves. The FIRST leg, because that is the one they have to catch; a later leg's line
    is a connection they will read off a board once they are moving, and naming it here would put
    two line numbers in one clause with nothing saying which to board.

    `reisevei` holds the transit legs in order, foot already excluded
    (llm_message.build_message_input), so leg zero is that first one. An empty or absent list falls
    through to the time alone — the wording this had before it named any line.
    """
    leg = next(iter(alternative.get("reisevei") or []), {})
    return _train_label(_LEG_MODE_NOUN.get(leg.get("transportmiddel")), leg.get("linje"), hhmm)


def _alternative_phrase(message_input: dict) -> str | None:
    """"Alternativ tog R11 08:24, framme kl 09:05" — the escape route, with its caveats.

    This is the wording prd actually ships (`VARSEL_LLM_ENABLED=false` there), so a caveat that
    reaches only the prompt reaches nobody. Two of them are stated rather than implied:

    * `ankommer_i_tide: false` — the alternative is framme AFTER the traveller's planned arrival.
      Routine, not exotic: since the selection rule gained its fallback tier the offer for a
      cancelled train is regularly the next possibility rather than a faster one, and a clause that
      named a departure time without saying so would tell that traveller they still make their
      appointment. Read as `is False`, not falsily: an input built before this key existed (a
      replayed decision row, `_message_input_from_alert` from an older process) says *nothing* about
      which side of the deadline this lands on, and asserting lateness from silence is a fabrication.
    * `egen_risiko` — the replacement is itself predicted to be in trouble. It is offered because
      nothing safer reached the destination in time, so it must not read as a clean escape.

    `minutter_spart` is deliberately not quoted. It is in the input only when positive (see
    `llm_message.build_message_input`), the two times already say it, and one figure per line is the
    register this template keeps.

    Same "assemble from the parts that exist" discipline as the rest of the body: the Journey Planner
    can answer without a readable departure or arrival time, and an alternative naming neither is not
    advice — it is dropped rather than rendered as "Alternativ kl None". The line and vehicle come
    from :func:`_first_leg_label` and are each optional in the same way.
    """
    alt = message_input.get("alternativ_reise") or {}
    departure, arrival = alt.get("avgang_kl"), alt.get("ankomst_kl")
    if not departure and not arrival:
        return None
    phrase = f"Alternativ {_first_leg_label(alt, departure)}" if departure else "Alternativ"
    if arrival:
        phrase += f", framme kl {arrival}" if departure else f" framme kl {arrival}"
    if alt.get("ankommer_i_tide") is False:
        phrase += " – senere enn planlagt ankomst"
    if alt.get("egen_risiko"):
        phrase += " (kan også bli rammet)"
    return phrase


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
    if message_input.get("ingen_melding"):
        # No operator has published anything about this train, so there is no text to quote and
        # `situasjoner` is empty -- without a headline of our own the body would open on whatever
        # detail line follows and read as though it were quoting somebody. It says only what the
        # warning is, not that no message stands behind it (2026-08-20): the caveat ran longer than
        # the warning, and the same shortening is why the prompt no longer asks for it either, so
        # the two writers stay comparable on this axis.
        summary = "Mulig avvik"

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
        train = _train_label(
            message_input.get("transportmiddel"), message_input.get("linje"), hhmm
        )
        detail = f"{train}: {detail}" if detail else train
    # The alternative comes LAST, after the cancellation or the delay: what has happened to the
    # traveller's own train is the news, and the replacement is the advice that follows from it.
    return "\n".join(
        part for part in (summary, detail, _alternative_phrase(message_input)) if part
    )


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


# Which /varsel variant a device installed, and the two things a notification has to carry to look
# like it. The page reskins itself from `?brand=`, but a NOTIFICATION is drawn by the OS from what
# the server sends: an unbranded install would otherwise show the Entur mark in the tray, and
# tapping it would land back on the Entur-styled page. `url` is also what the manifest's start_url
# says, so the two agree on where the app lives.
#
# DEFAULT_BRAND is what an absent or unrecognised value means, including every device registered
# before this existed — those records have no `brand` key at all.
DEFAULT_BRAND = "entur"
_BRAND_ASSETS: dict[str, dict[str, str]] = {
    "entur": {"icon": "/varsel/icons/pwa-192.png", "url": "/varsel/"},
    "none": {"icon": "/varsel/icons/pwa-192-none.png", "url": "/varsel/?brand=none"},
}


def normalize_brand(brand: str | None) -> str:
    """A client-supplied brand slug, reduced to one this server can actually draw."""
    return brand if brand in _BRAND_ASSETS else DEFAULT_BRAND


def _notification_payload(
    journey: dict,
    alert: Alert,
    message: str | None = None,
    *,
    journey_id: str | None = None,
    message_input: dict | None = None,
    brand: str = DEFAULT_BRAND,
) -> dict:
    """Norwegian Web Push payload for one journey×situation match.

    `message` is the LLM-written text when one was produced; the deterministic template is used
    otherwise, so a missing or failing message writer costs wording, never the notification. The
    title, url and situation_number are unchanged either way — the notification stays recognisable
    and the service worker's click-through keeps working.

    `message_input` is this decision's message input (see :func:`_message_input`); `None` means it
    could not be built, and the template falls back to reconstructing the little of it the wording
    needs from the alert — a push must not be lost to a payload problem.

    `brand` only ever moves the icon and the click-through url. The WORDING is deliberately
    identical across brands: it is what `/historikk` records, and a text that varied by variant
    would split every wording comparison in the decision log along a second axis.
    """
    from_name = journey.get("from_name") or journey["from_stop"]
    to_name = journey.get("to_name") or journey["to_stop"]
    assets = _BRAND_ASSETS[normalize_brand(brand)]
    url = assets["url"]
    if journey_id:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode({'journey': journey_id, 'situation': alert.situation_number})}"
    return {
        # The journey alone, no "Avvik: " prefix (2026-08-20): the app that sent it is already
        # named beside the title by the OS, the body says what the disruption is, and the two
        # words cost the stop names room on a lock screen that truncates on the right.
        "title": _tag_environment(f"{from_name} → {to_name}"),
        "body": message or _deterministic_body(message_input or _message_input_from_alert(alert)),
        "url": url,
        "icon": assets["icon"],
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


def deliver_to_devices(
    per_device: Mapping[str, list[Any]],
    prepare: Callable[[Any], tuple[dict, dict]],
) -> list[tuple[str, Any, str, dict]]:
    """Fan one batch of pending pushes out across devices, bounded by the module's worker pool.

    ``per_device`` maps device_id → that device's pending items, in send order; ``prepare`` turns
    one item into ``(subscription, payload)`` and runs on the fan-out's worker thread. Parallel
    ACROSS devices, sequential WITHIN one: each send can block for :data:`_PUSH_TIMEOUT_S` against
    a client-supplied endpoint (FCM, Mozilla, Apple), so sequential delivery would stall the
    poller for devices × 10 s in the bad case — while a "gone" result must still stop that
    device's REMAINING items this batch, which only holds if its own sends stay ordered.

    Returns ``(device_id, item, result, payload)`` flattened in ``per_device`` order; the caller
    owns what a result means — logging, dedup bookkeeping, and the gone → grace → drop rule
    (:func:`handle_gone_device`). Shared by check_and_notify's SX delivery loop and (injected by
    main, so that module imports no varsel) togvarsel_notify's forecast rounds.
    """

    def _deliver_device(entry: tuple[str, list[Any]]) -> list[tuple[str, Any, str, dict]]:
        """One device's pending items, in order, stopping at a dead subscription."""
        device_id, items = entry
        out = []
        for item in items:
            subscription, payload = prepare(item)
            result = _send_push(subscription, payload)
            out.append((device_id, item, result, payload))
            if result == "gone":
                break  # stop this device's remaining pushes this batch
        return out

    if not per_device:
        return []
    with ThreadPoolExecutor(max_workers=min(_PUSH_WORKERS, len(per_device))) as pool:
        batches = list(pool.map(_deliver_device, per_device.items()))
    return [r for batch in batches for r in batch]


def _remember_message_meta(
    message_key: tuple[str, str, str],
    *,
    status: str,
    provider: str | None = None,
    model: str | None = None,
    message_input: dict | None = None,
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
        if message_input is not None:
            # Keep the originating input with a cached wording. A later poll has different numbers
            # and must not be represented as the input that produced already-cached prose.
            meta["message_input"] = json.loads(json.dumps(message_input, ensure_ascii=False, default=str))
            try:
                import llm_message

                meta["prompt_version"] = llm_message.PROMPT_VERSION
            except (ImportError, AttributeError):
                logger.debug("Prompt version unavailable for feedback context", exc_info=True)


def _write_message(
    alert: Alert,
    *,
    journey_key: str,
    message_input: dict | None,
    outcome: dict[str, Any] | None = None,
) -> str | None:
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

    Gated on ``generation_enabled`` — "could a backend be asked at all" — and NOT on
    ``VARSEL_LLM_ENABLED``, which since 2026-08-07 says only which of the two wordings the traveller
    receives (:func:`_deliverable_message`). Writing in an environment that does not send the result
    is the point: prd runs with delivery off, and the recorded pair is the only way its own pushes
    can be compared against what the model would have said. The per-cycle budgets in
    :func:`check_and_notify` bound what that costs; nothing here widened them.

    `outcome`, when passed, is filled with ``{"status": ...}``: **why** this returned what it did,
    which the returned text alone cannot say — every failure mode here produces the same `None`.
    The caller needs it to decide which retry budget the failure should be charged to, and charging
    the wrong one is not a cosmetic mistake: it retires a writable payload for the situation's whole
    life (see :data:`_MAX_MESSAGE_ATTEMPTS`).

    An out-parameter rather than a richer return type, for exactly the reason
    `alternatives.fetch_candidates` gives for the same choice: this function is stubbed wholesale in
    25 places across the suites, and an unstubbed or legacy caller must keep the plain `str | None`
    contract. A stub that returns a string is unaffected; a stub that returns `None` leaves the
    status unset, which the caller reads as "no backend was asked" and charges to nothing — the
    conservative direction, since the alternative would be to retire subjects on a test double's
    behalf.
    """
    def note(status: str) -> None:
        if outcome is not None:
            outcome["status"] = status

    if message_input is None:
        # No backend was asked and none could be: there is nothing to prompt with. Charging a retry
        # budget for this would count the absence of an input as evidence about the model.
        note("no_input")
        return None  # _message_input already logged why
    try:
        import llm_message

        if not llm_message.generation_enabled():
            note("disabled")
            return None
        result = llm_message.write_message_detailed(
            message_input, journey_key=journey_key, situation_number=alert.situation_number
        )
        note(result.status)
        _remember_message_meta(
            _message_key(journey_key, alert),
            status=result.status,
            provider=result.provider,
            model=result.model,
            message_input=message_input,
        )
        return result.text
    except Exception:
        logger.warning(
            "Written-message step failed; falling back to the deterministic wording",
            exc_info=True,
            extra={"fields": {"event": "llm_message", "result": "error"}},
        )
        # Charged as transient, not payload. `llm_message.write_message_detailed` never raises, so
        # reaching here means the import failed, the environment is broken, or a future edit let
        # something escape — none of which is evidence that THIS input is unwritable, and all of
        # which a later cycle may find fixed.
        note("error")
        return None


def _deliverable_message(message_key: tuple[str, str, str]) -> str | None:
    """The written wording this journey × situation × departure may actually be SHOWN, or None.

    The single reader of `_messages` on the traveller's side, and the reason it is one function
    rather than a check at each call site. Since 2026-08-07 a wording is written whenever a backend
    can be tried (:func:`_write_message`), so the cache holds LLM prose in environments that have
    decided not to send it — prd, today. Two surfaces carry that text: the Web Push body
    (`_prepare_alert`) and the in-app journey card (`_journey_model`). Gating them separately is
    how they drift: the card is the one an implementer forgets, and the drift is silent, because a
    card quoting a wording the push beside it does not is not a crash and not a failing assertion.
    `llm_message.delivery_enabled()` therefore owns the answer, and both surfaces read it here.

    `None` is the state both surfaces already handle — `_notification_payload` falls back to the
    deterministic body, and `Alert.message = None` is what the PWA has always rendered before a
    message exists — so delivery-off needs no change on either side.

    NOT used by :func:`_record_decisions`: the log records what was WRITTEN, whether or not it was
    sent, which is the whole point of writing it in an environment that does not deliver it.

    Local import and a blanket swallow, the same shape as :func:`_write_message`: a deploy without
    the Vertex dependencies must still push, and this now sits on the delivery path where an
    ImportError would cost the notification itself rather than just its wording.
    """
    try:
        import llm_message

        if not llm_message.delivery_enabled():
            return None
    except Exception:
        logger.warning(
            "Could not read the message-delivery switch; sending the deterministic wording",
            exc_info=True,
            extra={"fields": {"event": "llm_message", "result": "error"}},
        )
        return None
    with _state_lock:
        return _messages.get(message_key)


def _generation_enabled() -> bool:
    """Whether a backend can be asked for a wording at all — see ``llm_message.generation_enabled``.

    Wrapped rather than called directly for the same reason as :func:`_deliverable_message`: the
    local import keeps a deploy without the Vertex dependencies pushing, and a failure here must read
    as "no wording", never as an exception on the poller thread.

    Answering False costs the cycle nothing and is the honest answer when the module cannot even be
    imported — but note the caller must not spend a retry attempt on it, because no backend was
    reached. See the give-up note in :func:`check_and_notify`.
    """
    try:
        import llm_message

        return llm_message.generation_enabled()
    except Exception:
        logger.warning(
            "Could not read the message-generation switch; using the deterministic wording",
            exc_info=True,
            extra={"fields": {"event": "llm_message", "result": "error"}},
        )
        return False


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


def _local_hhmm(value: datetime | None) -> str | None:
    """"08:24" in Europe/Oslo, or None. Mirrors `llm_message._hhmm` without importing it — that is
    the module :func:`_message_input_from_alert` is standing in for."""
    return value.astimezone(_OSLO).strftime("%H:%M") if value is not None else None


def _local_line_code(line: str | None) -> str | None:
    """"VYG:Line:L1" -> "L1". Mirrors `llm_message.line_code`, and for the same reason as
    :func:`_local_hhmm`: this runs on the path where importing that module is what failed."""
    return (line.rsplit(":", 1)[-1] or None) if line else None


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
        "avgang_kl": _local_hhmm(alert.departure_time),
        "situasjoner": [{"sammendrag": alert.summary}],
    }
    # Present only when known, exactly as build_message_input carries it -- _train_label branches on
    # the key, and the two wording sources must not disagree about whether this train has a line.
    if line := _local_line_code(alert.line):
        body["linje"] = line
    if not alert.has_situation:
        # Same correction as build_message_input makes, restated here because this path exists for
        # exactly the moment that one has failed. `alert.summary` on a no-SX alert is text this
        # codebase wrote, not an operator's; quoting it as though it were is the failure this
        # fallback would otherwise introduce on the one path with nothing left to catch it.
        body["ingen_melding"] = True
        body["situasjoner"] = []
    # BEFORE the cancellation branch, not after it. That branch returns, and a declared cancellation
    # is exactly the case where naming a replacement matters most -- appending this at the end of the
    # function would produce a push reading "Innstilt" and naming nothing to take instead. The keys
    # mirror build_message_input's, including that `minutter_spart` exists only when there is a
    # saving: the two wording sources must not disagree about whether this journey is faster.
    alt = alert.alternative_journey
    if alt is not None:
        alternative: dict[str, Any] = {
            "avgang_kl": _local_hhmm(alt.departure_time),
            "ankomst_kl": _local_hhmm(alt.arrival_time),
            "ankommer_i_tide": alt.arrives_before_original,
            "egen_risiko": alt.at_risk,
            # Same shape and same foot filter as build_message_input's, because _alternative_phrase
            # reads ONE document and takes the vehicle and line it names from leg zero. Omitting the
            # legs here would leave this path's wording saying "Alternativ kl 08:24" while the very
            # same offer, worded from a buildable input, named the train to board.
            "reisevei": [
                {"transportmiddel": leg.mode, "linje": leg.public_code or leg.line}
                for leg in (alt.legs or [])
                if leg.mode != "foot"
            ],
        }
        if alt.minutes_saved > 0:
            alternative["minutter_spart"] = alt.minutes_saved
        body["alternativ_reise"] = alternative
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

    Records **both** wordings (2026-08-07): `message_llm` is the raw cache read, `message_deterministic`
    is this cycle's template body, and `message`/`message_source` say which of the two the traveller
    actually got. Those are different questions now that ``VARSEL_LLM_ENABLED`` only selects the
    wording — prd writes one and sends the other — and the pair on one row is the only way that
    deployment's own pushes can be compared against what the model would have said.
    """
    try:
        import decisions
        import llm_message
        from llm_message import key_digest

        if not decisions.enabled():
            return
        # The GENERATION gate, not VARSEL_LLM_ENABLED: since 2026-08-07 the latter decides only
        # which wording the traveller is sent, and a row whose writer ran perfectly well must not
        # file itself as "llm_disabled" merely because prd delivers the deterministic text.
        generation_on = llm_message.generation_enabled()
        # Read ONCE for the whole cycle, like the gate above it: every row written here describes
        # the same tick's deliveries, and a flag re-read per alert could label two rows from one
        # cycle as having sent different wordings.
        delivery_on = llm_message.delivery_enabled()
        device_counts: dict[str, set[str]] = {}
        for _jid, journey in journeys:
            device_counts.setdefault(journey_key(journey), set()).add(journey["device_id"])

        rows = []
        for key, journey in by_journey_key.items():
            digest = key_digest(key)
            n_devices = len(device_counts.get(key, ()))
            for alert in alerts_by_key.get(key, []):
                message_key = _message_key(key, alert)
                # Read RAW, deliberately not through `_deliverable_message`: this is the half of the
                # pair recorded as `message_llm`, which is what was WRITTEN whether or not it was
                # sent. Since 2026-08-07 those differ by design — prd writes a wording and delivers
                # the deterministic one — and gating this read would throw away exactly the column
                # the comparison exists for. What was sent is decided below, on `delivery_on`.
                with _state_lock:
                    written = _messages.get(message_key)
                    meta = dict(_message_meta.get(message_key) or {})
                # No meta means the writer was never reached for this alert, and why is knowable
                # from the state that decided it: below the threshold, push (and with it the writer)
                # switched off, no backend available to ask — or alert-worthy but not among the
                # trains this journey wrote for this cycle (`not_selected`). Since 2026-08-21 that
                # last value is much narrower: a second situation about a train another situation
                # already covers (`one_alert_per_train` collapsed it), a train with no push pending
                # that was not the journey's worst, or one past `_MAX_MESSAGES_PER_JOURNEY`. It used
                # to mean "not the single worst situation on this journey" and so covered most of
                # the alert-worthy log -- 29 501 of dev's 33 900 alert-worthy rows over 14 days.
                status = meta.get("status")
                if status is None:
                    if not alert.alertworthy:
                        status = "not_alertworthy"
                    elif not push_on:
                        status = "push_disabled"
                    elif not generation_on:
                        # Kept as "llm_disabled" -- the value is an established column in
                        # `varsel_decisions` and rows written before 2026-08-07 use it -- but it now
                        # means "no backend could be asked" (VARSEL_LLM_PROVIDER=off, no project,
                        # every backend latched off), never "this deployment sends the template".
                        status = "llm_disabled"
                    else:
                        status = "not_selected"
                # BOTH wordings, on every row -- and `message`/`message_source` describing which one
                # was DELIVERED. The deterministic body is built unconditionally (not as a fallback)
                # from the same input `_notification_payload` words the push from, so the recorded
                # pair agrees with the push by construction rather than through a second template
                # copy; it is what dates a row as written after 2026-08-07, since a NULL `written`
                # cannot (it also means "the column did not exist yet").
                #
                # The choice is made on `delivery_on`, NEVER on "a wording exists". Since 2026-08-07
                # those are different questions: with delivery off the cache holds an LLM wording the
                # push did not carry, and `written or deterministic` would file a template push as
                # `llm` -- on the /historikk card, and permanently in `varsel_decision_outcomes`,
                # which the nightly job copies the pair into and never revises. It would also leave
                # "written but withheld" (`message_source='deterministic' AND message_llm IS NOT
                # NULL`) matching no row at all, which is the population prd's rollout is judged on.
                message_input = message_inputs.get(message_key)
                # Guarded per alert, not inside the cycle-wide try below -- the same rule
                # `_message_input` states for itself: one journey with an awkward payload must cost
                # that journey's row, not the whole cycle's decision log. This became load-bearing on
                # 2026-08-07: the wording used to be built only when no model text existed
                # (`written or _deterministic_body(...)`), and is now built for EVERY alert, so a
                # raise here reaches rows that previously skipped the call. Dropping the cycle would
                # break the log's one guarantee -- every decision, not every push -- and a miss
                # leaves no other trace.
                try:
                    deterministic = _deterministic_body(
                        message_input or _message_input_from_alert(alert)
                    )
                except Exception:
                    logger.warning(
                        "Could not word a decision for %s/%s; recording the rest of the cycle",
                        key,
                        getattr(alert, "situation_number", None),
                        exc_info=True,
                        extra={"fields": {"event": "varsel_decision_write", "result": "no_wording"}},
                    )
                    continue
                delivered = written if delivery_on else None
                message = delivered or deterministic
                rows.append(
                    decisions.build_row(
                        journey,
                        alert,
                        journey_digest=digest,
                        n_devices=n_devices,
                        pushed=(key, alert.situation_number) in pushed_keys,
                        message=message,
                        message_source="llm" if delivered else "deterministic",
                        message_llm=written,
                        message_deterministic=deterministic,
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


def device_subscription(device_id: str) -> tuple[dict, str] | None:
    """The device's push subscription and normalized brand, or None when the device is gone or
    never subscribed. Both are read under ONE take of the state lock — devices can be deleted or
    re-branded while a caller iterates a matched set, and a split read could describe one
    registration's subscription with another's icon."""
    with _state_lock:
        dev = _devices.get(device_id)
        subscription = dev.get("subscription") if dev else None
        brand = normalize_brand(dev.get("brand") if dev else None)
    if subscription is None:
        return None
    return subscription, brand


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


def handle_gone_device(device_id: str) -> bool:
    """React to a delivery's "gone" (404/410): keep the device while its subscription is inside
    the propagation grace window (see :func:`_device_is_fresh`), drop it otherwise. Returns True
    when the device was dropped — state changed, and the caller owns persisting it. Shared by the
    SX delivery loop and (injected) togvarsel_notify's rounds; the /varsel-api/test endpoint keeps
    its own gone handling because it turns the same two cases into HTTP statuses."""
    if _device_is_fresh(device_id):
        # Likely transient (subscription still propagating): keep it, retry next tick.
        logger.info(
            "Transient push failure for fresh device %s; keeping subscription",
            device_id,
        )
        return False
    _drop_device(device_id)
    return True


def _forget_message(message_key: tuple[str, str, str]) -> None:
    """Drop the cached wording for this journey × situation × departure, and its provenance with it.

    Both or neither. `_message_meta` is what :func:`_record_decisions` reports as `message_status`,
    so a message dropped without its meta files the next cycle's deterministic row as ``ok_cached`` —
    a row claiming to carry text that no longer exists.

    NEITHER failure count is cleared, deliberately — not `_message_attempts` and not
    `_message_errors`. They bound retries for a payload the writer cannot handle and for a backend
    that will not answer, and restarting either budget every time an alternative moved would re-open
    the ~720 calls-per-situation hole :data:`_MAX_MESSAGE_ATTEMPTS` was added to close. Only a
    SUCCESSFUL write clears them, which is the one event that is actual evidence about either.
    """
    with _state_lock:
        _messages.pop(message_key, None)
        _message_meta.pop(message_key, None)


def _reconcile_caches(key: str, alerts: list[Alert], now: datetime) -> None:
    """Bring `_alternatives` and `_messages` into line with this cycle's fresh match.

    Called BEFORE the "nothing is alert-worthy / push is off" early return, and over EVERY alert
    rather than the winning one. Both caches live as long as the SITUATION (up to its 12 h window)
    while the alerts underneath are re-scored every tick, and the commonest way an alternative goes
    stale is the disruption easing off until nothing crosses the gate — which takes that return and
    never reached any pop. Nothing cleared an entry at all before 2026-08-06, on the belief that an
    alternative was rare enough for a stale one not to be worth the code. That belief was never
    measured, and when it finally was (2026-08-06, 26 % of alerting subjects under the OLD rule —
    `data/queries/alternative_offer_rate.sql`) it did not hold, so this was likely already biting
    before the selection rule changed. A card could keep advising a superseded escape for hours.

    Four fresh readings, and telling them apart is the whole job:

    * **an alternative** — cache it. If it names a different journey than the cached text was written
      from, that text advises a train we no longer recommend, so it goes with it (a written message
      now quotes a specific departure time).
    * **not alert-worthy** — the situation eased off below the gate. There is no advice to carry, so
      neither cache keeps any: the alert still shows on the card, and it must not show last hour's
      wording and escape route beside this tick's mild numbers.
    * **"stay on your own train"** — a VERDICT (`ImpactResponse.original_is_best`, new with the
      2026-08-06 selection rule): the selection ran, over a real pool, and chose the traveller's own
      train. It supersedes a cached alternative and any wording built on one.
    * **nothing established** — no lookup ran for this train this cycle. A collapsed stop ref is
      asked without one on purpose (`match_journey`'s `pair_index == 0`), and the impact gate, a
      Journey Planner outage or the fetch budget can each skip it. Silence is not a verdict: the
      cached answer is kept, or the card would flap between offering an alternative and offering
      none on alternate ticks. The one exception is an alternative that has itself departed — worse
      than none, since it advises catching a train that has already left.

    The two middle cases are distinguishable only because `original_is_best` is carried separately
    from `alternative_journey`; a null alternative alone means both "we chose your train" and "we did
    not look", and clearing on the second is the flap.
    """
    with _state_lock:
        for alert in alerts:
            message_key = _message_key(key, alert)
            if not alert.alertworthy:
                _alternatives.pop(message_key, None)
                _forget_message(message_key)
                continue
            fresh = alert.alternative_journey
            if fresh is not None:
                cached = _alternatives.get(message_key)
                # Stored unconditionally (the card should show this tick's `at_risk` and this tick's
                # scoring of the replacement), but the wording is only dropped when the advice
                # itself changed -- see _alternative_identity.
                if cached is None or _alternative_identity(cached) != _alternative_identity(fresh):
                    _forget_message(message_key)
                _alternatives[message_key] = fresh
                continue
            # getattr, like every other read off the impact response in this module: it is duck-typed
            # (a synthetic provider need not carry the newest field), and this runs on the ingestion
            # thread outside the per-journey guard, where an AttributeError would cost the cycle.
            chose_original = bool(getattr(alert.impact, "original_is_best", False))
            # A verdict supersedes the cached answer outright; silence does not, and only a cached
            # alternative that has itself departed is dropped without one.
            superseded = chose_original or not _still_catchable(_alternatives.get(message_key), now)
            if superseded and _alternatives.pop(message_key, None) is not None:
                _forget_message(message_key)


class _WriteBudget:
    """This cycle's ceiling on written messages, claimable from several threads at once.

    Two limits behind one `claim()`, because a caller must not be able to satisfy one and ignore the
    other: a count (:data:`_MAX_LLM_CALLS_PER_CYCLE`, a policy choice about how much of a cycle's
    demand deserves prose) and a wall clock (:data:`_MAX_LLM_SECONDS_PER_CYCLE`, the ceiling on how
    long the ingestion thread may spend here).

    **The clock is RESERVED, not checked.** `monotonic() >= deadline` is a pre-check: a call passing
    it with a millisecond to spare still runs its full worst case afterwards, so the pass overshoots
    by however long one call can take -- measured at ~46 s against a documented 30 s ceiling before
    that was fixed. Reserving :data:`_WORST_CASE_CALL_S` up front makes the deadline a ceiling on
    when the pass ENDS. Concurrency does not weaken that: every thread reserves its own worst case
    before starting, so whatever is in flight when the deadline nears was already paid for.

    It is exactly this reservation that made the count safe to raise from 8 to 64. Sequentially the
    two limits were the same limit wearing different clothes -- 8 calls at ~2.2 s is ~18 s of a 30 s
    budget -- and raising the count alone would simply have moved the binding constraint to the
    clock, one capped call at a time. Across 16 workers they are genuinely independent.

    `refused` counts what the ceiling cost, so the one summary line per cycle can report it. Each
    refused train is also recorded durably as `message_status = 'capped'` on its own decision row,
    which is where the question is actually answered; the log line is for noticing.
    """

    def __init__(self, max_calls: int, deadline: float) -> None:
        self._lock = threading.Lock()
        self._max_calls = max_calls
        self._deadline = deadline
        self.claimed = 0
        self.refused = 0

    def claim(self) -> bool:
        """Reserve one call, or refuse. Never blocks for longer than the dict operations here."""
        with self._lock:
            if self.claimed >= self._max_calls or monotonic() + _WORST_CASE_CALL_S >= self._deadline:
                self.refused += 1
                return False
            self.claimed += 1
            return True


def check_and_notify(by_situation: dict[str, list]) -> None:
    """Send a push for every registered journey × newly alert-worthy situation.

    Called at the end of main's ingestion-change rebuild with the freshly-refreshed per-call
    snapshot (used here only to prune ``_notified``; the matching itself goes through ``/impact``).

    Two loops, and the split between them is load-bearing:

    1. **Compute, keyed by journey definition.** Journeys are grouped by :func:`journey_key`, so two
       devices registered on the same trip drive one ``/impact`` call and one written message per
       disrupted train between them — and therefore receive identical text.
    2. **Deliver, keyed by registration.** ``_notified`` stays keyed on ``journey_id`` so each
       device is notified once per situation. Collapsing dedup onto the journey key would mean the
       first device notified silently suppresses the push for everyone else on the same trip.

    Written messages are one per TRAIN a push is still coming for, since that is the grain loop 2
    delivers at (:func:`_pending_pushes`), bounded by :data:`_MAX_MESSAGES_PER_JOURNEY` and by
    :data:`_MAX_LLM_CALLS_PER_CYCLE` for the cycle. Trains beyond either cap get the deterministic
    wording this tick rather than delaying the poller thread.

    **Scoring and recording run even when push is disabled**; only delivery is gated. The decision
    log's guarantee is "every decision, not every push", and an early return here quietly broke it:
    an environment without VAPID keys recorded nothing at all, so /historikk looked like a broken
    deployment rather than an un-provisioned one. The LLM stays gated on push, though — writing
    traveller-facing prose nobody can receive at all is spend with no reader, and the deterministic
    body already captures what the decision was.

    **Writing a wording and sending it are separate switches** (2026-08-07). A message is written
    whenever a backend can be tried (``llm_message.generation_enabled``); ``VARSEL_LLM_ENABLED``
    decides only which of the two wordings the traveller receives, on both surfaces, through
    :func:`_deliverable_message`. So prd — delivery off — writes the model's version, pushes the
    deterministic one, and records both, which is the only way the two can be compared on prd's own
    traffic instead of on dev's. Note "gated on push" above is still true and is the wider gate:
    push off means no write at all, delivery off means write but send the template.
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
        for key in [k for k in _message_errors if k[1] not in live]:
            _message_errors.pop(key, None)
        for key in [k for k in _message_meta if k[1] not in live]:
            _message_meta.pop(key, None)
        changed = bool(stale)

    # ---- Loop 1: one impact call per journey definition, one written message per train ---------
    by_journey_key: dict[str, dict] = {}
    # Every registration behind each journey definition. `_notified` is keyed per journey_id
    # (loop 2 notifies each device once), so asking "is a push still coming for this train?" --
    # which is what decides where the writing budget goes -- needs the ids, not just the
    # definition they collapse to.
    jids_by_key: dict[str, list[str]] = {}
    for _jid, journey in journeys:
        _key = journey_key(journey)
        by_journey_key.setdefault(_key, journey)
        jids_by_key.setdefault(_key, []).append(_jid)

    alerts_by_key: dict[str, list[Alert]] = {}
    # What every message about this cycle's decisions is written from, one per journey definition x
    # situation. Built here, before any wording exists, so the prompt, the deterministic fallback and
    # the recorded row all read the same document rather than three separate builds of it. Local to
    # the cycle: unlike `_messages` it describes this tick's numbers and must not outlive them.
    message_inputs: dict[tuple[str, str, str], dict | None] = {}
    # Wall-clock budget, not just a call count -- see _WriteBudget for why the deadline is reserved
    # rather than checked. This is the ingestion thread: every second here is a second /disruptions,
    # /journey, /demo and /reisevalg serve staler data, and loop 2's pushes wait for loop 1.
    budget = _WriteBudget(_MAX_LLM_CALLS_PER_CYCLE, monotonic() + _MAX_LLM_SECONDS_PER_CYCLE)
    # One instant for the whole cycle, so "has this alternative departed?" cannot be answered
    # differently for two journeys scored a few seconds apart.
    cycle_now = datetime.now(UTC)

    # ---- Pass 1a: match every journey, CONCURRENTLY -------------------------------------------
    #
    # `match_journey(with_alternative=True)` is the poller's other piece of blocking external I/O:
    # up to `impact._MAX_ALTERNATIVE_LOOKUPS` Journey Planner calls at ~3 s each, per journey. Run
    # end to end that is the whole `alternatives._POLLER_FETCH_MAX_PER_MIN` ceiling x 3 s of stalled
    # ingestion in the bad case, which at ten times today's registrations is minutes, not seconds.
    # Same reasoning as loop 2's push fan-out, and the threads are equally nearly free -- each is
    # parked in a socket read.
    #
    # Safe to run concurrently because `match_journey` mutates nothing global: it builds its own
    # `worst_by_situation`/`by_train`, reads the stop-alias map and the prediction cache (both
    # rebuilt earlier on THIS thread, before check_and_notify is called), and reaches the outside
    # world only through `alternatives`, which has its own lock, its own single-flight and its own
    # fetch budget. What the fan-out did change there is that single-flight now has concurrent
    # callers for the first time, and it had to learn to WAIT rather than shrug -- see
    # `alternatives._KeyedTTLCache.get`'s `wait_for_refresh`. Without that half, journeys sharing a
    # stop pair would each be told there is no alternative, which is the regression a fan-out is
    # expected to cause and would have shipped silently.
    def _match(item: tuple[str, dict]) -> tuple[str, list[Alert]]:
        key, journey = item
        try:
            return key, match_journey(journey, with_alternative=True)
        except Exception:
            # One journey's failure must not stop the remaining journeys being scored or any push
            # going out. Empty alerts for this key; the next tick retries. Caught INSIDE the worker
            # so it stays this journey's failure -- an exception escaping into `pool.map` surfaces
            # at the point the results are consumed and would take the whole cycle with it.
            logger.warning(
                "Scoring failed for journey %s; skipping it this cycle",
                key,
                exc_info=True,
                extra={"fields": {"event": "varsel_match_error"}},
            )
            return key, []

    matched: list[tuple[str, list[Alert]]] = []
    if by_journey_key:
        with ThreadPoolExecutor(max_workers=min(_MATCH_WORKERS, len(by_journey_key))) as pool:
            matched = list(pool.map(_match, list(by_journey_key.items())))

    # ---- Pass 1b: reconcile, and decide what to write for -- SEQUENTIALLY ----------------------
    #
    # Deliberately not folded into the pass above. Everything here mutates module state
    # (`_alternatives`, `_messages`, `_message_attempts`, `_message_meta`) or reads it to make a
    # decision, and doing that on one thread is worth far more than the microseconds it costs: none
    # of it blocks on anything. The split is behaviour-preserving -- `_reconcile_caches` for one
    # journey never reads another journey's alerts, and `_pending_pushes` reads `_notified`, which
    # only loop 2 writes.
    write_queue: list[tuple[str, Alert, tuple[str, str, str], int, int]] = []
    for key, alerts in matched:
        journey = by_journey_key[key]
        alerts_by_key[key] = alerts
        for alert in alerts:
            message_inputs[_message_key(key, alert)] = _message_input(journey, alert)
        # ABOVE the early return, and over every alert: a superseded alternative most often goes
        # stale precisely when nothing is alert-worthy any more, which is the case that returns.
        # This is also what records a freshly found alternative, whether or not a message follows
        # it (cap reached, LLM disabled, push off) -- the in-app list wants it either way.
        #
        # Guarded like the match above it, and for a strictly bigger blast radius than the
        # 2026-07-29 incident that guard exists for: called bare, a raise here unwinds out of
        # check_and_notify entirely, so loop 2 never runs (no pushes at all this cycle) and
        # _record_decisions never runs (no rows either). What it touches is duck-typed -- an impact
        # response from an injected provider, a cached AlternativeJourney, an aware/naive datetime
        # comparison -- so "it cannot raise" is an assumption about somebody else's object. Keeping
        # one cycle's stale cache is the smaller failure by a wide margin.
        try:
            _reconcile_caches(key, alerts, cycle_now)
        except Exception:
            logger.warning(
                "Cache reconciliation failed for journey %s; keeping this cycle's cached "
                "alternatives and messages",
                key,
                exc_info=True,
                extra={"fields": {"event": "varsel_reconcile_error"}},
            )
        if not push_on:
            # No message writing without push: the text exists to be delivered, and the decision is
            # recorded with the deterministic wording either way.
            continue
        # ONE WORDING PER TRAIN THIS CYCLE WILL PUSH ABOUT, not one per journey (2026-08-21).
        #
        # Loop 2 delivers per train -- `one_alert_per_train`, the same collapse applied here --
        # while this loop wrote for `next(a for a in alerts if a.alertworthy)` alone. So a journey
        # with several disrupted departures pushed the model's prose for its worst train and the
        # template for every other one, recorded as `message_status = 'not_selected'`: 604 of dev's
        # 844 pushes over 14 days, 83 % of everything that reached a lock screen.
        #
        # `_pending_pushes` and not simply "every alert-worthy train", which the measurement says
        # would be the expensive way to fix the cheap half of the problem. Per journey-cycle on dev:
        # alert-worthy trains run p50 3 / p90 12 / max 22, while PUSHES run p90 1 / max 18 and 3 610
        # of 4 099 cycles push nothing at all -- the trains are alert-worthy again every tick, but
        # `_notified` means each one reaches a phone once. Writing for all of them would spend a
        # bounded budget re-wording trains nobody will be told about again, and starve the one train
        # that is about to be.
        #
        # The fallback keeps today's behaviour where it was right: with no push pending, the worst
        # train is still written for, because the in-app journey card reads these same wordings
        # through `_deliverable_message` and would otherwise decay to the template as soon as
        # `_reconcile_caches` forgot one. Same cost as before -- one call -- in the common cycle.
        pending = _pending_pushes(jids_by_key.get(key, ()), alerts)
        subjects = (pending or [a for a in one_alert_per_train(alerts) if a.alertworthy][:1])[
            :_MAX_MESSAGES_PER_JOURNEY
        ]
        for subject in subjects:
            message_key = _message_key(key, subject)
            with _state_lock:
                # Read RAW, deliberately not through `_deliverable_message`: this asks whether a
                # wording was already WRITTEN for this situation, which is a generation-side
                # question. Gating it on delivery would make the writer re-run every cycle wherever
                # delivery is off (prd today) — burning the cycle budget re-writing text that is
                # already cached.
                already_written = message_key in _messages
                attempts = _message_attempts.get(message_key, 0)
                errors = _message_errors.get(message_key, 0)
            # BEFORE the attempts check, and that ordering is the fix for a real give-up bug. A
            # `None` from `_write_message` costs an attempt (see pass 1c), but several of its paths
            # never call a backend at all — this one, where no backend can be asked. So three cycles
            # with generation off used to exhaust a situation permanently: when the 30-minute
            # `llm_message._LATCH_TTL` expired and a backend came back, the situation was already at
            # `_MAX_MESSAGE_ATTEMPTS` and was never offered to the writer again for its whole 12 h
            # life. That thins exactly the written-but-withheld population prd's rollout is judged
            # on.
            if not _generation_enabled():
                _remember_message_meta(message_key, status="llm_disabled")
                continue
            if already_written:
                # Reused from an earlier cycle: keep who wrote it, but say that this cycle's row
                # carries text written against an earlier cycle's payload, not the one recorded
                # beside it.
                _remember_message_meta(message_key, status="ok_cached")
                continue
            if attempts >= _MAX_MESSAGE_ATTEMPTS:
                # Without this the retry never stops. A write that fails for a payload-dependent
                # reason (a refusal, an over-long reply, an unparseable one) fails again on every 60
                # s tick for the situation's whole life -- ~720 Vertex calls that can change
                # nothing, because the push already went out with the deterministic wording, each
                # one burning a slot from _MAX_LLM_CALLS_PER_CYCLE that a train which WOULD have
                # succeeded now cannot use.
                _remember_message_meta(message_key, status="attempts_exhausted")
                continue
            if errors >= _MAX_TRANSIENT_ATTEMPTS:
                # The same stop, on the other budget and with a different meaning: the backend has
                # been unreachable for this subject a dozen times running. Recorded under its own
                # status so the two never have to be told apart by guesswork -- `attempts_exhausted`
                # is a verdict on the payload, `transient_exhausted` is a verdict on the morning,
                # and a rate computed across both measures neither.
                _remember_message_meta(message_key, status="transient_exhausted")
                continue
            write_queue.append((key, subject, message_key, attempts, errors))

    # SOONEST DEPARTURE FIRST, ACROSS JOURNEYS -- and this ordering is new (2026-08-21), because
    # until the budget could actually bind across journeys there was nothing to order. The queue
    # used to be walked in `_journeys` insertion order, so when the cap was reached the trains that
    # got prose were whichever journeys happened to be registered earliest, every cycle, forever.
    # That is invisible while ~44 registrations rarely reach a cap of 8 and indefensible at ten
    # times that: the same handful of travellers would take the whole budget on every busy morning.
    # Soonest-first is the rule `impact._MAX_ALTERNATIVE_LOOKUPS` already spends its own budget by,
    # and it is the one that matches what the wording is FOR -- a train leaving in six minutes can
    # still be acted on, one leaving in three hours will be written for again on a later cycle.
    #
    # `.timestamp()` rather than the datetime itself: the key must be total, and comparing an aware
    # datetime with a naive one raises. Trains with no departure time sort last and keep their
    # relative order (Python's sort is stable), which is `_pending_pushes`' severity order.
    write_queue.sort(
        key=lambda item: (
            item[1].departure_time is None,
            item[1].departure_time.timestamp() if item[1].departure_time else 0.0,
        )
    )

    # ---- Pass 1c: write the wordings, CONCURRENTLY --------------------------------------------
    #
    # The pass this whole restructure exists for. A Vertex call is ~2.2 s and up to
    # `_MAX_LLM_CALLS_PER_CYCLE` of them run per cycle; sequentially that is the difference between
    # a budget of 8 (~18 s, all of the ceiling) and one of 64 (~140 s, absurd). Across
    # `_LLM_WORKERS` it is ~9 s.
    def _write_one(item: tuple[str, Alert, tuple[str, str, str], int, int]) -> None:
        key, subject, message_key, attempts, errors = item
        # Claimed here rather than filtered above, so the ceiling is spent on calls that actually
        # START. A pre-filter would hand the budget to the first N in the queue whether or not the
        # clock still allowed them.
        if not budget.claim():
            _remember_message_meta(message_key, status="capped")
            return
        # _write_message catches Exception itself, so this is belt and braces for a future edit
        # that lets one escape. It charges a retry budget rather than returning past the counters, so
        # a raiser is subject to the same give-up rule as a None. Delivery is unaffected either way:
        # loop 2 pushes independently and falls back to the deterministic wording.
        outcome: dict[str, Any] = {}
        try:
            message = _write_message(
                subject,
                journey_key=key,
                message_input=message_inputs.get(message_key),
                outcome=outcome,
            )
        except Exception:
            logger.warning(
                "Written-message step raised for %s; using the deterministic wording",
                key,
                exc_info=True,
                extra={"fields": {"event": "llm_message", "result": "error"}},
            )
            message = None
            outcome["status"] = "error"
            _remember_message_meta(message_key, status="error")
        # WHICH BUDGET A FAILURE IS CHARGED TO -- the 2026-08-22 fix. Every `None` used to charge
        # `_message_attempts`, so three bad minutes at Vertex retired a subject's wording for the
        # situation's whole 12 h life. In prd that was not a corner case: of the subjects that ever
        # reached `attempts_exhausted` in the 14 days to 2026-08-21, the statuses preceding it were
        # 290 `error` and ZERO `rejected`/`ungrounded`, while 55 % of all subjects that hit an error
        # went on to succeed on a later cycle. The counter was, in practice, doing the exact opposite
        # of its job.
        #
        # Three outcomes, three treatments, and the third is the one worth naming: a status in
        # NEITHER set means no backend was asked (`disabled`, `unavailable`, no input, or a stub in
        # a test). Nothing was spent and nothing was learned, so it charges nothing -- and it must
        # not fall through to a default, which is how this was wrong before.
        status = outcome.get("status")
        with _state_lock:
            if message:
                _messages[message_key] = message
                # Both budgets, because a success says the payload is writable AND the backend is
                # reachable. Leaving the error count standing would let a subject that recovers
                # still be retired by errors it has demonstrably outlived.
                _message_attempts.pop(message_key, None)
                _message_errors.pop(message_key, None)
            elif status in _PAYLOAD_FAILURES:
                _message_attempts[message_key] = attempts + 1
            elif status in _TRANSIENT_FAILURES:
                _message_errors[message_key] = errors + 1

    if write_queue:
        with ThreadPoolExecutor(max_workers=min(_LLM_WORKERS, len(write_queue))) as pool:
            list(pool.map(_write_one, write_queue))
    if budget.refused:
        # ONCE per cycle, after the fact, and reporting what the ceiling actually cost rather than
        # which journey happened to hit it first -- with the queue sorted and the calls concurrent,
        # "from journey X onwards" would name an arbitrary thread's arbitrary moment.
        logger.info(
            "Written-message budget spent this cycle: %d written, %d trains capped (%d calls / "
            "%.0fs ceiling); the capped trains take the deterministic wording",
            budget.claimed,
            budget.refused,
            _MAX_LLM_CALLS_PER_CYCLE,
            _MAX_LLM_SECONDS_PER_CYCLE,
            extra={
                "fields": {
                    "event": "llm_message",
                    "result": "capped",
                    "written": budget.claimed,
                    "capped": budget.refused,
                }
            },
        )

    # ---- Loop 2: deliver per registration, deduped per device ---------------------------------
    # Collect first, then fan out through deliver_to_devices -- parallel ACROSS devices, sequential
    # WITHIN one; the worker/timeout rationale lives on that helper, which the togvarsel rounds
    # share. One broad situation produces one send per registration in a single cycle.
    per_device: dict[str, list[tuple[str, dict, dict, Alert, str, str]]] = {}
    for jid, journey in journeys if push_on else []:
        device = None
        brand = DEFAULT_BRAND
        with _state_lock:
            if jid not in _journeys:  # deleted while iterating
                continue
            dev = _devices.get(journey["device_id"])
            if dev is not None:
                device = dev.get("subscription")
                brand = normalize_brand(dev.get("brand"))
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
                (jid, device, journey, alert, key, brand)
            )

    def _prepare_alert(item: tuple[str, dict, dict, Alert, str, str]) -> tuple[dict, dict]:
        """One pending alert → (subscription, payload), run on the fan-out's worker thread."""
        jid, device, journey, alert, key, brand = item
        message_key = _message_key(key, alert)
        # The written wording only if this deployment delivers it; otherwise None, and
        # _notification_payload words the push from the same message input the model was shown.
        message = _deliverable_message(message_key)
        payload = _notification_payload(
            journey,
            alert,
            message,
            journey_id=jid,
            message_input=message_inputs.get(message_key),
            brand=brand,
        )
        return device, payload

    pushed_keys: set[tuple[str, str]] = set()
    if per_device:
        for _device_id, item, result, payload in deliver_to_devices(per_device, _prepare_alert):
            jid, _device, journey, alert, _key, _brand = item
            _log_push("alert", result, journey["device_id"], payload, journey_id=jid, alert=alert)
            if result == "sent":
                with _state_lock:
                    _notified.add(f"{jid}|{alert.situation_number}")
                pushed_keys.add((journey_key(journey), alert.situation_number))
                changed = True
            elif result == "gone":
                if handle_gone_device(journey["device_id"]):
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
    feedback_enabled: bool = Field(
        default=False, description="Whether per-alert usefulness feedback is available."
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
    brand: str | None = Field(
        default=None,
        max_length=24,
        description=(
            "Which /varsel variant this install came from ('entur', 'none'). Decides the icon on "
            "the notification and where tapping it lands. Optional: absent — an older PWA build, "
            "or a device registered before brands existed — means 'entur'."
        ),
    )

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
        feedback_enabled=feedback.enabled(),
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
            # Refreshed on every registration, so a device that reopens the app under a different
            # ?brand= has its notifications follow it rather than keeping the brand it first used.
            "brand": normalize_brand(body.brand),
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
    cached text also means the in-app list says exactly what the push said — which since 2026-08-07
    requires reading it through :func:`_deliverable_message`, because the cache now holds wordings
    this deployment may have decided not to deliver.

    `alerts` lets the caller supply an already-scored result, so a device with several registrations
    of the same journey definition runs the matcher once rather than once per registration.
    """
    key = journey_key(rec)
    # One card per train, not per SX message -- see one_alert_per_train. Collapsed here rather than
    # in match_journey because the matcher also feeds the decision log, which must keep every
    # situation. `alerts` may be shared across several registrations of the same definition, so this
    # builds a new list and never mutates the caller's.
    alerts = one_alert_per_train(match_journey(rec) if alerts is None else alerts)
    feedback_available = feedback.enabled()
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
            # Through the delivery gate, exactly as the push body is: the card must never quote a
            # wording the notification beside it did not carry. Only the message is gated -- the
            # alternative below is advice the traveller gets either way, worded by whichever
            # template or model wrote the text above it.
            alert.message = _deliverable_message(cache_key)
            alt = _alternatives.get(cache_key)
            # An alternative that has itself departed is worse than none: it advises catching a
            # train that has already left. The boundary lives in one place (_still_catchable) --
            # this read used `>=` while the selection rule pools on `>`.
            if _still_catchable(alt, now):
                alert.alternative_journey = alt
            if feedback_available:
                meta = dict(_message_meta.get(cache_key) or {})
                source = "llm" if alert.message is not None else "summary"
                originating_input = meta.get("message_input") if source == "llm" else None
                obs = alert.latest_observation
                snapshot = {
                    "display_source": source,
                    "displayed_text": alert.message if alert.message is not None else alert.summary,
                    "detail_text": _feedback_detail(alert),
                    "observation_text": _feedback_observation(obs),
                    "situation_number": alert.situation_number,
                    "operator": alert.operator,
                    "line": alert.line,
                    "departure_time": alert.departure_time.isoformat() if alert.departure_time else None,
                    "journey_context": {
                        "from_stop": rec["from_stop"], "from_name": rec.get("from_name"),
                        "to_stop": rec["to_stop"], "to_name": rec.get("to_name"),
                    },
                    "message_input": originating_input,
                    "llm_provider": meta.get("provider") if source == "llm" else None,
                    "llm_model": meta.get("model") if source == "llm" else None,
                    "prompt_version": meta.get("prompt_version") if source == "llm" else None,
                    "message_status": meta.get("status"),
                    "prediction": {
                        "cancelled": alert.cancelled, "cancel_probability": alert.cancel_probability,
                        "impact_probability": alert.impact_probability, "ge10_probability": alert.ge10_probability,
                        "delay_p50_min": alert.delay_p50_min, "delay_p90_min": alert.delay_p90_min,
                    },
                    "policy": {"alert_threshold": alert.alert_threshold, "alertworthy": alert.alertworthy},
                    "delivery": {"message_source": source},
                }
                alert.feedback_context_id = feedback_contexts.mint(snapshot)
                alert.feedback_enabled = alert.feedback_context_id is not None
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


def _feedback_detail(alert: Alert) -> str:
    """Literal detail line mirrored from the current React renderer."""
    parts: list[str] = []
    if alert.departure_time:
        parts.append(
            _train_label(
                alert.mode,
                _local_line_code(alert.line),
                _local_hhmm(alert.departure_time) or "",
            )
        )
    if alert.cancelled:
        parts.append("Innstilt")
    elif alert.cancel_probability >= _CANCEL_LIKELY:
        # JavaScript Math.round (used by the card) rounds non-negative .5 values upward; Python's
        # round uses ties-to-even and would make the persisted "exact" text disagree at 50.5 %.
        parts.append(f"{int(alert.cancel_probability * 100 + 0.5)} % sjanse for innstilling")
    elif delay := _delay_phrase(
        {
            "forsinkelse_min": {
                "typisk": alert.delay_p50_min,
                "verstefall": alert.delay_p90_min,
            }
        }
    ):
        parts.append(delay)
    return " · ".join(parts)


def _feedback_observation(obs: LatestObservation | None) -> str | None:
    if obs is None:
        return None
    stop = obs.at_stop_name or obs.at_stop
    if not stop:
        return None
    state = "innstilt" if obs.cancelled else (f"{obs.delayed_min} min forsinket" if obs.delayed_min else "i rute")
    when = f" (kl {obs.observed_at.astimezone(_OSLO):%H:%M})" if obs.observed_at else ""
    return f"Sist observert: {stop}, {state}{when}"


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


@router.post("/feedback", summary="Record isolated usefulness feedback for one displayed alert")
async def submit_feedback(request: Request) -> dict:
    """Read and bound the raw body before JSON decoding; downstream I/O stays off the event loop."""
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > feedback._MAX_BODY:
                raise HTTPException(status_code=413, detail="Payload too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > feedback._MAX_BODY:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks.append(chunk)
    try:
        body = feedback.parse(b"".join(chunks))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid feedback") from None
    return await run_in_threadpool(
        feedback.submit, body, environment=environment_label() or "prd"
    )


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
    # Subscription and brand under one lock take (see device_subscription), so the test push
    # cannot describe one registration's subscription with another's icon.
    resolved = device_subscription(body.device_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Enheten har ingen aktiv varsel-abonnement.")
    subscription, brand = resolved
    assets = _BRAND_ASSETS[brand]
    payload = {
        "title": _tag_environment("Varsler er aktivert"),
        "body": "Du vil nå få beskjed når et avvik kan ramme en av dine reiser.",
        "url": assets["url"],
        "icon": assets["icon"],
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
