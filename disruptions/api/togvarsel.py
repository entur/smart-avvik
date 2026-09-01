"""/togmelding-api — Togmeldingen: a rush-hour "weather forecast" for the Oslo local network.

Implements §8 of docs/togvarsel/02-findings-2026-08-27.md as revised by §14k. Four issues on
weekdays, each a calibrated probability that the rush is bad on the product bar (a call cancelled
OR ≥10 min late), mapped to green/yellow/red at the study's fixed bins:

    06:30  whole AM rush 06:30–09:00   model disruptions.togvarsel_m_am0630
    07:00  rest of the rush 07–09      model disruptions.togvarsel_m_am0700  (the sharpest, §14e)
    12:00  PM rush 15–18               model disruptions.togvarsel_m_pm1200
    14:00  PM rush 15–18               model disruptions.togvarsel_m_pm1400

The follow-up of each pair REPLACES its baseline on the page once issued (``SUPERSEDED_BY``): the
page shows at most one card per rush, the newest. Only the display is narrowed — every issue is
still computed, recorded and handed to the notify chain, which needs the baseline to decide
whether the follow-up changed level.

Between the morning issues and 12:00 the PM outlook is DETERMINISTIC two-state prose (§8 part 3:
"normale utsikter" vs "noe forhøyet risiko" after a red morning) — the study found no real PM
skill before ~12:00, and pretending otherwise with a probability would be theatre. The "kjente
avvik" list is likewise deterministic: it repeats what SIRI-SX already announces, it predicts
nothing. Reasons on each issue are rule-based sentences derived from the same feature values the
model saw — the features are human-readable by construction, no text generation involved.

How it runs: :func:`refresh` is called on every poller tick — wall-clock-driven, because an
issue is due at its clock cutoff whether or not new SX ingestion arrived. On each tick it
computes the issues that are DUE and not yet computed today — so a restart at 13:00 back-fills
06:30/07:00/12:00 retroactively, which is sound because every feature column is defined by a
fixed clock cutoff, not by "now" (recomputing later only gives the ET denominators more resolved
calls, i.e. numbers closer to the training-time batch view). A back-filled issue is marked
`late`: for a window already underway it saw post-cutoff reality, so skill audits must exclude
it. One tick does at most one rebuild of disruptions.togvarsel_features_today (shared by every
issue computed that tick) plus one ML.PREDICT per issue; every failure backs off per issue
(cooldown, then give up for the day) so a broken model or feed costs a bounded number of billable
scans. Weekends and holidays (a rollover probe of disruptions.togvarsel_calendar, confirmed by
the feature row's flag) get an honest "ingen prognose i dag" instead of a forecast trained on
days like them.

Every issued forecast lands in disruptions.togvarsel_issued — MERGE, first write per (d, issue)
wins — so realized skill can be audited later against the training chain's labels (§14k item 8:
claims come with verification, and the first product-side claim is the forecast itself).

No import of main (the package rule): main injects the BigQuery client via :func:`configure`.
BigQuery writes touch only this module's own tables (the *_today pair and togvarsel_issued).
Model names are fixed literals here — nothing user-supplied ever reaches SQL.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import feedback  # the /varsel feedback stack; gates and mints are shared, see togmelding_today
import feedback_contexts
from deployment import environment_label
from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()

_OSLO = ZoneInfo("Europe/Oslo")

_FEATURES_TODAY_SQL = (Path(__file__).parent / "togvarsel_features_today.sql").read_text()
_FEATURES_ROW_SQL = "SELECT * FROM `disruptions.togvarsel_features_today`"
_SX_TODAY_SQL = "SELECT * FROM `disruptions.togvarsel_sx_today` ORDER BY created_local"
# LOGISTIC_REG probability of the positive class. The label is BOOL in training; the CAST makes
# this robust to BQML serving it back as a string.
_PREDICT_SQL = """
SELECT p.prob AS p
FROM ML.PREDICT(MODEL `disruptions.togvarsel_m_{model}`,
                TABLE `disruptions.togvarsel_features_today`) AS t,
