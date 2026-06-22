import logging
import re
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
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
_DISRUPTIONS_AS_OF_QUERY = (Path(__file__).parent / "disruptions_as_of.sql").read_text()
_ALERT_PROFILES_QUERY = (Path(__file__).parent / "alert_profiles.sql").read_text()
# Per-call (per-train) serving: the planned-timetable call source and the scoring query.
_RAIL_CALLS_PLANNED_QUERY = (Path(__file__).parent / "rail_calls_planned_today.sql").read_text()
_PREDICT_CALLS_QUERY = (Path(__file__).parent / "predict_calls.sql").read_text()
_CALL_PREDICTIONS_QUERY = "SELECT * FROM `ent-kixavvik-dev.disruptions.call_predictions_today`"

# Hour-of-day in the impact lookup is Europe/Oslo local time (matches the table build),
# so resolve "now" to the Oslo hour when looking up a profile.
_OSLO = ZoneInfo("Europe/Oslo")


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


def _row_to_cached(r) -> _CachedDisruption:
    """Map one BigQuery row (active_disruptions.sql / disruptions_as_of.sql share the same
    column set) to a _CachedDisruption."""
    return _CachedDisruption(
        situation_number=r.situationNumber,
        summary=r.summary,
        description=r.description,
        category=r.category,
        onset_ts=r.onset_ts,
        reopen_ts=r.reopen_ts,
        # NULL for journey-only situations (no affected stops -> ARRAY_LENGTH(NULL)); the
        # response model requires an int, so treat "no stops" as 0. (Kept out of the SQL so the
        # model still sees the raw feature value it was trained on.)
        num_affected_stops=r.num_affected_stops or 0,
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


def refresh_disruptions_cache() -> None:
    """Reload the active-disruptions cache from predictions_today (reopen_ts IS NULL)."""
    rows = bq_client.query(_ACTIVE_DISRUPTIONS_QUERY).result()
    disruptions = [_row_to_cached(r) for r in rows]
    with _disruptions_lock:
        global _disruptions_cache, _disruptions_refreshed_at
        _disruptions_cache = disruptions
        _disruptions_refreshed_at = datetime.now(timezone.utc)
    logger.info("Refreshed disruptions cache: %d active disruptions", len(disruptions))


def fetch_disruptions_as_of(as_of: datetime) -> list[_CachedDisruption]:
    """Reconstruct the disruptions that were active AS OF `as_of` by running the point-in-time
    query against BigQuery (disruptions_as_of.sql), bypassing the live cache.

    The cache only ever holds today's disruptions scored as-of now, so an arbitrary historical
    instant has to be rebuilt from SX: onset/features, reopen state and the during-disruption
    features are all computed using only versions with event_ts <= `as_of`. Returns the same
    _CachedDisruption shape as the cache, so the endpoint renders it identically (with `as_of`
    standing in for `now`)."""
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("as_of", "TIMESTAMP", as_of)],
    )
    rows = bq_client.query(_DISRUPTIONS_AS_OF_QUERY, job_config=job_config).result()
    return [_row_to_cached(r) for r in rows]


# /situation renders one disruption by its situationNumber. Rather than the live cache (which
# only holds today's set and is filtered for the feed) we reconstruct it from SX as of now, via
# the same point-in-time query as GET /disruptions?at=now. The whole "as of now" snapshot is
# cached for 5 minutes and indexed by situationNumber, so repeated detail lookups share a single
# BigQuery run instead of one query per id. Guarded by its own lock.
_SITUATION_CACHE_TTL = timedelta(minutes=5)
_situation_lock = threading.Lock()
_situation_snapshot: dict[str, _CachedDisruption] = {}
_situation_snapshot_at: datetime | None = None


