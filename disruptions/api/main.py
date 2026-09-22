import functools
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

# Aliased: the route function below is also called `impact`, and inside its body a bare
# `impact.` would resolve to that function rather than to this module.
import alert_policy  # sibling module: the shared alert gate (thresholds, the "trouble" bar)
import alternatives  # sibling module: the Journey Planner lookup and its per-caller fetch budgets
import decisions  # sibling module: the append-only Avviksvarsel alert-decision log
import deployment  # sibling module: which environment is answering (dev/tst/prd), for the UI titles
import feedback_contexts  # exact display snapshots for isolated /varsel feedback
import historikk  # sibling module: alert decisions + realized outcomes (/historikk-api)
import impact as impact_engine  # sibling module: the /impact matcher, shared with /varsel-api
import journey_status  # sibling module: every train in a journey's window today, graded (the «I dag» strip)
import llm_message  # sibling module: LLM-written /varsel push text (Vertex)
import logging_setup  # noqa: F401  (import side effect: golden-path JSON logging on stdout)
import meldinger  # sibling module: past alert decisions replayed through today's prompt (/meldinger-api)
import metrics  # sibling module: Prometheus registry, HTTP-metrics middleware, log-level counting
import notification_api  # sibling module: the authenticated notification API (/notification-api/v1)
import notification_auth  # sibling module: client accounts, API keys, callback destinations, audit
import notification_events  # sibling module: this cycle's observations turned into committed events
import notification_pull  # sibling module: the journey-keyed public read (/notification-api/v1/journey-alerts)
import notification_servability  # sibling module: R6's freshness half, re-asked on the poller's cadence
import notification_store  # sibling module: durable webhook subscriptions, events and deliveries
import reisevalg  # sibling module: car vs train corridor recommendation (/reisevalg-api)
import rushhistorikk  # sibling module: did the rush forecasts land? (/rushhistorikk-api)
import sso  # sibling module: the Entur-SSO gate in front of /historikk and /meldinger
import togvarsel  # sibling module: the rush-hour forecast, Togmeldingen (/togmelding-api)
import togvarsel_notify  # sibling module: togvarsel rush forecasts as /varsel Web Pushes
import togvarsel_outcomes  # sibling module: what the rush actually did (/rushhistorikk's fasit)
import tracing_setup  # import side effect: OTel tracing when TRACING_ENABLED (fail-open, KTD3)
import varsel  # sibling module: registered-journey push alerts (/varsel + /varsel-api)
import varsel_state  # where a /varsel registration lives: the seam and its blob backend
import webhook_delivery  # sibling module: one bounded webhook attempt per pending notification event
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.cloud import bigquery
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_LAST_INGESTION_TIME_QUERY = (Path(__file__).parent / "last_ingestion_time.sql").read_text()
_RAIL_CALLS_QUERY = (Path(__file__).parent / "rail_calls_today.sql").read_text()
_DISRUPTIONS_QUERY = (Path(__file__).parent / "disruptions_today.sql").read_text()
_UPDATE_CATEGORIES_QUERY = (Path(__file__).parent / "update_categories.sql").read_text()
_PREDICT_QUERY = (Path(__file__).parent / "predict.sql").read_text()
_ACTIVE_DISRUPTIONS_QUERY = (Path(__file__).parent / "active_disruptions.sql").read_text()
_DISRUPTIONS_AS_OF_QUERY = (Path(__file__).parent / "disruptions_as_of.sql").read_text()
_ALERT_PROFILES_QUERY = (Path(__file__).parent / "alert_profiles.sql").read_text()
# Does any planned trip today run between these two places, and does any run the other way?
# Asked about many stop pairs at once since U10; its own header carries the argument for the
# shape. A FILE rather than the inline string it was until then, because nothing in this repo
# executes SQL and `data/queries/validate_varsel_sql.sh` can only dry-run a file — so a query
# held in a Python string is a query no check reaches.
_VARSEL_CONNECTIVITY_QUERY = (Path(__file__).parent / "varsel_connectivity.sql").read_text()
# Per-call (per-train) serving: the planned-timetable call source and the scoring query.
# NSB/prod family (rail): predict_calls.sql -> call_predictions_today.
_RAIL_CALLS_PLANNED_QUERY = (Path(__file__).parent / "rail_calls_planned_today.sql").read_text()
_PREDICT_CALLS_QUERY = (Path(__file__).parent / "predict_calls.sql").read_text()
_CALL_PREDICTIONS_QUERY = "SELECT * FROM `disruptions.call_predictions_today`"
# The journey-status spine (plan 2026-09-22-001): the day's planned Vy calls, read into memory after
# each planned rebuild. `SELECT *` so the load survives the deploy that adds a column (U4's
# `arr_delay_min`) before the table is rebuilt with it -- the row reader uses getattr.
_JOURNEY_STATUS_SPINE_QUERY = "SELECT * FROM `disruptions.rail_calls_planned_today`"
# ... and its actuals half: today's recorded Vy calls, after each 15-min actuals rebuild.
_JOURNEY_STATUS_ACTUALS_QUERY = "SELECT * FROM `disruptions.rail_calls_today`"
# Bus/GOA family (pooled_all): the bus planned call source, the bus SX source, and the bus scoring
# query -> call_predictions_bus_today (same columns as call_predictions_today PLUS `operator`).
_BUS_CALLS_PLANNED_QUERY = (Path(__file__).parent / "bus_calls_planned_today.sql").read_text()
_DISRUPTIONS_BUS_QUERY = (Path(__file__).parent / "disruptions_bus_initial_today.sql").read_text()
_PREDICT_CALLS_BUS_QUERY = (Path(__file__).parent / "predict_calls_bus.sql").read_text()
_CALL_PREDICTIONS_BUS_QUERY = (
    "SELECT * FROM `disruptions.call_predictions_bus_today`"
)
# /demo-api: retrospective journey search over the static demo tables
# (built offline by data/queries/create_demo_predictions.sql).
_DEMO_JOURNEYS_QUERY = (Path(__file__).parent / "demo_journeys.sql").read_text()
_DEMO_STOPS_QUERY = """
    SELECT stop_place, stop_name, n_calls, mode
    FROM `disruptions.demo_stops`
    ORDER BY n_calls DESC
"""
# Most recent scenario first: the demo tables gain days daily, and a partner is more convinced by
# last week's disruption than by a worse one from the spring holdout. Which situations make the
# list is still decided by realized impact (demo_highlights' per-operator cap + LIMIT 40, see
# create_demo_predictions.sql) -- this orders that set, it does not pick it. Sorted on the
# timestamp the dropdown actually shows (window_start), with impact as the tie-break so a busy
# day still leads with its worst.
_DEMO_HIGHLIGHTS_QUERY = """
    SELECT situationNumber, operator, summary, category, onset_ts, n_cancelled, n_delayed10,
           window_start, window_end, from_stop, from_name, to_stop, to_name
    FROM `disruptions.demo_highlights`
    ORDER BY window_start DESC, n_cancelled + n_delayed10 DESC
"""
_DEMO_WINDOW_QUERY = """
    SELECT MIN(call_date) AS window_start, MAX(call_date) AS window_end
    FROM `disruptions.demo_calls`
"""
# /varsel-api/stops: stops for the avviksvarsel journey pickers, from the planned-timetable call
# sources rebuilt on their TTLs (tiny GROUP BY over small tables). Rail and bus are queried
# separately and merged in Python (refresh_varsel_stops) so a missing/failing bus table never
# drops the rail stops. Each row carries today's call count (pickers rank matches by traffic)
# and which families serve the stop — GOA is the one rail operator in the bus/pooled table.
_VARSEL_STOPS_QUERY = """
    SELECT stop_place, ANY_VALUE(stop_name) AS stop_name, COUNT(*) AS n_calls,
           TRUE AS has_rail, FALSE AS has_bus
    FROM `disruptions.rail_calls_planned_today`
    WHERE stop_place IS NOT NULL
    GROUP BY stop_place
"""
_VARSEL_STOPS_BUS_QUERY = """
    SELECT stop_place, ANY_VALUE(stop_name) AS stop_name, COUNT(*) AS n_calls,
           LOGICAL_OR(operator = 'GOA') AS has_rail,
           LOGICAL_OR(operator != 'GOA') AS has_bus
    FROM `disruptions.bus_calls_planned_today`
    WHERE stop_place IS NOT NULL
    GROUP BY stop_place
"""
# The National Stop Register, which is what tells us that two StopPlaces are ONE physical place:
# co-located refs (a station's rail and bus sides, a ferry berth registered twice) share a
# `parentRef`. Measured 2026-08-03 over the 22 481 stops in the picker: 1 573 names were offered
# more than once, and 422 of the 427 same-name pairs within 300 m share a parent — so the parent is
# the registry's own notion of "same place", not a distance heuristic of ours. Without this the
# picker offered visually identical rows (two "Nyvoll ferjekai"), and choosing the wrong one failed
# in the most reassuring way available: `to_stop` narrowing dropped every situation and the card
# said "ingen aktive avvik akkurat nå".
#
# `topographicPlaceRef` names the stop's MUNICIPALITY in the registry's own topographic-place table
# — the disambiguator for the same-name stops that are NOT the same place (1 260 of those groups lie
# over 20 km apart). The municipality, not the county, since 2026-09-02 (design review 2026-09-01,
# finding 2): «Sandvika» is 29 different stops, eight of them in Troms, so a county suffix offered
# «Sandvika (Troms)» six times over and the municipality is what tells Dyrøy from Tromsø. Measured
# that day: 64 469 of 64 703 stop places resolve to a named municipality (99.6 %), every one of them
# with a county parent, and the table carries both the current numbering and the pre-2020 codes
# (`KVE:TopographicPlace:0219` and `:3201` both resolve to Bærum). The two-digit county prefix is
# still read alongside, as the fallback for the 121 refs the table does not resolve — only the
# `KVE:` codespace is Kartverket's numbering; `LAN:` (Sweden) and `WOF:` refs stay unlabelled rather
# than mapped through a table that does not describe them. Readable by any authenticated principal
# (the dataset grants allAuthenticatedUsers READER), so this works in prd as well as dev, and the
# whole thing is ~3 MB per refresh.
_VARSEL_STOP_PLACES_QUERY = """
    WITH tp AS (
      SELECT id, name, type, parentRef
      FROM `ent-data-sharing-ext-prd.national_stop_registry.topographic_places_all_versions`
      QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY version DESC) = 1
    ),
    sp AS (
      SELECT id,
             COALESCE(ANY_VALUE(parentRef.ref), id) AS place_id,
             ANY_VALUE(topographicPlaceRef.ref) AS tp_ref
      FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version`
      GROUP BY id
    )
    SELECT sp.id,
           sp.place_id,
           IF(STARTS_WITH(sp.tp_ref, 'KVE:'), SUBSTR(sp.tp_ref, -4, 2), NULL) AS county_no,
           IF(tp.type = 'municipality', tp.name, NULL) AS municipality,
           COALESCE(IF(tp.type = 'county', tp.name, NULL), parent.name) AS county
    FROM sp
    LEFT JOIN tp ON tp.id = sp.tp_ref
    LEFT JOIN tp AS parent ON parent.id = tp.parentRef
"""
# Municipality-number prefix -> county name. NSR carries both the current numbering and pre-2020
# codes (Kambo is 0104, not 3103), so both generations are mapped, each to the county that exists
# today. 30 / 38 / 54 are the 2020-2023 merged counties, kept under their own names because a stop
# numbered 30xx cannot be resolved to one of Viken's successors from the prefix alone. Anything not
# listed gets no label, which is the pre-existing behaviour rather than a guess.
_COUNTY_BY_MUNICIPALITY_PREFIX: dict[str, str] = {
    "01": "Østfold", "02": "Akershus", "03": "Oslo", "04": "Innlandet", "05": "Innlandet",
    "06": "Buskerud", "07": "Vestfold", "08": "Telemark", "09": "Agder", "10": "Agder",
    "11": "Rogaland", "12": "Vestland", "14": "Vestland", "15": "Møre og Romsdal",
    "16": "Trøndelag", "17": "Trøndelag", "18": "Nordland", "19": "Troms", "20": "Finnmark",
    "30": "Viken", "31": "Østfold", "32": "Akershus", "33": "Buskerud", "34": "Innlandet",
    "38": "Vestfold og Telemark", "39": "Vestfold", "40": "Telemark", "42": "Agder",
    "46": "Vestland", "50": "Trøndelag", "54": "Troms og Finnmark", "55": "Troms",
    "56": "Finnmark",
}

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

