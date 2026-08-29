"""Checks for /togmelding-api — the rush-hour forecast (togvarsel.py).

Hermetic: BigQuery is replaced with a fake that dispatches on the SQL it is handed and replays
canned rows — nothing here touches BigQuery, the models, or the network. The checks that matter
most are the gates: a weekend or holiday must never get a forecast (the models never saw such
days), an issue must never be computed before its clock cutoff, and a failed prediction must
degrade to "issue missing" rather than a wrong number or a dead endpoint.

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery python test_togvarsel.py  (or under pytest)
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import togvarsel
from fastapi import FastAPI
from fastapi.testclient import TestClient

_OSLO = ZoneInfo("Europe/Oslo")

# 2026-08-31 is a Monday; 2026-09-05 a Saturday.
MONDAY_0710 = datetime(2026, 8, 31, 7, 10, tzinfo=_OSLO)
MONDAY_1430 = datetime(2026, 8, 31, 14, 30, tzinfo=_OSLO)
SATURDAY = datetime(2026, 9, 5, 8, 0, tzinfo=_OSLO)


def _feature_row(**overrides) -> dict:
    """A quiet weekday feature row; override per test. Only rule-relevant columns need to exist —
    the fake never runs the models, so the model-only columns are irrelevant here."""
    base = {
        "d": date(2026, 8, 31),
        "holiday": False,
        "low_season": False,
        "et_cxj_0630": 0, "et_cxj_0700": 0, "et_t5_0630": 0.02, "et_t5_0700": 0.02,
        "et_t5_last_0630": 0.0, "et_t5_last_0700": 0.0,
        "et_cx_1200": 0, "et_cx_1400": 0, "et_t5_1200": 0.03, "et_t5_1400": 0.03,
        "et_t5_last_1200": 0.0, "et_t5_last_1400": 0.0,
        "sx_amj_0630": 0, "sx_amj_rest_0700": 0,
        "sx_big_0630": 0, "sx_big_0700": 0, "sx_big_1200": 0, "sx_big_1400": 0,
        "sx_mt_0630": 0, "sx_pmj_inn_1200": 0, "sx_pmj_inn_1400": 0,
        "catj_infra_am_0630": 0, "catj_infra_am_0700": 0,
        "cat_drift_personale_0630": 0, "cat_drift_personale_0700": 0,
        "prev_eve_t5": 0.05,
    }
    base.update(overrides)
    return base


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _FakeBQ:
    """Dispatches on the SQL text: the feature-rebuild script and bookkeeping writes return no
    rows; the three reads replay canned data. Records every query for assertions."""

    def __init__(self, feature_row=None, sx_rows=(), probs=None):
        self.feature_row = feature_row
        self.sx_rows = list(sx_rows)
        self.probs = probs or {}  # model suffix -> p (missing -> empty result)
        self.queries: list[str] = []
        self.inserted: list[str] = []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        if "ML.PREDICT" in sql:
            for model, p in self.probs.items():
                if f"togvarsel_m_{model}" in sql:
                    return _FakeJob([{"p": p}])
            return _FakeJob([])
        if "INSERT INTO" in sql:
            self.inserted.append(sql)
            return _FakeJob([])
        if "CREATE TABLE IF NOT EXISTS" in sql:
            return _FakeJob([])
        if sql.strip().startswith("SELECT") and "togvarsel_features_today" in sql:
            return _FakeJob([self.feature_row] if self.feature_row else [])
        if sql.strip().startswith("SELECT") and "togvarsel_sx_today" in sql:
            return _FakeJob(list(self.sx_rows))
        # the multi-statement feature rebuild script
        return _FakeJob([])


def _reset(fake) -> None:
    togvarsel.configure(bq_client=fake)
    with togvarsel._lock:
        togvarsel._state.clear()


# ---------------------------------------------------------------------------- pure rules

def test_levels_are_the_studys_bins():
    assert togvarsel.level_for(0.0) == "green"
    assert togvarsel.level_for(0.099) == "green"
    assert togvarsel.level_for(0.10) == "yellow"
    assert togvarsel.level_for(0.299) == "yellow"
    assert togvarsel.level_for(0.30) == "red"
    assert togvarsel.level_for(0.95) == "red"


def test_due_issues_respect_the_clock_and_what_is_done():
    due = togvarsel.due_issues(6.0, set())
    assert due == []
    assert [s.key for s in togvarsel.due_issues(6.6, set())] == ["0630"]
    assert [s.key for s in togvarsel.due_issues(7.2, {"0630"})] == ["0700"]
    assert [s.key for s in togvarsel.due_issues(23.0, set())] == ["0630", "0700", "1200", "1400"]


def test_am_reasons_read_the_features():
    row = _feature_row(et_cxj_0630=2, sx_big_0630=1, prev_eve_t5=0.4)
    sx = [{"oslo": True, "cls": "buss_for_tog", "summary": "Buss for tog Asker–Sandvika"}]
    reasons = togvarsel._am_reasons(row, "0630", sx)
    assert "2 tog er innstilt før kl. 06:30" in reasons
    assert any("Buss for tog Asker–Sandvika" in r for r in reasons)
    assert len(reasons) <= 4


def test_quiet_morning_gets_the_fallback_reason():
    assert togvarsel._am_reasons(_feature_row(), "0630", []) == [
        "ingen tegn til trøbbel i morgentimene"
    ]


def test_pm_reasons_read_the_features():
    row = _feature_row(et_cx_1400=7, sx_pmj_inn_1400=3)
    reasons = togvarsel._pm_reasons(row, "1400", [])
    assert "7 innstilte avganger så langt i dag" in reasons
    assert "3 avganger i ettermiddagsrushet er allerede innstilt" in reasons


def test_pm_outlook_is_deterministic_until_the_pm_issues_exist():
    def issue(key, level):
        return togvarsel.Issue(issue=key, scope="x", p=0.5, level=level, reasons=[],
                               computed_at=MONDAY_0710)

    assert togvarsel.pm_outlook_for({}) is None
    assert "normale utsikter" in togvarsel.pm_outlook_for({"0630": issue("0630", "green")})
    assert "forhøyet risiko" in togvarsel.pm_outlook_for({"0700": issue("0700", "red")})
    # once a PM model has run, the outlook points at it instead of the morning heuristic
    both = {"0630": issue("0630", "red"), "1400": issue("1400", "green")}
    assert togvarsel.pm_outlook_for(both) == "se prognosen fra kl. 14:00"


def test_known_deviations_filter():
    sx = [
        {"oslo": True, "cls": "buss_for_tog", "planned": False, "summary": "Buss for tog", "n_amj": 2, "n_pmj": 0},
        {"oslo": False, "cls": "buss_for_tog", "planned": False, "summary": "Utenfor Oslo"},
        {"oslo": True, "cls": "forsinket", "planned": False, "summary": "Forsinket"},  # not announced-deviation class
        {"oslo": True, "cls": "other", "planned": True, "summary": "Planlagt arbeid"},
    ]
    got = togvarsel.known_deviations_for(sx)
    assert [k.summary for k in got] == ["Buss for tog", "Planlagt arbeid"]
    assert got[0].n_am_rush_journeys == 2


def test_skill_note_is_seasonal():
    assert "Vinterhalvåret" in togvarsel.skill_note_for(date(2026, 1, 15))
    assert "værvarsel" in togvarsel.skill_note_for(date(2026, 6, 15))


# ---------------------------------------------------------------------------- refresh

def test_refresh_computes_due_issues_and_records_them():
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.05, "am0700": 0.42})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710)
    with togvarsel._lock:
        issues = dict(togvarsel._state["issues"])
    assert set(issues) == {"0630", "0700"}
    assert issues["0630"].level == "green" and issues["0630"].p == 0.05
    assert issues["0700"].level == "red"
    # one feature rebuild (the script), then reads + predicts; the forecasts were recorded
    scripts = [q for q in fake.queries if "Live togvarsel features" in q]
    assert len(scripts) == 1
    assert len(fake.inserted) == 2


def test_refresh_is_idempotent_within_the_day():
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.05, "am0700": 0.42})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710)
    n = len(fake.queries)
    togvarsel.refresh(now=MONDAY_0710)  # nothing new due -> not a single query
    assert len(fake.queries) == n


def test_refresh_backfills_after_a_restart():
    fake = _FakeBQ(feature_row=_feature_row(),
                   probs={"am0630": 0.05, "am0700": 0.1, "pm1200": 0.2, "pm1400": 0.33})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_1430)
    with togvarsel._lock:
        assert set(togvarsel._state["issues"]) == {"0630", "0700", "1200", "1400"}


def test_weekend_runs_no_queries():
    fake = _FakeBQ(feature_row=_feature_row())
    _reset(fake)
    togvarsel.refresh(now=SATURDAY)
    assert fake.queries == []


def test_holiday_row_blocks_the_forecast():
    fake = _FakeBQ(feature_row=_feature_row(holiday=True), probs={"am0630": 0.5})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710)
    with togvarsel._lock:
        assert togvarsel._state["holiday"] is True
        assert togvarsel._state["issues"] == {}
    assert not any("ML.PREDICT" in q for q in fake.queries)
    # and later ticks stay quiet for the rest of the day
    n = len(fake.queries)
    togvarsel.refresh(now=MONDAY_1430)
    assert len(fake.queries) == n


def test_missing_prediction_degrades_to_missing_issue():
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.05})  # am0700 model absent
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710)
    with togvarsel._lock:
        assert set(togvarsel._state["issues"]) == {"0630"}


def test_day_rollover_resets_state():
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.05, "am0700": 0.1})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710)
    tuesday = datetime(2026, 9, 1, 6, 40, tzinfo=_OSLO)
    fake.probs = {"am0630": 0.2}
    togvarsel.refresh(now=tuesday)
    with togvarsel._lock:
        assert togvarsel._state["date"] == date(2026, 9, 1)
        assert set(togvarsel._state["issues"]) == {"0630"}


# ---------------------------------------------------------------------------- payload + endpoint

def test_build_melding_weekday_shape():
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.05, "am0700": 0.42})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710)
    m = togvarsel.build_melding(MONDAY_0710)
    assert m.forecast_day is True
    assert [i.issue for i in m.issues] == ["0630", "0700"]
    assert m.next_issue == "1200"
    assert m.pm_outlook is not None and "forhøyet" in m.pm_outlook  # 07:00 was red
    assert m.skill_note


def test_build_melding_weekend():
    _reset(_FakeBQ())
    m = togvarsel.build_melding(SATURDAY)
    assert m.forecast_day is False
    assert m.issues == [] and m.next_issue is None and m.pm_outlook is None
    assert "hverdager" in m.no_forecast_reason


def test_endpoint_serves():
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.05})
    _reset(fake)
    app = FastAPI()
    app.include_router(togvarsel.router)
    resp = TestClient(app).get("/togmelding-api/today")
    assert resp.status_code == 200
    body = resp.json()
    assert {"date", "forecast_day", "issues", "known_deviations", "skill_note"} <= set(body)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