def get_situation(situation_id: str) -> _CachedDisruption | None:
    """Return the disruption with this situationNumber, reconstructed as of now.

    Backed by a 5-minute cache of today's disruptions (rebuilt from SX with the same
    point-in-time query as GET /disruptions?at=now), so a burst of detail requests costs at
    most one BigQuery run per 5 minutes. Returns None if no NSB disruption with that
    situationNumber exists in the lookback window (yesterday and today)."""
    global _situation_snapshot, _situation_snapshot_at
    now = datetime.now(timezone.utc)
    with _situation_lock:
        if (
            _situation_snapshot_at is not None
            and now - _situation_snapshot_at < _SITUATION_CACHE_TTL
        ):
            return _situation_snapshot.get(situation_id)
    # Stale or never loaded: rebuild outside the lock (BigQuery call) and publish. A concurrent
    # request may rebuild in parallel — a harmless duplicate query, not worth serializing on.
    snapshot = {d.situation_number: d for d in fetch_disruptions_as_of(now)}
    with _situation_lock:
        _situation_snapshot = snapshot
        _situation_snapshot_at = now
    return snapshot.get(situation_id)


# In-process copy of disruptions.category_line_hour_impact, indexed by (category, line,
# hour) for the back-off lookup. Tiny table, rarely changes (rebuilt offline), so we load
# the whole thing. Guarded by its own lock — independent of the disruptions cache.
_impact_lookup: dict[tuple[str, str, int], dict] = {}
_impact_lock = threading.Lock()

# Minimum distinct situations behind a cell before we trust it; thinner cells are skipped
# and the lookup backs off to a coarser grain (the global '*/*/-1' row is always accepted).
_MIN_SITUATIONS = 30


def refresh_impact_lookup() -> None:
    """Reload the (category, line, hour) -> impact-profile lookup from BigQuery."""
    rows = bq_client.query(_ALERT_PROFILES_QUERY).result()
    table = {(r["category"], r["line"], r["hour"]): dict(r.items()) for r in rows}
    with _impact_lock:
        global _impact_lookup
        _impact_lookup = table
    logger.info("Refreshed impact lookup: %d cells", len(table))


def _candidate_keys(category: str, line: str, hour: int) -> list[tuple[str, str, int]]:
    """Lookup keys from most specific to coarsest, dropping hour before line.

    e.g. ('signal','R31',8) -> (signal,R31,8) (signal,R31,-1) (signal,*,8) (signal,*,-1) (*,*,-1).
    The trailing ('*','*',-1) global row is always the last resort. '*' / -1 inputs mean the
    caller has no line / no hour, so those back-off steps are skipped.
    """
    keys: list[tuple[str, str, int]] = []
    if category != "*":
        if line != "*" and hour != -1:
            keys += [(category, line, hour), (category, line, -1), (category, "*", hour)]
        elif line != "*":
            keys += [(category, line, -1)]
        elif hour != -1:
            keys += [(category, "*", hour)]
        keys.append((category, "*", -1))
    keys.append(("*", "*", -1))
    seen: set[tuple[str, str, int]] = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


def lookup_alert_profile(category: str, line: str = "*", hour: int = -1) -> dict | None:
    """Return the most specific impact-profile cell for (category, line, hour) with enough
    data, backing off to coarser grains and finally the global row. None if nothing loaded."""
    with _impact_lock:
        table = _impact_lookup
    if not table:
        return None
    for key in _candidate_keys(category, line, hour):
        row = table.get(key)
        if row is not None and (key == ("*", "*", -1) or row["n_situations"] >= _MIN_SITUATIONS):
            return row
    return None


# ---- Per-call (per-train) predictions cache --------------------------------------------------
# disruptions.call_predictions_today holds one row per (affected call) of today's situations:
# a calibrated cancellation probability and typical/p90 delay, at each stop. Cached here indexed
# by jkey (the dated journey) so /journey is a dict lookup. Guarded by its own lock.
@dataclass(frozen=True)
class _CallPred:
    """One scored call (stop on a journey) within a situation's 12h window."""

    situation_number: str
    summary: str | None
    jkey: str
    line: str | None
    stop_place: str
    stop_name: str | None
    stop_order: int
    aimed_dep: datetime | None
    aimed_arr: datetime | None
    p_cancel: float
    dep_delay_typ_min: int | None
    dep_delay_p90_min: int | None
    arr_delay_typ_min: int | None
    arr_delay_p90_min: int | None


