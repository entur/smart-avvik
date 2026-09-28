"""/rushhistorikk-api — did the rush forecasts land, and did we tell anybody?

The feedback loop for Rushvarsel, and /historikk's sibling: that page scores what Avviksvarsel
decided about a registered journey, this one scores what the rush-hour forecast said about a day.
Three records join to answer it — :mod:`togvarsel` for what we forecast, :mod:`togvarsel_notify`'s
round log for what we communicated, and :mod:`togvarsel_outcomes` for what the rush actually did.

Two things this module is deliberate about:

* **The verdict is not computed here.** It is a fourteen-branch table over three named populations,
  and it lives in :mod:`rushhistorikk_verdicts` as pure functions. The SQL returns facts, Python
  classifies and aggregates. That is one definition of "traff", unit-testable without BigQuery.
* **Silence has three different meanings and the page keeps them apart.** ``venter`` is an outcome
  still being retried, ``ukjent`` one that can no longer be established (or a day whose call volume
  disqualifies it), and ``ikke vurdert`` an issue for which no notification round exists and none
  could be reconstructed. Folding any of them into a verdict would manufacture skill out of missing
  data, in one direction or the other.
* **A round nobody recorded is reconstructed from the forecast log, and labelled** (2026-09-02).
  The page exists to judge the MODEL, and the push policy is a pure function of the forecast level,
  the baseline's decision and the clock — so for an issue with no ``togvarsel_rounds`` row,
  :func:`reconstruct_rounds` replays :func:`togvarsel_notify.decide_issue` at the forecast's own
  ``computed_at`` and grades the result like any recorded round. A recorded row always wins; the
  replay fills gaps only. That is not a retreat from "recorded, never re-derived": what the replay
  cannot see (a gate closed before evaluating, how many devices matched) is exactly what the
  model-quality reading does not need, and what it changes is stated on every row
  (``reconstructed``), counted beside the rates (``n_reconstructed``), and kept out of
  ``varsler_sendt``, which says "sent" and means it. Before this, the page read «ikke vurdert» for
  every issue before the round log existed and would have for weeks — in tst, where the gate is
  permanently closed, forever.

**Nothing here is redacted, and that is a claim about the FORECAST fields only.** This page carries
no traveller data: a rush forecast is one product-wide prognosis over a fixed 39-station geography,
with no journey, route, device or departure behind it — the one way this surface is simpler than its
two siblings. But ``togvarsel_rounds`` also holds five subscriber-derived counts
(``matched_journeys``, ``matched_devices``, ``n_sent``, ``n_error``, ``n_gone``), which are
day-grained readings of a subscriber population currently numbering a handful. Those are recorded
for audit and **never served**: ``rushhistorikk_days.sql`` does not name them, which is
``meldinger_decisions.sql``'s rule that a query which cannot name a column cannot leak it however
the model above it drifts. ``test_rushhistorikk`` pins both halves.

Note what gates this page, because the answer is "nothing, today": ``sso.PROTECTED_PREFIXES``
carries both of its prefixes, but ``SSO_REQUIRED`` is ``"false"`` in every environment and no
environment wires the OAuth client, so ``sso.required()`` returns False and the middleware
short-circuits. With ``trafficType: public`` set once in the chart and inherited, the page and this
API are readable by anyone on all three hosts. Same posture as /historikk and /meldinger; the
registration is what makes turning the gate on a one-value change rather than a code change.

Reads only. No import of main (the package rule): main injects the BigQuery client via
:func:`configure`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import rushhistorikk_verdicts as verdicts
import togvarsel_notify
from deployment import environment_label
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# The page counts in Oslo days, because a rush is an Oslo-clock thing. rushhistorikk_verdicts is
# deliberately timezone-agnostic — it takes dates and a `today` — so the zone lives here.
_OSLO = ZoneInfo("Europe/Oslo")

# Read at import, not per request: a missing .sql becomes an import error that stops the app at
# startup rather than a 500 on the first page load.
_DAYS_QUERY = (Path(__file__).parent / "rushhistorikk_days.sql").read_text()

# The three tables behind this change a few times a weekday (four issue rows, four round rows, three
# outcome rows), so a short cache keeps a page refresh from costing a BigQuery query. Bounded
# because the endpoint is public and unauthenticated — though far more tightly than /historikk's,
# since the key space here is the four-value window allow-list rather than caller-chosen dates.
_CACHE_TTL_S = 120
_CACHE_MAX_ENTRIES = 16
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, Any]] = {}

# The picker's four values, and the API's allow-list. An allow-list rather than a range check for
# the reason `historikk._checked_operator` gives: the value lands in the response-cache key on a
# public endpoint, so accepting arbitrary integers lets a caller mint one cache entry and one
# BigQuery query per distinct value. Four values means four reachable keys.
#
# "All time" is a bounded 365 days. The tables are kilobytes, so the span costs nothing; the label
# outlives its meaning once the product has more than a year of weekdays.
WINDOW_DAYS: tuple[int, ...] = (7, 14, 30, 365)
_DEFAULT_DAYS = 14

# A blast-radius bound, not a pruning check — a too-narrow date bound reads as a CHEAPER query, so
# cost can never detect it. The day query measured 305 bytes; this is orders of magnitude above any
# reachable read and exists to catch a mistyped join, as meldinger's does.
_MAX_BYTES_BILLED = 2 * 1024**3


def _tznow() -> datetime:
    """Now, in the Oslo day the page counts in. Wrapped so tests can pin it."""
    return datetime.now(UTC)


_bq_client: Any | None = None


def configure(*, bq_client: Any) -> None:
    """Wire in the BigQuery client. Called once from main.lifespan."""
    global _bq_client
    _bq_client = bq_client


class ConfigResponse(BaseModel):
    """What the page needs to describe itself, independent of the data."""

    environment: str | None = Field(
        description='Non-prod environment name ("dev", "tst") for the page to show in its title; '
        "null in prd.",
    )


class IssueRow(BaseModel):
    """One forecast issue: what it said, what we communicated, and how it turned out."""

    model_config = ConfigDict(extra="forbid")

    issue: str = Field(description="Issue time, HHMM: 0630 | 0700 | 1200 | 1400.")
    rush: str = Field(description="Which rush this issue is about: am | pm.")
    forecast_level: str | None = Field(description="green | yellow | red, as forecast.")
    p: float | None = Field(description="The calibrated probability behind that level.")
    late: bool = Field(
        description="Forecast computed more than 10 min after its cutoff, so it saw post-cutoff "
        "reality. Excluded from Presisjon, kept in Dekning."
    )
    recorded: bool = Field(
        description="A notification round exists for this issue — recorded, or reconstructed from "
        "the forecast log (see `reconstructed`). False means 'ikke vurdert': none was recorded and "
        "none could be reconstructed."
    )
    reconstructed: bool = Field(
        description="The round is not a record: no togvarsel_rounds row existed, so the push policy "
        "(togvarsel_notify.decide_issue) was replayed on the forecast at its own computed_at — what "
        "would have been decided with delivery on and on time. Graded like a recorded round, because "
        "the page judges the model; counted apart, and never a sent push."
    )
    sent: bool = Field(
        description="A push was ISSUED — the policy decision, not delivery. A round can match zero "
        "devices and still be what the traveller would have received."
    )
    kind: str | None = Field(description="warning | update | all_clear when sent.")
    reason: str | None = Field(
        description="untimely | no_change | green_baseline when the round decided not to push; "
        "yellow_baseline on a row from the red-only window (2026-09-02 evening to 2026-09-03, "
        "PR #130, reverted)."
    )
    baseline_level: str | None = Field(
        description="For a follow-up, the level its baseline left in force — the level a confirming "
        "follow-up is graded on, since that is what the traveller holds."
    )
    decided_at: datetime | None = Field(description="When the round was evaluated.")
    actual_level: str | None = Field(
        description="The realized level over THIS issue's own trained window (0630 -> 06:30-09:00, "
        "0700 -> 07:00-09:00, 1200/1400 -> 15:00-18:00)."
    )
    actual_trouble10: float | None = Field(
        description="The realized share behind that level: calls cancelled OR >= 10 min late, over "
        "resolved calls."
    )
    provisional: bool = Field(
        description="The realized value is the same-day reading and can still change overnight. "
        "Excluded from the stats table."
    )
    verdict: str = Field(
        description="traff | traff_nesten | falsk_alarm | bom | venter | ukjent | ikke_vurdert. "
        "traff is the right class communicated, silence included: a green forecast on a green "
        "rush is a hit."
    )
    lead_min: float | None = Field(
        description="Minutes from the push to the start of the rush it is about — positive when the "
        "push preceded it. Near zero or negative for the morning issues by construction."
    )
    scored: bool = Field(
        description="Whether this issue is in the population Presisjon and the confusion matrix are "
        "computed over."
    )


class DayRow(BaseModel):
    """One weekday: its four issues, and what the two rushes actually did."""

    model_config = ConfigDict(extra="forbid")

    d: date = Field(description="The Oslo date.")
    issues: list[IssueRow] = Field(description="The day's issues, 0630 first. Usually four.")
    am_level: str | None = Field(description="Realized morning level over 06:30-09:00.")
    am_trouble10: float | None = Field(description="The share behind it.")
    amrest_level: str | None = Field(
        description="Realized level over 07:00-09:00 — the 0700 issue's own window, shown so the "
        "two morning figures cannot silently disagree."
    )
    pm_level: str | None = Field(description="Realized afternoon level over 15:00-18:00.")
    pm_trouble10: float | None = Field(description="The share behind it.")
    am_provisional_level: str | None = Field(description="What the page showed at 09:20, if it differs.")
    pm_provisional_level: str | None = Field(description="What the page showed at 18:20, if it differs.")
    eligible: bool | None = Field(
        description="The training chain's day-level test (am and pm both above 300 calls). False "
        "means the realized share means nothing and the day is not scored."
    )


class MatrixRow(BaseModel):
    """One row of the 3x3: a forecast level against the three realized levels."""

    model_config = ConfigDict(extra="forbid")

    forecast: str = Field(description="green | yellow | red, as forecast.")
    green: int = Field(description="Count where the rush was green.")
    yellow: int = Field(description="Count where the rush was yellow.")
    red: int = Field(description="Count where the rush was red.")


class StatsRow(BaseModel):
    """One row of «Slik har varslene truffet»: the total, or one issue slot."""

    model_config = ConfigDict(extra="forbid")

    slot: str = Field(description='"total", or an issue time: 0630 | 0700 | 1200 | 1400.')
    presisjon: float | None = Field(
        description="Of the pushes that went out on scored issues, the share that were justified: "
        "a yellow or red push on a rush that turned yellow or red (either level), or a friskmelding "
        "on a rush that turned green. Null on an empty denominator, never 0 — a calm window has no "
        "pushes to rate. The denominator is presisjon_den."
    )
    presisjon_den: int = Field(
        description="Pushes behind that rate: sent issues in the scored population, reconstructed "
        "rounds included."
    )
    dekning: float | None = Field(
        description="Of the situations where a push was owed, the share that got a justified one. "
        "A situation is an issue whose rush turned yellow or red with no warning standing (a "
        "warning was owed) or turned green with one standing (a friskmelding was owed); a "
        "confirming follow-up is none. Per ISSUE on a slot row and per RUSH on the total row, where "
        "a rush caught at the follow-up after a silent baseline counts as covered — the lateness "
        "is Varslingstid's to show."
    )
    dekning_den: int = Field(
        description="Situations behind that rate — issues on a slot row, rushes on the total row, "
        "over the delivered population with late and untimely included — and the count most "
        "likely to be a handful."
    )
    varslingstid_min: float | None = Field(
        description="Median minutes from the push to the rush's start. The last push per rush on "
        "the total row."
    )
    varslingstid_n: int = Field(description="Pushes behind that median.")
    varsler_sendt: int = Field(
        description="Pushes issued in this row's window, friskmeldinger included: one per RECORDED "
        "round that sent, over every issue whatever became of its day. The notification-volume "
        "figure. Reconstructed rounds are counted apart, in varsler_rekonstruert."
    )
    varsler_rekonstruert: int = Field(
        description="Pushes a reconstructed round would have issued. Apart from varsler_sendt "
        "because that column says sent and these never were."
    )
    varsler_venter: int = Field(
        description="Of varsler_sendt, the pushes on days whose outcome is not settled yet: in "
        "that count, not yet in the matrix. Why a same-day view's count exceeds its matrix."
    )
    n_scored: int = Field(description="Issues in the Presisjon population.")
    n_venter: int = Field(description="Outcome not settled yet.")
    n_ukjent: int = Field(description="Outcome unestablishable, or the day was ineligible.")
    n_ikke_vurdert: int = Field(
        description="No notification round recorded and none reconstructable (no computed_at on the "
        "forecast, or the policy refused the row)."
    )
    n_reconstructed: int = Field(
        description="Issues whose round was reconstructed from the forecast log rather than "
        "recorded. Orthogonal to the buckets: a reconstructed round is scored like a recorded one."
    )
    n_reconstructed_after_start: int = Field(
        description="Of those, the ones on a day the round log already covered — so a lost "
        "best-effort write or a closed gate is tellable apart from the pre-launch backlog."
    )
    n_late: int = Field(description="Forecast issued after its own cutoff.")
    n_untimely: int = Field(description="Round evaluated past its delivery deadline.")
    matrix: list[MatrixRow] = Field(
        description="Forecast level (rows) against realized level (columns), over scored issues."
    )


class CalibrationRow(BaseModel):
    """One forecast level: how often a rush forecast at that level turned bad."""

    model_config = ConfigDict(extra="forbid")

    level: str = Field(description="The forecast level: green, yellow or red.")
    n: int = Field(description="Scored issues forecast at this level, all four slots.")
    n_bad: int = Field(description="Of those, the ones whose rush turned yellow or red — the "
                       "models' label, whatever the realized level's shade.")
    mean_p: float | None = Field(description="The mean probability those forecasts quoted. "
                                 "Beside n_bad/n it says whether the level means what it promises.")


class Coverage(BaseModel):
    """The window from a commuter's side: per RUSH, was a bad one warned, and at what price.

    A rush is a day's morning or afternoon whose BASELINE issue (06:30, 12:00) is scored, judged
    on that issue's window. Warned means the model said yellow or red — at the baseline, or else at
    the follow-up — which is the push policy's own trigger, so reconstructed and recorded rounds
    read alike."""

    model_config = ConfigDict(extra="forbid")

    rushes: int
    days: int = Field(description="Distinct days those rushes fall on, for a per-weekday price.")
    bad: int = Field(description="Rushes that turned yellow or red.")
    bad_first: int = Field(description="Bad rushes warned at the baseline.")
    bad_late: int = Field(description="Bad rushes warned only at the follow-up.")
    bad_missed: int = Field(description="Bad rushes neither issue warned about.")
    calm_warned: int = Field(description="Green rushes that got a warning at either issue.")
    calm_quiet: int = Field(description="Green rushes neither issue warned about.")


class DaysResponse(BaseModel):
    """The page: one row per day, plus the stats table over the same population."""

    model_config = ConfigDict(extra="forbid")

    days_window: int = Field(description="The window the caller asked for, in days.")
    from_date: date = Field(description="First Oslo date included.")
    days: list[DayRow] = Field(description="Newest first.")
    stats: list[StatsRow] = Field(description="The total row first, then one per issue slot.")
    calibration: list[CalibrationRow] = Field(
        description="Green, yellow, red: how often each forecast level turned into a bad rush."
    )
    coverage: Coverage
    recording_started: date | None = Field(
        description="The earliest day a notification round was ever recorded. A reconstructed "
        "round BEFORE this is the expected pre-launch backlog; one after it can only mean a lost "
        "write or a closed gate."
    )


def _checked_days(days: int) -> int:
    """Reject a window outside the picker's four values.

    Not cosmetic validation: the value becomes part of the response-cache key, so accepting
    arbitrary integers on a public endpoint lets a caller mint one cache entry and one BigQuery
    query apiece. Same reasoning as historikk._checked_operator.
    """
    if days not in WINDOW_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"days must be one of {', '.join(str(d) for d in WINDOW_DAYS)}.",
        )
    return days


def _query(sql: str, params: list, cache_key: tuple):
    """Run a parameterised query behind the TTL cache. Rows are returned as a list."""
    if _bq_client is None:
        raise HTTPException(status_code=503, detail="Rush forecast log unavailable.")
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
        # Most likely cause on a fresh environment: no rush has resolved yet, or the app has not
        # started here since the tables were introduced. Say so rather than surfacing a traceback.
        logger.warning("Rush forecast query failed", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Could not read the rush forecast log; it may not have been written to yet.",
        ) from exc
    with _cache_lock:
        for key in [k for k, (ts, _) in _cache.items() if now - ts >= _CACHE_TTL_S]:
            _cache.pop(key, None)
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            for key in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX_ENTRIES // 5]:
                _cache.pop(key, None)
        _cache[cache_key] = (now, rows)
    return rows


def _known_slot(row: Any) -> bool:
    """Whether this row's issue key is one the taxonomy can score. See `days` for why it drops."""
    if str(row["issue"]) in verdicts.SCOPE_BY_SLOT:
        return True
    logger.warning(
        "rushhistorikk: dropping an issue key the taxonomy does not know: %r", row["issue"]
    )
    return False


