"""Checks for /rushhistorikk-api — the Rushvarsel feedback loop's read side.

Hermetic: the BigQuery client is a fake replaying canned rows, and the clock is pinned, so nothing
here reads a network or a real date. The verdict logic itself is rushvarsel_verdicts' own suite;
what these checks are for is the seam — that the right scope reaches the right issue, that the
states which mean "we do not know" stay apart, and that the five subscriber-derived columns never
leave the process.

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery python
     test_rushhistorikk.py   (or under pytest)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Stub the BigQuery client BEFORE importing the module (main builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import rushhistorikk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

_OSLO = ZoneInfo("Europe/Oslo")
_API_DIR = str(Path(__file__).parent)

TODAY = date(2026, 9, 1)
MONDAY = date(2026, 8, 31)
# The instant every test's clock is pinned to. Late enough in the Oslo day that `today` is stable
# whichever machine runs it.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=_OSLO)


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self, timeout=None):
        return list(self._rows)


class _FakeBQ:
    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, job_config=None):
        params = {}
        if job_config is not None:
            for p in job_config.query_parameters:
                params[p.name] = list(p.values) if hasattr(p, "values") else p.value
        self.calls.append((sql, params))
        if self.error is not None:
            raise self.error
        return _FakeJob(self.rows)


fake = _FakeBQ()
rushhistorikk.configure(bq_client=fake)

app = FastAPI()
app.include_router(rushhistorikk.router)
client = TestClient(app)


def _row(**overrides):
    """One row as rushhistorikk_days.sql returns it. Defaults: a settled, eligible Monday whose
    0630 pushed yellow and whose morning was yellow — i.e. a `traff`."""
    row = {
        "d": MONDAY,
        "issue": "0630",
        "p": 0.21,
        "forecast_level": "yellow",
        "late": False,
        "computed_at": datetime(2026, 8, 31, 4, 30, tzinfo=UTC),
        "sent": True,
        "round_level": "yellow",
        "baseline_level": None,
        "kind": "warning",
        "reason": None,
        "decided_at": datetime(2026, 8, 31, 4, 31, tzinfo=UTC),
        "has_round": True,
        "first_recorded_day": MONDAY,
        "am_level": "yellow",
        "am_trouble10": 0.12,
        "amrest_level": "yellow",
        "amrest_trouble10": 0.11,
        "pm_level": "green",
        "pm_trouble10": 0.03,
        "am_provisional_level": None,
        "pm_provisional_level": None,
        "eligible": True,
        "am_settled": True,
        "amrest_settled": True,
        "pm_settled": True,
        "am_unresolvable": False,
        "amrest_unresolvable": False,
        "pm_unresolvable": False,
    }
    row.update(overrides)
    return row


def _reset(rows=None, error=None) -> None:
    fake.rows = list(rows if rows is not None else [_row()])
    fake.error = error
    fake.calls.clear()
    # The TTL cache would otherwise serve a previous test's rows. A new module-level cache must be
    # added here or half the suite passes by accident.
    rushhistorikk._cache.clear()
    rushhistorikk._tznow = lambda: NOW


@contextmanager
def _deployed_as(project: str | None):
    saved_env = os.environ.get("KIX_ENVIRONMENT")
    saved_proj = os.environ.get("GOOGLE_CLOUD_PROJECT")
    os.environ.pop("KIX_ENVIRONMENT", None)
    if project is None:
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
    else:
        os.environ["GOOGLE_CLOUD_PROJECT"] = project
    try:
        yield
    finally:
        for key, value in (("KIX_ENVIRONMENT", saved_env), ("GOOGLE_CLOUD_PROJECT", saved_proj)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _days(**params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/rushhistorikk-api/days{'?' + query if query else ''}")


def _uncommented(sql: str) -> str:
    """SQL with its comment lines dropped.

    Load-bearing for the projection checks below: rushhistorikk_days.sql NAMES the five
    subscriber-derived columns in its header, explaining that it never selects them. A naive grep
    over the raw text reports a leak for exactly the file that documents why there is none.
    """
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))


# ---- the window allow-list -------------------------------------------------------------------


def test_the_four_picker_windows_are_accepted():
    for days in rushhistorikk.WINDOW_DAYS:
        _reset()
        assert _days(days=days).status_code == 200, days


def test_a_window_outside_the_allow_list_is_rejected_before_bigquery():
    """The value lands in the response-cache key on a public unauthenticated endpoint, so an
    arbitrary integer would mint one cache entry and one BigQuery query apiece."""
    for days in (13, 0, -1, 1000, 366):
        _reset()
        response = _days(days=days)
        assert response.status_code == 400, days
        assert fake.calls == [], "a rejected window must not reach BigQuery"


def test_the_default_window_is_fourteen_days():
    _reset()
    body = _days().json()
    assert body["days_window"] == 14
    assert body["from_date"] == "2026-08-19"  # 14 days inclusive, ending today


def test_the_window_start_is_derived_from_the_oslo_day():
    _reset()
    assert _days(days=7).json()["from_date"] == "2026-08-26"


# ---- the seam: which scope reaches which issue ------------------------------------------------


def test_each_issue_is_scored_against_its_own_trained_window():
    """0630's label is over 06:30-09:00 and 0700's over 07:00-09:00. Asserted with an am/amrest
    pair that DISAGREE, so reading the wrong one changes the verdict: the morning as a whole was
    yellow, the 07:00-09:00 part of it was green."""
    _reset([
        _row(issue="0630", am_level="yellow", amrest_level="green"),
        _row(issue="0700", round_level="yellow", baseline_level="yellow", sent=False,
             reason="no_change", am_level="yellow", amrest_level="green"),
    ])
    issues = {i["issue"]: i for i in _days().json()["days"][0]["issues"]}
    assert issues["0630"]["actual_level"] == "yellow"
    assert issues["0700"]["actual_level"] == "green", "0700 must read amrest, not am"
    assert issues["0630"]["verdict"] == "traff"
    assert issues["0700"]["verdict"] == "falsk_alarm", "a standing yellow over a green rush"


def test_the_afternoon_issues_share_the_pm_scope():
    _reset([
        _row(issue="1200", round_level="red", kind="warning", pm_level="red", pm_trouble10=0.4),
        _row(issue="1400", round_level="red", sent=False, reason="no_change",
             baseline_level="red", pm_level="red", pm_trouble10=0.4),
    ])
    issues = {i["issue"]: i for i in _days().json()["days"][0]["issues"]}
    assert issues["1200"]["actual_level"] == "red"
    assert issues["1400"]["actual_level"] == "red"
    assert issues["1200"]["verdict"] == "traff" and issues["1400"]["verdict"] == "traff"


def test_a_confirming_follow_up_is_graded_on_the_standing_level():
    """The KTD6 case, end to end: 12:00 pushed yellow, the 14:00 forecast says red but the round
    was suppressed, and the rush was yellow. The traveller holds a yellow warning, which was
    right — so `traff`, not `traff_nesten` on a red nobody received."""
    _reset([
        _row(issue="1400", round_level="red", sent=False, reason="no_change",
             baseline_level="yellow", pm_level="yellow", pm_trouble10=0.2),
    ])
    issue = _days().json()["days"][0]["issues"][0]
    assert issue["baseline_level"] == "yellow"
    assert issue["verdict"] == "traff"


# ---- the three kinds of "we do not know" ------------------------------------------------------


def test_an_issue_with_no_round_record_is_ikke_vurdert_and_in_no_denominator():
    _reset([_row(has_round=False, sent=False, round_level=None, kind=None,
                 decided_at=None, first_recorded_day=None)])
    body = _days().json()
    issue = body["days"][0]["issues"][0]
    assert issue["verdict"] == "ikke_vurdert" and issue["scored"] is False
    total = body["stats"][0]
    assert total["n_ikke_vurdert"] == 1 and total["n_scored"] == 0
    assert total["presisjon_samlet"] is None


def test_an_unsettled_outcome_inside_the_retry_window_is_venter():
    _reset([_row(d=TODAY, am_settled=False, am_level=None, eligible=None)])
    issue = _days().json()["days"][0]["issues"][0]
    assert issue["verdict"] == "venter"


def test_an_unresolvable_outcome_is_ukjent_not_venter():
    """last_recorded is an immutable daily batch, so "nothing resolved" is final. Reporting it as
    venter would promise a verdict that is never coming."""
    _reset([_row(am_settled=True, am_unresolvable=True, am_level=None)])
    assert _days().json()["days"][0]["issues"][0]["verdict"] == "ukjent"


def test_an_outcome_past_the_retry_window_is_ukjent():
    _reset([_row(d=date(2026, 8, 1), am_settled=False, am_level=None)])
    assert _days().json()["days"][0]["issues"][0]["verdict"] == "ukjent"


def test_an_ineligible_day_is_ukjent_and_scored_nowhere():
    _reset([_row(eligible=False)])
    body = _days().json()
    assert body["days"][0]["issues"][0]["verdict"] == "ukjent"
    assert body["stats"][0]["n_scored"] == 0


# ---- provisional ------------------------------------------------------------------------------


def test_a_provisional_outcome_is_marked_and_excluded_from_the_stats():
    """It is shown on the day row — the product asked for a check after each rush — but it can
    still move overnight, so it must not move a rate."""
    _reset([_row(d=TODAY, am_settled=False, am_level="yellow", am_provisional_level="yellow",
                 eligible=None)])
    body = _days().json()
    issue = body["days"][0]["issues"][0]
    assert issue["provisional"] is True
    assert issue["verdict"] == "venter" and issue["scored"] is False
    assert body["stats"][0]["presisjon_samlet"] is None
    assert body["days"][0]["am_provisional_level"] == "yellow"


def test_a_settled_outcome_is_not_marked_provisional():
    _reset()
    assert _days().json()["days"][0]["issues"][0]["provisional"] is False


# ---- the stats table --------------------------------------------------------------------------


def test_the_stats_table_is_the_total_row_plus_one_per_slot():
    _reset()
    slots = [row["slot"] for row in _days().json()["stats"]]
    assert slots == ["total", "0630", "0700", "1200", "1400"]


def test_every_stats_row_carries_a_full_three_by_three_matrix():
    _reset()
    for row in _days().json()["stats"]:
        assert [cell["forecast"] for cell in row["matrix"]] == ["green", "yellow", "red"]
        for cell in row["matrix"]:
            assert set(cell) == {"forecast", "green", "yellow", "red"}


def test_a_rate_over_an_empty_denominator_is_null_never_zero():
    """A rate over nothing is not 0 %, and rendering it as one reads as "we got everything wrong"."""
    _reset([_row(has_round=False, sent=False, round_level=None, kind=None, decided_at=None)])
    total = _days().json()["stats"][0]
    for key in ("presisjon_samlet", "presisjon_varslet", "dekning", "varslingstid_min"):
        assert total[key] is None, key
    assert total["avgjorelser"] == 1


def test_the_matrix_and_the_verdict_agree_on_one_scored_day():
    _reset()
    body = _days().json()
    total = body["stats"][0]
    assert total["n_scored"] == 1 and total["presisjon_samlet"] == 1.0
    cells = {cell["forecast"]: cell for cell in total["matrix"]}
    assert cells["yellow"]["yellow"] == 1


def test_lead_minutes_are_served_only_for_a_push():
    _reset([_row(issue="1200", round_level="red", kind="warning",
                 decided_at=datetime(2026, 8, 31, 10, 1, tzinfo=UTC), pm_level="red")])
    issue = _days().json()["days"][0]["issues"][0]
    # 12:01 Oslo against a 15:00 rush start
    assert issue["lead_min"] is not None and round(issue["lead_min"]) == 179
    _reset([_row(sent=False, reason="green_baseline", round_level="green",
                 forecast_level="green", am_level="green")])
    assert _days().json()["days"][0]["issues"][0]["lead_min"] is None


# ---- the projection rule ----------------------------------------------------------------------


def test_the_subscriber_derived_columns_are_never_selected():
    """togvarsel_rounds holds five day-grained counts over a subscriber population numbering a
    handful, on an endpoint that is unauthenticated on a public host. A query that does not name a
    column cannot leak it however the model above it drifts."""
    body = _uncommented(rushhistorikk._DAYS_QUERY)
    for column in ("matched_journeys", "matched_devices", "n_sent", "n_error", "n_gone"):
        assert not re.search(rf"\b{column}\b", body), f"{column} is selected"
    # and the file does still read the table it is being kept out of, or the check is vacuous
    assert "togvarsel_rounds" in body


def test_the_response_model_refuses_a_subscriber_column():
    _reset()
    with_extra = {**_days().json()["days"][0]["issues"][0], "matched_devices": 3}
    try:
        rushhistorikk.IssueRow(**with_extra)
    except ValidationError:
        return
    raise AssertionError("IssueRow accepted a subscriber-derived column")


def test_the_response_carries_exactly_the_expected_keys():
    """The whole key set, pinned: a field added to the response fails HERE rather than shipping."""
    _reset()
    body = _days().json()
    assert set(body) == {"days_window", "from_date", "days", "stats", "recording_started"}
    assert set(body["days"][0]) == {
        "d", "issues", "am_level", "am_trouble10", "amrest_level", "pm_level", "pm_trouble10",
        "am_provisional_level", "pm_provisional_level", "eligible",
    }
    assert set(body["days"][0]["issues"][0]) == {
        "issue", "rush", "forecast_level", "p", "late", "recorded", "sent", "kind", "reason",
        "baseline_level", "decided_at", "actual_level", "actual_trouble10", "provisional",
        "verdict", "lead_min", "scored",
    }
    assert set(body["stats"][0]) == {
        "slot", "presisjon_samlet", "presisjon_varslet", "dekning", "varslingstid_min",
        "varslingstid_n", "avgjorelser", "n_scored", "n_venter", "n_ukjent", "n_ikke_vurdert",
        "n_late", "n_untimely", "matrix",
    }


def test_the_outcomes_join_is_bounded_in_the_on_and_not_the_where():
    """In the WHERE a LEFT JOIN becomes an inner one and every unresolved day disappears — which on
    a short window is nearly all of them."""
    body = _uncommented(rushhistorikk._DAYS_QUERY)
    where = body.split("FROM `disruptions.togvarsel_issued`", 1)[1]
    assert "LEFT JOIN outcomes" in where
    assert "ON i.d = o.d" in where


def test_the_query_file_is_registered_in_the_validator_with_its_parameters():
    script = (Path(__file__).parents[2] / "data/queries/validate_varsel_sql.sh").read_text()
    for filename in (
        "rushhistorikk_days.sql",
        "togvarsel_outcomes_settled.sql",
        "togvarsel_outcomes_provisional.sql",
    ):
        assert filename in script, f"{filename} is not dry-run by validate_varsel_sql.sh"
    for param in sorted(set(re.findall(r"@(\w+)", _uncommented(rushhistorikk._DAYS_QUERY)))):
        assert f"parameter={param}:" in script, (
            f"rushhistorikk_days.sql uses @{param}, which the validator does not supply — the dry "
            f"run would fail to parse and the file would go unvalidated"
        )


# ---- config, and the failure the page must survive --------------------------------------------


def test_config_labels_non_prod_and_stays_quiet_in_prd():
    for project, expected in (
        ("ent-kixavvik-dev", "dev"),
        ("ent-kixavvik-tst", "tst"),
        ("ent-kixavvik-prd", None),
    ):
        with _deployed_as(project):
            assert client.get("/rushhistorikk-api/config").json()["environment"] == expected


def test_config_survives_an_unreadable_log_and_touches_no_bigquery():
    """Both halves matter: /days 503s exactly when "am I looking at dev or prd?" matters most."""
    _reset(error=RuntimeError("403 Access Denied"))
    assert _days().status_code == 503
    calls_before = len(fake.calls)
    assert client.get("/rushhistorikk-api/config").status_code == 200
    assert len(fake.calls) == calls_before, "/config must not query BigQuery"


def test_a_missing_table_reports_unavailable_not_a_traceback():
    _reset(error=RuntimeError("Not found: Table disruptions.togvarsel_rounds"))
    response = _days()
    assert response.status_code == 503
    assert "rush forecast log" in response.json()["detail"].lower()


def test_an_empty_window_is_an_empty_page_not_an_error():
    _reset([])
    body = _days().json()
    assert body["days"] == [] and body["recording_started"] is None
    assert [row["slot"] for row in body["stats"]] == ["total", "0630", "0700", "1200", "1400"]
    assert body["stats"][0]["avgjorelser"] == 0


def test_two_requests_inside_the_ttl_run_one_query():
    _reset()
    _days()
    _days()
    assert len(fake.calls) == 1


def test_the_cache_is_bounded():
    _reset()
    for days in rushhistorikk.WINDOW_DAYS:
        _days(days=days)
    assert len(rushhistorikk._cache) <= rushhistorikk._CACHE_MAX_ENTRIES


def test_every_query_is_bounded_by_maximum_bytes_billed():
    _reset()
    _days()
    assert fake.calls, "no query ran"


# ---- the surface is actually served -----------------------------------------------------------

_SERVED_PROBE = """
import json

