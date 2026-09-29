"""Checks for /togmelding-api — the rush-hour forecast (togvarsel.py).

Hermetic: BigQuery is replaced with a fake that dispatches on the SQL it is handed and replays
canned rows — nothing here touches BigQuery, the models, or the network. The checks that matter
most are the gates: a weekend or holiday must never get a forecast (the models never saw such
days), an issue must never be computed before its clock cutoff, and a failed prediction must
degrade to "issue missing" — never a wrong number or a dead endpoint — then back off instead of
re-running the billable feature scan every tick.

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery python test_togvarsel.py  (or under pytest)
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Rushvarsel's master gate, pinned ON before togvarsel is imported: every /togmelding-api route
# carries a router-level dependency on it and 404s while it is off (which is the default, and the
# posture every deployment is in today). Set at import, the way test_varsel_api pins
# KIX_ENVIRONMENT — each suite is its own process, deliberately (see the CI note), so this cannot
# leak into another. The 404 itself is checked by
# test_the_whole_api_404s_while_the_master_gate_is_off below, which turns it back off.
os.environ["SHOW_RUSHVARSEL"] = "true"

import togvarsel
from fastapi import FastAPI
from fastapi.testclient import TestClient

_OSLO = ZoneInfo("Europe/Oslo")

# 2026-08-31 is a Monday; 2026-09-05 a Saturday.
MONDAY_0635 = datetime(2026, 8, 31, 6, 35, tzinfo=_OSLO)  # only 06:30 is due
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


class _FakeNotFound(Exception):
    """Stands in for google.api_core.exceptions.NotFound: the real BigQuery client RAISES when a
    model or table is missing — it never returns an empty result set for that case."""


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self, timeout=None):
        return self._rows


class _FakeBQ:
    """Dispatches on the SQL text: the feature-rebuild script and bookkeeping writes return no
    rows; the reads replay canned data; an ML.PREDICT against a model not in `probs` raises,
    like the real client does. Records every query for assertions."""

    def __init__(self, feature_row=None, sx_rows=(), probs=None, calendar_holiday=False,
                 fail_rebuild=False):
        self.feature_row = feature_row
        self.sx_rows = list(sx_rows)
        self.probs = probs or {}  # model suffix -> p (a missing model RAISES)
        self.calendar_holiday = calendar_holiday  # None -> the calendar has no row for today
        self.fail_rebuild = fail_rebuild
        self.queries: list[str] = []
        self.inserted: list[str] = []
        self.merge_params: list[dict] = []  # per MERGE: {name: QueryParameter}
        self.ddl: list[str] = []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        if "ML.PREDICT" in sql:
            for model, p in self.probs.items():
                if f"togvarsel_m_{model}" in sql:
                    return _FakeJob([{"p": p}])
            raise _FakeNotFound("model not found")
        if "MERGE" in sql:
            self.inserted.append(sql)
            params = job_config.query_parameters if job_config is not None else []
            self.merge_params.append({q.name: q for q in params})
            return _FakeJob([])
        if "CREATE TABLE IF NOT EXISTS" in sql or sql.lstrip().startswith("ALTER TABLE"):
            self.ddl.append(sql)
            return _FakeJob([])
        if sql.strip().startswith("SELECT") and "togvarsel_calendar" in sql:
            return _FakeJob(
                [] if self.calendar_holiday is None else [{"holiday": self.calendar_holiday}]
            )
        if sql.strip().startswith("SELECT") and "togvarsel_features_today" in sql:
            return _FakeJob([self.feature_row] if self.feature_row else [])
        if sql.strip().startswith("SELECT") and "togvarsel_sx_today" in sql:
            return _FakeJob(list(self.sx_rows))
        # the multi-statement feature rebuild script
        if self.fail_rebuild:
            raise _FakeNotFound("rebuild failed")
        return _FakeJob([])


def _reset(fake) -> None:
    togvarsel.configure(bq_client=fake)
    # The readiness latch is a module global: left True by an earlier test, the CREATE/ALTER
    # pair never re-runs and a test about table readiness passes or fails on suite ORDER.
    togvarsel._issued_table_ready = False
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
    row = _feature_row(et_cxj_0630=2, prev_eve_t5=0.4)
    reasons = togvarsel._am_reasons(row, "0630")
    assert "2 tog er innstilt før kl. 06:30" in reasons
    assert "problemer i togtrafikken i går kveld" in reasons
    assert len(reasons) <= 4


def test_facts_the_model_does_not_credit_stay_out_of_the_reasons():
    """`sx_big` has a NEGATIVE weight in every live model and `sx_mt_0630` one of +0.09 with no
    lift in the training data (2026-09-03 measurement, _am_reasons' docstring), so a sentence
    reading either would argue for a level it did not raise. Both remain visible as FACTS — rows
    of the «kjente avvik» list — which is where 02-findings placed them."""
    row = _feature_row(sx_big_0630=1, sx_mt_0630=1, sx_big_1400=1)
    assert togvarsel._am_reasons(row, "0630") == ["ingen tegn til forstyrrelser i morgentimene"]
    assert togvarsel._pm_reasons(row, "1400") == ["normal drift så langt i dag"]
    sx = [
        {"oslo": True, "cls": "buss_for_tog", "summary": "Buss for tog Asker–Sandvika",
         "planned": True, "n_amj": 3, "n_pmj": 0},
        {"oslo": True, "cls": "faerre_vogner", "summary": "Færre vogner – vi mangler et togsett",
         "planned": False, "n_amj": 1, "n_pmj": 0},
    ]
    assert [k.summary for k in togvarsel.known_deviations_for(sx)] == [
        "Buss for tog Asker–Sandvika", "Færre vogner – vi mangler et togsett",
    ]


def test_quiet_morning_gets_the_fallback_reason():
    assert togvarsel._am_reasons(_feature_row(), "0630") == [
        "ingen tegn til forstyrrelser i morgentimene"
    ]


def test_pm_reasons_read_the_features():
    row = _feature_row(et_cx_1400=7, sx_pmj_inn_1400=3)
    reasons = togvarsel._pm_reasons(row, "1400")
    # ONE cancellation figure, scoped to the reader's rush (design review 2026-09-01, finding 17):
    # the day total and the rush count used to sit side by side in one push.
    assert reasons[0] == "3 avganger i ettermiddagsrushet er allerede innstilt"
    assert "7 innstilte avganger så langt i dag" not in reasons
    # Without a rush-scoped cancellation the day total is the figure that remains.
    assert "7 innstilte avganger så langt i dag" in togvarsel._pm_reasons(
        _feature_row(et_cx_1400=7), "1400")
    # Singular agreement (finding 4): never «1 avganger».
    assert togvarsel._pm_reasons(_feature_row(sx_pmj_inn_1400=1), "1400")[0] == (
        "1 avgang i ettermiddagsrushet er allerede innstilt"
    )


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
    assert got[0].n_situations == 1


def test_known_deviations_saying_the_same_thing_collapse_into_one_row():
    """Eleven «Færre vogner» messages are one row with the counts summed (design review
    2026-09-01, finding 3) -- and a different class or summary is still its own row."""
    sx = [
        {"oslo": True, "cls": "faerre_vogner", "planned": False, "summary": "Færre vogner", "n_amj": 1, "n_pmj": 0},
        {"oslo": True, "cls": "faerre_vogner", "planned": False, "summary": "Færre vogner ", "n_amj": 1, "n_pmj": 1},
        {"oslo": True, "cls": "faerre_vogner", "planned": False, "summary": "færre vogner", "n_amj": 0, "n_pmj": 1},
        {"oslo": True, "cls": "stengt", "planned": False, "summary": "Stengt: Ski", "n_amj": 3, "n_pmj": 0},
        {"oslo": True, "cls": "faerre_vogner", "planned": False, "summary": "Kortere tog Lillestrøm", "n_amj": 1},
    ]
    got = togvarsel.known_deviations_for(sx)
    assert [(k.cls, k.summary, k.n_situations) for k in got] == [
        ("faerre_vogner", "Færre vogner", 3),
        ("stengt", "Stengt: Ski", 1),
        ("faerre_vogner", "Kortere tog Lillestrøm", 1),
    ]
    assert (got[0].n_am_rush_journeys, got[0].n_pm_rush_journeys) == (2, 2)


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
    # 07:10 is 40 min past the 06:30 cutoff (a back-fill, hindsight risk) but only 10 min past
    # 07:00 — within the grace, so on time
    assert issues["0630"].late is True
    assert issues["0700"].late is False
    # one feature rebuild (the script), then reads + predicts; the forecasts were recorded
    # idempotently (MERGE keyed on (d, issue) — a restart's recompute can never duplicate a row)
    scripts = [q for q in fake.queries if "Live togvarsel features" in q]
    assert len(scripts) == 1
    assert len(fake.inserted) == 2
    assert all("MERGE" in q and "WHEN NOT MATCHED" in q for q in fake.inserted)


def _columns_declared(ddl: str) -> list[str]:
    """Column names in a one-column-per-line CREATE TABLE body."""
    body = ddl.split("(", 1)[1]
    return re.findall(r"^\s*(\w+)\s+(?:DATE|STRING|FLOAT64|TIMESTAMP|BOOL|ARRAY<STRING>)(?=[\s,)])",
                      body, re.MULTILINE)


def test_the_issued_write_names_every_column_it_declares():
    """CREATE ... IF NOT EXISTS is inert on the table every environment already has, so a column
    added to the DDL that the ALTER does not also add fails EVERY write against a deployed table —
    logged, forecast still served, and /rushhistorikk's spine quietly stops growing. Three things
    must agree: the columns the CREATE declares, the columns the MERGE inserts (each from a
    same-named parameter), and the columns the ALTER adds beyond the original seven."""
    declared = _columns_declared(togvarsel._ISSUED_DDL)
    insert_cols = re.search(r"INSERT \(([^)]*)\)", togvarsel._ISSUED_MERGE).group(1)
    values = re.search(r"VALUES \(([^)]*)\)", togvarsel._ISSUED_MERGE).group(1)
    inserted = [c.strip() for c in insert_cols.split(",")]
    assert inserted == declared
    assert [v.strip() for v in values.split(",")] == [f"@{c}" for c in inserted]
    original = ["d", "issue", "model", "p", "level", "computed_at", "late"]
    assert declared[: len(original)] == original
    altered = re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", togvarsel._ISSUED_ALTER)
    assert altered == declared[len(original):] == ["scope", "reasons"]


def test_the_recorded_forecast_carries_its_scope_and_reasons():
    """The reasons are the traveller-facing WHY, and until 2026-09-03 nothing durable held them:
    they lived in _state and the feature row they read was CREATE OR REPLACEd on the next rebuild.
    Now they ride the issued row exactly as served — same order, same text — and every
    @placeholder in the MERGE has a same-named parameter behind it. The buss-for-tog situation on
    the same morning lands in the «kjente avvik» list and NOT in the reasons (2026-09-03)."""
    row = _feature_row(et_cxj_0630=2, sx_big_0630=1)
    sx = [{"oslo": True, "cls": "buss_for_tog", "summary": "Buss for tog Asker–Sandvika",
           "planned": True, "n_amj": 3, "n_pmj": 0}]
    fake = _FakeBQ(feature_row=row, sx_rows=sx, probs={"am0630": 0.42})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0635)
    with togvarsel._lock:
        issue = togvarsel._state["issues"]["0630"]
        listed = [k.summary for k in togvarsel._state["sx"]]
    assert issue.reasons == ["2 tog er innstilt før kl. 06:30"]
    assert listed == ["Buss for tog Asker–Sandvika"]
    # the table was made ready before the first write: CREATE, then the ALTER for existing tables
    assert [d.lstrip().split()[0] for d in fake.ddl] == ["CREATE", "ALTER"]
    assert len(fake.merge_params) == 1
    params = fake.merge_params[0]
    placeholders = set(re.findall(r"@(\w+)", togvarsel._ISSUED_MERGE))
    assert set(params) == placeholders
    assert params["scope"].value == "morgenrushet 06:30–09:00" == issue.scope
    assert params["reasons"].array_type == "STRING"
    assert params["reasons"].values == issue.reasons
    assert issue.reasons  # never an empty array: the client would send it as NULL


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
        issues = dict(togvarsel._state["issues"])
    assert set(issues) == {"0630", "0700", "1200", "1400"}
    assert all(i.late for i in issues.values())  # every one saw post-cutoff reality


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


def test_missing_model_degrades_to_missing_issue_and_backs_off():
    """One model raising (prd before the model copy landed) must not drop the issues that DID
    compute, must not duplicate their audit rows, and must not re-run the billable feature
    rebuild every tick while it stays broken."""
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.05})  # am0700 model absent
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710)
    with togvarsel._lock:
        assert set(togvarsel._state["issues"]) == {"0630"}
    assert len(fake.inserted) == 1
    # cooldown: the very next tick runs no queries at all for the failed issue
    n = len(fake.queries)
    togvarsel.refresh(now=MONDAY_0710.replace(minute=11))
    assert len(fake.queries) == n
    # after the cooldown the issue is retried — and recovers when the model is back
    fake.probs["am0700"] = 0.2
    togvarsel.refresh(now=MONDAY_0710.replace(minute=40))
    with togvarsel._lock:
        issues = dict(togvarsel._state["issues"])
    assert set(issues) == {"0630", "0700"}
    assert issues["0700"].late is True  # recovered 40 min past its cutoff: hindsight-flagged
    assert len(fake.inserted) == 2  # 0630 was NOT re-recorded


def test_persistent_failure_gives_up_for_the_day():
    fake = _FakeBQ(feature_row=_feature_row(), probs={})  # every model absent
    _reset(fake)
    t = MONDAY_0710
    for _ in range(togvarsel._MAX_FAILS_PER_DAY):
        togvarsel.refresh(now=t)
        t += togvarsel._FAIL_COOLDOWN
    scripts = [q for q in fake.queries if "Live togvarsel features" in q]
    assert len(scripts) == togvarsel._MAX_FAILS_PER_DAY
    togvarsel.refresh(now=t + togvarsel._FAIL_COOLDOWN)  # given up: not even a rebuild
    assert len([q for q in fake.queries if "Live togvarsel features" in q]) == len(scripts)


def test_feature_rebuild_failure_backs_off():
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.05}, fail_rebuild=True)
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710)
    with togvarsel._lock:
        assert togvarsel._state["issues"] == {}
    n = len(fake.queries)
    togvarsel.refresh(now=MONDAY_0710.replace(minute=12))  # cooldown: no immediate retry
    assert len(fake.queries) == n


def test_calendar_probe_flags_a_holiday_before_the_first_issue():
    """The rollover probe reads togvarsel_calendar, so a holiday says 'helligdag' from the first
    tick of the day — not first when the 06:30 feature rebuild happens to run."""
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.5}, calendar_holiday=True)
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710.replace(hour=0, minute=5))
    with togvarsel._lock:
        assert togvarsel._state["holiday"] is True
    assert not any("Live togvarsel features" in q for q in fake.queries)
    m = togvarsel.build_melding(MONDAY_0710.replace(hour=0, minute=6))
    assert m.forecast_day is False and "Helligdag" in m.no_forecast_reason
    # and the rest of the day stays quiet
    n = len(fake.queries)
    togvarsel.refresh(now=MONDAY_0710)
    assert len(fake.queries) == n


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
    # 07:00 REPLACES 06:30 -- one card per rush, the newest of the pair. See SUPERSEDED_BY.
    assert [i.issue for i in m.issues] == ["0700"]
    assert m.next_issue == "1200"
    assert m.overdue_issues == []
    assert m.pm_outlook is not None and "forhøyet" in m.pm_outlook  # 07:00 was red
    assert m.skill_note


def test_a_follow_up_replaces_its_baseline_on_the_page():
    """07:00 replaces 06:30 and 14:00 replaces 12:00 -- one card per rush, the newest. Two cards
    for one rush are one forecast beside its stale predecessor, and a reader with a train to
    catch has to work out which of the two numbers still applies.

    The narrowing is display-only, which is the half that is easy to lose: the notify chain's
    change gate compares the follow-up's level against exactly the baseline it pushed, so a
    baseline dropped from `_state` (rather than from the payload) would silence every
    friskmelding.
    """
    fake = _FakeBQ(feature_row=_feature_row(),
                   probs={"am0630": 0.05, "am0700": 0.42, "pm1200": 0.2, "pm1400": 0.05})
    _reset(fake)

    # A baseline with no follow-up yet is still the current forecast for its rush.
    togvarsel.refresh(now=MONDAY_0710.replace(hour=6, minute=40))
    assert [i.issue for i in togvarsel.build_melding(MONDAY_0710).issues] == ["0630"]

    # The follow-up replaces it. And since a card is retired when its rush ends
    # (test_a_forecast_is_retired_when_its_rush_ends), at most one card is ever on the page:
    # the morning pair collapses to 07:00 while the morning lasts ...
    togvarsel.refresh(now=MONDAY_0710)
    assert [i.issue for i in togvarsel.build_melding(MONDAY_0710).issues] == ["0700"]

    # ... and the afternoon pair to 14:00, by which time the morning is long gone.
    togvarsel.refresh(now=MONDAY_1430)
    m = togvarsel.build_melding(MONDAY_1430)
    assert [i.issue for i in m.issues] == ["1400"]
    # ... while every issue is still computed, cached and handed to the notify seam.
    assert {i["issue"] for i in togvarsel.issued_today(now=MONDAY_1430)} == {
        "0630", "0700", "1200", "1400"
    }


def test_a_forecast_is_retired_when_its_rush_ends():
    """A card is about a window. Past that window it is a record of the past dressed as advice,
    and the reader has to work out for themselves that it no longer concerns them.

    prd, 2026-09-01 16:17: "Kl. 07:00 — morgenrushet 07:00–09:00 (Beregnet i etterkant, kl.
    15:38)". An afternoon restart clears `_state`, so the poller back-fills every due issue at
    once and marks it `late` — nothing was stale, the page simply had no rule for expiry.
    """
    fake = _FakeBQ(feature_row=_feature_row(),
                   probs={"am0630": 0.05, "am0700": 0.42, "pm1200": 0.2, "pm1400": 0.05})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_1430)  # the whole day computed at once, as a restart does

    # A card is retired when its own rush ENDS -- never when another one starts, so the
    # afternoon's forecast is on the page from the moment it is issued.
    at_0859 = MONDAY_0710.replace(hour=8, minute=59)
    assert [i.issue for i in togvarsel.build_melding(at_0859).issues] == ["0700", "1400"]
    # 09:00 sharp: the morning is over and its card goes; the afternoon's stays until 18:00.
    for hour in (9, 12, 17):
        assert [
            i.issue for i in togvarsel.build_melding(at_0859.replace(hour=hour, minute=0)).issues
        ] == ["1400"], hour

    # ... and every one of them is still cached, recorded and handed to the notify seam, whose
    # change gate compares a follow-up against exactly the baseline it pushed.
    evening = togvarsel.build_melding(MONDAY_1430.replace(hour=18, minute=1))
    assert evening.issues == []
    assert {i["issue"] for i in togvarsel.issued_today(now=MONDAY_1430.replace(hour=18, minute=1))} \
        == {"0630", "0700", "1200", "1400"}
    # An empty page for two opposite reasons: the day is done, not "the first one is coming".
    assert evening.rushes_over is True and evening.next_issue is None
    assert togvarsel.build_melding(MONDAY_0710.replace(hour=6)).rushes_over is False
    # The PM read-across goes with the rush it is about, or it points at a retired card.
    assert evening.pm_outlook is None
    assert togvarsel.build_melding(MONDAY_1430.replace(hour=17)).pm_outlook is not None


def test_the_rush_windows_agree_with_the_notify_chain():
    """/togmelding retires a card at RUSH_END_H; /varsel's panel drops the same rush at
    togvarsel_notify.RUSH_WINDOWS' end. Two surfaces of one product disagreeing about whether a
    rush is still a live topic is the failure — one showing the morning at 09:30 while the other
    has moved on. Pinned here rather than in test_togvarsel_notify, which stays hermetic."""
    import togvarsel_notify

    assert togvarsel.RUSH_END_H == {
        rush: end for rush, (_start, end) in togvarsel_notify.RUSH_WINDOWS.items()
    }
    # ... and every issue belongs to a rush both modules place the same way.
    assert {s.key: s.kind for s in togvarsel.ISSUE_SPECS} == togvarsel_notify.RUSH_OF_ISSUE


def test_a_superseded_baseline_is_not_reported_overdue():
    """A missing 06:30 whose 07:00 did arrive is not a hole the reader can act on. Reported, the
    page prints "Prognosen kl. 06:30 er forsinket" directly above the card that replaced it."""
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0700": 0.2})
    _reset(fake)
    with togvarsel._lock:  # 06:30 failed; only 07:00 got issued
        togvarsel._state.update(togvarsel._blank_state(MONDAY_0710.date()))
        togvarsel._state["issues"]["0700"] = togvarsel.Issue(
            issue="0700", scope="x", p=0.2, level="yellow", reasons=[], computed_at=MONDAY_0710
        )
    # Inside the morning rush: past 06:30 + grace, before the 09:00 that retires the card.
    m = togvarsel.build_melding(MONDAY_0710.replace(hour=8, minute=0))
    assert [i.issue for i in m.issues] == ["0700"]
    assert m.overdue_issues == []


def test_build_melding_distinguishes_overdue_from_not_yet_due():
    """#8 in the launch review: 'due but missing' must be visible in the payload, or the page
    shows 'first forecast coming soon' at 20:00 on a broken day."""
    _reset(_FakeBQ())  # nothing ever computed
    early = togvarsel.build_melding(MONDAY_0710.replace(hour=6, minute=0))
    assert early.overdue_issues == [] and early.next_issue == "0630"
    in_grace = togvarsel.build_melding(MONDAY_0710.replace(hour=6, minute=35))
    assert in_grace.overdue_issues == []  # 5 min past the cutoff: still just in flight
    evening = togvarsel.build_melding(MONDAY_0710.replace(hour=20, minute=0))
    assert evening.overdue_issues == ["0630", "0700", "1200", "1400"]
    assert evening.next_issue is None and evening.forecast_day is True


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


def test_the_whole_api_404s_while_the_master_gate_is_off():
    """SHOW_RUSHVARSEL off (its default) 404s EVERY route on this router, not just the payload.

    Router-level, so /config goes with /today: while the product is dark the surface must be
    indistinguishable from one that never shipped, and a /togmelding-api/config that answers 200
    tells a reader the page exists and is merely empty. The parse is the delivery-flag allow-list
    (deployment.show_rushvarsel), so "false", "" and anything unrecognised all read as off — the
    closed direction.
    """
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.05})
    _reset(fake)
    app = FastAPI()
    app.include_router(togvarsel.router)
    client = TestClient(app)
    for value in ("false", "", "0", "off", "disabled"):
        os.environ["SHOW_RUSHVARSEL"] = value
        try:
            assert client.get("/togmelding-api/today").status_code == 404, value
            assert client.get("/togmelding-api/config").status_code == 404, value
        finally:
            os.environ["SHOW_RUSHVARSEL"] = "true"
    # Unset is off too, and needs no helm value to be.
    del os.environ["SHOW_RUSHVARSEL"]
    try:
        assert client.get("/togmelding-api/today").status_code == 404
    finally:
        os.environ["SHOW_RUSHVARSEL"] = "true"
    assert client.get("/togmelding-api/today").status_code == 200  # and back on


class _pinned_clock:
    """Pin the endpoint's clock to the fakes' fixed Monday. What /today serves is a function of
    the hour — a card is retired once its rush ends — so reading the real clock would make these
    checks pass or fail by the time of day the suite happens to run."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __enter__(self) -> None:
        self._original = togvarsel._now
        togvarsel._now = lambda: self._now

    def __exit__(self, *exc: object) -> None:
        togvarsel._now = self._original


