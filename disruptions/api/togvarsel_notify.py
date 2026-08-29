"""Notification policy for togvarsel pushes — when a rush-hour forecast becomes a Web Push.

Togvarsel (togvarsel.py) issues four green/yellow/red prognoses per weekday: 0630 and 0700 for
the morning rush, 1200 and 1400 for the afternoon. This module decides which of those rounds
notify /varsel subscribers. The policy core below is pure functions over plain data — issue
dicts, a marker map, an explicit Oslo-aware `now` — so every decision is reproducible from
durable inputs (the issued level, the marker state, the clock) without rows in a decisions table.

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

All time logic is Europe/Oslo wall-clock. No import of main, varsel, or togvarsel (the package
rule): runtime dependencies — issue snapshots, journeys, delivery, marker storage — are injected
by main as the module grows; the policy core here needs none.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

# togvarsel's level vocabulary; anything else is a malformed round and is refused loudly.
_LEVELS = ("green", "yellow", "red")


@dataclass(frozen=True)
class _NotifySpec:
    key: str  # togvarsel issue key, "0630" — also its display time "06:30"
    due_h: float  # Oslo fractional hour the issue becomes computable — the evaluation order
    deadline_h: float  # notify only strictly BEFORE this Oslo wall-clock hour
    baseline: str | None  # the round a follow-up is compared against; None for baselines


# The notification companion to togvarsel.ISSUE_SPECS: one row per issue, keyed the same way.
# Deadlines are where the message stops being useful — 0630 is superseded by 0700 at 07:00,
# 0700 by the rush ending at 09:00, 1200 by 1400 at 14:00, 1400 by the rush ending at 18:00.
NOTIFY_SPECS: tuple[_NotifySpec, ...] = (
    _NotifySpec("0630", 6.5, 7.0, None),
    _NotifySpec("0700", 7.0, 9.0, "0630"),
    _NotifySpec("1200", 12.0, 14.0, None),
    _NotifySpec("1400", 14.0, 18.0, "1200"),
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


def _marker(level: str, pushed: bool) -> dict[str, Any]:
    return {"level": level, "pushed": pushed}


def _effective_baseline(spec: _NotifySpec, markers: Mapping[str, Any]) -> str:
    """The level the baseline round pushed, or green when it pushed nothing — what a follow-up's
    level must differ from to count as significant change (R2)."""
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

    now = now.astimezone(_OSLO)
    now_h = now.hour + now.minute / 60 + now.second / 3600
    if now_h >= spec.deadline_h:  # strictly-before cutoff: 07:00:00 sharp is already too late
        return Decision(key, level, push=False, marker=_marker(level, False), reason="untimely")

    if spec.baseline is None:
        if level == "green":
            return Decision(
                key, level, push=False, marker=_marker(level, False), reason="green_baseline"
            )
        return Decision(key, level, push=True, marker=_marker(level, True), kind="warning")

    base = _effective_baseline(spec, markers)
    if level == base:
        return Decision(key, level, push=False, marker=_marker(level, False), reason="no_change")
    if level == "green":
        kind = "all_clear"  # dropped back to green after a pushed warning (KTD11)
    elif base == "green":
        kind = "warning"  # the baseline pushed nothing, so there is nothing to "update"
    else:
        kind = "update"  # yellow <-> red transition against a pushed baseline
    return Decision(key, level, push=True, marker=_marker(level, True), kind=kind)


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
    0630 and issues 0700 in one tick resolves deterministically (KTD9). A round that raises is
    logged with its issue context, records no marker (so the next tick retries it, bounded by the
    timeliness deadline), and never stops the later rounds (R10). Returns the decisions made this
    pass; already-marked rounds are skipped entirely (R4: a completed round never re-fires)."""
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
