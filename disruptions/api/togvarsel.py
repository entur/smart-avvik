"""/togmelding-api — Togmeldingen: a rush-hour "weather forecast" for the Oslo local network.

Implements §8 of docs/togvarsel/02-findings-2026-08-27.md as revised by §14k. Four issues on
weekdays, each a calibrated probability that the rush is bad on the product bar (a call cancelled
OR ≥10 min late), mapped to green/yellow/red at the study's fixed bins:

    06:30  whole AM rush 06:30–09:00   model disruptions.togvarsel_m_am0630
    07:00  rest of the rush 07–09      model disruptions.togvarsel_m_am0700  (the sharpest, §14e)
    12:00  PM rush 15–18               model disruptions.togvarsel_m_pm1200
    14:00  PM rush 15–18               model disruptions.togvarsel_m_pm1400

Between the morning issues and 12:00 the PM outlook is DETERMINISTIC two-state prose (§8 part 3:
"normale utsikter" vs "noe forhøyet risiko" after a red morning) — the study found no real PM
skill before ~12:00, and pretending otherwise with a probability would be theatre. The "kjente
avvik" list is likewise deterministic: it repeats what SIRI-SX already announces, it predicts
nothing. Reasons on each issue are rule-based sentences derived from the same feature values the
model saw — the features are human-readable by construction, no text generation involved.

How it runs: :func:`refresh` is called from the poller's rebuild cycle (isolated there like every
other step). On each tick it computes the issues that are DUE and not yet computed today — so a
restart at 13:00 back-fills 06:30/07:00/12:00 retroactively, which is sound because every feature
column is defined by a fixed clock cutoff, not by "now" (recomputing later only gives the ET
denominators more resolved calls, i.e. numbers closer to the training-time batch view). One tick
does at most one rebuild of disruptions.togvarsel_features_today (shared by every issue computed
that tick) plus one ML.PREDICT per issue. Weekends and the holiday list (the feature row carries
the flag) get an honest "ingen prognose i dag" instead of a forecast trained on days like them.

Every issued forecast is appended to disruptions.togvarsel_issued so realized skill can be
audited later against the training chain's labels (§14k item 8: claims come with verification,
and the first product-side claim is the forecast itself).

Reads only; no import of main (the package rule): main injects the BigQuery client via
:func:`configure`. Model names are fixed literals here — nothing user-supplied ever reaches SQL.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
  d DATE, issue STRING, model STRING, p FLOAT64, level STRING, computed_at TIMESTAMP
)
"""
_ISSUED_INSERT = """
INSERT INTO `disruptions.togvarsel_issued` (d, issue, model, p, level, computed_at)
VALUES (@d, @issue, @model, @p, @level, @computed_at)
"""

# The study's three-level bins (§14a/§14e): pooled out-of-sample, green ≈ 1–6 % bad days,
# red ≈ 55–61 %. Fixed cuts on the calibrated probability, not quantiles.
GREEN_MAX = 0.10
RED_MIN = 0.30


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
    _IssueSpec("0700", 7.0, "am0700", "resten av morgenrushet 07:00–09:00", "am", "0700"),
    _IssueSpec("1200", 12.0, "pm1200", "ettermiddagsrushet 15:00–18:00", "pm", "1200"),
    _IssueSpec("1400", 14.0, "pm1400", "ettermiddagsrushet 15:00–18:00", "pm", "1400"),
)


