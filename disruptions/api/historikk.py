"""/historikk-api — what Avviksvarsel decided, and whether it was right.

Serves the alert-decision log (:mod:`decisions`) joined to the realized outcomes the nightly job
(``data/queries/label_varsel_decisions.sql``) labels it with. The page on top of it at
``/historikk`` is an internal tuning surface: the feedback loop the alerting stack otherwise has no
way to close.

Two things this module is deliberate about, because both are easy to get subtly wrong in a way that
flatters the system:

* **Unknown is not "fine".** A decision with no realized call carries ``verdict = null`` and
  ``outcome_state = 'ukjent'``, never ``correct_reject``. ``last_recorded``'s rail slice runs
  10-17 % thinner than the anshar extraction, so some calls never resolve; counting them as correct
  rejections would manufacture precision out of missing data.
* **Pending is not unknown either.** A decision made today cannot be labelled until tomorrow's run,
  so it reports ``outcome_state = 'venter'``. Folding the two together would make a fresh
  deployment look like a broken one.

One thing it is deliberate about in the other direction, because it was a reversal: the page's
from/to filters (2026-08-21) publish the set of registered journey endpoints, which this API
withheld entirely before then. They are served as a *vocabulary* by :func:`filters` and used as
predicates; no decision row carries them, so a card still names a train rather than a trip. What
the change concedes is that a from/to pair can be chosen and the matching departures read off. See
:class:`FilterOptions` and the header of ``historikk_decisions.sql``; stronger ground than that
means access control on ``/historikk``, not a smaller SELECT list -- and since 2026-09-10 that
access control exists: ``sso.RequireSSO`` fronts this API and the page with Entur SSO in every
environment (see ``sso.py``). The redaction rules below are kept as they are, deliberately: the gate
is one env var from off, and none of them costs the page anything.

Reads only. No import of main (the package rule): main injects the BigQuery client via
:func:`configure`, the same pattern reisevalg/varsel/llm_message/decisions already use. The stop
names and the place grouping behind the filters come from the National Stop Register directly
(:func:`_place_registry`), NOT from main's picker cache — that cache holds only today's timetable and
deliberately drops co-located refs, which is exactly how a station's rail side ended up offered as a
bare ref beside its own name.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# The decision log's own `channel` vocabulary (U16). Imported UNDER AN ALIAS for main.py's
# `impact_engine` reason: the route function below is itself named `decisions` and rebinds the
# module-level name, so a bare `decisions.CHANNELS` resolves to the route and raises at request
# time -- which is exactly what happened before this alias existed.
import decisions as decision_log
from deployment import environment_label
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_DECISIONS_QUERY = (Path(__file__).parent / "historikk_decisions.sql").read_text()
_SUMMARY_QUERY = (Path(__file__).parent / "historikk_summary.sql").read_text()
_FILTERS_QUERY = (Path(__file__).parent / "historikk_filters.sql").read_text()
_STOP_PLACES_QUERY = (Path(__file__).parent / "historikk_stop_places.sql").read_text()

# The tables behind this only change once a poller cycle (decisions) and once a night (outcomes),
# so a short cache keeps a page refresh from costing a BigQuery query. Same shape as main's
# _demo_meta_cache, including its own lock — except that one holds a single value while this is
# keyed on caller-chosen filters, so it needs a cap. The endpoint is public and unauthenticated:
# without one, an attacker varying dates mints an unbounded number of entries on a pod that never
# restarts, each one also costing a BigQuery query. Same reasoning and cap as alternatives.py.
_CACHE_TTL_S = 120
_CACHE_MAX_ENTRIES = 500
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, Any]] = {}

# The blast-radius bound on what one request can bill. This page ran with NO cap at all until
# 2026-09-12, while `/meldinger` and `/rushhistorikk` -- the same shape of page, over the same
# table, on the same public host -- both carried one; there was no reason for the difference beyond
# the order the three were written in.
#
# Be honest about what it does. A correctly pruned read of the decision log is a few MB and the
# whole log is ~1.5 GB (prd, 2026-09-11), so at 2 GiB this will NOT catch a pruning regression on
# today's table. What it catches is a query that starts reading something BIG: the ET tables a
# mistyped join can reach are ~10 TiB for a two-year scan, three orders of magnitude above this.
#
# THE DERIVATION, and why the value has not moved yet (R12, U4 step 4).
#
# The cap is meant to become FOUR TIMES the measured pruned 14-day scan, stated in ONE place for
# this page and /meldinger (which imports this constant) so the two cannot drift; /rushhistorikk
# reads other tables and keeps its own. Three things have to be true before that number can be
# written down, and only two of them are:
#
#   1. The read must prune. Since 2026-09-12 it does: the three queries go through
#      `varsel_decision_groups_between(d_start, d_end)` rather than the windowless view, so a
#      request reads its own `DATE(decided_at)` cover instead of the whole table.
#   2. The volume must be thinned, or the derived figure describes a log that no longer exists.
#      `varsel._thinning_reason` (KTD15) does that from the same date.
#   3. The thinned pruned scan must be MEASURED, at a scale that matters -- the harness at 10 000
#      definitions in dev, and prd's own `INFORMATION_SCHEMA.JOBS` figures for a 14-day request.
#      That measurement is owed and this constant moves with it, not before.
#
# Until then the value stays the 2 GiB its siblings use. Deriving a cap from today's 1.5 GB log
# would turn the log's growth into refusals on the page that reads the longest window (180 days),
# which is the failure this constant exists to prevent rather than to cause -- and the cap is not a
# pruning detector either way: a correctly pruned read is a few MB. It is a blast-radius bound on an
# endpoint sharing its project quota with the poller, and what it catches is a query that starts
# reading something big -- the ET tables a mistyped join can reach are ~10 TiB for a two-year scan.
_MAX_BYTES_BILLED = 2 * 1024**3

# A page shows a window, not a history. Bounded so a hand-written query string cannot ask for a
# scan of everything.
_MAX_WINDOW_DAYS = 180
_MAX_LIMIT = 500

_VERDICTS = ("hit", "false_alarm", "miss", "correct_reject")

# The codespaces this stack actually serves (main._SERVED_OPERATORS' live set, plus NSB for the
# rail family's own tagging). Validated rather than passed straight through, so an arbitrary
# caller-supplied string cannot mint a cache entry and a BigQuery query per distinct value.
_OPERATORS = frozenset(
    {"VYG", "NSB", "GOA", "KOL", "SOF", "NOR", "SKY", "ATB", "OST", "FIN", "MOR", "RUT", "GCO"}
)

_bq_client: Any | None = None


def configure(*, bq_client: Any) -> None:
    """Wire in the BigQuery client. Called once from main.lifespan."""
    global _bq_client
    _bq_client = bq_client


class Decision(BaseModel):
    """One alert decision: what we concluded, what we said, and what actually happened.

    Describes a train AND, since 2026-08-22, the endpoints of the journey it was watched for:
    `from_name` / `to_name` are served, so a card names a route beside `departure_time` and
    `n_devices`. That is the "somebody commutes A->B at 07:15" this model used to refuse — see the
    PRIVACY note atop `historikk_decisions.sql` for the reasoning and, more importantly, for the
    fact it rests on: prd's registrations are test users rather than members of the public. Team
    decision. `/meldinger` already published the same thing on the same grounds, and this brings
    the two surfaces back into step rather than opening a new one.

    What is still absent is `journey_digest` — the join key to the LLM audit log, which no page
    needs — and the raw stop REFS. The refs reach this process and are popped in
    :func:`_with_place_names`; only resolved NSR names leave it, exactly as `/meldinger` does it.

    `extra="forbid"` is the guard that keeps the remaining line where it is: the SQL and this model
    must agree column for column, so a SELECT that re-adds `journey_digest` — or that forgets to pop
    the refs — fails loudly here instead of silently publishing it.
    """

    model_config = ConfigDict(extra="forbid")

    situation_number: str | None = Field(description="SIRI-SX situationNumber.")
    summary: str | None = Field(description="Short Norwegian summary of the situation.")
    description: str | None = Field(description="The operator's fuller explanation.")
    operator: str | None = Field(description="Ref codespace: VYG, GOA, RUT, ...")
    line: str | None = Field(description="Line ref of the matched train.")
    mode: str | None = Field(description="Vehicle noun: 'Tog' or 'Buss'.")
    from_name: str = Field(
        description="Registered journey origin, resolved to an NSR name when possible."
    )
    to_name: str = Field(
        description="Registered journey destination, resolved to an NSR name when possible."
    )
    call_date: date | None = Field(description="Operating day of the boarding call (Europe/Oslo).")
    departure_time: datetime | None = Field(description="Aimed departure from the boarding stop (UTC).")

    n_decisions: int | None = Field(description="How many refresh cycles scored this train.")
    first_decided_at: datetime | None = Field(description="First time this train was scored (UTC).")
    last_decided_at: datetime | None = Field(
        description="Last time it was scored (UTC) — the closest look before departure.",
    )
    ever_alerted: bool | None = Field(description="Whether any cycle crossed the alert threshold.")
    first_alert_at: datetime | None = Field(description="When we first crossed it (UTC); null if never.")
    alert_lead_min: int | None = Field(
        description="Minutes of warning the traveller got — departure minus first_alert_at.",
    )
    max_impact_probability: float | None = Field(description="Highest alerting score reached.")
    final_impact_probability: float | None = Field(description="Score at the last look before departure.")
    final_cancel_probability: float | None = Field(description="Cancellation probability at that look.")
    final_delay_p50_min: int | None = Field(description="Median predicted departure delay (min).")
    final_delay_p90_min: int | None = Field(description="Bad-case predicted departure delay (min).")
    alert_threshold: float | None = Field(description="The operator's threshold applied here.")
    et_cancelled: bool | None = Field(
        description="SIRI-ET had already declared this boarding call cancelled when the decision "
        "the verdict judges was made, so the probabilities beside it are 1.0 by construction: we "
        "relayed a fact rather than predicted one. Null for decisions taken before 2026-08-05, "
        "which cannot be placed in either segment after the fact.",
    )
    pushed_any: bool | None = Field(
        description="Whether a notification was ISSUED on any channel: a Web Push the push service "
        "accepted, or — since 2026-09-13 — a webhook event that was minted. Web Push alone before "
        "that date. Read `channel` beside it: a 'both' subject can say true with no Web Push "
        "behind it.",
    )
    n_devices: int | None = Field(
        description="How many /varsel registrations shared this journey definition — the BROWSER "
        "side only, with n_subscriptions counting the client side.",
    )
    channel: str | None = Field(
        default=None,
        description="Which channel(s) watched this definition: 'browser', 'webhook' or 'both'. "
        "Null for decisions taken before 2026-09-13, when webhook routes were not recorded at "
        "all — the same third-bucket rule n_unsegmented follows, so a channel filter excludes "
        "them rather than guessing 'browser'.",
    )
    n_subscriptions: int | None = Field(
        default=None,
        description="How many webhook subscriptions shared this definition. A count and nothing "
        "more: no client, subscription, destination or external reference is recorded anywhere in "
        "the decision log, so a card never says whose integration it was.",
    )
    message: str | None = Field(description="The message text the traveller was sent.")
    message_source: str | None = Field(
        description="Which wording was sent: 'llm' or 'deterministic'. Since 2026-08-07 both are "
        "written for every alert-worthy decision and a deployment chooses which one it delivers, so "
        "'deterministic' means the traveller got the template — not that no model wording exists.",
    )

    actual_cancelled: bool | None = Field(description="ET declared the boarding call cancelled.")
    actual_dep_delay_min: int | None = Field(description="Realized departure delay (min).")
    actual_trouble: bool | None = Field(
        description="Realized: cancelled OR >= 10 min late. Null means UNKNOWN, not 'no trouble'.",
    )
    verdict: str | None = Field(
        description="'hit' | 'false_alarm' | 'miss' | 'correct_reject'; null when unresolved.",
    )
    outcome_state: str = Field(
        description="'avklart' (labelled), 'venter' (too recent to label) or 'ukjent' (labelled, "
        "but no realized call was found — permanent).",
    )


class DecisionsResponse(BaseModel):
    """Decisions whose boarding call falls in [from_date, to_date], newest departure first."""

    from_date: date = Field(description="Window start (operating day, Europe/Oslo).")
    to_date: date = Field(description="Window end, inclusive.")
    count: int = Field(description="Number of decisions returned.")
    decisions: list[Decision] = Field(description="Matching decisions, newest departure first.")


class ConfigResponse(BaseModel):
    """What the page needs to describe itself, independent of the decision log."""

    environment: str | None = Field(
        description='Non-prod environment name ("dev", "tst") for the page to show in its title; '
        "null in prd.",
    )


class PlaceOption(BaseModel):
    """One entry in the from/to dropdowns: one physical PLACE, not one stop ref.

    A station's rail and bus sides are separate NSR refs sharing a `parentRef`, and a registration
    lands on whichever one it was made against — so the two are offered as a single row here, the way
    `/varsel-api/stops` already offers them to the picker. `ref` is the row's identity and `n_refs`
    says how many refs it stands for; the server expands it back to all of them when filtering, which
    is the part that keeps the merge from hiding half the place's decisions behind the row.
    """

    ref: str = Field(
        description="The value to send back as from_place/to_place. The busiest of the refs this "
        "place covers, so it is one the decision log really carries — never a synthetic id.",
    )
    name: str | None = Field(
        description="National Stop Register name, or null when the registry could not be read or "
        "does not hold the ref. Clients fall back to showing the ref.",
    )
    n_refs: int = Field(
        default=1,
        description="How many stop refs this one row stands for. Above 1 means co-located refs "
        "were merged (a station's rail and bus sides); filtering by it matches all of them.",
    )


class FilterOptions(BaseModel):
    """What the page's dropdowns can be set to, over one window.

    Its own endpoint rather than something the client derives from ``/decisions``: that response is
    capped (``limit``, 200 from the UI) and ordered by departure, so options taken from it would
    quietly omit anything that only ran early in the window — and, once a filter were applied, would
    shrink to just the selected value and strand the user with no way back.

    This is also the API's allow-list. A value not in these arrays is rejected by ``/decisions`` and
    ``/summary`` rather than passed through, so a caller cannot mint an unbounded number of cache
    entries — and BigQuery queries — out of arbitrary strings. Same reasoning as
    :func:`_checked_operator`, with the vocabulary read from the data instead of hard-coded.

    PRIVACY: ``from_places``/``to_places`` are the registered journeys' own endpoints, which this API
    withheld entirely before 2026-08-21. They are published here as a *vocabulary* and are still not
    served per decision, so a card never says whose trip it was and a full listing does not pair an
    origin with a destination. What this does concede is that the pair can be chosen and the matching
    departures read off; see the header of ``historikk_decisions.sql``.
    """

    from_date: date = Field(description="Window start (operating day, Europe/Oslo).")
    to_date: date = Field(description="Window end, inclusive.")
    operators: list[str] = Field(description="Codespaces occurring on a decision in the window.")
    lines: list[str] = Field(
        description="Line refs occurring in the window, e.g. 'VYG:Line:R11'. Full refs, not the "
        "short code the page shows: the ref is what the decision log stores and what the filter "
        "compares against, and two codespaces can share a short code.",
    )
    from_places: list[PlaceOption] = Field(description="Registered boarding places, by name.")
    to_places: list[PlaceOption] = Field(description="Registered alighting places, by name.")
    channels: list[str] = Field(
        default_factory=list,
        description="The channel values a decision can carry: 'browser', 'webhook', 'both'. A "
        "FIXED vocabulary rather than one read from the window, unlike every list above it — "
        "three values is already a bounded response-cache key, and a channel absent from this "
        "window is a true answer ('none this week') rather than a value to hide, which is what "
        "the data-derived lists cannot say without a second query.",
    )


class Segment(BaseModel):
    """One slice of the window, split on where the decision's information came from.

    Same four verdicts, same arithmetic, over a subset of the same rows — so a caller can put the
    slices side by side and see what the blended figure is actually made of. There are three:
    `predicted` (an SX message existed and the models forecast the outcome), `relayed` (ET had
    already declared it) and `no_sx` (nobody had published anything; the models read real-time data
    alone). Every labelled verdict lands in exactly one, or in `n_unsegmented`.
    """

    n_hit: int = Field(description="Alerted, and trouble came.")
    n_false_alarm: int = Field(description="Alerted, no trouble.")
    n_miss: int = Field(description="Did not alert, trouble came.")
    n_correct_reject: int = Field(description="Did not alert, no trouble.")
    precision: float | None = Field(
        description="hit / (hit + false_alarm) within this segment. Null when it alerted nothing.",
    )
    recall: float | None = Field(
        description="hit / (hit + miss) within this segment, with the same caveat as the blended "
        "figure: among SX-flagged calls on registered journeys only.",
    )
    median_lead_min_on_hits: int | None = Field(
        default=None,
        description="Median minutes between the first alert and the AIMED departure, over this "
        "segment's hit TRAINS — not its hit rows. The traveller gets one alert per train however "
        "many SX messages sit behind it, and counting per situation lets a duplicate published "
        "after the train has gone drag the median below what anybody experienced (89 vs 145 min "
        "blended, measured over 14 days of prd on 2026-08-11). Exact for a cancellation; "
        "understates the usable warning for a delayed train, which left later than aimed. Negative "
        "when the first alert followed the aimed departure — still deliverable, because an alert "
        "is dropped by the train's predicted departure, not its aimed one. Null when the segment "
        "has no hits. This is the figure that splits hardest: 15 min predicted against 177 "
        "relayed.",
    )
    n_hit_with_30min_notice: int = Field(
        default=0,
        description="Hit trains in this segment alerted 30+ min before the aimed departure. "
        "Per train, like the median beside it — read it against n_hit_trains, not n_hit.",
    )
    n_hit_trains: int = Field(
        default=0,
        description="Distinct trains behind this segment's hits, collapsed on varsel's own "
        "one_alert_per_train key. The denominator for the two lead-time figures, and the reason "
        "they can be shown next to counts of situations without the two being confused: 19 "
        "predicted trains against 133 relayed, out of 156, over 14 days of prd on 2026-08-11.",
    )


class Summary(BaseModel):
    """Alerting performance over the window — counts first, rates second, coverage always.

    The headline figures are **blended**: every labelled decision, whether the app forecast the
    trouble or relayed a cancellation SIRI-ET had already declared. That is the honest answer to
    "can a traveller trust what this app tells them", which is what the page is for, and relaying
    what ET already knows is most of the value — nobody carries the ET feed on their phone.

    It is not the answer to "did the models see it coming", because a declared cancellation is
    served at probability 1.0 by construction: a gimme. `predicted` and `relayed` are the same
    window split on exactly that, and are the reason the blended figure can be quoted at all — on
    its own it moves with the **mix** of the two populations, and so can swing with nothing about
    the app having changed.
    """

    from_date: date = Field(description="Window start (operating day, Europe/Oslo).")
    to_date: date = Field(description="Window end, inclusive.")
    n_hit: int = Field(description="Alerted, and trouble came.")
    n_false_alarm: int = Field(description="Alerted, no trouble.")
    n_miss: int = Field(description="Did not alert, trouble came.")
    n_correct_reject: int = Field(description="Did not alert, no trouble.")
    n_unknown: int = Field(
        description="Labelled but unresolvable — no realized call found. Never counted as a "
        "correct rejection.",
    )
    n_pending: int = Field(description="Too recent to have been labelled yet.")
    precision: float | None = Field(
        description="hit / (hit + false_alarm). Null when nothing was alerted.",
    )
    recall: float | None = Field(
        description="hit / (hit + miss) among SX-flagged calls on registered journeys — NOT true "
        "recall over all disruptions, since the models never score an unflagged call. Null when "
        "no trouble was observed.",
    )
    median_lead_min_on_hits: int | None = Field(
        description="Median minutes of warning on the TRAINS we got right — the only figure here "
        "on that grain, since every other one counts decisions and a decision is per situation. "
        "The traveller gets one alert per train, so a second SX message about the same disruption "
        "landing after it has gone must not pull this down: 89 min per situation against 145 per "
        "train, measured over 14 days of prd on 2026-08-11.",
    )
    n_hit_with_30min_notice: int = Field(
        description="Trains alerted 30+ min before departure. Per train, like the median beside "
        "it — its denominator is n_hit_trains, never n_hit.",
    )
    n_hit_trains: int = Field(
        default=0,
        description="Distinct trains behind the hits, collapsed on varsel's one_alert_per_train "
        "key: 156 out of 514 hit rows over 14 days of prd on 2026-08-11.",
    )

    predicted: Segment = Field(
        description="Decisions where ET had NOT declared the cancellation when we decided — the "
        "models' own skill. Tune a threshold on this, not on the blended figure.",
    )
    relayed: Segment = Field(
        description="Decisions where ET had ALREADY declared it: the app repeated a published fact. "
        "Real value to the traveller, no evidence about the models.",
    )
    no_sx: Segment = Field(
        default_factory=lambda: Segment(
            n_hit=0, n_false_alarm=0, n_miss=0, n_correct_reject=0,
            precision=None, recall=None, median_lead_min_on_hits=None,
        ),
        description="Decisions about a train NO SX message named at all, scored from real-time "
        "data alone. Strictly less information than either segment above and its own calibration, "
        "so it is neither the models' skill on published disruptions nor a relayed fact — it is "
        "the answer to 'can we warn people the operator has not warned?'.",
    )
    n_unsegmented: int = Field(
        description="Labelled decisions taken before the flag existed (2026-08-05), in no "
        "segment. While this dominates, the split describes only a slice of the window.",
    )


def _checked_operator(operator: str | None) -> str | None:
    """Reject a codespace this stack does not serve.

    Not cosmetic validation: the value becomes part of the response-cache key, so accepting
    arbitrary strings on a public endpoint lets a caller mint one cache entry and one BigQuery
    query per distinct value. `verdict` is validated the same way for the same reason.
    """
    if operator is not None and operator not in _OPERATORS:
        raise HTTPException(
            status_code=400, detail=f"operator must be one of {', '.join(sorted(_OPERATORS))}."
        )
    return operator


def _checked_channel(channel: str | None) -> str | None:
    """Reject a channel outside `decisions.CHANNELS`.

    :func:`_checked_operator`'s job and its reason — the value lands in the response-cache key on
    an endpoint any Entur employee can reach, so an unbounded vocabulary is one cache entry and one
    BigQuery query per distinct string. Checked against the CONSTANT rather than against the window
    (which is what `_checked_choice` beside it does): the three values are what the writer can
    produce, and 'no webhook route in this window' has to be an empty result rather than a 400 —
    otherwise the page could not show a reader that the client channel is quiet.
    """
    if channel is not None and channel not in decision_log.CHANNELS:
        raise HTTPException(
            status_code=400, detail=f"channel must be one of {', '.join(decision_log.CHANNELS)}."
        )
    return channel


def _checked_choice(value: str | None, field: str, allowed: list[str]) -> str | None:
    """Reject a filter value no decision in the window carries.

    Same job as :func:`_checked_operator` one function up, and for the same reason — the value ends
    up in the response-cache key on a public endpoint — but the vocabulary here comes from the data
    rather than a constant, so it is read from :func:`_options` (itself cached) instead of hard-coded.

    A 400 rather than an empty result on purpose: "line R99 does not appear in this window" and "it
    appears and nothing matched the rest of your filters" are different answers, and a client that
    cannot tell them apart will show the second when it means the first. The page keeps the two
    apart by reconciling its selections against ``/filters`` whenever the window moves.
    """
    if value is not None and value not in allowed:
        raise HTTPException(status_code=400, detail=f"Unknown {field}: not present in this window.")
    return value


def _place_registry(refs: list[str]) -> dict[str, tuple[str | None, str]]:
    """StopPlace ref -> (name, place_id), read from the National Stop Register.

    Read from the registry rather than from main's picker cache, which is what this did first and
    which failed twice over: that cache holds only stops in TODAY's timetable, and it collapses
    co-located refs and drops the absorbed one entirely — so Asker stasjon's rail side came back
    nameless because its bus side had won the collapse. The registry names every ref that has ever
    existed and is the thing that knows the two are one place.

    Fails open to an empty mapping: no registry means no names and no grouping, which is the filters
    exactly as they behaved before this existed rather than a page that will not load.
    """
    if not refs:
        return {}
    from google.cloud import bigquery

    params = [bigquery.ArrayQueryParameter("refs", "STRING", sorted(refs))]
    try:
        rows = _query(_STOP_PLACES_QUERY, params, ("stop_places", tuple(sorted(refs))))
    except HTTPException:
        logger.warning("Stop registry unreadable; filter options will show bare refs", exc_info=True)
        return {}
    out: dict[str, tuple[str | None, str]] = {}
    for r in rows:
        row = dict(r)
        name = row.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else None
        out[row["id"]] = (name, row.get("place_id") or row["id"])
    return out


def _place_groups(counted: list[dict]) -> list[dict]:
    """Collapse `[{ref, n}]` into one entry per physical place, busiest ref first within each.

    The grouping rule is main.refresh_varsel_stops', deliberately copied rather than approximated:
    same NSR parent place AND the same name means one physical stop registered more than once, while
    a parent alone does not (one parent legitimately holds Oslo S and Jernbanetorget, which are not
    the same place and must not answer for each other). An **unnamed** ref is never merged either —
    its name cannot be shown to tell it from its neighbour, so folding it in would hide a ref behind
    a row nobody can read.

    Returns dicts of {ref, name, refs}: `ref` is the busiest member (ties on the ref itself, so the
    nomination cannot flip between refreshes and invalidate the page's selection under it), and
    `refs` is every member — what the query actually filters on.
    """
    registry = _place_registry([c["ref"] for c in counted])
    groups: dict[tuple, list[dict]] = {}
    for entry in counted:
        name, place_id = registry.get(entry["ref"], (None, entry["ref"]))
        key = (place_id, name.casefold()) if name else (entry["ref"], "")
        groups.setdefault(key, []).append({**entry, "name": name})

    out = []
    for members in groups.values():
        members.sort(key=lambda m: (-m["n"], m["ref"]))
        primary = members[0]
        out.append(
            {
                "ref": primary["ref"],
                # From the primary, so the label and the value describe the same stop. Members can
                # only differ in case here -- the grouping key is the casefolded name.
                "name": primary["name"],
                "refs": sorted(m["ref"] for m in members),
            }
        )
    return out


def _options(start: date, end: date) -> dict[str, Any]:
    """The filter vocabulary for the window: two lists of strings and two lists of place groups.

    Keyed on the window alone, so every filter combination on the page shares one cache entry and
    one BigQuery query — which is what makes it affordable to run this on the validation path of
    ``/decisions`` and ``/summary`` as well as for the dropdowns themselves.
    """
    from google.cloud import bigquery

    params = [
        bigquery.ScalarQueryParameter("from_date", "DATE", start),
        bigquery.ScalarQueryParameter("to_date", "DATE", end),
    ]
    rows = _query(_FILTERS_QUERY, params, ("filters", start, end))
    row = dict(rows[0]) if rows else {}

    def counted(field: str) -> list[dict]:
        return [
            {"ref": r["ref"], "n": r.get("n") or 0}
            for r in (row.get(field) or [])
            if (r or {}).get("ref")
        ]

    return {
        "operators": [v for v in (row.get("operators") or []) if v],
        "lines": [v for v in (row.get("lines") or []) if v],
        "from_places": _place_groups(counted("from_places")),
        "to_places": _place_groups(counted("to_places")),
    }


def _with_place_names(rows: list) -> list[Decision]:
    """Swap each row's stop REFS for resolved NSR names, then build the models.

    The refs are what the log records and what the filters match on, but they are not what leaves
    this process: they are popped here and replaced, so `Decision` never carries one and
    `extra="forbid"` keeps it that way. A ref that the registry cannot place falls back to the ref
    itself and then to "Ukjent" — a card reading `NSR:StopPlace:418` is ugly but true, while a blank
    one would read as a journey with no origin. One registry read covers the whole page; it fails
    open to an empty mapping, which degrades every card to its bare refs rather than 500ing a
    listing over a cosmetic lookup.
    """
    raw = [dict(r) for r in rows]
    refs = {ref for row in raw for ref in (row.get("from_stop"), row.get("to_stop")) if ref}
    registry = _place_registry(sorted(refs)) if refs else {}
    out = []
    for row in raw:
        from_ref = row.pop("from_stop", None)
        to_ref = row.pop("to_stop", None)
        row["from_name"] = registry.get(from_ref, (None, from_ref))[0] or from_ref or "Ukjent"
        row["to_name"] = registry.get(to_ref, (None, to_ref))[0] or to_ref or "Ukjent"
        out.append(Decision(**row))
    return out


def _place_refs(groups: list[dict], value: str | None, field: str) -> list[str]:
    """Expand a chosen place into EVERY ref standing for it; `[]` when nothing is chosen.

    The expansion is the whole point of grouping, not a detail of it. The dropdown offers one row
    per physical place and nominates one of its refs as the value; filtering on that ref alone would
    return only the registrations made against that side of the station and silently drop the rest —
    659 of Asker stasjon's 842 decisions, measured on dev 2026-08-21. Empty list means no filter,
    since BigQuery has no NULL array parameter.
    """
    if value is None:
        return []
    for group in groups:
        if group["ref"] == value:
            return group["refs"]
    raise HTTPException(status_code=400, detail=f"Unknown {field}: not present in this window.")


def _checked_places(
    start: date, end: date, line: str | None, from_place: str | None, to_place: str | None
) -> tuple[list[str], list[str]]:
    """Validate the three data-driven filters and expand the places, spending a query only when one
    of them is actually set. Returns the two ref arrays the queries take."""
    if line is None and from_place is None and to_place is None:
        return [], []
    opts = _options(start, end)
    _checked_choice(line, "line", opts["lines"])
    return (
        _place_refs(opts["from_places"], from_place, "from_place"),
        _place_refs(opts["to_places"], to_place, "to_place"),
    )


def _window(from_date: date | None, to_date: date | None) -> tuple[date, date]:
    """Resolve and validate the requested window."""
    today = datetime.now(UTC).date()
    end = to_date or today
    start = from_date or (end - timedelta(days=14))
    if end < start:
        raise HTTPException(status_code=400, detail="to_date must not precede from_date.")
    if (end - start).days > _MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=400, detail=f"Window too wide; keep it within {_MAX_WINDOW_DAYS} days."
        )
    return start, end


def _query(sql: str, params: list, cache_key: tuple):
    """Run a parameterised query behind the TTL cache. Rows are returned as a list."""
    if _bq_client is None:
        raise HTTPException(status_code=503, detail="Decision log unavailable.")
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=_MAX_BYTES_BILLED
    )
    try:
        rows = list(_bq_client.query(sql, job_config=job_config).result())
    except Exception as exc:
        # Most likely cause on a fresh environment: the tables do not exist yet because no decision
        # has been recorded. Say so rather than surfacing a raw BigQuery traceback.
        logger.warning("Decision-log query failed", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Could not read the decision log; it may not have been written to yet.",
        ) from exc
    with _cache_lock:
        # Drop expired entries, then cap. Without this the dict only ever grows: the key carries
        # caller-chosen dates and filters, and this pod does not restart.
        for key in [k for k, (ts, _) in _cache.items() if now - ts >= _CACHE_TTL_S]:
            _cache.pop(key, None)
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            for key in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX_ENTRIES // 5]:
                _cache.pop(key, None)
        _cache[cache_key] = (now, rows)
    return rows


router = APIRouter(prefix="/historikk-api", tags=["historikk"])


@router.get("/config", summary="Which deployment is serving this page")
def config() -> ConfigResponse:
    """The environment name the page appends to its title, or null in prd.

    Its own endpoint rather than a field on ``/summary`` deliberately: both data endpoints 503 when
    the decision log cannot be read (no client, tables not written to yet), which is exactly when
    knowing whether you are looking at dev or prd matters most. This one touches no BigQuery.
    """
    return ConfigResponse(environment=environment_label())


@router.get(
    "/filters",
    summary="The values the page's dropdowns can be set to",
    response_description="Operators, lines and registered places occurring in the window.",
)
def filters(
    from_date: date | None = Query(default=None, description="Window start (default: 14 days ago)."),
    to_date: date | None = Query(default=None, description="Window end, inclusive (default: today)."),
) -> FilterOptions:
    """Every operator, line, boarding place and alighting place a decision in the window carries.

    Scoped to the same window as the data, not to the whole log, so the dropdowns offer exactly what
    the page can show and the scan stays pruned on the outcomes partition. The consequence is that a
    selection can go stale when the window narrows — which is the client's job to reconcile, and the
    reason the data endpoints answer 400 rather than an empty list for a value that is not here.

    Deliberately *not* narrowed by the other filters: options derived from the current selection
    shrink to it, and there is then no way back to the rest of the window.
    """
    start, end = _window(from_date, to_date)
    opts = _options(start, end)

    def places(groups: list[dict]) -> list[PlaceOption]:
        # Sorted by what the reader sees. The SQL sorts by ref, which puts "NSR:StopPlace:337" next
        # to "NSR:StopPlace:338" and their names nowhere near each other.
        #
        # Unnamed refs go LAST rather than under their own text: sorting the two kinds together
        # interleaves them ("Lillestrøm", NSR:StopPlace:3247, ..., "Oslo S"), which reads as a list
        # that lost its ordering. Behind the named ones they read as what they are — the tail the
        # registry could not place. With the registry readable this tail is normally empty.
        options = [
            PlaceOption(ref=g["ref"], name=g["name"], n_refs=len(g["refs"])) for g in groups
        ]
        return sorted(options, key=lambda o: (o.name is None, (o.name or o.ref).casefold()))

    return FilterOptions(
        from_date=start,
        to_date=end,
        operators=opts["operators"],
        lines=opts["lines"],
        from_places=places(opts["from_places"]),
        to_places=places(opts["to_places"]),
        channels=list(decision_log.CHANNELS),
    )


@router.get(
    "/decisions",
    summary="Alert decisions with their realized outcome",
    response_description="Decisions in the window, newest departure first.",
)
def decisions(
    from_date: date | None = Query(default=None, description="Window start (default: 14 days ago)."),
    to_date: date | None = Query(default=None, description="Window end, inclusive (default: today)."),
    operator: str | None = Query(default=None, description="Filter to one codespace, e.g. 'VYG'."),
    line: str | None = Query(
        default=None,
        description="Filter to one line REF, e.g. 'VYG:Line:R11' — not the short code the page "
        "shows, since two codespaces can share one. Values come from /historikk-api/filters.",
    ),
    from_place: str | None = Query(
        default=None,
        description="Filter to registrations boarding at this StopPlace ref. Values come from "
        "/historikk-api/filters, which is also the allow-list this is checked against.",
    ),
    to_place: str | None = Query(
        default=None,
        description="Filter to registrations alighting at this StopPlace ref.",
    ),
    channel: str | None = Query(
        default=None,
        description="Filter to decisions watched on one channel: 'browser' (a /varsel "
        "registration), 'webhook' (a client subscription) or 'both'. Decisions taken before "
        "2026-09-13 carry no channel and are excluded by any value here.",
    ),
    verdict: str | None = Query(
        default=None,
        description="Filter to 'hit', 'false_alarm', 'miss' or 'correct_reject'. Excludes "
        "unresolved decisions, which have no verdict to match.",
    ),
    alerted_only: bool = Query(
        default=False,
        description="Only decisions that crossed the threshold. Leave off to see the decisions we "
        "did NOT alert on — the ones a miss is made of.",
    ),
    resolved_only: bool = Query(
        default=False,
        description="Only decisions whose realized outcome is known (outcome_state 'avklart'), "
        "dropping 'venter' and 'ukjent'. Composes with `verdict`, which narrows further: this is "
        "the whole judged population, that is one verdict within it.",
    ),
    sx_only: bool = Query(
        default=False,
        description="Only decisions about a call an operator published a SIRI-SX message for — "
        "i.e. drop the rows this codebase minted a situation number for (`KIX:`): the no-SX "
        "predictions and the ET-cancellation relay, neither of which has operator text behind it.",
    ),
    limit: int = Query(default=200, ge=1, le=_MAX_LIMIT, description="Maximum rows to return."),
) -> DecisionsResponse:
    """Every alert decision in the window, whether or not it produced a push.

    Non-alerting decisions are included by default on purpose: they are the only evidence a miss
    leaves behind, and a log filtered to what we did send can only ever measure precision.
    """
    from google.cloud import bigquery

    start, end = _window(from_date, to_date)
    operator = _checked_operator(operator)
    channel = _checked_channel(channel)
    from_refs, to_refs = _checked_places(start, end, line, from_place, to_place)
    if verdict is not None and verdict not in _VERDICTS:
        raise HTTPException(
            status_code=400, detail=f"verdict must be one of {', '.join(_VERDICTS)}."
        )
    params = [
        bigquery.ScalarQueryParameter("from_date", "DATE", start),
        bigquery.ScalarQueryParameter("to_date", "DATE", end),
        bigquery.ScalarQueryParameter("operator", "STRING", operator),
        bigquery.ScalarQueryParameter("line", "STRING", line),
        bigquery.ScalarQueryParameter("channel", "STRING", channel),
        bigquery.ArrayQueryParameter("from_place_refs", "STRING", from_refs),
        bigquery.ArrayQueryParameter("to_place_refs", "STRING", to_refs),
        bigquery.ScalarQueryParameter("verdict", "STRING", verdict),
        bigquery.ScalarQueryParameter("alerted_only", "BOOL", alerted_only),
        bigquery.ScalarQueryParameter("resolved_only", "BOOL", resolved_only),
        bigquery.ScalarQueryParameter("sx_only", "BOOL", sx_only),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    key = (
        "decisions", start, end, operator, line, from_place, to_place, channel,
        verdict, alerted_only, resolved_only, sx_only, limit,
    )
    rows = _query(_DECISIONS_QUERY, params, key)
    return DecisionsResponse(
        from_date=start,
        to_date=end,
        count=len(rows),
        decisions=_with_place_names(rows),
    )


@router.get(
    "/summary",
    summary="Alerting performance over a window",
    response_description="Verdict counts, precision/recall, and how much of the window is resolved.",
)
def summary(
    from_date: date | None = Query(default=None, description="Window start (default: 14 days ago)."),
    to_date: date | None = Query(default=None, description="Window end, inclusive (default: today)."),
    operator: str | None = Query(default=None, description="Filter to one codespace, e.g. 'VYG'."),
    line: str | None = Query(default=None, description="Filter to one line ref, e.g. 'VYG:Line:R11'."),
    from_place: str | None = Query(
        default=None, description="Filter to registrations boarding at this StopPlace ref."
    ),
    to_place: str | None = Query(
        default=None, description="Filter to registrations alighting at this StopPlace ref."
    ),
    channel: str | None = Query(
        default=None,
        description="Filter to decisions watched on one channel: 'browser' (a /varsel "
        "registration), 'webhook' (a client subscription) or 'both'. Decisions taken before "
        "2026-09-13 carry no channel and are excluded by any value here.",
    ),
) -> Summary:
    """Counts and rates over the window, with the unresolved share reported alongside.

    `n_unknown` and `n_pending` are part of the contract, not diagnostics: a precision computed
    over a fifth of the decisions is a different claim from one computed over almost all of them,
    and a caller cannot tell which it has without them.

    The five filters it accepts are exactly the ones that narrow the POPULATION — which trains, on
    whose registered journeys, watched from where — so these rates describe the same slice the list
    beside them shows.
    `verdict`, `alerted_only` and `resolved_only` narrow by OUTCOME instead and are deliberately not
    accepted here at all: filtering to hits would make precision read 100 %.
    """
    from google.cloud import bigquery

    start, end = _window(from_date, to_date)
    operator = _checked_operator(operator)
    channel = _checked_channel(channel)
    from_refs, to_refs = _checked_places(start, end, line, from_place, to_place)
    params = [
        bigquery.ScalarQueryParameter("from_date", "DATE", start),
        bigquery.ScalarQueryParameter("to_date", "DATE", end),
        bigquery.ScalarQueryParameter("operator", "STRING", operator),
        bigquery.ScalarQueryParameter("line", "STRING", line),
        bigquery.ScalarQueryParameter("channel", "STRING", channel),
        bigquery.ArrayQueryParameter("from_place_refs", "STRING", from_refs),
        bigquery.ArrayQueryParameter("to_place_refs", "STRING", to_refs),
    ]
    rows = _query(
        _SUMMARY_QUERY,
        params,
        ("summary", start, end, operator, line, from_place, to_place, channel),
    )
    row = dict(rows[0]) if rows else {}

    def segment(suffix: str) -> Segment:
        """One split of the window. Counts default to 0; rates stay None — a rate over nothing is
        not zero, and rendering it as 0 % would read as "we got everything wrong"."""
        return Segment(
            n_hit=row.get(f"n_hit_{suffix}") or 0,
            n_false_alarm=row.get(f"n_false_alarm_{suffix}") or 0,
            n_miss=row.get(f"n_miss_{suffix}") or 0,
            n_correct_reject=row.get(f"n_correct_reject_{suffix}") or 0,
            precision=row.get(f"precision_{suffix}"),
            recall=row.get(f"recall_{suffix}"),
            median_lead_min_on_hits=row.get(f"median_lead_min_on_hits_{suffix}"),
            n_hit_with_30min_notice=row.get(f"n_hit_with_30min_notice_{suffix}") or 0,
            n_hit_trains=row.get(f"n_hit_trains_{suffix}") or 0,
        )

    return Summary(
        from_date=start,
        to_date=end,
        n_hit=row.get("n_hit") or 0,
        n_false_alarm=row.get("n_false_alarm") or 0,
        n_miss=row.get("n_miss") or 0,
        n_correct_reject=row.get("n_correct_reject") or 0,
        n_unknown=row.get("n_unknown") or 0,
        n_pending=row.get("n_pending") or 0,
        precision=row.get("precision"),
        recall=row.get("recall"),
        median_lead_min_on_hits=row.get("median_lead_min_on_hits"),
        n_hit_with_30min_notice=row.get("n_hit_with_30min_notice") or 0,
        n_hit_trains=row.get("n_hit_trains") or 0,
        predicted=segment("predicted"),
        relayed=segment("relayed"),
        no_sx=segment("no_sx"),
        n_unsegmented=row.get("n_unsegmented") or 0,
    )