_call_pred_lock = threading.Lock()
_call_pred_by_jkey: dict[str, list[_CallPred]] = {}
_call_pred_refreshed_at: datetime | None = None

# The planned call source is an ~8 GB ET partition scan, but the published schedule barely moves
# hour-to-hour -- so rebuild it at most once per TTL, not on every ingestion. The per-call
# predictions themselves (which DO change as situations evolve) are cheap and rebuilt every tick.
# At 60 min the scan costs ~190 GB/day (~$1.2); a freshly-reported situation still scores against
# a schedule at most an hour stale, well within the model's 12 h window.
_planned_lock = threading.Lock()
_planned_refreshed_at: datetime | None = None
_PLANNED_TTL = timedelta(minutes=60)


def maybe_refresh_planned_calls() -> None:
    """Rebuild disruptions.rail_calls_planned_today if it is stale (older than _PLANNED_TTL).

    Time-gated because it scans today's ET partition (~8 GB); the schedule is stable enough that
    a 10-minute cadence is plenty. Always runs on first call (refreshed_at is None)."""
    global _planned_refreshed_at
    now = datetime.now(timezone.utc)
    with _planned_lock:
        fresh = (
            _planned_refreshed_at is not None and now - _planned_refreshed_at < _PLANNED_TTL
        )
    if fresh:
        return
    bq_client.query(_RAIL_CALLS_PLANNED_QUERY).result()
    with _planned_lock:
        _planned_refreshed_at = now
    logger.info("Rebuilt planned call source (rail_calls_planned_today)")


def refresh_call_predictions_cache() -> None:
    """Reload the per-call predictions cache from call_predictions_today, indexed by jkey."""
    rows = bq_client.query(_CALL_PREDICTIONS_QUERY).result()
    by_jkey: dict[str, list[_CallPred]] = {}
    for r in rows:
        by_jkey.setdefault(r.jkey, []).append(
            _CallPred(
                situation_number=r.situationNumber,
                summary=r.summary,
                jkey=r.jkey,
                line=r.lineRef,
                stop_place=r.stop_place,
                stop_name=r.stop_name,
                stop_order=r.stop_order,
                aimed_dep=r.aimed_dep,
                aimed_arr=r.aimed_arr,
                p_cancel=r.p_cancel,
                dep_delay_typ_min=r.dep_delay_typ_min,
                dep_delay_p90_min=r.dep_delay_p90_min,
                arr_delay_typ_min=r.arr_delay_typ_min,
                arr_delay_p90_min=r.arr_delay_p90_min,
            )
        )
    with _call_pred_lock:
        global _call_pred_by_jkey, _call_pred_refreshed_at
        _call_pred_by_jkey = by_jkey
        _call_pred_refreshed_at = datetime.now(timezone.utc)
    logger.info("Refreshed per-call predictions cache: %d journeys", len(by_jkey))


