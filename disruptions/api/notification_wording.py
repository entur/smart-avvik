"""What a notification about a disrupted departure SAYS — shared by every surface that sends one.

Extracted from :mod:`varsel` on 2026-09-08 (U1 of the client-notification-webhooks plan, KTD1:
"Reuse by extraction, not reimplementation"). Every function here was already written to be a pure
function of ONE document — the message input :func:`llm_message.build_message_input` produces — so
the extraction is a move rather than a rewrite: `varsel` keeps module-level aliases and the bytes a
traveller receives are unchanged.

What the move buys is that a second surface can produce the same Norwegian wording without
importing the browser push path. `varsel` is 147 KB carrying the poller's mutable state, the VAPID
keypair, the device registry and the decision-log writer; a notification event needs none of that and
must not be able to reach any of it (R16). The dependency therefore points one way:
`varsel -> notification_wording`, never back.

Three things this module deliberately does NOT own, each left where it is for a reason:

* **`varsel._notification_payload`** — a Web Push envelope: icon, click-through url, tag, brand.
  All of it is browser delivery state.
* **`varsel._tag_environment`** — the `[dev]` title prefix exists because two PWA installs are
  indistinguishable in a phone's tray. A notification event carries its environment in its credentials.
* **`varsel._message_input_from_alert`** — the last-resort reconstruction for the moment
  :mod:`llm_message` itself has failed. It exists to have no dependency on that module, and giving
  it one here would defeat it.

**Two policies, both read by code rather than described in a comment** (:data:`BROWSER` and
:data:`WEBHOOK`). What genuinely differs between the surfaces is which switch decides that a
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
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Any, NamedTuple

from line_modes import transport_mode

logger = logging.getLogger(__name__)

def mode_label(operator: str | None, line: str | None = None) -> str:
    """User-facing vehicle noun for a departure: its line's Journey Planner mode where known, so a
    Ruter T-bane is «T-bane» and a Skyss boat «Båt» (KIX-25); else 'Tog' for a rail codespace and
    'Buss' for the rest, which is all the codespace can say (T-bane and trikk ride under `RUT`).

    Capitalised because it heads its line; `_LEG_MODE_NOUN` is the one table, lower-case for the
    mid-clause alternative. The noun is in the wording key, so a line whose mode arrives after a
    restart is worded once more."""
    noun = _LEG_MODE_NOUN.get(transport_mode(operator, line) or "")
    return noun.capitalize() if noun else "Buss"


# ---- Trains taken out of the plan (plan 2026-09-23-001) --------------------------------------------
#: The synthetic situation `predict_calls.sql` relays a Vy train under when it is missing from today's
#: plan (`rail_calls_removed_today`). A certainty like an ET cancellation, but never pushed per train:
#: on 2026-09-23 one push per train would have been 601 pushes to 92 registrations, and 35 of them
#: would have spent their whole day's cap on it before 06:00. Browser registrations get one morning
#: digest instead; the webhook channel gets no event in v1. Here, in the leaf both of `varsel`'s
#: channels already import, so the two cannot disagree about which trains it covers.
PLAN_REMOVED_PREFIX = "KIX:PlanRemoved:"


def is_plan_removed(situation_number: str | None) -> bool:
    """Is this the situation of a train Vy took out of today's plan?"""
    return bool(situation_number) and situation_number.startswith(PLAN_REMOVED_PREFIX)


def plan_removed_situation(jkey: str) -> str:
    """The situation `predict_calls.sql` relays a removed train's `RemovedJourney:` jkey under."""
    return PLAN_REMOVED_PREFIX + jkey


#: How many trains the digest names before it counts the rest («og 32 til»).
DIGEST_NAMED_MAX = 6
_WEEKDAYS_PLURAL = ("mandager", "tirsdager", "onsdager", "torsdager", "fredager", "lørdager", "søndager")


def _listed(labels: list[str]) -> str:
    """«a, b og c», at most :data:`DIGEST_NAMED_MAX` named and «og K til» for the rest."""
    named, rest = labels[:DIGEST_NAMED_MAX], len(labels) - DIGEST_NAMED_MAX
    if rest > 0:
        return f"{', '.join(named)} og {rest} til"
    return named[0] if len(named) == 1 else f"{', '.join(named[:-1])} og {named[-1]}"


def removed_digest(trains: list[tuple[str, bool]], *, weekday: int) -> tuple[str, str]:
    """The morning push about a registration's trains Vy took out of today's plan (R7), as
    `(title, wording)`.

    `trains` is `(label, bus_for_train)` soonest first, the label as `train_label` names a departure
    («L1 Spikkestad 07:10»); `weekday` is today's, Monday 0. Deterministic and never written by a model: it
    states a count and a list, and the reason is the same for every train in it. The title carries
    no environment tag and the wording no route line -- the caller adds both, as for every other
    push, and the route's names are a traveller's free text, which may hold a newline of its own.
    """
    labels = [label for label, _bus in trains]
    buses = [label for label, bus in trains if bus]
    sentences = [
        f"{_listed(labels)} går ikke i dag.",
        (
            f"{'Toget' if len(labels) == 1 else 'De'} går vanligvis på {_WEEKDAYS_PLURAL[weekday]}, "
            "men finnes ikke i dagens rutetabell."
        ),
    ]
    if buses:
        # The card's own sentence when every train has one (`predict_calls.sql`'s description).
        sentences.append(
            "Vy har satt opp buss for tog."
            if len(buses) == len(labels)
            else f"Vy har satt opp buss for {_listed(buses)}."
        )
    return f"{len(labels)} tog er tatt ut av rutetabellen i dag", " ".join(sentences)


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


