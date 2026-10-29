"""What the rush actually did — the fasit /rushhistorikk scores Rushvarsel's forecasts against.

Two passes, because no single source serves both same-day timeliness and training-identical
semantics:

* **Provisional**, due 09:20 and 18:20 Oslo, from ``rail_calls_planned_today`` +
  ``rail_calls_today`` — tables the poller already builds. Four known skews against the label
  (see ``togvarsel_outcomes_provisional.sql``), so the value is marked and never enters a rate.
* **Authoritative**, due after 04:00 UTC, from ``realtime_siri_et_last_recorded``, reproducing each
  scope's own label expression. This is the value that agrees with the models, and it settles the
  row.

Why not a BigQuery scheduled query, which is how ``varsel-label-daily`` does the equivalent job for
/varsel: two schedules times two environments is four transfer configs, each holding its own COPY of
the SQL — the documented drift trap — pinned to a fixed UTC time that slides an hour against Oslo
twice a year, and unable to self-heal a run it missed. ``togvarsel.refresh()`` already establishes
the wall-clock-on-the-poller-tick pattern next door, and this pass re-derives its own work set every
tick, so a missed run costs nothing.

Reads only ``disruptions.*`` and the upstream ET/NSR tables. No import of ``main`` (the package
rule): main injects the BigQuery client via :func:`configure`.

Run the checks: ``uv run --with google-cloud-bigquery python test_togvarsel_outcomes.py``
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

_PROVISIONAL_QUERY = (Path(__file__).parent / "togvarsel_outcomes_provisional.sql").read_text()
_SETTLED_QUERY = (Path(__file__).parent / "togvarsel_outcomes_settled.sql").read_text()

# The three scopes, and the bar each one's model was trained against
# (data/queries/togvarsel/09_training.sql lines 5-7). ONE dict, so the level mapping below and any
# future reader cannot spell the bar differently:
#   am      06:30-09:00  y_am10      am_t10 >= 0.10
#   amrest  07:00-09:00  y_amrest10  am_t10_rest0700 >= 0.10
#   pm      15:00-18:00  y_pm10      pm_t10 >= 0.15
BARS: dict[str, float] = {"am": 0.10, "amrest": 0.10, "pm": 0.15}
SCOPES: tuple[str, ...] = ("am", "amrest", "pm")

# Which scopes each rush owns, and when a provisional check for it is due (Oslo hours).
#
# :20 and not :05, deliberately. Both provisional sources are TTL-gated off the SX-ingestion
# rebuild rather than rebuilt every tick (main._RAIL_CALLS_TTL 15 min, main._PLANNED_TTL 60 min), so
# an earlier check routinely reads a table that stops before the end of the window it is measuring.
# The rush itself ends at 09:00 / 18:00 (togvarsel.RUSH_END_H).
_PROVISIONAL_DUE: tuple[tuple[float, tuple[str, ...]], ...] = (
    (9.0 + 20 / 60, ("am", "amrest")),
    (18.0 + 20 / 60, ("pm",)),
)

# The authoritative pass reads last_recorded, whose one daily rewrite lands at ~05:03 Oslo: ~03:04 UTC
# in summer (measured 2026-09-01), ~04:03 UTC in winter (2026-10-29). 04:00 UTC was chosen in summer,
# far enough after the batch to be complete, early enough that a morning verdict is there by the time
# anybody looks; in winter it falls just BEFORE the batch. varsel-label-daily moved to 05:30 Oslo on
# 2026-10-29 for that reason (label_varsel_decisions.sql); this gate has not.
_SETTLED_DUE_UTC_HOUR = 4

# Matching label_varsel_decisions.sql: an outcome still inside this window is being retried, past it
# the attempts have given up and /rushhistorikk reports `ukjent`.
_RETRY_DAYS = 10

# How far back the authoritative pass reaches for days that have NO outcome row at all — the
# forecasts issued before this feature existed, in each environment once, and any day a long poller
# outage skipped entirely. Wider than the retry window on purpose: the window bounds how long a day
# whose batch never lands keeps being re-read, and a row-less day inside this horizon is re-read
# daily until it ages out, at 32.5 MB a read. Bounded so that cannot be forever. Sized to cover the
# forecast log's whole history when the page shipped (2026-09-02: three-plus weeks) with margin.
_BACKFILL_DAYS = 60

# The settled SELECT runs over a calendar span, and at 32.5 MB per day a 60-day backfill in one job
# is ~2 GB — at `_MAX_BYTES_BILLED`, so the first pass in a fresh environment would fail every
# retry and never settle anything. Spans wider than this are cut into consecutive chunks, one
# SELECT each: 14 days plus the query's own ±1-day reach is ~16 partitions, ~520 MB, a quarter of
# the bound.
_SETTLED_CHUNK_DAYS = 14

# The training chain's own eligibility test (09_training.sql): `am_n > 300 AND pm_n > 300`, which
# drops strike days and data gaps. A day below it has a realized share that means nothing.
#
# DAY-LEVEL, not per scope, and that distinction is load-bearing. 09_training.sql has no `amrest`
# term at all, and `amrest`'s window is a strict subset of `am`'s — so a per-scope test would mark
# the 0700 issue `ukjent` on days the chain scored happily.
_MIN_CALLS = 300

# A blast-radius bound on an unattended query against an upstream ET table, the reason
# meldinger._MAX_BYTES_BILLED exists. NOT a pruning check: a too-narrow date bound reads as a
# CHEAPER query, so cost can never detect it. Measured 32.5 MB for one settled day and 3.4 MB for a
# provisional run, so 2 GiB is ~60x the widest expected read.
# 6 GiB since 2026-09-21: the settled query reads the span plus 35 days of VYG GTFS for the
# planned-trains term (docs/rushvarsel/04-cuts), ~2 GB per 14-day chunk where it was 32 MB a day.
_MAX_BYTES_BILLED = 6 * 1024**3

_FAIL_COOLDOWN = timedelta(minutes=15)
_MAX_FAILS_PER_DAY = 8

_DDL = """
CREATE TABLE IF NOT EXISTS `disruptions.togvarsel_outcomes` (
  d DATE OPTIONS(description="Oslo date of the rush."),
  scope STRING OPTIONS(description="am (06:30-09:00) | amrest (07:00-09:00) | pm (15:00-18:00) — the window the issue's own model was trained on."),
  trouble10 FLOAT64 OPTIONS(description="Share of calls cancelled OR >= 10 min late, over resolved calls. The product bar. Since 2026-09-21 the planned calls of trains that never ran (pulled from ET without a cancellation, or cut from the day's plan) count as cancelled on both sides — docs/rushvarsel/04-cuts."),
  level STRING OPTIONS(description="green below the scope's bar, yellow to twice the bar, red at or above. Non-green coincides exactly with the training label being true."),
  n_resolved INT64 OPTIONS(description="Calls with a known outcome — trouble10's denominator."),
  n_unresolved INT64 OPTIONS(description="Calls neither cancelled nor with an actual time."),
  n_calls INT64 OPTIONS(description="All calls in the window. Eligibility reads this."),
  eligible BOOL OPTIONS(description="The training chain's day-level test (am and pm both > 300 calls). NULL on a provisional row, whose counts are not the same quantity."),
  settled BOOL OPTIONS(description="TRUE once confirmed against last_recorded. The stats table reads settled rows only."),
  unresolvable BOOL OPTIONS(description="last_recorded had no resolved call for the window, so no retry can help — reported as ukjent immediately."),
  provisional_trouble10 FLOAT64 OPTIONS(description="What the same-day pass measured. Never overwritten by the settled pass: the overnight disagreement rate is the measurement that decides whether that pass earns its keep."),
  provisional_level STRING OPTIONS(description="The level the page showed at 09:20 / 18:20."),
  provisional_at TIMESTAMP OPTIONS(description="When the provisional pass ran."),
  resolved_at TIMESTAMP OPTIONS(description="When the authoritative pass settled the row.")
)
PARTITION BY d
OPTIONS(description="Realized rush outcome per (Oslo day, scope) for /rushhistorikk. Never CREATE OR REPLACE, and never copied dev->prd: each environment resolves the forecasts it issued itself.")
"""

# Provisional: fills the provisional_* trio AND the served value, so the page has something to show
# after the rush. eligible stays NULL — see _MIN_CALLS. Never touches a settled row.
_PROVISIONAL_MERGE = """
MERGE `disruptions.togvarsel_outcomes` t
USING (SELECT @d AS d, @scope AS scope) s
ON t.d = s.d AND t.scope = s.scope
WHEN MATCHED AND NOT IFNULL(t.settled, FALSE) THEN UPDATE SET
  trouble10 = @trouble10, level = @level,
  n_resolved = @n_resolved, n_unresolved = @n_unresolved, n_calls = @n_calls,
  provisional_trouble10 = @trouble10, provisional_level = @level, provisional_at = @now