def _on_ingestion_change(last_ingestion_time: datetime | None) -> None:
    """Default change handler — logs the new ingestion time."""
    logger.info("Last ingestion time changed: %s", last_ingestion_time)
    bq_client.query(_RAIL_CALLS_QUERY).result()
    bq_client.query(_DISRUPTIONS_QUERY).result()
    # AI.CLASSIFY (Gemini) categorisation of any *new* free text. Isolated so a failure here
    # never kills the rebuild: predict.sql LEFT JOINs disruptions.categories and falls back to
    # 'UNKNOWN' (with the global '__ALL__' quantile offsets) for unclassified text, so the feed
    # still serves — already-classified incidents keep their category; genuinely new text shows
    # UNKNOWN until classification succeeds. The runtime SA needs roles/aiplatform.user for this
    # step; until that grant lands the call 403s and we degrade gracefully rather than serving an
    # empty feed.
    try:
        bq_client.query(_UPDATE_CATEGORIES_QUERY).result()
    except Exception:
        logger.exception(
            "Category classification (AI.CLASSIFY) failed; serving with existing categories "
            "(new text -> UNKNOWN). Check the SA has roles/aiplatform.user."
        )
    bq_client.query(_PREDICT_QUERY).result()
    refresh_disruptions_cache()
    # The impact lookup is a static, offline-built table — reloading it here just keeps it
    # current if it was rebuilt. Isolated so a missing/erroring lookup never breaks the feed.
    try:
        refresh_impact_lookup()
    except Exception:
        logger.exception("Impact lookup refresh failed; serving disruptions without alert profiles")
    # Per-call (per-train) predictions for /journey. Isolated so any failure here never breaks the
    # main /disruptions feed. The planned call source is rebuilt on a TTL (it scans ET); the
    # scoring + cache reload run every tick (cheap: materialized inputs + ML.PREDICT).
    try:
        maybe_refresh_planned_calls()
        bq_client.query(_PREDICT_CALLS_QUERY).result()
        refresh_call_predictions_cache()
    except Exception:
        logger.exception("Per-call predictions refresh failed; /journey will serve stale or empty")



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eager initial load so /alert_profile works before the first ingestion change fires
    # the poller's rebuild. Tolerate failure — the poller retries on every tick.
    try:
        refresh_impact_lookup()
    except Exception:
        logger.exception("Initial impact lookup load failed; will retry on ingestion poll")
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


class AlertProfile(BaseModel):
    """Historical impact profile for a disruption category (optionally a line and hour-of-day),
    for deciding whether a disruption is worth pushing to passengers.

    Derived from 2 years of realized NSB outcomes: for boardings flagged by an SX situation of
    this category, how often was the trip cancelled or badly delayed, and how bad. `grain` and
    the `matched_*` fields say exactly which cell was used after back-off (a sparse specific cell
    falls back to a coarser one), so a category-only match (`matched_line` = `*`,
    `matched_hour` = -1) is distinguishable from a precise category×line×hour match.
    """

    grain: str = Field(
        description=(
            "Which dimensions the matched cell is specific on: 'C'/'L'/'H' = "
            "category/line/hour, '-' = aggregated. e.g. 'C-H' = category & hour, line pooled."
        ),
    )
    matched_category: str = Field(description="Category of the matched cell.")
    matched_line: str = Field(description="Line of the matched cell; '*' if pooled over lines.")
    matched_hour: int = Field(
        description="Hour-of-day (Europe/Oslo, 0-23) of the matched cell; -1 if pooled over hours.",
    )
    alert_tier: str = Field(
        description=(
            "'high' / 'medium' / 'low' / 'suppress'. 'suppress' means the flag is no worse than "
            "this line+hour's normal traffic, or too little data to trust."
        ),
    )
    alert_score: float = Field(
        description=(
            "Rankable priority: confidence-weighted expected lost minutes per flagged boarding "
            "(a cancellation counted as 60). Higher = more worth alerting."
        ),
    )
    cancel_rate: float = Field(description="P(boarding cancelled) given a flag of this category.")
    trouble_rate: float = Field(
        description="P(cancelled OR delayed >= 10 min) given a flag of this category.",
    )
    trouble_lift: float | None = Field(
        description=(
            "trouble_rate relative to the unconditional baseline at the same line+hour. "
            ">1 means the flag signals more trouble than normal; ~1 means no added signal."
        ),
    )
    exp_disruption_min: float = Field(
        description="Expected lost minutes per flagged boarding (cancellation = 60 min equiv).",
    )
    delay_p50: int | None = Field(description="Median departure delay (min) among non-cancelled.")
    delay_p90: int | None = Field(description="90th-pct departure delay (min) among non-cancelled.")
    n_situations: int = Field(description="Distinct historical situations behind this cell.")
    n_boardings: int = Field(description="Historical flagged boardings behind this cell.")


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
    alert: AlertProfile | None = Field(
        default=None,
        description=(
            "How alert-worthy this disruption is right now, from the historical impact of its "
            "category at the current Oslo hour (line-agnostic — a disruption spans lines; use "
            "the /alert_profile endpoint for a specific line). Null if no profile is loaded."
        ),
    )

    def should_alert(self) -> bool:
        return (self.alert is not None
                and self.alert.alert_tier != "suppress"
                and self.alert.delay_p90 > self.minutes_since_onset)


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
                        "category": "infrastruktur/signal",
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
                        "alert": {
                            "grain": "C-H",
                            "matched_category": "infrastruktur/signal",
                            "matched_line": "*",
                            "matched_hour": 8,
                            "alert_tier": "high",
                            "alert_score": 13.1,
                            "cancel_rate": 0.1527,
                            "trouble_rate": 0.329,
                            "trouble_lift": 3.96,
                            "exp_disruption_min": 13.4,
                            "delay_p50": 2,
                            "delay_p90": 13,
                            "n_situations": 1933,
                            "n_boardings": 40395,
                        },
                    }
                ],
            }
        }
    }