def train_label(
    mode: str | None, line: str | None, hhmm: str, destination: str | None = None
) -> str:
    """Which departure this is, as a departure board names it: "Tog RE10 Hamar 09:24".

    The line is what makes the label identify *their* train rather than *a* train: a registered
    journey routinely matches several lines at once, so "Tog kl 09:24" leaves the reader to work out
    which of them left at 09:24 — on a lock screen, in the minutes before they walk to the platform.

    One label for both departures a body can name — the traveller's own train and the alternative
    offered instead of it — so the two read alike and a reader comparing them is comparing like with
    like. That is why it takes its parts rather than reading them off the message input: they come
    from the top level for the first and from `alternativ_reise.reisevei[0]` for the second.

    `mode` is passed in RATHER THAN LOOKED UP, and its case comes with it: the first label heads its
    own line ("Tog R10 08:00: …") while the second sits mid-clause after "Alternativ", where bokmål
    wants a lower-case common noun. This function cannot know which, so `_LEG_MODE_NOUN` is the one
    noun table and each caller sets the case: lower for the alternative, capitalised by `mode_label`
    for the alert's own `mode`.

    Every part is optional, and each missing one costs only itself: `linje` is absent when the ref
    was unknown, and a leg can carry a mode this codebase has no noun for. Inventing either would be
    the one failure worse than saying too little. Note the line-less form keeps "kl" and the line
    form drops it — "Tog 09:24" alone reads as a quantity, "Tog L1 09:24" as a line and a time.

    `destination` rides with the line and only with it, as on the board: "RE10 Hamar". RE10 ends at
    Hamar on some departures and at Lillehammer on others, so the line alone does not say which
    train either.
    """
    if line:
        if destination:
            line = f"{line} {destination}"
        return f"{mode} {line} {hhmm}" if mode else f"{line} {hhmm}"
    return f"{mode} kl {hhmm}" if mode else f"kl {hhmm}"


