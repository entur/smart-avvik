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
  The in-app list shows every match an operator PUBLISHED regardless, flagged with
  ``alertworthy``; a match no SX message named is shown only once it crosses that gate
  (:func:`_card_alerts`), since a sub-threshold one carries no operator text to read.
* **What the push says** — which train (``notification_wording.train_label``: "Tog L1 09:24", the
  line included because a journey matches several of them and a time alone does not say which),
  then the *alerting* quantity in ``/demo``'s wording: P(innstilling eller ≥ 10 min forsinkelse),
  then whichever driver dominates (likely cancellation, or the exceedance-stack delay interval).
  Never the raw ``dep_delay_typ_min`` regression: for today's alert-worthy boardings its median is
  2 min against the stack p50's 15 min (rail), so quoting it made a 93 %-impact alert read as
  "1 % sjanse for innstilling, typisk 2 min forsinket". An alternative, when there is one, closes
  the body with the same label over its FIRST transit leg ("Alternativ tog R11 08:24") — the
  departure the traveller has to catch, as opposed to a connection further along it.
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
import traceback
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from time import monotonic
from typing import Annotated, Any, Protocol
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import alternatives  # the Journey Planner fetch-path vocabulary the matcher forwards
import feedback
import feedback_contexts
import metrics  # sibling leaf module (no main import): the push-delivery counter (KTD11)
import notification_events  # the webhook channel's typed hand-off type and its emit step
import notification_wording  # the shared wording seam; `_LockedPassBudget` subclasses its budget
import reserved_budget  # the one reserve-don't-check pass budget
import varsel_state  # where a registration lives: the seam and today's blob backend (U13)

# The alert gate moved out to its own module on 2026-08-06 so `impact` can reach it too (importing
# it back from here would be a cycle — see alert_policy's docstring). Bound at module level rather
# than reached through `alert_policy.`, so `varsel.threshold_for` / `varsel.TROUBLE_DELAY_MIN` keep
# resolving for the callers that already read them off this module. TROUBLE_DELAY_MIN is such a
# re-export and nothing else: it is /demo's realized-outcome bar, not something varsel reads.
#
# `gate` arrived on 2026-09-08 (U1) and is the one that matters for this module: it is
# `threshold_for` plus the comparison, stated once, so this module's browser adapter and the
# client adapter being built beside it cannot come to disagree about what a threshold means. The
# per-operator table and the no-SX cut are unchanged, which is why `alert_threshold` still reads
# exactly what it always did on every existing decision row.
from alert_policy import (  # noqa: F401
    TROUBLE_DELAY_MIN,
    gate,
    min_threshold,
    threshold_for,
)
from alternatives import AlternativeJourney
from deployment import environment_label
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import (
    Path as PathParam,  # aliased: `Path` here is pathlib's (see _STATE_PATH)
)
from impact import AlternativeOutcome, ImpactResponse, LatestObservation, severity_key

# The WORDING moved out on 2026-09-08 (U1) so a second notification surface can produce the same
# Norwegian text without importing this module -- 147 KB of poller state, the device registry, the
# VAPID keypair and the decision-log writer, none of which a notification event may reach (R16). Bound
# at module level under their old names, not reached through `notification_wording.`, so every
# caller that already reads `varsel._deterministic_body` off this module keeps resolving -- `main`
# injects it into `/meldinger`, and two suites assert push bodies against it. A monkeypatch of one
# of these still reaches `_notification_payload`, which resolves the module global at call time.
from notification_wording import delay_phrase as _delay_phrase
from notification_wording import deterministic_body as _deterministic_body
from notification_wording import mode_label
from notification_wording import notification_title as _notification_title
from notification_wording import train_label as _train_label
from notification_wording import train_label_from_input as _train_label_from_input
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

# `mode_label` and its rail-codespace table moved to `notification_wording` with the wording that
# reads them (2026-09-08): a notification event needs the vehicle noun and must not import this module
# for it. Re-exported above, so `varsel.mode_label` -- which `main` calls twice -- still resolves.


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
# Written messages per PLACE PAIR per cycle -- per JOURNEY DEFINITION until KTD9 (2026-09-12), and
# the name is kept because it is referenced from CLAUDE.md, the operations document and several
# suites. The cycle cap above bounds the poller thread; this one stops a single corridor spending
# all of it. Per definition it multiplied by the number of registrations on that corridor, which is
# precisely the number the 100 000-commuter plan scales: five hundred registrations between Asker
# and Oslo S could queue fifteen hundred wordings for the same handful of trains.
#
# A corridor routinely has more disrupted trains than either budget can cover -- measured on dev
# over 4 099 journey-cycles: p50 3 alert-worthy trains, p90 12, max 22 -- but the trains that matter
# are the ones about to be PUSHED, and those are few (p90 1, max 18; 88 % of cycles push nothing).
# Sized like _MAX_ALTERNATIVE_LOOKUPS and for the same reason: three covers the overwhelming
# majority (39 of 489 pushing cycles push more than three) at a bounded worst case. The slots go to
# the SOONEST departures on the pair; trains past it take the deterministic wording and are recorded
# `not_selected`, so what the cap costs stays countable rather than being inferred from a log line.
_MAX_MESSAGES_PER_JOURNEY = 3
# At most this many alert pushes per REGISTRATION per Oslo day (design review 2026-09-01, finding
# 15: six notifications in one day for three journeys, before the service had a single real user).
# A line suspension otherwise pushes once per cancelled train in the window -- 3-5 in ~27 % of cases
# (the ET-relay multiplicity the team chose to watch on 2026-08-19) -- and fatigue is the one failure
# a notification channel cannot recover from: the traveller mutes it, and the push that mattered goes
# with the rest. Four rather than fewer because a long registered window (07:00-22:00 is common)
# legitimately spans two rushes and the cap is per registration, not per rush. Spent SOONEST
# DEPARTURE FIRST within a cycle, so the trains that can still be acted on win. A push that REPLACES
# an earlier one about the same train (same notification tag, see `_notification_tag`) is exempt: it
# adds nothing to the tray. A capped alert still reaches the decision log (`pushed=False`) and is
# re-offered every cycle, so the cap suppresses delivery and nothing else.
_MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY = 4
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

    `path` (2026-09-10) names which Journey Planner budget the provider's alternative lookups spend
    (`alternatives.FetchPath`); `_impact` sends it on EVERY call, defaulted to the poller's. CI runs
    no type checker, so a provider that accepts the keyword and drops it still satisfies this — the
    pin for that is a test asking the pull ledger whether it was charged
    (`test_varsel.test_the_real_provider_charges_the_pull_ledger`).
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
        path: alternatives.FetchPath = ...,
    ) -> ImpactResponse: ...


class ConnectivityProvider(Protocol):
    """main's "does a planned trip run between these stops today" lookup.

    A Protocol for the same reason as :class:`ImpactProvider`: it is a cross-module contract, and the
    consequence of drift is quiet. A TypeError here would be caught by the registration path's own
    guard and every journey would register unchecked, which looks exactly like a healthy deploy.

    Returns (from→to runs, to→from runs), or None when the question could not be answered — an
    outage, a timeout, the rate limit. None is not "no service".

    ``path`` names whose per-minute budget the question spends. The provider owns the vocabulary and
    the ceilings (`main._CONNECTIVITY_POLLER_PATH` and its neighbours); this module only forwards
    the tag it was handed, so an unrecognised one is charged the public budget rather than minting a
    new one. It is keyword-only and defaulted so a caller that does not care — and every existing
    one — is unchanged.
    """

    def __call__(
        self, from_refs: Sequence[str], to_refs: Sequence[str], *, path: str = ...
    ) -> tuple[bool, bool] | None: ...


class BatchConnectivityProvider(Protocol):
    """The same question about MANY stop pairs, in one job (U10).

    Answers are POSITIONAL -- one per input pair, in order -- which is what lets the caller pair
    each verdict with its own record without this module knowing what a pair is for. A provider
    that answered a different number of them would stamp routes with other routes' answers, so
    every caller checks the length rather than trusting it.

    ``None`` in any position is "could not be established" and is never "no service", exactly as on
    the single-pair form.

    **It takes no ``path``**, unlike the single-pair form. Both of its callers are poller sweeps, so
    whose budget they spend is a property of the wiring rather than of the call, and `main.lifespan`
    binds it once. A per-call tag would be a vocabulary this module does not own, forwarded by two
    functions that have no reason to disagree about it.
    """

    def __call__(
        self, pairs: Sequence[tuple[Sequence[str], Sequence[str]]]
    ) -> Sequence[tuple[bool, bool] | None]: ...


@dataclass(frozen=True)
class WebhookRecipient:
    """One webhook subscription as this pass sees it: a registration whose channel is a webhook.

    KTD3's whole shape in three fields. It is a **view merged at match time, never a
    materialization** — nothing here is ever written into :data:`_journeys`, :data:`_notified` or
    the GCS state blob, which is what makes a rollback to pre-feature code safe (`_save_state`
    writes known keys, so `_state_extra`'s round-trip could not have protected notification state that
    had leaked into the blob).

    ``journey`` carries exactly the five fields the shared work reads off a registration —
    ``from_stop``, ``to_stop``, ``begin``, ``end``, ``days`` — because those are what
    :func:`journey_key`, :func:`journey_windows` and :func:`_impact` consult, and
    ``llm_message.build_message_input`` resolves the two place NAMES from the stop registry rather
    than from the record.

    ``delivered`` is this channel's counterpart of :data:`_notified` at the channel's OWN grain: the
    ``alert_id`` of every (subscription, train) already delivered to THIS subscription, which is the
    grain the store mints events at and therefore the grain :func:`_pending_pushes` asks its
    question at through :func:`_webhook_pending`. Not the browser ledger's per-situation grain
    (2026-09-10): a second train under a delivered situation is a new event here. Firestore is the
    only dedup ledger for this channel; this is a read-only snapshot of it.

    **The four routing fields ride along, and until 2026-09-08 they deliberately did not** — the
    docstring here said a client document's destination and quotas "deliberately do not enter this
    pass", which was right while nothing in the process emitted an event. What changed the answer is
    ``subscription_version``, and specifically WHY the emit transaction reads it: it exists so a
    slow pass cannot commit obsolete work (``notification_store._subscription_obsolete``), which only
    means anything if the version compared is **the one the match was computed against**. Re-reading
    it at emit time would compare the stored value with itself — always equal, never refusing — and
    an alert about a train matched for a route the client has since amended would go out stamped
    with the new version. So the version comes from the load snapshot, and the three ids beside it
    come with it rather than earning a second store read each.

    What has NOT changed is the invariant KTD3 rests on: none of these ever reaches
    :data:`_journeys`, :data:`_notified`, the GCS state blob or a log line. They live in this
    cycle's locals, are handed to the emit step as a return value, and are gone with the pass.
    ``external_ref`` — the client's own opaque handle on a traveller — is carried for the event
    envelope and is the one field R16 keeps out of even a structured log field.
    """

    recipient_id: str
    journey: dict
    delivered: frozenset[str] = frozenset()
    # Appended after `delivered` on purpose: three suites construct this positionally, and every
    # existing caller must keep working.
    client_id: str = ""
    destination_id: str = ""
    subscription_version: int = 0
    external_ref: str = ""
    test: bool = False


class WebhookRecipientProvider(Protocol):
    """The webhook subscriptions this cycle should match, as RAW documents.

    A Protocol for the same reason as :class:`ImpactProvider`: it is a cross-module contract whose
    drift is quiet. A webhook recipient that silently stops being matched looks exactly like a
    healthy deployment with nothing disrupted, which is the failure mode this whole surface exists
    to avoid.

    Raw documents rather than parsed records, deliberately. R20 wants an unreadable document
    skipped rather than failing its collection, and only the reader can decide what "unreadable"
    means for the schema the *running binary* knows: a store adapter written against a newer
    document shape must be able to hand this pass something it refuses, one document at a time. So
    the provider's job is to read the store and this module's job is to validate
    (:func:`_parse_webhook_recipient`).

    Returning a lazy iterable was fine and expected while this was a Firestore cursor, and the two
    ceilings around it — a wall-clock wait and a document count — bounded the paging as well as the
    first call. **Since U6 the provider serves the store's in-memory mirror**, so it returns
    immediately, both ceilings are gone, and what bounds the STORE is one step earlier
    (`notification_store.sync_mirrors`), in its own phase. A provider that still reaches a socket
    must therefore carry its own deadline (KTD6): nothing in this pass bounds it any more.
    """

    def __call__(self) -> Iterable[Mapping[str, Any]]: ...


_impact_provider: ImpactProvider | None = None
_stops_provider: Callable[[], list[dict]] | None = None
_connectivity_provider: ConnectivityProvider | None = None
_connectivity_batch_provider: BatchConnectivityProvider | None = None
_togvarsel_notify_provider: Callable[[], bool] | None = None
# The webhook channel's two seams, both defaulting to "no webhook recipients" so an un-wired
# deployment -- which is every deployment until the store lands -- behaves byte for byte as it did
# before this existed. `_webhook_enabled` is main's flag read, injected rather than read here for
# `_togvarsel_notify_enabled`'s reason: one function is the seam, and it is consulted at CALL time
# so an environment change needs no import-time dance.
_webhook_enabled: Callable[[], bool] | None = None
_webhook_provider: WebhookRecipientProvider | None = None
# The tracing seam (R16). `main` injects `tracing_setup.span`; absent, every sub-pass span is a
# `nullcontext` and this module behaves exactly as it did before the seam existed. Injected rather
# than imported for the reason every other seam here is: `varsel` must import neither `main` nor
# the app's tracing wiring, and a module-level `import tracing_setup` would also run that module's
# import-time `setup()` inside every suite that imports varsel.
_span_provider: Callable[..., Any] | None = None
# The liveness seam (U2). `main` injects its `_beat`; absent, the passes below beat nothing and this
# module behaves exactly as it did before the seam existed. Same injection reasoning as the span
# above, plus one of its own: `_beat` moves BOTH of main's clocks (the monotonic one /health reads
# and the epoch gauge the runbook pages on), and a copy of that rule here would be the second place
# it had to stay true.
_beat_provider: Callable[[], None] | None = None
# The journey-alerts read's warm seam (U15). `main` injects `notification_pull.warm_figures`;
# absent, pass 1a is byte-for-byte what it was and the read computes on every miss, exactly as it
# did before the seam existed. Injected rather than imported, and that is a PLAN STOP CONDITION
# rather than a preference: `notification_pull` imports this module, so an import the other way is
# a cycle, and `test_varsel_api.test_varsel_does_not_import_the_pull_read` pins it.
# `varsel` therefore knows nothing about figures, generations or caches -- it hands over the one
# thing only pass 1a holds, this definition's alerts, and the read decides what to keep.
_figures_warmer: Callable[[str, dict, list["Alert"], datetime], None] | None = None
#: How many definitions the match pass may get through between heartbeats, and likewise how many
#: wordings the writing pass may. Small enough that a chunk cannot itself outlast
#: `HEALTH_STALE_AFTER_S` (600 s) — a definition's match is bounded by the Journey Planner's own
#: timeout and a wording by `llm_message`'s, so 64 of either is seconds, not minutes — and large
#: enough that the beat is not a per-definition function call on the hot path.
_HEARTBEAT_EVERY = 64
# Collapsed stop ref -> the primary ref for the same physical place (see set_stop_aliases). Empty
# until the first stop-list rebuild, and stays empty if the stop registry cannot be read — in which
# case every ref is its own place, exactly as before this map existed.
_stop_aliases: dict[str, str] = {}


def configure(
    *,
    impact_provider: ImpactProvider,
    stops_provider: Callable[[], list[dict]],
    registrations: varsel_state.RegistrationStore | None = None,
    connectivity_provider: ConnectivityProvider | None = None,
    connectivity_batch_provider: BatchConnectivityProvider | None = None,
    togvarsel_notify_provider: Callable[[], bool] | None = None,
    webhook_enabled: Callable[[], bool] | None = None,
    webhook_recipients_provider: WebhookRecipientProvider | None = None,
    span: Callable[..., Any] | None = None,
    beat: Callable[[], None] | None = None,
    figures_warmer: Callable[[str, dict, list["Alert"], datetime], None] | None = None,
) -> None:
    """Wire in main.py's /impact provider, stop list and planned-service check (once, at startup).

    `connectivity_provider` is optional: without it, registrations are simply not checked, which is
    what every deploy did before the check existed. `connectivity_batch_provider` is the same
    question asked about many stop pairs at once, and the sweeps use only that one -- absent, they
    establish nothing rather than falling back to a loop, because a loop is precisely the cost U10
    removed and a silent fallback to it would read in the logs as a healthy pass. `togvarsel_notify_provider` is main's
    TOGVARSEL_NOTIFY_ENABLED read, surfaced through /config so the PWA can announce the rush
    forecasts only where they are actually delivered; absent reads as off.

    `webhook_enabled` and `webhook_recipients_provider` are the webhook channel (KTD3). Both are
    optional and both default to off, which is not laziness about defaults: an un-wired deployment
    must be indistinguishable from the code before the channel existed, and it is the pair being
    absent — not a flag value — that guarantees it. The flag is injected separately from the store
    so the pass can decline to touch the store at all, which is what makes "the flag is unset, so
    nothing was read" a property of this module rather than of whoever wrote the provider.

    `span` is main's `tracing_setup.span` (R16), and it is a CALLABLE for the same reason as the
    two above rather than an import: this module must depend on neither `main` nor the app's
    tracing wiring. Absent, :func:`_span` yields a `nullcontext` and the pass is byte-for-byte what
    it was. Whatever is injected must accept `(name, attributes)` and honour the closed-vocabulary
    attribute rule — `tracing_setup.safe_attributes` is where that is enforced.

    `beat` is main's `_beat` (U2), injected for the same reason and with the same absent-is-a-no-op
    default. `/health`'s staleness rule means "no STEP has finished in `HEALTH_STALE_AFTER_S`", and
    this whole pass is ONE `_phase` — so without it a pass that merely got slow reads as a wedged
    poller, and the restart that follows comes up with both planned-call tables cold. See
    :data:`_HEARTBEAT_EVERY`.

    `figures_warmer` is `notification_pull.warm_figures` (U15), injected for the reason the two
    above are and for one that is stronger: `notification_pull` imports THIS module, so an import
    the other way is a cycle, and the direction is a plan stop condition pinned both ways. Absent,
    pass 1a is byte-for-byte what it was.

    `registrations` is where a registration LIVES (U13) — `varsel_state.BlobRegistrations` today,
    the Firestore-backed implementation next. Absent keeps the module's own blob backend, which is
    what every suite and every deployment before the cutover gets: the seam is proved by this
    module reaching its state only through it, not by a deployment having chosen a backend. Which
    one `main` hands over is the cutover's decision and is deliberately not a flag read here.
    """
    global _registrations
    global _impact_provider, _stops_provider, _connectivity_provider, _togvarsel_notify_provider
    global _connectivity_batch_provider
    global _webhook_enabled, _webhook_provider, _span_provider, _beat_provider, _figures_warmer
    _impact_provider = impact_provider
    _stops_provider = stops_provider
    if registrations is not None:
        _registrations = registrations
    _connectivity_provider = connectivity_provider
    _connectivity_batch_provider = connectivity_batch_provider
    _togvarsel_notify_provider = togvarsel_notify_provider
    _webhook_enabled = webhook_enabled
    _webhook_provider = webhook_recipients_provider
    _span_provider = span
    _beat_provider = beat
    _figures_warmer = figures_warmer


def _span(name: str, **attributes: Any):
    """One sub-pass span, or a no-op when nothing was injected.

    **Counts and timings only, and only the names in `tracing_setup.SPAN_ATTRIBUTE_NAMES`.** A span
    reaches Cloud Trace, where project IAM is the only gate, and these open exactly where the stop
    refs, place names, departure times and wordings are — so no attribute may carry one. The filter
    lives in `tracing_setup.safe_attributes`; what lives here is the discipline at the call site,
    and `test_tracing_setup` reads this file's own source to keep it.

    A broken provider degrades to no span rather than taking the cycle: this runs on the ingestion
    thread, where an instrumentation exception would cost a cycle's alerting. Only construction is
    guarded — `__enter__`/`__exit__` are the OTel API's, which does not raise from instrumentation.
    """
    provider = _span_provider
    if provider is None:
        return nullcontext()
    try:
        return provider(name, attributes)
    except Exception:  # noqa: BLE001 - instrumentation must never fail the pass it observes
        return nullcontext()


def _heartbeat() -> None:
    """Tell the poller's liveness seam that this pass is still making progress, or do nothing.

    Guarded for exactly `_span`'s reason and with a worse failure: this runs on the ingestion
    thread, so a raising seam would cost a whole cycle's pushes to report that the cycle is alive.
    """
    provider = _beat_provider
    if provider is None:
        return
    try:
        provider()
    except Exception:  # a liveness signal must never end the pass it reports on
        logger.warning("Heartbeat seam failed; continuing the pass", exc_info=True)


def _drain(results: Iterator[Any], total: int) -> list[Any]:
    """Consume a pool's results, beating the heartbeat every `_HEARTBEAT_EVERY` of them.

    `ThreadPoolExecutor.map` submits everything at once and yields in SUBMISSION order, so this
    beats as the pass progresses rather than as workers happen to finish — and a single item that
    never returns still blocks, which is correct: a pass wedged on one definition IS the state
    `/health` exists to catch. What it stops is the other reading, where a pass that is merely long
    (a Journey Planner outage holding `_MATCH_WORKERS` threads at their timeout, a bad morning at
    100 000 subscriptions) is mistaken for a wedged poller and restarted — see
    :data:`_HEARTBEAT_EVERY` and `main._beat`.
    """
    drained: list[Any] = []
    for item in results:
        drained.append(item)
        if len(drained) % _HEARTBEAT_EVERY == 0 and len(drained) < total:
            _heartbeat()
    return drained


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
        for jid, rec in _registrations.all_journeys():
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
            # Through the seam rather than into the record: `all_journeys` hands out copies, because
            # the store backend cannot hand out a live document and an in-place edit would work on
            # one backend and vanish on the other.
            _registrations.set_canonical_stops(jid, from_stop=new_from, to_stop=new_to)
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


# ---- Webhook channel: subscriptions as registrations with a webhook ---------------------------
#
# **This pass no longer bounds the read, because there is no read to bound** (U6). It used to cap
# the provider at 2 000 documents, on the reasoning that a pilot is one client and a few hundred
# subscriptions, so anything approaching the cap is a store bug rather than growth. At 100 000 it is
# growth, and a cap would silently match the first 2 000 -- so the cap is gone and the store holds
# the collection in a mirror it seeds once. What replaced the ceiling as the thing to watch is the
# mirror's readiness and watermark age, reported by the sync step that runs before this pass.


def webhook_channel_enabled() -> bool:
    """Is the webhook channel live in this deployment? Read at CALL time, never latched.

    Two things have to be true, and they are deliberately separate: main's flag (unset = off, the
    `_togvarsel_notify_enabled` convention) and a store to read. Either missing means the channel
    contributes nothing, and a cycle then reduces to exactly the pass that ran before the channel
    existed — which is the property the whole feature's rollback story rests on.
    """
    if _webhook_provider is None or _webhook_enabled is None:
        return False
    try:
        return bool(_webhook_enabled())
    except Exception:
        # A flag reader that raises must read as OFF, like every other gate in this repo: the
        # failure direction of a delivery switch is closed (llm_message.py argues it at length).
        logger.warning(
            "Webhook channel flag could not be read; treating the channel as off this cycle",
            exc_info=True,
            extra={"fields": {"event": "varsel_webhook_load", "result": "flag_error"}},
        )
        return False


class UnroutableChannel(ValueError):
    """A subscription document this pass is not the delivery path for (U5).

    Its own class rather than a bare `ValueError` because it is not an UNREADABLE document and must
    not be counted as one: the collection holds `web_push` documents from U12 onwards, and folding
    them into the skip counter would put a warning naming tens of thousands of perfectly healthy
    documents on every cycle -- which is how a real warning stops being read.
    """


