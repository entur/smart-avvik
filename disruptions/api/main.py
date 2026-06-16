import logging
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from google.cloud import bigquery
from google.cloud.bigquery import QueryJob
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_LAST_INGESTION_TIME_QUERY = (Path(__file__).parent / "last_ingestion_time.sql").read_text()
_RAIL_CALLS_QUERY = (Path(__file__).parent / "rail_calls_today.sql").read_text()
_DISRUPTIONS_QUERY = (Path(__file__).parent / "disruptions_today.sql").read_text()
_UPDATE_CATEGORIES_QUERY = (Path(__file__).parent / "update_categories.sql").read_text()
_PREDICT_QUERY = (Path(__file__).parent / "predict.sql").read_text()
_ACTIVE_DISRUPTIONS_QUERY = (Path(__file__).parent / "active_disruptions.sql").read_text()


@dataclass(frozen=True)
class _CachedDisruption:
    """One active disruption as cached from predictions_today.

    Holds the onset and the models' minutes-FROM-ONSET estimates. "Minutes from now"
    is derived per request (onset + estimate - now), never cached. The impact estimate
    is re-anchored to elapsed time on each rebuild (it comes from the during-disruption
    model), so it stays fresh between refreshes rather than drifting overdue.
    """

    situation_number: str
    summary: str | None
    description: str | None
    category: str
    onset_ts: datetime
    reopen_ts: datetime | None  # set once trains run again; impact may still linger after this
    num_affected_stops: int
    declared_window_min: int | None
    # reopen model: "trains running again in ~X min" (minutes from onset)
    pred_reopen_min: float
    reopen_p50_min: float
    reopen_p80_min: float
    reopen_p90_min: float
    # impact model: "timetable back to normal in ~Y min" (minutes from onset)
    pred_impact_min: float
    impact_p50_min: float
    impact_p80_min: float
    impact_p90_min: float


# In-process cache of active disruptions, refreshed on every ingestion change. Guarded by a
# lock because the poller thread writes it while request handlers read it.
_disruptions_lock = threading.Lock()
_disruptions_cache: list[_CachedDisruption] = []
_disruptions_refreshed_at: datetime | None = None


def refresh_disruptions_cache() -> None:
    """Reload the active-disruptions cache from predictions_today (reopen_ts IS NULL)."""
    rows = bq_client.query(_ACTIVE_DISRUPTIONS_QUERY).result()
    disruptions = [
        _CachedDisruption(
            situation_number=r.situationNumber,
            summary=r.summary,
            description=r.description,
            category=r.category,
            onset_ts=r.onset_ts,
            reopen_ts=r.reopen_ts,
            num_affected_stops=r.num_affected_stops,
            declared_window_min=r.declared_window_min,
            pred_reopen_min=r.pred_reopen_min,
            reopen_p50_min=r.reopen_p50_min,
            reopen_p80_min=r.reopen_p80_min,
            reopen_p90_min=r.reopen_p90_min,
            pred_impact_min=r.pred_impact_min,
            impact_p50_min=r.impact_p50_min,
            impact_p80_min=r.impact_p80_min,
            impact_p90_min=r.impact_p90_min,
        )
        for r in rows
    ]
    with _disruptions_lock:
        global _disruptions_cache, _disruptions_refreshed_at
        _disruptions_cache = disruptions
        _disruptions_refreshed_at = datetime.now(timezone.utc)
    logger.info("Refreshed disruptions cache: %d active disruptions", len(disruptions))


def _on_ingestion_change(last_ingestion_time: datetime | None) -> None:
    """Default change handler — logs the new ingestion time."""
    logger.info("Last ingestion time changed: %s", last_ingestion_time)
    bq_client.query(_RAIL_CALLS_QUERY).result()
    bq_client.query(_DISRUPTIONS_QUERY).result()
    bq_client.query(_UPDATE_CATEGORIES_QUERY).result()
    bq_client.query(_PREDICT_QUERY).result()
    refresh_disruptions_cache()



