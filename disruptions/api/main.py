import logging
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from google.cloud import bigquery
from google.cloud.bigquery import QueryJob
from pydantic import BaseModel

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

    Holds only the static parts: the onset and the model's minutes-FROM-ONSET estimates.
    "Minutes from now" is derived per request (onset + estimate - now), never cached.
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
app = FastAPI(lifespan=lifespan)


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
    """
    last_seen = get_last_ingestion_time()
    on_change(last_seen)
    while True:
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)
        current = get_last_ingestion_time()
        if current != last_seen:
            last_seen = current
            on_change(current)


class Estimate(BaseModel):
    """A duration estimate expressed as minutes from now.

    Values are signed: a negative value means the estimate's time has already elapsed
    while the disruption is still open (running longer than predicted). `overdue` is True
    when the point estimate has passed.
    """

    point_min_from_now: int
    p50_min_from_now: int
    p80_min_from_now: int
    p90_min_from_now: int
    overdue: bool


class Disruption(BaseModel):
    situation_number: str
    summary: str | None
    description: str | None
    category: str
    onset: datetime
    minutes_since_onset: int
    reopened: bool  # trains running again; impact may still be resolving (see `impact`)
    reopened_at: datetime | None  # when the reopening message was observed (None while ongoing)
    minutes_since_reopen: int | None  # minutes since reopening (None while ongoing)
    num_affected_stops: int
    declared_window_min: int | None
    reopen: Estimate  # "trains running again in ~X min from now" (past/overdue once reopened)
    impact: Estimate  # "timetable fully back to normal in ~Y min from now"


class DisruptionsResponse(BaseModel):
    as_of: datetime  # request time the "minutes from now" values were computed against
    refreshed_at: datetime | None  # when the cache was last reloaded from predictions_today
    count: int
    active_count: int  # not yet reopened
    reopened_count: int  # trains running again, impact possibly still resolving
    disruptions: list[Disruption]


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


@app.get("/disruptions")
async def disruptions() -> DisruptionsResponse:
    """List today's disruptions (still-ongoing and recently reopened) with reopen/impact
    estimates as minutes from now. Reopened ones are kept and flagged because impact
    typically resolves a while after the line reopens."""
    now = datetime.now(timezone.utc)
    with _disruptions_lock:
        cached = _disruptions_cache
        refreshed_at = _disruptions_refreshed_at

    items = [_to_disruption(d, now) for d in cached]
    reopened_count = sum(1 for x in items if x.reopened)
    return DisruptionsResponse(
        as_of=now,
        refreshed_at=refreshed_at,
        count=len(items),
        active_count=len(items) - reopened_count,
        reopened_count=reopened_count,
        disruptions=items,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/last_ingestion_time")
async def last_ingestion_time() -> dict[str, str]:
    return {"last_ingestion_time": str(get_last_ingestion_time()) }