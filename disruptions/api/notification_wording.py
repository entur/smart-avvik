"""What a notification about a disrupted departure SAYS — shared by every surface that sends one.

Extracted from :mod:`varsel` on 2026-09-08 (U1 of the partner-notification-webhooks plan, KTD1:
"Reuse by extraction, not reimplementation"). Every function here was already written to be a pure
function of ONE document — the message input :func:`llm_message.build_message_input` produces — so
the extraction is a move rather than a rewrite: `varsel` keeps module-level aliases and the bytes a
traveller receives are unchanged.

What the move buys is that a second surface can produce the same Norwegian wording without
importing the browser push path. `varsel` is 147 KB carrying the poller's mutable state, the VAPID
keypair, the device registry and the decision-log writer; a partner event needs none of that and
must not be able to reach any of it (R16). The dependency therefore points one way:
`varsel -> notification_wording`, never back.

Three things this module deliberately does NOT own, each left where it is for a reason:

* **`varsel._notification_payload`** — a Web Push envelope: icon, click-through url, tag, brand.
  All of it is browser delivery state.
* **`varsel._tag_environment`** — the `[dev]` title prefix exists because two PWA installs are
  indistinguishable in a phone's tray. A partner event carries its environment in its credentials.
* **`varsel._message_input_from_alert`** — the last-resort reconstruction for the moment
  :mod:`llm_message` itself has failed. It exists to have no dependency on that module, and giving
  it one here would defeat it.

**Two policies, both read by code rather than described in a comment** (:data:`BROWSER` and
:data:`PARTNER`). What genuinely differs between the surfaces is which switch decides that a
traveller receives the *model's* prose rather than the template, and which allowance a model call
is charged to. Everything else — the template itself, the title, the delay phrase, the label a
train is named by — is one implementation, because a wording that varied by surface would split
every comparison in `/historikk` and `/meldinger` along a second axis. That single implementation
is what KTD1 buys and what U1 exists for; nothing here decides WHICH departures are worth a
notification, which is the gate (:func:`alert_policy.gate`) and belongs to the matcher.

Withdrawal (`journey.alert.withdrawn`, U6) needs no re-cut here if it ever wants prose of its own.
:func:`deterministic_body` and :func:`notification_title` are siblings over the same document and
the parts they are assembled from (:func:`train_label`, :func:`delay_phrase`,
:func:`alternative_phrase`) are public, so a withdrawal wording would be a new function beside
them. Note the plan is explicit that a withdrawal "means the forecast changed, not that normal
service is guaranteed", so it is not an all-clear and must not be worded as one.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Any

logger = logging.getLogger(__name__)

# Rail codespaces (operator == ref codespace). vehicleMode is NULL for most bus producers, so the
# vehicle noun is derived from the codespace, not the feed; everything not rail is labelled "Buss".
# Moved here with `mode_label` so a wording never has to import `varsel` for a noun.
_RAIL_CODESPACES = frozenset({"VYG", "NSB", "GOA", "SJN", "FLT"})


def mode_label(operator: str | None) -> str:
    """User-facing vehicle noun for an operator: 'Tog' (rail) or 'Buss' (everything else)."""
    return "Tog" if operator in _RAIL_CODESPACES else "Buss"


# ---- The template wording -----------------------------------------------------------------------
def delay_phrase(message_input: dict) -> str | None:
    """"typisk 15–27 min forsinket" from the exceedance-stack interval (p50–p90, /demo's delay
    column); collapsed to one number when they coincide, None when neither is known."""
    delay = message_input.get("forsinkelse_min") or {}
    p50, p90 = delay.get("typisk"), delay.get("verstefall")
    if not p50 and not p90:
        return None
    if p50 and p90 and p90 > p50:
        return f"Typisk {p50}–{p90} min forsinket"
    return f"Typisk {p90 or p50} min forsinket"


def train_label(mode: str | None, line: str | None, hhmm: str) -> str:
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
# `train_label`, which cannot know where its caller will put the result. "T-bane" keeps its capital
# T in every position: it is an abbreviation, not a common noun.
def train_label_from_input(message_input: dict) -> str | None:
    """The traveller's own train as every notification names it, or ``None`` when the input has no
    departure time to name one with.

    Two things in one function because they are one rule. The label is
    :func:`train_label`'s, read off the message input's own top level; and a departure that is
    TOMORROW says so. The poller scores ~12 h ahead, so a push about an 07:18 train routinely goes
    out the evening before -- by design (design review 2026-09-01, finding 48) -- and read at 21:36
    "Tog L1 07:18 er innstilt" names tonight's train to anybody who does not already know the
    forecast horizon.

    ``None`` rather than a route fallback, because the three callers fall back differently: a title
    falls back to the route, a body omits the label's line, and a withdrawal puts the route where
    the train would have gone. Choosing for them here would make one of the three wrong.

    It exists as a function because the rule was written out three times -- twice here and once in
    ``varsel._partner_withdrawal_wording`` -- and a product rule that must never drift is exactly
    the thing not to keep three copies of.
    """
    hhmm = message_input.get("avgang_kl")
    if not hhmm:
        return None
    label = train_label(message_input.get("transportmiddel"), message_input.get("linje"), hhmm)
    return f"{label} i morgen" if message_input.get("i_morgen") else label


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
    return train_label(_LEG_MODE_NOUN.get(leg.get("transportmiddel")), leg.get("linje"), hhmm)


def alternative_phrase(message_input: dict) -> str | None:
    """"Alternativ tog R11 08:24, framme kl 09:05" — the escape route, with its caveats.

    This is the wording prd shipped while `VARSEL_LLM_ENABLED=false`, and it is what every surface
    falls back to when no model wrote anything, so a caveat that reaches only the prompt reaches
    nobody. Two of them are stated rather than implied:

    * `ankommer_i_tide: false` — the alternative is framme AFTER the traveller's planned arrival.
      Routine, not exotic: since the selection rule gained its fallback tier the offer for a
      cancelled train is regularly the next possibility rather than a faster one, and a clause that
      named a departure time without saying so would tell that traveller they still make their
      appointment. Read as `is False`, not falsily: an input built before this key existed (a
      replayed decision row, `varsel._message_input_from_alert` from an older process) says
      *nothing* about which side of the deadline this lands on, and asserting lateness from silence
      is a fabrication.
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
        # With the figure when the input carries one: «34 min senere enn planlagt ankomst» spares
        # the reader subtracting two clock times on a lock screen (design review 2026-09-01,
        # finding 16). `minutter_senere` is present only when positive -- see build_message_input.
        later = alt.get("minutter_senere")
        phrase += (
            f" – {later} min senere enn planlagt ankomst"
            if later
            else " – senere enn planlagt ankomst"
        )
    if alt.get("egen_risiko"):
        phrase += " (kan også bli rammet)"
    return phrase