def _issue_from_row(row: Any, today: date) -> verdicts.Issue:
    """One SQL row as the classifier's input.

    The scope triple is picked per issue, not per rush: 0630 is scored against am (06:30-09:00) and
    0700 against amrest (07:00-09:00), because that is the window each one's model was trained on.
    Giving 0700 the whole morning would hand it thirty minutes that had already happened when it was
    issued, and inflate its skill.
    """
    slot = str(row["issue"])
    scope = verdicts.SCOPE_BY_SLOT[slot]  # KeyError is caught by the caller — see _known_slot
    settled = bool(row[f"{scope}_settled"])
    return verdicts.Issue(
        d=row["d"],
        issue=slot,
        # The round's own level where there is one, the forecast otherwise: they agree by
        # construction, but only the round record exists for an issue we evaluated.
        level=row["round_level"] or row["forecast_level"],
        late=bool(row["late"]),
        recorded=bool(row["has_round"]),
        sent=bool(row["sent"]),
        kind=row["kind"],
        reason=row["reason"],
        baseline_level=row["baseline_level"],
        decided_at=row["decided_at"],
        computed_at=row["computed_at"],
        eligible=row["eligible"],
        outcome=verdicts.resolve_outcome_state(
            settled=settled,
            unresolvable=bool(row[f"{scope}_unresolvable"]),
            d=row["d"],
            today=today,
        ),
        actual=row[f"{scope}_level"] if settled else None,
        after_recording_started=(
            row["first_recorded_day"] is not None and row["d"] > row["first_recorded_day"]
        ),
    )