class CallPrediction(BaseModel):
    """Per-stop prediction for one train: chance the stop is cancelled, plus typical/p90 delay.

    Read the BOARDING stop's `cancel_probability` + `dep_delay_*` for "will my train leave, how
    late?"; read the ALIGHTING stop's `cancel_probability` + `arr_delay_*` for "will I reach my
    destination, how late?". Delays are whole minutes for a train that runs; `*_p90` is the
    conservative "bad case". Departure fields are null at a terminal, arrival fields at an origin.
    """

    stop_place: str = Field(description="NSR StopPlace ref of this stop.")
    stop_name: str | None = Field(description="Human-readable stop name (null if unresolved).")
    stop_order: int = Field(description="Position of this stop along the journey (1 = origin).")
    scheduled_departure: datetime | None = Field(description="Aimed departure (UTC); null at terminal.")
    scheduled_arrival: datetime | None = Field(description="Aimed arrival (UTC); null at origin.")
    cancel_probability: float = Field(
        description="Calibrated P(this stop is cancelled / the train does not serve it), 0-1.",
    )
    dep_delay_typ_min: int | None = Field(description="Typical departure delay (min) if it runs.")
    dep_delay_p90_min: int | None = Field(description="Bad-case (p90) departure delay (min).")
    arr_delay_typ_min: int | None = Field(description="Typical arrival delay (min) if it runs.")
    arr_delay_p90_min: int | None = Field(description="Bad-case (p90) arrival delay (min).")


class JourneyResponse(BaseModel):
    """Per-train predictions for a journey currently affected by a disruption.

    `calls` are the affected stops within 12h of the situation's onset, ordered along the line.
    When a journey is touched by more than one situation, the most severe (highest peak
    cancellation probability) is returned and named in `situation_number`.
    """

    journey_ref: str = Field(description="The journey looked up (echoes the request `id`).")
    line: str | None = Field(description="Line ref of the train, e.g. 'VYG:Line:R31'.")
    situation_number: str = Field(description="The SIRI-SX situation these predictions come from.")
    summary: str | None = Field(description="Short Norwegian summary of that situation.")
    as_of: datetime = Field(description="Server time of the response (UTC).")
    refreshed_at: datetime | None = Field(
        description="When the per-call cache was last rebuilt (UTC); null until first refresh.",
    )
    calls: list[CallPrediction] = Field(description="Affected stops, ordered by stop_order.")


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


def _oslo_hour(now: datetime) -> int:
    """Hour-of-day (0-23) in Europe/Oslo, matching the impact lookup's hour dimension."""
    return now.astimezone(_OSLO).hour


