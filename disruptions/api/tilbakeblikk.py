"""/tilbakeblikk-api — replay a window of past alert decisions through today's prompt.

/historikk answers "what did we send, and was it right". This surface answers "what would today's
prompt say about the same disruptions": it reads the ``message_input`` document already stored on
every decision row and re-runs the current prompt over it. Same filters, same stats, same cards; a
different window axis, and a wording that is generated rather than quoted.

**The window axis is the whole difference.** /historikk windows on ``call_date`` — the operating day
of the train. This page windows on ``decided_at`` — when the decision was taken — as a single day
plus a from/to time of day, all Europe/Oslo. That is what a prompt-iteration loop needs: a stretch
of real cycles whose recorded inputs can be replayed. It is also, incidentally, the first query in
this stack that prunes its main input, because ``varsel_decisions`` is ``PARTITION BY
DATE(decided_at)``.

**Both times are quantized to 15-minute steps and the range is half-open ``[from, to)``.** That is
not cosmetic: the endpoint is public and unauthenticated and every parameter lands in the response
cache key, so two free-form second-resolution times are on the order of a billion distinct keys per
day, each one a cache entry and a BigQuery query. ``from_time`` rounds down and ``to_time`` rounds
up, so a narrowed range never drops a minute the caller asked for, and the whole-day default runs
00:00 to the FOLLOWING midnight — 23:59:59.999999 is not on a 15-minute grid, and quantizing it down
would silently drop the last quarter-hour of every default request.

**This module is U1 of the plan and serves four routes only** — ``config``, ``filters``,
``decisions`` and ``summary``. The regeneration path (``/messages``) is a separate unit; the query
that feeds it (``tilbakeblikk_messages.sql``) is created here because that is the file that carries
the privacy boundary, and because :func:`configure` is where its client comes from.

Privacy is inherited verbatim from /historikk (KTD10) and extended by one column. No decision row
carries ``journey_digest``, ``from_stop``, ``to_stop`` — or ``message_input``, which embeds all three
of origin, destination and departure time in one blob. :class:`Decision` is ``extra="forbid"``, so a
future SELECT that re-adds a redacted column fails loudly here rather than quietly publishing it.
``/filters`` publishes place refs as the dropdown vocabulary exactly as /historikk's does; the
redaction is per row, not per response, or the Fra/Til filters could not exist at all.

Reads only. No import of main (the package rule): main injects the BigQuery client via
:func:`configure`. It DOES import :mod:`historikk`, for the constants the two surfaces must not let
drift — the served codespaces, the verdict vocabulary and the lookback bound — and it reads
``historikk_stop_places.sql`` as-is rather than cloning it. The rest of /historikk's logic is forked
rather than shared, which the plan accepts explicitly: a fix to one side must be mirrored to the
other.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import historikk
from deployment import environment_label
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_DECISIONS_QUERY = (Path(__file__).parent / "tilbakeblikk_decisions.sql").read_text()
_SUMMARY_QUERY = (Path(__file__).parent / "tilbakeblikk_summary.sql").read_text()
_FILTERS_QUERY = (Path(__file__).parent / "tilbakeblikk_filters.sql").read_text()
# Read but not routed here: /tilbakeblikk-api/messages is a later unit. It is loaded at import for
# the same reason as the other three -- a missing .sql becomes an import error that stops the whole
# app at startup rather than a 500 on the first page load.
_MESSAGES_QUERY = (Path(__file__).parent / "tilbakeblikk_messages.sql").read_text()
# /historikk's file, deliberately SHARED rather than cloned: it takes only @refs, has no window of
# its own, and fails open to bare refs. Two copies would be two things to keep in step for no gain.
_STOP_PLACES_QUERY = (Path(__file__).parent / "historikk_stop_places.sql").read_text()

# The decision log only changes once a poller cycle, so a short cache keeps a page refresh from
# costing a BigQuery query. Capped because the key carries caller-chosen values on a public endpoint
# and this pod never restarts. 100 rather than /historikk's 500: each entry can hold up to `limit`
# hydrated rows, the pod's memory limit is the binding constraint (1228 Mi in dev, forceReplicas 1,
# uvicorn binds only after lifespan's eager loads, so an OOMKill is zero capacity for a cold start),
# and this surface adds a second cache of its own for wordings.
_CACHE_TTL_S = 120
_CACHE_MAX_ENTRIES = 100
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, Any]] = {}

_OSLO = ZoneInfo("Europe/Oslo")

# The grid both ends of the range snap to. See the module docstring for why quantizing at all is the
# bound that makes a public endpoint keyed on two times affordable.
_STEP_MINUTES = 15
_MINUTES_PER_DAY = 24 * 60

_MAX_LIMIT = historikk._MAX_LIMIT

# A ceiling on what one request may bill, on the job config rather than in a comment. Nothing else in
# this repo sets one, and KTD3's cost argument is a pruning claim no test can check: the suites are
# hermetic and no .sql here is ever executed by a test.
#
# Sized from measurement (2026-08-21): the WHOLE decision log is 181.6 MB / 161 391 rows in prd and
# 135 MB / 111 980 rows in dev, and a correctly pruned three-partition read of it is a few MB. So be
# honest about what this does and does not do — at ~11x the entire unpruned log it will NOT catch a
# pruning regression on today's table. What it does catch is a query that starts reading something
# big: the ET tables a mistyped join could reach are ~10 TiB for a two-year scan, three orders of
# magnitude above this. It is a blast-radius bound on a public endpoint, not a pruning detector.
# Revisit the number when the decision log approaches it — at that point it becomes both.
_MAX_BYTES_BILLED = 2 * 1024**3

_bq_client: Any | None = None


def configure(*, bq_client: Any) -> None:
    """Wire in the BigQuery client. Called once from main.lifespan."""
    global _bq_client
    _bq_client = bq_client


class Decision(BaseModel):
    """One alert decision replayed: what we concluded, when it could have been said, what happened.

    /historikk's :class:`~historikk.Decision` field for field, with three differences.

    **Added**: ``would_send_at``, ``first_in_window`` and ``prompt_version`` — the replayed cycle,
    whether it was the subject's first opportunity, and which prompt wrote the row's own wording.

    **Removed**: ``message`` and ``message_source``. The stored wording is deliberately not on this
    card (R12). Showing the pushed text beside one regenerated from a possibly different cycle would
    reintroduce exactly the mismatch the 2026-08-21 fix removed from /historikk, where 41 % of cards
    quoted text nobody received.

    **Re-scoped**: the per-cycle aggregates below are computed over the cycles INSIDE the window,
    while the verdict comes whole from the nightly labelling job. A subject can therefore legitimately
    read ``ever_alerted: false`` beside ``verdict: 'hit'`` — it alerted outside the window the reader
    chose. Each such field says so; the page labels them rather than letting them read as
    /historikk's.

    Describes a *train*, not a traveller. The journey definition behind the decision
    (``journey_digest``, ``from_stop``, ``to_stop``) and the writer's input (``message_input``) are
    all absent: this endpoint is served unauthenticated on a public host, and those next to
    ``departure_time`` and ``n_devices`` would publish "somebody commutes A->B at 07:15".
    ``extra="forbid"`` is the guard that keeps that true — the SQL and this model must agree column
    for column, so a future SELECT that re-adds one fails loudly here.
    """

    model_config = ConfigDict(extra="forbid")

    situation_number: str | None = Field(description="SIRI-SX situationNumber.")
    summary: str | None = Field(description="Short Norwegian summary of the situation.")
    description: str | None = Field(description="The operator's fuller explanation.")
    operator: str | None = Field(description="Ref codespace: VYG, GOA, RUT, ...")
    line: str | None = Field(description="Line ref of the matched train.")
    mode: str | None = Field(description="Vehicle noun: 'Tog' or 'Buss'.")
    call_date: date | None = Field(description="Operating day of the boarding call (Europe/Oslo).")
    departure_time: datetime | None = Field(
        description="Aimed departure from the boarding stop (UTC).",
    )

    n_decisions: int | None = Field(
        description="WINDOW-SCOPED: refresh cycles that scored this train INSIDE the selected "
        "window, not over its whole life.",
    )
    first_decided_at: datetime | None = Field(
        description="WINDOW-SCOPED: first time it was scored inside the window (UTC).",
    )
    last_decided_at: datetime | None = Field(
        description="WINDOW-SCOPED: last time it was scored inside the window (UTC).",
    )
    ever_alerted: bool | None = Field(
        description="WINDOW-SCOPED: whether any cycle INSIDE the window crossed the alert "
        "threshold. False beside a 'hit' verdict is not a contradiction — it means the alerting "
        "happened outside the window.",
    )
    first_alert_at: datetime | None = Field(
        description="WINDOW-SCOPED: when we first crossed the threshold inside the window (UTC); "
        "null if we never did there.",
    )
    alert_lead_min: int | None = Field(
        description="WINDOW-SCOPED: departure minus first_alert_at, in minutes. Computed from the "
        "window's first alert rather than copied from the nightly job's whole-subject figure, so it "
        "and first_alert_at beside it describe the same cycle.",
    )
    max_impact_probability: float | None = Field(
        description="WINDOW-SCOPED: highest alerting score reached inside the window.",
    )
    final_impact_probability: float | None = Field(
        description="WINDOW-SCOPED: score at the last cycle inside the window.",
    )
    final_cancel_probability: float | None = Field(
        description="WINDOW-SCOPED: cancellation probability at that cycle.",
    )
    final_delay_p50_min: int | None = Field(
        description="WINDOW-SCOPED: median predicted departure delay (min) at that cycle.",
    )
    final_delay_p90_min: int | None = Field(
        description="WINDOW-SCOPED: bad-case predicted departure delay (min) at that cycle.",
    )
    alert_threshold: float | None = Field(description="The operator's threshold applied here.")
    et_cancelled: bool | None = Field(
        description="SIRI-ET had already declared this boarding call cancelled when the decision "
        "was made, so the probabilities beside it are 1.0 by construction: we relayed a fact rather "
        "than predicted one. Taken from the nightly job where it has run, so it agrees with the "
        "segment /summary counted this decision in. Null for decisions taken before 2026-08-05.",
    )
    pushed_any: bool | None = Field(
        description="WINDOW-SCOPED: whether a Web Push was actually delivered from a cycle inside "
        "the window.",
    )
    n_devices: int | None = Field(description="How many registrations shared this journey.")

    would_send_at: datetime | None = Field(
        description="When the replayed message could have been sent (UTC): the decided_at of the "
        "FIRST alert-worthy cycle inside the window that recorded a writer's input. Nothing was "
        "sent on this page, so this is the honest reading of 'Varsel kl.' — read it with "
        "first_in_window. Null means no cycle in the window was replayable at all, which is its own "
        "state and never 'no message'.",
    )
    first_in_window: bool = Field(
        description="Whether would_send_at is the subject's FIRST alert-worthy cycle anywhere, not "
        "merely the earliest one this window happens to contain. False means the train was already "
        "alerting before the window opened — narrowing the picker would otherwise silently move the "
        "claimed send time later — or that nothing was replayable.",
    )
    prompt_version: str | None = Field(
        description="SHA-256[:8] of the system prompt in force when the replayed cycle was written. "
        "Compare with llm_message.PROMPT_VERSION to see whether the row predates the current "
        "prompt. It does NOT separate a prompt change from sampling variance: each wording is a "
        "single draw from a non-deterministic decoder.",
    )

    actual_cancelled: bool | None = Field(description="ET declared the boarding call cancelled.")
    actual_dep_delay_min: int | None = Field(description="Realized departure delay (min).")
    actual_trouble: bool | None = Field(
        description="Realized: cancelled OR >= 10 min late. Null means UNKNOWN, not 'no trouble'.",
    )
    verdict: str | None = Field(
        description="'hit' | 'false_alarm' | 'miss' | 'correct_reject'; null when unresolved. Comes "
        "WHOLE from the nightly job — it judges the subject's entire life, not the window.",
    )
    outcome_state: str = Field(
        description="'avklart' (labelled), 'venter' (too recent to label) or 'ukjent' (labelled, "
        "but no realized call was found — permanent). A same-day window is almost entirely 'venter'.",
    )


class WindowFields(BaseModel):
    """The resolved window, echoed on every data response so a client never has to infer it.

    Four fields for one range, because two of them are what a picker needs and two are what a
    contract needs. ``from_time``/``to_time`` are the QUANTIZED times of day, which is what the page
    puts back into its 15-minute-step inputs; ``window_start``/``window_end`` are the exact half-open
    instants, so nothing about timezones or DST has to be re-derived downstream.

    ``to_time`` of ``00:00`` means the FOLLOWING midnight — the whole-day default. It cannot mean
    anything else: a literal 00:00 end would have to be at or before the start, which is rejected.
    """

    day: date = Field(description="The Europe/Oslo calendar day the decisions were taken on.")
    from_time: time = Field(description="Quantized start of the range, Europe/Oslo (inclusive).")
    to_time: time = Field(
        description="Quantized end of the range, Europe/Oslo (EXCLUSIVE). 00:00 means the "
        "following midnight, i.e. the rest of the day.",
    )
    window_start: datetime = Field(description="Exact first instant of the range (inclusive, UTC).")
    window_end: datetime = Field(description="Exact end of the range (EXCLUSIVE, UTC).")


class DecisionsResponse(WindowFields):
    """Decisions taken inside the window, newest departure first."""

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

    A station's rail and bus sides are separate NSR refs sharing a ``parentRef``, and a registration
    lands on whichever one it was made against — so the two are offered as a single row here. ``ref``
    is the row's identity and ``n_refs`` says how many refs it stands for; the server expands it back
    to all of them when filtering, which is the part that keeps the merge from hiding half the
    place's decisions behind the row.
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


class FilterOptions(WindowFields):
    """What the page's dropdowns can be set to, over one window.

    Its own endpoint rather than something the client derives from ``/decisions``: that response is
    capped and ordered by departure, so options taken from it would quietly omit anything that only
    ran early in the window — and, once a filter were applied, would shrink to the selected value.

    This is also the API's allow-list. A value not in these arrays is rejected by ``/decisions`` and
    ``/summary`` rather than passed through, so a caller cannot mint an unbounded number of cache
    entries — and BigQuery queries — out of arbitrary strings. It follows that the page must fetch
    these BEFORE the data on every window change, and clear a selection the narrowed window no longer
    contains, or a window change would flash an error the user never caused.

    PRIVACY: ``from_places``/``to_places`` are the registered journeys' own endpoints, published here
    as a *vocabulary* and never served per decision. The concession — that a pair can be chosen and
    the matching departures read off — is inherited from /historikk's 2026-08-21 reversal, not newly
    made here.
    """

    operators: list[str] = Field(description="Codespaces occurring on a decision in the window.")
    lines: list[str] = Field(
        description="Line refs occurring in the window, e.g. 'VYG:Line:R11'. Full refs, not the "
        "short code the page shows: the ref is what the decision log stores and what the filter "
        "compares against, and two codespaces can share a short code.",
    )
    from_places: list[PlaceOption] = Field(description="Registered boarding places, by name.")
    to_places: list[PlaceOption] = Field(description="Registered alighting places, by name.")


class Segment(BaseModel):
    """One slice of the window, split on where the decision's information came from.

    Same four verdicts, same arithmetic, over a subset of the same rows. There are three:
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
        "segment's hit TRAINS — not its hit rows. Comes whole from the nightly job, like the "
        "verdict, so it is the subject's real first alert and NOT the window-scoped lead the cards "
        "show; that is what keeps it comparable with /historikk's. Null when the segment has no "
        "hits.",
    )
    n_hit_with_30min_notice: int = Field(
        default=0,
        description="Hit trains in this segment alerted 30+ min before the aimed departure. "
        "Per train, like the median beside it — read it against n_hit_trains, not n_hit.",
    )
    n_hit_trains: int = Field(
        default=0,
        description="Distinct trains behind this segment's hits, collapsed on varsel's own "
        "one_alert_per_train key. The denominator for the two lead-time figures.",
    )