def _parse_webhook_recipient(doc: Mapping[str, Any]) -> WebhookRecipient:
    """One raw client document → a :class:`WebhookRecipient`, raising on anything unreadable.

    Restates :class:`JourneyIn`'s three window rules (HH:MM, a non-empty subset of 0..6, and
    ``begin < end`` so overnight windows are refused) rather than importing that model, for two
    reasons that both matter. `JourneyIn` requires a ``device_id``, which a webhook subscription by
    definition does not have; and this runs on the ingestion thread against documents written by a
    possibly NEWER binary, so it must validate the fields the pass actually reads and be
    indifferent to fields it does not — a model whose required set grows would start refusing every
    document in the collection on the deploy that adds a field.
    ``test_the_webhook_loader_refuses_exactly_what_the_registration_api_refuses`` pins the two
    against each other so the restatement cannot drift into being looser.

    **The stop refs are canonicalized here** (:func:`canonical_stop`), and that is not a tidy-up.
    `_canonicalize_journeys` only ever walks `_journeys`, so a client document naming a ref the
    stop registry has since collapsed onto a primary would produce a DIFFERENT `journey_key` than
    the browser registration for the identical physical trip — two groups, two /impact calls, two
    wordings, and AE1's whole claim quietly false. Same defect as the 2026-08-03 picker bug, one
    layer out.

    Raises rather than returning None: the caller's per-document guard is what turns a bad document
    into a skipped document, and a sentinel return would let a future edit forget to check it.
    """
    # FIRST, before any field this pass matches on. The store's query already filters the channel,
    # so this is the second of two independent statements and is meant to be: U6 replaces that query
    # with a mirror that filters in Python, and from U12 the collection holds a `web_push` document
    # per /varsel registration -- at which point "the load path forgot the filter" is a defect that
    # delivers a traveller's own journey to a client's webhook. Missing reads as `webhook`, which is
    # what every document written before the field meant.
    channel = str(doc.get("channel") or "webhook")
    if channel != "webhook":
        raise UnroutableChannel(f"channel {channel!r} is not delivered by this pass")
    recipient_id = str(doc["recipient_id"])
    if not recipient_id:
        raise ValueError("recipient_id must be a non-empty string")
    from_stop = canonical_stop(str(doc["from_stop"]))
    to_stop = canonical_stop(str(doc["to_stop"]))
    if not from_stop or not to_stop:
        raise ValueError("from_stop and to_stop must be non-empty")
    if from_stop == to_stop:
        raise ValueError("from_stop and to_stop must differ")
    begin, end = str(doc["begin"]), str(doc["end"])
    if not _HHMM_RE.match(begin) or not _HHMM_RE.match(end):
        raise ValueError("begin/end must be HH:MM (24h)")
    if _parse_hhmm(begin) >= _parse_hhmm(end):
        raise ValueError("begin must be before end (overnight windows are not supported)")
    days = sorted({int(d) for d in doc["days"]})
    if not days or any(d < 0 or d > 6 for d in days):
        raise ValueError("days must be a non-empty subset of 0..6 (0=Monday)")
    return WebhookRecipient(
        recipient_id=recipient_id,
        journey={
            "from_stop": from_stop,
            "to_stop": to_stop,
            "begin": begin,
            "end": end,
            "days": days,
        },
        delivered=frozenset(str(s) for s in doc.get("delivered") or ()),
        # Read PERMISSIVELY, unlike everything above it, and the asymmetry is the point. The fields
        # above are what the pass MATCHES on: a document that cannot supply them cannot be matched
        # at all, so it is refused and skipped. These four are what the pass ROUTES with, and a
        # document missing one can still be matched — it still collapses onto its journey key, still
        # shares the one /impact call and the one wording AE1 asserts. Refusing it would take a
        # browser registration's own wording away over a client field, so it is matched and simply
        # not emitted for: `notification_events.train_ref` and `_webhook_observations` drop a subject that
        # names no destination, and the count is logged.
        client_id=str(doc.get("client_id") or ""),
        destination_id=str(doc.get("destination_id") or ""),
        subscription_version=int(doc.get("version") or 0),
        external_ref=str(doc.get("external_ref") or ""),
        test=bool(doc.get("test")),
    )


def _load_webhook_recipients() -> list[WebhookRecipient]:
    """This cycle's webhook subscriptions, or an empty list — the pass's one guarded read.

    The ONE place this pass reads notification state (KTD6): passes 1a and 1b work off the snapshot it
    returns and never go back to the store, which is what keeps 1b free to stay sequential (it
    mutates module state and blocks on nothing).

    Three failure shapes, three treatments, and the middle one is what a naive version gets wrong:

    * **the store raises** — this cycle has no webhook recipients, logged once, and the browser
      half of the pass is untouched. Guarded in the shape of the `_reconcile_caches` guard below,
      and for a strictly bigger blast radius: called bare, a raise here unwinds out of
      `check_and_notify` entirely, so loop 2 never runs (no pushes at all) and `_record_decisions`
      never runs (no rows either).
    * **the provider is slow** — it is not, since U6. The provider reads the store's in-memory
      mirror, so there is no socket in this path and nothing to time out: the bounded worker
      thread that used to wrap it, and the abandoned-thread trade that came with it, are gone with
      the per-cycle Firestore read they existed to bound. What bounds the STORE is now one step
      earlier and its own phase (`notification_store.sync_mirrors`), where a slow call costs the
      mirror its freshness rather than costing this pass its cycle.
    * **one document is unreadable** — skipped, per document, never allowed to fail its collection
      (R20). The count and the exception CLASS are logged and the document body never is (R16): a
      webhook subscription is a route with times, and this log line is not the place for it.
    """
    if not webhook_channel_enabled():
        return []
    provider = _webhook_provider
    if provider is None:  # narrowed for the type checker; webhook_channel_enabled already said so
        return []
    started = monotonic()
    try:
        docs = list(provider())
    except Exception:
        logger.warning(
            "Webhook subscriptions could not be read this cycle; matching browser registrations "
            "only",
            exc_info=True,
            extra={"fields": {"event": "varsel_webhook_load", "result": "error"}},
        )
        return []

    recipients: list[WebhookRecipient] = []
    skipped: dict[str, int] = {}
    unroutable = 0
    for doc in docs:
        try:
            recipients.append(_parse_webhook_recipient(doc))
        except UnroutableChannel:
            # Not a skip and not a warning: another channel's document, correctly declined. Counted
            # so "where did the other 90 000 go?" has an answer on the load record.
            unroutable += 1
        except Exception as exc:
            # The traceback at DEBUG, the COUNT at WARNING below. Never the document: a client
            # subscription is a route with times and weekdays, and R16 keeps that out of routine
            # logs -- which a per-document warning quoting the record would defeat. The exception
            # messages reachable here are either a missing key name or one of this function's own
            # sentences, so the traceback is safe.
            logger.debug("Unreadable webhook subscription document; skipping it", exc_info=True)
            skipped[type(exc).__name__] = skipped.get(type(exc).__name__, 0) + 1
    if skipped:
        logger.warning(
            "Skipped %d unreadable webhook subscription document(s) this cycle (%s); the rest of "
            "the collection was matched",
            sum(skipped.values()),
            ", ".join(f"{name} x{n}" for name, n in sorted(skipped.items())),
            extra={
                "fields": {
                    "event": "varsel_webhook_load",
                    "result": "skipped",
                    "skipped": sum(skipped.values()),
                }
            },
        )
    logger.info(
        "Loaded %d webhook subscription(s) in %.0f ms (%d skipped, %d on another channel)",
        len(recipients),
        (monotonic() - started) * 1000,
        sum(skipped.values()),
        unroutable,
        extra={
            "fields": {
                "event": "varsel_webhook_load",
                "result": "ok",
                "n": len(recipients),
                "skipped": sum(skipped.values()),
                "unroutable": unroutable,
                "ms": int((monotonic() - started) * 1000),
                # `truncated` is GONE with the ceiling it reported (U6). There is no per-cycle read
                # to clip: the mirror holds the whole collection, so "did this cycle see all of it?"
                # is answered by the mirror's own readiness and watermark age instead, which the
                # sync step reports.
                "mirror_size": len(docs),
            }
        },
    )
    return recipients


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
#: How many flagged journeys one cycle may re-check, and therefore how many stop pairs the one
#: connectivity job may be asked about. `notification_servability._MAX_WRITES_PER_CYCLE`'s number and
#: its reason: at the measured ~296 s cycle period it carries ~145 000 permanently-flagged
#: registrations at the six-hourly cadence above, which is above the population this is sized for.
#: Before U10 there was no bound at all -- one BigQuery job per flagged journey, serially, on the
#: ingestion thread -- which was invisible at a pilot's few hundred and is a stalled cycle at a
#: hundred thousand.
_MAX_SERVICE_RECHECKS_PER_CYCLE = 2000
#: The seam's own ceiling on one question (`main._CONNECTIVITY_TIMEOUT_S`), restated because this
#: module may not import `main` and pinned equal by `test_poller_refresh`.
_WORST_CASE_SERVICE_CHECK_S = 20.0
#: The step's clock, RESERVED rather than measured -- `_WriteBudget`'s shape one function over -- so
#: it bounds when the step ENDS and not when its question starts. One question at the worst case
#: plus the state save that follows it; there is no per-record store write here, because these
#: records live in this process's own state blob.
_SERVICE_RECHECK_DEADLINE_S = 30.0


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


def _service_verdict(
    from_stop: str, to_stop: str, *, path: str | None = None
) -> tuple[bool, bool] | None:
    """Ask the injected provider about every ref of both places. None when unavailable.

    ``path`` is forwarded to the provider only when a caller names one, and that is not a style
    choice: a provider is an INJECTED seam, several suites supply two-positional fakes, and this
    function swallows every exception into ``None``. Passing ``path=`` unconditionally would turn
    each of those fakes into a silent "could not establish" — which is the exact failure this seam
    exists to avoid, arriving with a green test suite. `notification_api` and `notification_servability` are
    wired with a partial that names the client budget (see `main.lifespan`).
    """
    if _connectivity_provider is None:
        return None
    try:
        if path is None:
            return _connectivity_provider(place_refs(from_stop), place_refs(to_stop))
        return _connectivity_provider(place_refs(from_stop), place_refs(to_stop), path=path)
    except Exception:
        # Same rule as the provider's own failure path: an unanswerable question never blocks a
        # registration, and never becomes a warning either. The pair rides as a digest and never as
        # two refs (U10): these two ARE a registered traveller's own journey, and this record is
        # the last of the four carriers that put one in Cloud Logging.
        logger.warning(
            "Planned-service check raised for pair %s; registering without it",
            alternatives.pair_digest(from_stop, to_stop),
            exc_info=True,
            extra={
                "fields": {
                    "event": "varsel_service_check",
                    "result": "error",
                    "pair_digest": alternatives.pair_digest(from_stop, to_stop),
                }
            },
        )
        return None


def _service_verdicts(pairs: Sequence[tuple[str, str]]) -> list[tuple[bool, bool] | None]:
    """One question about many stop pairs, each expanded to every ref of both places.

    The batch form of :func:`_service_verdict` and the seam both servability sweeps run on, so the
    caveat a traveller is shown at registration and the answer a sweep stores six hours later stay
    the same question. Answers are positional; a provider that answers a different number of them
    discards the whole batch rather than pairing verdicts with the wrong routes.

    Whose per-minute budget the job spends is bound into the provider at wiring time -- see
    :class:`BatchConnectivityProvider`.
    """
    if _connectivity_batch_provider is None:
        return [None] * len(pairs)
    expanded = [(place_refs(from_stop), place_refs(to_stop)) for from_stop, to_stop in pairs]
    try:
        answers = list(_connectivity_batch_provider(expanded))
    except Exception:
        logger.warning(
            "Planned-service check raised for %d stop pair(s); every stored answer stands",
            len(pairs),
            exc_info=True,
            extra={
                "fields": {
                    "event": "varsel_service_check",
                    "result": "error",
                    "asked": len(pairs),
                }
            },
        )
        return [None] * len(pairs)
    if len(answers) != len(pairs):
        logger.warning(
            "The planned-service provider answered %d of %d stop pairs; the batch is discarded",
            len(answers),
            len(pairs),
            extra={
                "fields": {
                    "event": "varsel_service_check",
                    "result": "misaligned",
                    "asked": len(pairs),
                    "answers": len(answers),
                }
            },
        )
        return [None] * len(pairs)
    return answers