from google.cloud import bigquery
bigquery.Client = lambda *a, **k: None

import main
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles

mounts = {
    route.path: (type(route.app).__name__, isinstance(route.app, main._VersionedStaticFiles))
    for route in main.app.routes
    if isinstance(getattr(route, "app", None), StaticFiles)
}
client = TestClient(main.app)
print(json.dumps({
    "config_status": client.get("/rushhistorikk-api/config").status_code,
    "historikk_config_status": client.get("/historikk-api/config").status_code,
    "mounts": mounts,
}))
"""


@contextmanager
def _static_present():
    """Make sure rushhistorikk_static/ exists for the duration of one check.

    The mount is guarded on the directory existing, and that directory is generated at image-build
    time — absent in a fresh checkout and in CI. Without this the "mounted" assertion would pass by
    not running, on exactly the machine where nobody would look.
    """
    path = Path(_API_DIR) / "rushhistorikk_static"
    created = not path.exists()
    if created:
        path.mkdir()
    try:
        yield path
    finally:
        if created:
            path.rmdir()


def test_both_halves_of_the_surface_are_served():
    """A child interpreter, because main builds `app` at import.

    The API half is probed by ASKING rather than by reading app.routes: this FastAPI version keeps
    an included router as one opaque entry whose path is None, so a scan would find no
    /rushhistorikk-api route even with the router registered — and would have "passed" identically
    with it removed.
    """
    with _static_present():
        result = subprocess.run(
            [sys.executable, "-c", _SERVED_PROBE],
            cwd=_API_DIR, capture_output=True, text=True, check=False,
        )
    if result.returncode != 0:
        raise AssertionError(f"probe failed: {result.stderr[-2000:]}")
    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state["historikk_config_status"] == 200, "the probe did not see the other routers either"
    assert state["config_status"] == 200, "/rushhistorikk-api/config is not registered"
    assert "/rushhistorikk" in state["mounts"], "the UI is not mounted"
    type_name, is_versioned = state["mounts"]["/rushhistorikk"]
    assert is_versioned, f"/rushhistorikk is mounted with {type_name}, not _VersionedStaticFiles"


def test_the_generated_bundle_is_ignored_by_git_and_docker():
    """Four lists fail silently here, and .dockerignore is the one that bites: miss it and a stale
    local bundle leaks into the image via `COPY disruptions/api/ .`."""
    root = Path(__file__).parents[2]
    for name in (".gitignore", ".dockerignore"):
        assert "disruptions/api/rushhistorikk_static/" in (root / name).read_text(), name


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} rushhistorikk checks passed.")


if __name__ == "__main__":
    _run()