class Summary(WindowFields):
    """Alerting performance over the window — coverage first, then counts, then rates.

    **Coverage is first here, and it is not a diagnostic.** Verdicts arrive from the 04:00 UTC
    labelling job the following night, so on this page's default view — today, all day — nearly every
    subject is still ``venter`` and the rates below are computed over a handful of rows.
    /historikk avoids this by defaulting to 14 days on ``call_date``; this page cannot, because the
    question it answers is about a stretch of a specific day. ``n_subjects`` and ``n_labelled`` are
    what let it say so instead of showing confident-looking numbers.

    The headline figures are otherwise **blended**: every labelled decision, whether the app forecast
    the trouble or relayed a cancellation SIRI-ET had already declared. That is the honest answer to
    "can a traveller trust what this app tells them". It is not the answer to "did the models see it
    coming", because a declared cancellation is served at probability 1.0 by construction. The
    segments are the same window split on exactly that, and are the reason the blended figure can be
    quoted at all — on its own it moves with the MIX of the populations.

    Narrowed by the POPULATION filters only (day, time range, operator, line, from_place, to_place).
    ``verdict``, ``alerted_only`` and ``resolved_only`` narrow by OUTCOME and are deliberately not
    accepted: filtering to hits would make precision read 100 %.
    """

    n_subjects: int = Field(
        description="Every decision subject in the window — the population the card list shows. The "
        "denominator the rates below must be read against; n_labelled + n_unknown + n_pending.",
    )
    n_labelled: int = Field(
        description="How many of those carry a verdict. On a same-day window this is a few per "
        "cent, and the rates describe only that slice.",
    )
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
        description="Median minutes of warning on the TRAINS we got right. Whole-subject, from the "
        "nightly job — not the window-scoped alert_lead_min the cards carry.",
    )
    n_hit_with_30min_notice: int = Field(
        description="Trains alerted 30+ min before departure. Per train, like the median beside "
        "it — its denominator is n_hit_trains, never n_hit.",
    )
    n_hit_trains: int = Field(
        default=0,
        description="Distinct trains behind the hits, collapsed on varsel's one_alert_per_train key.",
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
        "data alone. Strictly less information than either segment above and its own calibration.",
    )
    n_unsegmented: int = Field(
        description="Labelled decisions taken before the flag existed (2026-08-05), in no segment.",
    )