def recheck_service() -> None:
    """Re-ask about journeys flagged as having no direct service, and clear the ones that now do.

    Called from the poller's refresh cycle. Bounded by design: only flagged journeys are re-asked,
    and each at most once per :data:`_SERVICE_RECHECK`, so this costs nothing on a healthy list.

    **One question per cycle since U10, not one per journey.** It used to issue a serial BigQuery
    job per flagged journey on the ingestion thread, with no per-cycle bound at all: invisible at a
    pilot's few hundred registrations and a stalled cycle at a hundred thousand. The distinct stop
    pairs are derived from the journeys this cycle will actually answer for and put to one job
    (`_service_verdicts`), and each answer fans back out to every journey on that pair -- so a
    corridor with five hundred registrations is one place in one query.

    The bound is on JOURNEYS (:data:`_MAX_SERVICE_RECHECKS_PER_CYCLE`), spent oldest-check-first
    with a missing stamp counting as the oldest there is. Deriving the pairs from the whole due set
    instead would spend an answer on journeys this cycle is not going to stamp, which leaves them
    due anyway.
    """
    now = datetime.now(UTC)
    started = monotonic()
    with _state_lock:
        due = [
            (jid, rec["from_stop"], rec["to_stop"], rec.get("service_checked_ts") or "")
            for jid, rec in _registrations.all_journeys()
            if rec.get("no_direct_service") and _service_check_due(rec, now)
        ]
    if not due:
        return
    # ISO-8601 sorts chronologically as a string, and an absent stamp is the empty string, which
    # sorts before every timestamp -- so "oldest first, never-checked first" is one key.
    due.sort(key=lambda entry: entry[3])
    admitted = due[:_MAX_SERVICE_RECHECKS_PER_CYCLE]
    capped = len(due) - len(admitted)
    if monotonic() + _WORST_CASE_SERVICE_CHECK_S > started + _SERVICE_RECHECK_DEADLINE_S:
        # RESERVED, not measured: a question that cannot finish inside the budget must not start,
        # or the step ends one whole worst case past it with the poller's cycle behind it.
        logger.warning(
            "The planned-service re-check had no room to ask this cycle; %d journey(s) stay due",
            len(due),
            extra={
                "fields": {"event": "varsel_service_check", "result": "deadline", "due": len(due)}
            },
        )
        return
    pairs = list(dict.fromkeys((from_stop, to_stop) for _jid, from_stop, to_stop, _ts in admitted))
    verdicts = {
        pair: answer
        for pair, answer in zip(pairs, _service_verdicts(pairs), strict=True)
        if answer is not None
    }
    cleared = 0
    stamped = 0
    with _state_lock:
        for jid, from_stop, to_stop, _ts in admitted:
            verdict = verdicts.get((from_stop, to_stop))
            if verdict is None:
                continue  # nothing established: the stored value AND its stamp are left alone
            if not _registrations.stamp_service_check(
                jid, checked_ts=_now_iso(), cleared=verdict[0], reverse_only=verdict[1]
            ):
                continue  # deleted while we were asking
            stamped += 1
            cleared += 1 if verdict[0] else 0
        if stamped:
            # ONE save for the whole sweep, inside the lock that made the edits. It used to save
            # only when something cleared, which left every stamp of an unchanged `false` in memory
            # until some other writer happened to persist -- so a restart re-asked them all.
            _save_state()
    fields = {
        "event": "varsel_service_check",
        "result": "capped" if capped else "swept",
        "due": len(due),
        "asked": len(pairs),
        "established": len(verdicts),
        "cleared": cleared,
        "stamped": stamped,
        "capped": capped,
        "ms": int((monotonic() - started) * 1000),
    }
    if capped:
        logger.warning(
            "The planned-service re-check hit its per-cycle bound: %d of %d due journeys answered",
            stamped,
            len(due),
            extra={"fields": fields},
        )
    elif cleared:
        logger.info(
            "Planned service appeared for %d journey(s); warning cleared",
            cleared,
            extra={"fields": fields},
        )
    else:
        logger.debug(
            "Planned-service re-check: %d of %d due journeys answered",
            stamped,
            len(due),
            extra={"fields": fields},
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
    *,
    path: alternatives.FetchPath = alternatives._POLLER_PATH,
) -> ImpactResponse | None:
    """One /impact call for a journey's boarding window, or None when unconfigured.

    `from_stop`/`to_stop` override the journey's own refs so :func:`match_journey` can ask about the
    other refs of the same physical place (see :func:`place_refs`).

    `path` is forwarded on every call and defaults to the poller's budget, which is what every
    caller before 2026-09-10 spent (`main._varsel_impact` hard-coded it); the journey-alert pull
    read passes its own so a burst of reads cannot starve the push cycle's reserved share.

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
        path=path,
    )


def _pair(journey: Mapping[str, Any]) -> tuple[str, str]:
    """The place pair a journey definition runs over — the unit the shared work is keyed on (KTD9).

    Both refs are already canonical: `create_journey` canonicalizes what a client submits
    (`canonical_stop`), and `_parse_webhook_recipient` does the same for a subscription document,
    so two registrations of one corridor cannot land on two pairs because one of them named a
    collapsed ref.
    """
    return (journey["from_stop"], journey["to_stop"])


def _decision_key(key: str, alert: "Alert") -> tuple[str, str, str]:
    """The DEFINITION-grained subject: (journey definition, situation, departure).

    This was `_message_key` until 2026-09-12, when the wording caches moved to the place pair
    (KTD9). What stayed behind is everything that is genuinely about one registered definition and
    would be WRONG to share across a corridor:

    * `check_and_notify`'s `pushed_keys`, read by `_record_decisions` as the row's `pushed` column.
      Two definitions on one pair routinely differ there — different windows, and `_notified` is
      per registration — so a pair-grained mark would report a push to a traveller who got none,
      into `pushed_any`, into `varsel_decision_groups`' quoted-wording pick and, through the
      nightly job, permanently into `varsel_decision_outcomes`.
    * `_decision_ledger`, the thinning rule's memory (KTD15): one row per definition per situation
      per departure is the grain the log is written at.
    * `message_inputs`, the pass-local document each row records.

    A tuple, not an f-string: `journey_key` itself embeds four `|` separators, so the string form
    needed `rsplit` on one line and `split` on the next three lines apart, with nothing declaring
    which encoding a key used.
    """
    dep = alert.departure_time.isoformat() if alert.departure_time else ""
    return (key, alert.situation_number, dep)


@dataclass(frozen=True)
class _Recipients:
    """Who watches one journey definition, in the only shape the decision log records them.

    U16's "unified recipient view" (2026-09-13, KTD4). Until then `_record_decisions` iterated
    `by_journey_key` — the BROWSER grouping — and a webhook-only definition was matched, worded and
    notified about while leaving no row anywhere: KTD10 held client routes out of a table
    `/historikk` and `/meldinger` publish, and those pages were public. They are behind Entur SSO
    since 2026-09-10, and at the population R1 sizes for "unscored" would have been the product.

    **Counts, never identities.** `n_devices` and `n_subscriptions` are the whole of what the two
    channels contribute beyond the channel name; no subscription, client, destination or external
    reference is carried, so the grain stays the journey DEFINITION — a route, a window and a
    weekday set, which names nobody. `test_notification_contract` pins that as a property of the
    files rather than of this comment.

    `journey` is the record the row is built from, and on a shared definition it is the BROWSER
    registration's: `match_targets` is built that way (`setdefault`, browser first) because a
    browser record carries `from_name`/`to_name`, the service caveat and the consent version, and
    `_notification_payload` reads them. Note the row itself takes only `from_stop`/`to_stop` off it,
    which both channels' records carry identically.
    """

    journey: dict
    channel: str
    n_devices: int = 0
    n_subscriptions: int = 0


def _recipient_view(
    by_journey_key: dict[str, dict],
    journeys: list[tuple[str, dict]],
    webhook_by_key: dict[str, list] | None = None,
    match_targets: dict[str, dict] | None = None,
) -> dict[str, _Recipients]:
    """Every journey definition this cycle matched, with who was watching it on each channel.

    The webhook arguments default to empty so a caller with no client channel — every test that
    drives the browser cycle, and a deployment with the flag off — gets exactly the view
    `_record_decisions` used to build for itself.

    A definition is in the result iff it is in `match_targets` (or, absent that, in
    `by_journey_key`): that is the set pass 1a actually matched, so the view and the alerts cannot
    disagree about which definitions exist.
    """
    webhook_by_key = webhook_by_key or {}
    targets = match_targets if match_targets is not None else by_journey_key
    device_ids: dict[str, set[str]] = {}
    for _jid, journey in journeys:
        device_ids.setdefault(journey_key(journey), set()).add(journey["device_id"])
    view: dict[str, _Recipients] = {}
    for key, journey in targets.items():
        browser = key in by_journey_key
        subscriptions = len(webhook_by_key.get(key, ()))
        # The literals are `decisions.CHANNELS`, written out rather than imported: this module
        # imports `decisions` LAZILY, inside `_record_decisions`, so that a deploy missing it
        # degrades to "no log" rather than "no pushes" — and a module-level import here to save
        # three string constants would retire that property.
        if browser and subscriptions:
            channel = "both"
        elif browser:
            channel = "browser"
        else:
            channel = "webhook"
        view[key] = _Recipients(
            journey=journey,
            channel=channel,
            n_devices=len(device_ids.get(key, ())),
            n_subscriptions=subscriptions,
        )
    return view


def _subject_key(pair: tuple[str, str], alert: "Alert") -> tuple[tuple[str, str], str, str]:
    """The PAIR-grained train slot: (place pair, situation, departure).

    The coarse half of KTD9's split, and it keys the three caches that must NOT carry the wording
    key's last four elements:

    * `_alternatives`, because the alternative cannot be keyed on its own identity — the lookup is
      skipped on most cycles and `_reconcile_caches` keeps the cached answer through that silence,
      which is a read that would miss under a key naming the answer it is looking for.
    * `_message_attempts` and `_message_errors`, because `_forget_message` states in as many words
      that a moved alternative must NOT restart either budget: keyed on the wording key, a
      replacement train that shifted by a minute would hand a hopeless payload a fresh three
      attempts on every cycle, which is the ~720-calls-per-situation hole those counters closed.
    """
    dep = alert.departure_time.isoformat() if alert.departure_time else ""
    return (pair, alert.situation_number, dep)


def _message_key(
    pair: tuple[str, str],
    alert: "Alert",
    *,
    now: datetime | None = None,
    alternatives_snapshot: Mapping[tuple, AlternativeJourney] | None = None,
) -> tuple:
    """KTD9's wording key for one (pair, train) — what `_messages` and `_message_meta` are keyed on.

    Built through `notification_wording.wording_key`, the leaf both this module and the
    journey-alert read import, so the pass and the read cannot come to disagree about what makes
    two wordings the same. See that function for what each element is doing there.

    **The alternative is the EFFECTIVE one**, read out of `_alternatives` rather than off the
    alert. That is the whole of the difference between this working and this costing a backend call
    per alerting train per cycle: a Journey Planner lookup is skipped on most cycles (the impact
    gate, the 6 h horizon, `impact._MAX_ALTERNATIVE_LOOKUPS`, an outage), the alert's own field is
    then `None`, and `_reconcile_caches` deliberately keeps the cached answer through that silence
    so the card does not flap. Keying on the raw field would turn every silent cycle into a miss.
    Read under `_state_lock` because pass 1b and the request path both call this.

    `now` is the cycle's single clock where the caller has one; «i morgen» is in the text, so it is
    in the key, and `_departs_tomorrow` is the same derivation `llm_message.build_message_input`
    uses for the `i_morgen` flag the read keys on.

    `alternatives_snapshot` is how a caller that already holds a copy avoids the lock — which is not
    an optimisation but a rule `_record_decisions` states in as many words: it snapshots `_messages`
    and `_message_meta` under ONE take for the whole pass, because at the pilot's scale a take per
    subject is tens of thousands of acquisitions on the poller thread that the request path can be
    waiting behind. Deriving the key inside its alert loop would have put every one of them back.
    """
    if alternatives_snapshot is not None:
        alternative = alternatives_snapshot.get(_subject_key(pair, alert))
    else:
        with _state_lock:
            alternative = _alternatives.get(_subject_key(pair, alert))
    return notification_wording.wording_key(
        pair,
        alert,
        alternative,
        tomorrow=_departs_tomorrow(alert.departure_time, now),
    )


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


#: What makes two alternatives the same advice — moved to `notification_wording` on 2026-09-12
#: (KTD9) so the wording key is one function rather than a copy per surface. Kept as a name here
#: because `_reconcile_caches` asks the question in its own terms, and because the reasoning behind
#: comparing at the MINUTE and carrying the two booleans lives in that function's docstring.
_alternative_identity = notification_wording.alternative_identity


# ---- State: devices, journeys, notified ------------------------------------------------------
#
# WHERE A REGISTRATION LIVES IS `varsel_state`'s, not this module's, since U13: one seam
# (`varsel_state.RegistrationStore`) with the blob backend below it today and U12's Firestore
# collections behind it next. Every read and write this module makes goes through
# :data:`_registrations`; nothing here indexes a state dict any more.
#
# The six names below are ALIASES of the blob backend's own objects, kept for one release so the
# `/varsel` suites and their siblings read exactly the state they always read while the seam itself
# is proved. They are the blob backend's, and they retire with it -- do not add a reader.
_registrations: varsel_state.RegistrationStore = varsel_state.BlobRegistrations(
    max_devices=_MAX_DEVICES, max_journeys=_MAX_JOURNEYS
)
_state_lock = varsel_state.lock
_devices = varsel_state._devices
_journeys = varsel_state._journeys
_notified = varsel_state._notified
_device_tombstones = varsel_state._device_tombstones
_pushed_trains = varsel_state._pushed_trains
_daily_pushes = varsel_state._daily_pushes
_togvarsel_rounds = varsel_state._togvarsel_rounds
_state_extra = varsel_state._state_extra
# Read-only, and aliased for the same reason: `_STATE_KEYS` is what the blob's
# rollback-insurance test asserts about, and `_STATE_BUCKET` is how a suite checks it is
# running in the documented bucket-less mode.
_STATE_KEYS = varsel_state._STATE_KEYS
_STATE_PATH = varsel_state._STATE_PATH
_STATE_BUCKET = varsel_state._STATE_BUCKET
_STATE_BLOB = varsel_state._STATE_BLOB
# The written wording for one (place pair, train), keyed by :func:`_message_key` — KTD9's shape,
# shared with the journey-alert read through `notification_wording.wording_key`. Keyed on the
# PLACE PAIR since 2026-09-12 and on the journey *definition* before that: what a wording says is a
# function of the trip's two ends and of the disrupted train, never of the window or weekday set a
# particular commuter registered, so a corridor with five hundred registrations now costs one
# backend call per disrupted train rather than five hundred. In-memory only: a redeploy re-writes
# messages rather than restoring stale advice from before the restart.
_messages: dict[tuple, str] = {}
# The alternative the poller found for that (pair, train), keyed by :func:`_subject_key` — the
# COARSE key, deliberately: an entry has to be findable on a cycle where no lookup ran, and a key
# naming the alternative's own identity cannot be. Cached rather than recomputed because the
# request path deliberately does NOT call the Journey Planner (see match_journey) -- without this
# the field would be structurally null on the only endpoint that serves it, which is how it shipped
# until review on 2026-07-29.
#
# Pair-keyed for a second reason beyond sharing the cost: `impact._MAX_ALTERNATIVE_LOOKUPS` bounds
# one /impact call, so which trains on a corridor get an answer depends on each definition's own
# window. Shared, a lookup ANY definition on the pair paid for answers the whole corridor.
#
# The situation's end is the OUTER bound of these caches' lives, not their whole lifecycle:
# _reconcile_caches drops an entry as soon as this cycle's match supersedes it (see its docstring).
# A 12 h-long situation would otherwise keep advising an escape route chosen against numbers from
# hours ago -- including one the selection rule has since decided against.
_alternatives: dict[tuple, AlternativeJourney] = {}
# `_subject_key` too, and that is the load-bearing half of KTD9's split: how many times writing
# this message has failed FOR REASONS TO DO WITH THE PAYLOAD, so an input the LLM cannot handle is
# not retried on every tick for the situation's whole life. Under the WORDING key a replacement
# train shifting by a minute would hand a hopeless payload a fresh budget every cycle, which is
# exactly the hole :data:`_MAX_MESSAGE_ATTEMPTS` closed -- see `_forget_message`, which refuses to
# clear these for the same reason.
_message_attempts: dict[tuple, int] = {}
# Same key, same lifecycle: how many times the backend was reached and failed TRANSIENTLY. Counted
# apart from `_message_attempts` because the two answer different questions -- "is this payload
# hopeless?" and "is Vertex having a bad morning?" -- and one counter answering both meant a bad
# morning permanently retired a perfectly writable payload. See :data:`_MAX_TRANSIENT_ATTEMPTS`.
_message_errors: dict[tuple, int] = {}
# The WORDING key, like `_messages` and dropped with it: how this (pair, train)'s message came
# about — {"status": ..., "provider": ..., "model": ...}. Written here rather than derived at record
# time because only this loop can tell a per-cycle cap from an exhausted retry from a model that
# refused, and once the deterministic wording is stored all three look identical. See
# decisions.build_row.
_message_meta: dict[tuple, dict[str, Any]] = {}

# ---- The decision log's thinning ledger (KTD15, R11) --------------------------------------------
#
# What `_record_decisions` remembers about a subject between cycles, keyed by :func:`_decision_key`:
# (journey definition, situation, departure). DEFINITION-grained and staying that way (KTD9 moved
# the wording caches to the place pair and left this alone): a row is written per definition, and a
# corridor's registrations differ in exactly the things thinning judges -- their windows decide
# which trains they see at all. One entry:
#
#   {"row": the last row BUILT for this subject, written or not,
#    "written": whether that row was the one sent to BigQuery,
#    "alertworthy": its class at the last observation,
#    "max_impact": the highest impact probability ever seen for it,
#    "written_at": when a row was last written (the quarter-hour clock)}
#
# ITS OWN STRUCTURE, not a field on `_message_meta`, and the reason is the one thing about this that
# is easy to get wrong: `_forget_message` drops that entry the tick a subject eases below the gate
# (`_reconcile_caches`), which is exactly the tick this rule needs the PRIOR class to notice a
# transition. A ledger that forgets on the transition is a ledger that cannot see one.
#
# Swept with `_messages` and `_notified` on the same liveness test (the closing row is written from
# the sweep -- see `_retire_decision_subjects`) and cleared by every suite's `_reset_state`, because
# a leaked half-spent ledger reads exactly like the give-up bug.
#
# WHAT IT COSTS IN MEMORY, stated because it is the one thing about this that does not get cheaper
# with scale: an entry holds a whole decision row, which prd measures at ~1 KB (`message_input` is
# most of it), and the ledger holds one entry per LIVE subject. Today that is ~1 500 subjects in prd,
# i.e. a megabyte and a half. At the 35 000-80 000 definitions R1 sizes for, extrapolating prd's
# ~15-20 subjects per matched definition and its ~1/3 match rate, it is 200 000-500 000 entries --
# 200-500 MB against a 2 048 MiB pod that already uses 350-435 MiB. That is the harness's
# measurement to take before the pilot grows, and the lever if it binds is to store the closing
# row's big text columns by reference to the last written row rather than by copy. It is NOT a
# reason to drop the closing row: without it `final_*` describes a cycle the rule skipped.
#
# ONE MORE TERM SINCE THE U4 REVIEW, and it is the only one an OUTAGE grows: a retiring subject
# whose closing row the write lost is put BACK here (`_record_decisions`' rollback) rather than
# dropped, so a BigQuery outage holds every retirement it spanned on top of the live subjects.
# ~1 KB each against prd's ~3 000 retirements a day -- tens of MB over a multi-hour outage today,
# 15-30x that at the pilot's target scale, and it drains the moment an insert is accepted. Bounded
# and worth the memory: a closing row exists nowhere else and no later cycle re-derives it.
_decision_ledger: dict[tuple[str, str, str], dict[str, Any]] = {}
#: How long a subject can go without a row when nothing about it has changed (KTD15).
#:
#: Measured 2026-09-11 in prd: 74 000-93 000 rows a day at 226 definitions, ~400 per definition per
#: day, 88-95 % of them sub-threshold no-SX subjects re-scored on a ~295 s cycle with nothing having
#: moved. At 35 000-80 000 definitions that is 14-32 M rows and 14-33 GB a day unthinned, which no
#: bytes cap on a 14-day /historikk window can serve (R12).
#:
#: WHAT THE RULE ACTUALLY CUTS, replayed over prd's own rows (2026-09-12, the two days to 09-10:
#: 168 436 rows over 6 462 subjects): **2.63x**, i.e. 38.1 % of the rows survive, and the same 38 %
#: for the never-alerting subjects that are almost all of them. The kept rows are 6 462 first
#: sightings, 44 pushes, 83 transitions, **13 502 new maxima** and 40 881 interval rows, plus 3 153
#: closing rows. The new-maximum trigger being a fifth of the total is the surprise and it is
#: correct rather than a leak: a no-SX probability drifts upward as the ask horizon shortens, and
#: every one of those rows is the peak `max_impact_probability` would otherwise not hold.
#:
#: So 15 min is NOT enough on its own at 100 000 -- 33 GB a day becomes 12.5, and a 14-day window is
#: still tens of GB. That is the decision-log volume question the plan carries, and its answer is a
#: per-day summary table the pages read instead of the groups view, gated on exactly this
#: measurement. A longer interval is not the answer: measured on the same rows, 30 min buys 3.76x
#: and 60 min 4.96x, and past about an hour the interval stops being a thinning rule and starts
#: deciding which cycle a reader is allowed to see. (A materiality band on the new maximum -- only
#: writing a rise of 0.05 or more -- buys 2.93x and costs the exact peak; not taken.)
#:
#: Zero disables the rule: every observation writes. That is what the log did before 2026-09-12, and
#: it is the arm the equivalence test replays against.
_DECISION_THINNING_INTERVAL_S = 15 * 60


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _serialize_state() -> str:
    return varsel_state.serialize_state()


def _apply_state(raw: str) -> None:
    varsel_state.apply_state(raw)


def load_state() -> None:
    """Load persisted state at startup: registrations through the seam, round markers from
    whichever home `varsel_state.configure_rounds` named. The second call is a no-op under the blob
    backend, whose own load has already applied them."""
    _registrations.load()
    varsel_state.load_rounds()


def _save_state() -> bool:
    """Make every mutation since the last call durable, and say whether it worked.

    The seam's :meth:`varsel_state.RegistrationStore.save`, which is the ONE asymmetry between the
    two backends and is stated rather than hidden: the blob batches, so this is the write that makes
    the preceding mutations durable, while the store has already written each of them as its own
    document and answers True with nothing to flush. So the call sequence -- mutate, then save -- is
    correct on both, and it is what lets the request path stop writing a blob at the cutover without
    a single call site moving.

    A suite that needs persistence to FAIL, or that counts writes, patches `varsel_state.save_state`
    rather than this name: everything persists through that function, this delegator and the
    backend's own call alike, which is what makes such a patch total.
    """
    return _registrations.save()


# ---- Togvarsel round markers + journey snapshot (injected into togvarsel_notify) --------------
# That module imports neither varsel nor main (the package rule), so main hands it these
# callables. Both marker accessors deal in detached copies: togvarsel_notify mutates its working
# map freely and hands it back whole through the store -- it never reaches varsel globals.
#
# The markers themselves are deliberately OFF the registration seam (see `varsel_state`): a rush
# round marker is not a registration, so it does not belong on a protocol whose every method is a
# question about one. Off the seam, not off durability -- `varsel_state.save_rounds` writes them to
# whichever home the backend named. This pair is all togvarsel_notify ever sees.


def togvarsel_rounds_for(date_str: str) -> dict[str, Any]:
    """A copy of the togvarsel round markers for one Oslo date ({} when none exist)."""
    return varsel_state.rounds_for(date_str)


def store_togvarsel_rounds(date_str: str, markers: Mapping[str, Any]) -> None:
    """Replace the round markers for `date_str` -- the current Oslo date -- and persist.

    Every OTHER date is pruned: markers dedup rounds and seed follow-up baselines for one day
    only, so the first write after midnight is what forgets yesterday. Persistence is
    :func:`varsel_state.save_rounds`' best-effort write-through -- the whole state blob under the
    blob backend, one Firestore document under the store -- and a failure there is logged, never
    raised, so the in-memory markers still dedup this process and the cost of a lost write is
    bounded by the next restart re-evaluating inside the timeliness window."""
    with _state_lock:
        varsel_state.store_rounds(date_str, markers)
        varsel_state.save_rounds()


def journeys_snapshot() -> dict[str, dict]:
    """A snapshot of the registered journeys, for togvarsel_notify's matcher (it only reads scalar
    fields, and the seam's own copy keeps a concurrent delete from tearing a record mid-iteration).
    """
    return dict(_registrations.all_journeys())


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
    journey: dict,
    now: datetime | None = None,
    *,
    with_alternative: bool = False,
    path: alternatives.FetchPath = alternatives._POLLER_PATH,
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

    `path` says whose Journey Planner budget those lookups spend (see :func:`_impact`).
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
                from_stop=from_ref, to_stop=to_ref, path=path,
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
                            bool(getattr(sit, "answered", True)),
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
                    "answered": known.answered,
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
        impact_p = dep.impact_probability
        # THE one gate, since 2026-09-08: `alert_policy.gate` states the comparison (and the
        # "None is never alert-worthy" rule) in a single place, so this browser adapter and the
        # client adapter being built beside it cannot come to disagree about what a threshold
        # means. It is `threshold_for` underneath and nothing else -- the per-operator table and
        # the no-SX cut are unchanged, which is why `alert_threshold` still reads exactly what it
        # always did on every existing decision row.
        threshold, alertworthy = gate(impact_p, sit.operator, has_situation=sit.has_situation)
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
            alertworthy=alertworthy,
            latest_observation=observation_by_line.get(sit.line),
            alternative_journey=getattr(sit, "alternative_journey", None),
            # This situation's own lookup, projected onto the response the alert carries: the four
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
                    # Defaulted True like the model field: a synthetic provider that predates the
                    # flag is reporting terminal absences, not throttled lookups.
                    "answered": bool(getattr(sit, "answered", True)),
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


def _pending_pushes(
    jids, alerts: list[Alert], webhook_recipients: Sequence[WebhookRecipient] = ()
) -> list[Alert]:
    """The alert-worthy trains a notification is still coming for, worst-first — on ANY channel.

    This is what the writing budget is spent on.

    One entry per TRAIN (`one_alert_per_train`, the same collapse loop 2 delivers through), so the
    keys these are written under are exactly the keys the delivery loop reads back.

    "Still coming" is per REGISTRATION, because `_notified` is: two devices on one trip are notified
    separately, so a train the first device already heard about is still pending for the second
    and its wording can still change what somebody sees. A journey definition nobody is registered
    on any more yields nothing, which is correct — there is no notification to write for.

    **A webhook subscription is another registration** (KTD3), so it is asked the same question
    against its own ledger: `WebhookRecipient.delivered`, Firestore's counterpart of `_notified`,
    read once per cycle in `_load_webhook_recipients`. Never the store itself — this is called
    from pass 1b, which is sequential precisely because it blocks on nothing. Its GRAIN is the
    channel's own, not the browser's: per TRAIN (`_webhook_pending`), because that channel mints one
    event per (subscription, train), where `_notified` is per situation because a browser is pushed
    once per situation.

    **The caller passes NO browser jids when `push_enabled()` is false**, and that half is easy to
    miss and expensive to get wrong (KTD4). `_notified` only grows on a successful browser send, so
    without it a keypair-less deployment (tst has no keypair) would report every alert-worthy train
    as pending for every browser registration on every cycle, and spend the whole write budget
    re-wording trains it can never deliver — competing with exactly the webhook subjects the gate
    change exists to serve.

    Deliberately approximate in one direction only. It does not check that each registration
    still has a live subscription (loop 2 does, and drops the device if the endpoint is gone), so
    a journey whose device has vanished can still attract a write. That over-writes at worst; the
    alternative — reading device state here — would let a stale subscription silently withhold
    the wording from a push that does go out, and a missing wording is the failure this whole
    path exists to prevent.
    """
    notified = _registrations.notified_snapshot(jids)
    return [
        alert
        for alert in one_alert_per_train(alerts)
        if alert.alertworthy
        and (
            any(f"{jid}|{alert.situation_number}" not in notified for jid in jids)
            or any(_webhook_pending(recipient, alert) for recipient in webhook_recipients)
        )
    ]


def _webhook_pending(recipient: WebhookRecipient, alert: Alert) -> bool:
    """Is a webhook notification about this TRAIN still coming for this subscription?

    Per train, because the store mints one event per (subscription, train) -- the `alert_id` -- and
    keys its delivered marks on it. Admitting on the situation number, as this did until
    2026-09-10, refused a second departure under an already-delivered situation: its event still
    went out (a new train is a new subject) but carried the template, while the first train's
    carried the model's prose. The id is derived through `notification_events.alert_id_for`, the
    same derivation the emit step's `train_ref` uses, so a mark the store wrote is a mark this
    reads. A train that cannot be a stable subject is never minted and is therefore never pending.
    """
    train_id = notification_events.alert_id_for(
        recipient.recipient_id,
        operator=alert.operator,
        line=alert.line,
        journey_ref=alert.jkey,
        stop_place=alert.stop_place,
        stop_order=alert.stop_order,
        departure_time=alert.departure_time,
    )
    return train_id is not None and train_id not in recipient.delivered


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


def alert_worthy_trains(alerts: list[Alert]) -> list[Alert]:
    """The trains a notification would go out for: the collapse FIRST, then the push gate.

    For the journey-keyed public read (2026-09-10), which must select trains exactly as the pass
    does. The order is the whole point and it is easy to get backwards: `_observations_for_key` walks
    :func:`one_alert_per_train` and tests `alertworthy` on each WINNER, so a train whose most
    severe reading is a sub-gate no-SX one (0.80 under its own 0.85) that outranks an over-gate
    published one (0.70 over 0.65) is NOT alert-worthy — the collapse kept the sub-gate winner.
    Filtering on the gate first would list that train through its published situation and tell
    a reader about a train the pass would be withdrawing. That loop is left untouched because its
    non-alert-worthy winners are its withdrawal subjects; this is the alert-worthy subset of what
    it iterates, in the collapse's own order, which the read sorts afterwards.
    """
    return [alert for alert in one_alert_per_train(alerts) if alert.alertworthy]


# ---- Notify ----------------------------------------------------------------------------------
# `_delay_phrase`, `_train_label`, `_LEG_MODE_NOUN`, `_first_leg_label`,
# `_alternative_phrase` and `_deterministic_body` moved to `notification_wording` on
# 2026-09-08 and are imported above under their old names. Not a rewrite: every one of them
# was already a pure function of the message input `llm_message.build_message_input`
# produces, which is what made the move a move. The bytes a traveller receives are unchanged,
# and `test_varsel`/`test_varsel_api` assert them against the imported names, so a drift is a
# failing assertion rather than a quietly different notification.


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


# `_notification_title` moved to `notification_wording` with the body it heads, and is
# imported above under its old name -- the two must name the train identically, so they
# belong together.


def _notification_tag(journey_id: str | None, alert: Alert) -> str | None:
    """One notification per TRAIN per registration on the lock screen (finding 14).

    The service worker used the situation number as the tag, so a forecast and the cancellation
    that confirmed it -- two SX messages, two situation numbers, one train -- stacked as two
    notifications. Keyed on the train (`_train_key`, the same identity `one_alert_per_train`
    collapses on), a later push about the same departure replaces the earlier one. `None` when the
    alert cannot say which train it is about; the worker then falls back to the situation number,
    which is what it always did.
    """
    key = _train_key(alert)
    if journey_id is None or key is None:
        return None
    return f"{journey_id}|" + "|".join(str(part) for part in key)


def notification_wording_for(
    message_input: dict,
    from_name: str,
    to_name: str,
    message: str | None,
    *,
    confirmed: bool = False,
) -> tuple[str, str]:
    """The title and body ONE notification carries, whatever channel carries it.

    Extracted from :func:`_notification_payload` on 2026-09-08 so the webhook channel's event
    envelope is byte-identical to the Web Push by CONSTRUCTION rather than by a parallel builder
    that agrees today. R7 states the claim ("the Norwegian title and body /varsel would have
    pushed"), and a second copy of two f-strings is exactly the shape that drifts silently: nothing
    crashes and no assertion fails when one of them stops prefixing the route.

    The BODY is the route on its own line followed by the wording (2026-09-02). The wording is what
    `/historikk` records and what the two writers are compared on, so the route was added AROUND it
    when the title stopped carrying it rather than written into it. The TITLE carries the
    environment tag — `[dev] `, leading because phones truncate on the right — and the body never
    does, so the recorded wording is the same text in every environment.
    """
    return (
        _tag_environment(
            _notification_title(message_input, from_name, to_name, confirmed=confirmed)
        ),
        f"{from_name} → {to_name}\n{message or _deterministic_body(message_input)}",
    )


def _notification_payload(
    journey: dict,
    alert: Alert,
    message: str | None = None,
    *,
    journey_id: str | None = None,
    message_input: dict | None = None,
    brand: str = DEFAULT_BRAND,
    confirmed: bool = False,
) -> dict:
    """Norwegian Web Push payload for one journey×situation match.

    `message` is the LLM-written text when one was produced; the deterministic template is used
    otherwise, so a missing or failing message writer costs wording, never the notification. The
    url and situation_number are unchanged either way — the notification stays recognisable and
    the service worker's click-through keeps working.

    `message_input` is this decision's message input (see :func:`_message_input`); `None` means it
    could not be built, and the template falls back to reconstructing the little of it the wording
    needs from the alert — a push must not be lost to a payload problem.

    The BODY is the route on its own line followed by the wording, unchanged (2026-09-02). The
    wording is what `/historikk` records and what the two writers are compared on, so the route
    was added AROUND it when the title stopped carrying it (:func:`_notification_title`) rather
    than written into it. `tag` is the train, see :func:`_notification_tag`; `confirmed` is
    whether this registration was already pushed about that train.

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
    message_input = message_input or _message_input_from_alert(alert)
    title, body = notification_wording_for(
        message_input, from_name, to_name, message, confirmed=confirmed
    )
    payload = {
        "title": title,
        "body": body,
        "url": url,
        "icon": assets["icon"],
        "situation_number": alert.situation_number,
    }
    tag = _notification_tag(journey_id, alert)
    if tag is not None:
        payload["tag"] = tag
    return payload


def _send_push(subscription: dict, payload: dict) -> tuple[str, dict | None]:
    """Send one Web Push. Returns ``(result, error_detail)``: result is 'sent', 'gone' (404/410 —
    drop the device) or 'error' (retry); error_detail is None on success, else the failure's
    status/host/body (body capped at 400 chars) for the caller's audit record.

    This function emits no log line of its own (R11): the failure detail rides the single
    ``event="push"`` record ``_log_push`` writes per delivery attempt, so a failed push is exactly
    one record — never a prose line plus an audit line saying the same thing.
    """
    from pywebpush import WebPushException, webpush

    endpoint = str(subscription.get("endpoint", ""))
    host = endpoint.split("/", 3)[2] if "://" in endpoint else endpoint[:40]
    error_detail: dict | None = None
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
        result = "sent"
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        # `str(exc)` and not only the response body: pywebpush raises this for failures that carry
        # NO response at all (a malformed VAPID key, an encryption error), where `text` is empty
        # and the exception's own message is the entire diagnosis. Dropping it left a detail dict
        # of three Nones — a record that says a push failed and nothing about why.
        body = (getattr(exc.response, "text", "") or "")[:400] or str(exc)[:400]
        error_detail = {"status": status, "host": host, "body": body}
        result = "gone" if status in (404, 410) else "error"
    except Exception as exc:  # noqa: BLE001  (deliberate: any raise must become one counted, audited "error")
        # The audit record is the only trace of an unexpected raise now, so it carries the
        # TRACEBACK, not just the exception's name: `type: str` leaves the raising frame
        # unrecoverable, and an unexpected raise here is by definition a defect whose location is
        # the whole question. Truncated, because it rides a log field.
        error_detail = {
            "status": None,
            "host": host,
            "body": f"{type(exc).__name__}: {exc}"[:400],
            "traceback": traceback.format_exc()[-2000:],
        }
        result = "error"
    # Exactly one increment per delivery attempt, on the single exit (KTD11) — a per-branch
    # `return` here is how the unexpected-exception path quietly stops being counted. The push
    # KIND stays in the structured `event="push"` log, never in a label, so the series set is
    # fixed at three.
    metrics.PUSH_TOTAL.labels(result=result).inc()
    return result, error_detail


def _send_push_result(sent: tuple[str, dict | None] | str) -> tuple[str, dict | None]:
    """Normalize a ``_send_push`` return to ``(result, error_detail)``.

    Suites across this repo stub ``_send_push`` with a bare result string; tolerating that shape
    here (detail ``None``) is what lets the stubbed contract survive — the ``fetch_candidates``
    precedent. The real function always returns the 2-tuple, so production never takes the
    fallback arm.
    """
    return sent if isinstance(sent, tuple) else (sent, None)


def _log_push(
    kind: str,
    result: str,
    device_id: str,
    payload: dict,
    *,
    journey_id: str | None = None,
    alert: Alert | None = None,
    error_detail: dict | None = None,
) -> None:
    """One structured log line per push attempt, message text included — the audit trail of what
    was actually sent to whom (``jsonPayload.event = "push"`` in Cloud Logging). A failed
    attempt's detail (``_send_push``'s status/host/body) rides this same record as the three
    ``push_error_*`` fields — the audit is symmetric (R11), and this record is the ONLY line."""
    fields = {
        "event": "push",
        "push_kind": kind,  # "alert" (journey×situation match) or "test"
        "result": result,  # "sent", "gone" or "error"
        "device_id": device_id,
        "journey_id": journey_id,
        "title": payload.get("title"),
        "body": payload.get("body"),
    }
    if error_detail is not None:
        fields.update(
            push_error_status=error_detail.get("status"),
            push_error_host=error_detail.get("host"),
            push_error_body=error_detail.get("body"),
        )
        if error_detail.get("traceback"):
            fields["push_error_traceback"] = error_detail["traceback"]
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
    # ONE record per attempt (R11), but not one SEVERITY: consolidating the old prose lines into
    # this record moved every failure to INFO, and the runbook's "any ERROR is worth a look" rule
    # watches ERROR only. A defect that makes EVERY push raise — a pywebpush upgrade, a malformed
    # VAPID key — would then produce no ERROR and no WARNING anywhere, leaving only the push
    # failure-RATIO rule, which needs >5 attempts in 30 min and so cannot see a quiet night.
    # `gone` stays INFO: a device that unsubscribed is the system working, and it is routine.
    level = logging.ERROR if error_detail and error_detail.get("traceback") else (
        logging.WARNING if result == "error" else logging.INFO
    )
    logger.log(
        level,
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
    prepare: Callable[[Any], tuple[dict, dict] | None],
) -> list[tuple[str, Any, str, dict, dict | None]]:
    """Fan one batch of pending pushes out across devices, bounded by the module's worker pool.

    ``per_device`` maps device_id → that device's pending items, in send order; ``prepare`` turns
    one item into ``(subscription, payload)`` and runs on the fan-out's worker thread. It may
    return ``None`` to withdraw an item it has decided must no longer be sent — the batch is
    collected before the pool starts, so ``prepare`` is the last look a caller gets before the
    network call (:func:`check_and_notify`'s ``_prepare_alert`` uses it for a registration deleted
    inside that window). A withdrawn item yields no result tuple at all, so the caller's own
    bookkeeping never sees it. Parallel
    ACROSS devices, sequential WITHIN one: each send can block for :data:`_PUSH_TIMEOUT_S` against
    a client-supplied endpoint (FCM, Mozilla, Apple), so sequential delivery would stall the
    poller for devices × 10 s in the bad case — while a "gone" result must still stop that
    device's REMAINING items this batch, which only holds if its own sends stay ordered.

    Returns ``(device_id, item, result, payload, error_detail)`` flattened in ``per_device``
    order — error_detail is ``_send_push``'s failure detail (None on success), so the caller's
    single ``event="push"`` record can carry it (R11); the caller
    owns what a result means — logging, dedup bookkeeping, and the gone → grace → drop rule
    (:func:`handle_gone_device`). Shared by check_and_notify's SX delivery loop and (injected by
    main, so that module imports no varsel) togvarsel_notify's forecast rounds.
    """

    def _deliver_device(entry: tuple[str, list[Any]]) -> list[tuple[str, Any, str, dict, dict | None]]:
        """One device's pending items, in order, stopping at a dead subscription."""
        device_id, items = entry
        out = []
        for item in items:
            prepared = prepare(item)
            if prepared is None:
                continue  # withdrawn between collection and dispatch; not a delivery attempt
            subscription, payload = prepared
            result, error_detail = _send_push_result(_send_push(subscription, payload))
            out.append((device_id, item, result, payload, error_detail))
            if result == "gone":
                break  # stop this device's remaining pushes this batch
        return out

    if not per_device:
        return []
    with ThreadPoolExecutor(max_workers=min(_PUSH_WORKERS, len(per_device))) as pool:
        batches = list(pool.map(_deliver_device, per_device.items()))
    return [r for batch in batches for r in batch]


def _remember_message_meta(
    message_key: tuple,
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
    message_key: tuple | None = None,
    outcome: dict[str, Any] | None = None,
) -> str | None:
    """The LLM-written body for this (place pair, train), or None to use the template wording.

    `message_key` is KTD9's wording key, under which the provenance is recorded. Passed in rather
    than derived here since 2026-09-12: the key now names the EFFECTIVE alternative, which only the
    pass that reconciled the caches knows, and deriving a second one here is how two call sites come
    to disagree about which entry they are writing. It defaults to `None` so the 25 suites that stub
    this function wholesale keep working, and to the caller's own derivation for a legacy caller.

    `journey_key` is the representative definition on the pair — see the queue in
    `check_and_notify`. One call serves every definition on that pair, so the `journey_digest` in
    the prompt audit record names the one whose row caused the call and not the others'.

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
        if message_key is not None:
            _remember_message_meta(
                message_key,
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


def _deliverable_message(message_key: tuple) -> str | None:
    """The written wording this (place pair, train) may actually be SHOWN, or None.

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


def _departs_tomorrow(departure: datetime | None, now: datetime | None = None) -> bool:
    """Is this departure on a later Oslo calendar day than `now`? Mirrors `llm_message._departs_tomorrow`
    without importing it, for the same reason `_local_hhmm` mirrors `_hhmm`."""
    if departure is None:
        return False
    now = now or datetime.now(UTC)
    # EXACTLY tomorrow. The poller scores ~12 h ahead so nothing further out reaches a wording, and
    # for anything that somehow did, «i morgen» would be a false statement rather than a missing one.
    return departure.astimezone(_OSLO).date() == now.astimezone(_OSLO).date() + timedelta(days=1)


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
    if _departs_tomorrow(alert.departure_time):
        body["i_morgen"] = True
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
            # Same shape and same foot filter as build_message_input's, because
            # notification_wording.alternative_phrase
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
        # Same rule as build_message_input: the figure rides only when the alternative is late
        # against the plan, so `notification_wording.alternative_phrase` never says
        # "0 min senere".
        later = getattr(alt, "minutes_after_original", None)
        if alt.arrives_before_original is False and later:
            alternative["minutter_senere"] = later
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


def _thinning_reason(
    entry: dict[str, Any] | None, alert: Alert, *, pushed: bool, now: datetime
) -> str | None:
    """Why this observation has to be written down, or None to let the ledger carry it (KTD15).

    Five triggers, and each one is the reason a figure `varsel_decision_groups` computes cannot be
    recovered from any other row:

    * ``first_seen`` — `first_decided_at` is MIN(decided_at) and nothing else can supply it.
    * ``transition`` — `first_alert_at` is MIN over the ALERTING rows and `ever_alerted` a
      LOGICAL_OR, so the cycle a subject crosses the gate in either direction is load-bearing; so is
      the `et_cancelled` pick, which reads the first alerting row.
    * ``new_max`` — `max_impact_probability` is a MAX, i.e. it is only ever as high as the highest
      row written. This is the trigger that keeps a threshold tunable.
    * ``pushed`` — `pushed_any` is a LOGICAL_OR and the quoted wording comes off the FIRST pushed
      row. A thinned push loses the fact and the text together.
    * ``interval`` — everything else, at most once per :data:`_DECISION_THINNING_INTERVAL_S`.

    `final_*` and `last_decided_at` are not in that list because they are served by the CLOSING row
    instead (see :func:`_retire_decision_subjects`): the subject's last observation is written when
    it retires, so the last row in the log is the last look we got, exactly as it was unthinned.

    ONE POPULATION DOES SHRINK, and it is not one R11 names: a cycle whose only news is that the
    model wrote a wording (`message_status = 'ok'`) is thinned unless another trigger fires, so where
    the push does not go out on that same cycle -- the daily cap, a push error -- the fresh
    `message_llm` never reaches the log and a later cycle records `ok_cached` instead. That thins
    `message_wording_comparison.sql`'s same-input population and any error-to-success recovery rate.
    A sixth trigger on "a wording was written this cycle" would close it and would cost little (44
    pushes in 168 436 measured rows), but KTD15 does not list one and this is not the unit to widen
    the rule in.
    """
    if entry is None:
        return "first_seen"
    if pushed:
        return "pushed"
    if bool(alert.alertworthy) != bool(entry.get("alertworthy")):
        return "transition"
    impact_p = alert.impact_probability
    if impact_p is not None and impact_p > entry.get("max_impact", float("-inf")):
        return "new_max"
    written_at = entry.get("written_at")
    if written_at is None or (now - written_at).total_seconds() >= _DECISION_THINNING_INTERVAL_S:
        return "interval"
    return None


def _retire_decision_subjects(*, live: set[str]) -> list[tuple[tuple[str, str, str], dict]]:
    """Close out every ledger subject whose SITUATION has ended, and return what still owes a row.

    Called from the stale sweep at the top of a cycle, where `_messages`, `_notified` and the rest
    are pruned on this same liveness test — and it has to be called from there rather than left to
    the end-of-cycle absence check, because by then the sweep has already dropped the entry and
    there is nothing left to close.

    It RETURNS rather than writing: the sweep holds `_state_lock` and does no I/O under it, and a
    cycle makes exactly one decision-log write. A subject whose last observation was itself written
    needs no closing row — that row is already in the log, and a second copy of it would move
    `n_decisions` and nothing else, so it is popped and not returned.

    **The ENTRY travels with the row, not the row alone**, and that pair is the whole point of the
    return type. Retiring pops the subject, so a closing row lost to a failed insert used to be lost
    for good: nothing was left in the ledger to offer it again, and `final_impact_probability` —
    "the closest look we got before departure" — then described an older cycle for the life of the
    table. The write fails open by design, so that was not a rare path. `_record_decisions` puts
    these back when its write is short (see its rollback), and the next cycle's sweep retires them
    again; each subject still retires exactly once, because a retirement that LANDED leaves nothing
    to put back.
    """
    closing: list[tuple[tuple[str, str, str], dict]] = []
    with _state_lock:
        for key in [k for k in _decision_ledger if k[1] not in live]:
            entry = _decision_ledger.pop(key)
            if not entry.get("written") and entry.get("row") is not None:
                closing.append((key, entry))
    return closing


def _record_decisions(
    recipients_by_key: dict[str, _Recipients],
    alerts_by_key: dict[str, list[Alert]],
    pushed_keys: set[tuple[str, str, str]],
    message_inputs: dict[tuple[str, str, str], dict | None],
    *,
    push_on: bool,
    webhook_llm: bool = False,
    now: datetime | None = None,
    closing_rows: list[tuple[tuple[str, str, str], dict]] | None = None,
) -> None:
    """Append this cycle's alert decisions to the decision log.

    Records **every** matched situation, not just the alert-worthy ones: a journey we decided not
    to alert on is exactly the evidence a miss is made of, and it exists nowhere else.

    **BOTH CHANNELS since 2026-09-13** (U16, KTD4), where this took the browser grouping alone. It
    now walks :func:`_recipient_view` — every definition pass 1a matched — and each row says on
    `channel` whether a /varsel registration, a client subscription or both were watching, with
    `n_devices` and `n_subscriptions` counting the two sides. Three things that follow are easy to
    get backwards and are handled explicitly below: `push_disabled` is a BROWSER answer and must
    not be given for a definition with no browser side; which LLM delivery switch chose the wording
    depends on the channel (`VARSEL_LLM_ENABLED` for a push, `NOTIFICATION_WEBHOOK_LLM_ENABLED` for
    an event); and `pushed` for the client side is a MINT, which happens after this function has
    run and arrives through :func:`record_minted_decisions`.

    Runs after delivery so `pushed` reflects what actually reached a device rather than what was
    queued. Like every other dependency in this cycle it fails open — the import is local so a
    deploy without the module still pushes, and :func:`decisions.record` never raises.

    Records **both** wordings (2026-08-07): `message_llm` is the raw cache read, `message_deterministic`
    is this cycle's template body, and `message`/`message_source` say which of the two the traveller
    actually got. Those are different questions now that ``VARSEL_LLM_ENABLED`` only selects the
    wording — prd writes one and sends the other — and the pair on one row is the only way that
    deployment's own pushes can be compared against what the model would have said.

    **Not every observation is a row, since 2026-09-12** (KTD15). The module used to write one per
    subject per cycle and said so in prose — the repeats *are* the lead-time series — and at
    35 000-80 000 definitions that is 14-33 GB a day, which no bytes cap on /historikk can serve.
    :func:`_thinning_reason` states what is kept; what is dropped is a standing subject nothing
    about which has moved, and the figures the pages compute are unchanged because the closing row
    carries the last look. `n_decisions` is the one column whose MEANING changes, and its
    description says so.

    `now` is the cycle's single clock (the same instant the match was made at), injectable for
    tests. `closing_rows` are the sweep's — `(key, entry)` pairs, not bare rows, so a closing row
    lost to a failed insert can be put back in the ledger and offered again (see
    :func:`_retire_decision_subjects`).
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
        cycle_now = now or datetime.now(UTC)
        # ONE take of the lock for the whole pass, not one per alert (KTD15). Both dicts are keyed
        # like the alerts being walked, so the copies are the same order of magnitude as the work;
        # what this removes is tens of thousands of lock acquisitions on the poller thread, each of
        # which the request path can be waiting behind. Shallow on purpose — the inner meta dict is
        # copied per alert below, exactly as it was when the lock was held per alert.
        with _state_lock:
            messages_snapshot = dict(_messages)
            meta_snapshot = dict(_message_meta)
            # The third copy is KTD9's: the wording key names the effective alternative, so deriving
            # one reads `_alternatives`. Taken here rather than per alert for the reason above --
            # `test_recording_a_cycles_decisions_takes_the_state_lock_a_constant_number_of_times`
            # pins the count, because the per-alert form is correct, invisible and O(subjects).
            alternatives_snapshot = dict(_alternatives)

        # Retiring subjects: the sweep's, plus this pass's own (collected below). Held as
        # `(key, entry)` so a lost write can put them back — a closing row is a subject's LAST look
        # and there is no later cycle that would produce it again.
        retired: list[tuple[tuple[str, str, str], dict]] = list(closing_rows or [])
        rows = [entry["row"] for _key, entry in retired]
        seen: set[tuple[str, str, str]] = set()
        # Ledger entries for the subjects this pass WRITES, held back until the insert has actually
        # been accepted. The write fails open by design, and committing before it lands would mark a
        # lost row as written: the subject would then be thinned for the next quarter hour, and — the
        # one that matters — its `max_impact` would have advanced past a peak no row in the log
        # holds, so the peak could never be written even after BigQuery recovered.
        wrote: list[tuple[tuple[str, str, str], dict[str, Any]]] = []
        for key, recipients in recipients_by_key.items():
            journey = recipients.journey
            digest = key_digest(key)
            n_devices = recipients.n_devices
            pair = _pair(journey)
            for alert in alerts_by_key.get(key, []):
                # TWO KEYS PER ROW SINCE KTD9, and mixing them up is the defect this split exists to
                # prevent. `decision_key` is DEFINITION-grained and owns everything about this row:
                # the push mark, the thinning ledger, and the pass-local message input the row
                # stores. `message_key` is the PAIR's wording, shared with every other definition on
                # the corridor -- so `message_llm` and `message_status` describe the one wording
                # that was written, which is exactly what those columns are for.
                decision_key = _decision_key(key, alert)
                message_key = _message_key(
                    pair, alert, now=cycle_now, alternatives_snapshot=alternatives_snapshot
                )
                seen.add(decision_key)
                pushed = decision_key in pushed_keys
                entry = _decision_ledger.get(decision_key)
                reason = _thinning_reason(entry, alert, pushed=pushed, now=cycle_now)
                if reason is None and entry is not None:
                    # Not written, but not forgotten: the stored row is re-stated with this cycle's
                    # figures so the CLOSING row carries the last look rather than the last write.
                    # Cheap by construction — no template rendering, no message-input JSON, which is
                    # the work the rule exists to stop repeating.
                    entry["row"] = {
                        **decisions.rescored(entry["row"], alert),
                        # The three recipient facts, restated from THIS cycle's view rather than
                        # carried: `rescored` only knows the alert, and a definition somebody
                        # subscribed to (or unsubscribed from) during the quiet middle would
                        # otherwise close with the count and the channel it had a quarter of an
                        # hour ago.
                        "n_devices": n_devices,
                        "channel": recipients.channel,
                        "n_subscriptions": recipients.n_subscriptions,
                    }
                    entry["written"] = False
                    entry["alertworthy"] = bool(alert.alertworthy)
                    if alert.impact_probability is not None:
                        entry["max_impact"] = max(
                            entry.get("max_impact", float("-inf")), alert.impact_probability
                        )
                    continue
                # Read RAW, deliberately not through `_deliverable_message`: this is the half of the
                # pair recorded as `message_llm`, which is what was WRITTEN whether or not it was
                # sent. Since 2026-08-07 those differ by design — prd writes a wording and delivers
                # the deterministic one — and gating this read would throw away exactly the column
                # the comparison exists for. What was sent is decided below, on `delivery_on`.
                written = messages_snapshot.get(message_key)
                meta = dict(meta_snapshot.get(message_key) or {})
                # No meta means the writer was never reached for this alert, and why is knowable
                # from the state that decided it: below the threshold, no deliverable recipient on
                # any channel, no backend available to ask — or alert-worthy but not among the
                # trains this journey wrote for this cycle (`not_selected`). Since 2026-08-21 that
                # last value is much narrower: a second situation about a train another situation
                # already covers (`one_alert_per_train` collapsed it), a train with no push pending
                # that was not the journey's worst, or one past `_MAX_MESSAGES_PER_JOURNEY`. It used
                # to mean "not the single worst situation on this journey" and so covered most of
                # the alert-worthy log -- 29 501 of dev's 33 900 alert-worthy rows over 14 days.
                #
                # NARROWER AGAIN SINCE KTD9 (2026-09-12), and a rate computed across that date is
                # measuring two things: a train written for because a SIBLING definition on the same
                # corridor needed it now records `ok`/`ok_cached` on this row too, where it recorded
                # `not_selected`. The wording is genuinely this row's -- it is the one text every
                # registration on the pair is sent -- so the status describes it correctly; what
                # changed is how many rows have one.
                status = meta.get("status")
                if status is None:
                    if not alert.alertworthy:
                        status = "not_alertworthy"
                    elif not push_on and recipients.channel != "webhook":
                        # A BROWSER answer, and the channel test is what keeps it one (U16). The
                        # value means "this deployment has no VAPID keypair, so nothing could be
                        # delivered to a browser and no wording was written" -- which is the reason
                        # a shared or browser-only definition got none, and says nothing at all
                        # about a definition only a client watches. Given for a webhook-only
                        # subject it would be a false cause on a row the page can display, and
                        # every historical row carrying this value would stop meaning one thing.
                        # Such a subject falls through to `not_selected` or `llm_disabled` below,
                        # which are the true answers: KTD4's write gate is "at least one
                        # deliverable recipient on ANY channel", so where a client could receive
                        # one, `meta` is set and neither branch is reached.
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
                message_input = message_inputs.get(decision_key)
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
                # WHICH SWITCH, and it is the row's channel that decides (U16). `delivery_on` is
                # `VARSEL_LLM_ENABLED`, which governs the Web Push and nothing else; the event a
                # client receives follows `NOTIFICATION_WEBHOOK_LLM_ENABLED`, read once for the
                # cycle as `webhook_llm` exactly as the two gates above are. So a webhook-only row
                # records the wording ITS recipient was sent, and a shared row keeps the browser's
                # -- the traveller's own push is what `/historikk` has always quoted, the row
                # carries `n_devices` to say a browser was there, and `channel` says the client may
                # have received the other one where a rollout has the two switches disagreeing.
                # Reading one switch for both channels was the alternative and it is wrong in a way
                # nothing would show: in a deployment with the webhook wording off, every
                # webhook-only row would quote prose no client ever got.
                delivered = written if (
                    webhook_llm if recipients.channel == "webhook" else delivery_on
                ) else None
                message = delivered or deterministic
                row = decisions.build_row(
                    journey,
                    alert,
                    journey_digest=digest,
                    n_devices=n_devices,
                    channel=recipients.channel,
                    n_subscriptions=recipients.n_subscriptions,
                        # `_decision_key`, not `(key, situation_number)` and NOT the pair-grained
                        # wording key: the mark has to be read at the grain the row is written at,
                        # which is (definition x situation x DEPARTURE) -- two definitions on one
                        # corridor genuinely differ here, since their windows decide which trains
                        # they see and `_notified` is per registration. One situation reaches 12 h
                        # from its onset and names several
                        # trains, so a two-part key lets one push mark every departure under it --
                        # into `pushed_any`, into the groups view's quoted-wording pick (the FIRST
                        # pushed row) and, through the nightly job, permanently into
                        # `varsel_decision_outcomes`. It agreed with this grain by accident until
                        # 2026-09-12, because `match_journey` keeps only the WORST alert per
                        # situation across every window, so one cycle held one departure per
                        # situation; KTD4 reads `pushed` as ground truth per TRAIN on both channels
                    # and must not rest on a collapse three call levels away.
                    pushed=pushed,
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
                rows.append(row)
                wrote.append(
                    (
                        decision_key,
                        {
                            "row": row,
                            "written": True,
                            "written_at": cycle_now,
                            "alertworthy": bool(alert.alertworthy),
                            "max_impact": max(
                                (entry or {}).get("max_impact", float("-inf")),
                                alert.impact_probability
                                if alert.impact_probability is not None
                                else float("-inf"),
                            ),
                        },
                    )
                )

        # Subjects the ledger holds that this cycle did not see. Two causes and one treatment: the
        # train left the match because it can no longer be CAUGHT (its situation lives on for hours
        # after the departures it describes), or the definition itself stopped matching. Either way
        # the subject is over, and its closing row is the last look we got at it — which is what
        # keeps `final_*` and `last_decided_at` equal to what an unthinned log would hold. The other
        # retirement, a situation that has ENDED, is closed by the stale sweep at the top of the
        # cycle and arrives here as `closing_rows`.
        for message_key in [k for k in _decision_ledger if k not in seen]:
            entry = _decision_ledger.pop(message_key)
            if not entry.get("written") and entry.get("row") is not None:
                rows.append(entry["row"])
                retired.append((message_key, entry))
        # `beat` is the poller's liveness seam: a cycle's rows are several insertAll requests now,
        # each able to spend `decisions._WRITE_RETRY`'s deadline plus an attempt, and /health reads
        # "no STEP has finished in HEALTH_STALE_AFTER_S". Unbeaten they are one silence inside the
        # single `varsel_notify` phase, and the restart it earns comes up with both planned-call
        # tables cold. `_heartbeat` swallows, and so does `decisions.record` around it, because the
        # rollback below must run whatever the seam does.
        accepted = decisions.record(rows, beat=_heartbeat)
        # A SHORT write, not only an empty one. `accepted` sums acceptances across the cycle's
        # chunks, so `not accepted` is true only when every request failed -- and a cycle that lost
        # one request of four would mark every subject in it written: thinned for the next quarter
        # hour against rows nobody holds, with `max_impact` advanced past a peak no row in the log
        # carries, so the peak can never be written even after BigQuery recovers.
        #
        # THE TRADE, both sides, because the conservative test is not free. On any short write the
        # whole cycle is re-offered next time, so the subjects whose rows DID land are written
        # twice. That is harmless: every figure `varsel_decision_groups` computes is a MIN, a MAX, a
        # LOGICAL_OR or a first/last pick, and `n_decisions` -- the one column a duplicate moves --
        # already changed meaning with the thinning rule and says so in its description. Losing a
        # peak is not harmless, and it is silent. So: duplicate rather than drop.
        #
        # WHAT A PERSISTENT SHORT WRITE DOES, since the trade above only describes a one-off. One
        # row BigQuery refuses on EVERY cycle -- a column drift, an oversized `message_input` --
        # makes this true every cycle, so no ledger entry ever commits, `written_at` never advances
        # and after one interval every subject writes every cycle: the pre-thinning volume is back
        # until the bad row is fixed. That is no worse than the week before KTD15 and it is loud
        # (`result="partial"` from `decisions` beside `result="rolled_back"` from here, every
        # cycle) -- but the two firing together is what it looks like, and nothing else produces
        # that pair.
        lost = accepted < len(rows)
        for message_key, entry in wrote:
            prior = _decision_ledger.get(message_key) if lost else None
            if not lost:
                _decision_ledger[message_key] = entry
            elif prior is not None:
                prior["row"] = entry["row"]
                # And the row is NOT in the log, so the subject owes a closing row again. Without
                # this the entry still claims its last look was written, and a subject that retires
                # on the next cycle would close with nothing -- leaving `final_*` describing the
                # older successful write rather than the last look, which is the exact hole the
                # rollback exists to close.
                prior["written"] = False
        if lost and retired:
            # Put the retiring subjects back so the next cycle's sweep (or its absence check) offers
            # their closing rows again. `setdefault`, so a key this cycle re-created wins -- it
            # cannot happen today (a retired key is popped before the pass reads the ledger, so its
            # subject reads as first-seen and a lost cycle commits nothing under it) and that is
            # exactly why the weaker call is the right one: it makes "retires exactly once" a
            # property rather than a coincidence. The entry still carries `written=False`, so the
            # closing row is owed until an insert actually takes it.
            with _state_lock:
                for message_key, entry in retired:
                    _decision_ledger.setdefault(message_key, entry)
        if lost:
            # The loss stops being invisible. `decisions` logs per REQUEST and cannot see what the
            # ledger did about it; this is the cycle-level line, and the closing count is the part
            # worth reading -- those rows exist nowhere else and no later cycle re-derives them.
            logger.warning(
                "Decision write was short (%d of %d rows accepted); re-offering %d subjects and "
                "%d closing rows next cycle",
                accepted,
                len(rows),
                len(wrote),
                len(retired),
                extra={
                    "fields": {
                        "event": "varsel_decision_write",
                        "result": "rolled_back",
                        "n_rows": len(rows),
                        "n_accepted": accepted,
                        "n_subjects": len(wrote),
                        "n_closing": len(retired),
                    }
                },
            )
    except Exception:
        logger.warning("Could not record alert decisions this cycle", exc_info=True)


def device_subscription(device_id: str) -> tuple[dict, str] | None:
    """The device's push subscription and normalized brand, or None when the device is gone or
    never subscribed. Both are read under ONE take of the state lock — devices can be deleted or
    re-branded while a caller iterates a matched set, and a split read could describe one
    registration's subscription with another's icon."""
    with _state_lock:
        dev = _registrations.get_device(device_id)
        subscription = dev.get("subscription") if dev else None
        brand = normalize_brand(dev.get("brand") if dev else None)
    if subscription is None:
        return None
    return subscription, brand


def _forget_device(device_id: str, reason: str) -> tuple[dict | None, int]:
    """Remove a device record while its journeys stay, and say so in the log.

    The removal and its tombstone are the seam's (`varsel_state.RegistrationStore.forget_device`):
    with journeys left behind the tombstone records WHY the record went, and with none there is
    nothing a later reader needs explained. What stays here is the structured event the device
    report and the log are joined on — the vocabulary is this module's, so a backend swap cannot
    change one line of it. The plain text line each caller logs predates it and is kept, since a
    saved log query may match on it.

    Returns the removed record (None when the id was unknown) and the journeys left behind.
    """
    record, journeys = _registrations.forget_device(device_id, reason=reason)
    if record is None:
        return None, 0
    logger.info(
        "Device %s lost its push subscription (%s), %d journeys kept",
        device_id,
        reason,
        journeys,
        extra={
            "fields": {
                "event": "varsel_device_gone",
                "device_id": device_id,
                "reason": reason,
                "journeys": journeys,
            }
        },
    )
    return record, journeys


def _drop_device(device_id: str) -> None:
    """Forget a device whose push subscription is gone (404/410). Journeys are kept — the client
    re-attaches its subscription (same device_id) if the user re-enables notifications, and since
    2026-09-04 the service worker does so by itself on `pushsubscriptionchange`. A tombstone
    records the reason (`_forget_device`), or the journeys would later read as registered by
    someone who never turned notifications on."""
    with _state_lock:
        record, _journeys_kept = _forget_device(device_id, "expired")
        if record is not None:
            logger.info("Dropped expired push subscription for device %s", device_id)


def _device_is_fresh(device_id: str) -> bool:
    """True while a device's subscription is younger than the propagation grace window. A 404/410
    for such a subscription is likely a transient push-service delay, not a dead endpoint, so the
    caller should retry later rather than drop it."""
    dev = _registrations.get_device(device_id)
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


def _forget_message(message_key: tuple) -> None:
    """Drop the cached wording for this (place pair, train), and its provenance with it.

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


def _reconcile_caches(pair: tuple[str, str], alerts: list[Alert], now: datetime) -> None:
    """Bring `_alternatives` and `_messages` into line with this cycle's fresh match.

    Takes the PLACE PAIR since 2026-09-12 (KTD9), not the journey definition. Two definitions on
    one corridor therefore reconcile the same entries: whichever of them this cycle established an
    alternative for, the answer stands for the pair, and a definition whose own lookup was refused
    by `impact._MAX_ALTERNATIVE_LOOKUPS` reads its neighbour's rather than silently offering none.

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
            subject_key = _subject_key(pair, alert)
            # The wording key AS IT STANDS, i.e. naming whatever alternative is cached right now.
            # Taken before any pop below, because that is the entry a superseded or below-threshold
            # reading has to forget: once the cache moves, the old key is no longer derivable and
            # the wording would sit there un-droppable until the situation ended.
            stale_key = _message_key(pair, alert, now=now)
            if not alert.alertworthy:
                _alternatives.pop(subject_key, None)
                _forget_message(stale_key)
                continue
            fresh = alert.alternative_journey
            if fresh is not None:
                cached = _alternatives.get(subject_key)
                # Stored unconditionally (the card should show this tick's `at_risk` and this tick's
                # scoring of the replacement), but the wording is only dropped when the advice
                # itself changed -- see _alternative_identity. Since KTD9 that identity is IN the
                # key, so the new key would miss the old entry anyway; the forget is what stops the
                # superseded one occupying memory for the rest of the situation's 12 h life.
                if cached is None or _alternative_identity(cached) != _alternative_identity(fresh):
                    _forget_message(stale_key)
                _alternatives[subject_key] = fresh
                continue
            # getattr, like every other read off the impact response in this module: it is duck-typed
            # (a synthetic provider need not carry the newest field), and this runs on the ingestion
            # thread outside the per-journey guard, where an AttributeError would cost the cycle.
            chose_original = bool(getattr(alert.impact, "original_is_best", False))
            # A verdict supersedes the cached answer outright; silence does not, and only a cached
            # alternative that has itself departed is dropped without one. THE SILENT CASE IS THE
            # COMMON ONE and the one KTD9's key is built around: the cached answer stays, so
            # `_message_key` keeps deriving the same key and the wording is not re-written.
            superseded = chose_original or not _still_catchable(_alternatives.get(subject_key), now)
            if superseded and _alternatives.pop(subject_key, None) is not None:
                _forget_message(stale_key)


# ---- The webhook channel's outgoing side: this cycle's subjects -------------------------------
#
# Everything below turns the SAME match, the SAME collapse and the SAME wording the browser channel
# used into the shape `notification_events` commits. It is the last thing `check_and_notify` does and it
# leaves nothing behind: the result is a return value (KTD3 -- a view merged at match time, never a
# materialization), so `_journeys`, `_notified` and the state blob still carry no client id and a
# rollback to pre-feature code has nothing of this to lose.

#: What a retraction says. Withdrawal means THE FORECAST CHANGED, not that normal service is
#: guaranteed — the store's own `record_below_threshold` docstring makes that the wording's job, and
#: it is the one sentence a traveller could most easily read as an all-clear. Norwegian, like every
#: other traveller-facing string in this module, and deliberately brand-independent for the reason
#: every push wording is: it is the text a comparison would be split by.
_WEBHOOK_WITHDRAWAL_DETAIL = (
    "Vi varsler ikke lenger om denne avgangen. Det betyr ikke at den er garantert i rute."
)
_WEBHOOK_WITHDRAWAL_TITLE = "varselet er trukket tilbake"


def _webhook_delivery_enabled() -> bool:
    """Whether a Notification event carries the model's prose rather than the template.

    :func:`notification_wording.webhook_delivery_enabled` (``NOTIFICATION_WEBHOOK_LLM_ENABLED``,
    unset = off), deliberately NOT :func:`_deliverable_message`, which is the BROWSER delivery gate
    (``VARSEL_LLM_ENABLED``). The two are separate on purpose: the rollout turns the webhook channel
    on with the template wording first and model prose only once matching and delivery qualify, and
    sharing one variable would flip client prose on the moment it goes on for the PWA, in the same
    deploy — precisely the coupling the 2026-08-07 generation/delivery split removed one layer down.

    Read ONCE per cycle by the caller, like ``_record_decisions``' own two gate reads: a flag
    re-read per subject could write for one train and not the next inside one pass.

    A failure answers False — the template — for :func:`_deliverable_message`'s reason: an
    unreadable switch is not evidence that a model's prose should go out.
    """
    try:
        return bool(notification_wording.webhook_delivery_enabled())
    except Exception:
        logger.warning(
            "Could not read the webhook wording-delivery switch; using the deterministic wording",
            exc_info=True,
            extra={"fields": {"event": "notification_wording", "result": "error"}},
        )
        return False


def _webhook_wording(message_key: tuple, *, enabled: bool) -> str | None:
    """The written wording a Notification event may carry, or None for the template.

    Reads :data:`_messages` RAW, which is the whole reason this is not
    :func:`_deliverable_message`: for a definition both channels share, that cache holds the ONE
    wording AE1 asserts, and reading it through the browser's delivery gate would withhold it from a
    client in prd for a reason about ``VARSEL_LLM_ENABLED``.
    """
    if not enabled:
        return None
    with _state_lock:
        return _messages.get(message_key)


def cached_wording_and_alternative(
    pair: tuple[str, str], alert: Alert, *, enabled: bool
) -> tuple[str | None, AlternativeJourney | None]:
    """The poller's cached wording and cached alternative for one (place pair, train), READ ONLY.

    For the journey-keyed public read (2026-09-10), which serves a registered definition what the
    pass already wrote rather than writing again. Takes the PAIR since 2026-09-12 (KTD9), which is
    what the read already holds and what the pass now keys on — so a read for one definition on a
    corridor is served by a wording the pass wrote for another.

    One `_state_lock` read for both, and :func:`_webhook_wording`'s rules for the wording: the RAW
    entry, not through the browser's delivery gate (`enabled` is the webhook channel's gate, as
    there); none for a declared cancellation; None where absent. The cancellation refusal is kept
    even though `cancelled` is now IN the key and a forecast can no longer share an entry with a
    certainty: it costs nothing (the template is the documented correct wording for a declared
    cancellation — «Innstilt», no delay figures) and it is one fewer thing to be wrong about. The
    alternative is returned whatever `enabled` says — it is the Journey Planner's answer and has
    nothing to do with whether a model's prose is delivered — and it is the raw entry too: whether
    it is still catchable is `_journey_model`'s policy, applied by the caller.

    **Never writes**, and that is a stop condition rather than a nicety: pass 1c records
    `ok_cached` for a key already in :data:`_messages`, so an entry a read had written would land
    on `/historikk` under the browser's attribution, and the poller's caches are its own.
    """
    with _state_lock:
        alternative = _alternatives.get(_subject_key(pair, alert))
        wording = (
            None
            if (not enabled or alert.cancelled)
            else _messages.get(_message_key(pair, alert))
        )
    return wording, alternative


def _webhook_withdrawal_wording(
    message_input: dict, from_name: str, to_name: str
) -> tuple[str, str]:
    """Title and body for a retraction, in :func:`notification_wording_for`'s two shapes.

    Not routed through that function, because there is no wording to retract *with*: the template
    body describes the disruption, and a retraction that led with "91 % sjanse for innstilling"
    would be the opposite of what it means. What it does keep is the two structural rules — the
    train named exactly as every other notification names it (:func:`_train_label`, falling back to
    the route when the input names no departure, which is :func:`notification_title`'s own
    fallback), and the route on the body's first line.
    """
    train = _train_label_from_input(message_input)
    if train:
        title = f"{train}: {_WEBHOOK_WITHDRAWAL_TITLE}"
    else:
        title = f"{from_name} → {to_name}: {_WEBHOOK_WITHDRAWAL_TITLE}"
    return _tag_environment(title), f"{from_name} → {to_name}\n{_WEBHOOK_WITHDRAWAL_DETAIL}"


def _webhook_situations(alerts: list[Alert]) -> list[dict]:
    """The contributing situations for one train, operator text included.

    Built from the UNCOLLAPSED alerts about that train rather than from the one
    :func:`one_alert_per_train` kept, because R8 asks for "the contributing situations" and because
    operators routinely publish several messages about one disruption — Vy sent both "Ta neste tog"
    and "Innstilt Drammen–Skøyen" for the same cancelled 15:03. The wording still comes from the
    single most severe one; this is what a client joins on and displays.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for alert in alerts:
        number = alert.situation_number
        if number in seen:
            continue
        seen.add(number)
        out.append(
            {
                "situation_number": number,
                "summary": alert.summary,
                "description": alert.description,
                # False means no SX message named this train at all, so `summary` is text this
                # codebase wrote rather than an operator's. A receiver presenting it as an
                # operator's statement would be making the 2026-08-20 mistake one layer out.
                "has_situation": bool(alert.has_situation),
            }
        )
    return out