@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = threading.Event()
    thread = threading.Thread(
        target=poll_for_changes,
        args=(_on_ingestion_change, stop_event),
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=5)



bq_client = bigquery.Client()

_API_DESCRIPTION = """
Predicts how long ongoing public-transport disruptions will last, from SIRI-SX
situation messages, using a BigQuery ML model.

For each active disruption the API serves two estimates:

* **reopen** — when trains are expected to be running again on the affected line.
* **impact** — when the timetable is expected to be *fully back to normal*. This
  usually lags the reopening, so a disruption is kept in the response (flagged
  `reopened`) for a while after trains start moving again. The impact estimate is
  re-computed as the disruption runs (it factors in how long it has already lasted),
  so it tracks long-running disruptions instead of going stale at the onset guess.

**Reading the estimates.** Every estimate is expressed as **whole minutes from
`as_of`** (the response's server time), not as an absolute timestamp. So
`point_min_from_now: 30` means "expected in about 30 minutes". Values go
**negative** once the predicted time passes while the disruption is still open —
the model under-predicted — and `overdue` is then `true`. Alongside the point
estimate, `p50`/`p80`/`p90` give increasingly conservative (later) bounds.

**Freshness.** Results are served from an in-process cache that is rebuilt
whenever new SIRI-SX data is ingested (roughly once a minute). `refreshed_at`
reports when the cache was last rebuilt; the "minutes from now" values are
recomputed on every request against `as_of`.
"""

app = FastAPI(
    lifespan=lifespan,
    title="Disruptions API",
    version="0.1.0",
    summary="Live duration predictions for rail disruptions, from SIRI-SX messages.",
    description=_API_DESCRIPTION,
    contact={"name": "Entur — Team KI-eksperimentering"},
)

# Public, read-only JSON API — allow any origin so it can be consumed from
# browsers (only GET endpoints are exposed, so this carries no write risk).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def get_last_ingestion_time() -> datetime | None:
    """Return the most recent ingestionTime for today's SIRI-SX partitions."""
    row = next(iter(bq_client.query(_LAST_INGESTION_TIME_QUERY).result()))
    return row.last_ingestion_time


def poll_for_changes(
    on_change: Callable[[datetime | None], None],
    stop_event: threading.Event | None = None,
    interval: float = 60,
) -> None:
    """Poll get_last_ingestion_time() once per minute, calling on_change when it changes.

    Pass a stop_event to interrupt the loop cleanly between polls (used for shutdown).

    Each poll (and its on_change rebuild) is wrapped: a failure — e.g. a transient
    BigQuery error, or SIRI read access not yet granted to the runtime identity — is
    logged and retried on the next tick rather than killing this daemon thread for good.
    That lets the service self-recover once access lands, without a restart.
    """
    last_seen = None
    seen_any = False
    while True:
        try:
            current = get_last_ingestion_time()
            if not seen_any or current != last_seen:
                last_seen = current
                seen_any = True
                on_change(current)
        except Exception:
            logger.exception("Ingestion poll failed; retrying on next tick")
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)


class Estimate(BaseModel):
    """A duration estimate expressed as minutes from now (`as_of`).

    Values are signed: a negative value means the estimate's time has already elapsed
    while the disruption is still open (running longer than predicted). `overdue` is True
    when the point estimate has passed.
    """

    point_min_from_now: int = Field(
        description=(
            "Point (mean) estimate, in whole minutes from `as_of`. Negative once the "
            "predicted time has passed while the disruption is still open."
        ),
    )
    p50_min_from_now: int = Field(
        description="50th-percentile (median) estimate, in whole minutes from `as_of`.",
    )
    p80_min_from_now: int = Field(
        description=(
            "80th-percentile estimate, in whole minutes from `as_of` — a more "
            "conservative (later) bound than p50."
        ),
    )
    p90_min_from_now: int = Field(
        description=(
            "90th-percentile estimate, in whole minutes from `as_of` — the most "
            "conservative (latest) bound exposed."
        ),
    )
    overdue: bool = Field(
        description=(
            "True when the point estimate's time has already passed "
            "(`point_min_from_now` < 0): the disruption is running longer than predicted."
        ),
    )