def deterministic_body(message_input: dict) -> str:
    """The template wording: what a notification says when no written message is available.

    Written from the same message input the model is prompted with (llm_message.build_message_input)
    rather than from an alert directly, so the two wordings are always built from one set of facts.
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
        detail = delay_phrase(message_input)
    train = train_label_from_input(message_input)
    if train:
        detail = f"{train}: {detail}" if detail else train
    # The alternative comes LAST, after the cancellation or the delay: what has happened to the
    # traveller's own train is the news, and the replacement is the advice that follows from it.
    return "\n".join(
        part for part in (summary, detail, alternative_phrase(message_input)) if part
    )


def notification_title(
    message_input: dict, from_name: str, to_name: str, *, confirmed: bool = False
) -> str:
    """The lock-screen title: the FACT about the train, never the registration's name.

    It was «Asker stasjon → Lillestrøm stasjon» until 2026-09-02 -- the route the traveller already
    knows, and one every phone truncated to «Asker stasjon → Lillestrøm s…» (design review
    2026-09-01, finding 13). The title is the surface a lock screen is scanned by, so it now says
    what happened, «Tog L1 07:18 er innstilt», and the route moved to the body's first line. Built
    from the same message input as the body, so the two name the train identically; the route is
    the fallback when the input names no departure.

    `confirmed` prefixes a declared cancellation with «Bekreftet:» when an earlier notification
    about the same train already reached this recipient (finding 14): the two arrive under one
    notification tag, so the second REPLACES the first, and the prefix says that is what it is.
    Only a cancellation earns it -- a second forecast about the same train is an update, not a
    confirmation of anything.
    """
    train = train_label_from_input(message_input)
    if train is None:
        return f"{from_name} → {to_name}"
    if message_input.get("innstilt"):
        title = f"{train} er innstilt"
        return f"Bekreftet: {title}" if confirmed else title
    if message_input.get("innstilling_prosent") is not None:
        return f"{train} kan bli innstilt"
    delay = message_input.get("forsinkelse_min") or {}
    if delay.get("typisk") or delay.get("verstefall"):
        return f"{train} kan bli forsinket"
    return f"{train}: mulig avvik"


# ---- The typed message input --------------------------------------------------------------------
# `llm_message.build_message_input` is duck-typed on purpose: it reads an `/impact` response and a
# `varsel.Alert` through `getattr` so it imports neither `main` nor `varsel`. That is exactly what
# lets a second surface reach it -- with facts it has, in the shape it has them -- without either
# module growing a dependency on the other, and without the produced document changing by one byte.
#
# It MUST stay one builder. `/meldinger` replays stored `message_input` documents and asserts
# `message_input_json(json.loads(s)) == s`; a second builder emitting the same keys in a different
# order, or one key more, breaks replay silently rather than loudly.


@dataclass(frozen=True)
class Route:
    """The two ends a wording names, as refs the builder resolves to registry names."""

    from_stop: str | None
    to_stop: str | None

    def as_journey(self) -> dict:
        return {"from_stop": self.from_stop, "to_stop": self.to_stop}


@dataclass(frozen=True)
class DepartureWordingFacts:
    """Everything a wording about one dated departure is written from — typed, surface-neutral.

    The field names are `varsel.Alert`'s, not because the partner path is a browser path but
    because `build_message_input` reads exactly these names off whatever it is handed. Naming them
    anything else here would mean a translation layer whose only job is to rename fields, and a
    translation layer between a fact and a lock screen is where a figure quietly goes missing.

    Everything is optional in the same way it is on an `Alert`: a missing part costs only itself.
    """

    situation_number: str = ""
    summary: str | None = None
    description: str | None = None
    operator: str | None = None
    line: str | None = None
    departure_time: datetime | None = None
    cancelled: bool = False
    cancel_probability: float = 0.0
    delay_p50_min: int | None = None
    delay_p90_min: int | None = None
    impact_probability: float | None = None
    has_situation: bool = True
    latest_observation: Any | None = None
    alternative_journey: Any | None = None

    @property
    def mode(self) -> str:
        return mode_label(self.operator)


@dataclass(frozen=True)
class _ImpactShim:
    """The `lines`/`situations` view `build_message_input` reads off an `/impact` response.

    A shim rather than a real `ImpactResponse` because the builder scopes both lists to the alert's
    own situation and line anyway — handing it one situation and one line is what it ends up with
    either way, and constructing a pydantic response here would invite a caller to believe the
    other fields on it meant something.
    """

    situations: list[Any] = field(default_factory=list)
    lines: list[Any] = field(default_factory=list)
    alternative_journey: Any | None = None


@dataclass(frozen=True)
class _SituationShim:
    situation_number: str
    summary: str | None
    description: str | None


@dataclass(frozen=True)
class _LineShim:
    line: str | None
    operator: str | None
    latest_observation: Any | None


def build_message_input(
    route: Route, facts: DepartureWordingFacts, *, now: datetime | None = None
) -> dict:
    """The message-input document for one dated departure — the same schema every surface uses.

    Delegates to :func:`llm_message.build_message_input`, which is the one place the schema lives
    (`/meldinger` replays documents it produced months ago, and `varsel_decisions.message_input`
    stores them). This function only assembles the two duck-typed arguments out of typed facts.

    Imported locally, like every other reader of that module in this codebase: a deploy without the
    Vertex dependencies must still be able to word a notification.
    """
    import llm_message

    return llm_message.build_message_input(
        route.as_journey(),
        _ImpactShim(
            situations=[
                _SituationShim(facts.situation_number, facts.summary, facts.description)
            ],
            lines=[_LineShim(facts.line, facts.operator, facts.latest_observation)],
            alternative_journey=facts.alternative_journey,
        ),
        facts,
        now=now,
    )


# ---- Delivery and generation policy per surface --------------------------------------------------
# The partner surface's own switch for whether a traveller receives the MODEL's prose rather than
# the template. Separate from VARSEL_LLM_ENABLED, and defaulting OFF, because the plan's rollout
# order is explicit: "Enable the partner flag in dev first, with deterministic wording, then model
# wording once matching and delivery qualify" — U8 step 3 states the same four steps. Sharing
# `VARSEL_LLM_ENABLED` would mean turning model prose on for partners the moment it is on for the
# PWA, in the same deploy, with one variable — which is precisely the coupling the 2026-08-07
# generation/delivery split removed one layer down. The name and the default are U1's to introduce
# and U8's to revisit when it writes the rollout document.
# Deliberately a copy of `llm_message._TRUTHY`, NOT an import of it, for the same reason
# `sso.py` keeps its own: every reader of `llm_message` in this module is imported INSIDE a
# function so that a deploy without the Vertex dependencies can still word a notification
# (see `build_message_input` below). Reading the constant at module scope would undo that
# and make this module unimportable exactly where it is meant to keep working. Flagged as
# duplication by a reuse review and kept, with this note, so it is not
# re-raised: the allow-list is four literals and the coupling it would buy costs more.
_TRUTHY = ("true", "1", "yes", "on")
_PARTNER_DELIVERY_ENABLED = (
    os.environ.get("NOTIFICATION_PARTNER_LLM_ENABLED", "false").strip().lower() in _TRUTHY
)


def partner_delivery_enabled() -> bool:
    """Whether a partner event carries the model's prose rather than the template wording.

    Read through a function rather than off the constant so a test can monkeypatch one name, the
    shape :func:`llm_message.delivery_enabled` already has.
    """
    return _PARTNER_DELIVERY_ENABLED


# How many backend calls one partner evaluation pass may make, and how many the surface may make
# across a trailing hour and day. Sized like `/meldinger`'s and for the same reason, which is worth
# restating because it is NOT about this surface's own latency: what is being bounded is the
# per-project Vertex quota the POLLER shares. A burst that eats it makes a real push wording 429,
# and a 429 is a transient failure that returns status="error" without trying another backend — so
# a traveller silently gets the template and nothing on that side says so.
#
# Every deployed environment pins VARSEL_LLM_PROVIDER=gemini, so one wording is at most one call.
_MAX_CALLS_PER_PASS = 32
_MAX_CALLS_PER_HOUR = 500
_MAX_CALLS_PER_DAY = 2000
_HOUR_WINDOW_S = 60.0 * 60
_DAY_WINDOW_S = 24.0 * 60 * 60

# The tally, keyed BY CALLER in the shape `alternatives._fetch_calls` and `meldinger._replay_calls`
# already use. The point of the keying is structural rather than arithmetic: the browser push path
# does not call this module at all, so its slot stays empty by construction, and a later caller
# cannot silently draw the partner allowance down (or be drawn down BY it — U1 scenario 3).
_PARTNER_PATH = "partner"
_BROWSER_PATH = "browser"
_budget_lock = threading.Lock()
_calls: dict[str, tuple[deque[float], deque[float]]] = {
    _PARTNER_PATH: (deque(), deque()),
    _BROWSER_PATH: (deque(), deque()),
}


def _spend(path: str, *, now: float | None = None) -> bool:
    """Charge one backend call to `path`, or refuse when either trailing window is full."""
    now = monotonic() if now is None else now
    with _budget_lock:
        hour, day = _calls[path]
        while hour and now - hour[0] > _HOUR_WINDOW_S:
            hour.popleft()
        while day and now - day[0] > _DAY_WINDOW_S:
            day.popleft()
        if len(hour) >= _MAX_CALLS_PER_HOUR or len(day) >= _MAX_CALLS_PER_DAY:
            return False
        hour.append(now)
        day.append(now)
        return True


def spent(path: str = _PARTNER_PATH) -> tuple[int, int]:
    """(hour, day) calls currently charged to `path` — for tests and for a diagnostics route."""
    with _budget_lock:
        hour, day = _calls[path]
        return (len(hour), len(day))


def _reset_budget() -> None:
    with _budget_lock:
        for hour, day in _calls.values():
            hour.clear()
            day.clear()


@dataclass
class WordingPolicy:
    """One surface's answer to "may a model be asked, and does its prose reach the traveller?".

    Both fields are read by :func:`write_wording`, not documentation. The two instances below are
    the explicit per-surface adapters KTD1's extraction asks for; a third surface adds a third
    instance rather than a branch.

    `deliver` is the delivery gate — `llm_message.delivery_enabled()` for the browser
    (VARSEL_LLM_ENABLED), :func:`partner_delivery_enabled` for a partner. `path` names the
    allowance a call is charged to. `state_factory` builds the backend-selection holder: the
    browser uses `llm_message`'s module-global one (the poller's own latch, which is the whole
    point of it), while a partner takes its own, exactly as `/meldinger`'s replay path does — a
    burst that drives both backends into a configuration error must not be able to read
    `generation_enabled()` False on the push path for the next thirty minutes.

    **`push_enabled()` is not a field here and must never become one** (KTD4). Whether this
    deployment holds a VAPID keypair is a fact about Web Push and says nothing about whether a
    partner event can be worded; a partner surface has to work in a deployment with no keypair at
    all.
    """

    name: str
    deliver: Callable[[], bool]
    path: str
    # The `caller` marker `llm_message` records and gates its prompt/reply DEBUG record on -- and
    # deliberately NOT the same value as `name`. `llm_message._log_fields` documents it as naming a
    # NON-browser surface, and the gate it feeds is truthiness (`if _LOG_PROMPTS and not replay and
    # not caller`), so passing `name` handed the browser a truthy marker and switched off exactly
    # the `/varsel` message debugging `helm/.../values-kub-ent-dev.yaml` turns on with
    # VARSEL_LLM_LOG_PROMPTS="true". `name` stays the human label on this module's own log records;
    # this is what crosses into llm_message. None for the browser is the whole point.
    caller: str | None = None
    state_factory: Callable[[], Any] | None = None
    _state: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def state(self) -> Any:
        """This surface's latch holder, built once and kept — None means the module-global one."""
        if self.state_factory is None:
            return None
        with self._lock:
            if self._state is None:
                self._state = self.state_factory()
            return self._state

    def forget_state(self) -> None:
        """Drop the cached holder so the next call builds a fresh one. Test-only — see
        :func:`reset_state`, which is what a suite should call."""
        with self._lock:
            self._state = None