def _webhook_payload(
    alert: Alert,
    contributing: list[Alert],
    *,
    message_input: dict,
    title: str,
    body: str,
) -> dict:
    """The event payload: the facts plus the Norwegian prose (R7).

    "Normal receipt needs no second request to us" is a property of what this carries, which is why
    the figures ride along beside the wording rather than behind a lookup.

    Two shapes, split on a declared cancellation exactly as every other writer in this module splits
    on it. When ET has already declared the call cancelled the probabilities are 1.0 by construction
    (``impact._cancelled_prediction``) and the delay figures are **null**, because every one of them
    meant "if it runs" — the same rule ``_deterministic_body`` and the message input follow, and the
    same rule ``event.schema.json`` states for ``phase: confirmed``.
    """
    delay = (
        None
        if alert.cancelled
        else {"typical_min": alert.delay_p50_min, "worst_case_min": alert.delay_p90_min}
    )
    alternative = getattr(alert, "alternative_journey", None)
    dumped: Any = None
    if alternative is not None:
        try:
            # `mode="json"` so the stored document holds JSON primitives: the event is serialized to
            # the wire bytes the signature covers, and a datetime that survived into it would need a
            # custom encoder on a path where a raise costs the delivery.
            dumped = alternative.model_dump(mode="json")
        except Exception:
            logger.debug("Could not serialize an alternative journey for a notification event", exc_info=True)
    return {
        "notification": {"title": title, "body": body, "action": None},
        "call": {
            "stop_place": alert.stop_place,
            "stop_order": alert.stop_order,
            "aimed_departure_time": (
                alert.departure_time.isoformat() if alert.departure_time else None
            ),
            "departs_tomorrow": bool(message_input.get("i_morgen")),
        },
        "probabilities": {
            "impact": alert.impact_probability,
            "cancellation": alert.cancel_probability,
            "delay_ge10": alert.ge10_probability,
            # The operator's own alerting bar, so a client can see WHY this cleared it rather than
            # inferring a threshold from the numbers. One gate serves both surfaces (KTD2).
            "alert_threshold": alert.alert_threshold,
        },
        "delay": delay,
        # A two-value vocabulary derived from what we actually know, `null` when neither is
        # established. New wire vocabulary, so U7's typed client and U8's published contract have to
        # match these two literals rather than invent their own.
        "effect": (
            "cancelled"
            if alert.cancelled
            else "delayed"
            if (alert.delay_p50_min or alert.delay_p90_min)
            else None
        ),
        "situations": _webhook_situations(contributing),
        "alternative_journey": dumped,
    }


