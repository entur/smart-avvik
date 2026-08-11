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

Reads only. No import of main (the package rule): main injects the BigQuery client via
:func:`configure`, the same pattern reisevalg/varsel/llm_message/decisions already use.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from deployment import environment_label
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_DECISIONS_QUERY = (Path(__file__).parent / "historikk_decisions.sql").read_text()
_SUMMARY_QUERY = (Path(__file__).parent / "historikk_summary.sql").read_text()

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

    Describes a *train*, not a traveller. The journey definition behind the decision
    (`journey_digest`, `from_stop`, `to_stop`) is deliberately absent: this endpoint is served
    unauthenticated on a public host, and those three next to `departure_time` and `n_devices`
    would publish "somebody commutes A->B at 07:15" — the very thing `llm_message.key_digest` and
    `VARSEL_LLM_LOG_PROMPTS=false` keep out of the access-controlled logs. They stay in
    `disruptions.varsel_decisions`, which is not public.

    `extra="forbid"` is the guard that keeps that true: the SQL and this model must agree column for
    column, so a future SELECT that re-adds a redacted column fails loudly here instead of silently
    publishing it.
    """

    model_config = ConfigDict(extra="forbid")

    situation_number: str | None = Field(description="SIRI-SX situationNumber.")
    summary: str | None = Field(description="Short Norwegian summary of the situation.")
    description: str | None = Field(description="The operator's fuller explanation.")
    operator: str | None = Field(description="Ref codespace: VYG, GOA, RUT, ...")
    line: str | None = Field(description="Line ref of the matched train.")
    mode: str | None = Field(description="Vehicle noun: 'Tog' or 'Buss'.")
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
    pushed_any: bool | None = Field(description="Whether a Web Push was actually delivered.")
    n_devices: int | None = Field(description="How many registrations shared this journey.")
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


class Segment(BaseModel):
    """One half of the window, split on whether the app relayed a fact or predicted one.

    Same four verdicts, same arithmetic, over a subset of the same rows — so a caller can put the
    two side by side and see what the blended figure is actually made of.
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
    n_unsegmented: int = Field(
        description="Labelled decisions taken before the flag existed (2026-08-05), in neither "
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

    job_config = bigquery.QueryJobConfig(query_parameters=params)
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
    "/decisions",
    summary="Alert decisions with their realized outcome",
    response_description="Decisions in the window, newest departure first.",
)
def decisions(
    from_date: date | None = Query(default=None, description="Window start (default: 14 days ago)."),
    to_date: date | None = Query(default=None, description="Window end, inclusive (default: today)."),
    operator: str | None = Query(default=None, description="Filter to one codespace, e.g. 'VYG'."),
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
    limit: int = Query(default=200, ge=1, le=_MAX_LIMIT, description="Maximum rows to return."),
) -> DecisionsResponse:
    """Every alert decision in the window, whether or not it produced a push.

    Non-alerting decisions are included by default on purpose: they are the only evidence a miss
    leaves behind, and a log filtered to what we did send can only ever measure precision.
    """
    from google.cloud import bigquery

    start, end = _window(from_date, to_date)
    operator = _checked_operator(operator)
    if verdict is not None and verdict not in _VERDICTS:
        raise HTTPException(
            status_code=400, detail=f"verdict must be one of {', '.join(_VERDICTS)}."
        )
    params = [
        bigquery.ScalarQueryParameter("from_date", "DATE", start),
        bigquery.ScalarQueryParameter("to_date", "DATE", end),
        bigquery.ScalarQueryParameter("operator", "STRING", operator),
        bigquery.ScalarQueryParameter("verdict", "STRING", verdict),
        bigquery.ScalarQueryParameter("alerted_only", "BOOL", alerted_only),
        bigquery.ScalarQueryParameter("resolved_only", "BOOL", resolved_only),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    key = ("decisions", start, end, operator, verdict, alerted_only, resolved_only, limit)
    rows = _query(_DECISIONS_QUERY, params, key)
    return DecisionsResponse(
        from_date=start,
        to_date=end,
        count=len(rows),
        decisions=[Decision(**dict(r)) for r in rows],
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
) -> Summary:
    """Counts and rates over the window, with the unresolved share reported alongside.

    `n_unknown` and `n_pending` are part of the contract, not diagnostics: a precision computed
    over a fifth of the decisions is a different claim from one computed over almost all of them,
    and a caller cannot tell which it has without them.
    """
    from google.cloud import bigquery

    start, end = _window(from_date, to_date)
    operator = _checked_operator(operator)
    params = [
        bigquery.ScalarQueryParameter("from_date", "DATE", start),
        bigquery.ScalarQueryParameter("to_date", "DATE", end),
        bigquery.ScalarQueryParameter("operator", "STRING", operator),
    ]
    rows = _query(_SUMMARY_QUERY, params, ("summary", start, end, operator))
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
        n_unsegmented=row.get("n_unsegmented") or 0,
    )