def _issue_row(row: Any, issue: verdicts.Issue) -> IssueRow:
    """One served issue: the row's own facts plus the three things the classifier derives.

    `scope` is resolved once, the way `_issue_from_row` does it — inline this was three separate
    dict lookups inside a comprehension inside a loop.

    Note what `provisional` CANNOT be: it is not `issue.outcome != OUTCOME_SETTLED`. The settled
    MERGE can write `settled = TRUE` and `unresolvable = TRUE` on the same row (the "no resolved
    call in this window" case), and `resolve_outcome_state` checks unresolvable FIRST — so the two
    diverge exactly there, and deriving this from the outcome would mark a settled-but-unresolvable
    day as still provisional.
    """
    scope = verdicts.SCOPE_BY_SLOT[issue.issue]
    return IssueRow(
        issue=issue.issue,
        rush=verdicts.RUSH_BY_SLOT[issue.issue],
        forecast_level=row["forecast_level"],
        p=row["p"],
        late=issue.late,
        recorded=issue.recorded,
        reconstructed=issue.reconstructed,
        sent=issue.sent,
        kind=issue.kind,
        reason=issue.reason,
        baseline_level=issue.baseline_level,
        decided_at=issue.decided_at,
        actual_level=issue.actual,
        actual_trouble10=row[f"{scope}_trouble10"],
        provisional=not bool(row[f"{scope}_settled"]) and row[f"{scope}_level"] is not None,
        verdict=verdicts.classify(issue),
        lead_min=verdicts.lead_minutes(issue.issue, issue.decided_at) if issue.sent else None,
        scored=verdicts.population(issue) == verdicts.BUCKET_SCORED,
    )