def _webhook_observations(
    webhook_by_key: dict[str, list[WebhookRecipient]],
    alerts_by_key: dict[str, list[Alert]],
    match_targets: dict[str, dict],
    message_inputs: dict[tuple[str, str, str], dict | None],
    *,
    webhook_llm: bool,
    wordings: dict[tuple[str, str, str], tuple[str, str]] | None = None,
) -> list[notification_events.WebhookObservation]:
    """This cycle's webhook observations — one per (definition, train), alert-worthy or not.

    Below-threshold trains are included on purpose: a withdrawal is a lifecycle transition and the
    consecutive-below-threshold count that drives it lives on the alert subject, so the observation
    has to reach the store to be counted. ``notification_events`` is what keeps that affordable — it
    reads which subjects stand and spends a transaction only where one can be written.

    The wording, the collapse and the payload are computed once per (definition, train) and shared
    across the recipients on that definition, which is the same economy AE1 asserts for the match
    and the wording: two recipients watching one trip see identical text.

    **One observation per train rather than one subject per recipient since U9.** The wording and
    the payload were always shared by reference; the dataclasses around them were not, so a
    corridor with 100 000 subscriptions and AE3's thirteen departures built 1.3 million of them
    here before `notification_events` saw one — on the ingestion thread, in a pod whose memory
    limit equals its request. The fan-out now happens inside the emit loop, after the standing set
    has said whether a store call can happen at all.
    """
    observations: list[notification_events.WebhookObservation] = []
    unroutable = 0
    failed = 0
    for key, recipients in webhook_by_key.items():
        try:
            unroutable += _observations_for_key(
                observations,
                key,
                recipients,
                alerts_by_key=alerts_by_key,
                match_targets=match_targets,
                message_inputs=message_inputs,
                webhook_llm=webhook_llm,
                wordings=wordings,
            )
        except Exception:
            # Per DEFINITION, the same rule `_reconcile_caches` and `_match` are guarded by: one
            # journey's awkward payload must cost that journey's observations, not the cycle's. What
            # this touches is duck-typed -- an alternative journey from an injected provider, a
            # pydantic dump -- so "it cannot raise" would be an assumption about somebody else's
            # object, and the caller's outer guard would drop every OTHER client's events with it.
            failed += 1
            logger.warning(
                "Could not build webhook subjects for a journey definition; the rest of the "
                "cycle's client work is unaffected",
                exc_info=True,
                extra={"fields": {"event": "notification_emit", "result": "subject_error"}},
            )
    if unroutable:
        # The COUNT and never the document (R16). A subscription with no destination was still
        # matched and still contributed to the shared wording; it simply cannot be routed.
        logger.warning(
            "%d webhook subject(s) name no destination and were not emitted for",
            unroutable,
            extra={"fields": {"event": "notification_emit", "result": "unroutable", "n": unroutable}},
        )
    if failed:
        logger.warning(
            "%d journey definition(s) produced no webhook subjects this cycle",
            failed,
            extra={"fields": {"event": "notification_emit", "result": "subjects_skipped", "n": failed}},
        )
    return observations


@dataclass(frozen=True)
class _ClientWording:
    """What the CLIENT channel told one decision subject about one train, as the log stores it.

    Four fields rather than the (text, source) pair this started as, and the two that were added
    are the ones a COPIED row gets wrong. :func:`record_minted_decisions` builds its row from the
    thinning ledger's copy, and on a thinned cycle that copy came through :func:`decisions.rescored`
    — which refreshes the prediction figures and carries `message_input`, `message_deterministic`
    and `message_status` from the last row actually WRITTEN, up to
    :data:`_DECISION_THINNING_INTERVAL_S` ago. The browser channel never meets that, because
    `pushed` is one of the thinning rule's keep-reasons and so a pushed row is always a fresh
    :func:`decisions.build_row`; a mint is not in `pushed_keys` and therefore lands on whatever row
    the rule left behind. `/meldinger` replays the FIRST pushed row's `message_input` and stamps its
    `decided_at` as `would_send_at`, so the copy would have it replaying a quarter-hour-old document
    beside this cycle's figures — systematically, for every client subject, on the page this channel
    is being scored by.

    `message_input` is the DICT, not the serialized form: the mint is a small set (lifecycle
    transitions only) while an observation is produced for every alert-worthy train on every
    definition with a client on it, so the JSON and the template body are rendered by the reader
    for the subjects that actually minted rather than here for all of them.
    """

    #: The text the client's event carried, without the route line the body leads with.
    message: str
    #: 'llm' or 'deterministic' — which of the two wordings that text is.
    source: str
    #: The model's wording where one was read from the cache, else None. Never a browser-gate
    #: read: `_webhook_wording` answers None when THIS channel's flag is off, which is the
    #: "written but withheld" state and not an absence.
    llm: str | None
    #: The document this cycle would word from — the same object `_record_decisions` stores.
    message_input: dict | None