class Disruption(BaseModel):
    """A single disruption, with reopen and impact estimates resolved to minutes from `as_of`."""

    situation_number: str = Field(
        description="SIRI-SX situationNumber — stable identifier for this disruption.",
    )
    summary: str | None = Field(
        description="Short Norwegian summary from the SX message (null if none provided).",
    )
    description: str | None = Field(
        description="Longer Norwegian description from the SX message (null if none provided).",
    )
    category: str = Field(description="Disruption category, as classified from the SX message.")
    onset: datetime = Field(description="When the disruption began (first observed onset), UTC.")
    minutes_since_onset: int = Field(
        description="Whole minutes elapsed from `onset` to `as_of` (never negative).",
    )
    reopened: bool = Field(
        description=(
            "True once trains are running again on the affected line. Impact may still be "
            "resolving — see `impact`."
        ),
    )
    reopened_at: datetime | None = Field(
        description="When the reopening was observed (UTC); null while still ongoing.",
    )
    minutes_since_reopen: int | None = Field(
        description="Whole minutes since `reopened_at`; null while still ongoing.",
    )
    num_affected_stops: int = Field(description="Number of stops affected by the disruption.")
    declared_window_min: int | None = Field(
        description=(
            "Operator-declared validity window in minutes, if the SX message stated one "
            "(else null)."
        ),
    )
    reopen: Estimate = Field(
        description=(
            "Estimate of when trains will be running again. Once `reopened` is true these "
            "values are in the past (overdue)."
        ),
    )
    impact: Estimate = Field(
        description=(
            "Estimate of when the timetable will be fully back to normal — typically lags "
            "the reopening."
        ),
    )


class DisruptionsResponse(BaseModel):
    """Today's disruptions plus the timing context needed to interpret the estimates."""

    as_of: datetime = Field(
        description="Server time the \"minutes from now\" values were computed against (UTC).",
    )
    refreshed_at: datetime | None = Field(
        description=(
            "When the cache was last rebuilt from new SIRI-SX data (UTC); null until the "
            "first refresh."
        ),
    )
    count: int = Field(description="Total number of disruptions returned (active + reopened).")
    active_count: int = Field(description="Number not yet reopened.")
    reopened_count: int = Field(
        description="Number that have reopened but whose impact may still be resolving.",
    )
    disruptions: list[Disruption] = Field(
        description="The disruptions, each with reopen and impact estimates as minutes from `as_of`.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "as_of": "2026-06-15T09:30:00Z",
                "refreshed_at": "2026-06-15T09:29:12Z",
                "count": 1,
                "active_count": 1,
                "reopened_count": 0,
                "disruptions": [
                    {
                        "situation_number": "ENT:SituationNumber:1234",
                        "summary": "Signalfeil ved Oslo S",
                        "description": "Innstilte og forsinkede tog grunnet signalfeil.",
                        "category": "signalFailure",
                        "onset": "2026-06-15T08:45:00Z",
                        "minutes_since_onset": 45,
                        "reopened": False,
                        "reopened_at": None,
                        "minutes_since_reopen": None,
                        "num_affected_stops": 6,
                        "declared_window_min": 120,
                        "reopen": {
                            "point_min_from_now": 30,
                            "p50_min_from_now": 25,
                            "p80_min_from_now": 55,
                            "p90_min_from_now": 80,
                            "overdue": False,
                        },
                        "impact": {
                            "point_min_from_now": 70,
                            "p50_min_from_now": 60,
                            "p80_min_from_now": 100,
                            "p90_min_from_now": 130,
                            "overdue": False,
                        },
                    }
                ],
            }
        }
    }


def _minutes_from_now(onset_ts: datetime, est_min_from_onset: float, now: datetime) -> int:
    """Convert a minutes-from-onset estimate to whole minutes from now.

    May be negative — a negative value means the estimate's time has already passed while
    the disruption is still open (see Estimate.overdue).
    """
    elapsed_min = (now - onset_ts).total_seconds() / 60
    return round(est_min_from_onset - elapsed_min)