def reconstruct_rounds(day_issues: list[verdicts.Issue]) -> list[verdicts.Issue]:
    """One day's issues with every missing round replaced by what the policy would have decided.

    The page judges the MODEL, and the push policy is a pure function of the forecast level, the
    baseline's marker and the clock — so for an issue with no round record the policy is replayed on
    the forecast at its own `computed_at`, the instant the real round would have been evaluated at,
    with the day's other rounds as its marker state. Baseline before follow-up, in issue order, so
    a reconstructed 0700 reads the reconstructed 0630's marker exactly as the real pass would have.

    Three rules. A RECORDED round is never touched: its marker seeds the replay and its row stands,
    including the facts a replay could not know (a suppression caused by the poller's own clock, how
    many devices matched). The replay calls `togvarsel_notify.decide_issue` and restates none of
    its rules — one policy, so the page and the pusher cannot disagree about what would have gone
    out. And an issue the replay cannot serve — no `computed_at`, or a level the policy refuses —
    stays as it came, i.e. `ikke vurdert`, logged rather than silently scored.
    """
    order = {slot: index for index, slot in enumerate(verdicts.SLOTS)}
    markers: dict[str, Any] = {
        issue.issue: {"level": issue.level, "pushed": issue.sent}
        for issue in day_issues
        if issue.recorded and issue.level in verdicts.LEVELS
    }
    out: list[verdicts.Issue] = []
    for issue in sorted(day_issues, key=lambda i: order.get(i.issue, len(order))):
        if issue.recorded or issue.computed_at is None or issue.level not in verdicts.LEVELS:
            out.append(issue)
            continue
        computed_at = issue.computed_at
        if computed_at.tzinfo is None:  # BigQuery hands back aware UTC; a naive value is a fixture
            computed_at = computed_at.replace(tzinfo=UTC)
        try:
            decision = togvarsel_notify.decide_issue(
                {"issue": issue.issue, "level": issue.level}, markers, computed_at
            )
        except Exception:
            logger.warning(
                "rushhistorikk: could not reconstruct the %s round for %s; left as ikke vurdert",
                issue.issue,
                issue.d,
                exc_info=True,
            )
            out.append(issue)
            continue
        markers[issue.issue] = decision.marker
        out.append(
            replace(
                issue,
                recorded=True,
                reconstructed=True,
                sent=decision.push,
                kind=decision.kind,
                reason=decision.reason,
                baseline_level=decision.baseline_level,
                decided_at=computed_at,
            )
        )
    return out