WHEN NOT MATCHED THEN
  INSERT (d, scope, trouble10, level, n_resolved, n_unresolved, n_calls, eligible, settled,
          unresolvable, provisional_trouble10, provisional_level, provisional_at, resolved_at)
  VALUES (@d, @scope, @trouble10, @level, @n_resolved, @n_unresolved, @n_calls, NULL, FALSE,
          FALSE, @trouble10, @level, @now, NULL)
"""

# Authoritative: overwrites the served value and settles the row, LEAVING provisional_* alone.
_SETTLED_MERGE = """
MERGE `disruptions.togvarsel_outcomes` t
USING (SELECT @d AS d, @scope AS scope) s
ON t.d = s.d AND t.scope = s.scope
WHEN MATCHED AND NOT IFNULL(t.settled, FALSE) THEN UPDATE SET
  trouble10 = @trouble10, level = @level,
  n_resolved = @n_resolved, n_unresolved = @n_unresolved, n_calls = @n_calls,
  eligible = @eligible, settled = TRUE, unresolvable = @unresolvable, resolved_at = @now
WHEN NOT MATCHED THEN
  INSERT (d, scope, trouble10, level, n_resolved, n_unresolved, n_calls, eligible, settled,
          unresolvable, provisional_trouble10, provisional_level, provisional_at, resolved_at)
  VALUES (@d, @scope, @trouble10, @level, @n_resolved, @n_unresolved, @n_calls, @eligible, TRUE,
          @unresolvable, NULL, NULL, NULL, @now)
