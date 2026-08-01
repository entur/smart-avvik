"""The alert-decision log: persist what Avviksvarsel concluded, so it can be checked later.

Until this module existed, the only record of what a traveller was told was Cloud Logging, and
nothing anywhere recorded what actually *happened* to the train we warned about. That makes the
one question worth asking about an alerting system unanswerable: when we alerted, were we right?

What is recorded is **every decision**, not every push. A row is written for each situation the
matcher matched to a registered journey — including the ones that stayed below the alert
threshold. That asymmetry is the whole point: a journey we did not alert on leaves no trace
otherwise, so `miss` and `correct_reject` would be unobservable and any "precision" figure would
be measuring only the half of the confusion matrix that flatters us.

Rows are written once per refresh cycle per (journey definition x situation x departure), so the
same decision reappears as its prediction evolves. Those repeats are not duplicates to be
suppressed — they are the lead-time series, the axis alerting quality varies along most
(eval/impact_eval.py: alertable ~30-60 min pre-departure, and ask-time features decay).

Design notes
------------
* **No import of main** (the rule the rest of this package follows): main injects a BigQuery client
  via :func:`configure`. varsel imports this module lazily, the way it already imports
  :mod:`llm_message`, so a deploy missing it degrades to "no log" rather than "no pushes".
* **Fail-open, always.** Every entry point swallows its exceptions. A push that reached a
  traveller must never be lost to a logging problem, and the poller thread must never die in one.
* **Streaming inserts, not DML.** `insert_rows_json` has no daily table-modification quota (DML
  does, at 1500/table/day, which a 60 s poller can approach), and the streaming buffer's
  restriction — no DML UPDATE for ~90 min — costs nothing here because this table is append-only
  by design and the labelling job writes a *separate* outcomes table.
* **Privacy.** `journey_digest` (shared with :func:`llm_message.key_digest` so the two records
  join) plus a device count. No device_id, no journey_id, no time window, no weekday pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # type-checker only: no runtime import, so the varsel <-> decisions cycle stays broken
    from varsel import Alert

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

TABLE = "disruptions.varsel_decisions"

_CREATE_TABLE_SQL = (Path(__file__).parent / "create_varsel_decisions.sql").read_text()

# Injected by main.lifespan; None disables recording entirely (tests, and any deploy where the
# client could not be built).
_bq_client: Any | None = None
_table_ready = False

# How long to wait before retrying the CREATE after a failure. Without it a persistent problem
# (a missing permission, a malformed statement) re-issues the DDL on every 60 s poller tick for the
# life of the process, burning a BigQuery job each time to fail the same way. Same shape as
# llm_message._LATCH_TTL: back off, but expire so a fixed grant takes effect without a redeploy.
_ENSURE_RETRY_S = 15 * 60
_ensure_failed_at: float | None = None

# The DDL runs on the startup path of a single always-on pod. Every other bq call in this app is
# unbounded, but this one is new and sits in front of the poller thread, so it gets an explicit
# ceiling rather than inheriting "wait forever".
_ENSURE_TIMEOUT_S = 60.0


def configure(*, bq_client: Any) -> None:
    """Wire in the BigQuery client. Called once from main.lifespan."""
    global _bq_client
    _bq_client = bq_client


def enabled() -> bool:
    return _bq_client is not None


def ensure_table() -> bool:
    """Create the decision log if it is not there yet. Safe to call repeatedly.

    Done from the app rather than a provisioning script because prd is an isolated project fed by
    free BigQuery copies and nobody runs build scripts against it — the table would simply not
    exist there. `CREATE TABLE IF NOT EXISTS` never touches an existing table's contents.
    """
    global _table_ready, _ensure_failed_at
    if _bq_client is None:
        return False
    if _table_ready:
        return True
    if _ensure_failed_at is not None and monotonic() - _ensure_failed_at < _ENSURE_RETRY_S:
        return False
    try:
        _bq_client.query(_CREATE_TABLE_SQL).result(timeout=_ENSURE_TIMEOUT_S)
        _table_ready = True
        _ensure_failed_at = None
        logger.info("Decision log ready (%s)", TABLE)
    except Exception:
        # Not fatal: recording is an observability feature, and the alerting path it observes must
        # keep working without it. Backed off rather than retried every tick.
        _ensure_failed_at = monotonic()
        logger.warning("Could not ensure %s; decisions will not be recorded", TABLE, exc_info=True)
    return _table_ready


def _oslo_date(when: datetime | None):
    """Operating day of a call, in the timezone the call tables key on."""
    return when.astimezone(_OSLO).date() if when is not None else None


def build_row(
    journey: dict,
    alert: Alert,
    *,
    journey_digest: str,
    n_devices: int,
    pushed: bool,
    message: str | None,
    message_source: str,
) -> dict:
    """One decision as a BigQuery row.

    `alert` is read through duck typing so this module imports neither varsel nor main at runtime;
    the annotation is resolved only by a type checker (see the TYPE_CHECKING import above), which
    is what keeps a renamed `Alert` field from silently becoming a column of NULLs.

    `message` is the text that was actually sent — the written one when an LLM produced it, and
    otherwise the same deterministic body the push carried. `message_source` says which, because
    the two are indistinguishable once stored, and prd runs with the LLM off.
    """
    departure = getattr(alert, "departure_time", None)
    decided_at = getattr(alert, "decided_at", None) or datetime.now(tz=_OSLO)
    minutes_to_departure = None
    if departure is not None and decided_at is not None:
        minutes_to_departure = int((departure - decided_at).total_seconds() // 60)
    impact = getattr(alert, "impact", None)
    return {
        "decided_at": decided_at.isoformat(),
        "journey_digest": journey_digest,
        "from_stop": journey.get("from_stop"),
        "to_stop": journey.get("to_stop"),
        "situation_number": getattr(alert, "situation_number", None),
        "summary": getattr(alert, "summary", None),
        "description": getattr(alert, "description", None),
        "operator": getattr(alert, "operator", None),
        "line": getattr(alert, "line", None),
        "mode": getattr(alert, "mode", None),
        "jkey": getattr(alert, "jkey", None),
        "call_date": _oslo_date(departure).isoformat() if departure is not None else None,
        "stop_place": getattr(alert, "stop_place", None),
        "stop_order": getattr(alert, "stop_order", None),
        "departure_time": departure.isoformat() if departure is not None else None,
        "minutes_to_departure": minutes_to_departure,
        "impact_probability": getattr(alert, "impact_probability", None),
        "cancel_probability": getattr(alert, "cancel_probability", None),
        "ge10_probability": getattr(alert, "ge10_probability", None),
        "delay_p50_min": getattr(alert, "delay_p50_min", None),
        "delay_p90_min": getattr(alert, "delay_p90_min", None),
        "alert_threshold": getattr(alert, "alert_threshold", None),
        "alertworthy": bool(getattr(alert, "alertworthy", False)),
        "pushed": bool(pushed),
        "n_devices": n_devices,
        "message": message,
        "message_source": message_source,
        "snapshot_refreshed_at": (
            impact.refreshed_at.isoformat()
            if impact is not None and getattr(impact, "refreshed_at", None) is not None
            else None
        ),
    }


def record(rows: list[dict]) -> int:
    """Append decision rows. Returns how many were accepted; never raises.

    A failure here is logged and dropped. The alternative — letting it propagate — would put a
    BigQuery outage on the path between a live disruption and a traveller's phone.
    """
    if not rows or not ensure_table():
        return 0
    try:
        errors = _bq_client.insert_rows_json(TABLE, rows)
    except Exception:
        logger.warning("Could not record %d alert decisions", len(rows), exc_info=True)
        return 0
    if errors:
        # insert_rows_json reports per-row failures instead of raising, so a schema drift shows up
        # here as a silently short write unless it is logged explicitly.
        logger.warning(
            "%d of %d alert decisions rejected by BigQuery: %s",
            len(errors),
            len(rows),
            errors[:3],
            extra={"fields": {"event": "varsel_decision_write", "result": "partial"}},
        )
        return len(rows) - len(errors)
    logger.info(
        "Recorded %d alert decisions",
        len(rows),
        extra={"fields": {"event": "varsel_decision_write", "result": "ok", "n_rows": len(rows)}},
    )
    return len(rows)