def _observations_for_key(
    out: list[notification_events.WebhookObservation],
    key: str,
    recipients: list[WebhookRecipient],
    *,
    alerts_by_key: dict[str, list[Alert]],
    match_targets: dict[str, dict],
    message_inputs: dict[tuple[str, str, str], dict | None],
    webhook_llm: bool,
    wordings: dict[tuple[str, str, str], _ClientWording] | None = None,
) -> int:
    """One journey definition's observations, appended to `out`. Returns the unroutable count.

    Split out so the per-definition guard in :func:`_webhook_observations` has something to wrap.

    `wordings` collects a :class:`_ClientWording` per decision subject for U16: what the CLIENT was
    told about this train, as the decision log stores a wording — the text alone, without the route
    line the event body leads with — beside the document it was worded from. It is filled here
    because this is where the choice between the model's text and the template is actually made;
    recovering it from the body afterwards would mean stripping a line by convention, and
    recomputing it would be a second copy of that choice.
    """
    unroutable = 0
    alerts = alerts_by_key.get(key) or []
    if not alerts:
        return 0
    journey = match_targets.get(key) or {}
    # The pair the shared wording is cached under. `match_targets` is built from these keys so the
    # lookup always hits; the fallback is what keeps a missing record costing a template rather than
    # a KeyError inside the caller's per-definition guard.
    pair = _pair(journey) if journey.get("from_stop") and journey.get("to_stop") else ("", "")
    contributing: dict[tuple, list[Alert]] = {}
    for alert in alerts:
        train = _train_key(alert)
        if train is not None:
            contributing.setdefault(train, []).append(alert)
    for winner in one_alert_per_train(alerts):
        train = _train_key(winner)
        if train is None:
            # No train identity, so no stable `alert_id`: two different trains would share one
            # and the receiver's own revision rule would suppress the second recipient's
            # notification. `notification_events.train_ref` refuses the same set and counts it.
            continue
        # TWO KEYS, and they are not interchangeable (KTD9). The pass-local message input is
        # per DEFINITION -- it is the document `_record_decisions` stores on this definition's row
        # -- while the wording is per (pair, train), shared with every other definition on the
        # corridor and with the browser channel.
        message_input = (
            message_inputs.get(_decision_key(key, winner)) or _message_input_from_alert(winner)
        )
        resolved = message_input.get("reise") or {}
        # SERVER-SIDE names only, never a browser registration's `from_name`/`to_name`. Those are
        # up to 120 characters of unauthenticated free text, and on a definition both channels
        # share the browser record is what `match_targets` holds -- so until 2026-09-10 whoever
        # registered a journey on `/varsel-api/journeys` first wrote the route line of a CLIENT's
        # notification about the same trains. `llm_message.build_message_input` already refuses
        # exactly this for the prompt (see the note above its `_canonical_stop_name`), and `reise`
        # is its registry-resolved pair; the bare ref is the bounded fallback when the input had to
        # be rebuilt off the alert (`_message_input_from_alert` carries no `reise`). The cost is
        # stated rather than hidden: a push and an event about one train agree byte for byte only
        # when the traveller's typed name equals the registry's, which is the ordinary case and
        # `test_a_webhook_subject_carries_byte_identical_wording_to_the_web_push` pins it.
        from_name = resolved.get("fra") or journey.get("from_stop") or ""
        to_name = resolved.get("til") or journey.get("to_stop") or ""
        decision_key = _decision_key(key, winner)
        if winner.alertworthy:
            # The cached wording, read once and kept, because it is BOTH the event's body and what
            # U16 records as this client's `message`/`message_source`. Asking `_webhook_wording`
            # twice would be a second read of a cache another thread can change mid-pass.
            cached = (
                None
                if winner.cancelled
                else _webhook_wording(_message_key(pair, winner), enabled=webhook_llm)
            )
            title, body = notification_wording_for(
                message_input,
                from_name,
                to_name,
                # NO CACHED WORDING FOR A DECLARED CANCELLATION. Kept as a belt-and-braces
                # refusal: since KTD9 (2026-09-12) `cancelled` is IN the wording key, so a wording
                # written while the train was merely at risk can no longer share an entry with the
                # reading taken after ET declared it gone -- which is what this line was added for,
                # nothing having forgotten it in between (`_reconcile_caches`' only unconditional
                # forget is the below-threshold one, a declared cancellation stays alert-worthy at
                # (1.0, 1.0), and the write pass short-circuits on `message_key in _messages`).
                # It costs nothing to keep and the reasoning below is still the reason the template
                # is right here. The title is rebuilt from THIS
                # cycle's input, so the event read «Tog L1 07:18 er innstilt» over «91 % sjanse for
                # innstilling, typisk 15-27 min forsinket» beside `delay: null` and
                # `effect: "cancelled"` in the same payload. The template is the documented correct
                # wording here -- `_deterministic_body` renders «Innstilt» and carries no delay
                # figures, which is `impact._cancelled_prediction`'s own rule -- so refusing the
                # cache costs nothing and states the certainty.
                #
                # The BROWSER card's half of the same defect IS closed by the key change: it
                # reads through `_deliverable_message` under the same key, which now carries
                # `cancelled`, so a forecast's prose can no longer head a certainty's card either.
                cached,
                # Always False, and structurally so. The browser's «Bekreftet:» prefix comes off
                # `_pushed_trains`, which is per REGISTRATION and per device — a client has no
                # such record here, and the envelope carries the same fact better:
                # `type=journey.alert.updated` with `phase=confirmed` says a revision replaces
                # an earlier one about this train, which is what the prefix was standing in for.
                confirmed=False,
            )
            if wordings is not None:
                wordings[decision_key] = _ClientWording(
                    message=cached if cached else _deterministic_body(message_input),
                    source="llm" if cached else "deterministic",
                    llm=cached,
                    message_input=message_input,
                )
        else:
            title, body = _webhook_withdrawal_wording(message_input, from_name, to_name)
            if wordings is not None:
                # A retraction has no model behind it by construction (`_webhook_withdrawal_wording`
                # explains why there is no wording to retract WITH), so the source is the template
                # whatever the flag says.
                wordings[decision_key] = _ClientWording(
                    message=_WEBHOOK_WITHDRAWAL_DETAIL,
                    source="deterministic",
                    llm=None,
                    message_input=message_input,
                )
        payload = _webhook_payload(
            winner,
            contributing.get(train, [winner]),
            message_input=message_input,
            title=title,
            body=body,
        )
        situations = tuple(s["situation_number"] for s in payload["situations"])
        routable: list[notification_events.Recipient] = []
        for recipient in recipients:
            if not recipient.client_id or not recipient.destination_id:
                unroutable += 1
                continue
            routable.append(
                notification_events.Recipient(
                    subscription_id=recipient.recipient_id,
                    client_id=recipient.client_id,
                    destination_id=recipient.destination_id,
                    subscription_version=recipient.subscription_version,
                    external_ref=recipient.external_ref,
                    test=recipient.test,
                )
            )
        if not routable:
            # An observation nobody can be routed to is not an observation: the count above has
            # already recorded it, and an empty fan-out would cost the emit loop a train's sort key
            # and a `TrainRef` for nothing.
            continue
        out.append(
            notification_events.WebhookObservation(
                recipients=tuple(routable),
                alertworthy=bool(winner.alertworthy),
                cancelled=bool(winner.cancelled),
                situations=situations,
                operator=winner.operator,
                line=winner.line,
                journey_ref=winner.jkey,
                stop_place=winner.stop_place,
                stop_order=winner.stop_order,
                departure_time=winner.departure_time,
                title=title,
                body=body,
                payload=payload,
                expected_departure_time=_expected_departure_of(winner),
                decision_key=decision_key,
            )
        )
    return unroutable


def _expected_departure_of(alert: Alert) -> datetime | None:
    """When the train is expected to actually leave: aimed time plus the typical delay predicted
    for it -- `impact._expected_departure`'s rule, restated on the Alert because the matcher's
    `CallPred` is gone by now. This is what R12's "the departure" has to mean for the webhook
    event's validity: the matcher keeps a late train past its aimed time on exactly this figure
    (`catchable_after`), and an event that expired at the aimed time was never attempted for it.
    A declared cancellation carries no delay figure, so its expected departure is its aimed one."""
    if alert.departure_time is None:
        return None
    return alert.departure_time + timedelta(minutes=max(alert.delay_p50_min or 0, 0))


@dataclass(frozen=True)
class PassOutcome:
    """What one notification pass produced for the steps that run AFTER it.

    A RETURN VALUE, not a module-level snapshot, and that is a decision rather than a style: a
    module-level ledger would need registering in `check_and_notify`'s per-cycle stale sweep and in
    `test_varsel_api._reset_state`, and a leaked half-cycle snapshot reads exactly like the give-up
    bug those sweeps exist to prevent. Additive, so every existing caller — `main`'s poller before
    this change, and every suite — keeps working by ignoring it.

    Handing it to `main` rather than acting on it here is what makes KTD6's ordering claim
    structural: the client emit and delivery steps run in their own `main._phase`, *after this
    function has returned*, so a raise in either cannot unwind `_record_decisions` or `_save_state`
    at all rather than merely being sequenced after them.
    """

    webhook_observations: tuple[notification_events.WebhookObservation, ...] = ()
    #: What the CLIENT channel would tell each decision subject, as the log stores a wording: see
    #: :class:`_ClientWording`. Read by :func:`record_minted_decisions` after the emit step says
    #: which subjects were actually notified — so this is a pass-local lookup rather than a claim
    #: that anything was sent.
    #:
    #: A mapping rather than a field on the observation because the reader is keyed the other way
    #: round: emit reports decision keys, and walking every observation to find one would make the
    #: mint write O(observations x mints) on exactly the corridor U9 emptied of per-recipient work.
    webhook_wordings: Mapping[tuple[str, str, str], _ClientWording] = field(default_factory=dict)


def record_minted_decisions(outcome: "PassOutcome", minted: Sequence[tuple[str, str, str]]) -> int:
    """Mark the decision subjects this cycle MINTED a client notification for. Returns rows written.

    U16's answer to the one ordering problem KTD4 and KTD6 create between them, and it is worth
    stating as a problem before it is stated as a mechanism. `pushed` has to mean "a notification
    was issued on this cycle" for both channels (KTD4). The browser half is easy: loop 2 sends
    before `_record_decisions` runs, so the mark is in hand when the row is built. The client half
    is not: emit runs in `main`'s own phase AFTER `check_and_notify` has returned, deliberately, so
    that a raise in it cannot unwind the decision log or the state save at all (KTD6). U9 wrote the
    choice down on `EmitReport.minted`: a cycle's mints reach the NEXT cycle's rows, or the row is
    written somewhere else. This is the second.

    So: one extra row per newly-notified subject, written here, in the same phase as the emit that
    produced it. It is a COPY of the ledger's own row — the one `_record_decisions` either wrote or
    restated minutes ago — with `pushed` flipped and the client's five wording columns restated from
    this cycle. Copying the rest rather than rebuilding it is the point: a second builder would be a
    second definition of what a decision row is, and it would drift the first time `build_row`
    gained a field.

    **The five that are restated are restated because the copy would be STALE, not merely
    approximate.** On a thinned cycle the ledger's row came through `decisions.rescored`, which
    refreshes the prediction figures and carries the message columns from the last row actually
    written — so `message_input` can be up to `_DECISION_THINNING_INTERVAL_S` old beside figures
    that are not. `/meldinger` replays the first pushed row's `message_input` and stamps that row's
    `decided_at` as `would_send_at`, so a copied input would have the page replaying a
    quarter-hour-old document under this cycle's timestamp. The browser channel cannot meet this at
    all: `pushed` is one of the thinning rule's keep-reasons, so a pushed row there is always a
    fresh `build_row`. A mint is deliberately NOT in `pushed_keys` — that is what stops a subject
    holding two PUSHED rows at one instant — so it lands on whatever row the rule left behind, and
    restating is what makes `decided_at`, the figures and the input describe one cycle.

    **What it costs, stated where it is paid.**

    * A mint cycle can hold TWO rows for one subject at one `decided_at` — the pass's and this one.
      `varsel_decision_groups` is a MIN/MAX/LOGICAL_OR/first-pick aggregate throughout, so the only
      figure that moves is `n_decisions`, which already counts rows written rather than cycles
      scored and says so. The row is skipped entirely where the ledger's row already says `pushed`,
      so a subject never has two PUSHED rows at one instant and the view's "first pushed row"
      wording pick stays deterministic.
    * A lost write loses the mark for good; no later cycle re-derives it, because the mint is a
      transition and the next cycle sees an unchanged phase. That is exactly the browser channel's
      existing hole — `pushed_keys` is pass-local and a short write commits no ledger entry — so
      this adds no new class of loss, and `decisions.record` logs the short write either way.
    * `llm_provider`, `llm_model` and — where the rule below leaves it — `message_status` are
      CARRIED, so they describe the browser pass's last write for this definition rather than the
      client's wording. The one that could mislead is `message_status`, because `ok` claims a
      same-input pair and `message_wording_comparison.sql` filters on exactly that. So a row whose
      restated input DIFFERS from the one the ledger was carrying is demoted to `ok_cached`, which
      is precisely what that value already documents — «message_llm from an EARLIER cycle's input,
      message_deterministic from this one». A row whose input did not move keeps its `ok`, and that
      is the ordinary case rather than a corner: most mints are the `predicted` transition, and a
      transition is one of the thinning rule's keep-reasons, so the pass wrote that row fresh on
      this very cycle.
    * A WITHDRAWAL mints too, and it marks a row whose `alertworthy` is false. That combination
      cannot arise on the browser channel and it is not an error: a retraction is a notification the
      client received, and a withdrawal can only exist where something was standing, so the
      SUBJECT's `ever_alerted` is true regardless. The view quotes the FIRST pushed row, which is
      the alert's rather than its retraction's.

    Runs on the poller thread inside `main`'s `notification_webhooks` phase and swallows everything,
    like every other decision-log path: a client-side bookkeeping row must never cost a cycle.
    """
    if not minted:
        return 0
    try:
        import decisions

        if not decisions.enabled():
            return 0
        rows: list[dict] = []
        stamped: list[tuple[tuple[str, str, str], dict]] = []
        missing = 0
        now = datetime.now(UTC)
        with _state_lock:
            # One take of the lock for the whole pass, `_record_decisions`' rule: the ledger is read
            # by the request path's own sweep and this runs on the poller thread.
            entries = {key: _decision_ledger.get(key) for key in minted}
        for key in minted:
            entry = entries.get(key)
            if entry is None or entry.get("row") is None:
                # Two causes and one treatment. The subject retired between the match and the mint
                # (its situation ended, or the train stopped being catchable), so the sweep has
                # already popped it and written its closing row — or its very first row was lost to
                # a short write, which commits no ledger entry, so there has never been a row to
                # copy. Counted rather than warned per subject: at this population a per-row line is
                # a log nobody reads.
                missing += 1
                continue
            if entry["row"].get("pushed"):
                # A Web Push already went out for this very subject on this cycle, so the fact
                # `pushed` carries is recorded and a second row would only add a second PUSHED row
                # at one `decided_at` — which is what makes the view's first-pushed wording pick
                # ambiguous. The browser wording is the one the page then quotes; `channel` says a
                # client was there too.
                continue
            row = {**entry["row"], "pushed": True}
            wording = outcome.webhook_wordings.get(key)
            if wording is not None:
                try:
                    deterministic = _deterministic_body(wording.message_input or {})
                except Exception:
                    # Guarded per subject, `_record_decisions`' own rule one function along: one
                    # awkward payload costs that subject's mark, never the pass's. The outer
                    # `except` would cost every mint on the cycle.
                    logger.warning(
                        "Could not word the client's template for %s; its mark is skipped",
                        key[0],
                        exc_info=True,
                        extra={"fields": {"event": "varsel_decision_write",
                                          "result": "mint_no_wording"}},
                    )
                    continue
                row.update({
                    "message": wording.message,
                    "message_source": wording.source,
                    # NOT `wording.llm` alone. `_webhook_wording` answers None when THIS channel's
                    # flag is off while a model wording sits in the cache, which is the documented
                    # "written but withheld" state (`message_source='deterministic'` AND
                    # `message_llm IS NOT NULL`) — and blanking it would delete that population
                    # from the client's half of the log. So the cached text wins where there is
                    # one, and the pass's own answer stands where there is not.
                    "message_llm": wording.llm or entry["row"].get("message_llm"),
                    "message_deterministic": deterministic,
                    "message_input": _message_input_json(wording.message_input),
                })
                if (
                    row.get("message_llm")
                    and row.get("message_status") == "ok"
                    and row["message_input"] != entry["row"].get("message_input")
                ):
                    # THE INPUT COMPARISON IS THE WHOLE RULE, and the obvious shorter version --
                    # "a mint row carrying a model wording reads `ok_cached`" -- is wrong on the
                    # ordinary case. Most mints are the `predicted` transition, whose cycle is a
                    # thinning keep-reason, so the pass wrote a fresh row minutes-free of staleness
                    # and its `ok` is exactly true: `message_llm` and the `message_input` beside it
                    # ARE the same-input pair the value claims, which is what
                    # `message_wording_comparison.sql` filters on. Demoting those would retire the
                    # client channel from that population almost entirely. Only where the restated
                    # input DIFFERS from the one the ledger was carrying has the claim stopped
                    # holding, and `ok_cached` is the value that already documents precisely that
                    # asymmetry. Both sides went through `_message_input_json`, so byte-equality is
                    # the comparison and not an approximation of one.
                    row["message_status"] = "ok_cached"
            rows.append(row)
            stamped.append((key, row))
        if not rows:
            if missing:
                logger.info(
                    "%d minted subject(s) had no row to mark (retired, or never written)",
                    missing,
                    extra={"fields": {"event": "varsel_decision_write", "result": "mint_no_row",
                                      "n": missing}},
                )
            return 0
        accepted = decisions.record(rows, beat=_heartbeat)
        if accepted < len(rows):
            # No rollback to do and nothing to put back: unlike a closing row, a mint row describes
            # an instant rather than a subject's last look, and the ledger entry it was copied from
            # is untouched above. Saying so is the whole treatment.
            logger.warning(
                "The mint marks were short (%d of %d rows accepted); those subjects read as not "
                "notified on this cycle",
                accepted,
                len(rows),
                extra={"fields": {"event": "varsel_decision_write", "result": "mint_short",
                                  "n_rows": len(rows), "n_accepted": accepted}},
            )
            return accepted
        with _state_lock:
            for key, row in stamped:
                entry = _decision_ledger.get(key)
                if entry is None:
                    # Retired between the read above and here — one cycle's sweep runs on this same
                    # thread, so this is only reachable through the request path's own deletes.
                    continue
                entry["row"] = row
                entry["written"] = True
                entry["written_at"] = now
        return accepted
    except Exception:
        logger.warning(
            "Could not record this cycle's mint marks; the events themselves are unaffected",
            exc_info=True,
            extra={"fields": {"event": "varsel_decision_write", "result": "mint_error"}},
        )
        return 0


@dataclass
class _WriteSubject:
    """One (place pair, train) the cycle may write a wording for — the write queue's unit (KTD9).

    Was a six-tuple until 2026-09-12, when it grew two things a tuple was the wrong shape for: the
    entry is now BUILT ONCE and ADOPTED by later definitions on the same corridor, and it carries
    two different keys (`message_key` is pair-and-advice grained, `subject_key` is the coarse train
    slot the two retry budgets are charged to).

    `key` and `journey` are the REPRESENTATIVE definition — whichever one created the entry, with a
    browser definition taking it over from a webhook-only one. One backend call now serves every
    definition on the pair, so exactly one of them can appear as the `journey_digest` in the prompt
    audit record; it is the one whose row could carry that record anyway, and the others record the
    shared wording with no call of their own, which is the shape `ok_cached` already has.

    `browser` is the P1 discriminator and decides three things at once: which wording policy runs,
    whether the poller's own `llm_message` latch is consulted, and whether the call claims the
    browser allowance or only the clock. True as soon as ANY browser registration on the pair
    contributed this train.
    """

    pair: tuple[str, str]
    key: str
    subject: "Alert"
    journey: dict
    message_key: tuple
    subject_key: tuple
    message_input: dict | None
    browser: bool
    attempts: int = 0
    errors: int = 0


def _write_subject(
    pair: tuple[str, str],
    key: str,
    subject: "Alert",
    journey: dict,
    now: datetime,
    *,
    own_input: dict | None,
    browser: bool,
) -> _WriteSubject:
    """Build a queue entry, with the message input RECONCILED against the effective alternative.

    The wording key names the alternative `_alternatives` holds for this (pair, train) — the
    effective one — while a definition's own alert carries whatever ITS `/impact` call established,
    which can be nothing at all: `impact._MAX_ALTERNATIVE_LOOKUPS` bounds one call, so on a corridor
    the lookup a neighbour paid for is routinely the only answer there is. Prompting from the
    definition's raw input would then write text that does not mention the replacement train the
    key claims, and the next cycle would find that entry already written and never correct it.

    So the input is rebuilt off an alert carrying the effective alternative, and only when the two
    actually differ: the identity comparison is `_alternative_identity`'s, the same one that decides
    whether a cached wording still stands, and in the ordinary case it matches and the pass-local
    document is reused unchanged.
    """
    subject_key = _subject_key(pair, subject)
    with _state_lock:
        effective = _alternatives.get(subject_key)
    own = subject.alternative_journey
    same = (own is None and effective is None) or (
        own is not None
        and effective is not None
        and _alternative_identity(own) == _alternative_identity(effective)
    )
    message_input = (
        own_input
        if same
        else _message_input(journey, subject.model_copy(update={"alternative_journey": effective}))
    )
    return _WriteSubject(
        pair=pair,
        key=key,
        subject=subject,
        journey=journey,
        message_key=_message_key(pair, subject, now=now),
        subject_key=subject_key,
        message_input=message_input,
        browser=browser,
    )


class _LockedPassBudget(notification_wording.PassBudget):
    """`notification_wording.PassBudget` with a lock, because pass 1c is `_LLM_WORKERS`-wide.

    That class documents itself as "not thread-shared: one pass, one counter", which is true of
    `/meldinger`'s own sequential use and false here — `self.left -= 1` is a read-modify-write and
    the GIL does not make it atomic. Unlocked, the ceiling could be overspent by up to one call per
    worker. Small, and worth eight lines to remove: the shape is `_WriteBudget.claim`'s, which every
    reader of this file already knows.
    """

    def __init__(self, limit: int | None = None) -> None:
        super().__init__() if limit is None else super().__init__(limit)
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            return super().take()


class _WriteBudget(reserved_budget.ReservedBudget):
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
        super().__init__(max_calls, deadline, _WORST_CASE_CALL_S)

    def claim_clock(self) -> bool:
        """Reserve the wall clock WITHOUT spending the call allowance. What a webhook subject claims.

        `notification_wording.PassBudget`'s docstring states the split and the reason, and it is not
        a nicety: the CLOCK is a property of the pass — two surfaces writing inside one pass cannot
        each have their own, and the whole point of the reservation is that the deadline bounds when
        the pass ENDS — while the COUNT is a property of the surface. Merged, a client with a few
        hundred subscriptions would spend :data:`_MAX_LLM_CALLS_PER_CYCLE` on client wordings and
        silently retire the browser ones, which is that constant's own lesson read the other way
        round (raising the count without the width just moves the binding constraint to the clock).
        A client call is charged to `notification_wording`'s per-pass budget and its caller-keyed
        trailing hour/day instead.

        `refused` is still incremented, so the one summary line per cycle reports what the clock
        cost whichever surface paid it.
        """
        with self._lock:
            if monotonic() + self._worst_case >= self._deadline:
                self.refused += 1
                return False
            return True