def _with_reconstructed_rounds(issues: list[verdicts.Issue]) -> list[verdicts.Issue]:
    """`reconstruct_rounds` applied per day, returned in the caller's order so the (row, issue)
    pairing the response is built from survives. (d, issue) is unique: togvarsel_issued is keyed on
    it and the round join is one-to-one on the same key."""
    by_day: dict[date, list[verdicts.Issue]] = {}
    for issue in issues:
        by_day.setdefault(issue.d, []).append(issue)
    filled = {
        (issue.d, issue.issue): issue
        for group in by_day.values()
        for issue in reconstruct_rounds(group)
    }
    return [filled[(issue.d, issue.issue)] for issue in issues]


def _matrix_rows(matrix: dict[str, dict[str, int]]) -> list[MatrixRow]:
    return [
        MatrixRow(
            forecast=level,
            green=matrix[level]["green"],
            yellow=matrix[level]["yellow"],
            red=matrix[level]["red"],
        )
        for level in verdicts.LEVELS
    ]


def _stats_rows(stats: verdicts.Stats) -> list[StatsRow]:
    return [
        StatsRow(
            slot=row.slot,
            presisjon=row.presisjon,
            presisjon_den=row.presisjon_den,
            dekning=row.dekning,
            dekning_den=row.dekning_den,
            varslingstid_min=row.varslingstid_min,
            varslingstid_n=row.varslingstid_n,
            varsler_sendt=row.varsler_sendt,
            varsler_rekonstruert=row.varsler_rekonstruert,
            varsler_venter=row.varsler_venter,
            n_scored=row.counts.n_scored,
            n_venter=row.counts.n_venter,
            n_ukjent=row.counts.n_ukjent + row.counts.n_ineligible,
            n_ikke_vurdert=row.counts.n_ikke_vurdert,
            n_reconstructed=row.counts.n_reconstructed,
            n_reconstructed_after_start=row.counts.n_reconstructed_after_start,
            n_late=row.counts.n_late,
            n_untimely=row.counts.n_untimely,
            matrix=_matrix_rows(row.matrix),
        )
        for row in stats.rows
    ]