def _browser_delivery_enabled() -> bool:
    import llm_message

    return llm_message.delivery_enabled()


def _new_partner_state() -> Any:
    import llm_message

    return llm_message.new_partner_state()


BROWSER = WordingPolicy(name="browser", deliver=_browser_delivery_enabled, path=_BROWSER_PATH)
PARTNER = WordingPolicy(
    name="partner",
    deliver=partner_delivery_enabled,
    path=_PARTNER_PATH,
    caller="partner",
    state_factory=_new_partner_state,
)

_POLICIES = (BROWSER, PARTNER)


def reset_state() -> None:
    """Forget every charge and every cached latch holder. Test-only; no serving path calls it.

    ONE seam, doing both, because the two leak the same way and a suite that remembered only the
    obvious half would look reset and not be. The allowance is the obvious half: a partly-spent
    trailing hour makes the next test degrade to the template for a reason having nothing to do
    with what it is checking, which reads exactly like the give-up bug those tests exist to pin
    (`varsel._message_errors`, 2026-08-22, is the same lesson one module over). The latch holder is
    the half that is easy to forget and worse to leak: a test that drives every backend into a
    `_ConfigurationError` latches THIS process's `PARTNER` holder off for `llm_message._LATCH_TTL`
    — thirty minutes, i.e. the rest of the run — so every later partner wording is a template and
    nothing says why.

    Iterates the policies rather than naming `PARTNER`, so a third surface's holder is cleared by
    existing on the tuple instead of by somebody remembering to add a line here.
    """
    _reset_budget()
    for policy in _POLICIES:
        policy.forget_state()