UNNEST(t.predicted_label_probs) AS p
WHERE LOWER(CAST(p.label AS STRING)) = 'true'
"""
_ISSUED_DDL = """
CREATE TABLE IF NOT EXISTS `disruptions.togvarsel_issued` (
  d DATE, issue STRING, model STRING, p FLOAT64, level STRING, computed_at TIMESTAMP, late BOOL
)
"""
# First write per (d, issue) wins: a restart or failure-path recompute must neither duplicate
# nor rewrite the forecast as it was originally served.
_ISSUED_MERGE = """
MERGE `disruptions.togvarsel_issued` t
USING (SELECT @d AS d, @issue AS issue) s
ON t.d = s.d AND t.issue = s.issue
WHEN NOT MATCHED THEN
  INSERT (d, issue, model, p, level, computed_at, late)
  VALUES (@d, @issue, @model, @p, @level, @computed_at, @late)
"""
# The rollover probe: is today a holiday? Tiny lookup, so the page can say "helligdag" from the
# first tick of the day instead of first learning it from the 06:30 feature row.
_CALENDAR_TODAY_SQL = """
SELECT holiday FROM `disruptions.togvarsel_calendar` WHERE d = CURRENT_DATE('Europe/Oslo')
"""

# The study's three-level bins (§14a/§14e): pooled out-of-sample, green ≈ 1–6 % bad days,
# red ≈ 55–61 %. Fixed cuts on the calibrated probability, not quantiles.
GREEN_MAX = 0.10
RED_MIN = 0.30

# An issue computed more than this after its due time is marked `late` (it saw post-cutoff
# reality — restart back-fills, backoff retries), and the payload lists a due-but-missing issue
# as overdue only past the same grace. Normal ticks land within a minute or two of the cutoff.
_GRACE_H = 10 / 60
# Failure backoff — the negative-caching twin of the holiday path: a failed issue is retried at
# most every _FAIL_COOLDOWN, and abandoned for the day after _MAX_FAILS_PER_DAY, so a persistent
# failure (a missing model in prd, a broken feed) costs a bounded number of billable scans.
_FAIL_COOLDOWN = timedelta(minutes=15)
_MAX_FAILS_PER_DAY = 8


@dataclass(frozen=True)
class _IssueSpec:
    key: str  # "0630" — also the issue's display time "06:30"
    due_h: float  # Oslo fractional hour the issue becomes computable
    model: str  # suffix of disruptions.togvarsel_m_*
    scope: str  # what window the probability is about, for display
    kind: str  # "am" | "pm" — which reason rules apply
    suffix: str  # feature-column suffix for the reason rules


ISSUE_SPECS: tuple[_IssueSpec, ...] = (
    _IssueSpec("0630", 6.5, "am0630", "morgenrushet 06:30–09:00", "am", "0630"),
    _IssueSpec("0700", 7.0, "am0700", "morgenrushet 07:00–09:00", "am", "0700"),
    _IssueSpec("1200", 12.0, "pm1200", "ettermiddagsrushet 15:00–18:00", "pm", "1200"),
    _IssueSpec("1400", 14.0, "pm1400", "ettermiddagsrushet 15:00–18:00", "pm", "1400"),
)

# Each rush has a baseline issue and a follow-up that REPLACES it on the page. The follow-up
# answers the same question over what is left of the same window from a sharper model (§14e), so
# two cards for one rush are not two forecasts — they are one forecast and its stale predecessor,
# and a reader with a train to catch has to work out which number still applies. Display only.
# The superseded issue is still computed, still recorded in togvarsel_issued and still handed to
# `issued_today()`, because the notify chain's change gate compares the follow-up's level against
# exactly the baseline it pushed (togvarsel_notify) — dropping it from `_state` would silence
# every friskmelding.
SUPERSEDED_BY: dict[str, str] = {"0630": "0700", "1200": "1400"}


class Issue(BaseModel):
    issue: str = Field(description="Issue time, HHMM (e.g. '0630')")
    scope: str
    p: float = Field(description="Calibrated probability of a bad window (trouble10 bar)")
    level: str = Field(description="green | yellow | red")
    reasons: list[str]
    computed_at: datetime
    late: bool = Field(
        default=False,
        description="Computed more than 10 min after its due time (restart/backoff back-fill, "
        "so it saw post-cutoff reality) — skill audits must exclude these",
    )
    # Set on the /togmelding-api/today response only, on a COPY (_with_feedback_context): the
    # cached Issue is shared with issued_today()'s notify seam and stays a pure forecast.
    feedback_context_id: str | None = Field(
        default=None,
        description="Opaque, short-lived handle for the exact forecast rendering; null when disabled.",
    )
    feedback_enabled: bool = Field(
        default=False,
        description="Whether this server currently accepts isolated feedback for this forecast.",
    )


class KnownDeviation(BaseModel):
    summary: str | None
    cls: str
    planned: bool
    n_am_rush_journeys: int
    n_pm_rush_journeys: int


class Togmelding(BaseModel):
    date: date
    generated_at: datetime | None
    forecast_day: bool = Field(description="False on weekends/holidays — no forecast issued")
    no_forecast_reason: str | None = None
    next_issue: str | None = Field(default=None, description="HHMM of the next issue not yet due")
    overdue_issues: list[str] = Field(
        default_factory=list,
        description="HHMM of issues past due (plus a 10 min grace) but not issued — the forecast "
        "is delayed or failed, as opposed to not yet due",
    )
    issues: list[Issue]
    pm_outlook: str | None = None
    known_deviations: list[KnownDeviation]
    skill_note: str


class ConfigResponse(BaseModel):
    """What the page needs to describe itself, independent of the forecast."""

    environment: str | None = Field(
        description='Non-prod environment name ("dev", "tst") for the page to show in its title; '
        "null in prd.",
    )


_bq_client: Any = None
_lock = threading.Lock()
_state: dict[str, Any] = {}  # guarded by _lock; see _blank_state
_issued_table_ready = False


def configure(*, bq_client: Any) -> None:
    global _bq_client
    _bq_client = bq_client


def _blank_state(d: date) -> dict[str, Any]:
    return {
        "date": d,
        "issues": {},  # key -> Issue
        "sx": [],  # list[KnownDeviation]
        "holiday": False,
        "calendar_checked": False,  # the rollover togvarsel_calendar probe ran for this day
        "backoff": {},  # key -> {"fails": int, "until": datetime}; see _note_failure
        "generated_at": None,
    }


def _f(row: dict, name: str) -> float:
    """A feature value as a float, with NULL (no data) read as 0 for rule purposes."""
    v = row.get(name)
    return 0.0 if v is None else float(v)


def level_for(p: float) -> str:
    if p < GREEN_MAX:
        return "green"
    if p < RED_MIN:
        return "yellow"
    return "red"


def _am_reasons(row: dict, suffix: str, sx: list[dict]) -> list[str]:
    """Why the morning probability is what it is, in the features' own terms.

    Rule-based and threshold-y on purpose: every sentence is a direct reading of a feature the
    model actually uses, so the reason can never say something the forecast did not see."""
    cut = f"{suffix[:2]}:{suffix[2:]}"
    out: list[str] = []
    cxj = int(_f(row, f"et_cxj_{suffix}"))
    if cxj >= 1:
        out.append(f"{cxj} tog er innstilt før kl. {cut}")
    if _f(row, f"et_t5_last_{suffix}") >= 0.25:
        out.append("forsinkelser i lokaltrafikken den siste halvtimen")
    elif _f(row, f"et_t5_{suffix}") >= 0.20:
        out.append("forsinkelser i morgentimene så langt")
    amj = int(_f(row, f"sx_amj_rest_{suffix}") if suffix == "0700" else _f(row, f"sx_amj_{suffix}"))
    if amj >= 2:
        out.append(f"{amj} rushtidsavganger er allerede flagget i avviksmeldingene")
    if _f(row, f"sx_big_{suffix}") >= 1:
        big = next((s for s in sx if s.get("oslo") and s.get("cls") in
                    ("buss_for_tog", "stengt", "faerre_avganger") and s.get("summary")), None)
        out.append(f"større avvik annonsert: «{big['summary']}»" if big
                   else "større avvik annonsert (buss for tog / stengt strekning)")
    if _f(row, f"catj_infra_am_{suffix}") >= 1:
        out.append("meldt infrastrukturproblem som rammer rushtidstog")
    if _f(row, f"cat_drift_personale_{suffix}") >= 1:
        out.append("personalmangel meldt")
    if suffix == "0630" and _f(row, "sx_mt_0630") >= 1:
        out.append("manglende togsett meldt")
    if _f(row, "prev_eve_t5") >= 0.25:
        out.append("problemer i togtrafikken i går kveld")
    if not out:
        out.append("ingen tegn til trøbbel i morgentimene")
    return out[:4]


def _pm_reasons(row: dict, suffix: str, sx: list[dict]) -> list[str]:
    out: list[str] = []
    cx = int(_f(row, f"et_cx_{suffix}"))
    if cx >= 5:
        out.append(f"{cx} innstilte avganger så langt i dag")
    if _f(row, f"et_t5_last_{suffix}") >= 0.25:
        out.append("forsinkelser den siste timen")
    elif _f(row, f"et_t5_{suffix}") >= 0.20:
        out.append("forsinkelser gjennom dagen så langt")
    pmj_inn = int(_f(row, f"sx_pmj_inn_{suffix}"))
    if pmj_inn >= 1:
        out.append(f"{pmj_inn} avganger i ettermiddagsrushet er allerede innstilt")
    if _f(row, f"sx_big_{suffix}") >= 1:
        out.append("større avvik annonsert (buss for tog / stengt strekning)")
    if not out:
        out.append("normal drift så langt i dag")
    return out[:4]


def reasons_for(spec: _IssueSpec, row: dict, sx: list[dict]) -> list[str]:
    if spec.kind == "am":
        return _am_reasons(row, spec.suffix, sx)
    return _pm_reasons(row, spec.suffix, sx)


def skill_note_for(d: date) -> str:
    """§14k item 7: report skill honestly, by season (§14f: winter mornings are harder)."""
    if d.month in (10, 11, 12, 1, 2, 3):
        return (
            "Vinterhalvåret er vanskeligere å varsle: plutselige infrastrukturfeil kommer oftere "
            "og uten forvarsel, så regn med flere bomvarsler enn om sommeren."
        )
    return (
        "Treffsikkerheten er omtrent som et par dagers værvarsel: grønne morgener er ~2× "
        "tryggere enn snittet, røde ~3× verre. Plutselige feil etter utsendelse fanges ikke."
    )


def pm_outlook_for(issues: dict[str, Issue]) -> str | None:
    """§8 part 3: before 12:00 the PM outlook is two deterministic states keyed on the morning;
    from 12:00 the PM issues carry the real (modelled) outlook and this becomes a pointer."""
    if "1400" in issues or "1200" in issues:
        latest = issues.get("1400") or issues.get("1200")
        return f"se prognosen fra kl. {latest.issue[:2]}:{latest.issue[2:]}"
    morning = issues.get("0700") or issues.get("0630")
    if morning is None:
        return None
    if morning.level == "red":
        return (
            "noe forhøyet risiko i ettermiddag — etter en dårlig morgen har ettermiddagsrushet "
            "historisk hatt ~32 % sjanse for trøbbel, mot ~11 % ellers; oppdatert prognose kl. 12 og 14"
        )
    return "normale utsikter for ettermiddagsrushet; oppdatert prognose kl. 12 og 14"


# Announced-deviation classes worth repeating in the deterministic list (crowding — færre
# vogner — included: it is the one crowding signal that exists, §9 of the findings doc).
_DEVIATION_CLASSES = frozenset(
    {"buss_for_tog", "stengt", "faerre_avganger", "innstilt", "faerre_vogner", "ny_kjoererute"}
)


def known_deviations_for(sx: list[dict]) -> list[KnownDeviation]:
    out = []
    for s in sx:
        if not s.get("oslo"):
            continue
        if not (s.get("planned") or s.get("cls") in _DEVIATION_CLASSES):
            continue
        out.append(
            KnownDeviation(
                summary=s.get("summary"),
                cls=str(s.get("cls")),
                planned=bool(s.get("planned")),
                n_am_rush_journeys=int(s.get("n_amj") or 0),
                n_pm_rush_journeys=int(s.get("n_pmj") or 0),
            )
        )
    return out[:12]


def _rows(query: str) -> list[dict]:
    return [dict(r) for r in _bq_client.query(query).result()]


def _record_issue(d: date, issue: Issue, model: str) -> None:
    """Record the issued forecast for later skill audits (first write per (d, issue) wins).
    Failure is logged, never raised — the forecast the traveller sees must not depend on the
    bookkeeping write. timeout=60 on both calls: this sits in front of the poller thread, the
    same latch-guarded shape decisions.py and feedback_contexts.py bound the same way."""
    global _issued_table_ready
    try:
        # Deferred import so hermetic tests need no google libs.
        from google.cloud import bigquery

        if not _issued_table_ready:
            _bq_client.query(_ISSUED_DDL).result(timeout=60)
            _issued_table_ready = True
        cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("d", "DATE", d),
                bigquery.ScalarQueryParameter("issue", "STRING", issue.issue),
                bigquery.ScalarQueryParameter("model", "STRING", model),
                bigquery.ScalarQueryParameter("p", "FLOAT64", issue.p),
                bigquery.ScalarQueryParameter("level", "STRING", issue.level),
                bigquery.ScalarQueryParameter("computed_at", "TIMESTAMP", issue.computed_at),
                bigquery.ScalarQueryParameter("late", "BOOL", issue.late),
            ]
        )
        _bq_client.query(_ISSUED_MERGE, job_config=cfg).result(timeout=60)
    except Exception:
        logger.exception("togvarsel: recording issued forecast failed (forecast still served)")


def due_issues(now_h: float, computed: set[str]) -> list[_IssueSpec]:
    return [s for s in ISSUE_SPECS if s.due_h <= now_h and s.key not in computed]


def _note_failure(key: str, now: datetime) -> None:
    """Register a failed attempt for an issue (call with _lock held)."""
    b = _state["backoff"].setdefault(key, {"fails": 0, "until": now})
    b["fails"] += 1
    if b["fails"] >= _MAX_FAILS_PER_DAY:
        b["until"] = datetime.max.replace(tzinfo=_OSLO)  # state resets at day rollover
        logger.error("togvarsel: issue %s failed %d times; giving up for today", key, b["fails"])
    else:
        b["until"] = now + _FAIL_COOLDOWN


def _retryable(key: str, now: datetime) -> bool:
    """Whether the issue is out of its failure cooldown (call with _lock held)."""
    b = _state["backoff"].get(key)
    return b is None or now >= b["until"]


def refresh(now: datetime | None = None) -> None:
    """Compute any due-but-missing issues. Called on every poller tick; blocking BigQuery I/O.

    One features rebuild per tick at most, shared by every issue computed on that tick. Each
    issue fails independently and backs off independently — one broken model must neither drop
    the issues already computed nor re-run the billable feature scan every tick."""
    now = now.astimezone(_OSLO) if now else datetime.now(_OSLO)
    today = now.date()
    now_h = now.hour + now.minute / 60.0

    with _lock:
        if _state.get("date") != today:
            _state.clear()
            _state.update(_blank_state(today))
        if now.weekday() >= 5 or _state["holiday"]:
            return
        probe_calendar = not _state["calendar_checked"]

    # First weekday tick of the day: one tiny calendar lookup, so a holiday reads "helligdag"
    # from midnight instead of from the first feature rebuild after 06:30. Best effort — the
    # feature row's flag (same table) still gates every forecast.
    if probe_calendar:
        holiday = False
        try:
            cal_rows = _rows(_CALENDAR_TODAY_SQL)
            if cal_rows:
                holiday = bool(cal_rows[0]["holiday"])
            else:
                logger.error(
                    "togvarsel: togvarsel_calendar has no row for %s — extend "
                    "data/queries/togvarsel/00_calendar.sql and re-run copy_runtime_to_prd.sh",
                    today,
                )
        except Exception:
            logger.exception("togvarsel: calendar probe failed; the feature row still gates")
        with _lock:
            _state["calendar_checked"] = True
            _state["holiday"] = _state["holiday"] or holiday
        if holiday:
            logger.info("togvarsel: %s is a holiday; no forecast issued", today)
            return

    with _lock:
        missing = [s for s in due_issues(now_h, set(_state["issues"])) if _retryable(s.key, now)]
    if not missing:
        return

    # Rebuild the live feature row + today's SX list (one multi-statement job), then read both.
    # A failure here charges every issue the rebuild was serving, capping the daily spend.
    try:
        _bq_client.query(_FEATURES_TODAY_SQL).result()
        rows = _rows(_FEATURES_ROW_SQL)
        if not rows:
            raise RuntimeError("togvarsel_features_today came back empty")
        row = rows[0]
        sx = _rows(_SX_TODAY_SQL)
    except Exception:
        logger.exception("togvarsel: feature rebuild failed; backing off")
        with _lock:
            for spec in missing:
                _note_failure(spec.key, now)
        return

    if row.get("holiday"):
        with _lock:
            _state["holiday"] = True
            _state["sx"] = known_deviations_for(sx)
            _state["generated_at"] = now
        logger.info("togvarsel: %s is a holiday; no forecast issued", today)
        return

    for spec in missing:
        try:
            p_rows = _rows(_PREDICT_SQL.format(model=spec.model))
            if not p_rows:
                raise RuntimeError(f"ML.PREDICT for {spec.model} returned no probability row")
            p = float(p_rows[0]["p"])
        except Exception:
            logger.exception("togvarsel: computing issue %s failed; backing off", spec.key)
            with _lock:
                _note_failure(spec.key, now)
            continue
        issue = Issue(
            issue=spec.key,
            scope=spec.scope,
            p=round(p, 3),
            level=level_for(p),
            reasons=reasons_for(spec, row, sx),
            computed_at=now,
            late=now_h > spec.due_h + _GRACE_H,
        )
        # The served state first, the audit write second: bookkeeping trouble must never cost
        # the traveller a computed forecast.
        with _lock:
            _state["issues"][spec.key] = issue
            _state["backoff"].pop(spec.key, None)
        _record_issue(today, issue, spec.model)
        logger.info(
            "togvarsel: issued %s p=%.3f level=%s",
            spec.key, p, issue.level,
            extra={"fields": {"event": "togvarsel_issue", "issue": spec.key,
                              "p": p, "level": issue.level}},
        )

    with _lock:
        _state["sx"] = known_deviations_for(sx)
        _state["generated_at"] = now


def issued_today(now: datetime | None = None) -> list[dict[str, Any]]:
    """Today's issued issues as plain dicts — togvarsel_notify's injected snapshot, so that
    module never reads ``_state`` (main wires this in). Empty before the day's first
    refresh, on a stale (yesterday's) state, and on weekends/holidays where no issues exist.
    Each dict is a full Issue dump (issue, level, scope, p, reasons, computed_at, late) — the
    notify chain's wording reads scope and reasons on top of the policy's issue+level."""
    now = now.astimezone(_OSLO) if now else datetime.now(_OSLO)
    with _lock:
        if _state.get("date") != now.date():
            return []
        return [issue.model_dump() for issue in _state["issues"].values()]


def visible_issues(issues: dict[str, Issue]) -> list[Issue]:
    """Today's issues in issue order, with each superseded baseline dropped once its follow-up
    has been issued. A baseline whose follow-up has NOT arrived (not yet due, failed, backing
    off) is still the current forecast for its rush and stays."""
    return [issues[k] for k in sorted(issues) if SUPERSEDED_BY.get(k) not in issues]


def build_melding(now: datetime) -> Togmelding:
    """The response payload as a pure function of (cached state, now) — recomputed per request,
    so next_issue and the weekend gate can never serve stale."""
    now = now.astimezone(_OSLO)
    today = now.date()
    with _lock:
        fresh = _state.get("date") == today
        issues = dict(_state["issues"]) if fresh else {}
        sx = list(_state["sx"]) if fresh else []
        holiday = bool(_state.get("holiday")) if fresh else False
        generated_at = _state.get("generated_at") if fresh else None

    weekend = now.weekday() >= 5
    now_h = now.hour + now.minute / 60.0
    upcoming = [s for s in ISSUE_SPECS if s.due_h > now_h]
    # Past due (plus grace) but not issued: the honest "delayed or failed" signal, distinct from
    # "not yet due" — without it the page cannot tell a quiet 06:00 from a broken 20:00.
    overdue = (
        []
        if (weekend or holiday)
        else [
            s.key
            for s in ISSUE_SPECS
            if now_h > s.due_h + _GRACE_H
            and s.key not in issues
            # A missing baseline whose follow-up did arrive is not a hole the reader can act on:
            # the follow-up replaced it. Reported, the page would print "Prognosen kl. 06:30 er
            # forsinket" directly above the 07:00 card that superseded it.
            and SUPERSEDED_BY.get(s.key) not in issues
        ]
    )
    no_forecast = None
    if weekend:
        no_forecast = "Rushvarselet lages bare på hverdager."
    elif holiday:
        no_forecast = "Helligdag — rushvarselet lages bare på vanlige hverdager."

    return Togmelding(
        date=today,
        generated_at=generated_at,
        forecast_day=not (weekend or holiday),
        no_forecast_reason=no_forecast,
        next_issue=upcoming[0].key if (not weekend and not holiday and upcoming) else None,
        overdue_issues=overdue,
        issues=visible_issues(issues),
        pm_outlook=None if (weekend or holiday) else pm_outlook_for(issues),
        known_deviations=sx,
        skill_note=skill_note_for(today),
    )


# Literal level line mirrored from the page's renderer (LEVEL_TEXT in
# demo/togmelding/src/format.ts) — the varsel._feedback_detail convention: the persisted snapshot
# must say what the traveller actually read, not a paraphrase of it.
# test_feedback_snapshot_mirrors_the_page pins the mirror.
_LEVEL_TEXT = {
    "green": "liten fare for forstyrrelser",
    "yellow": "noe usikkert",
    "red": "stor fare for forstyrrelser",
}


def _feedback_snapshot(issue: Issue, d: date) -> dict[str, Any]:
    """The exact rendering of one issued forecast, shaped for the shared feedback context table.

    Same key set as varsel's per-alert snapshot — these ARE varsel_feedback_contexts' columns, and
    persist() writes them positionlessly by name — with the forecast discriminated the documented
    way: display_source='togvarsel' and a KIX:Togvarsel:<date>:<issue> situation number, never a
    new column or PostHog event (a new event name would drop these rows out of the existing
    exporter/reviewer, exactly the meldinger-rename lesson). Every value is a pure function of the
    issued Issue, so the fingerprint is byte-stable across requests and the page's 5-minute
    polling refreshes one lease per issue instead of minting fresh contexts.
    """
    return {
        "display_source": "togvarsel",
        # JavaScript Math.round (used by the page) rounds non-negative .5 upward; Python's round
        # uses ties-to-even and would make the persisted "exact" text disagree at half percents.
        "displayed_text": f"{_LEVEL_TEXT.get(issue.level, issue.level)}"
        f" (ca. {int(issue.p * 100 + 0.5)} % sjanse for mye trøbbel)",
        "detail_text": f"Kl. {issue.issue[:2]}:{issue.issue[2:]} — {issue.scope}",
        "observation_text": None,
        "situation_number": f"KIX:Togvarsel:{d.isoformat()}:{issue.issue}",
        "operator": "VYG",  # the forecast's whole scope: Vy's Oslo locals
        "line": None,
        "departure_time": None,
        "journey_context": None,
        "message_input": None,
        "llm_provider": None,
        "llm_model": None,
        "prompt_version": None,
        "message_status": None,
        "prediction": {
            "issue": issue.issue,
            "scope": issue.scope,
            "p": issue.p,
            "level": issue.level,
            # The reasons are traveller-visible (the card's bullet list), so they belong in the
            # snapshot: a "not useful" beside a wrong-sounding reason is the actionable reading.
            "reasons": issue.reasons,
            "computed_at": issue.computed_at.isoformat(),
            "late": issue.late,
        },
        "policy": {"green_max": GREEN_MAX, "red_min": RED_MIN},
        "delivery": {"surface": "togmelding"},
    }


def _with_feedback_context(issue: Issue, d: date) -> Issue:
    """A copy of the cached Issue carrying a feedback capability for this exact rendering."""
    context_id = feedback_contexts.mint(_feedback_snapshot(issue, d))
    # mint() returns None when the context cache is full; the card then simply offers no widget.
    return issue.model_copy(
        update={"feedback_context_id": context_id, "feedback_enabled": context_id is not None}
    )


@router.get("/togmelding-api/config", response_model=ConfigResponse)
def togmelding_config() -> ConfigResponse:
    """Which deployment is serving the page, for the "(dev)" it appends to its heading and title.

    Its own endpoint rather than a field on ``/today``, the way /historikk and /meldinger already
    do it: /today can be empty or stale for perfectly ordinary reasons (a weekend, a failed
    feature rebuild, a holiday), and "am I looking at dev or prd?" matters most exactly then.
    This one reads GOOGLE_CLOUD_PROJECT and touches nothing else.
    """
    return ConfigResponse(environment=environment_label())


@router.get("/togmelding-api/today", response_model=Togmelding)
def togmelding_today() -> Togmelding:
    """The page payload, each issued forecast annotated with a feedback capability when the
    /varsel feedback stack is configured (dev today; the flag and secrets are per-environment).
    Minting happens here on the request path — never in refresh() or build_melding(), which the
    poller and the notify chain also feed from — and on copies, so `_state` stays clean."""
    melding = build_melding(datetime.now(_OSLO))
    if melding.issues and feedback.enabled():
        melding.issues = [_with_feedback_context(i, melding.date) for i in melding.issues]
    return melding
