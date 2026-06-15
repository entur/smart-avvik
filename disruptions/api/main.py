import logging
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from google.cloud import bigquery
from google.cloud.bigquery import QueryJob

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_LAST_INGESTION_TIME_QUERY = (Path(__file__).parent / "last_ingestion_time.sql").read_text()
_RAIL_CALLS_QUERY = (Path(__file__).parent / "rail_calls_today.sql").read_text()
_DISRUPTIONS_QUERY = (Path(__file__).parent / "disruptions_today.sql").read_text()
_UPDATE_CATEGORIES_QUERY = (Path(__file__).parent / "update_categories.sql").read_text()
_PREDICT_QUERY = (Path(__file__).parent / "predict.sql").read_text()

def _on_ingestion_change(last_ingestion_time: datetime | None) -> None:
    """Default change handler — logs the new ingestion time."""
    logger.info("Last ingestion time changed: %s", last_ingestion_time)
    bq_client.query(_RAIL_CALLS_QUERY).result()
    bq_client.query(_DISRUPTIONS_QUERY).result()
    bq_client.query(_UPDATE_CATEGORIES_QUERY).result()
    bq_client.query(_PREDICT_QUERY).result()



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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/last_ingestion_time")
async def last_ingestion_time() -> dict[str, str]:
    return {"last_ingestion_time": str(get_last_ingestion_time()) }