def test_endpoint_mints_one_feedback_context_per_issued_forecast():
    """When the /varsel feedback stack is configured, each issued forecast on the payload carries
    its own short-lived capability — and three things about the minting are load-bearing. The id
    is STABLE across requests (feedback_contexts dedups byte-equivalent snapshots), which is what
    lets the page skip re-renders on an unchanged payload and keeps the cache at one entry per
    forecast instead of one per poll. The persisted snapshot says exactly what the card rendered,
    discriminated the documented way: display_source='togvarsel' and a KIX:Togvarsel: situation
    number, never a new column or event. And the annotation lands on a COPY — the cached Issue,
    and therefore issued_today()'s notify seam, must stay a pure forecast."""
    import feedback
    import feedback_contexts

    fake = _FakeBQ(feature_row=_feature_row(),
                   probs={"am0630": 0.15, "am0700": 0.34, "pm1200": 0.2, "pm1400": 0.05})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_1430)  # the whole day computed at once, as a restart does
    feedback_contexts._reset_for_tests()
    original_enabled = feedback.enabled
    feedback.enabled = lambda: True
    try:
        app = FastAPI()
        app.include_router(togvarsel.router)
        client = TestClient(app)
        # 07:10: a card is retired when its rush ENDS, never before the rush starts, so the
        # back-filled afternoon forecast rides along beside the live morning one — which is what
        # puts two forecasts on one payload to compare ids across.
        with _pinned_clock(MONDAY_0710):
            first = client.get("/togmelding-api/today").json()
            second = client.get("/togmelding-api/today").json()
        issues = {i["issue"]: i for i in first["issues"]}
        # 06:30 and 12:00 were issued too; the page carries only what superseded them.
        assert set(issues) == {"0700", "1400"}
        assert all(i["feedback_enabled"] and i["feedback_context_id"] for i in issues.values())
        assert issues["1400"]["feedback_context_id"] != issues["0700"]["feedback_context_id"]
        assert second["issues"] == first["issues"]  # stable ids -> byte-stable cards across polls

        snapshot = feedback_contexts.resolve(issues["0700"]["feedback_context_id"])
        assert snapshot["display_source"] == "togvarsel"
        assert snapshot["situation_number"] == f"KIX:Togvarsel:{first['date']}:0700"
        # the literal page rendering, JS-rounded (0.34 -> 34; >= RED_MIN 0.30 -> red)
        assert snapshot["displayed_text"] == "stor fare for forstyrrelser (ca. 34 % sjanse for mye forstyrrelser)"
        # the card's heading, rush + level (the issue time is a small line under it since 2026-09-02)
        assert snapshot["detail_text"] == "Morgenrushet 07:00–09:00 — rødt nivå"
        assert snapshot["operator"] == "VYG"
        assert snapshot["journey_context"] is None and snapshot["message_input"] is None
        assert snapshot["prediction"]["level"] == "red" and snapshot["prediction"]["reasons"]
        assert snapshot["policy"] == {"green_max": togvarsel.GREEN_MAX, "red_min": togvarsel.RED_MIN}
        assert snapshot["delivery"] == {"surface": "togmelding"}

        for issue in togvarsel.issued_today(now=MONDAY_1430):
            assert issue["feedback_context_id"] is None
            assert issue["feedback_enabled"] is False
    finally:
        feedback.enabled = original_enabled
        feedback_contexts._reset_for_tests()