def _bad(issue: verdicts.Issue) -> bool:
    return issue.actual in ("yellow", "red")


def _warns(issue: verdicts.Issue | None) -> bool:
    return issue is not None and issue.level in ("yellow", "red")


def _calibration(pairs: list[tuple[Any, verdicts.Issue]]) -> list[CalibrationRow]:
    """Over the scored issues, the Presisjon/matrix population, so the three figures and the
    table's cannot describe different forecasts."""
    scored = [(row, i) for row, i in pairs if verdicts.population(i) == verdicts.BUCKET_SCORED]
    out = []
    for level in verdicts.LEVELS:
        at = [(row, i) for row, i in scored if i.level == level]
        ps = [row["p"] for row, _ in at if row["p"] is not None]
        out.append(CalibrationRow(level=level, n=len(at), n_bad=sum(_bad(i) for _, i in at),
                                  mean_p=sum(ps) / len(ps) if ps else None))
    return out


def _coverage(issues: list[verdicts.Issue]) -> Coverage:
    by_key = {(i.d, i.issue): i for i in issues}
    c = dict.fromkeys(("rushes", "bad", "bad_first", "bad_late", "bad_missed", "calm_warned",
                      "calm_quiet"), 0)
    days = set()
    for (d, slot), base in by_key.items():
        if slot not in ("0630", "1200") or verdicts.population(base) != verdicts.BUCKET_SCORED:
            continue
        first = _warns(base)
        late = not first and _warns(by_key.get((d, "0700" if slot == "0630" else "1400")))
        days.add(d)
        c["rushes"] += 1
        if _bad(base):
            c["bad"] += 1
            c["bad_first" if first else "bad_late" if late else "bad_missed"] += 1
        else:
            c["calm_warned" if first or late else "calm_quiet"] += 1
    return Coverage(days=len(days), **c)