# The Journey Planner's transport modes in Norwegian. A closed set: `alternatives._TRIP_QUERY`
# asks for exactly these six (foot legs never reach a wording -- build_message_input drops them),
# so this is a complete translation rather than a best effort. Anything outside it yields no noun
# at all: "Alternativ R11 08:24" says less than it could, while a guessed noun would put the wrong
# vehicle on a lock screen, and the line and time still identify the departure without one.
#
# LOWER CASE (2026-08-20), unlike the `mode` on the alert, which `mode_label` capitalises from this
# same table since KIX-25. These are ordinary bokmål nouns, and this label is the only one that
# lands mid-clause -- always after "Alternativ", never at the start of a line -- so "Alternativ
# tog R11 08:24" is what Norwegian orthography asks for and "Alternativ Tog R11 08:24" reads as a
# proper noun. The alert's own label keeps its capital because it heads its
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
    ``varsel._webhook_withdrawal_wording`` -- and a product rule that must never drift is exactly
    the thing not to keep three copies of.
    """
    hhmm = message_input.get("avgang_kl")
    if not hhmm:
        return None
    label = train_label(
        message_input.get("transportmiddel"),
        message_input.get("linje"),
        hhmm,
        message_input.get("destinasjon"),
    )
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
    return train_label(
        _LEG_MODE_NOUN.get(leg.get("transportmiddel")),
        leg.get("linje"),
        hhmm,
        leg.get("destinasjon"),
    )


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
    elif message_input.get("kommer_ikke_fram"):
        # The vehicle leaves, and ET has cancelled the call where the traveller gets off. As certain
        # as «Innstilt» and as figure-free, and the body's first line already names the stop.
        detail = DESTINATION_CANCELLED
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
    title = _train_title(message_input)
    if title is None:
        return f"{from_name} → {to_name}"
    certain = message_input.get("innstilt") or message_input.get("kommer_ikke_fram")
    return f"Bekreftet: {title}" if confirmed and certain else title


def outcome_verb(message_input: dict) -> str:
    """What the input says happens to the train, as the title's own verb phrase: «er innstilt»
    (ET has declared it), «kan bli innstilt» (cancellation is the most likely outcome, so
    `innstilling_prosent` is present), «kan bli forsinket» (a delay figure), else «mulig avvik».

    One branch for the title and for :func:`alerts_summary`'s counts: a summary saying «2 kan bli
    innstilt» beside an entry whose title says «kan bli forsinket» would be two readings of one
    figure, and a count that put a forecast under a confirmed verb would state it as fact.
    """
    if message_input.get("innstilt"):
        return "er innstilt"
    if message_input.get("kommer_ikke_fram"):
        return "kjører ikke helt fram"
    if message_input.get("innstilling_prosent") is not None:
        return "kan bli innstilt"
    delay = message_input.get("forsinkelse_min") or {}
    if delay.get("typisk") or delay.get("verstefall"):
        return "kan bli forsinket"
    return "mulig avvik"


def _train_title(message_input: dict) -> str | None:
    """«Tog L1 07:18 er innstilt», «Tog L1 07:18: mulig avvik»; None when no departure is named."""
    train = train_label_from_input(message_input)
    if train is None:
        return None
    verb = outcome_verb(message_input)
    return f"{train}: {verb}" if verb == "mulig avvik" else f"{train} {verb}"


# ---- The one-sentence summary of several alerts (the favourites row, 2026-09-25) ----------------
#: An operator summary longer than this does not lead the sentence: the counts after it would fall
#: off a favourites row, and cutting operator text changes what it says (the 2026-08-20 rule), so
#: the sentence names the worst train instead.
CAUSE_MAX_CHARS = 60
#: Operator text that never leads as a cause, matched whole after `_normalized`. From the most
#: repeated Norwegian rail summaries of the 120 days to 2026-09-25 (one count per situation): the
#: per-train ADVICE Vy attaches beside its cancellation message («Ta neste tog», 2 690 situations,
#: is the second most common summary there is, and «Ta neste tog: 2 tog er innstilt» puts the advice
#: before the fact), and the STATUS words that only restate what the counts already say or, for
#: «Toget kjører igjen», contradict them. Replacement-transport notices («Buss for tog», «Planlegger
#: minibuss for tog», «Vy kjører maxitaxi på strekningen») are deliberately NOT here: leading with
#: what the operator has arranged is what a glance at a favourites row is for.
CAUSE_STOP_PHRASES = frozenset({
    "ta neste tog",
    "ta andre tog",
    "ta ruters buss",
    "ta ruters t-bane",
    "bruk de forreste vognene",
    "forsinket",
    "forsinkelse forventes",
    "toget står",
    "toget kjører igjen",
})
_VERB_ORDER = (
    "er innstilt",
    # Certain like the one above and just as much a reason to look for another way (2026-10-19), so
    # it ranks next to it and ahead of every forecast.
    "kjører ikke helt fram",
    "kan bli innstilt",
    "kan bli forsinket",
    "mulig avvik",
)
#: The template's line for a departure that leaves but never reaches the traveller's stop: ET has
#: cancelled the call there (`kommer_ikke_fram`). The card and the feedback snapshot say it verbatim.
DESTINATION_CANCELLED = "Kjører ikke helt fram"


@dataclass(frozen=True)
class _Vocabulary:
    """The words of one language's summary. The sentence's SHAPE is one function for all of them, so
    the Norwegian, the English and the nynorsk line cannot count, order or name differently (KIX-26)."""

    #: per :func:`outcome_verb` key: (singular, plural)
    verbs: dict[str, tuple[str, str]]
    #: (singular, plural) by the message input's lower-cased `transportmiddel`
    nouns: dict[str, tuple[str, str]]
    any_noun: tuple[str, str]
    #: a title's head noun by the same key; a mode outside it names the line and time alone
    title_nouns: dict[str, str]
    more: str  # «2 til kan bli forsinket»
    with_word: str  # «3 tog med mulig avvik»
    today: str
    tomorrow: str
    and_word: str
    at: str  # the line-less label's «kl»
    #: the payload situation field a cause is quoted from in this language; None for the Norwegian,
    #: whose cause is the operator's own `summary`
    cause_field: str | None


_NB = _Vocabulary(
    verbs={v: (v, v) for v in _VERB_ORDER},
    nouns={
        "tog": ("tog", "tog"),
        "buss": ("buss", "busser"),
        "t-bane": ("T-bane", "T-baner"),
        "trikk": ("trikk", "trikker"),
        "båt": ("båt", "båter"),
    },
    any_noun=("avgang", "avganger"),
    title_nouns={},
    more="til",
    with_word="med",
    today="i dag",
    tomorrow="i morgen",
    and_word="og",
    at="kl",
    cause_field=None,
)
_EN = _Vocabulary(
    verbs={
        "er innstilt": ("is cancelled", "are cancelled"),
        "kjører ikke helt fram": ("does not run all the way", "do not run all the way"),
        "kan bli innstilt": ("may be cancelled", "may be cancelled"),
        "kan bli forsinket": ("may be delayed", "may be delayed"),
        "mulig avvik": ("possible disruption", "possible disruption"),
    },
    nouns={
        "tog": ("train", "trains"),
        "buss": ("bus", "buses"),
        "t-bane": ("metro train", "metro trains"),
        "trikk": ("tram", "trams"),
        "båt": ("boat", "boats"),
    },
    any_noun=("departure", "departures"),
    title_nouns={"tog": "Train", "buss": "Bus", "t-bane": "Metro", "trikk": "Tram", "båt": "Boat"},
    more="more",
    with_word="with",
    today="today",
    tomorrow="tomorrow",
    and_word="and",
    at="at",
    cause_field="summary_en",
)
#: Nynorsk names a vehicle as bokmål does, so only the verbs and «i morgon» move. A weak participle
#: agrees in the plural when it is predicative («2 tog er innstilte»); «forseinka» does not inflect.
_NN = _Vocabulary(
    verbs={
        "er innstilt": ("er innstilt", "er innstilte"),
        "kjører ikke helt fram": ("køyrer ikkje heilt fram", "køyrer ikkje heilt fram"),
        "kan bli innstilt": ("kan bli innstilt", "kan bli innstilte"),
        "kan bli forsinket": ("kan bli forseinka", "kan bli forseinka"),
        "mulig avvik": ("mogleg avvik", "mogleg avvik"),
    },
    nouns={
        "tog": ("tog", "tog"),
        "buss": ("buss", "bussar"),
        "t-bane": ("T-bane", "T-banar"),
        "trikk": ("trikk", "trikkar"),
        "båt": ("båt", "båtar"),
    },
    any_noun=("avgang", "avgangar"),
    title_nouns={"tog": "Tog", "buss": "Buss", "t-bane": "T-bane", "trikk": "Trikk", "båt": "Båt"},
    more="til",
    with_word="med",
    today="i dag",
    tomorrow="i morgon",
    and_word="og",
    at="kl",
    cause_field="summary_nn",
)


def _joined(parts: list[str], and_word: str) -> str:
    """«a, b og c» / "a, b and c". At most five parts reach it (four verbs and tomorrow)."""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} {and_word} {parts[-1]}"


def _summary_title(message_input: dict, v: _Vocabulary) -> str | None:
    """:func:`_train_title` in `v`'s words, from the same fields: the Norwegian one IS that title,
    and the others name the train the way it does, with the mode noun translated."""
    if v is _NB:
        return _train_title(message_input)
    hhmm = message_input.get("avgang_kl")
    if not hhmm:
        return None
    mode = (message_input.get("transportmiddel") or "").lower()
    label = train_label(
        v.title_nouns.get(mode), message_input.get("linje"), hhmm, message_input.get("destinasjon")
    )
    if not message_input.get("linje"):
        label = label.replace(" kl ", f" {v.at} ").replace("kl ", f"{v.at} ", 1)
    if message_input.get("i_morgen"):
        label = f"{label} {v.tomorrow}"
    verb = outcome_verb(message_input)
    return f"{label}: {v.verbs[verb][0]}" if verb == "mulig avvik" else f"{label} {v.verbs[verb][0]}"


def _counted(
    inputs: list[dict], noun: tuple[str, str], v: _Vocabulary, *, beside_tomorrow: bool = False
) -> str:
    """«4 tog er innstilt, 2 kan bli forsinket i dag og 1 i morgen»: one count per outcome, worst
    first, the noun on the first count only, in that count's number. Tomorrow's trains are one
    trailing count, so today's verbs count today's trains alone; all tomorrow, the counts carry
    « i morgen» instead. Today's counts say « i dag» only when the sentence mentions «i morgen» --
    here, or in the named train these counts follow (`beside_tomorrow`) -- since a sentence about
    one day needs no day (the owner's call, 2026-09-25)."""
    today = [t for t in inputs if not t.get("i_morgen")]
    tomorrow = [t for t in inputs if t.get("i_morgen")]
    parts: list[str] = []
    for verb in _VERB_ORDER:
        n = sum(1 for t in (today or tomorrow) if outcome_verb(t) == verb)
        if n:
            head = f"{n} {noun[0] if n == 1 else noun[1]}" if not parts else str(n)
            phrase = v.verbs[verb][0 if n == 1 else 1]
            parts.append(f"{head} {v.with_word} {phrase}" if verb == "mulig avvik" else f"{head} {phrase}")
    if today and (tomorrow or beside_tomorrow):
        parts[-1] += f" {v.today}"
    if today and tomorrow:
        parts.append(f"{len(tomorrow)} {v.tomorrow}")
    return _joined(parts, v.and_word) if today else f"{_joined(parts, v.and_word)} {v.tomorrow}"


def _operator_texts(situations: list[dict]) -> list[str]:
    """The operator-written summaries among a train's situations, in payload order, less the
    phrases on :data:`CAUSE_STOP_PHRASES`.

    `has_situation` is the gate, not the situation number's shape: False means the summary is text
    this codebase wrote (a `KIX:` placeholder such as «Mulig avvik» or «Tatt ut av dagens
    rutetabell»), and leading with it would present our own words as an operator's cause.
    """
    return [
        text
        for situation in situations
        if situation.get("has_situation")
        if (text := (situation.get("summary") or "").strip().rstrip("."))
        if text.lower() not in CAUSE_STOP_PHRASES
    ]


def _shared_cause(trains: list[tuple[dict, list[dict]]]) -> str | None:
    """Operator text EVERY train's situations carry, chosen in the worst train's payload order.

    Text, not a situation number: the matcher keeps one train per situation (`impact.for_trip`'s
    `select="worst"`, folded by `varsel._collect_alerts`), so two entries never name the same
    number and a guard on it would be a branch that cannot fire. What two trains can share is the
    operator saying the same thing about each -- a per-journey message repeated down a line -- and
    that is the one case a cause may be stated for all of them.
    """
    common = set.intersection(*(set(_operator_texts(situations)) for _, situations in trains))
    for text in _operator_texts(trains[0][1]):
        if text in common and len(text) <= CAUSE_MAX_CHARS:
            return text
    return None


def _translated_cause(cause: str, situations: list[dict], field: str) -> str | None:
    """The cause in another language, off the worst train's situation that carries it (`field`,
    `summary_en` or `summary_nn`, which `varsel._webhook_situations` attaches). The cause is CHOSEN
    on the Norwegian text, because every rule that picks it is a rule about Norwegian; None when that
    situation has no translation or it runs past :data:`CAUSE_MAX_CHARS`, and that language's line
    then takes the no-cause branch rather than quote one language inside the other."""
    for situation in situations:
        if (situation.get("summary") or "").strip().rstrip(".") == cause:
            translated = (situation.get(field) or "").strip().rstrip(".")
            return translated if translated and len(translated) <= CAUSE_MAX_CHARS else None
    return None


def _summary(trains: list[tuple[dict, list[dict]]], v: _Vocabulary) -> str | None:
    inputs = [message_input for message_input, _ in trains]
    lead = _summary_title(inputs[0], v)
    if len(trains) == 1:
        return lead
    modes = {(t.get("transportmiddel") or "").lower() for t in inputs}
    noun = v.nouns.get(modes.pop(), v.any_noun) if len(modes) == 1 else v.any_noun
    cause = _shared_cause(trains)
    if cause and v.cause_field:
        cause = _translated_cause(cause, trains[0][1], v.cause_field)
    if cause:
        return f"{cause}: {_counted(inputs, noun, v)}"
    worst = outcome_verb(inputs[0])
    if lead is None or sum(1 for t in inputs if outcome_verb(t) == worst) > 1:
        return _counted(inputs, noun, v)
    # The named train and the counts are one sentence, so «i dag» follows the same rule across
    # both: the lead says it when the rest mentions tomorrow, the rest says it when the lead does.
    lead_tomorrow = bool(inputs[0].get("i_morgen"))
    rest_tomorrow = any(t.get("i_morgen") for t in inputs[1:])
    if rest_tomorrow and not lead_tomorrow:
        lead = f"{lead} {v.today}"
    return f"{lead}, {_counted(inputs[1:], (v.more, v.more), v, beside_tomorrow=lead_tomorrow)}"


def alerts_summary(trains: list[tuple[dict, list[dict]]]) -> tuple[str | None, str | None, str | None]:
    """One sentence over every alert-worthy train on a definition, for a favourites row that is
    skimmed rather than read, as `(Norwegian, English, nynorsk)` (KIX-26). `trains` is `(message_input,
    payload situations)` in WORST-FIRST order over every alive train and not only the listed ones,
    so a truncated answer reads the same as a full one.

    The operator's own cause leads when every train's situations carry the same operator text
    (:func:`_shared_cause`): «Signalfeil ved Asker: 4 tog er innstilt og 2 kan bli forsinket».
    Otherwise the worst train is named as its own title names it and the rest are counted: «Tog
    L1 07:18 er innstilt, 2 til kan bli forsinket» -- but only while it is the ONLY train with that
    outcome. When another train shares the worst outcome, naming one of them says nothing the
    others do not, so the counts stand alone: «4 tog er innstilt, 1 kan bli innstilt og 3 kan bli
    forsinket» (2026-09-25). A single train is its title alone. The counts are per
    :func:`outcome_verb`, so a confirmed and a forecast train never share a verb, and tomorrow's
    trains are counted apart -- «14 tog er innstilt i dag og 2 i morgen», with «i dag» said only
    when «i morgen» is.

    The English line is the same sentence in :data:`_EN`'s words ("Train L1 07:18 is cancelled, 2
    more may be delayed"), and the nynorsk one in :data:`_NN`'s («Tog L1 07:18 er innstilt, 2 til
    kan bli forseinka»). Their one divergence is the cause: the situation's own translation
    (:func:`_translated_cause`) or, when there is none, the no-cause branch.

    `(None, None, None)` for no trains: "nothing to say", never all-clear -- an empty list means no
    alert-worthy train among the trains the service scores.
    """
    if not trains:
        return None, None, None
    return _summary(trains, _NB), _summary(trains, _EN), _summary(trains, _NN)


# ---- One text over a train's operator messages (2026-10-18) -------------------------------------
#: Vy publishes a forecast («Forsinkelse forventes»: «Vi forventer at toget blir forsinket fra
#: Skien») and, once the train has left, the fact («Forsinket»: «Toget kjørte 40 minutter forsinket
#: fra Skien»), and leaves the forecast open beside it. The fact supersedes it: all 15 such pairs in
#: prd over the 14 days to 2026-10-18 named the same origin. Keyed on the summary pair Vy uses, so a
#: forecast under any other wording is never dropped.
_SUPERSEDED_BY = {"forsinkelse forventes": "forsinket"}
#: Vy's advice to wait or switch train, each its own situation (691 of the 717 «Ta …» summaries over
#: the 30 days to 2026-10-27). «Ta Ruters buss» is not here: it names a mode the alternative
#: departure beside the text cannot.
_TAKE_ANOTHER_TRAIN = ("ta neste tog", "ta andre tog")
#: A sentence ends at terminal punctuation followed by a capital, so «11. oktober» and «kl. 14:00»
#: stay whole. A missed boundary only costs a deduplication; a false one could drop half a sentence.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-ZÆØÅ])")


def _summary_key(text: str | None) -> str:
    return (text or "").strip().rstrip(".").lower()


def _deduped(texts: list[str]) -> str | None:
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        text = " ".join(text.split())
        if not text:
            continue
        if text[-1] not in ".!?":
            text += "."
        for sentence in _SENTENCE_BREAK.split(text):
            if sentence not in seen:
                seen.add(sentence)
                out.append(sentence)
    return " ".join(out) or None


def situations_text(situations: list[dict]) -> tuple[str | None, str | None, str | None]:
    """The operator's messages about one train as ONE text, `(Norwegian, English, nynorsk)`, for a client
    that would otherwise stack every `description` -- 52 % of alert-worthy trains on prd's
    registered journeys carried two or more over the 14 days to 2026-10-18.

    Every word is the operator's: messages are only left out or reordered, never rewritten. A
    superseded forecast goes (:data:`_SUPERSEDED_BY`), a sentence an earlier message already said
    goes (the shared cause, «Dette skyldes …»), and advice («Ta neste tog …», «Ta andre tog fra …»)
    goes whenever a fact is left to say (2026-10-27: the app's card ran to a list of 25 stations,
    and the alternative departure under it already says which train to take) -- otherwise the
    payload's order, which is severity. Exact sentences only: two messages that differ only in a
    place name («Stengt: Lysaker–Stabekk» / «… Lysaker–Oslo S») differ in exactly what matters.

    Only `has_situation` messages contribute, since anything else is text this codebase wrote. The
    situations to keep are chosen once, on the Norwegian, and each language is deduplicated on its
    own, so the languages cannot disagree about which message survived. A translation is None
    unless every kept message has its counterpart in that language, rather than quote Norwegian
    inside English or bokmål inside nynorsk.
    """
    kept = [s for s in situations if s.get("has_situation") and (s.get("description") or s.get("summary"))]
    summaries = {_summary_key(s.get("summary")) for s in kept}
    kept = [s for s in kept if _SUPERSEDED_BY.get(_summary_key(s.get("summary"))) not in summaries]
    facts = [s for s in kept if not _summary_key(s.get("summary")).startswith(_TAKE_ANOTHER_TRAIN)]
    kept = facts or kept
    fields = ["description" if s.get("description") else "summary" for s in kept]
    translations = [
        [s.get(f"{field}_{language}") for s, field in zip(kept, fields, strict=True)] for language in ("en", "nn")
    ]
    return (
        _deduped([s[field] for s, field in zip(kept, fields, strict=True)]),
        *(_deduped(texts) if all(texts) else None for texts in translations),
    )


# ---- What a wording is keyed on (KTD9) ----------------------------------------------------------
#
# One shape over two caches: the poller's (`varsel._messages`) and the journey-alert read's
# (`notification_pull._wordings`), with a read-through order that never writes the poller's.
# It lives here, in the leaf both import, because the alternative is a dependency between them --
# and `notification_pull` already imports `varsel`, so the arrow could only point the wrong way.
#
# The unit is the PLACE PAIR and not the journey definition. What a wording says is a function of
# the trip's two ends and of the disrupted train, never of the window or the weekday set a
# particular commuter registered: `llm_message.build_message_input` reads `from_stop` and `to_stop`
# off the definition and nothing else. Keyed per definition -- as `varsel` was until 2026-09-12 --
# one corridor with five hundred registrations cost five hundred backend calls per disrupted train
# and could show five hundred travellers five hundred different sentences about one departure.
#
# Every other element is something the wording STATES, so two keys that differ in one of them are
# two different sentences and must not share an entry:
#
# * `situation_number` -- the operator's own text is quoted from it.
# * the departure -- the wording names the train by its time; a message written at 06:05 about the
#   07:52 headed the card above the 09:12 when the key carried only (journey, situation).
# * `cancelled` -- a declared cancellation states a certainty and carries no delay figures, where a
#   forecast hedges. Before this the two shared an entry and nothing forgot it in between.
# * `tomorrow` -- «i morgen» is in the text (design review 2026-09-01, finding 48).
# * the alternative's IDENTITY -- the replacement train is named with its departure time.
# * the prompt version -- a reworded brief is a different writer.
# * the PREDICTION SNAPSHOT the alert was scored against (2026-09-22). This one is different in
#   kind from the six above: the others are things the sentence NAMES, and this is everything it
#   quotes a number for. The wording states `innstilling_prosent` and both `forsinkelse_min`
#   figures, which `varsel._webhook_payload` publishes live beside it as `probabilities` and
#   `delay` -- so without this term a sentence outlived the figures it was written against and a
#   caller read «89 % sjanse for innstilling» next to `cancellation: 0.1737`. Keying on the three
#   figures instead was measured and refused: over 351 prd subjects it gives 3.23 wordings per
#   subject against 3.93 for no reuse at all, with no saturation (0.76-0.88 new wordings per
#   sampled cycle in EVERY bucket, including the subjects that live longest), so it is the cost of
#   switching reuse off without the guarantee of having switched it off. The snapshot is the
#   honest unit: one wording per cycle per train, agreeing with every figure in the payload rather
#   than with the three a regex could have compared.
#
#   Read off `winner.impact.refreshed_at` rather than passed, because the ALERT is what both sides
#   hold -- the pass, the in-app card and the journey-alert read all key from an alert and none of
#   them holds the response separately. `main.refresh_call_predictions_cache` stamps it
#   unconditionally on every rebuild, so an entry is reusable for exactly the cycle that wrote it,
#   which is what still makes a burst of reads on one corridor cost one wording between them.
#   A `None` snapshot keys as "" and reuses as before: an alert with no response attached carries
#   no claim about which figures it was scored against, so there is nothing to invalidate on.
#
#   NOTHING SWEEPS the entries this orphans, deliberately. A key carrying a snapshot that has
#   passed can never be read again -- the reader derives its key from the current alert -- so an
#   orphan is memory and not a wrong answer, and the existing per-situation liveness sweep in
#   `varsel.check_and_notify` already drops it when the situation ends. At prd's measured 9.4
#   wordings a cycle that is a few thousand short strings a day.


def alternative_identity(alt: Any) -> tuple:
    """What makes two alternatives the same advice: everything a wording actually states.

    Moved here from `varsel` on 2026-09-12 (KTD9) so the key above is one function rather than two
    copies; duck-typed on an `alternatives.AlternativeJourney` exactly as everything else in this
    module is duck-typed, so no import travels with it.

    Not the whole model — both `*_prediction` blocks carry continuous probabilities re-scored
    against the live prediction cache on every cycle, so comparing those would invalidate the
    written message on nearly every tick, turning ``ok_cached`` into a per-cycle LLM call. That is
    exactly what the per-cycle call and clock ceilings exist to prevent.

    The two BOOLEANS are here, though, and the two times are not the whole story (2026-08-06).
    `at_risk` and `arrives_before_original` are each stated out loud — "(kan også bli rammet)",
    and the caveat that the traveller arrives later than they planned — so an alternative whose
    times round the same but whose `at_risk` has flipped false → true is NOT the same advice. It
    was treated as such: the cache stored the rescored journey and kept the message written when
    the flag was false, so the cached text promised a clean escape that had stopped existing. They
    cost nothing to include: a boolean flips at most once or twice in a situation's life, where a
    probability moves every cycle, so this cannot reintroduce the flapping the minute-rounding
    below exists to stop.

    The times are compared AT THE MINUTE, for that same reason. Both are Journey Planner *expected*
    times carried at second precision, while every wording renders ``%H:%M``: a realtime nudge from
    08:24:12 to 08:24:41 changed the identity, dropped the message and spent an LLM call to produce
    byte-identical prose — roughly every other cycle per alerting journey, out of a budget of 8.
    The resolution of the comparison has to be the resolution of the advice.
    """
    def minute(ts: datetime | None) -> datetime | None:
        return ts.replace(second=0, microsecond=0) if ts is not None else None

    return (
        minute(getattr(alt, "departure_time", None)),
        minute(getattr(alt, "arrival_time", None)),
        getattr(alt, "at_risk", None),
        getattr(alt, "arrives_before_original", None),
    )


def prompt_version() -> str:
    """`llm_message.PROMPT_VERSION`, or an empty marker on a deploy without the Vertex dependencies
    — a wording is then never written anyway, so nothing is keyed wrongly.

    A local import, like every other reach for that module from here: this runs on the poller
    thread and on the read's own pool, and an ImportError there must cost a cache key rather than
    a notification.
    """
    try:
        import llm_message

        return llm_message.PROMPT_VERSION
    except ImportError:
        return ""


def wording_key(
    pair: tuple[str, str], winner: Any, alternative: Any, *, tomorrow: bool
) -> tuple:
    """The cache key for one wording — see the block above for what each element is doing there.

    `winner` is the alert the wording is about, duck-typed. `alternative` is the one the wording
    would name, or `None`; the caller decides WHICH alternative that is, and the two callers
    deliberately differ — the read passes the answer its own per-train cache holds, and the poller
    passes the EFFECTIVE one from `varsel._alternatives`, which is the cached answer rather than
    this cycle's raw field. That distinction is load-bearing on the poller side: a lookup is
    skipped on most cycles, and keying on the raw field would make every silent cycle a miss and
    re-write text that is already written.

    `tomorrow` is passed rather than read off the message input, so a caller that has no input
    (the in-app card reads the cache with nothing but the alert) derives the same key as the pass
    that wrote it.
    """
    departure = getattr(winner, "departure_time", None)
    snapshot = getattr(getattr(winner, "impact", None), "refreshed_at", None)
    return (
        tuple(pair),
        getattr(winner, "situation_number", None),
        departure.isoformat() if departure is not None else "",
        bool(getattr(winner, "cancelled", False)),
        bool(getattr(winner, "destination_cancelled", False)),
        bool(tomorrow),
        alternative_identity(alternative) if alternative is not None else None,
        prompt_version(),
        snapshot.isoformat() if snapshot is not None else "",
    )


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

    The field names are `varsel.Alert`'s, not because the notification API path is a browser path but
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
    destination: str | None = None
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
        return mode_label(self.operator, self.line)


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
# The client surface's own switch for whether a traveller receives the MODEL's prose rather than
# the template. Separate from VARSEL_LLM_ENABLED, and defaulting OFF, because the plan's rollout
# order is explicit: "Enable the client flag in dev first, with deterministic wording, then model
# wording once matching and delivery qualify" — U8 step 3 states the same four steps. Sharing
# `VARSEL_LLM_ENABLED` would mean turning model prose on for clients the moment it is on for the
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
_WEBHOOK_DELIVERY_ENABLED = (
    os.environ.get("NOTIFICATION_WEBHOOK_LLM_ENABLED", "false").strip().lower() in _TRUTHY
)


def webhook_delivery_enabled() -> bool:
    """Whether a notification event carries the model's prose rather than the template wording.

    Read through a function rather than off the constant so a test can monkeypatch one name, the
    shape :func:`llm_message.delivery_enabled` already has.
    """
    return _WEBHOOK_DELIVERY_ENABLED


# How many backend calls one client evaluation pass may make, and how many the surface may make
# across a trailing hour and day. Sized like `/meldinger`'s and for the same reason, which is worth
# restating because it is NOT about this surface's own latency: what is being bounded is the
# per-project Vertex quota the POLLER shares. A burst that eats it makes a real push wording 429,
# and a 429 is a transient failure that returns status="error" without trying another backend — so
# a traveller silently gets the template and nothing on that side says so.
#
# Every deployed environment pins VARSEL_LLM_PROVIDER=gemini, so one wording is at most one call.
#
# RAISED 4x on 2026-09-22, with the snapshot term in `wording_key`: a wording is now reusable for
# one cycle rather than for the situation's 12 h life, and the measured cost of that is 3.93
# wordings per subject against 1. The ceilings are what stop a surface eating the per-project
# Vertex quota, so they have to move with what a correct wording now costs -- left alone, this
# channel would have spent its hour in its first few minutes and served the template for the rest.
# 4x and not 3.93x because a ceiling is a guard rail: the point is that it does not bind on an
# ordinary day, and prd's actual draw is ~318 calls a day against 27 688 available.
_MAX_CALLS_PER_PASS = 128
_MAX_CALLS_PER_HOUR = 2000
_MAX_CALLS_PER_DAY = 10000
_HOUR_WINDOW_S = 60.0 * 60
_DAY_WINDOW_S = 24.0 * 60 * 60

# The tally, keyed BY CALLER in the shape `alternatives._fetch_calls` and `meldinger._replay_calls`
# already use. The point of the keying is structural rather than arithmetic: the browser push path
# does not call this module at all, so its slot stays empty by construction, and a later caller
# cannot silently draw the client allowance down (or be drawn down BY it — U1 scenario 3).
_WEBHOOK_PATH = "webhook"
_BROWSER_PATH = "browser"
# The journey-keyed PUBLIC read on `/notification-api/v1` (2026-09-10), which words on demand for
# an unauthenticated caller. Its own slot for the same structural reason, and its own CEILINGS,
# below: a public endpoint's allowance has to drain over minutes rather than in one burst.
_PULL_PATH = "pull"

# The pull read's three ceilings, public so the operations document can quote them by name. The
# per-minute one is what the other two surfaces do not have: an hour's allowance spent in its first
# seconds by a burst of reads would leave the next fifty-nine minutes on the template, and a burst
# is exactly what a public read path gets. 120 a minute is a sixteenth of the hour -- the ratio
# is kept from when these were 30/500/2000, and all three moved 4x on 2026-09-22 with the
# snapshot term, for the reason written above `_MAX_CALLS_PER_PASS`.
WORDINGS_PER_MIN = 120
WORDINGS_PER_HOUR = 2000
WORDINGS_PER_DAY = 10000
_MINUTE_WINDOW_S = 60.0


class _Ceilings(NamedTuple):
    """One path's three ceilings; `per_min` None means no per-minute window binds it."""

    per_min: int | None
    per_hour: int
    per_day: int


# Per PATH, keyed like `alternatives._FETCH_CEILINGS`, and the ONE source `_spend` reads: putting
# the numbers on the policy instead would let a bare `_spend("pull")` -- which the suites call --
# silently lose the minute cap. `WordingPolicy.ceilings` reads this table by path. The ledger is
# derived from it, so a path cannot exist here without a slot, and an unknown path is a `KeyError`
# on its first call rather than a silent zero.
_CEILINGS: dict[str, _Ceilings] = {
    _WEBHOOK_PATH: _Ceilings(None, _MAX_CALLS_PER_HOUR, _MAX_CALLS_PER_DAY),
    _BROWSER_PATH: _Ceilings(None, _MAX_CALLS_PER_HOUR, _MAX_CALLS_PER_DAY),
    _PULL_PATH: _Ceilings(WORDINGS_PER_MIN, WORDINGS_PER_HOUR, WORDINGS_PER_DAY),
}


class _Ledger(NamedTuple):
    """One path's trailing windows. `minute` is LAST so `[0]` stays the hour deque, which is how
    the suites reached into a slot before the minute window existed."""

    hour: deque[float]
    day: deque[float]
    minute: deque[float]


_budget_lock = threading.Lock()
_calls: dict[str, _Ledger] = {path: _Ledger(deque(), deque(), deque()) for path in _CEILINGS}


def _spend(path: str, *, now: float | None = None) -> bool:
    """Charge one backend call to `path`, or refuse when any of its trailing windows is full.

    The ceilings are the path's own (:data:`_CEILINGS`); the minute window is pruned and checked
    only for a path that has one, so the two older surfaces cost exactly what they did.
    """
    now = monotonic() if now is None else now
    ceilings = _CEILINGS[path]
    with _budget_lock:
        hour, day, minute = _calls[path]
        while hour and now - hour[0] > _HOUR_WINDOW_S:
            hour.popleft()
        while day and now - day[0] > _DAY_WINDOW_S:
            day.popleft()
        while minute and now - minute[0] > _MINUTE_WINDOW_S:
            minute.popleft()
        if len(hour) >= ceilings.per_hour or len(day) >= ceilings.per_day:
            return False
        if ceilings.per_min is not None and len(minute) >= ceilings.per_min:
            return False
        hour.append(now)
        day.append(now)
        minute.append(now)
        return True


def spent(path: str = _WEBHOOK_PATH) -> tuple[int, int]:
    """(hour, day) calls currently charged to `path` — for tests and for a diagnostics route.

    Two figures, not three, so every existing reader and the operations document are unchanged;
    a minute count is asserted through a refusal, or read off `_calls[path].minute`.
    """
    with _budget_lock:
        hour, day, _minute = _calls[path]
        return (len(hour), len(day))


def _reset_budget() -> None:
    with _budget_lock:
        for ledger in _calls.values():
            for window in ledger:
                window.clear()


@dataclass
class WordingPolicy:
    """One surface's answer to "may a model be asked, and does its prose reach the traveller?".

    Both fields are read by :func:`write_wording`, not documentation. The instances below are the
    explicit per-surface adapters KTD1's extraction asks for; a further surface adds a further
    instance rather than a branch — which is how `PULL` arrived on 2026-09-10.

    `deliver` is the delivery gate — `llm_message.delivery_enabled()` for the browser
    (VARSEL_LLM_ENABLED), :func:`webhook_delivery_enabled` for a client. `path` names the
    allowance a call is charged to. `state_factory` builds the backend-selection holder: the
    browser uses `llm_message`'s module-global one (the poller's own latch, which is the whole
    point of it), while a client takes its own, exactly as `/meldinger`'s replay path does — a
    burst that drives both backends into a configuration error must not be able to read
    `generation_enabled()` False on the push path for the next thirty minutes.

    **`push_enabled()` is not a field here and must never become one** (KTD4). Whether this
    deployment holds a VAPID keypair is a fact about Web Push and says nothing about whether a
    notification event can be worded; a client surface has to work in a deployment with no keypair at
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

    @property
    def ceilings(self) -> _Ceilings:
        """This surface's per-minute / hour / day ceilings, read off :data:`_CEILINGS` by path."""
        return _CEILINGS[self.path]


def _browser_delivery_enabled() -> bool:
    import llm_message

    return llm_message.delivery_enabled()


def _new_webhook_state() -> Any:
    import llm_message

    return llm_message.new_webhook_state()


def _new_pull_state() -> Any:
    import llm_message

    return llm_message.new_pull_state()


BROWSER = WordingPolicy(name="browser", deliver=_browser_delivery_enabled, path=_BROWSER_PATH)
WEBHOOK = WordingPolicy(
    name="webhook",
    deliver=webhook_delivery_enabled,
    path=_WEBHOOK_PATH,
    caller="webhook",
    state_factory=_new_webhook_state,
)

# The journey-keyed public read (2026-09-10). `deliver` is the webhook channel's gate rather than
# the browser's: what the read serves is the client channel's wording, and the browser gate is
# about what a /varsel traveller receives. A truthy `caller` is what suppresses the prompt/reply
# record in `llm_message` (R11); its own holder is what keeps a burst of reads that drives a
# backend into a configuration error from reading `generation_enabled()` False on the poller or
# on the webhook channel (R4).
PULL = WordingPolicy(
    name="pull",
    deliver=webhook_delivery_enabled,
    path=_PULL_PATH,
    caller="pull",
    state_factory=_new_pull_state,
)

_POLICIES = (BROWSER, WEBHOOK, PULL)


def reset_state() -> None:
    """Forget every charge and every cached latch holder. Test-only; no serving path calls it.

    ONE seam, doing both, because the two leak the same way and a suite that remembered only the
    obvious half would look reset and not be. The allowance is the obvious half: a partly-spent
    trailing hour makes the next test degrade to the template for a reason having nothing to do
    with what it is checking, which reads exactly like the give-up bug those tests exist to pin
    (`varsel._message_errors`, 2026-08-22, is the same lesson one module over). The latch holder is
    the half that is easy to forget and worse to leak: a test that drives every backend into a
    `_ConfigurationError` latches THIS process's `WEBHOOK` holder off for `llm_message._LATCH_TTL`
    — thirty minutes, i.e. the rest of the run — so every later webhook wording is a template and
    nothing says why.

    Iterates the policies rather than naming `WEBHOOK`, so a third surface's holder is cleared by
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

    **Which budget a webhook wording is to draw on, stated because it is two answers and not
    one.** Nothing in U1 calls this class: the caller arrives with U2, and this paragraph is the
    contract it and U6 have to honour rather than a description of the tree. A webhook subject is
    to be written for inside the poller's existing writing pass (`check_and_notify`'s pass 1c), so
    it shares that pass's WALL-CLOCK deadline — `varsel._WriteBudget`, which reserves
    `_WORST_CASE_CALL_S` up front so the deadline stays a ceiling on when the pass *ends*. What it
    must NOT share is the CALL allowance: `varsel._MAX_LLM_CALLS_PER_CYCLE` (64) counts browser
    wordings and is left alone, and a client call is charged to this class plus the caller-keyed
    trailing hour/day above. The split is deliberate. The clock is a property of the pass, and two
    surfaces writing inside one pass cannot each have their own; the count is a property of the
    surface, and merging them would let a busy client morning silently retire the browser
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
    policy: WordingPolicy = WEBHOOK,
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
       can be compared on real traffic and records both). A notification event carries one wording.
    2. **`generation_enabled`** on this surface's own holder — which is where
       `VARSEL_LLM_PROVIDER=off`, the egress kill switch, stops a client call. That switch is
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
            "%s wording allowance exhausted; using the deterministic wording",
            policy.name,
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