class Issue(BaseModel):
    issue: str = Field(description="Issue time, HHMM (e.g. '0630')")
    scope: str
    p: float = Field(description="Calibrated probability of a bad window (trouble10 bar)")
    level: str = Field(description="green | yellow | red")
    reasons: list[str]
    computed_at: datetime


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
    issues: list[Issue]
    pm_outlook: str | None = None
    known_deviations: list[KnownDeviation]
    skill_note: str


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
    """Append the issued forecast for later skill audits. Failure is logged, never raised —
    the forecast the traveller sees must not depend on the bookkeeping write."""
    global _issued_table_ready
    try:
        # Deferred import so hermetic tests need no google libs.
        from google.cloud import bigquery

        if not _issued_table_ready:
            _bq_client.query(_ISSUED_DDL).result()
            _issued_table_ready = True
        cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("d", "DATE", d),
                bigquery.ScalarQueryParameter("issue", "STRING", issue.issue),
                bigquery.ScalarQueryParameter("model", "STRING", model),
                bigquery.ScalarQueryParameter("p", "FLOAT64", issue.p),
                bigquery.ScalarQueryParameter("level", "STRING", issue.level),
                bigquery.ScalarQueryParameter("computed_at", "TIMESTAMP", issue.computed_at),
            ]
        )
        _bq_client.query(_ISSUED_INSERT, job_config=cfg).result()
    except Exception:
        logger.exception("togvarsel: recording issued forecast failed (forecast still served)")


def due_issues(now_h: float, computed: set[str]) -> list[_IssueSpec]:
    return [s for s in ISSUE_SPECS if s.due_h <= now_h and s.key not in computed]


def refresh(now: datetime | None = None) -> None:
    """Compute any due-but-missing issues. Called from the poller cycle; blocking BigQuery I/O.

    One features rebuild per tick at most, shared by every issue computed on that tick."""
    now = now.astimezone(_OSLO) if now else datetime.now(_OSLO)
    today = now.date()

    with _lock:
        if _state.get("date") != today:
            _state.clear()
            _state.update(_blank_state(today))
        if now.weekday() >= 5 or _state["holiday"]:
            return
        missing = due_issues(now.hour + now.minute / 60.0, set(_state["issues"]))
    if not missing:
        return

    # Rebuild the live feature row + today's SX list (one multi-statement job), then read both.
    _bq_client.query(_FEATURES_TODAY_SQL).result()
    rows = _rows(_FEATURES_ROW_SQL)
    if not rows:
        logger.warning("togvarsel: features_today came back empty; skipping this tick")
        return
    row = rows[0]
    sx = _rows(_SX_TODAY_SQL)

    if row.get("holiday"):
        with _lock:
            _state["holiday"] = True
            _state["sx"] = known_deviations_for(sx)
            _state["generated_at"] = now
        logger.info("togvarsel: %s is a holiday; no forecast issued", today)
        return

    computed: dict[str, Issue] = {}
    for spec in missing:
        p_rows = _rows(_PREDICT_SQL.format(model=spec.model))
        if not p_rows:
            logger.warning("togvarsel: ML.PREDICT for %s returned nothing", spec.model)
            continue
        p = float(p_rows[0]["p"])
        issue = Issue(
            issue=spec.key,
            scope=spec.scope,
            p=round(p, 3),
            level=level_for(p),
            reasons=reasons_for(spec, row, sx),
            computed_at=now,
        )
        computed[spec.key] = issue
        _record_issue(today, issue, spec.model)
        logger.info(
            "togvarsel: issued %s p=%.3f level=%s",
            spec.key, p, issue.level,
            extra={"fields": {"event": "togvarsel_issue", "issue": spec.key,
                              "p": p, "level": issue.level}},
        )

    with _lock:
        _state["issues"].update(computed)
        _state["sx"] = known_deviations_for(sx)
        _state["generated_at"] = now


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
    no_forecast = None
    if weekend:
        no_forecast = "Togmeldingen lages bare på hverdager."
    elif holiday:
        no_forecast = "Helligdag — togmeldingen lages bare på vanlige hverdager."

    return Togmelding(
        date=today,
        generated_at=generated_at,
        forecast_day=not (weekend or holiday),
        no_forecast_reason=no_forecast,
        next_issue=upcoming[0].key if (not weekend and not holiday and upcoming) else None,
        issues=[issues[k] for k in sorted(issues)],
        pm_outlook=None if (weekend or holiday) else pm_outlook_for(issues),
        known_deviations=sx,
        skill_note=skill_note_for(today),
    )


@router.get("/togmelding-api/today", response_model=Togmelding)
def togmelding_today() -> Togmelding:
    return build_melding(datetime.now(_OSLO))