"""

# The work set: days we issued a forecast for that are not fully settled yet. Derived every tick
# rather than tracked, which is what makes a missed run self-heal. Bounded to days strictly before
# today, because last_recorded carries no complete day for today.
#
# Two reaches, and they answer different questions. Inside the RETRY window, any day not fully
# settled: the provisional rows live here, and a day the batch had not covered yet is re-read until
# the window closes. Beyond it, out to the BACKFILL horizon, only days with NO outcome row at all —
# the pre-feature backlog, or a day a long outage skipped — because a day with rows there was either
# settled or deliberately marked unresolvable, and re-reading either re-runs an immutable query to
# reach the same answer.
_WORK_SET_SQL = """
WITH issued AS (
  SELECT DISTINCT d FROM `disruptions.togvarsel_issued`
  WHERE d BETWEEN DATE_SUB(@today, INTERVAL @backfill_days DAY) AND DATE_SUB(@today, INTERVAL 1 DAY)
),
resolved AS (
  SELECT d, COUNTIF(IFNULL(settled, FALSE)) AS n_settled
  FROM `disruptions.togvarsel_outcomes`
  WHERE d BETWEEN DATE_SUB(@today, INTERVAL @backfill_days DAY) AND DATE_SUB(@today, INTERVAL 1 DAY)
  GROUP BY d
)
SELECT i.d
FROM issued AS i
LEFT JOIN resolved AS r USING (d)
WHERE
  (i.d >= DATE_SUB(@today, INTERVAL @retry_days DAY) AND IFNULL(r.n_settled, 0) < @n_scopes)
  OR (i.d < DATE_SUB(@today, INTERVAL @retry_days DAY) AND r.d IS NULL)