@dataclass(frozen=True)
class Wording:
    """What one notification will actually say, and where the text came from.

    `title` and `body` are always present: the template is free, always available, and every
    failure path below falls back to it, so a wording is never missing. `source` is the one thing
    the strings cannot be reverse-engineered into — 'deterministic' and 'llm' produce the same
    shape — and `status` says why, using :class:`llm_message.MessageResult`'s vocabulary plus the
    three this module's own bounds add (`capped`, `rate_limited`, `not_delivered`).
    """

    title: str
    body: str
    source: str
    status: str
    message_input: dict


class PassBudget:
    """How many backend calls one evaluation pass has left. Not thread-shared: one pass, one
    counter, checked before a call rather than after — the shape `varsel._WriteBudget` uses.

    **Which budget a partner wording is to draw on, stated because it is two answers and not
    one.** Nothing in U1 calls this class: the caller arrives with U2, and this paragraph is the
    contract it and U6 have to honour rather than a description of the tree. A partner subject is
    to be written for inside the poller's existing writing pass (`check_and_notify`'s pass 1c), so
    it shares that pass's WALL-CLOCK deadline — `varsel._WriteBudget`, which reserves
    `_WORST_CASE_CALL_S` up front so the deadline stays a ceiling on when the pass *ends*. What it
    must NOT share is the CALL allowance: `varsel._MAX_LLM_CALLS_PER_CYCLE` (64) counts browser
    wordings and is left alone, and a partner call is charged to this class plus the caller-keyed
    trailing hour/day above. The split is deliberate. The clock is a property of the pass, and two
    surfaces writing inside one pass cannot each have their own; the count is a property of the
    surface, and merging them would let a busy partner morning silently retire the browser
    wordings — which is `_MAX_LLM_CALLS_PER_CYCLE`'s own lesson read the other way round (raising
    the count without the width just moves the binding constraint to the clock).
    """

    def __init__(self, limit: int = _MAX_CALLS_PER_PASS) -> None:
        self.left = limit

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def write_wording(
    route: Route,
    facts: DepartureWordingFacts,
    *,
    policy: WordingPolicy = PARTNER,
    subject_id: str = "",
    now: datetime | None = None,
    budget: PassBudget | None = None,
    message_input: dict | None = None,
) -> Wording:
    """Title and body for one dated departure, model-written when this surface delivers that.

    Never raises and never returns without a wording: every failure — an unconfigured project, a
    latched-off backend, a timeout, an exhausted allowance, an ungrounded reply — falls through to
    the template, which is built from the very same document the model was handed.

    Four bounds, in the order they are checked, and the order matters:

    1. **the surface's delivery gate**. Off means the model is not asked at all — there is nothing
       to compare against here (unlike `varsel`, which writes in prd precisely so the two wordings
       can be compared on real traffic and records both). A partner event carries one wording.
    2. **`generation_enabled`** on this surface's own holder — which is where
       `VARSEL_LLM_PROVIDER=off`, the egress kill switch, stops a partner call. That switch is
       process-wide by design: it means "no traveller data leaves for Vertex", and a surface that
       could opt out of it would make it a lie. Asked before anything is charged, so the kill
       switch reports `disabled` for as long as it is set rather than turning into
       `rate_limited` once the allowance it never actually spent runs out.
    3. **the pass budget**, so one evaluation of a busy morning cannot spend the surface's whole
       hourly allowance in a single sweep.
    4. **the trailing hour/day allowance**, charged to this surface's own key.

    `subject_id` is passed to the writer as its correlation key and MUST be an opaque service-issued
    id (a `departure_id`), never a route pattern: `varsel` passes `journey_key`, which is literally
    `from|to|begin|end|days`, and hashes it before logging for exactly that reason.
    """
    message_input = (
        message_input
        if message_input is not None
        else build_message_input(route, facts, now=now)
    )
    from_name = (message_input.get("reise") or {}).get("fra") or route.from_stop or ""
    to_name = (message_input.get("reise") or {}).get("til") or route.to_stop or ""
    title = notification_title(message_input, from_name, to_name)
    body = deterministic_body(message_input)

    def template(status: str) -> Wording:
        return Wording(title, body, "deterministic", status, message_input)

    if not _read_switch(policy.deliver, "delivery"):
        return template("not_delivered")
    try:
        import llm_message

        state = policy.state()
        # BEFORE the allowance is charged, not after. `VARSEL_LLM_PROVIDER=off` -- the egress kill
        # switch -- makes no call at all, so charging first would burn the surface's whole hourly
        # allowance against a switch that is doing its job and then report `rate_limited` instead
        # of `disabled`: a misleading diagnosis of exactly the control an operator reaches for in
        # an incident.
        if not llm_message.generation_enabled(state):
            return template("disabled")
    except Exception:
        logger.warning(
            "Could not read the message-generation switch; using the deterministic wording",
            exc_info=True,
            extra={"fields": {"event": "notification_wording", "result": "error",
                              "surface": policy.name}},
        )
        return template("error")
    if budget is not None and not budget.take():
        return template("capped")
    if not _spend(policy.path):
        logger.info(
            "Partner wording allowance exhausted; using the deterministic wording",
            extra={"fields": {"event": "notification_wording", "result": "rate_limited",
                              "surface": policy.name}},
        )
        return template("rate_limited")

    try:
        result = llm_message.write_message_detailed(
            message_input,
            journey_key=subject_id,
            situation_number=facts.situation_number,
            state=state,
            caller=policy.caller,
        )
    except Exception:
        logger.warning(
            "Written-message step failed; falling back to the deterministic wording",
            exc_info=True,
            extra={"fields": {"event": "notification_wording", "result": "error",
                              "surface": policy.name}},
        )
        return template("error")
    if not result.text:
        return template(result.status)
    return Wording(title, result.text, "llm", result.status, message_input)


def _read_switch(fn: Callable[[], bool], what: str) -> bool:
    """Read a delivery switch without letting it raise onto an evaluation pass.

    Named for what it does rather than `_gate`, which it was called before `alert_policy.gate`
    existed one module over: that one decides whether a DEPARTURE is worth a notification, this one
    only reads a boolean. Two functions called "gate" a module apart, answering unrelated
    questions, is a comment nobody would write on purpose.

    A failure answers False — the template — for the same reason `varsel._deliverable_message`
    does: a deploy without the Vertex dependencies must still send a notification, and an
    unreadable switch is not evidence that a model's prose should go out.
    """
    try:
        return bool(fn())
    except Exception:
        logger.warning(
            "Could not read the %s switch; using the deterministic wording",
            what,
            exc_info=True,
            extra={"fields": {"event": "notification_wording", "result": "error"}},
        )
        return False