def test_endpoint_offers_no_feedback_when_the_stack_is_off():
    """feedback.enabled() False — the default everywhere the flag and secrets are not provisioned
    — means null/false fields and NOTHING minted: the page renders no widget, and the endpoint
    spends no context-cache entry per poll on a deployment where a submission could only 'disabled'."""
    import feedback
    import feedback_contexts

    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.15})
    _reset(fake)
    togvarsel.refresh(now=MONDAY_0710)
    feedback_contexts._reset_for_tests()
    original_enabled = feedback.enabled
    feedback.enabled = lambda: False
    try:
        app = FastAPI()
        app.include_router(togvarsel.router)
        with _pinned_clock(MONDAY_0710):
            (issue,) = TestClient(app).get("/togmelding-api/today").json()["issues"]
        assert issue["feedback_context_id"] is None and issue["feedback_enabled"] is False
        assert feedback_contexts._by_id == {}
    finally:
        feedback.enabled = original_enabled


def test_feedback_snapshot_matches_the_context_table_columns():
    """feedback_contexts.persist() writes the snapshot into varsel_feedback_contexts BY KEY; a key
    the table does not have makes the streamed insert fail — logged and swallowed while the rating
    still reaches PostHog, exactly the invisible 2026-08-27 failure mode that module's header
    documents — so pin the key set against the DDL the app runs at startup."""
    import re

    import feedback_contexts

    columns = set(re.findall(r"^\s{2}(\w+)\s", feedback_contexts._DDL, re.MULTILINE))
    assert "display_source" in columns  # the parse found the column list, not noise
    issue = togvarsel.Issue(
        issue="0630", scope="morgenrushet 06:30–09:00", p=0.05, level="green",
        reasons=["Ingen tegn til forstyrrelser."], computed_at=MONDAY_0710,
    )
    snapshot = togvarsel._feedback_snapshot(issue, date(2026, 8, 31))
    assert set(snapshot) == columns - {"context_id", "context_date", "persisted_at"}