def _alert_profile_model(row: dict | None) -> AlertProfile | None:
    """Build the AlertProfile response from a lookup row (None passes through)."""
    if row is None:
        return None
    return AlertProfile(
        grain=row["grain"],
        matched_category=row["category"],
        matched_line=row["line"],
        matched_hour=row["hour"],
        alert_tier=row["alert_tier"],
        alert_score=row["alert_score"],
        cancel_rate=row["cancel_rate"],
        trouble_rate=row["trouble_rate"],
        trouble_lift=row["trouble_lift"],
        exp_disruption_min=row["exp_disruption_min"],
        delay_p50=row["delay_p50"],
        delay_p90=row["delay_p90"],
        n_situations=row["n_situations"],
        n_boardings=row["n_boardings"],
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
        alert=_alert_profile_model(lookup_alert_profile(d.category, "*", _oslo_hour(now))),
    )


@app.get(
    "/disruptions",
    summary="List today's active and recently-reopened disruptions",
    response_description="Disruptions with reopen/impact estimates as minutes from `as_of`",
    tags=["disruptions"],
)
async def disruptions(
    at: datetime | None = Query(
        default=None,
        description=(
            "Optional point in time (ISO 8601, e.g. `2026-06-15T09:00:00Z`) to reconstruct the "
            "feed AS OF. When omitted (the default), the live feed is served from the in-process "
            "cache as of the current time. When given, the disruptions active at that instant are "
            "rebuilt from BigQuery using only data known by then (no later reopening/updates leak "
            "in), and every estimate is expressed relative to `at` rather than now. A naive value "
            "is assumed UTC. Must not be in the future."
        ),
    ),
) -> DisruptionsResponse:
    """List today's disruptions (still-ongoing and recently reopened), each with reopen and
    impact estimates as **minutes from now**.

    Reopened disruptions are kept in the response and flagged (`reopened: true`) because impact
    typically takes a while to resolve after the line reopens. Use `active_count` /
    `reopened_count` to split the two, and `as_of` as the reference point for every estimate.

    Pass `at` to reconstruct the feed at a past instant instead of now (see the parameter docs);
    `as_of` then echoes `at`, and `refreshed_at` is null because the result is queried live rather
    than served from the cache.
    """
    if at is None:
        now = datetime.now(timezone.utc)
        with _disruptions_lock:
            cached = _disruptions_cache
            refreshed_at = _disruptions_refreshed_at
    else:
        now = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
        if now > datetime.now(timezone.utc):
            raise HTTPException(
                status_code=400,
                detail="`at` must not be in the future; point-in-time reconstruction only covers past instants.",
            )
        cached = fetch_disruptions_as_of(now)
        refreshed_at = None

    items = [
        disruption
        for d in cached
        for disruption in [_to_disruption(d, now)]
        if disruption.should_alert()
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
    "/situation",
    summary="Render a single disruption by its SIRI-SX situationNumber",
    response_description="The disruption with reopen/impact estimates as minutes from `as_of`.",
    tags=["disruptions"],
)
async def situation(
    id: str = Query(
        description="SIRI-SX situationNumber to render, e.g. 'ENT:SituationNumber:1234'.",
    ),
) -> Disruption:
    """Render a single disruption identified by its `situationNumber`.

    Unlike /disruptions — which only surfaces alert-worthy situations — this returns the
    requested situation regardless of alert tier, reconstructed from SIRI-SX as of now (the
    same point-in-time build as GET /disruptions?at=now) and cached for 5 minutes. Every
    estimate is expressed as minutes from the render time. 404 if no NSB disruption with that
    situationNumber is found in the lookback window (yesterday and today).
    """
    d = get_situation(id)
    if d is None:
        raise HTTPException(
            status_code=404,
            detail=f"No disruption found for situationNumber {id!r}.",
        )
    return _to_disruption(d, datetime.now(timezone.utc))


