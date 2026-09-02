"""Notification policy for togvarsel pushes — when a rush-hour forecast becomes a Web Push.

Togvarsel (togvarsel.py) issues four green/yellow/red prognoses per weekday: 0630 and 0700 for
the morning rush, 1200 and 1400 for the afternoon. This module decides which of those rounds
notify /varsel subscribers. The policy core below is pure functions over plain data — issue
dicts, a marker map, an explicit Oslo-aware `now` — so a decision is a function of its inputs
rather than of anything ambient. That is NOT the same as being reconstructible after the fact, and
the Round Record section below is where the difference is argued: what the markers do not preserve
is which rounds were never evaluated at all, so every round is recorded to
`disruptions.togvarsel_outcomes`' sibling table as it is decided.

The gates, in order, per round:

    timeliness   a round notifies only while its message is still useful, strictly before an
                 Oslo wall-clock deadline: 0630 < 07:00, 0700 < 09:00, 1200 < 14:00, 1400 < 18:00.
                 A restart back-fill evaluated past the deadline is recorded as suppressed and
                 never revisited. Judged at evaluation time, which subsumes togvarsel's `late`
                 flag — the policy never reads it.
    baselines    0630 and 1200 push iff the level is yellow or red.
    follow-ups   0700 and 1400 push iff the level differs from the EFFECTIVE baseline: the level
                 their baseline round pushed, or green when it pushed nothing (not issued,
                 suppressed, or green). A significant change is a level transition, never a
                 probability delta — the paired models answer different questions with different
                 calibration, so raw p is not comparable across them; the level is. A green
                 follow-up after a pushed yellow/red baseline is the one green that pushes: the
                 all-clear ("friskmelding"), the trust-building update after a warning.

Round markers `{issue: {"level": ..., "pushed": ...}}` are the single state primitive: dedup (at
most one evaluation per round, ever) and the follow-up baseline in one. The caller owns date
scoping and persistence. Within one pass a round's marker is committed before the next round is
decided, so a restart that back-fills a baseline and issues its follow-up in the same tick
resolves deterministically. Each round is decided in its own try/except: a failure records no
marker (the round is retried next tick, bounded by its deadline) and never stops later rounds.

Below the policy core sits the matching layer: which registrations a pushing round affects
and which devices get the push, deduped. The geography is `disruptions.togvarsel_oslo_stations`
— fetched once per Oslo day through the injected BigQuery client and never duplicated in Python
(that table DEFINES "the Oslo area" for the whole togvarsel chain). A journey is affected
iff both alias-expanded endpoints are in that set, today's Oslo weekday is in its `days`, and its
stored-inclusive [begin, end] window overlaps — compared half-open — the LIVE part of the issue's
scope window, [max(scope start, evaluation time), scope end): a push about a rush window the
traveller has already exited is noise, friskmelding included.

On top of matching sits composition and delivery. The wording is deterministic Norwegian
assembled from the issue's own fields — level, scope text, rule-based reasons — never an LLM
 and never a journey-, line- or corridor-specific claim: the model's grain is the whole
network, and the wording keeps that honest. Each pushing round sends one push per matched device
 through varsel's own delivery machinery; a send error is counted and logged, never retried
within the round — the marker is already committed, and a forecast is lower-stakes than a
train alert.

At the top sits :func:`evaluate`, the per-tick orchestration main calls right after
togvarsel.refresh() on the wall-clock tick: gates first — while the delivery flag is off or VAPID
is unset, NOTHING is evaluated and no markers are written, so an enable-restart inside a
timeliness window still delivers — then the station set (unavailable → no markers), then
decide_rounds over the injected issued-today snapshot, then match + push per pushing round, with
the marker map persisted through the injected store accessor after each round's delivery
and the whole pass wrapped so one failure never breaks the poller tick.

Beside all of that sits one read-only surface, :func:`rush_outlook`: the same matching and the
same supersession, asked per device and answered without sending anything, so /varsel can show the
levels a traveller is being notified about at the top of its page. It reuses the poller's station
cache rather than fetching (:func:`cached_station_set`) and never queries BigQuery, and it differs
from the push policy in exactly one deliberate way — the window is the whole rush rather than the
part still ahead of now (see :func:`affected_rushes`).

All time logic is Europe/Oslo wall-clock. No import of main, varsel, or togvarsel (the package
rule): runtime dependencies are injected — main passes the BigQuery client and evaluate()'s seams
via :func:`configure` (togvarsel's own idiom), the matcher takes varsel's `place_refs` as a plain
callable, and :func:`push_round` takes varsel's delivery seams (device_subscription,
deliver_to_devices, handle_gone_device, the brand-asset table, the environment tagger) the same
way; the policy core needs none. Two HTTP surfaces, both on a router main mounts:
GET /togmelding-api/outlook (the panel above) and the dev-only test trigger at the bottom, which
refuses everything but the dev deployment and fires one synthetic round through the same
composition + delivery seams without ever touching the markers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from deployment import environment_label
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

# togvarsel's level vocabulary; anything else is a malformed round and is refused loudly.
_LEVELS = ("green", "yellow", "red")

# Blast-radius bound on the round-record MERGE, matching rushhistorikk._MAX_BYTES_BILLED.
_MAX_BYTES_BILLED = 2 * 1024**3


@dataclass(frozen=True)
class _NotifySpec:
    key: str  # togvarsel issue key, "0630" — also its display time "06:30"
    due_h: float  # Oslo fractional hour the issue becomes computable — the evaluation order
    deadline_h: float  # notify only strictly BEFORE this Oslo wall-clock hour
    baseline: str | None  # the round a follow-up is compared against; None for baselines
    scope_start_h: float  # the rush window the forecast is ABOUT — what journeys are
    scope_end_h: float  # matched against, distinct from due/deadline (when the push may go out)


# The notification companion to togvarsel.ISSUE_SPECS: one row per issue, keyed the same way.
# Deadlines are where the message stops being useful — 0630 is superseded by 0700 at 07:00,
# 0700 by the rush ending at 09:00, 1200 by 1400 at 14:00, 1400 by the rush ending at 18:00.
# Scopes mirror the forecasts' own windows (the PM issues are both about the 15:00–18:00 rush,
# issued at 12:00 and 14:00 — advance warning is the product's point).
NOTIFY_SPECS: tuple[_NotifySpec, ...] = (
    _NotifySpec("0630", 6.5, 7.0, None, 6.5, 9.0),
    _NotifySpec("0700", 7.0, 9.0, "0630", 7.0, 9.0),
    _NotifySpec("1200", 12.0, 14.0, None, 15.0, 18.0),
    _NotifySpec("1400", 14.0, 18.0, "1200", 15.0, 18.0),
)
_SPEC_BY_KEY = {s.key: s for s in NOTIFY_SPECS}


@dataclass(frozen=True)
class Decision:
    """One round's outcome. `push` with kind "warning" | "update" | "all_clear", or suppression
    with reason "untimely" | "no_change" | "green_baseline". `marker` is the entry the round
    commits either way: {"level": ..., "pushed": ...} — its presence is what makes the round
    never re-evaluated, so even a suppressed round records one."""

    issue: str
    level: str
    push: bool
    marker: dict[str, Any]
    kind: str | None = None  # set iff push
    reason: str | None = None  # set iff not push
    # The level this round's baseline left in force (_effective_baseline), or None for a baseline
    # round. Carried on the Decision rather than recomputed later because /rushhistorikk grades an
    # unsent follow-up on the level the traveller actually holds, not on the forecast nobody saw —
    # and recording it here is what keeps that off a second row's survival (see _record_round).
    baseline_level: str | None = None


def _marker(level: str, pushed: bool) -> dict[str, Any]:
    return {"level": level, "pushed": pushed}


def _effective_baseline(spec: _NotifySpec, markers: Mapping[str, Any]) -> str:
    """The level the baseline round pushed, or green when it pushed nothing — what a follow-up's
    level must differ from to count as significant change."""
    m = markers.get(spec.baseline)
    if m and m.get("pushed"):
        return str(m["level"])
    return "green"


def decide_issue(issue: Mapping[str, Any], markers: Mapping[str, Any], now: datetime) -> Decision:
    """The per-round policy as a pure function; mutates nothing. Raises on a malformed round
    (unknown issue key or level) — decide_rounds turns that into "no marker, retry next tick"."""
    key = str(issue["issue"])
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown togvarsel issue {key!r}")
    level = issue["level"]
    if level not in _LEVELS:
        raise ValueError(f"issue {key}: unknown level {level!r}")

    # Resolved before the deadline check, not inside the change branch: an UNTIMELY follow-up
    # leaves its baseline's warning standing, and /rushhistorikk grades it on that level. Pure
    # read of the markers, so hoisting it changes no decision.
    base = _effective_baseline(spec, markers) if spec.baseline is not None else None

    now = now.astimezone(_OSLO)
    now_h = now.hour + now.minute / 60 + now.second / 3600
    if now_h >= spec.deadline_h:  # strictly-before cutoff: 07:00:00 sharp is already too late
        return Decision(
            key,
            level,
            push=False,
            marker=_marker(level, False),
            reason="untimely",
            baseline_level=base,
        )

    if spec.baseline is None:
        if level == "green":
            return Decision(
                key, level, push=False, marker=_marker(level, False), reason="green_baseline"
            )
        return Decision(key, level, push=True, marker=_marker(level, True), kind="warning")

    assert base is not None  # spec.baseline is set, so the hoisted resolution ran
    if level == base:
        return Decision(
            key,
            level,
            push=False,
            marker=_marker(level, False),
            reason="no_change",
            baseline_level=base,
        )
    if level == "green":
        kind = "all_clear"  # dropped back to green after a pushed warning
    elif base == "green":
        kind = "warning"  # the baseline pushed nothing, so there is nothing to "update"
    else:
        kind = "update"  # yellow <-> red transition against a pushed baseline
    return Decision(
        key, level, push=True, marker=_marker(level, True), kind=kind, baseline_level=base
    )


def _due_order(issue: Mapping[str, Any]) -> tuple[float, str]:
    spec = _SPEC_BY_KEY.get(str(issue.get("issue")))
    return (spec.due_h if spec else float("inf"), str(issue.get("issue")))


def decide_rounds(
    issues: Iterable[Mapping[str, Any]],
    markers: MutableMapping[str, Any],
    now: datetime,
) -> list[Decision]:
    """Decide every unmarked round in one pass, in due-time order regardless of input order.

    Commits each round's marker into `markers` (in place) before the next round is decided, so a
    same-tick follow-up reads the just-decided baseline marker — the restart case that back-fills
    0630 and issues 0700 in one tick resolves deterministically. A round that raises is
    logged with its issue context, records no marker (so the next tick retries it, bounded by the
    timeliness deadline), and never stops the later rounds. Returns the decisions made this
    pass; already-marked rounds are skipped entirely (a completed round never re-fires)."""
    decisions: list[Decision] = []
    for issue in sorted(issues, key=_due_order):
        try:
            key = str(issue["issue"])
            if key in markers:
                continue
            decision = decide_issue(issue, markers, now)
        except Exception:
            logger.exception(
                "togvarsel_notify: deciding issue %r failed; no marker recorded, retried next tick",
                issue.get("issue") if isinstance(issue, Mapping) else issue,
            )
            continue
        markers[decision.issue] = decision.marker
        decisions.append(decision)
    return decisions


# ---- Round record -----------------------------------------------------------------------
#
# What /rushhistorikk scores. The round markers in the /varsel state blob are the runtime
# primitive, but `varsel.store_togvarsel_rounds` keeps EXACTLY ONE DATE — the first write after
# midnight destroys yesterday's — so they are not a history. This table is, and it records every
# round including the ones that decided NOT to push, plus the reason: a suppressed round is a real
# outcome for the traveller, and a page that cannot see it has to guess.
#
# It is not re-derivable AS A RECORD. `decide_issue` is a pure function of (level, marker state,
# clock), so reconstructing it at query time looks feasible and, for the record, is not: it cannot
# see a round the gate closed before evaluating (which writes no markers at all), cannot see the
# `untimely` verdict on a back-filled round, and cannot see how many devices matched. Same rule as
# /meldinger's `pushed`. What /rushhistorikk DOES do (2026-09-02, `rushhistorikk.reconstruct_rounds`)
# is replay decide_issue for issues with NO row here — labelled `reconstructed`, never counted as a
# sent push — because its purpose is to judge the model, and for that the forecast plus this policy
# is the whole input. A row here always wins over the replay.
#
# `baseline_level` is the value _effective_baseline computed, stored here rather than read off the
# BASELINE's row at query time. That indirection would make one swallowed best-effort write invert
# a verdict: a 06:30 red that pushed and was right reads as a miss if only its own MERGE failed.
#
# PARTITION BY d with no expiry, deliberately: the device counts below are the only sensitive
# columns and /rushhistorikk never projects them, while the page's whole value is longitudinal —
# expiring partitions would delete the history it exists to show.
_ROUNDS_DDL = """
CREATE TABLE IF NOT EXISTS `disruptions.togvarsel_rounds` (
  d DATE OPTIONS(description="Oslo date of the rush the round is about."),
  issue STRING OPTIONS(description="Issue time, HHMM: 0630 | 0700 | 1200 | 1400."),
  level STRING OPTIONS(description="The level this issue forecast: green | yellow | red."),
  baseline_level STRING OPTIONS(description="For a follow-up, the level its baseline left in force (_effective_baseline); NULL for a baseline round."),
  sent BOOL OPTIONS(description="A push was ISSUED here. The policy decision, not delivery: a round can match zero devices."),
  kind STRING OPTIONS(description="warning | update | all_clear when sent; NULL when suppressed."),
  reason STRING OPTIONS(description="untimely | no_change | green_baseline when suppressed; NULL when sent."),
  decided_at TIMESTAMP OPTIONS(description="When the round was evaluated — the push time Varslingstid measures from."),
  matched_journeys INT64 OPTIONS(description="Registered journeys matched. Never served by /rushhistorikk-api."),
  matched_devices INT64 OPTIONS(description="Devices matched. Never served by /rushhistorikk-api."),
  n_sent INT64 OPTIONS(description="Deliveries that succeeded. Never served. NULL when the fan-out raised."),
  n_error INT64 OPTIONS(description="Deliveries that errored. Never served."),
  n_gone INT64 OPTIONS(description="Subscriptions reported gone. Never served.")
)
PARTITION BY d
OPTIONS(description="One row per Rushvarsel notification round as it was DECIDED, pushes and suppressions alike. Read by /rushhistorikk-api. Never CREATE OR REPLACE and never copied dev->prd: each environment records its own rounds.")
"""
# First write per (d, issue) wins, matching togvarsel_issued. A marked round never re-fires, so a
# second write would mean a duplicate rather than a refinement.
_ROUNDS_MERGE = """
MERGE `disruptions.togvarsel_rounds` t
USING (SELECT @d AS d, @issue AS issue) s
ON t.d = s.d AND t.issue = s.issue
WHEN NOT MATCHED THEN
  INSERT (d, issue, level, baseline_level, sent, kind, reason, decided_at,
          matched_journeys, matched_devices, n_sent, n_error, n_gone)
  VALUES (@d, @issue, @level, @baseline_level, @sent, @kind, @reason, @decided_at,
          @matched_journeys, @matched_devices, @n_sent, @n_error, @n_gone)