ORDER BY i.d
"""

_bq_client: Any = None
_table_ready = False
# Per-Oslo-day state: which passes have run, and the failure backoff. Poller-thread only, same
# discipline as togvarsel's _state, so no lock.
_state: dict[str, Any] = {}

# The authoritative pass's once-per-day latch, and it lives OUT HERE on purpose: `_state` is scoped
# to the Oslo day (a rush is an Oslo-clock thing) while this gate is a UTC hour (the batch it waits
# for rewrites at ~03:04 UTC). Held as a boolean inside `_state`, the two calendars shared one dict
# and the Oslo rollover re-armed the UTC gate at 22:00/23:00 UTC — five hours before the batch. Two
# calendars, two variables.
_settled_last_utc: date | None = None


def configure(*, bq_client: Any) -> None:
    """Wire in the BigQuery client. Called once from main.lifespan."""
    global _bq_client
    _bq_client = bq_client


def level_for(scope: str, trouble10: float | None) -> str | None:
    """green below the scope's bar, yellow to twice the bar, red at or above. None for None.

    Only the GREEN boundary follows from the models: the bar IS the training label's threshold, so a
    non-green level coincides exactly with the label being true and the page splits "bad" into two
    severities rather than inventing a second definition of it. Twice-the-bar is a display
    convention chosen for class balance — measured over 345 non-holiday weekdays it gives
    82.0/11.6/6.4 % for the morning and 82.9/12.8/4.3 % for the afternoon.

    NULL is outside this mapping's domain, not a fourth level: trouble10 is NULL exactly when no
    call in the window resolved, and a day with no evidence is `ukjent`, never green.
    """
    if trouble10 is None:
        return None
    bar = BARS[scope]
    if trouble10 < bar:
        return "green"
    if trouble10 < 2 * bar:
        return "yellow"
    return "red"


def eligible_for(counts: dict[str, int]) -> bool:
    """The training chain's day-level test, given {scope: n_calls} for one day.

    `amrest` inherits the day's verdict rather than being tested on its own window: 09_training.sql
    thresholds `am_n` and `pm_n` only, and `amrest`'s window is a strict subset of `am`'s.
    """
    return counts.get("am", 0) > _MIN_CALLS and counts.get("pm", 0) > _MIN_CALLS


def provisional_due(now_h: float, done: set[str]) -> tuple[str, ...]:
    """Scopes whose provisional check is due at `now_h` Oslo and has not run today."""
    due: list[str] = []
    for at_h, scopes in _PROVISIONAL_DUE:
        if now_h >= at_h:
            due.extend(s for s in scopes if s not in done)
    return tuple(due)


def settled_due(now: datetime, last_run_utc: date | None) -> bool:
    """Whether the authoritative pass is due: at or after 04:00 UTC on a UTC day it has not run.

    KEYED ON THE UTC DATE, and that is the whole point. The gate is a UTC hour because the batch it
    waits for is (last_recorded rewrites at ~03:04 UTC), but the module's other state is scoped to
    the OSLO day because a rush is an Oslo-clock thing. Holding "has it run?" as a boolean inside
    the Oslo-day state put those two calendars in one dict, and the result was not a near miss:
    at Oslo midnight — 22:00 UTC in CEST — the Oslo rollover cleared the flag while
    `hour >= 4` was trivially true, so the pass ran about five hours BEFORE the batch it depends on
    landed, found nothing, and settled every day as `unresolvable` — permanently, and with no error
    anywhere. The two calendars now live in two variables instead of one.
    """
    now_utc = now.astimezone(UTC)
    return now_utc.hour >= _SETTLED_DUE_UTC_HOUR and last_run_utc != now_utc.date()


def _blank_state(d: date) -> dict[str, Any]:
    """The Oslo-day-scoped state. Note what is NOT here: the authoritative pass's last-run date,
    which is UTC-scoped and must survive the Oslo rollover (see settled_due)."""
    return {"date": d, "provisional_done": set(), "backoff": {}, "forecast_day": False}


# Has a forecast been issued for today? One tiny lookup, and the provisional pass's population test:
# the settled pass resolves ISSUED days only (see _WORK_SET_SQL), so a provisional row for a day no
# forecast was issued on — a weekend, a holiday, a morning the issue failed — is a row nothing ever
# settles. Measured in prd on 2026-09-10: Saturday 5 and Sunday 6 September each carried three
# `settled = FALSE` rows the page could never join to, and every weekend would have added two more.
_ISSUED_TODAY_SQL = """
SELECT 1 AS issued FROM `disruptions.togvarsel_issued` WHERE d = @today LIMIT 1
"""


def _forecast_day(today: date, now: datetime, scopes: tuple[str, ...]) -> bool:
    """Whether the provisional pass has anything to measure today.

    A weekend is answered without a query — no forecast is ever issued on one. A weekday asks
    `togvarsel_issued` once and remembers a yes for the rest of the Oslo day; a no is re-asked when
    the next window comes due, because a morning whose issues all failed can still issue at 12:00
    and 14:00, and the afternoon check is then owed. Two probes on a holiday, one on a weekday.
    """
    if now.weekday() >= 5:
        return False
    if _state.get("forecast_day"):
        return True
    rows = list(
        _bq_client.query(_ISSUED_TODAY_SQL, job_config=_job_config([_scalar("today", "DATE", today)]))
        .result(timeout=60)
    )
    if rows:
        _state["forecast_day"] = True
        return True
    logger.info(
        "togvarsel_outcomes: no forecast issued for %s by %s; skipping provisional %s",
        today,
        now.strftime("%H:%M"),
        ",".join(scopes),
        extra={"fields": {"event": "togvarsel_outcomes_skip", "day": str(today), "scopes": list(scopes)}},
    )
    return False


def _retryable(key: str, now: datetime) -> bool:
    b = _state["backoff"].get(key)
    return b is None or now >= b["until"]


def _note_failure(key: str, now: datetime) -> None:
    """Back off a failing pass, and give up for the day at _MAX_FAILS_PER_DAY.

    Same shape as togvarsel._note_failure: the state resets at the Oslo day rollover, so
    `datetime.max` means "not again today" rather than "never".
    """
    b = _state["backoff"].setdefault(key, {"fails": 0, "until": now})
    b["fails"] += 1
    if b["fails"] >= _MAX_FAILS_PER_DAY:
        b["until"] = datetime.max.replace(tzinfo=_OSLO)
        logger.error("togvarsel_outcomes: %s failed %d times; giving up for today", key, b["fails"])
    else:
        b["until"] = now + _FAIL_COOLDOWN


def ensure_table() -> None:
    """Create disruptions.togvarsel_outcomes if absent. Called once from main.lifespan.

    Not left to the first write: /rushhistorikk LEFT JOINs this table on every request, and a
    missing table is a BigQuery error, so the page would 503 in any environment where no rush has
    resolved yet — every environment, on the day this ships. Failure is logged, never raised.
    """
    global _table_ready
    if _bq_client is None or _table_ready:
        return
    try:
        _bq_client.query(_DDL).result(timeout=60)
        _table_ready = True
    except Exception:
        logger.exception("togvarsel_outcomes: creating the table failed; retrying on write")


def _ensure_table_ready() -> None:
    """Create the table if the startup attempt failed. Called before each pass's first write.

    ensure_table()'s own log says "retrying on write", and until this existed that was simply untrue:
    a BigQuery blip during startup left the table absent until someone redeployed. Its sibling
    togvarsel_notify._record_round has always carried this latch fallback; this is the same shape.
    """
    global _table_ready
    if _table_ready:
        return
    _bq_client.query(_DDL).result(timeout=60)
    _table_ready = True


def _job_config(params: list) -> Any:
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=_MAX_BYTES_BILLED
    )


def _scalar(name: str, type_: str, value: Any) -> Any:
    from google.cloud import bigquery

    return bigquery.ScalarQueryParameter(name, type_, value)


def _outcome_scalars(row_d, scope, trouble10, n_resolved, n_unresolved, n_calls, now) -> list:
    """The eight parameters both MERGEs share. BigQuery binds @name by name, not by position, so a
    shared builder plus per-pass extras is exactly equivalent to spelling both lists out."""
    return [
        _scalar("d", "DATE", row_d),
        _scalar("scope", "STRING", scope),
        _scalar("trouble10", "FLOAT64", trouble10),
        _scalar("level", "STRING", level_for(scope, trouble10)),
        _scalar("n_resolved", "INT64", n_resolved),
        _scalar("n_unresolved", "INT64", n_unresolved),
        _scalar("n_calls", "INT64", n_calls),
        _scalar("now", "TIMESTAMP", now),
    ]


def _run_provisional(day: date, scopes: tuple[str, ...], now: datetime) -> set[str]:
    """One same-day pass over `scopes`, writing the provisional trio and the served value.

    Returns the scopes the query actually answered for, which is what `refresh` marks done. It
    returned nothing from 2026-09-02 to 2026-09-10 while the caller iterated the result: every pass
    wrote its rows and then raised, so the retry loop re-ran the same SELECT and MERGEs every 15
    minutes to the give-up, and the 18:20 window — with nothing marked done — re-wrote the morning's
    rows on top, so `provisional_at` read ~20:10 on every weekday instead of 09:20.
    """
    from google.cloud import bigquery

    _ensure_table_ready()
    cfg = _job_config(
        [
            _scalar("day", "DATE", day),
            bigquery.ArrayQueryParameter("scopes", "STRING", list(scopes)),
        ]
    )
    rows = list(_bq_client.query(_PROVISIONAL_QUERY, job_config=cfg).result(timeout=120))
    for row in rows:
        scope = row["scope"]
        trouble10 = row["trouble10"]
        _bq_client.query(
            _PROVISIONAL_MERGE,
            job_config=_job_config(
                _outcome_scalars(
                    row["d"], scope, trouble10,
                    row["n_resolved"], row["n_unresolved"], row["n_calls"], now,
                )
            ),
        ).result(timeout=60)
    answered = {row["scope"] for row in rows}
    logger.info(
        "togvarsel_outcomes: provisional %s for %s — %d rows",
        ",".join(scopes),
        day,
        len(rows),
        extra={
            "fields": {
                "event": "togvarsel_outcomes_pass",
                "pass": "provisional",
                "day": str(day),
                "scopes": list(scopes),
                "rows": len(rows),
            }
        },
    )
    return answered


def _run_settled(days: Sequence[date], now: datetime) -> None:
    """The authoritative pass over every day in the work set: all three scopes, settling each row.

    ONE SELECT per span of up to `_SETTLED_CHUNK_DAYS` calendar days, not one per day. The query
    takes a real range (BETWEEN, GROUP BY d, scope) and this is the caller that needs it: the work
    set is normally a single day, but it is a backlog exactly once per environment —
    `togvarsel_issued` already holds forecasts issued before this feature existed, and the
    `_BACKFILL_DAYS` horizon reaches them — and again after any poller outage longer than a day. At
    one SELECT plus three MERGEs per day that was up to ~28 sequential BigQuery jobs in a single
    tick, and this runs on the SOLE poller thread, after the SX rebuild and the notify pass, so the
    cost is not /rushhistorikk's latency but a poller tick that goes quiet for tens of seconds — on
    the backlog pass, a few minutes, once. The chunking is a BYTES bound, not a latency one: the
    whole backfill horizon in one SELECT sits at `_MAX_BYTES_BILLED` and would fail every retry.

    The MERGE loop stays per (day, scope) and iterates the WORK SET, never the scanned span: a day
    inside min..max that already settled, or a weekend with no issued forecast, must not get a row.
    Re-reading an already-settled day is harmless anyway — the MERGE guard is
    `WHEN MATCHED AND NOT settled` — but the loop is what keeps "a day with no issued forecast is
    never resolved" true.

    A scope the query returns no row for is still settled, as `unresolvable` — but ONLY when some
    other scope did return one. That is the discriminator between the two causes of an empty read,
    and it is the whole reason the retry window exists. With a sibling scope resolved, the batch has
    demonstrably landed for that day and "no resolved call in this window" is final: last_recorded
    is immutable, so retrying for ten days re-runs one query to reach the same NULL. With NO scope
    resolved, the likeliest cause is that the batch has not landed for that day at all, and writing
    three `unresolvable` rows would burn the day permanently on the strength of one early read —
    `resolve_outcome_state` reads unresolvable BEFORE the window, so nothing ever revisits it. Such
    a day is therefore skipped entirely: it stays in the work set, reads `venter`, is retried each
    day, and turns `ukjent` at day 11 on its own.
    """
    if not days:
        return
    _ensure_table_ready()
    for chunk in _spans(days, _SETTLED_CHUNK_DAYS):
        cfg = _job_config(
            [_scalar("from_date", "DATE", chunk[0]), _scalar("to_date", "DATE", chunk[-1])]
        )
        by_day: dict[date, dict[str, Any]] = {}
        for row in _bq_client.query(_SETTLED_QUERY, job_config=cfg).result(timeout=180):
            by_day.setdefault(row["d"], {})[row["scope"]] = row
        for day in chunk:
            _settle_day(day, by_day.get(day, {}), now)


def _spans(days: Sequence[date], max_span_days: int) -> list[list[date]]:
    """The work set cut into consecutive runs each covering fewer than `max_span_days` calendar
    days, sorted. By calendar span and not by count: the work set holds weekdays only, so a count
    would let a chunk stretch over more partitions than it has days."""
    spans: list[list[date]] = []
    current: list[date] = []
    for day in sorted(days):
        if current and (day - current[0]).days >= max_span_days:
            spans.append(current)
            current = []
        current.append(day)
    if current:
        spans.append(current)
    return spans


def _settle_day(day: date, rows: dict[str, Any], now: datetime) -> None:
    """Write one day's three scopes from the rows the range SELECT returned for it.

    A day no scope resolved for writes NOTHING — see `_run_settled` on why that is not the same
    answer as "unresolvable".
    """
    if not rows:
        logger.info(
            "togvarsel_outcomes: %s returned no resolved calls; leaving it for the retry window",
            day,
            extra={
                "fields": {
                    "event": "togvarsel_outcomes_pass",
                    "pass": "settled",
                    "day": str(day),
                    "rows": 0,
                }
            },
        )
        return
    eligible = eligible_for({scope: (rows[scope]["n_calls"] or 0) for scope in rows})
    for scope in SCOPES:
        row = rows.get(scope)
        trouble10 = row["trouble10"] if row is not None else None
        _bq_client.query(
            _SETTLED_MERGE,
            job_config=_job_config(
                _outcome_scalars(
                    day, scope, trouble10,
                    row["n_resolved"] if row else 0,
                    row["n_unresolved"] if row else 0,
                    row["n_calls"] if row else 0,
                    now,
                )
                + [
                    _scalar("eligible", "BOOL", eligible),
                    _scalar("unresolvable", "BOOL", trouble10 is None),
                ]
            ),
        ).result(timeout=60)
    logger.info(
        "togvarsel_outcomes: settled %s — %d of %d scopes resolved, eligible=%s",
        day,
        len(rows),
        len(SCOPES),
        eligible,
        extra={
            "fields": {
                "event": "togvarsel_outcomes_pass",
                "pass": "settled",
                "day": str(day),
                "rows": len(rows),
                "eligible": eligible,
            }
        },
    )


def _work_set(today: date) -> list[date]:
    """Past days we issued a forecast for whose outcome is not fully settled."""
    from google.cloud import bigquery

    cfg = _job_config(
        [
            _scalar("today", "DATE", today),
            bigquery.ScalarQueryParameter("retry_days", "INT64", _RETRY_DAYS),
            bigquery.ScalarQueryParameter("backfill_days", "INT64", _BACKFILL_DAYS),
            bigquery.ScalarQueryParameter("n_scopes", "INT64", len(SCOPES)),
        ]
    )
    rows = _bq_client.query(_WORK_SET_SQL, job_config=cfg).result(timeout=60)
    return [row["d"] for row in rows]


def refresh(now: datetime | None = None) -> None:
    """One tick of both passes. Called from the poller's wall-clock path, beside togvarsel.refresh.

    Wall-clock, not ingestion-driven: a rush ends at its hour whether or not SX moved. Every failure
    is contained per pass, so a dead provisional query cannot stop the authoritative one.
    """
    if _bq_client is None:
        return
    now = (now or datetime.now(UTC)).astimezone(_OSLO)
    today = now.date()
    if _state.get("date") != today:
        _state.clear()
        _state.update(_blank_state(today))
    now_h = now.hour + now.minute / 60 + now.second / 3600

    global _settled_last_utc
    due = provisional_due(now_h, _state["provisional_done"])
    # Keyed on the WINDOW, not on the pass. One "provisional" budget meant a morning outage that
    # burned the eight-failure give-up silently forfeited the afternoon check too, on a day when
    # nothing was wrong by 18:20.
    due_key = f"provisional:{'+'.join(due)}"
    if due and _retryable(due_key, now):
        try:
            if _forecast_day(today, now, due):
                answered = _run_provisional(today, due, now)
                # Only a scope the query actually answered for is done. Marking an unanswered scope
                # done would retire it for the rest of the Oslo day on the strength of one empty
                # read — and the page would show `venter` until the overnight pass, with nothing
                # retrying.
                _state["provisional_done"].update(answered)
            else:
                # A day with no forecast has nothing to measure, and the settled pass will never
                # visit it either: marking the due scopes done is what keeps a weekend from
                # writing rows nothing settles. The next window re-asks (see _forecast_day).
                _state["provisional_done"].update(due)
            _state["backoff"].pop(due_key, None)
        except Exception:
            logger.exception("togvarsel_outcomes: provisional pass failed; retrying after backoff")
            _note_failure(due_key, now)

    if settled_due(now, _settled_last_utc) and _retryable("settled", now):
        try:
            _run_settled(_work_set(today), now)
            _settled_last_utc = now.astimezone(UTC).date()
            _state["backoff"].pop("settled", None)
        except Exception:
            logger.exception("togvarsel_outcomes: settled pass failed; retrying after backoff")
            _note_failure("settled", now)