@app.get(
    "/journey",
    summary="Per-train cancellation + delay predictions for a journey",
    response_description="Per-stop predictions for the affected train.",
    tags=["disruptions"],
)
async def journey(
    id: str = Query(
        description=(
            "Journey identifier — a SIRI DatedServiceJourney ref (e.g. "
            "'VYG:DatedServiceJourney:318_LHM-DRM_25-09-01') or the bare jkey. The operator "
            "prefix is stripped, so either form works."
        ),
    ),
    from_stop: str | None = Query(
        default=None,
        description="Optional boarding StopPlace ref (NSR:StopPlace:...) to filter the calls to.",
    ),
    to_stop: str | None = Query(
        default=None,
        description="Optional alighting StopPlace ref (NSR:StopPlace:...) to filter the calls to.",
    ),
) -> JourneyResponse:
    """Per-train predictions for a journey currently affected by a disruption.

    Returns the affected stops, each with a calibrated cancellation probability and typical/p90
    delay. For a passenger: the boarding stop answers "will my train leave, and how late?"
    (`cancel_probability` + `dep_delay_*`); the alighting stop answers "will I reach my
    destination, and how late?" (`cancel_probability` + `arr_delay_*`). Pass `from_stop` /
    `to_stop` to return just those stops.

    404 if no active disruption affects this journey (the train is not in today's per-call
    predictions). When several situations touch the journey, the most severe is returned.
    """
    jkey = re.sub(r"^[A-Za-z]+:", "", id)
    with _call_pred_lock:
        rows = _call_pred_by_jkey.get(jkey)
        refreshed_at = _call_pred_refreshed_at
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No active disruption affecting journey {id!r}.",
        )
    # A journey can be touched by several situations; surface the most severe (highest peak
    # cancellation probability across its stops).
    by_sit: dict[str, list[_CallPred]] = {}
    for r in rows:
        by_sit.setdefault(r.situation_number, []).append(r)
    chosen = max(by_sit.values(), key=lambda rs: max(x.p_cancel for x in rs))
    calls = sorted(chosen, key=lambda x: x.stop_order)
    if from_stop is not None or to_stop is not None:
        wanted = {s for s in (from_stop, to_stop) if s is not None}
        calls = [c for c in calls if c.stop_place in wanted]
    head = chosen[0]
    return JourneyResponse(
        journey_ref=id,
        line=head.line,
        situation_number=head.situation_number,
        summary=head.summary,
        as_of=datetime.now(timezone.utc),
        refreshed_at=refreshed_at,
        calls=[
            CallPrediction(
                stop_place=c.stop_place,
                stop_name=c.stop_name,
                stop_order=c.stop_order,
                scheduled_departure=c.aimed_dep,
                scheduled_arrival=c.aimed_arr,
                cancel_probability=c.p_cancel,
                dep_delay_typ_min=c.dep_delay_typ_min,
                dep_delay_p90_min=c.dep_delay_p90_min,
                arr_delay_typ_min=c.arr_delay_typ_min,
                arr_delay_p90_min=c.arr_delay_p90_min,
            )
            for c in calls
        ],
    )


@app.get(
    "/alert_profile",
    summary="Historical impact profile for a category (optionally a line and hour)",
    response_description="The most specific impact profile with enough data, after back-off.",
    tags=["disruptions"],
)
async def alert_profile(
    category: str = Query(description="Disruption category, e.g. 'infrastruktur/signal'."),
    line: str = Query(
        default="*",
        description="Line code without the 'VYG:Line:' prefix, e.g. 'R31'. Omit (or '*') to pool over lines.",
    ),
    hour: int | None = Query(
        default=None,
        ge=-1,
        le=23,
        description=(
            "Hour-of-day in Europe/Oslo (0-23). Omit to use the current Oslo hour; pass -1 to "
            "pool over all hours."
        ),
    ),
) -> AlertProfile:
    """Look up the historical impact profile for a (category, line, hour) cell.

    Backs off to coarser cells when the specific one is too sparse — drop the hour, then the
    line, finally the global row — so a result is (almost) always returned. Check `grain` /
    `matched_*` to see how specific the match actually is, and `alert_tier` / `alert_score`
    to rank it. 404 only if the lookup table has not loaded yet.
    """
    h = _oslo_hour(datetime.now(timezone.utc)) if hour is None else hour
    row = lookup_alert_profile(category, line, h)
    if row is None:
        raise HTTPException(status_code=404, detail="No impact profile available (lookup not loaded).")
    return _alert_profile_model(row)


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