"""
_rounds_table_ready = False


def ensure_table() -> None:
    """Create disruptions.togvarsel_rounds if absent. Called once from main.lifespan.

    Not left to the first write, and that is the whole point: _evaluate returns before deciding
    anything while TOGVARSEL_NOTIFY_ENABLED is off — permanently true in tst — so a lazily created
    table would never exist there, and /rushhistorikk's LEFT JOIN would 503 the entire page in the
    one environment that never notifies. Failure is logged, never raised: BigQuery must not block
    startup, and the per-write latch below still recovers.
    """
    global _rounds_table_ready
    if _bq_client is None or _rounds_table_ready:
        return
    try:
        _bq_client.query(_ROUNDS_DDL).result(timeout=60)
        _rounds_table_ready = True
    except Exception:
        logger.exception("togvarsel_notify: creating togvarsel_rounds failed; retrying on write")


def _record_round(
    d: date, decision: Decision, decided_at: datetime, summary: Mapping[str, Any] | None
) -> None:
    """Record one round for /rushhistorikk. Best-effort: a BigQuery problem never costs a push.

    `summary` is push_round's return for a pushing round, or None when the round was suppressed or
    its fan-out raised. `sent` follows decision.push either way — it records that a push was
    ISSUED, which is what the page's taxonomy grades; the counts say whether anyone got it.

    What a swallowed failure costs the page: that issue reads "ikke vurdert", which the API splits
    on the earliest decided_at so it is not mistaken for the pre-launch backlog.
    """
    global _rounds_table_ready
    try:
        # Deferred import so hermetic tests need no google libs.
        from google.cloud import bigquery

        if not _rounds_table_ready:
            _bq_client.query(_ROUNDS_DDL).result(timeout=60)
            _rounds_table_ready = True
        cfg = bigquery.QueryJobConfig(
            # The same blast-radius bound every other BigQuery job in this repo carries. It cannot
            # catch a pruning regression on a MERGE this small — it is there so a mistyped table
            # reference cannot reach a ~10 TiB ET partition from the poller thread.
            maximum_bytes_billed=_MAX_BYTES_BILLED,
            query_parameters=[
                bigquery.ScalarQueryParameter("d", "DATE", d),
                bigquery.ScalarQueryParameter("issue", "STRING", decision.issue),
                bigquery.ScalarQueryParameter("level", "STRING", decision.level),
                bigquery.ScalarQueryParameter("baseline_level", "STRING", decision.baseline_level),
                bigquery.ScalarQueryParameter("sent", "BOOL", decision.push),
                bigquery.ScalarQueryParameter("kind", "STRING", decision.kind),
                bigquery.ScalarQueryParameter("reason", "STRING", decision.reason),
                bigquery.ScalarQueryParameter("decided_at", "TIMESTAMP", decided_at),
                *(
                    bigquery.ScalarQueryParameter(name, "INT64", value)
                    for name, value in (
                        ("matched_journeys", (summary or {}).get("matched_journeys")),
                        ("matched_devices", (summary or {}).get("matched_devices")),
                        ("n_sent", (summary or {}).get("sent")),
                        ("n_error", (summary or {}).get("error")),
                        ("n_gone", (summary or {}).get("gone")),
                    )
                ),
            ]
        )
        _bq_client.query(_ROUNDS_MERGE, job_config=cfg).result(timeout=60)
    except Exception:
        logger.exception(
            "togvarsel_notify: recording round %s failed (the push itself is unaffected)",
            decision.issue,
            extra={
                "fields": {
                    "event": "togvarsel_notify_round_record_failed",
                    "d": str(d),
                    "issue": decision.issue,
                }
            },
        )


# ---- Station set ------------------------------------------------------------------------

_STATIONS_SQL = "SELECT stop_place FROM `disruptions.togvarsel_oslo_stations`"

_bq_client: Any = None
# {"date": Oslo date, "refs": frozenset} once fetched; "error_date" throttles the failure log to
# one ERROR per Oslo day. Touched only from the poller thread (same discipline as togvarsel's
# state), so no lock.
_station_state: dict[str, Any] = {}

# evaluate()'s injected seams, set by configure(). All-or-nothing at evaluation time:
# until main wires the full set, evaluate() is a silent no-op — the same closed-gate posture as
# the flag itself.
_seams: dict[str, Any] = {}
_EVALUATE_SEAMS = (
    "enabled",
    "push_ready",
    "issued_today",
    "journeys",
    "expand_refs",
    "resolve_device",
    "deliver",
    "handle_gone",
    "brand_assets",
    "tag_title",
    "load_markers",
    "store_markers",
)


def configure(
    *,
    bq_client: Any,
    enabled: Callable[[], bool] | None = None,
    push_ready: Callable[[], bool] | None = None,
    issued_today: Callable[[], list[dict[str, Any]]] | None = None,
    journeys: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
    expand_refs: Callable[[str], Iterable[str]] | None = None,
    resolve_device: Callable[[str], tuple[dict, str] | None] | None = None,
    deliver: Callable[..., list[tuple[str, Any, str, dict]]] | None = None,
    handle_gone: Callable[[str], bool] | None = None,
    brand_assets: Mapping[str, Mapping[str, str]] | None = None,
    tag_title: Callable[[str], str] | None = None,
    load_markers: Callable[[str], Mapping[str, Any]] | None = None,
    store_markers: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> None:
    """Inject the module's runtime seams: the BigQuery client for :func:`station_set`,
    and — for :func:`evaluate` — the two gates (`enabled` reads the delivery flag, `push_ready`
    varsel's VAPID state), togvarsel's issued-today snapshot, varsel's journey snapshot,
    place-alias expander, delivery machinery, brand assets and environment tagger, and the
    round-marker accessor pair (`load_markers(date) -> copy`, `store_markers(date, markers)`
    persists and prunes to that date). Everything past `bq_client` is optional so the station-set
    tests keep their one-argument call; a partial wiring simply leaves evaluate() a no-op."""
    global _bq_client, _seams
    _bq_client = bq_client
    _seams = {
        "enabled": enabled,
        "push_ready": push_ready,
        "issued_today": issued_today,
        "journeys": journeys,
        "expand_refs": expand_refs,
        "resolve_device": resolve_device,
        "deliver": deliver,
        "handle_gone": handle_gone,
        "brand_assets": brand_assets,
        "tag_title": tag_title,
        "load_markers": load_markers,
        "store_markers": store_markers,
    }


def station_set(now: datetime) -> frozenset[str] | None:
    """The Oslo local-network StopPlace refs, fetched once per Oslo day — or None while unavailable.

    The table defines the product's geography, so it is never duplicated in Python; an
    empty result is refused for the same reason (it would make every journey silently unaffected).
    On failure the NEXT call simply retries: no cooldown and no daily give-up — deliberately
    unlike togvarsel's _FAIL_COOLDOWN, whose 15-minute cooldown and 8-fails give-up would keep
    rounds dark after a morning outage recovers, and the retry here is one trivial 39-row scan.
    While this returns None the caller must evaluate nothing and write no markers, so the
    timeliness deadline suppresses honestly once the set is back. On prd the table exists only
    after copy_runtime_to_prd.sh — a real launch state — so persistent failure logs one ERROR per
    Oslo day (then warnings) instead of a scream per tick."""
    today = now.astimezone(_OSLO).date()
    if _station_state.get("date") == today:
        return _station_state["refs"]
    try:
        rows = _bq_client.query(_STATIONS_SQL).result()
        refs = frozenset(str(r["stop_place"]) for r in rows)
        if not refs:
            raise RuntimeError("disruptions.togvarsel_oslo_stations came back empty")
    except Exception:
        if _station_state.get("error_date") == today:
            logger.warning("togvarsel_notify: station set still unavailable; retrying next tick")
        else:
            _station_state["error_date"] = today
            logger.exception(
                "togvarsel_notify: fetching the Oslo station set failed; togvarsel rounds are "
                "skipped (no markers) until it succeeds — on prd, has copy_runtime_to_prd.sh run?"
            )
        return None
    # `refs` BEFORE `date`: the request path reads this cache without a lock (see
    # :func:`cached_station_set`), and publishing the date first would let a reader see today's
    # date and then miss the refs it is supposed to imply.
    _station_state["refs"] = refs
    _station_state["date"] = today
    logger.info(
        "togvarsel_notify: fetched %d Oslo stations (expect 39)",
        len(refs),
        extra={"fields": {"event": "togvarsel_notify_stations", "count": len(refs)}},
    )
    return refs


def cached_station_set(now: datetime) -> frozenset[str] | None:
    """Today's station set if the poller has already fetched it — and NEVER a query.

    The read-only twin of :func:`station_set`, for the request path (:func:`rush_outlook`).
    /togmelding-api is public and unauthenticated, and station_set deliberately has no failure
    cooldown — sound on a poller tick, one billable scan per page view during an outage here. No
    lock: the fetch publishes `refs` before `date`, so a reader that sees today's date has already
    seen its refs.

    A warm cache also means more than "the table was readable". :func:`_evaluate` reaches
    station_set only past the delivery flag and the VAPID gate, so a warm cache is evidence that
    rounds are actually being evaluated today — which is what makes it the right gate for a panel
    whose whole claim is "you are being notified about this".
    """
    return (
        _station_state["refs"]
        if _station_state.get("date") == now.astimezone(_OSLO).date()
        else None
    )


# ---- Journey matching -----------------------------------------------------------------


def _parse_hhmm(value: str) -> int:
    """"HH:MM" -> minutes since midnight (varsel's window encoding), refusing nonsense.

    Deliberately stricter than varsel's own ``_parse_hhmm`` (which accepts "25:99" as 1599):
    the range check is what lets one malformed legacy record be skipped and logged instead of
    silently matching, and this module cannot import varsel to share it."""
    h, m = str(value).split(":")
    if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
        raise ValueError(f"not a wall-clock time: {value!r}")
    return int(h) * 60 + int(m)


def _endpoint_listed(
    ref: str, stations: Collection[str], expand_refs: Callable[[str], Iterable[str]]
) -> bool:
    """Whether any ref of `ref`'s physical place is a listed station. `expand_refs` is varsel's
    place_refs, injected: a registration made on a bus-side alias of Lysaker still counts as
    Lysaker (accepted imprecision — registrations carry no mode, and the network-level wording
    keeps the message honest)."""
    return any(r in stations for r in expand_refs(str(ref)))


def _affects(
    journey: Mapping[str, Any],
    weekday: int,
    window_start: float,
    window_end: float,
    stations: Collection[str],
    expand_refs: Callable[[str], Iterable[str]],
) -> bool:
    """Whether one registration is inside `[window_start, window_end)` minutes and the geography.

    The three tests both callers share — today's weekday is in `days`, the stored-inclusive
    [begin, end] window overlaps (compared half-open), both endpoints' alias-expanded ref sets
    intersect the station set. What differs is the window they pass: :func:`match_journeys` uses
    the part of the rush still ahead of evaluation time, :func:`affected_rushes` the whole rush.
    Raises on a malformed record; both callers turn that into "skip this journey and log it"."""
    if weekday not in journey["days"]:
        return False
    begin_min = _parse_hhmm(journey["begin"])
    end_min = _parse_hhmm(journey["end"])
    if not (window_start < window_end and begin_min < window_end and window_start < end_min):
        return False
    return _endpoint_listed(journey["from_stop"], stations, expand_refs) and _endpoint_listed(
        journey["to_stop"], stations, expand_refs
    )


def match_journeys(
    issue_key: str,
    journeys: Mapping[str, Mapping[str, Any]],
    stations: Collection[str],
    expand_refs: Callable[[str], Iterable[str]],
    now: datetime,
) -> tuple[list[str], set[str]]:
    """The registrations an issue affects and the device set to push to, deduped.

    A journey is affected iff today's Oslo weekday is in its `days`, its stored-inclusive
    [begin, end] window overlaps — compared half-open — the live part of the issue's scope window
    [max(scope start, now), scope end), and both endpoints' alias-expanded ref sets intersect the
    station set. The evaluation-time bound means a journey whose window has fully passed at
    evaluation never matches — a late warning and a friskmelding are equally noise to a traveller
    who has exited the rush. One malformed record (a legacy begin/end, a broken days list) is
    skipped and logged, never kills the round — the same per-journey guard as varsel's
    check_and_notify loop 1. Raises only on an unknown issue key (the caller's bug, not data)."""
    spec = _SPEC_BY_KEY.get(str(issue_key))
    if spec is None:
        raise ValueError(f"unknown togvarsel issue {issue_key!r}")
    now = now.astimezone(_OSLO)
    weekday = now.weekday()
    now_min = now.hour * 60 + now.minute + now.second / 60
    live_start = max(spec.scope_start_h * 60, now_min)
    live_end = spec.scope_end_h * 60

    matched: list[str] = []
    devices: set[str] = set()
    for jid, journey in journeys.items():
        try:
            if not _affects(journey, weekday, live_start, live_end, stations, expand_refs):
                continue
            device = str(journey["device_id"])
        except Exception:
            logger.exception(
                "togvarsel_notify: journey %s is malformed; skipped for issue %s", jid, issue_key
            )
            continue
        matched.append(jid)
        devices.add(device)
    return matched, devices


# ---- Rush outlook (the status panel /varsel shows at the top) -----------------------------

# The two rushes, derived from NOTIFY_SPECS rather than restated, so a scope change moves the
# panel with the pushes: which rush an issue belongs to, and each rush's whole window (the union
# of its two issues' scopes — am 06:30–09:00, pm 15:00–18:00).
RUSH_OF_ISSUE: dict[str, str] = {
    spec.key: ("am" if spec.scope_start_h < 12 else "pm") for spec in NOTIFY_SPECS
}


def _rush_windows() -> dict[str, tuple[float, float]]:
    windows: dict[str, tuple[float, float]] = {}
    for spec in NOTIFY_SPECS:
        rush = RUSH_OF_ISSUE[spec.key]
        start, end = windows.get(rush, (spec.scope_start_h, spec.scope_end_h))
        windows[rush] = (min(start, spec.scope_start_h), max(end, spec.scope_end_h))
    return windows


RUSH_WINDOWS: dict[str, tuple[float, float]] = _rush_windows()


class RushForecast(BaseModel):
    """One rush's current level, for a device that is notified about it."""

    rush: str = Field(description='"am" (morgenrushet) or "pm" (ettermiddagsrushet)')
    issue: str = Field(
        description="HHMM of the forecast shown — the NEWEST issued for this rush, so a follow-up "
        "replaces its baseline exactly as it does on /togmelding"
    )
    scope: str = Field(description="The window the probability is about, verbatim from togvarsel")
    level: str = Field(description="green | yellow | red")
    p: float = Field(description="Calibrated probability of a bad window (the trouble10 bar)")


class RushOutlook(BaseModel):
    rushes: list[RushForecast] = Field(
        description="Rushes not yet over that this device would be notified about; [] whenever "
        "there is nothing to show — off deployment, weekend/holiday, nothing issued yet, no "
        "registration in the Oslo geography, or every rush already past."
    )


def affected_rushes(
    device_id: str,
    journeys: Mapping[str, Mapping[str, Any]],
    stations: Collection[str],
    expand_refs: Callable[[str], Iterable[str]],
    now: datetime,
) -> set[str]:
    """Which rushes this device would be notified about today — "am", "pm", both or neither.

    :func:`_affects`' three tests, over this device's registrations only, with ONE deliberate
    difference from :func:`match_journeys`: the window is the rush's WHOLE span, not the part
    still ahead of `now`. match_journeys clamps to evaluation time because a push about a window
    the traveller has already left is noise. A status panel is the opposite — a 07:00–08:00
    registration should keep reading the morning's level until the morning rush itself is over at
    09:00, rather than going blank at 08:00 with the trains still late. What bounds the panel is
    therefore the rush's end (:func:`rush_outlook`), never the registration's.

    One malformed record is skipped and logged, exactly as in match_journeys' loop.
    """
    weekday = now.astimezone(_OSLO).weekday()
    affected: set[str] = set()
    for jid, journey in journeys.items():
        if str(journey.get("device_id")) != device_id:
            continue
        for rush, (start_h, end_h) in RUSH_WINDOWS.items():
            if rush in affected:
                continue
            try:
                if _affects(journey, weekday, start_h * 60, end_h * 60, stations, expand_refs):
                    affected.add(rush)
            except Exception:
                logger.exception(
                    "togvarsel_notify: journey %s is malformed; skipped for the %s outlook",
                    jid,
                    rush,
                )
                break  # a record that cannot be parsed cannot match the other rush either
    return affected


def rush_outlook(device_id: str, now: datetime | None = None) -> list[RushForecast]:
    """Today's rush forecasts this device is notified about, for the rushes not yet over.

    Same policy as the pushes, read-only: the level shown is the newest issued forecast for the
    rush (a follow-up supersedes its baseline, as on /togmelding), the device must have a
    registration the round would have matched, and a rush whose window has ended is dropped —
    a level nobody can still act on is noise on a page about catching trains.

    Two gates, both closed by default. `enabled()` is the delivery flag, so the panel appears only
    where these pushes are actually sent — the same read /varsel-api/config already surfaces as
    `togvarsel_notify`. And :func:`cached_station_set` must be warm, which additionally implies
    the poller got past the VAPID gate and is evaluating rounds today; a cold cache yields [], and
    never a BigQuery query on this request path. The VAPID gate is not consulted directly: a
    device with no push subscription still gets an honest forecast, and the page already says
    push is unavailable.
    """
    seams = _seams
    if any(
        seams.get(key) is None for key in ("enabled", "issued_today", "journeys", "expand_refs")
    ):
        return []
    if not seams["enabled"]():
        return []
    now = (now if now is not None else datetime.now(_OSLO)).astimezone(_OSLO)
    stations = cached_station_set(now)
    if stations is None:
        return []
    issued = {str(issue["issue"]): issue for issue in seams["issued_today"]()}
    if not issued:
        return []  # weekend, holiday, or nothing issued yet today
    now_h = now.hour + now.minute / 60 + now.second / 3600
    affected = affected_rushes(device_id, seams["journeys"](), stations, seams["expand_refs"], now)
    if not affected:
        return []
    out: list[RushForecast] = []
    for rush, (_start_h, end_h) in sorted(RUSH_WINDOWS.items(), key=lambda item: item[1]):
        if rush not in affected or now_h >= end_h:
            continue
        candidates = [
            spec for spec in NOTIFY_SPECS if RUSH_OF_ISSUE[spec.key] == rush and spec.key in issued
        ]
        if not candidates:
            continue  # this rush has no forecast yet (the other one may well have)
        issue = issued[max(candidates, key=lambda spec: spec.due_h).key]
        out.append(
            RushForecast(
                rush=rush,
                issue=str(issue["issue"]),
                scope=str(issue["scope"]),
                level=str(issue["level"]),
                p=float(issue["p"]),
            )
        )
    return out


# ---- Message composition and round delivery ----------------------------

# Deterministic Norwegian only: every phrase below is assembled from the issue's own fields,
# so a push can never say something the forecast did not see. The scope text and the reasons come
# verbatim from togvarsel (its Issue payload), which keeps the two surfaces in one vocabulary.
_LEVEL_ADJ = {"green": "grønt", "yellow": "gult", "red": "rødt"}
_RUSH_NAME = {
    "0630": "morgenrushet",
    "0700": "morgenrushet",
    "1200": "ettermiddagsrushet",
    "1400": "ettermiddagsrushet",
}
# Leading reasons only: a notification body is read on a lock screen that truncates, and
# togvarsel's rule engine already orders them by weight.
_BODY_REASONS = 2
# The entur-branded install has the forecast page to land on; the unbranded one does not, so
# it keeps its own varsel URL — the body carries the essentials, no click-through required.
_TOGMELDING_URL = "/togmelding"


def _sentence(text: str) -> str:
    t = str(text).strip().rstrip(".")
    return f"{t[:1].upper()}{t[1:]}."


def compose_message(decision: Decision, issue: Mapping[str, Any]) -> tuple[str, str]:
    """(title, body) for one pushing round — deterministic Norwegian, untagged (the environment
    prefix is the payload's concern, see :func:`build_payload`).

    The title identifies a rush FORECAST with its level — "Rushvarsel", never train-alert framing,
    so a red morning that legitimately produces both an SX push and this one stays tellable apart
   . An update against a pushed baseline is marked "Oppdatert"; the all-clear is a
    friskmelding, free of every word the warnings use — its body carries no reasons either, since
    green's own reasons still talk about trouble signs. Raises on a non-push decision: composing
    a message for a suppressed round is the caller's bug."""
    if not decision.push or decision.kind not in ("warning", "update", "all_clear"):
        raise ValueError(f"not a pushing decision: {decision}")
    rush = _RUSH_NAME[decision.issue]
    scope = str(issue["scope"])
    if decision.kind == "all_clear":
        return (
            f"Friskmelding: grønt nivå for {rush}",
            f"Utsiktene for {scope} er tilbake på grønt nivå.",
        )
    adj = _LEVEL_ADJ[decision.level]
    reasons = [str(r) for r in (issue.get("reasons") or [])][:_BODY_REASONS]
    if decision.kind == "update":
        title = f"Oppdatert rushvarsel: {adj} nivå for {rush}"
        lead = f"Prognosen for {scope} er endret til {adj} nivå."
    else:
        title = f"Rushvarsel: {adj} nivå for {rush}"
        lead = f"Prognosen for {scope} er {adj} nivå."
    return title, " ".join([lead, *(_sentence(r) for r in reasons)])


def build_payload(
    decision: Decision,
    issue: Mapping[str, Any],
    brand: str,
    *,
    brand_assets: Mapping[str, Mapping[str, str]],
    tag_title: Callable[[str], str],
) -> dict[str, Any]:
    """The Web Push payload for one device of `brand`.

    `brand_assets` (varsel's _BRAND_ASSETS) and `tag_title` (varsel's _tag_environment) are
    injected by main. The icon follows the brand exactly as SX pushes do; the URL follows
    The entur brand lands on /togmelding, the unbranded install keeps its own varsel URL.
    The wording is identical across brands (varsel precedent: only icon and URL ever move). No
    situation_number: this is not an SX situation, and the standalone-push precedent (varsel's
    /varsel-api/test) already omits it."""
    title, body = compose_message(decision, issue)
    assets = brand_assets.get(brand) or brand_assets["entur"]
    return {
        "title": tag_title(title),
        "body": body,
        "url": assets["url"] if brand == "none" else _TOGMELDING_URL,
        "icon": assets["icon"],
    }


def push_round(
    decision: Decision,
    issue: Mapping[str, Any],
    matched: Collection[str],
    devices: Collection[str],
    *,
    resolve_device: Callable[[str], tuple[dict, str] | None],
    deliver: Callable[
        [Mapping[str, list[Any]], Callable[[Any], tuple[dict, dict]]],
        list[tuple[str, Any, str, dict]],
    ],
    handle_gone: Callable[[str], bool],
    brand_assets: Mapping[str, Mapping[str, str]],
    tag_title: Callable[[str], str],
) -> dict[str, Any]:
    """Deliver one pushing round to its matched devices — one push per device.

    Every delivery seam is varsel's, injected by main: `resolve_device`
    (device_subscription — the deleted-while-iterating re-check under varsel's lock), `deliver`
    (deliver_to_devices — the bounded parallel fan-out), `handle_gone` (handle_gone_device — the
    grace-then-drop rule). A device that resolves to nothing was deleted between matching and
    delivery, or registered without push: skipped and counted, never a crash. Send errors are NOT
    retried within the round — the round marker is already committed, and a forecast is
    lower-stakes than a train alert — so the counts are logged in ONE structured round event
    and returned; a returned "dropped" > 0 means device state changed and the caller must
    persist it. `matched` (the affected journey ids) rides along for that log only."""
    if not decision.push:
        raise ValueError(f"push_round called for a non-push decision: {decision}")
    payload_by_brand: dict[str, dict[str, Any]] = {}
    per_device: dict[str, list[tuple[dict, dict]]] = {}
    no_subscription = 0
    for device_id in sorted(devices):  # deterministic order, for the fan-out and the logs alike
        resolved = resolve_device(device_id)
        if resolved is None:
            no_subscription += 1
            continue
        subscription, brand = resolved
        payload = payload_by_brand.get(brand)
        if payload is None:
            payload = payload_by_brand[brand] = build_payload(
                decision, issue, brand, brand_assets=brand_assets, tag_title=tag_title
            )
        per_device[device_id] = [(subscription, payload)]  # one item per device per round
    counts = {"sent": 0, "error": 0, "gone": 0}
    dropped = 0
    for device_id, _item, result, _payload in deliver(per_device, lambda item: item):
        counts[result] = counts.get(result, 0) + 1
        if result == "gone" and handle_gone(device_id):
            dropped += 1
    summary: dict[str, Any] = {
        "issue": decision.issue,
        "level": decision.level,
        "kind": decision.kind,
        "matched_journeys": len(matched),
        "matched_devices": len(devices),
        "no_subscription": no_subscription,
        "dropped": dropped,
        **counts,
    }
    logger.info(
        "togvarsel_notify: round %s %s (%s) — %d journeys on %d devices matched; "
        "%d sent, %d error (not retried), %d gone (%d dropped)",
        decision.issue,
        decision.level,
        decision.kind,
        len(matched),
        len(devices),
        counts["sent"],
        counts["error"],
        counts["gone"],
        dropped,
        extra={"fields": {"event": "togvarsel_notify_round", **summary}},
    )
    return summary


# ---- Per-tick orchestration ------------------------------------------------


def evaluate(now: datetime | None = None) -> None:
    """One tick's togvarsel notification pass — main calls this right after togvarsel.refresh()
    on the every-tick wall-clock path (never from the SX-ingestion rebuild).

    Gate order is the safety story: while the flag is off or VAPID is unset this returns
    before ANY evaluation, writing no markers — which is exactly what lets an enable-restart
    inside a timeliness window still deliver that round. While the station set is unavailable,
    the same: nothing evaluated, nothing marked. Marker ordering:
    decide_rounds commits every round's marker in memory before any fan-out, and the injected
    store accessor persists after each pushing round's delivery — plus once at the end of a
    suppression-only pass. The store's persistence is best-effort (varsel's _save_state never
    raises), so a lost write costs at most the accepted crash-window duplicate on the next
    restart, never the in-process dedup. Wrapped whole: a failure here is logged and the tick
    goes on (main wraps its call too)."""
    try:
        _evaluate(now if now is not None else datetime.now(_OSLO))
    except Exception:
        logger.exception("togvarsel_notify: evaluate failed this tick; retrying next tick")


def _evaluate(now: datetime) -> None:
    s = _seams
    if any(s.get(key) is None for key in _EVALUATE_SEAMS):
        return  # not wired for delivery (e.g. a station-set-only configuration): nothing to do
    if not s["enabled"]():
        return  # no evaluation and NO markers, so an enable-restart still delivers
    if not s["push_ready"]():
        return  # no VAPID keys: degrade to a no-op under the same no-markers contract
    now = now.astimezone(_OSLO)
    stations = station_set(now)
    if stations is None:
        return  # evaluate nothing while the geography is unavailable; retry next tick
    issues = s["issued_today"]()
    if not issues:
        return  # weekend/holiday, or nothing issued yet today — the natural no-op
    date_str = now.date().isoformat()
    markers: dict[str, Any] = dict(s["load_markers"](date_str))
    decisions = decide_rounds(issues, markers, now)
    if not decisions:
        return  # every issued round already carries a marker — nothing changed, nothing to save
    for decision in decisions:
        if not decision.push:
            logger.info(
                "togvarsel_notify: round %s %s suppressed (%s)",
                decision.issue,
                decision.level,
                decision.reason,
                extra={
                    "fields": {
                        "event": "togvarsel_notify_round",
                        "issue": decision.issue,
                        "level": decision.level,
                        "decision": decision.reason,
                    }
                },
            )
    pushes = [d for d in decisions if d.push]
    issue_by_key = {str(issue["issue"]): issue for issue in issues}
    journeys = s["journeys"]() if pushes else {}
    summaries: dict[str, Mapping[str, Any]] = {}
    for decision in pushes:
        try:
            matched, devices = match_journeys(
                decision.issue, journeys, stations, s["expand_refs"], now
            )
            summaries[decision.issue] = push_round(
                decision,
                issue_by_key[decision.issue],
                matched,
                devices,
                resolve_device=s["resolve_device"],
                deliver=s["deliver"],
                handle_gone=s["handle_gone"],
                brand_assets=s["brand_assets"],
                tag_title=s["tag_title"],
            )
        except Exception:
            logger.exception(
                "togvarsel_notify: delivering round %s failed; its marker is already committed, "
                "so the round is not retried and later rounds go on",
                decision.issue,
            )
        # Persist AFTER the round's fan-out: the in-memory commit above already protects
        # this process, and a crash inside the window costs at most one accepted duplicate.
        s["store_markers"](date_str, markers)
    if not pushes:
        s["store_markers"](date_str, markers)  # suppression-only pass: the markers still count

    # Record EVERY decision, here and nowhere else. One loop after both store_markers sites,
    # because neither of them reaches a suppression: the sites above are inside the `pushes` loop
    # and in the `not pushes` branch, so on a MIXED pass — a restart back-fill that suppresses
    # 0630 as untimely and pushes 0700 in one tick, the case the marker ordering above exists for
    # — a hook attached to either site would silently drop the suppressed round, and the page
    # would read it as "never evaluated" rather than as the outcome it was.
    #
    # Deliberately NOT called from push_round: POST /togmelding-api/test-notify reaches that
    # function directly on the public dev host with a caller-supplied device_id and a synthetic
    # 0630 decision, and on a first-write-wins MERGE that write would take the day's row and the
    # real 06:30 round would be lost for good.
    for decision in decisions:
        _record_round(now.date(), decision, now, summaries.get(decision.issue))


# ---- HTTP surfaces --------------------------------------------------------------------------

router = APIRouter()


@router.get(
    "/togmelding-api/outlook",
    response_model=RushOutlook,
    summary="Today's rush forecasts a device is notified about",
)
async def outlook(
    device_id: str = Query(description="Client-generated device UUID, as /varsel-api uses."),
) -> RushOutlook:
    """The status panel /varsel renders at the top of the page, with its link to /togmelding.

    Device-scoped because the panel's claim is "you are being notified about this": a rush level is
    network-wide, but whether it reaches this traveller depends on their registrations. Nothing
    device-identifying is returned — see :func:`rush_outlook` for the policy and the two gates.

    `async def`, unlike :func:`test_notify` below: every input is already in this process (the
    poller's station cache, togvarsel's issued snapshot, varsel's journey snapshot), so there is no
    blocking call to keep off the event loop, and no reason to spend an anyio worker on a page that
    every /varsel poll asks.
    """
    return RushOutlook(rushes=rush_outlook(device_id))


# ---- Dev-only test trigger -----------------------------------------------------------------

# The synthetic round the trigger sends: a fixed yellow warning whose reasons say TEST in plain
# Norwegian, so a screenshot of the notification can never pass for a real forecast. Everything
# else — wording template, environment tag, brand icon, click-through — is the REAL pipe
# (compose_message/build_payload/push_round over the injected seams); only the round DECISION is
# synthetic, and with it the markers, which the trigger must never read or write.
_TEST_DECISION = Decision(
    issue="0630", level="yellow", push=True, marker=_marker("yellow", True), kind="warning"
)
_TEST_ISSUE: Mapping[str, Any] = {
    "issue": "0630",
    "scope": "morgenrushet 06:30–09:00",
    "reasons": (
        "dette er et testvarsel, ikke en ekte prognose",
        "eksempel på årsak: 2 tog er innstilt før kl. 06:30",
    ),
}
# What the trigger needs wired (a subset of _EVALUATE_SEAMS): the VAPID gate and push_round's
# delivery seams. Deliberately NOT `enabled`: the trigger exists to prove the pipe before the
# delivery flag opens it, and `load_markers`/`store_markers` are excluded by design.
_TRIGGER_SEAMS = ("push_ready", "resolve_device", "deliver", "handle_gone", "brand_assets", "tag_title")


class TestNotifyIn(BaseModel):
    """Request one synthetic togvarsel push — the mirror of varsel's TestIn, same field, same bounds."""

    device_id: str = Field(min_length=8, max_length=100)


@router.post("/togmelding-api/test-notify", summary="Send a synthetic togvarsel push (dev only)")
def test_notify(body: TestNotifyIn) -> dict:
    """Send one synthetic rush-forecast push to this device — proves the whole pipe (wording,
    environment tag, brand icon, click-through) without waiting for a real round.

    Served ONLY by the dev deployment, and the gate fails closed: environment_label() is
    read per call and everything that is not exactly "dev" is refused — tst ("tst"), prd and
    unset (both None), and any unrecognized label alike — with a plain 404, before any seam is
    touched, so the refused route is indistinguishable from an absent one. Delivery is
    :func:`push_round` over the seams main injected, which is the same code path a real round
    takes; the response statuses mirror /varsel-api/test (404 unknown device, 425 fresh-gone,
    410 gone-and-dropped, 502 send error). One dev-only wrinkle, accepted: a device dropped here
    is dropped in memory only (the trigger has no persist seam that is not the markers'), so the
    next real round or SX push re-drops and persists it.

    Plain `def`, like varsel's test_push: the send blocks for up to the push timeout against a
    client-supplied endpoint, which on `async def` would freeze the whole event loop.
    """
    if environment_label() != "dev":
        raise HTTPException(status_code=404, detail="Not Found")
    s = _seams
    if any(s.get(key) is None for key in _TRIGGER_SEAMS) or not s["push_ready"]():
        raise HTTPException(status_code=503, detail="Push er ikke konfigurert på tjeneren.")
    summary = push_round(
        _TEST_DECISION,
        _TEST_ISSUE,
        [],
        [body.device_id],
        resolve_device=s["resolve_device"],
        deliver=s["deliver"],
        handle_gone=s["handle_gone"],
        brand_assets=s["brand_assets"],
        tag_title=s["tag_title"],
    )
    if summary["no_subscription"]:
        raise HTTPException(status_code=404, detail="Enheten har ingen aktiv varsel-abonnement.")
    if summary["gone"]:
        if summary["dropped"]:
            raise HTTPException(
                status_code=410, detail="Abonnementet er utløpt; slå på varsler på nytt."
            )
        raise HTTPException(
            status_code=425,
            detail="Varselet er nettopp slått på. Vent et lite øyeblikk og prøv testvarsel igjen.",
        )
    if summary["error"]:
        raise HTTPException(status_code=502, detail="Kunne ikke sende testvarsel.")
    return {"status": "sent"}