def test_feedback_snapshot_mirrors_the_page():
    """_LEVEL_TEXT and the percentage phrase are COPIES of the page's own rendering — the snapshot
    must say what the traveller actually read, so an edit to either side must show up here (the
    cheap grep guard; nothing executes the page's JS). Also pins the wiring the widget depends on:
    the shared submission endpoint and the two payload fields the card gates on.

    Reads the page's SOURCES (demo/togmelding/src), not its build output: since 2026-09-01 the
    page is a Vite bundle on Entur Linje rather than one committed HTML file, so togmelding_static/
    is generated — absent in a fresh checkout, and minified when present."""
    src = Path(togvarsel.__file__).parents[2] / "demo" / "togmelding" / "src"
    page = "\n".join(
        path.read_text() for path in sorted(src.rglob("*")) if path.suffix in (".ts", ".tsx")
    )
    assert page, f"no page sources found under {src}"
    for text in togvarsel._LEVEL_TEXT.values():
        assert text in page, f"page no longer renders the level text {text!r}"
    assert " % sjanse for mye forstyrrelser)" in page
    assert "/varsel-api/feedback" in page
    assert "feedback_context_id" in page and "feedback_enabled" in page


def test_the_level_vocabulary_is_one_copy_per_app_and_the_copies_agree():
    """/varsel and /togmelding each carry `src/levels.ts` — a guarded duplicate, like personvern.ts,
    because each front-end is its own npm app and the image's node stage copies only its own
    directory. Until 2026-09-02 the two apps had drifted into GRØNT/GULT/RØDT vs GRØNN/GUL/RØD for
    one concept, and the middle sentence («noe usikkert») measured something else than the two
    beside it (design review 2026-09-01, findings 11 and 12). Byte-identical, and each sentence is
    the server's, which is what the persisted feedback snapshot quotes."""
    demo = Path(togvarsel.__file__).parents[2] / "demo"
    varsel_copy = (demo / "avviksvarsel" / "src" / "levels.ts").read_text()
    togmelding_copy = (demo / "togmelding" / "src" / "levels.ts").read_text()
    assert varsel_copy == togmelding_copy, "demo/*/src/levels.ts have drifted apart; edit both"
    for text in togvarsel._LEVEL_TEXT.values():
        assert f"'{text}'" in togmelding_copy, f"levels.ts no longer carries {text!r}"
    for adjective in togvarsel.LEVEL_ADJ.values():
        assert f"'{adjective}'" in togmelding_copy, f"levels.ts no longer carries {adjective!r}"
    assert "'noe usikkert'" not in togmelding_copy
    for label in ("GRØNT", "GULT", "RØDT"):
        assert f"'{label}'" in togmelding_copy
    # the /varsel panel and the /togmelding card both read the copy rather than their own literals
    for page in (demo / "avviksvarsel" / "src" / "components" / "RushOutlook.tsx",
                 demo / "togmelding" / "src" / "components" / "IssueCard.tsx"):
        assert "levels'" in page.read_text() or "./format'" in page.read_text(), page


def test_issued_today_snapshots_plain_dicts_for_the_notify_seam():
    """issued_today() is togvarsel_notify's injected snapshot (U4): plain dicts carrying the
    Issue payload fields the notify chain reads, [] before the first refresh of the day, and []
    on a stale (yesterday's) state — old issues must never leak into a new day's rounds."""
    fake = _FakeBQ(feature_row=_feature_row(), probs={"am0630": 0.15})
    _reset(fake)
    assert togvarsel.issued_today(now=MONDAY_0710) == []  # before any refresh
    togvarsel.refresh(now=MONDAY_0710)
    issued = togvarsel.issued_today(now=MONDAY_0710)
    assert [i["issue"] for i in issued] == ["0630"]  # am0700 failed -> only 0630 exists
    (issue,) = issued
    assert isinstance(issue, dict)
    assert issue["level"] == "yellow" and issue["late"] is True
    assert {"scope", "reasons", "computed_at", "p"} <= set(issue)
    # the next day sees a stale state, not yesterday's issues
    assert togvarsel.issued_today(now=datetime(2026, 9, 1, 7, 10, tzinfo=_OSLO)) == []


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