class _Window(NamedTuple):
    """A resolved, quantized, half-open window: one Oslo day and a range of instants inside it."""

    day: date
    from_minutes: int
    to_minutes: int
    from_ts: datetime
    to_ts: datetime

    @property
    def cache_key(self) -> tuple:
        """What identifies this window in the response cache.

        The QUANTIZED minutes, never the caller's raw times — that is what makes two requests inside
        the same 15-minute step share one entry and one BigQuery query, which is the bound the
        quantization exists for.
        """
        return (self.day, self.from_minutes, self.to_minutes)

    def fields(self) -> dict[str, Any]:
        """The window half of every response body."""
        return {
            "day": self.day,
            "from_time": _as_time(self.from_minutes),
            "to_time": _as_time(self.to_minutes),
            "window_start": self.from_ts,
            "window_end": self.to_ts,
        }


def _as_time(minutes: int) -> time:
    """Minutes-from-midnight as a time of day; the following midnight renders as 00:00.

    Unambiguous in a response because a literal 00:00 end is impossible: it would have to be at or
    before the start, which :func:`_resolve_window` rejects.
    """
    return time(hour=(minutes // 60) % 24, minute=minutes % 60)


def _oslo_instant(day: date, minutes: int) -> datetime:
    """The instant `minutes` after midnight on `day`, Europe/Oslo — 1440 being the next midnight.

    Wall-clock arithmetic on an aware datetime, which is what makes the whole-day default correct on
    the two days a year that are 23 or 25 hours long: midnight + 1440 min is the NEXT midnight, not
    "24 hours later".

    The DST edges are defined rather than special-cased. A time inside the spring-forward gap
    (02:00-03:00) resolves through the pre-transition offset, so 02:15 becomes 03:15 local; an
    ambiguous fall-back time takes the first (summer-time) occurrence. Both are one-off quarter-hours
    a year on a tuning surface, and both are stable rather than erroring.
    """
    return datetime.combine(day, time(0, 0), tzinfo=_OSLO) + timedelta(minutes=minutes)


def _floor_to_step(t: time) -> int:
    """Minutes from midnight, rounded DOWN to the 15-minute grid."""
    return (t.hour * 60 + t.minute) // _STEP_MINUTES * _STEP_MINUTES


def _ceil_to_step(t: time) -> int:
    """Minutes from midnight, rounded UP to the 15-minute grid; may be 1440 (the next midnight).

    Seconds count: 14:00:01 rounds up to 14:15, because the caller asked for a moment after 14:00
    and the range is half-open. A time already exactly on the grid stays where it is — snapping it
    forward would widen every boundary-aligned request by a quarter-hour for nothing.
    """
    total = t.hour * 60 + t.minute + (1 if (t.second or t.microsecond) else 0)
    return -(-total // _STEP_MINUTES) * _STEP_MINUTES


def _resolve_window(
    day: date | None, from_time: time | None, to_time: time | None
) -> _Window:
    """Resolve and validate the requested day and time range.

    Bounded in BOTH dimensions, and both bounds exist for the same reason: every value here lands in
    the response-cache key on a public unauthenticated endpoint, where an unbounded key space is an
    unbounded number of cache entries and BigQuery queries.

    * The day defaults to today in Europe/Oslo. A future day is rejected (there are no decisions
      there, so it can only be key-space) and so is one further back than
      ``historikk._MAX_WINDOW_DAYS`` — the same lookback /historikk bounds its window by.
    * The times default to the whole day, 00:00 to the FOLLOWING midnight, and are quantized to
      15-minute steps: from down, to up, range half-open.

    ``to_time`` at or before ``from_time`` is a 400 rather than an empty page. Equal endpoints are
    rejected too, not only inverted ones: after quantization an equal pair is an empty range exactly
    when it lands on a step boundary and a non-empty one otherwise, and a rule whose behaviour
    depends on where the clock hands happen to be is worse than a refusal.
    """
    today = datetime.now(_OSLO).date()
    day = day or today
    if day > today:
        raise HTTPException(status_code=400, detail="day must not be in the future.")
    if (today - day).days > historikk._MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"day is too far back; keep it within {historikk._MAX_WINDOW_DAYS} days.",
        )
    if from_time is not None and to_time is not None and to_time <= from_time:
        raise HTTPException(status_code=400, detail="to_time must be later than from_time.")

    from_minutes = _floor_to_step(from_time) if from_time is not None else 0
    to_minutes = _ceil_to_step(to_time) if to_time is not None else _MINUTES_PER_DAY
    return _Window(
        day=day,
        from_minutes=from_minutes,
        to_minutes=to_minutes,
        from_ts=_oslo_instant(day, from_minutes),
        to_ts=_oslo_instant(day, to_minutes),
    )


def _checked_operator(operator: str | None) -> str | None:
    """Reject a codespace this stack does not serve.

    Not cosmetic validation: the value becomes part of the response-cache key, so accepting arbitrary
    strings on a public endpoint lets a caller mint one cache entry and one BigQuery query per
    distinct value. The vocabulary is /historikk's, imported rather than copied so the two surfaces
    cannot come to disagree about which operators exist.
    """
    if operator is not None and operator not in historikk._OPERATORS:
        raise HTTPException(
            status_code=400,
            detail=f"operator must be one of {', '.join(sorted(historikk._OPERATORS))}.",
        )
    return operator


def _checked_choice(value: str | None, field: str, allowed: list[str]) -> str | None:
    """Reject a filter value no decision in the window carries.

    Same job as :func:`_checked_operator`, and for the same reason, but the vocabulary comes from the
    data rather than a constant, so it is read from :func:`_options` (itself cached).

    A 400 rather than an empty result on purpose: "line R99 was not decided on in this window" and
    "it was, and nothing matched the rest of your filters" are different answers, and a client that
    cannot tell them apart will show the second when it means the first.
    """
    if value is not None and value not in allowed:
        raise HTTPException(status_code=400, detail=f"Unknown {field}: not present in this window.")
    return value


def _place_registry(refs: list[str]) -> dict[str, tuple[str | None, str]]:
    """StopPlace ref -> (name, place_id), read from the National Stop Register.

    Read from the registry rather than from main's picker cache, which is what /historikk did first
    and which failed twice over: that cache holds only stops in TODAY's timetable, and it collapses
    co-located refs and drops the absorbed one entirely — so Asker stasjon's rail side came back
    nameless because its bus side had won the collapse. The registry names every ref that has ever
    existed and is the thing that knows the two are one place.

    Fails open to an empty mapping: no registry means no names and no grouping, which is a dropdown
    of bare refs rather than a page that will not load.
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
    a parent alone does not (one parent legitimately holds Oslo S and Jernbanetorget). An **unnamed**
    ref is never merged either — its name cannot be shown to tell it from its neighbour, so folding
    it in would hide a ref behind a row nobody can read.

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


def _options(window: _Window) -> dict[str, Any]:
    """The filter vocabulary for the window: two lists of strings and two lists of place groups.

    Keyed on the window alone, so every filter combination on the page shares one cache entry and one
    BigQuery query — which is what makes it affordable to run this on the validation path of
    ``/decisions`` and ``/summary`` as well as for the dropdowns themselves.
    """
    from google.cloud import bigquery

    params = [
        bigquery.ScalarQueryParameter("day", "DATE", window.day),
        bigquery.ScalarQueryParameter("from_ts", "TIMESTAMP", window.from_ts),
        bigquery.ScalarQueryParameter("to_ts", "TIMESTAMP", window.to_ts),
    ]
    rows = _query(_FILTERS_QUERY, params, ("filters", *window.cache_key))
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


def _place_refs(groups: list[dict], value: str | None, field: str) -> list[str]:
    """Expand a chosen place into EVERY ref standing for it; `[]` when nothing is chosen.

    The expansion is the whole point of grouping, not a detail of it. The dropdown offers one row per
    physical place and nominates one of its refs as the value; filtering on that ref alone would
    return only the registrations made against that side of the station and silently drop the rest —
    659 of Asker stasjon's 842 decisions on /historikk, measured on dev 2026-08-21. Empty list means
    no filter, since BigQuery has no NULL array parameter.
    """
    if value is None:
        return []
    for group in groups:
        if group["ref"] == value:
            return group["refs"]
    raise HTTPException(status_code=400, detail=f"Unknown {field}: not present in this window.")


def _checked_places(
    window: _Window, line: str | None, from_place: str | None, to_place: str | None
) -> tuple[list[str], list[str]]:
    """Validate the three data-driven filters and expand the places, spending a query only when one
    of them is actually set. Returns the two ref arrays the queries take."""
    if line is None and from_place is None and to_place is None:
        return [], []
    opts = _options(window)
    _checked_choice(line, "line", opts["lines"])
    return (
        _place_refs(opts["from_places"], from_place, "from_place"),
        _place_refs(opts["to_places"], to_place, "to_place"),
    )


def _query(sql: str, params: list, cache_key: tuple):
    """Run a parameterised query behind the TTL cache. Rows are returned as a list."""
    if _bq_client is None:
        raise HTTPException(status_code=503, detail="Decision log unavailable.")
    now = monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    from google.cloud import bigquery

    # maximum_bytes_billed is the backstop for a cost claim nothing else can check; see the constant.
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
        # caller-chosen days, times and filters, and this pod does not restart.
        for key in [k for k, (ts, _) in _cache.items() if now - ts >= _CACHE_TTL_S]:
            _cache.pop(key, None)
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            for key in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX_ENTRIES // 5]:
                _cache.pop(key, None)
        _cache[cache_key] = (now, rows)
    return rows


router = APIRouter(prefix="/tilbakeblikk-api", tags=["tilbakeblikk"])


@router.get("/config", summary="Which deployment is serving this page")
def config() -> ConfigResponse:
    """The environment name the page appends to its title, or null in prd.

    Its own endpoint rather than a field on ``/summary`` deliberately: both data endpoints 503 when
    the decision log cannot be read, which is exactly when knowing whether you are looking at dev or
    prd matters most. This one touches no BigQuery.
    """
    return ConfigResponse(environment=environment_label())


@router.get(
    "/filters",
    summary="The values the page's dropdowns can be set to",
    response_description="Operators, lines and registered places decided on inside the window.",
)
def filters(
    day: date | None = Query(default=None, description="Oslo day of the decisions (default: today)."),
    from_time: time | None = Query(
        default=None,
        description="Start of the time range, Europe/Oslo (default: 00:00). Rounded DOWN to a "
        "15-minute step.",
    ),
    to_time: time | None = Query(
        default=None,
        description="End of the time range, Europe/Oslo, EXCLUSIVE (default: the following "
        "midnight). Rounded UP to a 15-minute step.",
    ),
) -> FilterOptions:
    """Every operator, line, boarding place and alighting place decided on inside the window.

    Scoped to the same window as the data, not to the whole log, so the dropdowns offer exactly what
    the page can show. The consequence is that a selection can go stale when the window narrows —
    which is the client's job to reconcile, and the reason the data endpoints answer 400 rather than
    an empty list for a value that is not here.

    Deliberately *not* narrowed by the other filters: options derived from the current selection
    shrink to it, and there is then no way back to the rest of the window.
    """
    window = _resolve_window(day, from_time, to_time)
    opts = _options(window)

    def places(groups: list[dict]) -> list[PlaceOption]:
        # Sorted by what the reader sees. The SQL sorts by ref, which puts "NSR:StopPlace:337" next
        # to "NSR:StopPlace:338" and their names nowhere near each other.
        #
        # Unnamed refs go LAST rather than under their own text: sorting the two kinds together
        # interleaves them, which reads as a list that lost its ordering. Behind the named ones they
        # read as what they are — the tail the registry could not place.
        options = [PlaceOption(ref=g["ref"], name=g["name"], n_refs=len(g["refs"])) for g in groups]
        return sorted(options, key=lambda o: (o.name is None, (o.name or o.ref).casefold()))

    return FilterOptions(
        **window.fields(),
        operators=opts["operators"],
        lines=opts["lines"],
        from_places=places(opts["from_places"]),
        to_places=places(opts["to_places"]),
    )


@router.get(
    "/decisions",
    summary="Alert decisions taken in a day-and-time window, with their realized outcome",
    response_description="Decisions decided inside the window, newest departure first.",
)
def decisions(
    day: date | None = Query(default=None, description="Oslo day of the decisions (default: today)."),
    from_time: time | None = Query(
        default=None, description="Start of the range, Europe/Oslo (default 00:00; rounded down)."
    ),
    to_time: time | None = Query(
        default=None,
        description="End of the range, Europe/Oslo, EXCLUSIVE (default the following midnight; "
        "rounded up).",
    ),
    operator: str | None = Query(default=None, description="Filter to one codespace, e.g. 'VYG'."),
    line: str | None = Query(
        default=None,
        description="Filter to one line REF, e.g. 'VYG:Line:R11' — not the short code the page "
        "shows, since two codespaces can share one. Values come from /tilbakeblikk-api/filters.",
    ),
    from_place: str | None = Query(
        default=None,
        description="Filter to registrations boarding at this StopPlace ref. Values come from "
        "/tilbakeblikk-api/filters, which is also the allow-list this is checked against.",
    ),
    to_place: str | None = Query(
        default=None, description="Filter to registrations alighting at this StopPlace ref."
    ),
    verdict: str | None = Query(
        default=None,
        description="Filter to 'hit', 'false_alarm', 'miss' or 'correct_reject'. Excludes "
        "unresolved decisions, which have no verdict to match — nearly all of a same-day window.",
    ),
    alerted_only: bool = Query(
        default=False,
        description="Only decisions that crossed the threshold INSIDE the window. Leave off to see "
        "the decisions we did NOT alert on — the ones a miss is made of.",
    ),
    resolved_only: bool = Query(
        default=False,
        description="Only decisions whose realized outcome is known (outcome_state 'avklart').",
    ),
    sx_only: bool = Query(
        default=False,
        description="Only decisions about a call an operator published a SIRI-SX message for — "
        "i.e. drop the rows this codebase minted a situation number for (`KIX:`).",
    ),
    limit: int = Query(default=200, ge=1, le=_MAX_LIMIT, description="Maximum rows to return."),
) -> DecisionsResponse:
    """Every alert decision taken inside the window, whether or not it produced a push.

    Non-alerting decisions are included by default on purpose: they are the only evidence a miss
    leaves behind, and a log filtered to what we did send can only ever measure precision. On this
    page they matter for a second reason — a sub-threshold cycle records a ``message_input`` too, but
    it is never the replayed one, so a subject that never crossed the gate inside the window has no
    replayable cycle at all.
    """
    from google.cloud import bigquery

    window = _resolve_window(day, from_time, to_time)
    operator = _checked_operator(operator)
    from_refs, to_refs = _checked_places(window, line, from_place, to_place)
    if verdict is not None and verdict not in historikk._VERDICTS:
        raise HTTPException(
            status_code=400, detail=f"verdict must be one of {', '.join(historikk._VERDICTS)}."
        )
    params = [
        bigquery.ScalarQueryParameter("day", "DATE", window.day),
        bigquery.ScalarQueryParameter("from_ts", "TIMESTAMP", window.from_ts),
        bigquery.ScalarQueryParameter("to_ts", "TIMESTAMP", window.to_ts),
        bigquery.ScalarQueryParameter("operator", "STRING", operator),
        bigquery.ScalarQueryParameter("line", "STRING", line),
        bigquery.ArrayQueryParameter("from_place_refs", "STRING", from_refs),
        bigquery.ArrayQueryParameter("to_place_refs", "STRING", to_refs),
        bigquery.ScalarQueryParameter("verdict", "STRING", verdict),
        bigquery.ScalarQueryParameter("alerted_only", "BOOL", alerted_only),
        bigquery.ScalarQueryParameter("resolved_only", "BOOL", resolved_only),
        bigquery.ScalarQueryParameter("sx_only", "BOOL", sx_only),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    # Every query parameter is in the key -- the day, BOTH quantized times, and every filter. The key
    # is a positional tuple, so a parameter added to the list above and forgotten here fails
    # silently: for two minutes, whichever request landed first decides what both callers see.
    key = (
        "decisions", *window.cache_key, operator, line, from_place, to_place,
        verdict, alerted_only, resolved_only, sx_only, limit,
    )
    rows = _query(_DECISIONS_QUERY, params, key)
    return DecisionsResponse(
        **window.fields(),
        count=len(rows),
        decisions=[Decision(**dict(r)) for r in rows],
    )


@router.get(
    "/summary",
    summary="Alerting performance over the decisions taken in a window",
    response_description="Verdict counts, precision/recall, and how much of the window is resolved.",
)
def summary(
    day: date | None = Query(default=None, description="Oslo day of the decisions (default: today)."),
    from_time: time | None = Query(
        default=None, description="Start of the range, Europe/Oslo (default 00:00; rounded down)."
    ),
    to_time: time | None = Query(
        default=None,
        description="End of the range, Europe/Oslo, EXCLUSIVE (default the following midnight; "
        "rounded up).",
    ),
    operator: str | None = Query(default=None, description="Filter to one codespace, e.g. 'VYG'."),
    line: str | None = Query(default=None, description="Filter to one line ref, e.g. 'VYG:Line:R11'."),
    from_place: str | None = Query(
        default=None, description="Filter to registrations boarding at this StopPlace ref."
    ),
    to_place: str | None = Query(
        default=None, description="Filter to registrations alighting at this StopPlace ref."
    ),
) -> Summary:
    """Counts and rates over the window, with the unresolved share reported alongside.

    `n_subjects`, `n_labelled`, `n_unknown` and `n_pending` are part of the contract, not
    diagnostics. On this page they carry more weight than on /historikk: the default window is
    today, verdicts arrive from the 04:00 UTC job the following night, so nearly everything is
    `venter` and a precision computed over a handful of rows is a different claim from one computed
    over almost all of them. A caller cannot tell which it has without them.

    The filters it accepts are exactly the ones that narrow the POPULATION — which cycles, which
    trains, on whose registered journeys — so these rates describe the same slice the list beside
    them shows. `verdict`, `alerted_only`, `resolved_only` and `sx_only` narrow by OUTCOME instead
    and are deliberately not accepted here at all: filtering to hits would make precision read 100 %.
    """
    from google.cloud import bigquery

    window = _resolve_window(day, from_time, to_time)
    operator = _checked_operator(operator)
    from_refs, to_refs = _checked_places(window, line, from_place, to_place)
    params = [
        bigquery.ScalarQueryParameter("day", "DATE", window.day),
        bigquery.ScalarQueryParameter("from_ts", "TIMESTAMP", window.from_ts),
        bigquery.ScalarQueryParameter("to_ts", "TIMESTAMP", window.to_ts),
        bigquery.ScalarQueryParameter("operator", "STRING", operator),
        bigquery.ScalarQueryParameter("line", "STRING", line),
        bigquery.ArrayQueryParameter("from_place_refs", "STRING", from_refs),
        bigquery.ArrayQueryParameter("to_place_refs", "STRING", to_refs),
    ]
    rows = _query(
        _SUMMARY_QUERY,
        params,
        ("summary", *window.cache_key, operator, line, from_place, to_place),
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
        **window.fields(),
        n_subjects=row.get("n_subjects") or 0,
        n_labelled=row.get("n_labelled") or 0,
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