def _estimate(
    onset_ts: datetime, point: float, p50: float, p80: float, p90: float, now: datetime
) -> Estimate:
    """Build an Estimate of minutes-from-now; overdue when the point estimate has passed."""
    point_from_now = _minutes_from_now(onset_ts, point, now)
    return Estimate(
        point_min_from_now=point_from_now,
        p50_min_from_now=_minutes_from_now(onset_ts, p50, now),
        p80_min_from_now=_minutes_from_now(onset_ts, p80, now),
        p90_min_from_now=_minutes_from_now(onset_ts, p90, now),
        overdue=point_from_now < 0,
    )


def _to_disruption(d: _CachedDisruption, now: datetime) -> Disruption:
    """Render a cached disruption with estimates resolved to minutes from `now`."""
    reopened_at = d.reopen_ts
    return Disruption(
        situation_number=d.situation_number,
        summary=d.summary,
        description=d.description,
        category=d.category,
        onset=d.onset_ts,
        minutes_since_onset=max(0, round((now - d.onset_ts).total_seconds() / 60)),
        reopened=reopened_at is not None,
        reopened_at=reopened_at,
        minutes_since_reopen=(
            max(0, round((now - reopened_at).total_seconds() / 60)) if reopened_at else None
        ),
        num_affected_stops=d.num_affected_stops,
        declared_window_min=d.declared_window_min,
        reopen=_estimate(
            d.onset_ts, d.pred_reopen_min, d.reopen_p50_min, d.reopen_p80_min, d.reopen_p90_min, now
        ),
        impact=_estimate(
            d.onset_ts, d.pred_impact_min, d.impact_p50_min, d.impact_p80_min, d.impact_p90_min, now
        ),
    )


@app.get(
    "/disruptions",
    summary="List today's active and recently-reopened disruptions",
    response_description="Disruptions with reopen/impact estimates as minutes from `as_of`",
    tags=["disruptions"],
)
async def disruptions() -> DisruptionsResponse:
    """List today's disruptions (still-ongoing and recently reopened), each with reopen and
    impact estimates as **minutes from now**.

    Reopened disruptions are kept in the response and flagged (`reopened: true`) because impact
    typically takes a while to resolve after the line reopens. Use `active_count` /
    `reopened_count` to split the two, and `as_of` as the reference point for every estimate.
    """
    now = datetime.now(timezone.utc)
    with _disruptions_lock:
        cached = _disruptions_cache
        refreshed_at = _disruptions_refreshed_at

    items = [
        disruption
        for d in cached
        for disruption in [_to_disruption(d, now)]
        if disruption.impact.p90_min_from_now > 0
    ]
    reopened_count = sum(1 for x in items if x.reopened)
    return DisruptionsResponse(
        as_of=now,
        refreshed_at=refreshed_at,
        count=len(items),
        active_count=len(items) - reopened_count,
        reopened_count=reopened_count,
        disruptions=items,
    )


@app.get(
    "/health",
    summary="Liveness check",
    response_description='Always `{"status": "ok"}` when the service is up.',
    tags=["diagnostics"],
)
async def health() -> dict[str, str]:
    """Lightweight liveness probe — returns 200 with `{"status": "ok"}` and does not touch
    BigQuery."""
    return {"status": "ok"}


@app.get(
    "/last_ingestion_time",
    summary="Most recent SIRI-SX ingestion time",
    response_description='`{"last_ingestion_time": "<timestamp>"}` (the string `"None"` if no data yet).',
    tags=["diagnostics"],
)
async def last_ingestion_time() -> dict[str, str]:
    """Return the latest SIRI-SX ingestion time the service has observed for today.

    This is the upstream-freshness signal that drives the cache refresh, exposed for debugging.
    """
    return {"last_ingestion_time": str(get_last_ingestion_time())}