# Ceiling on every BigQuery wait the POLLER makes (get_last_ingestion_time + the query jobs the
# rebuild issues from this module), via `result(timeout=...)`. It exists for /health's staleness
# check: kept below HEALTH_STALE_AFTER_S (600 s) so a SLOW upstream surfaces as the existing
# per-tick logged error — the heartbeat keeps beating, the next tick retries — instead of wedging
# the loop past the threshold and restart-looping the pod against an outage a restart cannot fix.
# Request-path queries (disruptions_as_of, /demo-api) deliberately keep unbounded waits.
_POLLER_QUERY_TIMEOUT_S = 240


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
    rows = bq_client.query(_ACTIVE_DISRUPTIONS_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
    disruptions = [_row_to_cached(r) for r in rows]
    with _disruptions_lock:
        global _disruptions_cache, _disruptions_refreshed_at
        _disruptions_cache = disruptions
        _disruptions_refreshed_at = datetime.now(UTC)
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
    now = datetime.now(UTC)
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
    rows = bq_client.query(_ALERT_PROFILES_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
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

_call_pred_lock = threading.Lock()
# Keyed by (operator, jkey): bus ServiceJourney refs are NOT date-scoped, so the bare jkey collides
# across operators (SKY:ServiceJourney:12345 and ATB:ServiceJourney:12345 both strip to the same
# jkey). The operator is the ref codespace prefix, which /journey recovers from the id.
_call_pred_by_key: dict[tuple[str, str], list[impact_engine.CallPred]] = {}
# jkey -> the set of operators that carry it, so a bare-jkey /journey lookup (no prefix) can still
# resolve when unambiguous, and 409 when it is not.
_call_pred_by_jkey_index: dict[str, set[str]] = {}
# Same scored calls, indexed by situationNumber, so /impact can resolve a passenger's train from
# (situation, from_stop, at) without a journey ref. situationNumber is publisher-namespaced, so the
# NSB and bus tables merge here without collision.
_call_pred_by_situation: dict[str, list[impact_engine.CallPred]] = {}
# The SAME scored calls, indexed by the stop they call at (U2, 2026-09-12): `{stop_place:
# [(situationNumber, CallPred)]}`. This is the posting list `impact` prunes a scan with, so the
# matcher costs per place PAIR per generation instead of per registered definition — see the block
# above `impact._index_lock`. Built here, in the ingest loop, rather than lazily in the engine so
# the poller thread pays the O(calls) pass and never a request thread; the engine still builds its
# own when this is missing or belongs to another generation.
_call_pred_by_stop_ref: dict[str, list[tuple[str, impact_engine.CallPred]]] = {}
_call_pred_refreshed_at: datetime | None = None

# /impact matches the passenger's intended departure (`at`) to the nearest affected departure at
# `from_stop`. If the closest affected call is further than this, the train is treated as not
# affected (omitted) — wide enough to absorb an approximate `at`, tight enough not to grab a
# different service on the line.
_IMPACT_AT_TOLERANCE = timedelta(minutes=90)

# Default boarding stop for /impact when the caller omits from_stop: Oslo S, the busiest hub.
_DEFAULT_FROM_STOP = "NSR:StopPlace:337"  # Oslo S

# `rail_calls_today` is today's ET *actuals* slice, and it was the single most expensive thing the
# poller did: CREATE OR REPLACE over the whole day-so-far, so one rebuild costs 0.02 GB just after
# midnight and 3.4 GB by 23:00 (measured 2026-08-08). Ungated it ran on every SX ingestion change
# -- 474 times that day, 543 GB, 53 % of the environment's scanned bytes.
#
# Two reasons that cadence bought nothing. The poller wakes on the wrong feed: it polls SIRI-SX
# ingestion (a disruption *text* being published) while this table reads SIRI-ET (a train actually
# passing a stop) -- the two advance independently. And nothing live reads it: `/journey`,
# `/impact` and every varsel push score against `rail_calls_planned_today` (predict_calls.sql), not
# this table. Its consumers were retrospective -- the `actual_*` columns in predict.sql and the
# impact label in disruptions_today.sql -- both measured over an onset + 12 h window that a quarter
# of an hour cannot move. Since 2026-09-22 it has ONE live reader, `refresh_rail_actuals` (the
# /varsel «I dag» strip), which is what the 15 min buys a traveller: a recorded arrival reaches the
# strip within a quarter of an hour instead of the planned table's hour. It reads the table the
# rebuild already wrote, so the cost below is unchanged.
#
# The ET source is DAY-partitioned and unclustered, so there is no cheap incremental read to reach
# for; running it less often is the only lever. Cost is linear in rebuild count (~1.7 GB per run
# averaged over a day), so 15 min takes it from ~522 GB/day to ~163 GB/day per environment.
_rail_calls_lock = threading.Lock()
_rail_calls_refreshed_at: datetime | None = None
_RAIL_CALLS_TTL = timedelta(minutes=15)


def maybe_refresh_rail_calls() -> None:
    """Rebuild disruptions.rail_calls_today if it is stale (older than _RAIL_CALLS_TTL).

    The actuals-side sibling of maybe_refresh_planned_calls. Always runs on first call
    (refreshed_at is None).

    The date check is not redundant with the TTL: the table holds one UTC day (the SQL prunes on
    `DATE(_PARTITIONTIME) = CURRENT_DATE()`), so at midnight its contents become *yesterday's*
    until the next rebuild. Without it a tick at 00:05 would be served a stale day for the rest of
    the TTL. Forcing the rollover rebuild is close to free -- just after midnight is the cheapest
    moment of the day to run this query."""
    global _rail_calls_refreshed_at
    now = datetime.now(UTC)
    with _rail_calls_lock:
        fresh = (
            _rail_calls_refreshed_at is not None
            and now - _rail_calls_refreshed_at < _RAIL_CALLS_TTL
            and _rail_calls_refreshed_at.date() == now.date()
        )
    if fresh:
        return
    bq_client.query(_RAIL_CALLS_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
    with _rail_calls_lock:
        _rail_calls_refreshed_at = now
    logger.info("Rebuilt actuals call source (rail_calls_today)")


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
    the hourly cadence above is plenty. Always runs on first call (refreshed_at is None)."""
    global _planned_refreshed_at
    now = datetime.now(UTC)
    with _planned_lock:
        fresh = (
            _planned_refreshed_at is not None and now - _planned_refreshed_at < _PLANNED_TTL
        )
    if fresh:
        return
    bq_client.query(_RAIL_CALLS_PLANNED_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
    with _planned_lock:
        _planned_refreshed_at = now
    logger.info("Rebuilt planned call source (rail_calls_planned_today)")


def _rail_operator(line_ref: str | None) -> str:
    """The rail family's operator: the lineRef codespace (`VYG:Line:L1` -> `VYG`). One statement,
    because the journey-status spine and the prediction cache must derive it alike or the strip's
    `(operator, jkey)` join to the predictions misses silently and every train reads grey."""
    return (line_ref or "VYG").split(":")[0]


def _planned_call(r, *, call_source: str | None = None) -> journey_status.PlannedCall:
    """One call-table row as the journey-status engine holds it."""
    return journey_status.PlannedCall(
        operator=_rail_operator(r.lineRef),
        jkey=r.jkey,
        line=r.lineRef,
        stop_place=r.stop_place,
        stop_order=r.stop_order,
        aimed_dep=r.aimed_dep,
        aimed_arr=getattr(r, "aimed_arr", None),
        dep_delay_min=r.dep_delay_min,
        arr_delay_min=getattr(r, "arr_delay_min", None),
        call_cancelled=bool(r.call_cancelled),
        call_source=call_source or getattr(r, "call_source", None),
        # Fail open, like every other reader of the two activities: NULL is permission.
        boarding_allowed=getattr(r, "boarding_allowed", None) is not False,
        alighting_allowed=getattr(r, "alighting_allowed", None) is not False,
    )


def refresh_rail_actuals() -> None:
    """Load `rail_calls_today` into `journey_status` when the table has been rebuilt since the copy
    it holds -- every 15 min at most (`_RAIL_CALLS_TTL`), a no-op on the ticks between.

    Every row is a recorded call (the SQL keeps nothing else), so each is tagged `recorded` here;
    the table carries no `call_source` column. Same stamp-is-the-guard rule as the spine: a failed
    load keeps the previous index and the next tick retries."""
    with _rail_calls_lock:
        built = _rail_calls_refreshed_at
    if built is None or journey_status.actuals_refreshed_at() == built:
        return
    rows = bq_client.query(_JOURNEY_STATUS_ACTUALS_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
    table = journey_status.CallTable((_planned_call(r, call_source="recorded") for r in rows), built)
    journey_status.set_actuals(table)
    logger.info("Loaded the journey-status actuals: %d recorded calls", len(table))


def refresh_planned_spine() -> None:
    """Load `rail_calls_planned_today` into `journey_status` when the table has been rebuilt since
    the copy it holds. One ~22 000-row SELECT per rebuild, i.e. hourly; a no-op on every other tick.

    Stamped with `_planned_refreshed_at`, the instant the REBUILD finished, not the load: that is
    how old the timetable the strip draws is, and it is also the guard -- a failed load leaves the
    engine's stamp behind the table's, so the next tick tries again, while the engine keeps serving
    the copy it had."""
    with _planned_lock:
        built = _planned_refreshed_at
    if built is None or journey_status.spine_refreshed_at() == built:
        return
    rows = bq_client.query(_JOURNEY_STATUS_SPINE_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
    table = journey_status.CallTable((_planned_call(r) for r in rows), built)
    journey_status.set_spine(table)
    logger.info("Loaded the journey-status spine: %d planned calls", len(table))


# The bus planned scan is heavier than rail (it materialises calls for ~10 bus/local codespaces
# off the same ET partition, ~12 GB/run). Give it its own, slower TTL and its own lock so a stale
# or failed bus scan degrades bus only and never gates the rail cadence.
_bus_planned_lock = threading.Lock()
_bus_planned_refreshed_at: datetime | None = None
_BUS_PLANNED_TTL = timedelta(minutes=120)


def maybe_refresh_bus_planned_calls() -> None:
    """Rebuild disruptions.bus_calls_planned_today if it is stale (older than _BUS_PLANNED_TTL).

    Same rationale as maybe_refresh_planned_calls, but slower — the bus schedule is just as stable
    and the scan is larger. Always runs on first call (refreshed_at is None)."""
    global _bus_planned_refreshed_at
    now = datetime.now(UTC)
    with _bus_planned_lock:
        fresh = (
            _bus_planned_refreshed_at is not None
            and now - _bus_planned_refreshed_at < _BUS_PLANNED_TTL
        )
    if fresh:
        return
    bq_client.query(_BUS_CALLS_PLANNED_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
    with _bus_planned_lock:
        _bus_planned_refreshed_at = now
    logger.info("Rebuilt bus planned call source (bus_calls_planned_today)")


def _call_pred_from_row(r, operator: str) -> impact_engine.CallPred:
    """Map one prediction row (call_predictions_today or _bus_today) to a CallPred, tagging it
    with its operator (the ref codespace)."""
    return impact_engine.CallPred(
        situation_number=r.situationNumber,
        summary=r.summary,
        description=r.description,
        jkey=r.jkey,
        operator=operator,
        line=r.lineRef,
        stop_place=r.stop_place,
        stop_name=r.stop_name,
        stop_order=r.stop_order,
        aimed_dep=r.aimed_dep,
        aimed_arr=r.aimed_arr,
        et_declared_cancelled=r.et_declared_cancelled,
        et_dep_delay_min=r.et_dep_delay_min,
        call_source=r.call_source,
        p_cancel=r.p_cancel,
        dep_delay_typ_min=r.dep_delay_typ_min,
        dep_delay_p90_min=r.dep_delay_p90_min,
        arr_delay_typ_min=r.arr_delay_typ_min,
        arr_delay_p90_min=r.arr_delay_p90_min,
        p_arr_ge10=r.p_arr_ge10,
        p_arr_ge20=r.p_arr_ge20,
        arr_delay_p50_stack_min=r.arr_delay_p50_stack_min,
        arr_delay_p90_stack_min=r.arr_delay_p90_stack_min,
        p_impact=r.p_impact,
        p_dep_ge10=r.p_dep_ge10,
        p_dep_ge20=r.p_dep_ge20,
        dep_delay_p50_stack_min=r.dep_delay_p50_stack_min,
        dep_delay_p90_stack_min=r.dep_delay_p90_stack_min,
        p_impact_dep=r.p_impact_dep,
        # getattr, not r.has_situation: call_predictions_bus_today has no such column (the bus
        # pooled family was not extended to the no-SX population), and neither does a rail table
        # built by a predict_calls.sql older than 2026-08-19 -- during a rollout the app can outrun
        # the poller by one cycle. Both cases mean "an SX message named this call", which is what
        # the default says.
        has_situation=getattr(r, "has_situation", True),
        # Same getattr rule, same rollout window: a prediction table built before 2026-09-01 has
        # neither column, and "not stated" is permission. The matcher reads these to keep a
        # boarding-only long-distance call from being offered as somebody's trip.
        boarding_allowed=getattr(r, "boarding_allowed", None) is not False,
        alighting_allowed=getattr(r, "alighting_allowed", None) is not False,
    )


def refresh_call_predictions_cache() -> None:
    """Reload the per-call predictions cache from BOTH the NSB (call_predictions_today) and the
    bus/GOA (call_predictions_bus_today) tables, keyed by (operator, jkey).

    NSB rows are tagged with the lineRef codespace ('VYG'); bus rows carry an explicit `operator`
    column. The bus load is isolated so a missing/failing bus table (e.g. before the first bus
    predict runs) never blocks NSB serving."""
    by_key: dict[tuple[str, str], list[impact_engine.CallPred]] = {}
    by_situation: dict[str, list[impact_engine.CallPred]] = {}
    by_jkey_index: dict[str, set[str]] = {}
    by_stop_ref: dict[str, list[tuple[str, impact_engine.CallPred]]] = {}

    def _ingest(rows, operator_of: Callable[..., str]) -> None:
        for r in rows:
            op = operator_of(r)
            pred = _call_pred_from_row(r, op)
            by_key.setdefault((op, r.jkey), []).append(pred)
            by_situation.setdefault(r.situationNumber, []).append(pred)
            by_jkey_index.setdefault(r.jkey, set()).add(op)
            # The fourth index (U2): one more `setdefault` in a loop that is already O(calls), so
            # the matcher's scan can be bounded by the stop pair it was asked about. The situation
            # travels IN the entry rather than being read back off the CallPred, because the key of
            # `by_situation` is what scopes a scan and the engine must not have to assume the two
            # agree — they do here, and that is exactly the assumption worth not encoding twice.
            by_stop_ref.setdefault(pred.stop_place, []).append((r.situationNumber, pred))

    # NSB/prod family: operator is the rail codespace of the lineRef (VYG:Line:... -> 'VYG').
    _ingest(
        bq_client.query(_CALL_PREDICTIONS_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S),
        lambda r: _rail_operator(r.lineRef),
    )
    # Bus/GOA/pooled family: the table carries an explicit operator column.
    try:
        _ingest(
            bq_client.query(_CALL_PREDICTIONS_BUS_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S),
            lambda r: r.operator,
        )
    except Exception:
        logger.exception(
            "Bus per-call predictions load failed; serving NSB-only this refresh "
            "(is call_predictions_bus_today built yet?)"
        )

    with _call_pred_lock:
        global _call_pred_by_key, _call_pred_by_jkey_index, _call_pred_by_stop_ref
        global _call_pred_by_situation, _call_pred_refreshed_at
        _call_pred_by_key = by_key
        _call_pred_by_jkey_index = by_jkey_index
        _call_pred_by_situation = by_situation
        _call_pred_by_stop_ref = by_stop_ref
        _call_pred_refreshed_at = datetime.now(UTC)
    logger.info(
        "Refreshed per-call predictions cache: %d (operator, journey) keys, %d situations, "
        "%d stop refs",
        len(by_key),
        len(by_situation),
        len(by_stop_ref),
    )


# ---- Avviksvarsel providers ------------------------------------------------------------------
# The varsel module (registered-journey push alerts) reads the live per-call snapshot and the
# stop list (rail + bus) through these providers, injected via varsel.configure() at startup so
# varsel never imports main (no cycle). The stop list is refreshed alongside the planned sources.
_varsel_stops_lock = threading.Lock()
_varsel_stops_cache: list[dict] = []


def _varsel_stop_places() -> dict[str, tuple[str, str | None]]:
    """NSR ref -> (place_id, region label). Empty when the registry cannot be read.

    The region is the MUNICIPALITY name when the registry resolves one, else the county the
    municipality number's prefix maps to, else None — so a ref the topographic table does not know
    is labelled the way every ref was until 2026-09-02, not left bare. `getattr`, because the row
    shape is also what the hermetic suite hands in, and an older fixture carries no municipality.

    Fails open like every other sub-query here: no registry means no grouping and no region
    labels, which is what the picker did before this existed — not an empty stop list.
    """
    places: dict[str, tuple[str, str | None]] = {}
    try:
        for r in bq_client.query(_VARSEL_STOP_PLACES_QUERY).result(
            timeout=_POLLER_QUERY_TIMEOUT_S
        ):
            county_no = getattr(r, "county_no", None)
            region = (
                getattr(r, "municipality", None)
                or getattr(r, "county", None)
                or (_COUNTY_BY_MUNICIPALITY_PREFIX.get(county_no) if county_no else None)
            )
            places[r.id] = (r.place_id, region)
    except Exception:
        logger.exception(
            "The national stop registry could not be read; stops keep their own place ids "
            "(same-named stops will be offered separately and carry no region)"
        )
    return places


def refresh_varsel_stops() -> None:
    """Reload the stop picker list from the rail AND bus planned call sources (tiny scans).

    Each source is queried independently and merged by stop_place, so a missing/failing bus table
    never drops the rail stops (and vice versa). The merge sums call counts and ORs the mode
    flags — a stop served by both families gets mode "Tog/Buss".

    Co-located refs are then collapsed to ONE picker row: same NSR parent place AND the same name
    means one physical stop registered more than once (a station's rail and bus sides, a ferry berth
    with two refs), and offering both is how a traveller ends up watching a quay their trip never
    calls at. The row keeps the busiest ref, sums the calls, and the refs it absorbed become
    aliases — `varsel` rewrites registrations onto the primary so both surfaces agree on one ref per
    place. Grouping needs the name as well as the parent: a parent can hold genuinely different
    stops (Oslo S and Jernbanetorget), and merging those would answer a search for one with the
    other. `region` on each row is the municipality (see `_varsel_stop_places`), which the picker
    appends to a name that belongs to several places.
    """
    merged: dict[str, dict] = {}
    for q in (_VARSEL_STOPS_QUERY, _VARSEL_STOPS_BUS_QUERY):
        try:
            for r in bq_client.query(q).result(timeout=_POLLER_QUERY_TIMEOUT_S):
                e = merged.setdefault(
                    r.stop_place,
                    {"stop_place": r.stop_place, "stop_name": None, "n_calls": 0,
                     "has_rail": False, "has_bus": False},
                )
                e["stop_name"] = e["stop_name"] or r.stop_name
                e["n_calls"] += r.n_calls
                e["has_rail"] = e["has_rail"] or r.has_rail
                e["has_bus"] = e["has_bus"] or r.has_bus
        except Exception:
            logger.exception("A varsel stops sub-query failed; continuing with the other source(s)")

    places = _varsel_stop_places()
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in merged.values():
        place_id, county = places.get(e["stop_place"], (e["stop_place"], None))
        e["place_id"] = place_id
        e["region"] = county
        name = (e["stop_name"] or "").strip().casefold()
        # An unnamed stop is never merged: its name cannot be shown to disambiguate it either, so
        # collapsing it into a neighbour would hide a ref behind a row nobody can read.
        key = (place_id, name) if name else (e["stop_place"], "")
        groups.setdefault(key, []).append(e)

    stops: list[dict] = []
    aliases: dict[str, str] = {}
    for members in groups.values():
        # Busiest ref wins, ties broken on the ref itself so the primary does not flip between
        # refreshes (a flipping primary would rewrite registrations back and forth).
        primary, *rest = sorted(members, key=lambda e: (-e["n_calls"], e["stop_place"]))
        has_rail = any(e["has_rail"] for e in members)
        has_bus = any(e["has_bus"] for e in members)
        stops.append(
            {
                "stop_place": primary["stop_place"],
                "stop_name": primary["stop_name"],
                "n_calls": sum(e["n_calls"] for e in members),
                "mode": "Tog/Buss" if has_rail and has_bus else ("Tog" if has_rail else "Buss"),
                "place_id": primary["place_id"],
                "region": primary["region"],
            }
        )
        for e in rest:
            aliases[e["stop_place"]] = primary["stop_place"]

    with _varsel_stops_lock:
        global _varsel_stops_cache
        _varsel_stops_cache = stops
    varsel.set_stop_aliases(aliases)
    logger.info(
        "Refreshed varsel stops: %d rows (%d refs collapsed onto a co-located primary)",
        len(stops),
        len(aliases),
    )


def _varsel_stops() -> list[dict]:
    with _varsel_stops_lock:
        return _varsel_stops_cache


# Answers are cached: the planned tables are rebuilt on their own TTL, so re-asking about the same
# pair within a few minutes cannot change the verdict, and a client retrying a rejected registration
# must not cost a query each time.
_CONNECTIVITY_TTL_S = 10 * 60
# A ceiling on how often a JOB may be issued — one job however many pairs it asks about, since U10.
# /varsel-api is unauthenticated, and the cache cannot bound the cost of a caller that keeps
# inventing pairs; that caller asks about ONE pair per request, so for it a job and a pair are still
# the same thing and this number means exactly what it did. Past the ceiling the check reports
# "unknown" and registration proceeds unvalidated — the same degradation as a BigQuery outage, which
# is the right way for a guard-rail to fail.
_CONNECTIVITY_MAX_PER_MIN = 30
# ...and the poller's two servability sweeps get their own, smaller ceiling, because the paragraph
# above is an argument about the public unauthenticated endpoint only.
# `alternatives._POLLER_FETCH_MAX_PER_MIN` split from `_FETCH_MAX_PER_MIN` for exactly this reason --
# "an abuse-control number was setting the alerting budget only by accident" -- and this was the
# same mistake one layer over: client onboarding and `notification_servability.recheck` both reached
# this seam and spent the traveller's tally. A client may issue
# `notification_auth.PER_CLIENT_PER_MIN` = 120 create/replace requests a minute, so a bulk
# onboarding of an existing commuter base -- which is what that channel is FOR -- exhausted a
# traveller-facing budget as a side effect, and did it silently: `None` reads as "could not
# establish", so a real traveller's journey then registers with no no-direct-service caveat.
#
# It was `_NOTIFICATION_API_CONNECTIVITY_MAX_PER_MIN` (15) until U10, when onboarding stopped asking
# at all. What is left on this path is the two sweeps -- `notification_servability.recheck` and
# `varsel.recheck_service` -- each issuing ONE job per ~296 s cycle however many records are
# pending, so 4/min is an order of magnitude above the reachable draw rather than a budget anybody
# spends. The worst-case BigQuery draw from this seam therefore goes 30 + 15 -> 30 + 4 per minute.
_POLLER_CONNECTIVITY_MAX_PER_MIN = 4
_CONNECTIVITY_REQUEST_PATH = "request"
_CONNECTIVITY_POLLER_PATH = "poller"
# One job's worth of pairs. The callers bound their own sweeps well under this; it is here so a
# caller that forgets to costs its own pairs an answer — they come back `None`, which every caller
# already reads as "could not establish" and leaves due — rather than costing the process a
# multi-megabyte query parameter.
_CONNECTIVITY_MAX_PAIRS_PER_JOB = 2000
# Derived rather than chosen, and it was a flat 512 until U10: two sweeps run per cycle
# (`notification_servability.recheck` and `varsel.recheck_service`), each able to fill a whole job,
# so anything under twice the job bound would have a sweep evicting the other sweep's answers — and
# both evicting the traveller-facing entries the cache exists for. Doubled again for the request
# path's own pairs. Each entry is two short ref tuples, a float and two booleans, so the whole cache
# is a megabyte or so.
_CONNECTIVITY_CACHE_MAX = 4 * _CONNECTIVITY_MAX_PAIRS_PER_JOB
# This one runs on a request path, so it gets an explicit ceiling rather than "wait forever": a slow
# BigQuery is not a reason for a traveller's registration to hang.
_CONNECTIVITY_TIMEOUT_S = 20.0
_connectivity_lock = threading.Lock()
# ONE cache across both paths, deliberately: an established answer about a stop pair is the same
# answer whoever asked, and splitting it would make the poller's sweep re-ask questions a traveller
# has already paid for. Only the BUDGET is per caller.
_connectivity_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], tuple[float, tuple[bool, bool]]] = {}
_connectivity_calls: dict[str, list[float]] = {
    _CONNECTIVITY_REQUEST_PATH: [],
    _CONNECTIVITY_POLLER_PATH: [],
}


def _connectivity_key(
    from_refs: Sequence[str], to_refs: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The cache key for one pair. Sorted, so the order a caller lists refs in cannot split it."""
    return (tuple(sorted(from_refs)), tuple(sorted(to_refs)))


def _varsel_connectivity_batch(
    pairs: Sequence[tuple[Sequence[str], Sequence[str]]],
    *,
    path: str = _CONNECTIVITY_REQUEST_PATH,
) -> list[tuple[bool, bool] | None]:
    """One answer per input pair — (runs from→to today, runs to→from today), or None — in ONE job.

    Takes every ref of each place (`varsel.place_refs`), not just the canonical one: each feed
    records its calls on its own ref, so asking about one ref of a merged stop would report "no
    service" for a trip that plainly exists.

    **Answers are positional**, so the caller pairs them with its own list and nothing here has to
    know what a pair is FOR. Duplicates in the input are asked about once and answered twice; a pair
    the cache already holds costs no place in the job at all.

    ``path`` names WHOSE per-minute budget this job spends — see
    :data:`_POLLER_CONNECTIVITY_MAX_PER_MIN`. It defaults to the public one, which is the safe
    direction: a caller that forgets to name itself is charged the tighter, traveller-facing tally
    rather than silently minting a budget of its own.

    Every failure — the ceiling, a BigQuery error, a pair the job answered nothing for — is ``None``
    for the pairs it touches and never ``False``: an unanswerable question is not evidence of no
    service, and a caller that stored it as one would render a caveat R5 forbids.
    """
    keys = [_connectivity_key(from_refs, to_refs) for from_refs, to_refs in pairs]
    answers: dict[tuple[tuple[str, ...], tuple[str, ...]], tuple[bool, bool]] = {}
    now = monotonic()
    with _connectivity_lock:
        for key in keys:
            hit = _connectivity_cache.get(key)
            if hit is not None and now - hit[0] < _CONNECTIVITY_TTL_S:
                answers[key] = hit[1]
        # Deduped and in first-seen order, which is what makes `pair_id` an index into this list.
        asked = [key for key in dict.fromkeys(keys) if key not in answers]
        if not asked:
            return [answers.get(key) for key in keys]
        if len(asked) > _CONNECTIVITY_MAX_PAIRS_PER_JOB:
            logger.warning(
                "A connectivity batch asked about %d stop pairs; %d of them are answered as "
                "unknown and stay due",
                len(asked),
                len(asked) - _CONNECTIVITY_MAX_PAIRS_PER_JOB,
                extra={
                    "fields": {
                        "event": "varsel_service_check",
                        "result": "truncated",
                        "path": path,
                        "asked": len(asked),
                    }
                },
            )
            asked = asked[:_CONNECTIVITY_MAX_PAIRS_PER_JOB]
        ceiling = (
            _POLLER_CONNECTIVITY_MAX_PER_MIN
            if path == _CONNECTIVITY_POLLER_PATH
            else _CONNECTIVITY_MAX_PER_MIN
        )
        calls = _connectivity_calls.setdefault(path, [])
        calls[:] = [t for t in calls if now - t < 60]
        if len(calls) >= ceiling:
            logger.warning(
                "Connectivity checks rate-limited on the %s path (%d/min); registering without "
                "the check",
                path,
                ceiling,
                extra={
                    "fields": {
                        "event": "varsel_service_check",
                        "result": "rate_limited",
                        "path": path,
                        "asked": len(asked),
                    }
                },
            )
            return [answers.get(key) for key in keys]
        calls.append(now)
    parameter = bigquery.ArrayQueryParameter(
        "pairs",
        "STRUCT",
        [
            bigquery.StructQueryParameter(
                None,
                bigquery.ScalarQueryParameter("pair_id", "INT64", index),
                bigquery.ArrayQueryParameter("from_refs", "STRING", list(from_refs)),
                bigquery.ArrayQueryParameter("to_refs", "STRING", list(to_refs)),
            )
            for index, (from_refs, to_refs) in enumerate(asked)
        ],
    )
    try:
        job = bq_client.query(
            _VARSEL_CONNECTIVITY_QUERY,
            job_config=bigquery.QueryJobConfig(query_parameters=[parameter]),
        )
        rows = list(job.result(timeout=_CONNECTIVITY_TIMEOUT_S))
    except Exception:
        # Never blocks a registration: an unanswerable question is not evidence of no service. The
        # refs are deliberately NOT in this message — see `_log_connectivity`.
        logger.warning(
            "Could not check planned service for %d stop pair(s); registering without the check",
            len(asked),
            exc_info=True,
            extra={
                "fields": {
                    "event": "varsel_service_check",
                    "result": "error",
                    "path": path,
                    "asked": len(asked),
                }
            },
        )
        return [answers.get(key) for key in keys]
    established: dict[tuple[tuple[str, ...], tuple[str, ...]], tuple[bool, bool]] = {}
    for row in rows:
        index = row.pair_id
        if index is not None and 0 <= index < len(asked):
            established[asked[index]] = (bool(row.forward), bool(row.reverse))
    # A pair the job returned no row for stays OUT of `established` and answers `None`. The query
    # LEFT JOINs from the parameter, so it cannot happen — and if it ever does, "we could not
    # establish it" is the only safe reading of a missing row.
    with _connectivity_lock:
        for key, verdict in established.items():
            _connectivity_cache[key] = (now, verdict)
        if len(_connectivity_cache) > _CONNECTIVITY_CACHE_MAX:
            # Keep the newest, in one pass. Evicting one entry per insertion — which is what this
            # did while a job asked about one pair — is quadratic once a sweep inserts thousands.
            keep = sorted(
                _connectivity_cache.items(), key=lambda item: item[1][0], reverse=True
            )[:_CONNECTIVITY_CACHE_MAX]
            _connectivity_cache.clear()
            _connectivity_cache.update(keep)
    answers.update(established)
    _log_connectivity(asked, established, path=path)
    return [answers.get(key) for key in keys]


def _log_connectivity(
    asked: Sequence[tuple[tuple[str, ...], tuple[str, ...]]],
    established: Mapping[tuple[tuple[str, ...], tuple[str, ...]], tuple[bool, bool]],
    *,
    path: str,
) -> None:
    """What a reader gets out of a connectivity job, WITHOUT either stop ref.

    Until U10 this wrote both refs into the record's *message* on every established verdict, which
    is why the webhook channel's "no notification log line carries an origin or a destination" claim
    needed a qualifier: a webhook subscription's own stop pair reached Cloud Logging under
    `/varsel`'s event name. It is a registered traveller's origin and destination either way, and
    `alternatives.pair_digest` is the derivation this repo already uses for exactly this — salted per
    process, because an unsalted SHA-256 over two public enumerable refs is a reversible pseudonym.

    ONE record per pair while a job asks about one, which is the traveller's registration and the
    case a reader looks up by hand; ONE summary for a batch, because a sweep asking about thousands
    would otherwise be thousands of INFO lines a cycle — and each of those answers is stored on the
    record it is about, which is where a reader should look for it.

    The digest here is over the first EXPANDED ref of each side, while `varsel._service_verdict`'s
    own error path digests the caller's canonical refs — so one stop pair can carry two labels.
    Cosmetic by construction: `pair_digest` is documented as a label for a human reading logs and
    explicitly not a join key, and correlating within one record's own event name still works.
    """
    if len(asked) == 1:
        key = asked[0]
        verdict = established.get(key)
        if verdict is None:
            return
        logger.info(
            "Planned service for pair %s: forward=%s reverse=%s",
            alternatives.pair_digest(key[0][0] if key[0] else "?", key[1][0] if key[1] else "?"),
            verdict[0],
            verdict[1],
            extra={
                "fields": {
                    "event": "varsel_service_check",
                    "result": "ok",
                    "path": path,
                    "pair_digest": alternatives.pair_digest(
                        key[0][0] if key[0] else "?", key[1][0] if key[1] else "?"
                    ),
                    "forward": verdict[0],
                    "reverse": verdict[1],
                }
            },
        )
        return
    forward = sum(1 for verdict in established.values() if verdict[0])
    reverse_only = sum(1 for verdict in established.values() if not verdict[0] and verdict[1])
    logger.info(
        "Planned service established for %d of %d stop pair(s): %d served, %d reverse only",
        len(established),
        len(asked),
        forward,
        reverse_only,
        extra={
            "fields": {
                "event": "varsel_service_check",
                "result": "batch",
                "path": path,
                "asked": len(asked),
                "established": len(established),
                "forward": forward,
                "reverse_only": reverse_only,
            }
        },
    )


def _varsel_connectivity(
    from_refs: Sequence[str],
    to_refs: Sequence[str],
    *,
    path: str = _CONNECTIVITY_REQUEST_PATH,
) -> tuple[bool, bool] | None:
    """(runs from→to today, runs to→from today), or None when it could not be established.

    A batch of one, and deliberately not a second implementation: the caveat a traveller is shown at
    registration and the answer a sweep stores six hours later have to be the same question, and two
    queries that must agree are the arrangement this repo has already watched drift apart twice.
    """
    return _varsel_connectivity_batch([(from_refs, to_refs)], path=path)[0]



def _varsel_snapshot() -> dict[str, list]:
    with _call_pred_lock:
        return _call_pred_by_situation


def _impact_snapshot() -> tuple[dict[str, list[impact_engine.CallPred]], datetime | None]:
    """The per-call prediction snapshot, injected into the impact engine via configure().

    The engine owns no state; the poller writes this cache and this is the only reader it gets.
    """
    with _call_pred_lock:
        return _call_pred_by_situation, _call_pred_refreshed_at


def _impact_stop_ref_index() -> (
    tuple[dict[str, list[tuple[str, impact_engine.CallPred]]], datetime | None]
):
    """The stop-ref posting lists and the generation they were built for (U2).

    The stamp travels with the index because this is a SECOND acquisition of `_call_pred_lock`,
    taken after `_impact_snapshot`'s: a refresh landing between the two would otherwise hand the
    engine an index describing calls the snapshot it holds no longer has, which is not a stale
    answer but a wrong one. `impact._stop_ref_index_for` compares the two stamps and indexes the
    snapshot itself when they disagree.
    """
    with _call_pred_lock:
        return _call_pred_by_stop_ref, _call_pred_refreshed_at


def _varsel_refreshed_at() -> datetime | None:
    """When the per-call prediction cache was last loaded, or None before the poller's first load.

    The freshness provider for the journey-keyed public read on `/notification-api/v1`
    (2026-09-10): a cache nobody has loaded yet has no situations in it, and a read that answered
    "no disruptions" off it would be reporting an empty process as a calm day. The read answers 503
    while this is None. The second half of `_impact_snapshot`, under the same lock, so the answer
    can never describe a different load than the situations /impact matched from.
    """
    return _impact_snapshot()[1]


def _impact_journey_calls(operator: str | None, jkey: str) -> list[impact_engine.CallPred] | None:
    """The scored calls of ONE journey, or None when no live situation touches it.

    The second reader injected into the impact engine (beside `_impact_snapshot`), over the same
    poller-owned rows indexed the other way. It is how the engine asks what we know about a train
    the Journey Planner offered as an alternative — a question the by-situation snapshot can only
    answer by scanning it. Mirrors `_reisevalg_call_predictions`' resolution over this same index.

    **A codespace that misses is a miss, not an invitation to guess.** `_reisevalg_call_predictions`
    falls back to the bare-jkey index when the prefixed lookup finds nothing; that is safe there
    because its input is one journey ref the caller already believes in. Here the ref comes from the
    Journey Planner and always carries a codespace, so falling back would mean answering "VYG's
    ServiceJourney:123 is not in the cache" with ATB's — bus ServiceJourney refs are not
    date-scoped, and that collision is exactly what keying on (operator, jkey) exists to prevent.
    The bare-jkey path is therefore reached only when the ref carries no codespace at all, and even
    then only when a single operator claims it (the same rule /journey applies before its 409).
    """
    with _call_pred_lock:
        if operator is not None:
            rows = _call_pred_by_key.get((operator, jkey))
        else:
            ops = _call_pred_by_jkey_index.get(jkey, set())
            rows = _call_pred_by_key.get((next(iter(ops)), jkey)) if len(ops) == 1 else None
    return list(rows) if rows else None


def _varsel_impact(
    *,
    from_stop: str,
    to_stop: str,
    window_start: datetime,
    window_end: datetime,
    catchable_after: datetime | None = None,
    with_alternative: bool = False,
    min_impact: float = 0.0,
    path: alternatives.FetchPath = alternatives._POLLER_PATH,
) -> impact_engine.ImpactResponse:
    """/impact for a registered journey's boarding window, injected into varsel via configure().

    Structurally typed as `varsel.ImpactProvider` since 2026-07-29 — a signature drift here is now a
    type error rather than a runtime TypeError swallowed by check_and_notify's per-journey guard,
    which is how this contract broke once and took all push alerting with it while the suite stayed
    green. The regression test is test_the_real_provider_signature_matches.

    `with_alternative` defaults to False so the /varsel-api request path never makes a blocking
    Journey Planner call; the poller passes True.

    `select="worst"` rather than /impact's `nearest`: an alert asks "is any train in this window
    alert-worthy?", not "which train leaves closest to a given minute". Same matcher, same severity
    order and same response shaping as the HTTP endpoint — that is the point of routing varsel
    through here instead of letting it keep its own matcher.
    """
    return impact_engine.for_trip(
        from_stop=from_stop,
        to_stop=to_stop,
        window_start=window_start,
        window_end=window_end,
        select="worst",
        catchable_after=catchable_after,
        with_alternative=with_alternative,
        min_impact=min_impact,
        # Charge this provider's Journey Planner fetches to the CALLER's budget, defaulting to the
        # poller's reserved one and never the public one. Until 2026-09-10 this was a flat
        # "poller", exact while `check_and_notify` was the only caller passing
        # `with_alternative=True`; the journey-alert pull read now names its own path, and a
        # provider that swallowed it would spend the push cycle's share on every read. What the
        # default still protects: an unauthenticated caller flooding /impact cannot starve the
        # push cycle it shares this module-global cache with.
        path=path,
    )


def _reisevalg_call_predictions(journey_ref: str) -> list | None:
    """Per-call prediction rows for a full dated journey ref (e.g.
    'VYG:DatedServiceJourney:81613_DRM-DAL_26-07-13'), or None when no active disruption
    touches it. Mirrors /journey's codespace-prefix resolution over the (operator, jkey)
    cache; injected into reisevalg via configure() as a strictly read-only lookup (its
    snapshot veto can only demote a recommendation, never promote one)."""
    m = re.match(r"^([A-Za-z]+):", journey_ref)
    with _call_pred_lock:
        rows = _call_pred_by_key.get((m.group(1), journey_ref[m.end():])) if m else None
        if rows is None:
            ops = _call_pred_by_jkey_index.get(journey_ref, set())
            if len(ops) == 1:
                rows = _call_pred_by_key.get((next(iter(ops)), journey_ref))
    return list(rows) if rows else None


#: Where Linux publishes this process's resident set size, in pages, as the second field of a
#: one-line file. Read directly rather than through psutil, which is not a dependency of the image
#: and would be one more distribution in it for one integer. Absent everywhere but Linux — a
#: developer's macOS has no /proc — which is why every reader here tolerates ``None``.
_STATM = Path("/proc/self/statm")


def _rss_bytes() -> int | None:
    """This process's resident set size in bytes, or ``None`` where /proc does not exist.

    ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` is the portable alternative and is the wrong
    number: it is the process's high-WATER mark, so it never falls, and a per-phase delta computed
    from it reads 0 for every phase after the largest one — which is indistinguishable from a phase
    that allocated nothing, and is exactly the reading a capacity run is looking for.

    Returns ``None`` rather than 0 on failure for the same reason: a missing reading must be
    absent from the record, never a plausible-looking zero.
    """
    try:
        pages = int(_STATM.read_text().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return pages * os.sysconf("SC_PAGE_SIZE")


@contextmanager
def _phase(name: str, timings: dict[str, int]):
    """Time one step of the poller's rebuild, recording what it cost whether or not it succeeded.

    There was no instrumentation on this cycle at all until 2026-08-21, and answering "where does
    the cycle go?" meant subtracting the timestamps of unrelated INFO lines that happen to sit at
    step boundaries — which only works for the steps that log, and reports a step that logs nothing
    as free. That was survivable at 44 registered journeys, where the whole varsel path is ~1.5 s of
    a ~55 s cycle; it stops being survivable at ten times that, where the question becomes which of
    several growing steps to widen first, and the honest answer has to come from a measurement
    rather than from whichever step someone happens to suspect.

    **Timed in `finally`, and the exception is re-raised untouched.** A step that fails slowly is
    the single most interesting reading here — a Journey Planner outage, a BigQuery job that sits
    at its timeout — and a context manager that only recorded the happy path would go quiet exactly
    then. Nothing here swallows: every `try/except` around these steps already decides what a
    failure means for the rebuild, and this must not take that decision away from them, so it
    cannot be used to make a step tolerant that was not tolerant before.

    `timings` accumulates the whole cycle's breakdown for the one summary line at the end. Keys are
    stable and machine-readable (`jsonPayload.fields.phase`), so a per-step latency metric or an
    alert on one step's regression can be built from these without parsing prose. It stays a
    name -> **int ms** map and nothing else: `poller_cycle` publishes it verbatim as `phases`, and
    the two figures added below ride on the per-phase record instead.

    **`cpu_ms` and `rss_delta_bytes` beside `ms`** (R16). Wall clock cannot say whether a step is
    bound by one CPython thread or by a socket, and that is the only question a capacity decision
    turns on: pools and cores buy wall clock for waits and nothing at all for bytecode (KTD17). So
    the record carries process CPU for the step and how much resident memory it left behind.
    Both are whole-process, not per-thread: request-path work served during the phase counts, which
    is the honest reading — it is one interpreter — and a wide pool's CPU can legitimately exceed
    the step's wall clock. `rss_delta_bytes` is omitted rather than zeroed where /proc does not
    exist, so an absent reading cannot be mistaken for a phase that allocated nothing.
    """
    started = monotonic()
    started_cpu = time.process_time()
    rss_before = _rss_bytes()
    result = "ok"
    try:
        # A child span of the cycle root span via context (KTD4) — non-recording, and costless,
        # when tracing is off. Inside the try on purpose: the span records the exception, and the
        # except/finally below keep exactly the semantics documented above.
        with tracing_setup.span(name):
            yield
    except Exception:
        result = "error"
        raise
    finally:
        ms = round((monotonic() - started) * 1000)
        # `max(0.0, ...)` is not defensiveness about process_time, which is monotonic per process;
        # it is about the ROUNDING below and about any future baseline mistake reaching the sum of
        # a histogram, where a negative observation is accepted silently and shows up as nothing.
        cpu_s = max(0.0, time.process_time() - started_cpu)
        rss_after = _rss_bytes()
        timings[name] = ms
        fields = {
            "event": "poller_phase",
            "phase": name,
            "ms": ms,
            "cpu_ms": round(cpu_s * 1000),
            "result": result,
        }
        if rss_before is not None and rss_after is not None:
            fields["rss_delta_bytes"] = rss_after - rss_before
        logger.info(
            "Poller phase %s finished in %d ms (%d ms CPU, %s)",
            name,
            ms,
            fields["cpu_ms"],
            result,
            extra={"fields": fields},
        )
        # Beside the log line, never instead of it (the event is an operator contract). `phase` is
        # the small fixed vocabulary of step names above, so the label stays bounded.
        metrics.POLLER_PHASE_DURATION.labels(phase=name).observe(ms / 1000)
        metrics.POLLER_PHASE_CPU.labels(phase=name).observe(cpu_s)
        metrics.POLLER_PHASE_TOTAL.labels(phase=name, result=result).inc()
        # A finished step — successful or failed — is progress: the thread is moving, which is the
        # only thing /health's staleness branch asks about. In `finally` beside the rest, so a
        # step that fails after a long wait still proves liveness rather than counting toward a
        # wedge. See `_beat`.
        _beat()


def _on_ingestion_change(last_ingestion_time: datetime | None) -> None:
    """Default change handler — logs the new ingestion time."""
    logger.info("Last ingestion time changed: %s", last_ingestion_time)
    timings: dict[str, int] = {}
    cycle_started = monotonic()
    # The cycle is ONE root span with per-phase children (KTD4): each `_phase` opens a child via
    # context, and parent-based sampling then also governs the BigQuery client's automatic spans
    # inside the cycle. The `finally` sits INSIDE the span so the cycle-summary log line carries
    # the trace-correlation keys (R17). Non-recording, and costless, when tracing is off.
    with tracing_setup.span("poller_cycle"):
        try:
            _rebuild_all(timings)
            # EPOCH seconds, never `monotonic()` (KTD1's two-clock rule): the runbook's freshness
            # alerts compute `time() - value`, which a monotonic export silently breaks. This
            # gauge measures cache freshness — it moves only when SIRI-SX moves — not liveness;
            # `_beat` in poll_for_changes owns the liveness one.
            #
            # On the SUCCESS path only. In the `finally` beside the duration below, a rebuild that
            # died partway still advanced it: with `predict_sql` raising every cycle — it is one
            # of the four steps deliberately NOT wrapped in its own try/except, so it propagates —
            # every cache goes hours stale while the runbook's staleness rule stays green forever.
            # What the gauge can still not see is a step that IS isolated (varsel_notify,
            # predict_calls_*, …) failing on every cycle: the rebuild returns normally and this
            # runs. That is what `kix_poller_phase_total{result="error"}` is for, and the help
            # text says so rather than overclaiming.
            metrics.POLLER_LAST_CYCLE.set(time.time())
        finally:
            # In `finally` for the same reason each phase is: a rebuild that dies partway through
            # is when the breakdown is worth having, and `poll_for_changes` catches whatever comes
            # out of here and retries on the next tick, so this line is the only record that the
            # cycle ran at all. `phases` carries only the steps that were reached, which is itself
            # the diagnosis.
            total_ms = round((monotonic() - cycle_started) * 1000)
            logger.info(
                "Poller cycle finished in %.1f s: %s",
                total_ms / 1000,
                ", ".join(f"{name} {ms} ms" for name, ms in timings.items()),
                extra={
                    "fields": {
                        "event": "poller_cycle",
                        "ms": total_ms,
                        "phases": dict(timings),
                    }
                },
            )
            # In `finally` deliberately, unlike the freshness gauge above: a failed cycle's
            # duration is exactly the reading worth having (a step that died slowly).
            metrics.POLLER_CYCLE_DURATION.observe(total_ms / 1000)


def _rebuild_all(timings: dict[str, int]) -> None:
    """Every cache the poller rebuilds on an ingestion change, in order, each step timed.

    Split out of :func:`_on_ingestion_change` purely so the cycle-total logging can wrap it in a
    `try/finally` without indenting the whole body — the step order, the isolation boundaries and
    what each failure degrades are all exactly as they were.
    """
    # Actuals scan, gated on its own TTL — see maybe_refresh_rail_calls. The two queries below
    # read the table it builds and still run every tick; they just read actuals that are at most
    # _RAIL_CALLS_TTL old, which is what their onset + 12 h windows already tolerate.
    with _phase("rail_calls", timings):
        maybe_refresh_rail_calls()
    # The «I dag» strip's recorded calls (journey_status). Its own try: the step above is the one
    # whose failure aborts the rebuild, and a failed LOAD of a table it did build must not.
    try:
        with _phase("rail_actuals_cache", timings):
            refresh_rail_actuals()
    except Exception:
        logger.exception("Journey-status actuals load failed; serving the previous copy")
    with _phase("disruptions_sql", timings):
        bq_client.query(_DISRUPTIONS_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
    # AI.CLASSIFY (Gemini) categorisation of any *new* free text. Isolated so a failure here
    # never kills the rebuild: predict.sql LEFT JOINs disruptions.categories and falls back to
    # 'UNKNOWN' (with the global '__ALL__' quantile offsets) for unclassified text, so the feed
    # still serves — already-classified incidents keep their category; genuinely new text shows
    # UNKNOWN until classification succeeds. The runtime SA needs roles/aiplatform.user for this
    # step; until that grant lands the call 403s and we degrade gracefully rather than serving an
    # empty feed.
    try:
        with _phase("categories", timings):
            bq_client.query(_UPDATE_CATEGORIES_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
    except Exception:
        logger.exception(
            "Category classification (AI.CLASSIFY) failed; serving with existing categories "
            "(new text -> UNKNOWN). Check the SA has roles/aiplatform.user."
        )
    with _phase("predict_sql", timings):
        bq_client.query(_PREDICT_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
    with _phase("disruptions_cache", timings):
        refresh_disruptions_cache()
    # The impact lookup is a static, offline-built table — reloading it here just keeps it
    # current if it was rebuilt. Isolated so a missing/erroring lookup never breaks the feed.
    try:
        with _phase("impact_lookup", timings):
            refresh_impact_lookup()
    except Exception:
        logger.exception("Impact lookup refresh failed; serving disruptions without alert profiles")
    # Road-volume baselines for /reisevalg-api — same offline-built-lookup pattern as the impact
    # lookup above. Isolated so a missing/erroring table (it only exists once the reisevalg
    # backtest has run) never breaks the feed; the road side just reports unavailable.
    try:
        with _phase("svv_baselines", timings):
            reisevalg.refresh_baselines()
    except Exception:
        logger.exception("SVV baselines refresh failed; reisevalg road side may be unavailable")
    # Rail-stop picker list for the avviksvarsel PWA. Isolated so a failure never breaks the feed.
    #
    # ABOVE the per-call block, and that placement is load-bearing rather than tidy (2026-09-22).
    # `refresh_call_predictions_cache` below stamps `_call_pred_refreshed_at`, and since
    # `notification_wording.wording_key` carries that stamp, every cached wording is orphaned the
    # instant it moves -- so everything between the swap and `varsel_notify` writing the new ones is
    # a window where the in-app card and the journey-alert read serve the deterministic line instead
    # of the model's. This step was ~8 s of a ~20 s window (measured on prd, 12 cycles, 2026-09-22)
    # and it has no business being in it: it reads `{rail,bus}_calls_planned_today` straight from
    # BigQuery and neither reads the prediction snapshot nor is read by it.
    #
    # What it costs is that the picker list is built from the planned tables BEFORE this cycle's
    # `maybe_refresh_*_planned_calls` may rebuild them. Those run on a 60/120 min TTL and are a
    # no-op on most ticks, so the list is at most one cycle behind on the tick that does rebuild --
    # and the list is which stations exist, which is not what changes between two ticks.
    try:
        with _phase("varsel_stops", timings):
            refresh_varsel_stops()
    except Exception:
        logger.exception("Varsel stops refresh failed; /varsel-api/stops may be stale or empty")
    # Per-call (per-train) predictions for /journey. Isolated so any failure here never breaks the
    # main /disruptions feed. The planned call source is rebuilt on a TTL (it scans ET); the
    # scoring + cache reload run every tick (cheap: materialized inputs + ML.PREDICT).
    try:
        with _phase("rail_planned_calls", timings):
            maybe_refresh_planned_calls()
        # The «I dag» strip's timetable (journey_status). Its own try: a failed load keeps the
        # previous spine and must not cost this tick its predictions.
        try:
            with _phase("rail_planned_cache", timings):
                refresh_planned_spine()
        except Exception:
            logger.exception("Journey-status spine load failed; serving the previous copy")
        with _phase("predict_calls_rail", timings):
            bq_client.query(_PREDICT_CALLS_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
        # Bus/GOA (pooled_all) per-call predictions. Nested + isolated so any bus failure degrades
        # bus only — NSB /journey keeps serving from call_predictions_today. Builds today's bus SX
        # snapshot, refreshes the bus planned source on its own TTL, then scores.
        try:
            with _phase("disruptions_bus_sql", timings):
                bq_client.query(_DISRUPTIONS_BUS_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
            with _phase("bus_planned_calls", timings):
                maybe_refresh_bus_planned_calls()
            with _phase("predict_calls_bus", timings):
                bq_client.query(_PREDICT_CALLS_BUS_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S)
        except Exception:
            logger.exception(
                "Bus per-call predictions failed; /journey + /varsel serve NSB-only this tick"
            )
        with _phase("call_predictions_cache", timings):
            refresh_call_predictions_cache()  # loads whichever of the two tables are present
    except Exception:
        logger.exception("Per-call predictions refresh failed; /journey will serve stale or empty")
    # Journeys flagged "no direct service" at registration: ask again, and drop the warning from any
    # that a later timetable does connect (a weekend route registered on a Friday). Costs nothing
    # when nothing is flagged, and its own failure must not touch the alerting below.
    try:
        with _phase("varsel_recheck_service", timings):
            varsel.recheck_service()
    except Exception:
        logger.exception("Varsel service re-check failed; existing warnings stand")
    # Registered-journey alerts: match every registration -- browser and, where the webhook channel
    # is wired, webhook subscriptions merged in under the same journey_key -- against the fresh
    # snapshot and notify any newly alert-worthy hit. Isolated (like the other steps) so a delivery
    # failure never breaks the feed rebuild.
    #
    # It is NOT a no-op without VAPID, and the comment here said it was until 2026-09-08. Scoring
    # and decision recording have run regardless of the keypair since /historikk shipped, and since
    # KTD4 the wording pass does too wherever a recipient on some channel can receive it. What a
    # missing keypair still stops is browser delivery, and nothing else.
    # The notification store's mirrors, brought up to date BEFORE the pass reads them (U6, KTD5).
    #
    # **Its own phase, and ahead of `varsel_notify` rather than inside it.** The pass reads the
    # mirror from memory now, so a slow store no longer costs the pass its cycle -- it costs the
    # mirror its freshness, which is a different failure with a different response, and attributing
    # the two to one phase would hide exactly that. Ahead of the pass because a mirror synced after
    # it would serve every cycle the state of the one before.
    #
    # **On the poller thread, which is what "after the port binds" means.** The first call seeds
    # both mirrors by paging the collections, and at 100 000 subscriptions that is a long read: in
    # the lifespan it would hold the port closed for the whole of it and fail the readiness probe of
    # a deployment doing exactly what it was told to. `_beat` moves the poller's liveness clock
    # between pages, or `/health`'s "no step has finished in 600 s" restarts the pod mid-read,
    # forever.
    #
    # Guarded like every other step, and the failure is deliberately NOT fatal to the cycle: the
    # mirrors keep what they had, the pass matches against a slightly older population, and the
    # browser half is untouched. An empty mirror on a transient error would read to every
    # downstream reader as "every client unsubscribed".
    if webhook_channel := varsel.webhook_channel_enabled():
        try:
            with _phase("notification_mirror", timings):
                report = notification_store.sync_mirrors(beat=_beat)
                logger.info(
                    "Subscription mirror: %d held, %d synced, %d removed over %d page(s)%s",
                    report.size,
                    report.synced,
                    report.removed,
                    report.pages,
                    " (re-seeded)" if report.reseeded else "",
                    extra={
                        "fields": {
                            "event": "notification_store_load",
                            "result": "mirror_reseed" if report.reseeded else "mirror_sync",
                            "mirror_size": report.size,
                            "synced": report.synced,
                            "removed": report.removed,
                            "pages": report.pages,
                            "truncated": report.truncated,
                            "watermark_age_s": report.watermark_age_s,
                            # Documents the read returned that this process had already superseded
                            # with its own commit (U13). Expect zeros on a webhook-only deployment
                            # and small numbers once `/varsel` registrations are in the store.
                            "overtaken": report.overtaken,
                        }
                    },
                )
                # The series an alert rule selects on. Set from the report rather than re-derived,
                # so the gauge and the log record cannot disagree about the same cycle.
                if report.watermark_age_s is not None:
                    metrics.MIRROR_AGE.set(report.watermark_age_s)
        except Exception:
            logger.exception(
                "The notification store's mirrors could not be synced; this cycle matches the "
                "population from the last successful sync"
            )
    outcome: varsel.PassOutcome | None = None
    try:
        with _phase("varsel_notify", timings):
            outcome = varsel.check_and_notify(_varsel_snapshot())
    except Exception:
        logger.exception("Avviksvarsel notify failed")
    # The client (webhook) channel's two WRITING steps, in their own phase and deliberately after
    # the one above (KTD6, plus the orchestrator's resolution of where the phase reading goes).
    #
    # Three things this placement buys, and each of them was a choice.
    #
    # It runs after `check_and_notify` has RETURNED, so it is strictly later than KTD6's literal
    # "after `_record_decisions` and `_save_state`" — a raise here cannot unwind the decision log
    # write or the state save at all, rather than merely being sequenced after them. Same thread,
    # same cycle, same process, so nothing KTD6 settled with the user (decide and deliver in the
    # pass, not in a separate Kubernetes worker) is touched.
    #
    # It is a `_phase`, so the Verification Contract's cycle-budget row has a real `poller_phase`
    # reading — which is also the number KTD6's own reversal trigger is stated in ("move delivery
    # out when the webhook phase exceeds 30 s of measured phase time on any cycle in a week"). The
    # two steps' own ceilings add to 28 s, so that trigger means what it says.
    #
    # And it is fed by a RETURN VALUE rather than a module-level snapshot in `varsel`: a new
    # module-level ledger there would need registering in `check_and_notify`'s per-cycle stale sweep
    # and in `test_varsel_api._reset_state`, and a leaked half-cycle snapshot reads exactly like the
    # give-up bug those sweeps exist to prevent.
    #
    # Guarded like every other step here, and the two halves are guarded separately (R20): an emit
    # failure must not cost the deliveries already committed. Delivery runs even when the pass above
    # raised and `outcome` is None — the pending set is Firestore's, not this cycle's.
    # `webhook_channel` was read ONCE for the cycle, above the mirror sync, and is used by all
    # THREE notification steps. Two reads cannot disagree in practice -- the flag is an environment
    # variable and a container does not change one while it runs -- but `webhook_channel_enabled`
    # catches and logs a raise from the injected reader, so a second read wrote the same warning
    # with the same traceback twice on every cycle.
    if webhook_channel:
        with _phase("notification_webhooks", timings):
            try:
                if outcome is not None:
                    # The per-client pending ceiling, resolved HERE rather than in the emit module:
                    # `notification_events` pins its own import set exactly, and the answer lives in
                    # `notification_auth`. This module already imports both.
                    report = notification_events.emit(
                        outcome.webhook_observations,
                        pending_quota=lambda client_id: notification_auth.quotas_for(
                            client_id
                        ).max_pending_events,
                        # The same seam `varsel.configure(beat=...)` takes, and for the same
                        # reason one step further along: since U9 this pass may run for 80 s, and
                        # `/health`'s staleness rule means "no STEP has finished in 600 s" — so a
                        # phase that merely got slow must keep saying so while it works.
                        beat=_beat,
                    )
                    # U16: the decision log's `pushed` means "a notification was issued on any
                    # channel" (KTD4), and for the client channel that is the MINT the pass above
                    # just committed. It has to be written here rather than inside
                    # `check_and_notify`, because emit deliberately runs after that function has
                    # returned — see `record_minted_decisions`, which carries the whole argument.
                    #
                    # Its own `try`, and not folded into the emit call's: an event that was
                    # committed must not be un-notified by a bookkeeping row, and — the direction
                    # that actually bites — a decision-log problem must not be reported as
                    # "Notification event emission failed" on a cycle where every event was
                    # emitted. The function swallows too; this is the second net.
                    try:
                        varsel.record_minted_decisions(outcome, report.minted_decisions)
                    except Exception:
                        logger.exception("Recording this cycle's mint marks failed")
            except Exception:
                logger.exception("Notification event emission failed")
            # U11: delivery moves to `webhook_worker`, its own Deployment from this same image, and
            # this flag is how a deployment says so. Read INSIDE the phase rather than beside the
            # `webhook_channel` check above, because the two answer different questions and folding
            # them would make "the channel is on" imply "this process delivers" — which is exactly
            # what stops being true here. The phase stays, carrying the emit pass; its `ms` drops by
            # the delivery ceiling, which is what a reader comparing cycles across the flag will see.
            if not delivery_worker_enabled():
                try:
                    webhook_delivery.deliver_pending()
                except Exception:
                    logger.exception("Webhook delivery failed")
    # R6's freshness half, and AE13's second clause: webhook subscriptions carrying a stale "no
    # direct service" caveat -- or one that was never established -- are re-asked, and the caveat is
    # dropped from any pair a later timetable does connect. `varsel.recheck_service` above does
    # exactly this for browser registrations and cannot reach these: it walks `varsel._journeys`,
    # which is browser-only by design. Without it a weekend-only or seasonal pair carries its caveat
    # for the life of the subscription, which is the refusal R5 forbids with extra steps.
    #
    # ITS OWN PHASE, and not folded into either neighbour. Not into `varsel_recheck_service`,
    # because a Firestore read and a client write inside the browser phase would blur both the
    # timing attribution and KTD6's guarantee that client work runs after the browser cycle's
    # durable writes. And not into `notification_webhooks`, for a sharper reason: that phase's measured
    # `ms` is the number KTD6's reversal trigger is stated in ("move delivery out when the webhook
    # phase exceeds 30 s of measured phase time"), its two steps reserve 28 s of ceiling against it,
    # and adding this step's own reservation would make that trigger fire on a healthy cycle.
    #
    # What that reservation is, stated as the number it actually is:
    # `notification_servability._DEADLINE_S` (30 s) plus one `set_servability`, because the budget
    # reserves the BigQuery question and not the store write that follows it. The module's own
    # `_DEADLINE_S` comment carries why the residual is left rather than reserved. Typical is ~0:
    # nothing is due on a healthy list, and a due one costs ~2 s.
    #
    # Gated on the same `webhook_channel_enabled()` as the phase above, deliberately, even though
    # `/notification-api/v1` mounts unconditionally and serves the caveat whether or not the channel
    # is on. Gating it keeps "the flag is unset, so no notification state was read" a property of the
    # poller; ungated, every deployment that has onboarded nobody -- and tst, which has no named
    # database -- would read Firestore and ask BigQuery on every cycle for a population that is
    # empty. What a client sees in the onboarding window is the create-time answer with
    # `servable_checked_at` publishing its age, which is R6's "how fresh" rather than an all-clear.
    if webhook_channel:
        with _phase("notification_servability", timings):
            try:
                notification_servability.recheck()
            except Exception:
                logger.exception("Client servability re-check failed; existing caveats stand")

    # LAST, and only under the store backend: the rollback artefact (U14). Writing the store back
    # into the blob is what makes an erasure reach it -- the import creates and never deletes, so a
    # rollback to `gcs` without this resurrects a traveller who pressed «Slett alt». The interval
    # is the rollback loss window and the operations document states it as one.
    #
    # On the poller's tick rather than as a CronJob from the chart, which the plan offers as the
    # alternative: a second workload would need the bucket name and the database resolved again,
    # per environment, from values this process already has -- and the first thing to drift would
    # be the bucket, whose name differs between dev and prd.
    #
    # After everything else, because it is the one step of the cycle nobody is waiting for: a slow
    # or failing export costs the artefact its freshness and the cycle nothing.
    if _varsel_store is not None:
        with _phase("varsel_state_export", timings):
            _export_varsel_state(_varsel_store)


#: How often the rollback artefact is rewritten, and therefore the worst-case window in which it
#: still holds a registration a traveller has erased. Six hours matches the servability sweep's
#: cadence and is well inside the documented "within 24 hours" that the deletion contract already
#: promises for cleanup.
_VARSEL_EXPORT_INTERVAL_S = 6 * 3600
_varsel_exported_at: float = 0.0


def _export_varsel_state(store: varsel_state.StoreRegistrations) -> None:
    """Rewrite the state blob from the store, at most every `_VARSEL_EXPORT_INTERVAL_S`.

    Never raises into the cycle: an export that fails leaves the previous artefact in place, which
    is stale rather than absent, and the next tick tries again. The log line is here rather than in
    `varsel_state` for that module's own reason -- it emits no structured event, so which vocabulary
    a reader greps for stays a property of the caller.
    """
    global _varsel_exported_at
    now = time.monotonic()
    if _varsel_exported_at and now - _varsel_exported_at < _VARSEL_EXPORT_INTERVAL_S:
        return
    try:
        report = varsel_state.export_store_to_blob(store, beat=_beat)
    except Exception:
        logger.exception("Could not export varsel state; the previous rollback artefact stands")
        return
    _varsel_exported_at = now
    logger.info(
        "Exported varsel state to the rollback artefact: %d devices, %d journeys, %d marks",
        report.devices,
        report.journeys,
        report.marks,
        extra={"fields": {"event": "varsel_state_export", **report.as_fields()}},
    )


def _togvarsel_notify_enabled() -> bool:
    """The togvarsel push delivery gate, read per call so a check always reflects the environment.

    TWO flags, ANDed, and the order of the words matters: deployment.show_rushvarsel() is the
    master gate over the whole product (SHOW_RUSHVARSEL, unset = off), and TOGVARSEL_NOTIFY_ENABLED
    stays what it always was — the per-environment delivery switch, with dev's and prd's "true"
    left in place. So while the product is dark this reads False everywhere, and turning it back on
    is that one value per environment rather than a set of flags to re-derive.

    The AND is deliberately concentrated here rather than spread over three call sites, because
    this one function is the seam for all THREE ways rushvarsel reaches a traveller:
    togvarsel_notify.evaluate()'s `enabled` gate (no push, and no marker written),
    /varsel-api/config's `togvarsel_notify` (which hides the PWA's «Nytt: Rushvarsel» notice) and
    rush_outlook()'s own gate (which empties the outlook panel at the top of /varsel). The page
    404s are the one thing NOT here — a static mount and a router cannot read a provider.

    Allow-list parse on both, the delivery-flag convention VARSEL_LLM_ENABLED established
    (llm_message.py explains why the failure direction must be OFF) — but with UNSET parsing as
    off, deliberately unlike that flag's default-"true" read: the code default matches the closed
    gate instead of relying on helm values alone. While this is off, togvarsel_notify.evaluate()
    writes no markers, so an enable-restart inside a timeliness window still delivers that round —
    a property that carries over to the master gate unchanged.
    """
    if not deployment.show_rushvarsel():
        return False
    return os.environ.get("TOGVARSEL_NOTIFY_ENABLED", "").strip().lower() in llm_message._TRUTHY


def _notification_webhooks_enabled() -> bool:
    """The client (webhook) notification channel's gate, read per call.

    ONE flag, deliberately — the convention above and not its shape. `_togvarsel_notify_enabled`
    ANDs two because rushvarsel has a master product gate over three separate surfaces; the client
    channel has exactly one surface and one question, so a second flag would be a value nobody
    varies and one more thing to re-derive when the channel is turned on.

    What it keeps from that function is everything that matters: an allow-list parse (so a typo is
    off rather than on), a call-time environment read (ci.yml:68-73 records what an import-time read
    costs), and **unset = off**, so the code default matches the closed gate instead of relying on
    helm values alone. It is injected into `varsel.configure` rather than read there, so the one
    function is the seam — and `varsel` declines to touch the store at all while this reads False,
    which is what makes "the flag is unset, so no notification state was read" a property of the pass.
    """
    return os.environ.get("NOTIFICATION_WEBHOOKS_ENABLED", "").strip().lower() in llm_message._TRUTHY


#: What `VARSEL_STATE_BACKEND` may say. `gcs` is the GCS state blob `/varsel` has always had and is
#: the DEFAULT, so an unset variable is byte-for-byte the deployment that ran before the seam
#: existed; `store` is U12's Firestore collections. Spelled out rather than treated as a boolean
#: because the value is what an operator types under pressure, and "which store?" wants a name.
_VARSEL_STATE_BACKENDS = ("gcs", "store")


#: The store backend when this deployment uses one, else None -- which is also what tells the
#: poller whether there is a rollback artefact to write. Set by :func:`_registration_store`.
_varsel_store: varsel_state.StoreRegistrations | None = None


def _registration_store() -> varsel_state.RegistrationStore:
    """Where `/varsel` registrations live this deployment, from `VARSEL_STATE_BACKEND`.

    **Fail loud at startup, never at runtime**, which is `webhook_worker`'s rule and is the whole
    reason this is resolved in the lifespan rather than per call. Asking for `store` without the
    Firestore database, or without the reserved `browser` client's account, is a deployment that
    would answer every registration with a 500 and every cycle with an empty population -- the
    failure that looks exactly like "nobody has registered anything". Raising here fails the
    lifespan, which fails the readiness probe, which rolls the release back.

    The `browser` account is required rather than defaulted for one specific number:
    `notification_auth.quotas_for` answers an unknown client with `DEFAULT_QUOTAS`, whose
    `max_active_subscriptions` is 500. A deployment that took that default would refuse the 501st
    traveller with "Tjenesten er full" and nothing would say why. So the account has to exist, and
    creating it -- with the two quotas that were `varsel._MAX_DEVICES` and `varsel._MAX_JOURNEYS` --
    is the cutover's own runbook step.

    The quotas are resolved PER WRITE rather than captured here, so an operator's `set-quotas` takes
    effect without a restart; the lookup is the cached account read every request already pays for.
    """
    backend = os.environ.get("VARSEL_STATE_BACKEND", "").strip().lower() or "gcs"
    if backend not in _VARSEL_STATE_BACKENDS:
        raise RuntimeError(
            f"VARSEL_STATE_BACKEND is {backend!r}; it must be one of {_VARSEL_STATE_BACKENDS}"
        )
    # The togvarsel round markers follow the registrations rather than choosing a home of their
    # own: a rollback to the blob-reading binary has to find them where that binary looks. Set
    # before either branch returns, so moving the backend moves them with it.
    varsel_state.configure_rounds(in_store=backend == "store")
    if backend == "gcs":
        return varsel_state.BlobRegistrations(
            max_devices=varsel._MAX_DEVICES, max_journeys=varsel._MAX_JOURNEYS
        )
    if not notification_store.enabled():
        raise RuntimeError(
            "VARSEL_STATE_BACKEND=store needs the notification store: set "
            "NOTIFICATION_FIRESTORE_DATABASE"
        )
    account = notification_auth.get_account(varsel_state.BROWSER_CLIENT_ID)
    if account is None or not account.active:
        raise RuntimeError(
            f"VARSEL_STATE_BACKEND=store needs an active {varsel_state.BROWSER_CLIENT_ID!r} client "
            "account carrying the two /varsel ceilings as its quotas"
        )
    logger.info(
        "/varsel registrations are served from the notification store (client %s)",
        varsel_state.BROWSER_CLIENT_ID,
        extra={
            "fields": {
                "event": "varsel_state_backend",
                "result": "store",
                "client_id": varsel_state.BROWSER_CLIENT_ID,
            }
        },
    )
    global _varsel_store
    store = varsel_state.StoreRegistrations(
        client_id=varsel_state.BROWSER_CLIENT_ID,
        quotas=lambda: (
            notification_auth.quotas_for(varsel_state.BROWSER_CLIENT_ID).max_devices,
            notification_auth.quotas_for(
                varsel_state.BROWSER_CLIENT_ID
            ).max_active_subscriptions,
        ),
    )
    _import_varsel_state(store)
    # Kept so the poller can write the rollback artefact from it. A module global rather than a
    # second `_registration_store()` call, which would re-run the import's marker read every cycle.
    _varsel_store = store
    return store


def _import_varsel_state(store: varsel_state.StoreRegistrations) -> None:
    """Run U14's cutover import, here in the lifespan and before the port binds (R15).

    Before the port binds, so `/varsel` never answers a request against a half-imported store --
    a traveller whose journey list came back empty would delete nothing and register again, and
    the store would then hold both. Under the Recreate strategy the blob's writer has already
    stopped, so what the import reads cannot move underneath it.

    The LOG LINE is here rather than in the import, and that is `varsel_state`'s own rule: the seam
    emits no structured event, so which vocabulary a reader greps for stays a property of the
    caller whichever backend answered. A failure is re-raised: it fails the lifespan, which fails
    the readiness probe, which rolls the release back to the binary still reading the blob -- which
    is intact, because the import creates and never deletes.
    """
    report = varsel_state.import_blob_into_store(store)
    logger.info(
        "varsel state import: %s (%d devices, %d journeys, %d marks in %.1fs)",
        report.result,
        report.devices + report.devices_synthesised,
        report.journeys,
        report.marks,
        report.seconds,
        extra={"fields": {"event": "varsel_state_import", **report.as_fields()}},
    )


def delivery_worker_enabled() -> bool:
    """Is webhook delivery somebody else's job? From NOTIFICATION_DELIVERY_WORKER_ENABLED (U11).

    The same allow-list convention as every other flag here — only true/1/yes/on enable, and the
    code default for an UNSET value is off — but read in the OPPOSITE direction: on means this
    process does LESS. `webhook_worker` is its own Deployment from the same image and delivers
    unconditionally, so the only thing that has to move is whether the poller also does.

    **Off is the safe default and that is the whole point of the flag's polarity.** A deployment
    with no worker keeps delivering exactly as it did; a worker release that fails to become Ready
    leaves the poller still delivering, because turning this on is a separate change made after the
    worker is up. The window where both deliver is deliberate rather than tolerated: two readers
    each apply the per-destination ceiling to their own read, so a destination can briefly see twice
    its `deliveries_per_minute`, and U8's lease is what stops a traveller being notified twice.

    Emit and servability are NOT gated by it. Emit is KTD6's one writer of a subject's phase and
    revision, which stays in this process; servability is a BigQuery question this process already
    has the client for. What moves is the POST and the terminal write beside it.
    """
    return (
        os.environ.get("NOTIFICATION_DELIVERY_WORKER_ENABLED", "").strip().lower()
        in llm_message._TRUTHY
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Metrics exporter FIRST, before any BigQuery load (KTD2 in the observability plan): the
    # platform's annotation scraper targets the pod IP before the pod is Ready, so METRICS_PORT is
    # the only window into the up-to-300 s startup below. A dedicated internal port, never the
    # public 8080. configure() is idempotent and must be re-run if logging_setup.setup() ever
    # re-runs (setup() wipes root handlers, the level-counting handler with them). A bind failure
    # is logged and tolerated: losing the exporter must never take the serving app down with it.
    metrics.configure()
    try:
        metrics.start(int(os.environ.get("METRICS_PORT", "9464")))
    except Exception:
        logger.exception("Prometheus exporter failed to start; metrics unavailable this process")
    # Eager initial load so /alert_profile works before the first ingestion change fires
    # the poller's rebuild. Tolerate failure — the poller retries on every tick.
    try:
        refresh_impact_lookup()
    except Exception:
        logger.exception("Initial impact lookup load failed; will retry on ingestion poll")
    # Avviksvarsel: inject providers, load persisted subscriptions/journeys, and eager-load the
    # stop picker so /varsel is usable before the first ingestion tick. All tolerant of failure.
    impact_engine.configure(
        snapshot_provider=_impact_snapshot,
        call_lookup=_impact_journey_calls,
        # The inverted index (U2): the matcher's scan is bounded by the stop pair it was asked
        # about instead of by the size of the cache, so the cycle's cost scales with distinct place
        # pairs rather than with registrations.
        stop_ref_index=_impact_stop_ref_index,
    )
    # BEFORE `varsel.configure`, whose `registrations=` argument resolves the backend and, under
    # `VARSEL_STATE_BACKEND=store`, immediately reads this store: the `browser` account, and then
    # U14's cutover import. `notification_store.enabled()` is `a client was injected, or a database
    # id was named`, and the database id is named HERE -- so with the block below `varsel.configure`
    # the store backend refused to start with "set NOTIFICATION_FIRESTORE_DATABASE", naming the one
    # variable the deployment had plainly set. It would have failed the lifespan, and therefore the
    # release, on the cutover deploy and nowhere before it.
    notification_store.configure(
        database=os.environ.get("NOTIFICATION_FIRESTORE_DATABASE", "").strip() or None,
        # One span per Firestore call, carrying its own interpreter cost (KTD2 sizes the widened
        # emit pass in interpreter ms per transaction). Injected for `varsel`'s reason: the store
        # must import neither this module nor `tracing_setup`.
        span=tracing_setup.span,
    )
    notification_auth.configure()

    varsel.configure(
        impact_provider=_varsel_impact,
        stops_provider=_varsel_stops,
        # Where a `/varsel` registration lives (U13), from `VARSEL_STATE_BACKEND`. Unset is `gcs`,
        # which is the five dicts and the state blob `/varsel` has always had -- so an untouched
        # deployment is byte-for-byte what it was. `store` is U12's Firestore collections, and
        # `_registration_store` refuses to start without what that needs.
        registrations=_registration_store(),
        connectivity_provider=_varsel_connectivity,
        # The same question about many stop pairs at once, which is what BOTH servability sweeps
        # run on: one BigQuery job answers every pending pair (U10), where one job per pair bounded
        # the client sweep to four records a cycle and the browser one to nothing at all. The
        # poller budget is bound HERE rather than passed per call, because both callers are poller
        # sweeps and the tag is a vocabulary `varsel` does not own.
        connectivity_batch_provider=functools.partial(
            _varsel_connectivity_batch, path=_CONNECTIVITY_POLLER_PATH
        ),
        # /config tells the PWA whether rush-forecast pushes are delivered HERE, so the page's
        # notice can never promise a push class this environment's gate withholds.
        togvarsel_notify_provider=_togvarsel_notify_enabled,
        # The webhook channel's two halves, both now wired. `webhook_channel_enabled()` requires
        # BOTH, so a deployment missing either — every deployment until the flag is set, and tst,
        # which has no named database — is byte for byte the pass that ran before the channel
        # existed. The flag is injected separately from the store so `varsel` can decline to touch
        # the store at all, which is what makes "the flag is unset, so no notification state was read" a
        # property of the pass rather than of whoever wrote the provider.
        #
        # `recipient_documents` returns RAW documents on purpose (R20): only the reader can decide
        # what "unreadable" means for the schema the running binary knows, so the store hands over
        # the shape and `varsel._parse_webhook_recipient` validates it one document at a time. What
        # bounds the poller's WAIT on it is `varsel._WEBHOOK_LOAD_TIMEOUT_S` (5 s, on a worker
        # thread that is abandoned rather than joined); the store call itself carries no deadline of
        # its own, so a hung read costs a leaked thread and a delayed pod termination.
        webhook_enabled=_notification_webhooks_enabled,
        webhook_recipients_provider=notification_store.recipient_documents,
        # The tracing seam (R16). `varsel` opens one span per sub-pass through this callable and
        # imports neither this module nor `tracing_setup`: without it `varsel_notify` shows in a
        # trace as one opaque phase with the decision-log insert under it and nothing else, so
        # "which of the five passes is the cycle in?" has no answer a capacity run can read.
        span=tracing_setup.span,
        # The liveness seam (U2). This whole pass is ONE `_phase`, so without it the pass beats
        # exactly once — at its end — and `/health`'s "no STEP has finished in 600 s" rule cannot
        # tell a pass that got slow from a poller that is wedged. Injected as a callable for the
        # same reason as `span`: `varsel` imports neither this module nor the tracing wiring.
        beat=_beat,
        # The journey-alerts read's warm seam (U15), the one wire that runs from the pass INTO the
        # read. Pass 1a publishes its own match as this cycle's figures for every registered
        # definition, so a favourite that is also a registration is a figures hit for whoever asks,
        # for the whole cycle, at no matcher cost. Wired from here because this module is the only
        # one allowed to know both: `notification_pull` imports `varsel`, so `varsel` may not
        # import it back. Order does not matter -- `warm_figures` reads the generation through the
        # provider below at CALL time, and the first pass cannot run before the lifespan yields.
        figures_warmer=notification_pull.warm_figures,
    )
    # The journey-keyed public read's one seam (2026-09-10): when the per-call prediction cache
    # was loaded, so it can answer 503 rather than "no disruptions" before the poller's first load.
    # Its stop cache and matcher come through `varsel`'s own providers, wired just above.
    notification_pull.configure(refreshed_at=_varsel_refreshed_at)
    # The «I dag» strip's engine: the prediction stamp for `as_of`, and every ref of a place, which
    # it may not import from `varsel` itself (it is a leaf both surfaces call).
    journey_status.configure(
        predictions_refreshed_at=_varsel_refreshed_at, place_refs=varsel.place_refs
    )
    llm_message.configure(stop_name_provider=_varsel_stops)
    # The webhook channel's two Firestore-backed planes and the authenticated API over
    # them. All three are wired UNCONDITIONALLY, and the flag above is deliberately not consulted
    # here: `NOTIFICATION_WEBHOOKS_ENABLED` gates the notification PASS -- whether the poller reads
    # webhook subscriptions and delivers events -- while provisioning a client and letting it
    # register watches is the step that comes first. Onboarding is otherwise circular: nothing can
    # be registered until the pass is on, and turning the pass on with nothing registered is what
    # a rollback is supposed to look like.
    #
    # What actually makes the API answer is store AND pepper, read at call time
    # (`notification_auth.available()`): a deployment with no named database or no `NOTIFICATION_KEY_PEPPER`
    # answers 503 to every route rather than 401, because a 401 would tell a client their
    # credential is wrong when it is this deployment that is unconfigured. tst has neither today,
    # so it serves 503 -- honest, and cheap: nothing here builds a Firestore client until a request
    # arrives, so an unconfigured environment pays no import and needs no credential.
    #
    # ONE client for the whole channel: `notification_store.configure` -- above, beside the
    # registration backend it has to precede -- is the only place that names the database, and
    # `notification_auth` reads through `notification_store.firestore_client()`, so there is one
    # gRPC channel and one connection pool in a process whose poller already shares the thread.
    notification_api.configure(
        store=notification_store,
        auth=notification_auth,
        # /varsel's own, injected rather than imported by the module, so a client ref the stop
        # registry has collapsed lands on the same journey key a browser registration would. The
        # connectivity seam that sat beside it is gone (U10): a create and a replace no longer ask
        # BigQuery at all, and `servable` is answered by the poller's sweep.
        canonicalize=varsel.canonical_stop,
    )
    # The poller-side half of the same question (R6, AE13's second clause). The BATCH form of the
    # seam above since U10, and `varsel._service_verdicts` reaches the same
    # `_varsel_connectivity_batch` a create-time check does -- a subscription's caveat and its
    # re-check have to be the answer one function gives, or the create-time verdict and the refresh
    # could disagree about a pair while both looked right. Unwired, `notification_servability.recheck()`
    # returns before it reads the store, so nothing here is a cost in a deployment that never sets
    # the seam.
    notification_servability.configure(verdicts=varsel._service_verdicts)
    # The alert-decision log. ensure_table() is a CREATE TABLE IF NOT EXISTS, run here rather than
    # by a provisioning script because prd is an isolated project fed by free copies that nobody
    # runs build scripts against — without this the table would simply be missing there. Its own
    # failure path is internal (logged, never raised), so a BigQuery problem cannot block startup.
    decisions.configure(bq_client=bq_client)
    decisions.ensure_table()
    feedback_contexts.configure(bq_client=bq_client)
    feedback_contexts.ensure_table()
    historikk.configure(bq_client=bq_client)
    rushhistorikk.configure(bq_client=bq_client)
    # /meldinger reads the same decision log and replays each row's stored message_input through
    # today's prompt. The template writer is passed in rather than imported by the module —
    # varsel.py is 147 KB of poller state, and _deterministic_body is a pure function of the
    # stored input.
    meldinger.configure(
        bq_client=bq_client, deterministic_writer=varsel._deterministic_body
    )
    varsel.load_state()
    try:
        refresh_varsel_stops()
    except Exception:
        logger.exception("Initial varsel stops load failed; will retry on ingestion poll")
    # Reisevalg: inject the read-only per-call snapshot lookup + BigQuery client, then eager-load
    # the road-volume baselines so the volume provider can serve before the first ingestion tick.
    # Tolerant of failure (and of the table not existing yet) — the poller retries on every tick.
    reisevalg.configure(snapshot_lookup=_reisevalg_call_predictions, bq_client=bq_client)
    try:
        reisevalg.refresh_baselines()
    except Exception:
        logger.exception("Initial svv baselines load failed; will retry on ingestion poll")
    # Togmeldingen: BigQuery only (live feature rebuild + ML.PREDICT), driven wall-clock from
    # every poll tick (see poll_for_changes). No eager load — the first tick past an issue time
    # computes it, and a restart back-fills the day's earlier issues as `late` (the features are
    # clock-cutoff-defined, see togvarsel.py).
    togvarsel.configure(bq_client=bq_client)
    # Togvarsel push rounds (togvarsel_notify): the forecast above delivered as /varsel Web
    # Pushes. Wired the same way (the module imports neither varsel nor togvarsel, so every seam
    # is injected): the two gates, togvarsel's issued snapshot, varsel's journey/device/delivery
    # machinery, and the round-marker accessors in the persisted varsel state. Driven from the
    # same wall-clock tick as togvarsel.refresh (see poll_for_changes), never the SX rebuild.
    togvarsel_notify.configure(
        bq_client=bq_client,
        enabled=_togvarsel_notify_enabled,
        push_ready=varsel.push_enabled,
        issued_today=togvarsel.issued_today,
        journeys=varsel.journeys_snapshot,
        expand_refs=varsel.place_refs,
        resolve_device=varsel.device_subscription,
        deliver=varsel.deliver_to_devices,
        handle_gone=varsel.handle_gone_device,
        brand_assets=varsel._BRAND_ASSETS,
        tag_title=varsel._tag_environment,
        load_markers=varsel.togvarsel_rounds_for,
        store_markers=varsel.store_togvarsel_rounds,
    )
    # The round log /rushhistorikk scores, created here rather than on first write: evaluate()
    # returns before deciding anything while TOGVARSEL_NOTIFY_ENABLED is off — permanently true in
    # tst — so a lazily created table would never exist there and /rushhistorikk-api would 503 the
    # whole page in the one environment that never notifies. Same reasoning as decisions.py above.
    togvarsel_notify.ensure_table()
    # The realized rush outcome /rushhistorikk scores against. Created at startup for the same
    # reason as the round log: the page LEFT JOINs it on every request, so a missing table 503s the
    # whole surface until the first rush resolves — which is every environment on the day it ships.
    togvarsel_outcomes.configure(bq_client=bq_client)
    togvarsel_outcomes.ensure_table()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=poll_for_changes,
        args=(_on_ingestion_change, stop_event),
        daemon=True,
    )
    thread.start()
    # The handle /health reads to tell a dead poller from one never started. Stored after
    # start() (a stored-but-unstarted handle would read as dead), and no request can race the
    # store anyway: the app serves nothing until this lifespan yields.
    global _poller_thread
    _poller_thread = thread
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=5)
        # The pull read's enrichment pool: stop it without waiting, so a task parked on a Vertex or
        # Journey Planner call cannot hold the process open past the poller's own five seconds.
        notification_pull.shutdown()



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

# Public API — allow any origin so it can be consumed from browsers. The disruptions feed is
# read-only (GET); the avviksvarsel endpoints (/varsel-api) additionally accept POST/DELETE to
# register a device's push subscription and journeys, keyed only by a client-generated device_id
# (prototype, no auth — see demo/avviksvarsel/README.md).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Entur-SSO gate (sso.py): OIDC against Auth0's partner tenant, Entra-federated, gated on the
# organisation claim — in front of the internal surfaces, /historikk, /meldinger, /rushhistorikk
# and /demo, pages and APIs both (sso.PROTECTED_PREFIXES is the source of truth). Everything else
# stays public. Off unless SSO_REQUIRED is set (every environment sets it since 2026-09-10); when
# required but unconfigured the gated paths fail CLOSED (503), never open. Added after
# CORSMiddleware, so it runs OUTSIDE it; it exempts OPTIONS itself so a preflight still gets its
# CORS answer.
app.add_middleware(sso.RequireSSO)

# HTTP request metrics (metrics.py): counts and times every request by method, first path segment
# and status class. Added after RequireSSO — Starlette runs the last-added middleware outermost —
# so a request the SSO gate rejects (302/401/503) is still counted. The tracing middleware is
# added directly after this line, outside both.
app.add_middleware(metrics.HTTPMetrics)

# OpenTelemetry request tracing (tracing_setup.py) — a no-op unless TRACING_ENABLED. Instrumented
# AFTER the metrics middleware so tracing is outermost (KTD10's stack: OTel → metrics →
# RequireSSO → CORS → routes): an SSO-rejected request is traced and counted alike. /health is
# excluded; every URL span attribute leaves with its query string stripped (R15b).
tracing_setup.instrument_app(app)

# Avviksvarsel API (registered-journey push alerts). Registered before the /varsel static mount
# so /varsel-api/* resolves to these routes.
app.include_router(varsel.router)

# Reisevalg API (car vs train recommendation for the Lier -> Oslo S corridor). Registered before
# the static mounts, like /varsel-api.
app.include_router(reisevalg.router)

# Varselhistorikk API (what Avviksvarsel decided, and whether it turned out right). Registered
# before the /historikk static mount, like the two above.
app.include_router(historikk.router)

# Meldinger API (a window of past alert decisions, replayed through today's prompt). Registered
# before the /meldinger static mount, like the three above.
#
# Served in every environment, and know what that publishes. The surface regenerates model prose
# from the stored message_input, and the prompt instructs the model to advise on the traveller's
# own route, so the text routinely names both ends of a trip — next to departure_time and
# n_devices, on a host where `trafficType: public` is set once in the chart and inherited
# everywhere. The /historikk per-row redaction (journey_digest, from_stop and to_stop withheld,
# Decision extra="forbid") still holds here, but it cannot close this: the exposure is in the
# GENERATED SENTENCE, not in a column. Measured 2026-08-22 over 14 days of prd decisions, 21 of 31
# distinct from/to pairs (68 %) map to exactly one journey definition, so a Fra/Til selection in
# the page's own dropdowns isolates a single registration two times out of three.
#
# It is accepted on ONE fact rather than on that measurement: prd's registered journeys are TEST
# USERS, not members of the public (team decision, 2026-08-22). There is no real traveller behind a
# k=1 pair, and the corridors carry tens of thousands of people a day, so naming one as disrupted
# discloses nothing about anybody. WHAT WOULD CHANGE THE ANSWER is prd taking registrations from
# the public — re-run that measurement then, and the fix is access control in front of /meldinger
# (a chart change, since trafficType is set once), not a shorter SELECT list. A page built on
# hypothetical journeys rather than registrations would retire the question outright.
app.include_router(meldinger.router)

# Rushhistorikk API (did the rush forecasts land, and did we tell anybody?). Registered before
# the /rushhistorikk static mount, like the routers above.
#
# What this publishes, since the SSO prefixes it registers gate nothing today (SSO_REQUIRED is
# "false" in every environment and no environment wires the OAuth client): a rush forecast is one
# product-wide prognosis over a fixed 39-station geography, so there is no journey, route, device or
# departure behind any row — the one way this surface is simpler than /historikk and /meldinger. The
# five subscriber-derived counts on togvarsel_rounds are the exception, and rushhistorikk_days.sql
# never names them.
app.include_router(rushhistorikk.router)

# Webhook notification API (/notification-api/v1): the authenticated surface a client BFF manages
# its own commuter registrations through. Three things about this registration are deliberate.
#
# It is NOT behind sso.RequireSSO, and the client prefix is deliberately absent from
# sso.PROTECTED_PREFIXES: that gate answers an unconfigured deployment with 503 and an
# unauthenticated *browser* with a 302, and a machine caller following a redirect into a Google
# login screen gets an HTML page where it expected JSON. Its own credential dependency
# (notification_auth.authenticate, KTD8) runs per request at router level instead, so a route added
# later is gated even if its author forgets the parameter.
#
# It is registered unconditionally, unlike /togmelding-api: there is no product-secrecy gate here
# and the whole point of the surface is a published contract, so a client can be provisioned and
# can register watches before the notification pass is switched on for that environment.
# Availability is decided per request by notification_auth.available() — store plus pepper — which
# answers 503 where either is missing.
#
# And it is registered before the static mounts, like every other API router: nothing serves
# /notification-api as a page, but the ordering rule is the one this file follows throughout.
app.include_router(notification_api.router)
# The journey-keyed PUBLIC read on the same prefix (2026-09-10). Its own router, because the one
# above carries the credential and throttle dependencies at router level and a read with no
# credential cannot live on it; same prefix, so the published contract stays one API.
app.include_router(notification_pull.router)

# Togmelding API (the rush-hour forecast). Registered before the /togmelding static mount, like
# the routers above. Serves from the in-process cache the poller's togvarsel phase maintains.
#
# Togvarsel-notify's two routes ride the same gate, both over the seams injected in lifespan above.
# GET /togmelding-api/outlook is the status panel /varsel shows at the top of its page: today's
# rush levels for a device, gated on the delivery flag and on the poller's station cache, so it
# only ever claims "you are being notified about this" where that is true.
# POST /togmelding-api/test-notify is the dev-only test trigger: one synthetic rush-forecast push
# through the same composition + delivery seams the real rounds use. It gates itself on
# deployment.environment_label() == "dev" per call — tst, prd and unset labels get a plain 404 —
# so mounting it everywhere is safe and keeps the image identical across environments.
#
# Both routers are gated on SHOW_RUSHVARSEL (deployment.show_rushvarsel), and TWO mechanisms is
# deliberate rather than belt-and-braces, because they close different things:
#   - not INCLUDING them, here, is what keeps /togmelding-api out of the public OpenAPI schema.
#     `docs_url` is FastAPI's default, so /docs lists every registered route: a router that is
#     present but 404s would still publish the endpoint names of a product we are not showing, and
#     a listing is itself a mention. Import-time, like the /togmelding mount below and for the
#     same reason — an env flag only moves on a pod restart, which is when this line is re-read.
#   - the router-level `deployment.require_rushvarsel` dependency inside each module is what makes
#     the gate hold PER REQUEST wherever the router is mounted — the two test suites mount it on
#     apps of their own, and it is what a future mount inherits without having to remember this
#     block. It is also the only thing standing in front of the dev-only test trigger, which is
#     deliberately NOT wired to the `enabled` seam (_TRIGGER_SEAMS omits it, so the pipe can be
#     proven before the delivery flag opens) and would otherwise still send a real push.
if deployment.show_rushvarsel():
    app.include_router(togvarsel.router)
    app.include_router(togvarsel_notify.router)
else:
    logger.info("SHOW_RUSHVARSEL is off; /togmelding-api not registered (404, and off /docs)")

# The SSO login/callback/logout routes (public by construction — they are how a session is made).
app.include_router(sso.router)

# --- How long a browser may keep a static file ----------------------------------------------
#
# Starlette's StaticFiles sends ETag and Last-Modified but no Cache-Control, and a response with
# neither Cache-Control nor Expires is *heuristically* cacheable: the browser is free to invent a
# freshness lifetime (commonly a tenth of the age since Last-Modified) and skip revalidating
# entirely. For a content-hashed asset that is harmless — a new build has a new filename. For
# index.html it is the whole deployment: the file keeps its name and its <script src> is the only
# thing that names the current bundle, so a stale copy pins the browser to the OLD bundle and the
# deploy never reaches that user.
#
# Not hypothetical. Measured on prd 2026-08-11, minutes after a deploy: the server was serving
# index-BA3KKIi9.js while a browser whose index.html was 40 minutes old was still running
# index-N5X3j20f.js — the pre-fix bundle, with the very bug the deploy had just fixed. It reads
# exactly like a failed deploy, which is the expensive part.
#
# So the two kinds of file get opposite treatment, and the split is the filename, not the type:
#   assets/  — Vite writes ONLY content-hashed names here, so the bytes behind a URL never change.
#              A year, `immutable`, never revalidated. This is where the payload is.
#   anything else — index.html, the PWA's manifest, service worker and icons, /eval's notebooks.
#              `no-cache` does not mean "do not store": it means "ask first". The ETag is already
#              there, so the usual answer is a 304 with no body.
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_REVALIDATE_CACHE_CONTROL = "no-cache"


class _VersionedStaticFiles(StaticFiles):
    """StaticFiles that says how long each file may be kept.

    Use this for every static mount rather than StaticFiles itself; a mount added with the plain
    class silently reverts to heuristic caching, which fails in the one direction nobody checks —
    the page keeps working, it is just the wrong version of it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Resolved once, and it has to be resolved at all: StaticFiles hands file_response the
        # REALPATH of the file (it resolves symlinks before serving, so a client cannot escape the
        # mount) while `directory` stays exactly as it was passed in. Compare the two unresolved
        # and nothing ever matches — every file, assets included, quietly takes the cautious
        # branch below and the immutable half of this simply never happens. That is precisely how
        # it failed when first written: caught by the tests on macOS, where a temp dir under
        # /var/... resolves to /private/var/..., and it would NOT have shown up in the container.
        self._root = Path(self.directory).resolve()

    def file_response(self, full_path, stat_result, scope, status_code=200):
        # Set on the RESPONSE, deliberately, rather than before the super() call. The base method
        # decides on the spot whether this is a 304 and, if so, returns a NotModifiedResponse built
        # from the headers it had at that moment — so a header added earlier would have to survive
        # that copy (it would: cache-control is in NOT_MODIFIED_HEADERS), while one added here lands
        # on either kind of response. A revalidation that answered without Cache-Control would
        # re-enter heuristic caching on every 304 and quietly undo all of this.
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["cache-control"] = self._cache_control(full_path)
        return response

    def _cache_control(self, full_path) -> str:
        """`immutable` only for a file whose name can never mean different bytes."""
        try:
            relative = Path(full_path).resolve().relative_to(self._root)
        except ValueError:
            # Not under the mount root at all (a symlink out, an odd path). Unknown provenance is
            # not a reason to promise a year of immutability.
            return _REVALIDATE_CACHE_CONTROL
        if relative.parts[:1] == ("assets",):
            return _IMMUTABLE_CACHE_CONTROL
        return _REVALIDATE_CACHE_CONTROL


# Pre-rendered marimo eval notebooks, served read-only at /eval (index at /eval/,
# each notebook at /eval/<name>.html). They are static snapshots built offline by
# export_eval.sh — outputs baked in — so serving them costs no BigQuery and starts no
# kernel. The directory is generated (absent in a fresh checkout until exported), so
# mount it only when present rather than failing startup.
#
# Every file here keeps its name across a re-export, so none of it is immutable — which is what
# _VersionedStaticFiles concludes on its own, there being no assets/ directory.
_EVAL_STATIC_DIR = Path(__file__).parent / "eval_static"
if _EVAL_STATIC_DIR.is_dir():
    app.mount("/eval", _VersionedStaticFiles(directory=_EVAL_STATIC_DIR, html=True), name="eval")
    logger.info("Mounted static eval notebooks at /eval from %s", _EVAL_STATIC_DIR)
else:
    logger.info("No eval_static/ directory; /eval not mounted (run export_eval.sh to build it)")

# The journey-prediction demo UI (demo/reisedemo), built to static assets by its build script.
# Same pattern as /eval: generated directory, mounted only when present. The data behind it is
# served by the /demo-api endpoints below.
_DEMO_STATIC_DIR = Path(__file__).parent / "demo_static"
if _DEMO_STATIC_DIR.is_dir():
    app.mount("/demo", _VersionedStaticFiles(directory=_DEMO_STATIC_DIR, html=True), name="demo")
    logger.info("Mounted demo UI at /demo from %s", _DEMO_STATIC_DIR)
else:
    logger.info("No demo_static/ directory; /demo not mounted (run demo/reisedemo/build.sh)")

# The Avviksvarsel PWA (demo/avviksvarsel), built to static assets by its build script. Same
# generated-directory pattern as /eval and /demo; the manifest, service worker and icons ship in
# this directory and are served at /varsel/. Its data + push endpoints are /varsel-api (above).
_VARSEL_STATIC_DIR = Path(__file__).parent / "varsel_static"
if _VARSEL_STATIC_DIR.is_dir():
    app.mount("/varsel", _VersionedStaticFiles(directory=_VARSEL_STATIC_DIR, html=True), name="varsel")
    logger.info("Mounted avviksvarsel PWA at /varsel from %s", _VARSEL_STATIC_DIR)
else:
    logger.info("No varsel_static/ directory; /varsel not mounted (run demo/avviksvarsel/build.sh)")

# The Varselhistorikk UI (demo/varselhistorikk) — which alert decisions were taken and how they
# turned out. Same generated-directory pattern as the mounts above; its data comes from
# /historikk-api (registered further up, before these mounts).
_HISTORIKK_STATIC_DIR = Path(__file__).parent / "historikk_static"
if _HISTORIKK_STATIC_DIR.is_dir():
    app.mount(
        "/historikk", _VersionedStaticFiles(directory=_HISTORIKK_STATIC_DIR, html=True), name="historikk"
    )
    logger.info("Mounted varselhistorikk UI at /historikk from %s", _HISTORIKK_STATIC_DIR)
else:
    logger.info(
        "No historikk_static/ directory; /historikk not mounted "
        "(run demo/varselhistorikk/build.sh)"
    )

# The Togmelding page (demo/togmelding) — the rush-hour forecast, rendered from
# /togmelding-api/today. Same generated-directory pattern as the mounts above: it was a single
# COMMITTED hand-written HTML file until 2026-09-01, when it moved onto Entur Linje to match
# /varsel — which means npm packages, and therefore a Vite bundle built in the image.
#
# Gated on SHOW_RUSHVARSEL (deployment.show_rushvarsel), the master gate over the whole product:
# with it off the page is simply never mounted, so /togmelding and /togmelding/ both answer a
# plain 404 because no route claims the path, exactly as if the surface had never shipped. Read at
# import, unlike every other check on this flag — a mount cannot be conditioned per request
# without a StaticFiles subclass, and Mount's own path regex would answer the bare /togmelding
# with a 307 to the trailing slash BEFORE any per-request check could 404 it. An env flag only
# changes on a pod restart anyway, which is also when this line is re-read.
_TOGMELDING_STATIC_DIR = Path(__file__).parent / "togmelding_static"
if not deployment.show_rushvarsel():
    logger.info("SHOW_RUSHVARSEL is off; /togmelding not mounted (the whole surface 404s)")
elif _TOGMELDING_STATIC_DIR.is_dir():
    app.mount(
        "/togmelding",
        _VersionedStaticFiles(directory=_TOGMELDING_STATIC_DIR, html=True),
        name="togmelding",
    )
    logger.info("Mounted togmelding page at /togmelding from %s", _TOGMELDING_STATIC_DIR)
else:
    logger.info(
        "No togmelding_static/ directory; /togmelding not mounted "
        "(run demo/togmelding/build.sh)"
    )

# The Meldinger UI (demo/meldinger) — the same decisions, replayed through today's prompt.
# Same generated-directory pattern as the mounts above; its data comes from /meldinger-api, which
# is registered unconditionally beside it (see the router above for what the surface publishes).
# The Rushhistorikk UI (demo/rushhistorikk) — did the rush forecasts land, and did we tell
# anybody? Same generated-directory pattern as the mounts above; its data comes from
# /rushhistorikk-api, registered unconditionally beside it.
_RUSHHISTORIKK_STATIC_DIR = Path(__file__).parent / "rushhistorikk_static"
if _RUSHHISTORIKK_STATIC_DIR.is_dir():
    app.mount(
        "/rushhistorikk",
        _VersionedStaticFiles(directory=_RUSHHISTORIKK_STATIC_DIR, html=True),
        name="rushhistorikk",
    )
    logger.info("Mounted rushhistorikk UI at /rushhistorikk from %s", _RUSHHISTORIKK_STATIC_DIR)
else:
    logger.info(
        "No rushhistorikk_static/ directory; /rushhistorikk not mounted "
        "(run demo/rushhistorikk/build.sh)"
    )


_MELDINGER_STATIC_DIR = Path(__file__).parent / "meldinger_static"
if _MELDINGER_STATIC_DIR.is_dir():
    app.mount(
        "/meldinger",
        _VersionedStaticFiles(directory=_MELDINGER_STATIC_DIR, html=True),
        name="meldinger",
    )
    logger.info("Mounted meldinger UI at /meldinger from %s", _MELDINGER_STATIC_DIR)
else:
    logger.info(
        "No meldinger_static/ directory; /meldinger not mounted "
        "(run demo/meldinger/build.sh)"
    )


def get_last_ingestion_time() -> datetime | None:
    """Return the most recent ingestionTime for today's SIRI-SX partitions."""
    row = next(
        iter(bq_client.query(_LAST_INGESTION_TIME_QUERY).result(timeout=_POLLER_QUERY_TIMEOUT_S))
    )
    return row.last_ingestion_time


# Poller liveness, read by GET /health (KTD1). The heartbeat is MONOTONIC on purpose: the age
# check must not jump on wall-clock corrections, and the epoch gauge a later unit exports is a
# different clock that must never leak in here. No lock, unlike the caches above: one writer (the
# poller thread), scalar assignment is atomic under the GIL, and a read racing one tick behind is
# indistinguishable from probing a moment earlier. The thread handle is what tells "poller died"
# from "poller never started" — hermetic tests and local runs without the lifespan leave it None,
# and /health must stay 200 for them.
_poller_heartbeat_monotonic: float | None = None
_poller_thread: threading.Thread | None = None
_health_degraded_logged = False  # one health_degraded record per 200→503 transition


def _beat() -> None:
    """Record that the poller thread is still making progress — BOTH clocks, always together.

    Called at the top of every iteration and again at each progress point inside it: the end of
    every `_phase`, after each of the wall-clock tick blocks, and — since U2 — once per chunk of
    definitions inside `varsel`'s match and wording passes, through the `beat` seam injected at
    `varsel.configure`. That last one is not a `_beat()` call any grep of this file will find, and
    it is the one that matters most at scale: the whole alerting pass is a single `_phase`, so
    without it a pass that merely got slow at 100 000 subscriptions is indistinguishable from a
    wedged poller. One beat per ITERATION was the
    original shape and it made the threshold mean the wrong thing: an iteration is ~12-16
    sequential BigQuery calls (each bounded at `_POLLER_QUERY_TIMEOUT_S`), plus togvarsel's rebuild
    and reads, plus the notify and servability ceilings, plus the sleep — so its worst case is tens
    of minutes and its COLD case is already minutes. A restart zeroes `_planned_refreshed_at` and
    `_bus_planned_refreshed_at`, so the first iteration after one rebuilds both ET partition scans
    (~8 GB rail, ~12 GB bus) on top of everything else; at `HEALTH_STALE_AFTER_S` = 600 a healthy
    but cold start could cross the line, 503, get restarted by liveness, and come up cold again —
    a self-sustaining crash loop costing ~20 GB of scans per turn, with no single query anywhere
    near its own ceiling. Beating on progress makes the threshold mean what KTD1 says it means:
    not "an iteration took longer than 600 s" but "no STEP has finished in 600 s", which is the
    wedged-poller state a per-query timeout cannot see and `is_alive()` cannot see either.

    Both clocks move together or the fix just relocates the false alarm: the monotonic one is
    /health's (age arithmetic, immune to a wall-clock step) and the epoch gauge is the runbook's
    rule 1 at 900 s. Beating only the first would leave rule 1 paging on exactly the cold iteration
    /health had just learned to tolerate.
    """
    global _poller_heartbeat_monotonic
    _poller_heartbeat_monotonic = time.monotonic()
    metrics.POLLER_LAST_TICK.set(time.time())


def _health_stale_after_s() -> float:
    """Staleness threshold for /health, from HEALTH_STALE_AFTER_S (seconds; default 600).

    Read at call time so tests (and a redeploy-free env tweak) take effect per request. `0`
    disables the staleness branch; thread-death detection stays active regardless. An unparsable
    value falls back to the default rather than taking the probe down with a ValueError.
    """
    raw = os.environ.get("HEALTH_STALE_AFTER_S", "").strip()
    try:
        return float(raw) if raw else 600.0
    except ValueError:
        return 600.0


def _poller_health() -> tuple[bool, str | None]:
    """The ordered liveness decision behind GET /health: (healthy, reason when not).

    Order matters and is KTD1's: no thread handle → healthy (the poller was never started — see
    the globals above); a handle that is not alive → dead, regardless of the staleness setting; a
    heartbeat older than the threshold → wedged (the thread is alive but stopped iterating, e.g.
    blocked in an unbounded upstream wait — is_alive() cannot see that state); else healthy. A
    None heartbeat beside a live thread is the sub-second window before the first iteration.
    """
    if _poller_thread is None:
        return True, None
    if not _poller_thread.is_alive():
        return False, "poller thread has died"
    stale_after = _health_stale_after_s()
    if stale_after > 0 and _poller_heartbeat_monotonic is not None:
        age = time.monotonic() - _poller_heartbeat_monotonic
        if age > stale_after:
            return False, f"poller heartbeat is {age:.0f} s old (limit {stale_after:.0f} s)"
    return True, None


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
        # Liveness heartbeat for /health (R1): unconditional, at the TOP of the iteration —
        # before anything that can fail — so a broken upstream reads as the logged per-tick
        # error below while the pod stays up. `_beat` also runs at every progress point INSIDE
        # the iteration (see its docstring), so a slow-but-moving cycle stays healthy and only a
        # step that has stopped finishing reads as a wedge.
        _beat()
        try:
            current = get_last_ingestion_time()
            if not seen_any or current != last_seen:
                last_seen = current
                seen_any = True
                on_change(current)
        except Exception:
            logger.exception("Ingestion poll failed; retrying on next tick")
        _beat()
        # Togmeldingen (/togmelding-api) runs on EVERY tick, not just ingestion changes: an issue
        # is due at its clock cutoff (06:30/07:00/12:00/14:00) whether or not new SX arrived, so
        # it must not hang off the SX cadence. Cheap in-memory no-op on the vast majority of
        # ticks; on a weekday tick past an un-issued forecast time it rebuilds the live togvarsel
        # feature row and scores the due models. Isolated so a failure never touches the feed —
        # the endpoint keeps serving whatever issues the day already has.
        try:
            togvarsel.refresh()
        except Exception:
            logger.exception(
                "Togvarsel refresh failed; /togmelding-api serves the day's existing issues"
            )
        _beat()
        # Togvarsel pushes ride the same wall-clock tick, AFTER refresh so the tick that issues a
        # round is the tick that can notify it — and never the SX-ingestion rebuild above.
        # Isolated the same way: a notify failure costs notifications, never the feed or the tick.
        try:
            togvarsel_notify.evaluate()
        except Exception:
            logger.exception("Togvarsel notify failed this tick; retrying on next tick")
        _beat()
        # The rush outcome resolves on the same wall-clock tick, and for the same reason: a rush
        # ends at its hour whether or not SX moved. Both of its passes are due-gated internally
        # (provisional 09:20/18:20 Oslo, authoritative after 04:00 UTC), so this is a cheap no-op on
        # all but a few ticks a day. Isolated like the two above: a fasit failure costs
        # /rushhistorikk a day of verdicts, never the feed, the forecast or the notifications.
        try:
            togvarsel_outcomes.refresh()
        except Exception:
            logger.exception("Togvarsel outcome refresh failed this tick; retrying on next tick")
        _beat()
        # The labelling-freshness detector rides the same wall-clock tick, fourth and last (R8):
        # the nightly varsel-label-daily job runs OUTSIDE this process, so nothing else in here
        # would ever notice it dying — and past its 10-day retry window the unlabelled decisions
        # are lost for good. decisions rate-limits itself to one BigQuery check per 6 h, so this
        # is a cheap in-memory no-op on all but ~4 ticks a day. Deliberately NOT a _rebuild_all
        # phase (KTD7): a quiet night with no SX movement — hence no rebuild — is exactly when a
        # dead labelling job must still be noticed. Isolated like the three blocks above: the
        # function never raises by design, so this except is belt-and-braces against a defect in
        # the detector itself, which must cost the check and never the feed, the pushes or the tick.
        try:
            decisions.check_labelling_freshness()
        except Exception:
            logger.exception("Labelling freshness check failed this tick; retrying on next tick")
        _beat()
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
    dep_delay_p90_min: int | None = Field(
        description="Bad-case (p90) departure delay (min), from the calibrated exceedance stack "
        "when available (narrower at the same coverage than the legacy per-bucket lookup).",
    )
    dep_delay_p50_min: int | None = Field(
        default=None,
        description="Median departure delay (min) from the exceedance stack; with "
        "dep_delay_p90_min this forms the 'expect X-Y min' interval. Null at a terminal.",
    )
    dep_ge10_probability: float | None = Field(
        default=None,
        description="Calibrated P(departure >= 10 min late | the train runs), 0-1. Null at a terminal.",
    )
    dep_impact_probability: float | None = Field(
        default=None,
        description="Calibrated P(this stop is cancelled OR the departure is >= 10 min late), 0-1 "
        "— the boarding-stop alerting score. Null at a terminal.",
    )
    arr_delay_typ_min: int | None = Field(description="Typical arrival delay (min) if it runs.")
    arr_delay_p90_min: int | None = Field(
        description="Bad-case (p90) arrival delay (min), from the calibrated exceedance stack "
        "when available (narrower at the same coverage than the legacy per-bucket lookup).",
    )
    arr_delay_p50_min: int | None = Field(
        default=None,
        description="Median arrival delay (min) from the exceedance stack; with arr_delay_p90_min "
        "this forms the 'expect X-Y min' interval. Null at an origin.",
    )
    arr_ge10_probability: float | None = Field(
        default=None,
        description="Calibrated P(arrival >= 10 min late | the train runs), 0-1. Null at an origin.",
    )
    impact_probability: float | None = Field(
        default=None,
        description="Calibrated P(this stop is cancelled OR the arrival is >= 10 min late), 0-1 — "
        "the recommended single alerting score. Null at an origin.",
    )


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
    deprecated=True,
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
    """**Deprecated:** use `/impact` (a passenger's trip) or `/journey` (a specific train) for
    per-train cancellation and delay predictions instead.

    List today's disruptions (still-ongoing and recently reopened), each with reopen and
    impact estimates as **minutes from now**.

    Reopened disruptions are kept in the response and flagged (`reopened: true`) because impact
    typically takes a while to resolve after the line reopens. Use `active_count` /
    `reopened_count` to split the two, and `as_of` as the reference point for every estimate.

    Pass `at` to reconstruct the feed at a past instant instead of now (see the parameter docs);
    `as_of` then echoes `at`, and `refreshed_at` is null because the result is queried live rather
    than served from the cache.
    """
    if at is None:
        now = datetime.now(UTC)
        with _disruptions_lock:
            cached = _disruptions_cache
            refreshed_at = _disruptions_refreshed_at
    else:
        now = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
        if now > datetime.now(UTC):
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
    deprecated=True,
)
async def situation(
    id: str = Query(
        description="SIRI-SX situationNumber to render, e.g. 'ENT:SituationNumber:1234'.",
    ),
) -> Disruption:
    """**Deprecated:** use `/impact?situation=...` for the per-train impact of a single
    situation instead.

    Render a single disruption identified by its `situationNumber`.

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
    return _to_disruption(d, datetime.now(UTC))


@app.get(
    "/journey",
    summary="Per-train cancellation + delay predictions for a journey",
    response_description="Per-stop predictions for the affected train.",
    tags=["disruptions"],
)
async def journey(
    id: str = Query(
        description=(
            "Journey identifier — a SIRI journey ref: rail 'VYG:DatedServiceJourney:318_LHM-DRM_"
            "25-09-01' or bus 'RUT:ServiceJourney:<id>'. The codespace prefix disambiguates the "
            "operator; the bare jkey ('DatedServiceJourney:…' / 'ServiceJourney:…') also works when "
            "a single operator carries it (else 409 — include the prefix)."
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
    # Recover the operator (codespace prefix) so bus ServiceJourney jkeys don't collide across
    # operators. The cache jkey is the ref minus its leading codespace and itself starts with a
    # ref-type token ("DatedServiceJourney:"/"ServiceJourney:"), so we try the prefixed reading
    # first ((operator, jkey) from "<CS>:<jkey>") and, if that misses, fall back to treating the
    # whole id as a bare jkey — which resolves only when a single operator carries it (else 409).
    m = re.match(r"^([A-Za-z]+):", id)
    ambiguous_ops: list[str] | None = None
    with _call_pred_lock:
        refreshed_at = _call_pred_refreshed_at
        rows = _call_pred_by_key.get((m.group(1), id[m.end():])) if m else None
        if rows is None:
            ops = _call_pred_by_jkey_index.get(id, set())
            if len(ops) > 1:
                ambiguous_ops = sorted(ops)
            elif ops:
                rows = _call_pred_by_key.get((next(iter(ops)), id))
    if ambiguous_ops is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Journey key {id!r} is ambiguous across operators {ambiguous_ops}; "
                "include the codespace prefix (e.g. 'RUT:ServiceJourney:...')."
            ),
        )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No active disruption affecting journey {id!r}.",
        )
    # A journey can be touched by several situations; surface the most severe (highest peak
    # cancellation probability across its stops).
    by_sit: dict[str, list[impact_engine.CallPred]] = {}
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
        as_of=datetime.now(UTC),
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
                dep_delay_p90_min=(
                    c.dep_delay_p90_stack_min
                    if c.dep_delay_p90_stack_min is not None
                    else c.dep_delay_p90_min
                ),
                dep_delay_p50_min=c.dep_delay_p50_stack_min,
                dep_ge10_probability=c.p_dep_ge10,
                dep_impact_probability=c.p_impact_dep,
                arr_delay_typ_min=c.arr_delay_typ_min,
                arr_delay_p90_min=(
                    c.arr_delay_p90_stack_min
                    if c.arr_delay_p90_stack_min is not None
                    else c.arr_delay_p90_min
                ),
                arr_delay_p50_min=c.arr_delay_p50_stack_min,
                arr_ge10_probability=c.p_arr_ge10,
                impact_probability=c.p_impact,
            )
            for c in calls
        ],
    )



@app.get(
    "/impact",
    summary="Per-train cancellation + delay impact on a passenger's trip",
    response_description="Situations and lines affecting the trip, each with departure/arrival predictions.",
    tags=["disruptions"],
)
def impact(
    from_stop: str = Query(
        default=_DEFAULT_FROM_STOP,
        description=(
            "Boarding StopPlace ref (NSR:StopPlace:...). Defaults to Oslo S (NSR:StopPlace:337)."
        ),
    ),
    at: datetime | None = Query(
        default=None,
        description=(
            "Intended departure time from `from_stop` (ISO 8601). Used to pick, per line, the "
            "affected train closest to this time. Defaults to the current time. A naive value is "
            "assumed UTC."
        ),
    ),
    situation: str | None = Query(
        default=None,
        description=(
            "Optional SIRI-SX situationNumber. When given, only that situation is considered "
            "(`situations` is length 1, or 0 if it does not affect the trip). When omitted, every "
            "situation affecting a departure from `from_stop` near `at` is returned."
        ),
    ),
    to_stop: str | None = Query(
        default=None,
        description="Optional alighting StopPlace ref (NSR:StopPlace:...) for the arrival prediction.",
    ),
) -> impact_engine.ImpactResponse:
    """Per-train impact of one or more situations on a passenger's intended trip.

    Resolves, for each affected line, the train departing `from_stop` nearest `at`, and scores it
    at the boarding stop (`departure_prediction`: will it leave, how late?) and — when `to_stop`
    is given — at the alighting stop (`arrival_prediction`: will I arrive, how late?), using the
    same per-call model as /journey.

    The result is exposed two ways: `situations` (one entry per situation, worst-case over its
    lines) and `lines` (one entry per line, worst-case over the situations affecting it). Pass
    `situation` to scope to a single situation. Both lists are empty when nothing affects the trip.

    Plain ``def`` (NOT ``async def``) on purpose, matching ``reisevalg.recommendation``: this may
    make a blocking Journey Planner call, which must run in the anyio threadpool. On the
    single-worker event loop it would freeze ``/journey``, ``/varsel-api`` and the ``/health``
    liveness probe (found in review 2026-07-29 -- it was ``async def`` with a blocking urlopen).
    """
    if at is None:
        at_utc = datetime.now(UTC)
    else:
        at_utc = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    return impact_engine.for_trip(
        from_stop=from_stop,
        to_stop=to_stop,
        window_start=at_utc - _IMPACT_AT_TOLERANCE,
        window_end=at_utc + _IMPACT_AT_TOLERANCE,
        select="nearest",
        at=at_utc,
        situation=situation,
    )


@app.get(
    "/alert_profile",
    summary="Historical impact profile for a category (optionally a line and hour)",
    response_description="The most specific impact profile with enough data, after back-off.",
    tags=["disruptions"],
    deprecated=True,
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
    """**Deprecated:** use the calibrated per-train `impact_probability` served by `/impact`
    and `/journey` to decide whether to alert, instead of these historical category profiles.

    Look up the historical impact profile for a (category, line, hour) cell.

    Backs off to coarser cells when the specific one is too sparse — drop the hour, then the
    line, finally the global row — so a result is (almost) always returned. Check `grain` /
    `matched_*` to see how specific the match actually is, and `alert_tier` / `alert_score`
    to rank it. 404 only if the lookup table has not loaded yet.
    """
    h = _oslo_hour(datetime.now(UTC)) if hour is None else hour
    row = lookup_alert_profile(category, line, h)
    if row is None:
        raise HTTPException(status_code=404, detail="No impact profile available (lookup not loaded).")
    return _alert_profile_model(row)


# ---- /demo-api: retrospective "how good were the predictions?" journey demo -------------------
# Serves the static demo dataset (temporal holdout 2026-04 -> 2026-06, NSB rail on the prod
# model family + GOA/bus on the pooled_all family) built by
# data/queries/create_demo_predictions.sql: for every SX-flagged call, the calibrated
# predictions the models would have served at three ask times, next to the realized outcome.
# The UI at /demo (demo/reisedemo, built into demo_static/) consumes these endpoints.

# The alert decision shown in the demo: flag when P(cancelled OR >= 10 min late) crosses the
# operator's live alerting threshold (alert_policy.threshold_for — the per-operator cuts tuned at
# ~90 % precision by eval/tune_alert_thresholds.py, 0.65 default), so the demo's verdicts
# replay exactly what the varsel push pipeline would have decided.
# Realized outcome counts as "trouble" when the call was cancelled or >= 10 min late — the
# same event the impact probability predicts, so it comes from alert_policy (single definition).
# Taken from the policy module directly rather than through varsel: /demo needs the gate, not the
# push surface that happens to be its other reader.
_DEMO_TROUBLE_DELAY_MIN = alert_policy.TROUBLE_DELAY_MIN

# The demo tables are near-static snapshots (extend_demo_recent.sql refreshes them on
# demand), so their metadata (stop picker, scenario list, date window) is cached with a
# TTL long enough to keep BigQuery traffic negligible but short enough that a dataset
# refresh shows up without a redeploy. Guarded by its own lock.
_DEMO_META_TTL_S = 30 * 60
_demo_meta_lock = threading.Lock()
_demo_meta_cache: "tuple[float, DemoMeta] | None" = None


class DemoStop(BaseModel):
    """A stop available in the demo dataset's journey search."""

    stop_place: str = Field(description="NSR StopPlace ref.")
    stop_name: str | None = Field(description="Human-readable stop name.")
    n_calls: int = Field(description="Calls at this stop within the demo window.")
    mode: str = Field(description="What serves this stop: 'Tog', 'Buss' or 'Tog/Buss'.")


class DemoHighlight(BaseModel):
    """A pre-picked high-impact situation, with a suggested trip + window that showcases it."""

    situation_number: str = Field(description="SIRI-SX situationNumber.")
    operator: str = Field(description="Codespace of the worst-hit journey, e.g. 'NSB' or 'RUT'.")
    mode: str = Field(description="Vehicle noun for that operator: 'Tog' or 'Buss'.")
    summary: str | None = Field(description="First-version Norwegian summary of the situation.")
    category: str | None = Field(description="Disruption category.")
    onset: datetime = Field(description="When the SX message arrived (UTC).")
    n_cancelled: int = Field(description="Realized cancelled calls among the flagged calls.")
    n_delayed10: int = Field(description="Realized >= 10 min late (non-cancelled) flagged calls.")
    window_start: datetime = Field(description="First affected departure (UTC).")
    window_end: datetime = Field(description="Last affected departure (UTC).")
    from_stop: str = Field(description="Suggested boarding StopPlace ref.")
    from_name: str | None = Field(description="Suggested boarding stop name.")
    to_stop: str = Field(description="Suggested alighting StopPlace ref.")
    to_name: str | None = Field(description="Suggested alighting stop name.")


class DemoMeta(BaseModel):
    """Everything the demo UI needs to render its controls."""

    window_start: date = Field(description="First date covered by the demo dataset.")
    window_end: date = Field(description="Last date covered by the demo dataset.")
    alert_threshold: float = Field(
        description="Default impact-probability threshold behind `would_alert`; each journey "
        "carries its operator's actual threshold.",
    )
    stops: list[DemoStop] = Field(description="Stops offered in the from/to pickers.")
    highlights: list[DemoHighlight] = Field(
        description="One-click example scenarios, most recent first (they are selected by "
        "realized impact, then ordered by date).",
    )


class DemoConfig(BaseModel):
    """What the demo UI needs to describe itself, independent of the demo dataset."""

    environment: str | None = Field(
        description='Non-prod environment name ("dev", "tst") for the page to show in its title; '
        "null in prd.",
    )


class DemoSnapshot(BaseModel):
    """The prediction as it stood at one ask time, plus how it compares to the outcome."""

    cancel_probability: float = Field(description="Calibrated P(this call is cancelled), 0-1.")
    ge10_probability: float | None = Field(description="Calibrated P(>= 10 min late | runs), 0-1.")
    impact_probability: float | None = Field(
        description="Calibrated P(cancelled OR >= 10 min late), 0-1 — the alerting score.",
    )
    delay_p50_min: int | None = Field(description="Median predicted delay (min) if the call runs.")
    delay_p90_min: int | None = Field(description="Bad-case (p90) predicted delay (min).")
    would_alert: bool = Field(
        description="Whether this prediction crosses the journey's `alert_threshold`.",
    )
    verdict: str | None = Field(
        description=(
            "Prediction vs realized outcome: 'hit' (alerted, trouble came), 'correct_reject' "
            "(no alert, no trouble), 'miss' (no alert, trouble came), 'false_alarm' (alerted, "
            "no trouble). Null when the outcome is unknown."
        ),
    )


class DemoSide(BaseModel):
    """One end of the journey (boarding or alighting): outcome + the three ask-time snapshots."""

    stop_place: str = Field(description="NSR StopPlace ref of this end.")
    stop_name: str | None = Field(description="Stop name of this end.")
    scheduled_time: datetime = Field(description="Aimed departure (boarding) / arrival (alighting), UTC.")
    actual_cancelled: bool = Field(description="Whether this call was cancelled.")
    actual_delay_min: int | None = Field(
        description="Realized delay in whole minutes (negative = early); null if unobserved.",
    )
    actual_trouble: bool | None = Field(
        description="Realized outcome: cancelled OR >= 10 min late. Null when unknown.",
    )
    minutes_from_onset: int | None = Field(
        description=(
            "Minutes from SX message arrival to this call's aimed time — how much warning was "
            "possible. Null when no situation flagged the journey."
        ),
    )
    at_message: DemoSnapshot | None = Field(
        description="Prediction at SX message arrival (no observable train state yet).",
    )
    t60: DemoSnapshot | None = Field(
        description="Prediction 60 min before the call; null if the SX arrived later than that.",
    )
    t30: DemoSnapshot | None = Field(
        description="Prediction 30 min before the call; null if the SX arrived later than that.",
    )


class DemoSituation(BaseModel):
    """The SX situation whose predictions are shown for this journey (most severe if several)."""

    situation_number: str = Field(description="SIRI-SX situationNumber.")
    summary: str | None = Field(description="First-version Norwegian summary.")
    category: str | None = Field(description="Disruption category.")
    onset: datetime | None = Field(description="When the SX message arrived (UTC).")


class DemoJourney(BaseModel):
    """One matching journey: departure predictions/outcome left, arrival right."""

    journey_ref: str = Field(
        description="Operator-prefixed journey key, e.g. 'NSB:DatedServiceJourney:...' or "
        "'RUT:ServiceJourney:...' — same format /journey accepts.",
    )
    operator: str = Field(description="Codespace the journey runs in, e.g. 'NSB', 'GOA', 'RUT'.")
    mode: str = Field(description="Vehicle noun for the operator: 'Tog' or 'Buss'.")
    alert_threshold: float = Field(
        description="The operator's alerting threshold applied in this journey's `would_alert`.",
    )
    line: str | None = Field(description="Line ref, e.g. 'NSB:Line:L1'.")
    situation: DemoSituation | None = Field(
        description="The situation behind the predictions; null if nothing flagged this journey.",
    )
    departure: DemoSide = Field(description="Boarding stop: outcome + ask-time snapshots.")
    arrival: DemoSide = Field(description="Alighting stop: outcome + ask-time snapshots.")


class DemoJourneysResponse(BaseModel):
    """Journeys from `from_stop` to `to_stop` departing within [start, end]."""

    from_stop: str = Field(description="Boarding StopPlace ref queried.")
    to_stop: str = Field(description="Alighting StopPlace ref queried.")
    start: datetime = Field(description="Window start (UTC).")
    end: datetime = Field(description="Window end (UTC).")
    count: int = Field(description="Number of journeys returned.")
    journeys: list[DemoJourney] = Field(description="Matching journeys, earliest departure first.")


def _demo_actual_trouble(cancelled: bool, delay_min: int | None) -> bool | None:
    """Realized 'trouble' = cancelled or >= 10 min late; None when the delay was unobserved."""
    if cancelled:
        return True
    if delay_min is None:
        return None
    return delay_min >= _DEMO_TROUBLE_DELAY_MIN


def _demo_snapshot(row, side: str, lead: str, actual_trouble: bool | None,
                   threshold: float) -> DemoSnapshot | None:
    """Build one ask-time snapshot from the flat demo_journeys.sql row; None if unavailable
    (no situation, or the SX message had not yet arrived at this ask time)."""
    p_cancel = row[f"{side}_{lead}_p_cancel"]
    if p_cancel is None:
        return None
    p_impact = row[f"{side}_{lead}_p_impact"]
    would_alert = p_impact is not None and p_impact >= threshold
    verdict = None
    if actual_trouble is not None:
        verdict = (
            ("hit" if actual_trouble else "false_alarm")
            if would_alert
            else ("miss" if actual_trouble else "correct_reject")
        )
    return DemoSnapshot(
        cancel_probability=p_cancel,
        ge10_probability=row[f"{side}_{lead}_p_ge10"],
        impact_probability=p_impact,
        delay_p50_min=row[f"{side}_{lead}_p50"],
        delay_p90_min=row[f"{side}_{lead}_p90"],
        would_alert=would_alert,
        verdict=verdict,
    )


def _demo_side(row, side: str, stop_place: str, stop_name, scheduled, cancelled: bool,
               delay_min: int | None, threshold: float) -> DemoSide:
    """Assemble one end of a journey: realized outcome + the three ask-time snapshots."""
    actual_trouble = _demo_actual_trouble(cancelled, delay_min)
    return DemoSide(
        stop_place=stop_place,
        stop_name=stop_name,
        scheduled_time=scheduled,
        actual_cancelled=cancelled,
        actual_delay_min=delay_min,
        actual_trouble=actual_trouble,
        minutes_from_onset=row[f"{side}_min_from_onset"],
        at_message=_demo_snapshot(row, side, "onset", actual_trouble, threshold),
        t60=_demo_snapshot(row, side, "l60", actual_trouble, threshold),
        t30=_demo_snapshot(row, side, "l30", actual_trouble, threshold),
    )


def get_demo_meta() -> DemoMeta:
    """Load and serve the demo dataset's metadata: window, stops, highlight scenarios."""
    global _demo_meta_cache
    with _demo_meta_lock:
        if _demo_meta_cache is not None and time.monotonic() - _demo_meta_cache[0] < _DEMO_META_TTL_S:
            return _demo_meta_cache[1]
    window = next(iter(bq_client.query(_DEMO_WINDOW_QUERY).result()))
    if window.window_start is None:
        raise HTTPException(
            status_code=503,
            detail="Demo dataset not built (run data/queries/create_demo_predictions.sql).",
        )
    stops = [
        DemoStop(stop_place=r.stop_place, stop_name=r.stop_name, n_calls=r.n_calls, mode=r.mode)
        for r in bq_client.query(_DEMO_STOPS_QUERY).result()
    ]
    highlights = [
        DemoHighlight(
            situation_number=r.situationNumber,
            operator=r.operator,
            mode=varsel.mode_label(r.operator),
            summary=r.summary,
            category=r.category,
            onset=r.onset_ts,
            n_cancelled=r.n_cancelled,
            n_delayed10=r.n_delayed10,
            window_start=r.window_start,
            window_end=r.window_end,
            from_stop=r.from_stop,
            from_name=r.from_name,
            to_stop=r.to_stop,
            to_name=r.to_name,
        )
        for r in bq_client.query(_DEMO_HIGHLIGHTS_QUERY).result()
    ]
    meta = DemoMeta(
        window_start=window.window_start,
        window_end=window.window_end,
        alert_threshold=alert_policy.threshold_for(None),
        stops=stops,
        highlights=highlights,
    )
    with _demo_meta_lock:
        _demo_meta_cache = (time.monotonic(), meta)
    return meta


@app.get(
    "/demo-api/config",
    summary="Which deployment is serving this page",
    response_description="The environment name for the page title; null in prd.",
    tags=["demo"],
)
async def demo_config() -> DemoConfig:
    """The environment name the demo page appends to its title, or null in prd.

    Separate from `/demo-api/meta` on purpose: meta reads the demo tables and can fail (a fresh
    environment has none copied in), and the page still renders its masthead in that state — which
    is exactly when it matters whether a partner is looking at dev or prd. This touches no BigQuery.
    """
    return DemoConfig(environment=deployment.environment_label())


@app.get(
    "/demo-api/meta",
    summary="Demo dataset metadata: date window, stops, example scenarios",
    response_description="Window, stop picker entries and one-click highlight scenarios.",
    tags=["demo"],
)
async def demo_meta() -> DemoMeta:
    """Metadata for the /demo UI's controls.

    The demo dataset is a static snapshot of the models' temporal holdout (no retraining data):
    every prediction shown was computed with only information available at the stated ask time,
    and outcomes are the realized SIRI-ET delays/cancellations. Cached in-process after the
    first call.
    """
    return get_demo_meta()


@app.get(
    "/demo-api/journeys",
    summary="Journeys between two stops with ask-time predictions vs realized outcome",
    response_description="Matching journeys, each with departure and arrival sides.",
    tags=["demo"],
)
async def demo_journeys(
    from_stop: str = Query(description="Boarding StopPlace ref (NSR:StopPlace:...)."),
    to_stop: str = Query(description="Alighting StopPlace ref (NSR:StopPlace:...)."),
    start: datetime = Query(description="Window start for the departure time (ISO 8601, naive = UTC)."),
    end: datetime = Query(description="Window end for the departure time (ISO 8601, naive = UTC)."),
) -> DemoJourneysResponse:
    """All journeys departing `from_stop` within [start, end] that later call at `to_stop`.

    For each journey, both ends carry the realized outcome and — when an SX situation flagged
    it — the calibrated predictions as they stood at message arrival, 60 min and 30 min before
    the call (each later ask time only exists when the message had arrived by then). Journeys
    no situation flagged are returned too, with `situation: null`: the models only score
    SX-flagged calls, so "no prediction" is itself the served answer.
    """
    if from_stop == to_stop:
        raise HTTPException(status_code=400, detail="from_stop and to_stop must differ.")
    start_utc = start if start.tzinfo is not None else start.replace(tzinfo=UTC)
    end_utc = end if end.tzinfo is not None else end.replace(tzinfo=UTC)
    if end_utc <= start_utc:
        raise HTTPException(status_code=400, detail="end must be after start.")
    if end_utc - start_utc > timedelta(days=7):
        raise HTTPException(status_code=400, detail="Window too wide; keep it within 7 days.")
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("from_stop", "STRING", from_stop),
            bigquery.ScalarQueryParameter("to_stop", "STRING", to_stop),
            bigquery.ScalarQueryParameter("start_ts", "TIMESTAMP", start_utc),
            bigquery.ScalarQueryParameter("end_ts", "TIMESTAMP", end_utc),
        ],
    )
    rows = bq_client.query(_DEMO_JOURNEYS_QUERY, job_config=job_config).result()
    journeys = []
    for r in rows:
        situation = None
        if r.situationNumber is not None:
            situation = DemoSituation(
                situation_number=r.situationNumber,
                summary=r.summary,
                category=r.category,
                onset=r.onset_ts,
            )
        threshold = alert_policy.threshold_for(r.operator)
        journeys.append(
            DemoJourney(
                journey_ref=f"{r.operator}:{r.jkey}",
                operator=r.operator,
                mode=varsel.mode_label(r.operator),
                alert_threshold=threshold,
                line=r.lineRef,
                situation=situation,
                departure=_demo_side(
                    r, "dep", r.from_stop, r.from_name, r.from_aimed_dep,
                    r.act_dep_cancelled, r.act_dep_delay_min, threshold,
                ),
                arrival=_demo_side(
                    r, "arr", r.to_stop, r.to_name, r.to_aimed_arr,
                    r.act_arr_cancelled, r.act_arr_delay_min, threshold,
                ),
            )
        )
    return DemoJourneysResponse(
        from_stop=from_stop,
        to_stop=to_stop,
        start=start_utc,
        end=end_utc,
        count=len(journeys),
        journeys=journeys,
    )


@app.get(
    "/health",
    summary="Liveness check",
    response_description=(
        '`{"status": "ok"}` while the poller is healthy (or never started); 503 with '
        '`{"status": "unhealthy", "reason": ...}` when it has died or its heartbeat is stale.'
    ),
    tags=["diagnostics"],
)
async def health(response: Response) -> dict[str, str]:
    """Liveness probe with the poller's pulse behind it — still touches no BigQuery.

    Both kube probes point here, so the 503 trips liveness and the pod restarts: that is the
    intended self-healing for a dead or wedged poller (KTD1), whose old failure mode was a
    healthy-looking pod serving stale caches and pushing nothing. The decision is
    :func:`_poller_health`; a process without the lifespan (tests, local dev) stays 200. The one
    `event="health_degraded"` record per 200→503 transition — not per probe, recovery re-arms —
    is the surviving forensic line, since the access log will not carry /health.
    """
    global _health_degraded_logged
    healthy, reason = _poller_health()
    if healthy:
        _health_degraded_logged = False
        return {"status": "ok"}
    if not _health_degraded_logged:
        _health_degraded_logged = True
        logger.error(
            "Health degraded: %s",
            reason,
            extra={"fields": {"event": "health_degraded", "reason": reason}},
        )
    response.status_code = 503
    return {"status": "unhealthy", "reason": reason}


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