router = APIRouter(prefix="/rushhistorikk-api", tags=["rushhistorikk"])


@router.get("/config", summary="Which deployment is serving this page")
def config() -> ConfigResponse:
    """The environment name the page appends to its title, or null in prd.

    Its own endpoint rather than a field on ``/days`` deliberately: that endpoint 503s when the log
    cannot be read (no client, tables not written to yet), which is exactly when knowing whether you
    are looking at dev or prd matters most. This one touches no BigQuery.
    """
    return ConfigResponse(environment=environment_label())


@router.get("/days", summary="Rush forecasts by day, with the stats table over the same window")
def days(
    days: int = Query(
        default=_DEFAULT_DAYS,
        description="Window in days. One of 7, 14, 30 or 365 (365 is the page's 'all time').",
    ),
) -> DaysResponse:
    """One row per day plus «Slik har varslene truffet», from one query.

    Both come from the same rows on purpose: the population the rates describe is exactly the list
    beside them, so the two cannot drift. /historikk splits its decisions and summary because its
    decision log is large and its list is capped; neither holds here — four issue rows per weekday
    means a year is about a thousand rows.
    """
    from google.cloud import bigquery

    window = _checked_days(days)
    today = _tznow().astimezone(_OSLO).date()
    from_date = today - timedelta(days=window - 1)
    rows = _query(
        _DAYS_QUERY,
        [bigquery.ScalarQueryParameter("from_date", "DATE", from_date)],
        ("days", window, today),
    )

    # An issue key the taxonomy does not know is DROPPED, not fatal. `verdicts.classify` is
    # deliberately total so no state can 500 the page, and indexing SCOPE_BY_SLOT here would have
    # undone that for the one input most likely to change: a fifth issue added to
    # togvarsel.ISSUE_SPECS reaches togvarsel_issued long before it reaches this page. Logged
    # rather than silent, because a dropped row is a real gap in what the page reports.
    rows = [row for row in rows if _known_slot(row)]
    issues = _with_reconstructed_rounds([_issue_from_row(row, today) for row in rows])
    stats = verdicts.summarize(issues)

    by_day: dict[date, list[tuple[Any, verdicts.Issue]]] = {}
    day_facts: dict[date, Any] = {}
    for row, issue in zip(rows, issues, strict=True):
        by_day.setdefault(row["d"], []).append((row, issue))
        day_facts[row["d"]] = row

    day_rows: list[DayRow] = []
    for d in sorted(by_day, reverse=True):
        facts = day_facts[d]
        day_rows.append(
            DayRow(
                d=d,
                issues=[
                    _issue_row(row, issue)
                    for row, issue in sorted(by_day[d], key=lambda pair: pair[1].issue)
                ],
                am_level=facts["am_level"],
                am_trouble10=facts["am_trouble10"],
                amrest_level=facts["amrest_level"],
                pm_level=facts["pm_level"],
                pm_trouble10=facts["pm_trouble10"],
                am_provisional_level=facts["am_provisional_level"],
                pm_provisional_level=facts["pm_provisional_level"],
                eligible=facts["eligible"],
            )
        )

    return DaysResponse(
        days_window=window,
        from_date=from_date,
        days=day_rows,
        stats=_stats_rows(stats),
        calibration=_calibration(list(zip(rows, issues, strict=True))),
        coverage=_coverage(issues),
        recording_started=rows[0]["first_recorded_day"] if rows else None,
    )