def check_and_notify(by_situation: dict[str, list]) -> PassOutcome:
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

    Written messages are one per (PLACE PAIR, TRAIN) a push is still coming for — the grain loop 2
    delivers at (:func:`_pending_pushes`) collapsed onto the corridor (KTD9), so two definitions of
    one trip with different windows cost one call and read the same sentence. Bounded by
    :data:`_MAX_MESSAGES_PER_JOURNEY` per pair and by :data:`_MAX_LLM_CALLS_PER_CYCLE` for the
    cycle. Trains beyond either cap get the deterministic wording this tick rather than delaying the
    poller thread.

    **Scoring and recording run even when push is disabled**; only delivery is gated. The decision
    log's guarantee is "every decision, not every push", and an early return here quietly broke it:
    an environment without VAPID keys recorded nothing at all, so /historikk looked like a broken
    deployment rather than an un-provisioned one.

    **Wording depends on a deliverable RECIPIENT, not on a VAPID keypair** (KTD4, 2026-09-08). This
    used to skip the whole writing pass on ``push_enabled()``, and the reason it gave was right —
    writing traveller-facing prose nobody can receive is spend with no reader — while the condition
    was wrong: it read "no keypair" as "no reader". A webhook-only deployment, and tst which holds no
    keypair at all, therefore wrote nothing and recorded ``message_status='push_disabled'`` on every
    row. The gate is now the condition it was reaching for: at least one deliverable recipient on
    any channel — browser push enabled, or a webhook subscription with a delivery still pending for
    that train. A journey with no deliverable channel still writes nothing, so
    ``message_status='push_disabled'`` keeps its meaning for browser subjects.

    **Webhook subscriptions are registrations with a webhook channel** (KTD3), merged into pass 1a
    under :func:`journey_key` so one match and one wording serve both channels. It is a view, not a
    materialization: `_journeys`, `_notified` and the state blob never carry a client id. Note
    `_record_decisions` is handed the BROWSER grouping only (KTD10) — the decision log is published
    on public hosts, and a webhook-only route therefore produces no row at all, which is KTD12's
    recorded limitation rather than an oversight.

    **The webhook channel's writes and sends happen AFTER this returns** (KTD6). What this function
    produces for them is a :class:`PassOutcome` — one subject per (subscription, train) with the
    wording already resolved — which `main` hands to its own poller phase. Returning the work rather
    than doing it is strictly stronger than KTD6's literal placement on KTD6's own stated reason: a
    raise in a step that runs after this function has returned cannot unwind `_record_decisions` or
    `_save_state` at all, rather than merely being sequenced after them, and it puts the step under
    `main`'s existing per-step `try/except` and `_phase` isolation. Same thread, same cycle, same
    process either way, so nothing KTD6 settled with the user is touched. The return value is
    additive: every caller that ignores it behaves exactly as before.

    **Writing a wording and sending it are separate switches** (2026-08-07). A message is written
    whenever a backend can be tried (``llm_message.generation_enabled``); ``VARSEL_LLM_ENABLED``
    decides only which of the two wordings the traveller receives, on both surfaces, through
    :func:`_deliverable_message`. So prd — delivery off — writes the model's version, pushes the
    deterministic one, and records both, which is the only way the two can be compared on prd's own
    traffic instead of on dev's. Note the recipient gate above is the wider one: no deliverable
    recipient means no write at all, delivery off means write but send the template.
    """
    push_on = push_enabled()
    # Read once for the whole cycle: whether a Notification event carries the model's prose. It decides
    # both whether a webhook-only definition is written for at all and which wording its event
    # carries, and one pass must not answer that two ways.
    webhook_llm = _webhook_delivery_enabled()
    with _state_lock:
        journeys = _registrations.all_journeys()
        # Prune notified entries and cached messages whose situation is no longer live. In-place
        # updates (not `-=`) so the module globals stay globals rather than becoming function-locals.
        live = set(by_situation.keys())
        # A train that left is done being pushed about (same grace as journey_windows, so a train
        # still being advised as delayed-but-catchable keeps its entry), and the daily ledger
        # forgets yesterday. All three are the seam's, because all three are registration state --
        # the caches below are this process's own and stay here.
        oslo_today = datetime.now(_OSLO).date().isoformat()
        stale_marks = _registrations.prune(
            live_situations=live,
            trains_gone_by=datetime.now(UTC) - _WINDOW_CATCH_UP,
            oslo_today=oslo_today,
        )
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
        # NOTHING FOR THE WEBHOOK CHANNEL SWEEPS HERE, and that is a property worth stating rather
        # than an omission. The client snapshot loaded below is a pass-local list, and Firestore is
        # the only dedup ledger for that channel (KTD3), so this pass holds no notification state to go
        # stale. Anything a later unit adds at module level -- a per-cycle counter, a budget -- goes
        # here AND in `test_varsel_api._reset_state`: a leaked half-spent budget is indistinguishable
        # from a give-up bug. The per-destination delivery skip is the deliberate exception and must
        # stay pass-local, or one failure pauses a destination permanently (KTD6).
        changed = bool(stale_marks)

    # The decision log's own sweep, on the same liveness test one line of reasoning above -- but it
    # RETURNS rather than dropping state, because a thinned subject's last look has not been
    # written yet (KTD15). Outside the lock block so the subjects travel as a pass-local list to the
    # cycle's single decision-log write, which is the rule every other write here follows: no I/O
    # under `_state_lock`, and one insert per cycle. `(key, entry)` pairs rather than bare rows:
    # retiring pops the subject, so a closing row the write loses has to be restorable or it is
    # gone for good.
    closing_subjects = _retire_decision_subjects(live=live)

    # ---- The one client read: BEFORE pass 1a, and before the write budget's clock starts -----
    #
    # KTD6: the load necessarily precedes 1a, because that is what webhook subscriptions are matched
    # from, and it is the ONLY client read passes 1a and 1b depend on. Placed outside the
    # `_state_lock` block above (no store I/O under that lock, `_prepare_alert`'s rule) and before
    # `_WriteBudget` is constructed below, so a slow store spends the cycle's own tail rather than
    # silently eating the 30 s wording ceiling. Empty list when the channel is off, unreachable or
    # slow -- every failure shape is inside the helper.
    webhook_recipients = _load_webhook_recipients()

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

    # The same grouping for the webhook channel, and the merged set pass 1a actually matches.
    # `by_journey_key` stays BROWSER-ONLY, but what that now buys is narrower than it was: it is one
    # of the two inputs to `_recipient_view`, whose job is to say which CHANNEL a definition belongs
    # to. Until 2026-09-13 it was what `_record_decisions` iterated, so a webhook-only definition was
    # matched, worded and notified about and left no row anywhere (KTD10, while /historikk and
    # /meldinger were public). Since U16 every matched definition is recorded, at the same definition
    # grain and with a channel and two counts; no client identity goes with it.
    #
    # `setdefault` order matters: the browser record wins for a shared key. A browser registration
    # carries `from_name`/`to_name`, the service caveat and the consent version, none of which the
    # shared work reads -- but `_record_decisions` and `_notification_payload` do, and they must keep
    # seeing the record the traveller actually registered rather than a client's projection of it.
    webhook_by_key: dict[str, list[WebhookRecipient]] = {}
    for recipient in webhook_recipients:
        webhook_by_key.setdefault(journey_key(recipient.journey), []).append(recipient)
    match_targets: dict[str, dict] = dict(by_journey_key)
    for _key, _recipients in webhook_by_key.items():
        match_targets.setdefault(_key, _recipients[0].journey)

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
    # The WEBHOOK surface's own call allowance, spent inside this same pass and therefore under the
    # same wall clock -- `notification_wording.PassBudget`'s docstring states that contract and this
    # is the caller it was written for. The COUNT is deliberately separate from
    # `_MAX_LLM_CALLS_PER_CYCLE`: the clock is a property of the pass and two surfaces writing inside
    # one pass cannot each have their own, while the count is a property of the surface, and merging
    # them would let a busy client morning silently retire the browser wordings.
    webhook_budget = _LockedPassBudget()
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
    # Pass-local, for the same reason `PassOutcome` is a return value: a module-level counter would
    # need registering in the stale sweep and in `_reset_state`, and a leaked one reads exactly like
    # a warm that is failing now. Appended to from `_MATCH_WORKERS` threads, which needs no lock --
    # `list.append` is atomic and the only thing read off it is the length.
    warm_failures: list[int] = []

    def _match(item: tuple[str, dict]) -> tuple[str, list[Alert]]:
        key, journey = item
        try:
            # `cycle_now`, never the worker's own wall clock (U2). Three decisions about the
            # traveller ride on this instant -- which windows are open, which trains can still be
            # caught, and the `decided_at` the decision log records -- and sampled per journey they
            # disagree by however long the pass takes, which at 100 000 subscriptions is seconds.
            # It is also what lets `impact`'s per-generation window memo be SHARED: a memo keyed on
            # a window each caller derived from its own clock is shared by nobody.
            alerts = match_journey(journey, cycle_now, with_alternative=True)
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
        # The journey-alerts read's warm (U15), in its OWN try and deliberately not inside the one
        # above: that one answers an exception with empty alerts for the journey, which is the right
        # answer for a match that failed and the wrong one for a warm that did -- it would drop a
        # registered traveller's alerts for the whole cycle to save a cache entry. The read is a
        # READER of this pass, never a step of it, and a warm that raises costs one figures miss.
        warmer = _figures_warmer
        if warmer is not None:
            try:
                warmer(key, journey, alerts, cycle_now)
            except Exception:
                # Counted, not logged per definition: at a hundred thousand registrations a
                # per-definition warning is a hundred thousand lines for one cause, which is how a
                # real warning stops being read (`varsel_webhook_load`'s `unroutable` lesson). The
                # traceback still goes to DEBUG, once per failure.
                logger.debug("Journey-alerts figures warm raised", exc_info=True)
                warm_failures.append(1)
        return key, alerts

    matched: list[tuple[str, list[Alert]]] = []
    # The five sub-pass spans (R16) start here. `varsel_notify` showed as one opaque phase with the
    # decision-log insert under it and nothing else, so "which of the five is the cycle in?" could
    # only be answered by adding log lines. Opened unconditionally, including at zero definitions:
    # a pass that did nothing is a reading, and a missing span reads as an uninstrumented one.
    with _span(
        "varsel_match",
        definitions=len(match_targets),
        recipients=len(webhook_recipients),
        workers=min(_MATCH_WORKERS, len(match_targets)) if match_targets else 0,
    ):
        if match_targets:
            with ThreadPoolExecutor(max_workers=min(_MATCH_WORKERS, len(match_targets))) as pool:
                matched = _drain(
                    pool.map(_match, list(match_targets.items())), len(match_targets)
                )
    if warm_failures:
        logger.warning(
            "The journey-alerts figures warm failed for %d of %d definitions; those reads compute",
            len(warm_failures),
            len(match_targets),
            extra={"fields": {"event": "notification_pull", "result": "warm_failed", "n": len(warm_failures)}},
        )
    # A pass boundary is a progress point in exactly `main._phase`'s sense, and the two beats inside
    # `_drain` above deliberately skip the last chunk so this is the one that reports the pass done.
    _heartbeat()

    # ---- Pass 1b: reconcile, and decide what to write for -- SEQUENTIALLY ----------------------
    #
    # Deliberately not folded into the pass above. Everything here mutates module state
    # (`_alternatives`, `_messages`, `_message_attempts`, `_message_meta`) or reads it to make a
    # decision, and doing that on one thread is worth far more than the microseconds it costs: none
    # of it blocks on anything. The split is behaviour-preserving -- `_reconcile_caches` for one
    # journey never reads another journey's alerts, and `_pending_pushes` reads `_notified`, which
    # only loop 2 writes.
    with _span("varsel_reconcile", matched=len(matched)):
        # ONE ENTRY PER (PLACE PAIR, TRAIN), not per (definition, train) -- KTD9. A corridor with
        # five hundred registrations is one wording and one backend call per disrupted train, and
        # every traveller on it reads the same sentence. Keyed on `_train_key` where the alert
        # names a train and on the pair-grained subject otherwise, which is the one case
        # `one_alert_per_train` also refuses to merge: an alert that cannot be tied to a departure
        # is never folded into another, because showing one card too many is a much smaller error
        # than hiding an unrelated disruption.
        queued: dict[tuple, _WriteSubject] = {}
        for key, alerts in matched:
            journey = match_targets[key]
            pair = _pair(journey)
            alerts_by_key[key] = alerts
            # WHICH WORDING POLICY writes for this train, and since KTD9 the discriminator is the
            # PAIR rather than the definition: `by_journey_key` is browser-only by construction
            # (KTD10), so "some contributing definition is in it" is exactly "a browser
            # registration on this pair needs this train". A train no browser registration
            # contributed must go through `notification_wording.WEBHOOK`, because the browser
            # policy's prompt audit record carries the traveller's origin, destination and
            # departure time at DEBUG wherever VARSEL_LLM_LOG_PROMPTS is on -- which dev is -- and
            # R16 forbids that for client data. WEBHOOK suppresses that record in CODE rather than
            # by an environment variable, drops `message` from the INFO record, and latches against
            # its own holder. Its FIRST bound is the webhook delivery switch, so with client prose
            # undelivered — every deployment today — the writer is still invoked (AE10: a client
            # registration with no VAPID keypair matches and is written for) and answers with the
            # template, asking no backend and charging nothing.
            #
            # A SHARED pair stays on the browser policy, unavoidably and by design: AE1 pins one
            # match and one wording between the two channels, so there is one call and it is the
            # browser's. The prompt record for that route would exist with no webhook channel at
            # all, because a browser registration holds the same route -- so R16's claim is about
            # webhook-only routes plus every other client field, and U8 publishes exactly that.
            # Widening it from the definition to the pair widens the browser side, never the
            # client one: a pair no browser registration is on is still written under WEBHOOK.
            browser = key in by_journey_key
            # PER DEFINITION, and deliberately not shared. `build_message_input` reads only
            # `from_stop`/`to_stop` off the record, so two definitions on one pair do produce the
            # same document -- but this is the document `_record_decisions` stores on each row, and
            # a row's `message_input` must be the one built beside its own alert.
            for alert in alerts:
                message_inputs[_decision_key(key, alert)] = _message_input(journey, alert)
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
                _reconcile_caches(pair, alerts, cycle_now)
            except Exception:
                logger.warning(
                    "Cache reconciliation failed for journey %s; keeping this cycle's cached "
                    "alternatives and messages",
                    key,
                    exc_info=True,
                    extra={"fields": {"event": "varsel_reconcile_error"}},
                )
            # ONE WORDING PER TRAIN THIS CYCLE WILL NOTIFY ABOUT, not one per journey (2026-08-21).
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
            # It is gated on `push_on` because the CARD is the reader it exists for, and the card is
            # the browser channel; a webhook subject with nothing pending has already been told.
            #
            # KTD4's gate, expressed as three lines rather than a boolean somewhere else. `jids` are
            # withheld when push is off -- see `_pending_pushes` for why that half is load-bearing --
            # so `pending` is exactly "a recipient on some channel still needs telling about this
            # train", and `push_on or pending` is exactly "some channel can deliver". With no client
            # recipients (every deployment until the store lands, and every existing test) both
            # predicates reduce to what they were: `pending` is the browser list, and the guard below
            # is the old `if not push_on: continue`.
            pending = _pending_pushes(
                jids_by_key.get(key, ()) if push_on else (), alerts, webhook_by_key.get(key, ())
            )
            if not push_on and not pending:
                # No deliverable recipient on ANY channel: a keypair-less deployment with no client
                # subscription pending for this journey. The decision is still recorded below, with the
                # deterministic wording, and `message_status` still reads `push_disabled` for it.
                continue
            subjects = pending or (
                [a for a in one_alert_per_train(alerts) if a.alertworthy][:1] if push_on else []
            )
            # NO PER-DEFINITION SLICE HERE any more: `_MAX_MESSAGES_PER_JOURNEY` is a PAIR's cap
            # since KTD9 and is applied below, once the corridor's definitions have all been seen.
            # Sliced here it multiplied by the number of registrations on a corridor, which is
            # exactly the number this plan scales.
            for subject in subjects:
                train = _train_key(subject)
                queue_key = (pair, train) if train is not None else ("_untied", _subject_key(pair, subject))
                entry = queued.get(queue_key)
                if entry is None:
                    queued[queue_key] = _write_subject(
                        pair, key, subject, journey, cycle_now,
                        own_input=message_inputs.get(_decision_key(key, subject)),
                        browser=browser,
                    )
                elif browser and not entry.browser:
                    # A browser registration on the pair takes the entry over: the policy, the
                    # latch and the call allowance all follow `browser`, and the representative
                    # definition has to be one whose own row could carry the prompt record.
                    queued[queue_key] = _write_subject(
                        pair, key, subject, journey, cycle_now,
                        own_input=message_inputs.get(_decision_key(key, subject)),
                        browser=True,
                    )

        # SOONEST DEPARTURE FIRST, ACROSS PAIRS -- the ordering is from 2026-08-21 (it used to be
        # `_journeys` insertion order, so a bound cap handed the budget to whichever travellers
        # registered earliest, every cycle, forever) and KTD9 only changes the unit it orders from
        # a (definition, train) to a (pair, train).
        #
        # An alternative was considered and NOT taken, and is recorded here because it is an open
        # product question rather than a settled one: spending the clock in DESCENDING SUBSCRIPTION
        # COUNT per (pair, train), with the departure as tie-break, so a bad morning's bounded
        # number of model wordings goes to the trains with the most recipients. It is refused for
        # now because it inverts the premise the product rests on -- a push about a train that has
        # left is worthless, so a busy train three hours out must not beat a quiet one leaving in
        # five minutes -- and because departure order is the rule `impact._MAX_ALTERNATIVE_LOOKUPS`
        # already spends its own budget by. Decide it with a measurement, not with this reasoning.
        #
        # `.timestamp()` rather than the datetime itself: the key must be total, and comparing an aware
        # datetime with a naive one raises. Trains with no departure time sort last and keep their
        # relative order (Python's sort is stable), which is `_pending_pushes`' severity order.
        ordered = sorted(
            queued.values(),
            key=lambda entry: (
                entry.subject.departure_time is None,
                entry.subject.departure_time.timestamp() if entry.subject.departure_time else 0.0,
            ),
        )
        # `_MAX_MESSAGES_PER_JOURNEY` PER PAIR, applied to the sorted list so the slots go to the
        # soonest departures on each corridor. Trains past it take the deterministic wording and
        # are recorded `not_selected`, exactly as a train past the per-definition cap was.
        per_pair: dict[tuple[str, str], int] = {}
        write_queue: list[_WriteSubject] = []
        for entry in ordered:
            spent = per_pair.get(entry.pair, 0)
            if spent >= _MAX_MESSAGES_PER_JOURNEY:
                continue
            per_pair[entry.pair] = spent + 1
            with _state_lock:
                # Read RAW, deliberately not through `_deliverable_message`: this asks whether a
                # wording was already WRITTEN for this train, which is a generation-side
                # question. Gating it on delivery would make the writer re-run every cycle wherever
                # delivery is off (prd today) — burning the cycle budget re-writing text that is
                # already cached.
                already_written = entry.message_key in _messages
                entry.attempts = _message_attempts.get(entry.subject_key, 0)
                entry.errors = _message_errors.get(entry.subject_key, 0)
            # BEFORE the attempts check, and that ordering is the fix for a real give-up bug. A
            # `None` from `_write_message` costs an attempt (see pass 1c), but several of its paths
            # never call a backend at all — this one, where no backend can be asked. So three cycles
            # with generation off used to exhaust a situation permanently: when the 30-minute
            # `llm_message._LATCH_TTL` expired and a backend came back, the situation was already at
            # `_MAX_MESSAGE_ATTEMPTS` and was never offered to the writer again for its whole 12 h
            # life. That thins exactly the written-but-withheld population prd's rollout is judged
            # on.
            #
            # Asked for a BROWSER subject only. `_generation_enabled` reads `llm_message`'s
            # module-global latch, which is the poller's own; the WEBHOOK policy latches against its
            # own holder precisely so the two callers cannot disable each other, and consulting the
            # browser latch for a webhook-only subject would hand that isolation straight back.
            # `write_wording` asks the same question against the right holder as its second bound.
            if entry.browser and not _generation_enabled():
                _remember_message_meta(entry.message_key, status="llm_disabled")
                continue
            if already_written:
                # Reused from an earlier cycle: keep who wrote it, but say that this cycle's row
                # carries text written against an earlier cycle's payload, not the one recorded
                # beside it.
                _remember_message_meta(entry.message_key, status="ok_cached")
                continue
            if entry.attempts >= _MAX_MESSAGE_ATTEMPTS:
                # Without this the retry never stops. A write that fails for a payload-dependent
                # reason (a refusal, an over-long reply, an unparseable one) fails again on every 60
                # s tick for the situation's whole life -- ~720 Vertex calls that can change
                # nothing, because the push already went out with the deterministic wording, each
                # one burning a slot from _MAX_LLM_CALLS_PER_CYCLE that a train which WOULD have
                # succeeded now cannot use. Charged at the PAIR's grain (`_subject_key`), so a
                # corridor learns a payload is hopeless once rather than once per registration.
                _remember_message_meta(entry.message_key, status="attempts_exhausted")
                continue
            if entry.errors >= _MAX_TRANSIENT_ATTEMPTS:
                # The same stop, on the other budget and with a different meaning: the backend has
                # been unreachable for this subject a dozen times running. Recorded under its own
                # status so the two never have to be told apart by guesswork -- `attempts_exhausted`
                # is a verdict on the payload, `transient_exhausted` is a verdict on the morning,
                # and a rate computed across both measures neither.
                _remember_message_meta(entry.message_key, status="transient_exhausted")
                continue
            write_queue.append(entry)

    # ---- Pass 1c: write the wordings, CONCURRENTLY --------------------------------------------
    #
    # The pass this whole restructure exists for. A Vertex call is ~2.2 s and up to
    # `_MAX_LLM_CALLS_PER_CYCLE` of them run per cycle; sequentially that is the difference between
    # a budget of 8 (~18 s, all of the ceiling) and one of 64 (~140 s, absurd). Across
    # `_LLM_WORKERS` it is ~9 s.
    def _write_webhook_only(entry: _WriteSubject) -> tuple[str | None, str | None]:
        """One CLIENT-ONLY (pair, train)'s wording, through `notification_wording.WEBHOOK`.

        Returns `(message, status)` in `_write_message`'s own vocabulary, so pass 1c's budget
        bookkeeping below is one branch rather than two.

        **Only an `llm`-sourced wording is a message.** `write_wording` never returns without a
        wording -- every failure falls through to the template -- so returning its body unconditional
        would cache the TEMPLATE in `_messages`, and the next cycle's `already_written` check would
        then report `ok_cached` and never ask again. That is the give-up bug in a new place: with the
        webhook delivery switch off (every deployment today) it would freeze the first template
        forever, and with it on it would freeze whatever a transient Vertex failure produced.

        `subject_id` is the representative definition's key, which is what `_write_message` passes
        on the browser path and is protected the same way: `llm_message` hashes it (`key_digest`)
        before it reaches a log field, so the pattern itself never leaves the process.
        `write_wording`'s docstring asks for an opaque service-issued id and names that hashing as
        the reason varsel's own value is acceptable; there is no departure id at this grain, and two
        conventions for one parameter would be worse than one. What actually keeps a client route
        out of the prompt audit record is the POLICY -- `WEBHOOK.caller` suppresses the prompt/reply
        DEBUG record in code and drops `message` from the INFO record -- not the shape of this
        argument.
        """
        message_input = entry.message_input
        if message_input is None:
            # Nothing to prompt with. Charges no retry budget, for `_write_message`'s reason: the
            # absence of an input is not evidence about the model.
            return None, "no_input"
        try:
            wording = notification_wording.write_wording(
                notification_wording.Route(entry.pair[0], entry.pair[1]),
                notification_wording.DepartureWordingFacts(
                    situation_number=entry.subject.situation_number
                ),
                policy=notification_wording.WEBHOOK,
                subject_id=entry.key,
                budget=webhook_budget,
                message_input=message_input,
            )
        except Exception:
            logger.warning(
                "Webhook wording step raised; using the deterministic wording",
                exc_info=True,
                extra={"fields": {"event": "notification_wording", "result": "error",
                                  "surface": "webhook"}},
            )
            return None, "error"
        _remember_message_meta(
            entry.message_key, status=wording.status, message_input=message_input
        )
        return (wording.body if wording.source == "llm" else None), wording.status

    def _write_one(entry: _WriteSubject) -> None:
        # Claimed here rather than filtered above, so the ceiling is spent on calls that actually
        # START. A pre-filter would hand the budget to the first N in the queue whether or not the
        # clock still allowed them.
        #
        # A webhook subject claims the CLOCK only -- see `_WriteBudget.claim_clock`. Its call
        # allowance is `notification_wording`'s own, which is what keeps a client with a few
        # hundred subscriptions from spending the browser wordings' budget.
        if not (budget.claim() if entry.browser else budget.claim_clock()):
            _remember_message_meta(entry.message_key, status="capped")
            return
        # _write_message catches Exception itself, so this is belt and braces for a future edit
        # that lets one escape. It charges a retry budget rather than returning past the counters, so
        # a raiser is subject to the same give-up rule as a None. Delivery is unaffected either way:
        # loop 2 pushes independently and falls back to the deterministic wording.
        outcome: dict[str, Any] = {}
        try:
            if entry.browser:
                message = _write_message(
                    entry.subject,
                    journey_key=entry.key,
                    message_input=entry.message_input,
                    message_key=entry.message_key,
                    outcome=outcome,
                )
            else:
                message, outcome["status"] = _write_webhook_only(entry)
        except Exception:
            logger.warning(
                "Written-message step raised for %s; using the deterministic wording",
                entry.key,
                exc_info=True,
                extra={"fields": {"event": "llm_message", "result": "error"}},
            )
            message = None
            outcome["status"] = "error"
            _remember_message_meta(entry.message_key, status="error")
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
        # TWO KEYS AGAIN (KTD9): the wording is stored under the advice it names, while the two
        # retry counters are charged to the COARSE train slot -- a replacement train that shifted
        # by a minute must not hand a hopeless payload a fresh budget, which is the hole
        # `_MAX_MESSAGE_ATTEMPTS` closed (`_forget_message` refuses to clear them for the same
        # reason).
        with _state_lock:
            if message:
                _messages[entry.message_key] = message
                # Both budgets, because a success says the payload is writable AND the backend is
                # reachable. Leaving the error count standing would let a subject that recovers
                # still be retired by errors it has demonstrably outlived.
                _message_attempts.pop(entry.subject_key, None)
                _message_errors.pop(entry.subject_key, None)
            elif status in _PAYLOAD_FAILURES:
                _message_attempts[entry.subject_key] = entry.attempts + 1
            elif status in _TRANSIENT_FAILURES:
                _message_errors[entry.subject_key] = entry.errors + 1

    with _span(
        "varsel_write_wordings",
        queued=len(write_queue),
        workers=min(_LLM_WORKERS, len(write_queue)) if write_queue else 0,
    ):
        if write_queue:
            with ThreadPoolExecutor(max_workers=min(_LLM_WORKERS, len(write_queue))) as pool:
                _drain(pool.map(_write_one, write_queue), len(write_queue))
        _heartbeat()  # the pass boundary, as after pass 1a
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
    with _span("varsel_deliver", registrations=len(journeys) if push_on else 0):
        per_device: dict[str, list[tuple[str, dict, dict, Alert, str, str, bool]]] = {}
        oslo_today = cycle_now.astimezone(_OSLO).date().isoformat()
        # Pushes this cycle will attempt per registration, so the daily cap is judged against what is
        # about to go out as well as what has gone: without the reservation a cycle with five cancelled
        # trains on one journey would pass all five through a ledger that still read zero.
        reserved: dict[str, int] = {}
        capped: dict[str, int] = {}
        # WHICH REGISTRATIONS HAVE A TRAIN AT ALL, first and from memory. The device read used to
        # come first, which is one store round trip per registration per cycle under the store
        # backend -- serial, on this thread, for a question almost every registration answers "no"
        # to. Ordering it this way makes the reads bounded by what is about to be PUSHED rather than
        # by the population, and one multi-get then costs one round trip for all of them.
        candidates: list[tuple[str, dict, str, list[Alert]]] = []
        for jid, journey in journeys if push_on else []:
            key = journey_key(journey)
            # One push per train, not per SX message. Two situations about one cancelled departure
            # used to mean two notifications a minute apart, saying the same thing with different
            # percentages (2026-08-05). `_notified` still keys on the surviving situation_number, so
            # the suppressed one simply never notifies -- and `_record_decisions` below still sees
            # both.
            #
            # SOONEST DEPARTURE FIRST, the write queue's rule (pass 1c) applied to delivery: when
            # the daily cap below binds, it must be spent on the trains the traveller can still act
            # on.
            trains = sorted(
                (a for a in one_alert_per_train(alerts_by_key.get(key, [])) if a.alertworthy),
                key=lambda a: (
                    a.departure_time is None,
                    a.departure_time.timestamp() if a.departure_time else 0.0,
                ),
            )
            if trains:
                candidates.append((jid, journey, key, trains))
        devices = _registrations.devices_for(j["device_id"] for _jid, j, _k, _t in candidates)
        for jid, journey, key, trains in candidates:
            with _state_lock:
                if _registrations.get_journey(jid) is None:  # deleted while iterating
                    continue
            dev = devices.get(journey["device_id"])
            if dev is None:
                continue
            device = dev.get("subscription")
            brand = normalize_brand(dev.get("brand"))
            if device is None:
                continue
            # ONE read of this registration's push marks for the whole train loop below, which is
            # the shape the store backend needs: three fields of one journey document, so asking per
            # train per question would be three reads per push. Safe as a snapshot because nothing
            # in this loop writes them -- the marks are written in loop 2, after every delivery --
            # and `reserved` carries what THIS cycle is about to spend.
            ledger = _registrations.push_ledger(jid)
            sent_today = ledger.sent_today if ledger.day == oslo_today else 0
            for alert in trains:
                if alert.situation_number in ledger.notified:
                    continue
                tag = _notification_tag(jid, alert)
                # An earlier push about this TRAIN (another situation about it) already reached
                # this registration: the new one replaces it on the lock screen under the same tag,
                # so it is not counted against the cap and its title may say «Bekreftet:».
                replaces = tag is not None and tag in ledger.pushed_trains
                if (
                    not replaces
                    and sent_today + reserved.get(jid, 0) >= _MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY
                ):
                    capped[jid] = capped.get(jid, 0) + 1
                    continue
                if not replaces:
                    reserved[jid] = reserved.get(jid, 0) + 1
                per_device.setdefault(journey["device_id"], []).append(
                    (jid, device, journey, alert, key, brand, replaces)
                )
        if capped:
            # Once per cycle. The alerts themselves are still recorded below with pushed=False and are
            # re-offered next cycle, so this line is the only place the suppression is visible.
            logger.info(
                "Daily push cap (%d per registration) withheld %d alert push(es) across %d "
                "registration(s) this cycle",
                _MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY,
                sum(capped.values()),
                len(capped),
                extra={
                    "fields": {
                        "event": "push_capped",
                        "withheld": sum(capped.values()),
                        "registrations": len(capped),
                        "cap": _MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY,
                    }
                },
            )

        def _prepare_alert(
            item: tuple[str, dict, dict, Alert, str, str, bool],
        ) -> tuple[dict, dict] | None:
            """One pending alert → (subscription, payload), run on the fan-out's worker thread.

            ``None`` means "the registration went away; skip the send". The collection loop above
            already re-checks `_journeys` (a delete landing while it iterates), but everything it
            collects -- the device's SUBSCRIPTION included -- is captured before the pool starts, so a
            «Slett alt» arriving inside the fan-out window would otherwise still put a push on the
            phone after the UI said everything was deleted, and the "sent" result below would write a
            `_notified` key for a journey that no longer exists. This is the same re-check, moved to
            the latest point that still precedes the network call. It takes `_state_lock` for two dict
            lookups and never across `_send_push`.
            """
            jid, device, journey, alert, key, brand, replaces = item
            with _state_lock:
                if (
                    _registrations.get_journey(jid) is None
                    or _registrations.get_device(journey["device_id"]) is None
                ):
                    return None
            # The PAIR's wording (KTD9) beside this DEFINITION's message input: the text is what
            # every traveller on the corridor is sent, the input is the document this registration's
            # own row records and the template is rendered from.
            message = _deliverable_message(_message_key(_pair(journey), alert, now=cycle_now))
            payload = _notification_payload(
                journey,
                alert,
                message,
                journey_id=jid,
                message_input=message_inputs.get(_decision_key(key, alert)),
                brand=brand,
                confirmed=replaces,
            )
            return device, payload

        # KEYED PER DEFINITION AND PER DEPARTURE (`_decision_key`'s shape), because that is the
        # grain `_record_decisions` writes a row at -- NOT the pair-grained wording key, which two
        # registrations on one corridor share while their pushes differ. See the note on the
        # `pushed=` argument there.
        pushed_keys: set[tuple[str, str, str]] = set()
        if per_device:
            for _device_id, item, result, payload, error_detail in deliver_to_devices(
                per_device, _prepare_alert
            ):
                jid, _device, journey, alert, _key, _brand, replaces = item
                _log_push(
                    "alert",
                    result,
                    journey["device_id"],
                    payload,
                    journey_id=jid,
                    alert=alert,
                    error_detail=error_detail,
                )
                if result == "sent":
                    with _state_lock:
                        # Same liveness rule as `_prepare_alert`, re-asked because the delete can also
                        # land during the send itself: a dedup key for a journey that no longer exists
                        # would be persisted by the save below and reloaded on the next restart.
                        if _registrations.get_journey(jid) is not None:
                            _registrations.mark_notified(jid, alert.situation_number)
                            changed = True
                            # A replacement notification takes the earlier one's place on the lock
                            # screen under the same tag, so the day's count is not charged for it --
                            # decided HERE and not in the seam, because "is this a replacement" is
                            # the caller's question and was answered when the push was queued.
                            _registrations.record_push(
                                jid,
                                tag=_notification_tag(jid, alert),
                                departure=alert.departure_time,
                                oslo_day=oslo_today,
                                charge_cap=not replaces,
                            )
                    pushed_keys.add(_decision_key(journey_key(journey), alert))
                elif result == "gone":
                    if handle_gone_device(journey["device_id"]):
                        changed = True

    # BOTH channels since 2026-09-13 (U16, KTD4), at the journey-DEFINITION grain both of them
    # already share. `pushed_keys` is still only ever written by loop 2 above and is still purely
    # the browser's: a client notification is a MINT, which happens in the phase after this function
    # returns, and `record_minted_decisions` writes its own row for it there. Marking one here would
    # be a claim about a transaction that has not been attempted yet.
    with _span("varsel_record_decisions", definitions=len(match_targets)):
        _record_decisions(
            _recipient_view(by_journey_key, journeys, webhook_by_key, match_targets),
            alerts_by_key,
            pushed_keys,
            message_inputs,
            push_on=push_on,
            webhook_llm=webhook_llm,
            # The cycle's own clock, the one the match was made at -- not a second `now()` taken
            # here. The thinning rule is an elapsed-minutes test, and a pass that takes 60 s would
            # otherwise measure it from an instant nothing else in the cycle shares.
            now=cycle_now,
            closing_rows=closing_subjects,
        )

    if changed:
        _save_state()

    # LAST, after the cycle's two durable writes, and handed back rather than acted on: KTD6 orders
    # every client step that writes or sends after `_record_decisions` and `_save_state`, and
    # returning the work means a raise in it runs after this function has returned and cannot unwind
    # either of them at all. Guarded, because building a subject reads duck-typed objects (an
    # alternative journey from an injected provider, a pydantic dump) and a raise here would cost the
    # browser cycle its own tail for a client field.
    webhook_wordings: dict[tuple[str, str, str], tuple[str, str]] = {}
    try:
        observations = _webhook_observations(
            webhook_by_key,
            alerts_by_key,
            match_targets,
            message_inputs,
            webhook_llm=webhook_llm,
            wordings=webhook_wordings,
        )
    except Exception:
        logger.warning(
            "Could not build this cycle's webhook subjects; no notification event is emitted for it",
            exc_info=True,
            extra={"fields": {"event": "notification_emit", "result": "build_error"}},
        )
        observations = []
    return PassOutcome(
        webhook_observations=tuple(observations), webhook_wordings=webhook_wordings
    )


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
    togvarsel_notify: bool = Field(
        default=False,
        description=(
            "Whether subscriptions here also receive Oslo rush-hour forecast pushes "
            "(SHOW_RUSHVARSEL and TOGVARSEL_NOTIFY_ENABLED, ANDed in main); the PWA shows its "
            "notice AND its rush outlook panel only when true, so while the product is dark "
            "/varsel mentions rushvarsel nowhere."
        ),
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
            "Where the stop is: the municipality from the National Stop Register (the county only "
            "for the few refs the registry's topographic table does not resolve). Pickers append it "
            "when a name is offered by more than one place; NULL when the registry cannot place the "
            "stop at all."
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
    consent_text_version: str | None = Field(
        default=None,
        max_length=40,
        pattern=r"^\d{4}-\d{2}-\d{2}\.\d+$",
        description=(
            "Which version of the privacy/consent text the traveller was shown when registering "
            "(the PWA's CONSENT_TEXT_VERSION, e.g. '2026-08-31.1'), so which text was live at a "
            "given time is provable afterwards. Optional: an older PWA build sends nothing. "
            "Constrained like every sibling string here — the value rides the state blob on every "
            "save and lands in a structured log field, so an unbounded client string is not free."
        ),
    )

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
    consent_text_version: str | None = Field(
        default=None,
        description=(
            "The privacy/consent text version recorded at registration. NULL for a journey "
            "registered by a client that sends none, or before the field existed."
        ),
    )
    alerts: list[Alert] = Field(
        description="Situations currently affecting this journey, worst first — one per "
        "train. Published (SX) matches appear whether or not they cross the push threshold; "
        "a match no SX message named appears only if it does. See `_card_alerts`.",
    )
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
        togvarsel_notify=(
            _togvarsel_notify_provider() if _togvarsel_notify_provider is not None else False
        ),
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
        # The signup instant and the tombstone that carries it across a lapse are the seam's (see
        # `varsel_state.BlobRegistrations.put_device`): which of the three sources it comes from is
        # a property of the record's history, not of this request. So is the CEILING, since U13's
        # store half -- it is the client's quota there, enforced inside the create transaction where
        # U7 put it, and a read-then-compare out here is a race the blob only got away with because
        # it held a lock across both halves.
        try:
            _registrations.put_device(
                body.device_id,
                subscription=body.subscription,
                brand=normalize_brand(body.brand),
            )
        except varsel_state.CapacityExceeded as exc:
            logger.warning(
                "Global device cap reached; refusing a new registration",
                extra={"fields": {"event": "varsel_capacity", "kind": exc.kind}},
            )
            raise HTTPException(
                status_code=503, detail="Tjenesten er full. Prøv igjen senere."
            ) from None
        _save_state()
    return {"status": "ok"}


@router.delete("/devices/{device_id}", summary="Forget a device's push subscription")
def delete_device(device_id: str) -> dict:
    """Remove a device's subscription (e.g. the user turned notifications off). Journeys are kept.

    Plain `def`: `_save_state` blocks on GCS. See `upsert_device`.
    """
    with _state_lock:
        _forget_device(device_id, "turned_off")
        _save_state()
    return {"status": "ok"}


def _card_alerts(alerts: list[Alert]) -> list[Alert]:
    """The matches a journey card should show: everything an operator published, plus the no-SX
    predictions that cleared their own push threshold.

    A call NO SX message named is scored anyway (predict_calls.sql's `nosx` branch, 3/4 of the
    timetable) and every such match used to reach the card, headlined "Mulig avvik" and gated only
    by whether a situation touched the journey at all -- which for a no-SX row is every departure
    on it. So a quiet morning filled the page with a card per train quoting a few minutes of
    typical delay, and the disruption worth reading sat somewhere in the list. There is nothing to
    read on a sub-threshold one either: with no operator text behind it, the card is the model's
    own numbers and nothing else.

    Applied BEFORE `one_alert_per_train`, which is not interchangeable with after. The collapse
    keeps the most severe alert per train, and the two populations are cut at different bars
    (`alert_policy.threshold_for`, 0.85 vs 0.65) -- so a no-SX alert can outrank an alert-worthy
    published one about the same train, win the collapse, and then be dropped here, taking a card
    the traveller should have seen with it.

    Display only. `match_journey` still returns every match, so the decision log keeps its row
    (those rows are the lead-time series, and a match that stayed under the gate is exactly what a
    `miss` is made of) and the push gate is unchanged -- a sub-threshold alert was never pushed.
    """
    return [alert for alert in alerts if alert.has_situation or alert.alertworthy]


@router.delete("/devices/{device_id}/data", summary="Delete everything stored about a device")
def delete_device_data(
    device_id: Annotated[str, PathParam(min_length=8, max_length=100)],
) -> dict:
    """«Slett alt»: the device record, every journey it registered, and those journeys' dedup
    entries — one lock acquisition, one save (the GDPR art. 17 request, served in one call).

    Distinct from :func:`delete_device`, which is «Skru av»: that one forgets the push subscription
    and deliberately KEEPS the journeys. A client-side loop over the two per-item deletes would be
    non-atomic, N round trips, and would strand the `_notified` entries — which live in the
    persisted blob, so a redeploy would restore dedup keys pointing at journeys that no longer
    exist.

    Idempotent, following `delete_device` (`pop` with a default, 200 on a miss) and deliberately
    NOT `delete_journey`, which 404s: a client retrying after a dropped response must not see a
    spurious failure. The unknown-id path does **zero** I/O — no save, no log. Every mutation
    here holds `_state_lock` across a blocking GCS upload, so an unauthenticated endpoint that
    uploaded on each random UUID would be free write amplification, and the lock it holds while
    doing so is the one the poller and every registration need.

    The one non-200 is a **failed durable write**, which rolls back and 503s — see below. This is
    the only caller that reads `_save_state`'s return value: everywhere else the in-memory state
    is authoritative and a lost write costs one restart's worth of freshness, whereas here it
    costs the erasure itself.

    The path param carries the same bounds every body model puts on `device_id` (FastAPI validates
    a path param only where constraints are declared). No charset pattern: the legacy fallback id
    shape `dev-<ts>-<rand>` must stay legal.

    Plain `def`, not `async def`: `_save_state` blocks on GCS. See `upsert_device`.

    Note what is deliberately left alone: the caches keyed on the journey *definition*
    (`_messages`, `_message_attempts`) carry no device identifier and are shared by every
    registration of that definition, so clearing them here would discard another device's wording.
    They expire with their situation.
    """
    # ONE seam call, because the two backends make it atomic by means neither can express in the
    # other's steps: the blob pops under one lock and restores everything when the durable write
    # fails, the store runs U12's paged erase transaction. What stays here is the HTTP shape and the
    # log vocabulary.
    erasure = _registrations.erase_device(device_id)
    if not erasure.found:
        return {"status": "ok", "journeys_deleted": 0}
    if not erasure.durable:
        # The durable copy still holds everything, so the erasure did NOT happen. Reporting
        # success here is how an art. 17 request gets quietly undone: `load_state` reads the
        # blob on the next restart and brings the device, its journeys and their dedup keys
        # back — by which time the client has cleared its device id, so what came back is
        # unreachable by the person it belongs to. The seam has put it all back, which is what
        # makes the retry correct: without that the second attempt would find nothing, take the
        # zero-I/O unknown-id path above, and answer 200 over a blob that is still full.
        logger.error(
            "Could not persist the data deletion for device %s; rolled it back",
            device_id,
            extra={
                "fields": {
                    "event": "varsel_delete_all_persist_failed",
                    "device_id": device_id,
                    "journeys": erasure.journeys_deleted,
                }
            },
        )
        raise HTTPException(
            status_code=503, detail="Kunne ikke slette dataene akkurat nå. Prøv igjen."
        )
    logger.info(
        "Deleted all data for device %s (%d journeys)",
        device_id,
        erasure.journeys_deleted,
        extra={
            "fields": {
                "event": "varsel_delete_all",
                "device_id": device_id,
                "journeys_deleted": erasure.journeys_deleted,
            }
        },
    )
    return {"status": "ok", "journeys_deleted": erasure.journeys_deleted}


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
    pair = _pair(rec)
    matched = match_journey(rec) if alerts is None else alerts
    # One card per train, not per SX message -- see one_alert_per_train. Collapsed here rather than
    # in match_journey because the matcher also feeds the decision log, which must keep every
    # situation. `alerts` may be shared across several registrations of the same definition, so this
    # builds a new list and never mutates the caller's.
    alerts = one_alert_per_train(_card_alerts(matched))
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
            #
            # Keyed on the PLACE PAIR since KTD9, which is what makes this card show the wording the
            # pass wrote for a NEIGHBOUR's registration of the same corridor -- the whole point of
            # the shared key, and why `_message_key` derives the alternative slot itself rather than
            # taking it from a caller that has no cycle to read one from.
            cache_key = _message_key(pair, alert, now=now)
            # Through the delivery gate, exactly as the push body is: the card must never quote a
            # wording the notification beside it did not carry. Only the message is gated -- the
            # alternative below is advice the traveller gets either way, worded by whichever
            # template or model wrote the text above it.
            alert.message = _deliverable_message(cache_key)
            alt = _alternatives.get(_subject_key(pair, alert))
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
                    # None beside a written message: the card hides the line there (finding 18),
                    # and the snapshot must say what the traveller read, not what it could have.
                    "detail_text": None if alert.message is not None else _feedback_detail(alert),
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
        # `.get`, not `[…]`: every record persisted before the field existed lacks the key.
        consent_text_version=rec.get("consent_text_version"),
        alerts=alerts,
        # Suppressed while something is actually matching: a card cannot credibly say "we found no
        # departures between these stops" directly above a disruption on one of them.
        # Judged on the MATCHES, not on the filtered card list: a no-SX alert that was dropped
        # for being sub-threshold is still a train running between these two stops, and "vi fant
        # ingen avganger mellom disse stoppene" is a claim about the timetable rather than about
        # today's disruptions.
        warning=None if matched else _service_warning(rec),
    )


def _feedback_detail(alert: Alert) -> str:
    """Literal detail line mirrored from the current React renderer (JourneyCard's `AlertLine`).

    The card shows this line only under the operator's SUMMARY; beside a written message it said the
    same thing twice (design review 2026-09-01, finding 18), so `_journey_model` records None in that
    case. The train label gains « i morgen» exactly as the card's `trainLabel` does."""
    parts: list[str] = []
    if alert.departure_time:
        train = _train_label(
            alert.mode,
            _local_line_code(alert.line),
            _local_hhmm(alert.departure_time) or "",
        )
        if _departs_tomorrow(alert.departure_time):
            train = f"{train} i morgen"
        parts.append(train)
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
    mine = _registrations.device_journeys(device_id)
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
        mine = len(_registrations.device_journeys(body.device_id))
        if mine >= _MAX_JOURNEYS_PER_DEVICE:
            raise HTTPException(
                status_code=409,
                detail=f"Maks {_MAX_JOURNEYS_PER_DEVICE} reiser per enhet.",
            )
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
            # Which privacy text this traveller was shown. Stored on the RECORD (the state blob
            # carries it verbatim, so it needs no migration) and never on a varsel_decisions
            # column: adding one there breaks the registered scheduled query.
            "consent_text_version": body.consent_text_version,
        }
        if verdict is not None:
            rec["service_checked_ts"] = _now_iso()
            if not verdict[0]:
                rec["no_direct_service"] = True
                rec["service_reverse_only"] = verdict[1]
        try:
            _registrations.create_journey(jid, rec)
        except varsel_state.CapacityExceeded as exc:
            logger.warning(
                "Global journey cap reached; refusing a new registration",
                extra={"fields": {"event": "varsel_capacity", "kind": exc.kind}},
            )
            raise HTTPException(
                status_code=503, detail="Tjenesten er full. Prøv igjen senere."
            ) from None
        _save_state()
    try:
        model = _journey_model(jid, rec)
    except Exception:
        # The registration is already committed, so a failure rendering the response would otherwise
        # burn one of the 20 per-device slots on every retry while the UI shows nothing (review
        # 2026-07-29). Roll it back and let the client retry cleanly.
        with _state_lock:
            _registrations.delete_journey(jid)
            _save_state()
        logger.exception("Could not render the new journey; rolled the registration back")
        raise HTTPException(
            status_code=503, detail="Kunne ikke opprette reisen akkurat nå. Prøv igjen."
        ) from None
    # Logged only once the registration STANDS -- the rollback above un-registers it, and an audit
    # record of a registration that does not exist is worse than none. The journey is identified by
    # its `journey_id` and never by from_stop/to_stop or their names: keeping the commute pattern
    # out of Cloud Logging is the whole point, and this record sits beside device_id-bearing ones
    # (event="push", event="client_error").
    #
    # And deliberately NOT by `journey_digest`, which this record carried when it was first
    # written. The digest is `llm_message.key_digest` -- the key `varsel_decisions` and
    # event="llm_message" are both written under -- while nothing else in Cloud Logging pairs it
    # with a device: event="push" carries device_id + journey_id, event="llm_message" carries the
    # digest alone. Logging the two together here would be the join that makes every decision row
    # device-linkable through the logs, and the privacy page states the decision log
    # «identifiserer ikke enheten din» -- with THIS record's own justification resting on the same
    # property. `journey_id` gives consent provenance everything it needs (which registration was
    # made under which text) without minting that join.
    logger.info(
        "Journey registered (consent text %s)",
        body.consent_text_version or "unversioned",
        extra={
            "fields": {
                "event": "journey_registered",
                "journey_id": jid,
                "device_id": body.device_id,
                "consent_text_version": body.consent_text_version,
            }
        },
    )
    return model


@router.delete("/journeys/{journey_id}", summary="Delete a registered journey")
def delete_journey(
    journey_id: str,
    device_id: str = Query(description="The owning device; a journey is only deleted by its owner."),
) -> dict:
    """Delete a journey. 404 if it doesn't exist or isn't owned by this device.

    Plain `def`: `_save_state` blocks on GCS. See `upsert_device`.
    """
    with _state_lock:
        # Ownership is checked HERE and the removal is the seam's: whose journey it is is this
        # API's rule, while dropping the owner's tombstone with the last of its journeys is a
        # property of how registrations are stored. By ID rather than by listing the device's
        # journeys and looking for this one: that reads the same answer out of a query where a
        # document read will do, and the store backend is where that difference is a bill.
        rec = _registrations.get_journey(journey_id)
        if rec is None or rec["device_id"] != device_id:
            raise HTTPException(status_code=404, detail="Reise ikke funnet.")
        _registrations.delete_journey(journey_id)
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
    result, error_detail = _send_push_result(_send_push(subscription, payload))
    _log_push("test", result, body.device_id, payload, error_detail=error_detail)
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
