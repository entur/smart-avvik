"""Checks for /historikk-api — the alert-decision log with realized outcomes.

Hermetic: BigQuery is replaced with a fake that records the SQL and parameters it was handed and
replays canned rows, so nothing here touches BigQuery or the network.

The checks that matter most are the ones about *absence*: an unresolved decision must never be
reported as a correct rejection, and "too recent to judge" must stay distinguishable from "judged,
and we could not find the train". Both would otherwise turn missing data into flattering numbers.

Run: uv run --with fastapi --with httpx --with pydantic python test_historikk.py  (or under pytest)
"""

import os
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import historikk
from fastapi import FastAPI
from fastapi.testclient import TestClient

DEP = datetime(2026, 7, 30, 6, 15, tzinfo=UTC)


def _row(**overrides) -> dict:
    """A labelled decision row as the SQL returns it; override per test."""
    base = {
        "situation_number": "S1",
        "summary": "Signalfeil ved Asker",
        "description": "Det er meldt om signalfeil.",
        "operator": "VYG",
        "line": "VYG:Line:R11",
        "mode": "Tog",
        "call_date": date(2026, 7, 30),
        "departure_time": DEP,
        "n_decisions": 4,
        "first_decided_at": DEP - timedelta(minutes=90),
        "last_decided_at": DEP - timedelta(minutes=5),
        "ever_alerted": True,
        "first_alert_at": DEP - timedelta(minutes=48),
        "alert_lead_min": 48,
        "max_impact_probability": 0.93,
        "final_impact_probability": 0.93,
        "final_cancel_probability": 0.81,
        "final_delay_p50_min": 15,
        "final_delay_p90_min": 40,
        "alert_threshold": 0.65,
        "pushed_any": True,
        "n_devices": 2,
        "message": "Toget ditt 08:15 er rammet av signalfeil ved Asker.",
        "message_source": "llm",
        "actual_cancelled": True,
        "actual_dep_delay_min": None,
        "actual_trouble": True,
        "verdict": "hit",
        "outcome_state": "avklart",
    }
    base.update(overrides)
    return base


class _FakeBQ:
    """Records what it was asked and replays canned rows."""

    def __init__(self):
        self.rows: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.error: Exception | None = None

    def query(self, sql, job_config=None):
        params = {p.name: p.value for p in (job_config.query_parameters if job_config else [])}
        self.calls.append((sql, params))
        if self.error is not None:
            raise self.error
        rows = self.rows

        class _Job:
            def result(self):
                return list(rows)

        return _Job()


fake = _FakeBQ()
historikk.configure(bq_client=fake)

app = FastAPI()
app.include_router(historikk.router)
client = TestClient(app)


def _reset(rows=None, error=None):
    fake.rows = list(rows or [])
    fake.calls.clear()
    fake.error = error
    historikk._cache.clear()  # the TTL cache would otherwise serve a previous test's rows


def test_decisions_returns_rows():
    _reset([_row()])
    r = client.get("/historikk-api/decisions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    d = body["decisions"][0]
    assert d["verdict"] == "hit"
    assert d["message"].startswith("Toget ditt")
    assert d["alert_lead_min"] == 48


def test_unresolved_decision_is_not_a_correct_rejection():
    """The trap this whole surface is built to avoid.

    A decision whose train could not be found in ET has an UNKNOWN outcome. Reporting it as
    `correct_reject` — the tempting default, since nothing bad was recorded — would invent correct
    behaviour out of missing data and inflate every rate computed downstream.
    """
    _reset([_row(actual_cancelled=None, actual_dep_delay_min=None, actual_trouble=None,
                 verdict=None, outcome_state="ukjent")])
    d = client.get("/historikk-api/decisions").json()["decisions"][0]
    assert d["verdict"] is None
    assert d["actual_trouble"] is None
    assert d["outcome_state"] == "ukjent"


def test_pending_and_unknown_stay_distinct():
    """'Not judged yet' and 'judged, train not found' are different facts.

    Collapsing them would make a freshly deployed environment — where everything is pending —
    indistinguishable from a broken join where nothing will ever resolve.
    """
    _reset([
        _row(verdict=None, actual_trouble=None, outcome_state="venter"),
        _row(situation_number="S2", verdict=None, actual_trouble=None, outcome_state="ukjent"),
    ])
    states = {d["situation_number"]: d["outcome_state"]
              for d in client.get("/historikk-api/decisions").json()["decisions"]}
    assert states == {"S1": "venter", "S2": "ukjent"}


def test_non_alerting_decisions_are_included_by_default():
    """A miss only exists in the record because we log what we did NOT alert on."""
    _reset([_row(ever_alerted=False, pushed_any=False, first_alert_at=None, alert_lead_min=None,
                 max_impact_probability=0.21, verdict="miss", actual_trouble=True)])
    d = client.get("/historikk-api/decisions").json()["decisions"][0]
    assert d["ever_alerted"] is False
    assert d["verdict"] == "miss"
    # The default must not quietly filter to alerts only.
    assert fake.calls[0][1]["alerted_only"] is False


def test_alerted_only_filter_is_passed_through():
    _reset([_row()])
    client.get("/historikk-api/decisions", params={"alerted_only": "true"})
    assert fake.calls[0][1]["alerted_only"] is True


def test_verdict_filter_is_validated():
    _reset([_row()])
    assert client.get("/historikk-api/decisions", params={"verdict": "hit"}).status_code == 200
    bad = client.get("/historikk-api/decisions", params={"verdict": "definitely-not"})
    assert bad.status_code == 400
    assert "verdict must be one of" in bad.json()["detail"]


def test_window_defaults_and_validation():
    _reset([_row()])
    body = client.get("/historikk-api/decisions").json()
    span = date.fromisoformat(body["to_date"]) - date.fromisoformat(body["from_date"])
    assert span == timedelta(days=14)

    _reset([_row()])
    reversed_window = client.get(
        "/historikk-api/decisions", params={"from_date": "2026-07-30", "to_date": "2026-07-01"}
    )
    assert reversed_window.status_code == 400

    _reset([_row()])
    too_wide = client.get(
        "/historikk-api/decisions", params={"from_date": "2020-01-01", "to_date": "2026-07-30"}
    )
    assert too_wide.status_code == 400


def test_operator_filter_and_limit_reach_the_query():
    _reset([_row()])
    client.get("/historikk-api/decisions", params={"operator": "RUT", "limit": 25})
    params = fake.calls[0][1]
    assert params["operator"] == "RUT"
    assert params["limit"] == 25


def test_unknown_operator_is_rejected():
    """An unserved codespace must not reach BigQuery or mint a cache entry.

    The endpoint is public and unauthenticated, and `operator` is part of the cache key, so an
    arbitrary string would buy an attacker one cached query per distinct value.
    """
    _reset([_row()])
    bad = client.get("/historikk-api/decisions", params={"operator": "NOT-AN-OPERATOR"})
    assert bad.status_code == 400
    assert "operator must be one of" in bad.json()["detail"]
    assert fake.calls == [], "a rejected operator must not reach BigQuery"


def test_response_carries_no_traveller_journey_identifiers():
    """The public response describes a train, never whose trip it was.

    from_stop/to_stop/journey_digest next to departure_time and n_devices would publish
    "somebody commutes A->B at 07:15" -- exactly what llm_message.key_digest keeps out of the logs.
    This asserts the whole key set, so a future field addition fails here rather than shipping.
    """
    _reset([_row()])
    d = client.get("/historikk-api/decisions").json()["decisions"][0]
    assert not {"journey_digest", "from_stop", "to_stop", "stop_place"} & set(d)
    assert set(d) == {
        "situation_number", "summary", "description", "operator", "line", "mode", "call_date",
        "departure_time", "n_decisions", "first_decided_at", "last_decided_at",
        "ever_alerted", "first_alert_at", "alert_lead_min",
        "max_impact_probability", "final_impact_probability", "final_cancel_probability",
        "final_delay_p50_min", "final_delay_p90_min", "alert_threshold", "pushed_any",
        "n_devices", "message", "message_source", "actual_cancelled", "actual_dep_delay_min",
        "actual_trouble", "verdict", "outcome_state",
    }


def test_cache_is_bounded():
    """A public endpoint whose cache key carries caller-chosen values needs a cap.

    This pod never restarts, so an unbounded dict is a slow memory leak an anonymous caller can
    drive. Distinct windows are the cheapest way to mint keys.
    """
    _reset([_row()])
    for i in range(historikk._CACHE_MAX_ENTRIES + 50):
        client.get("/historikk-api/decisions", params={"from_date": f"2026-0{i % 7 + 1}-01"})
    assert len(historikk._cache) <= historikk._CACHE_MAX_ENTRIES


def test_limit_is_capped():
    _reset([_row()])
    assert client.get("/historikk-api/decisions", params={"limit": 10_000}).status_code == 422


def test_summary_reports_counts_rates_and_coverage():
    _reset([{
        "n_hit": 9, "n_false_alarm": 1, "n_miss": 3, "n_correct_reject": 40,
        "n_unknown": 7, "n_pending": 2,
        "precision": 0.9, "recall": 0.75,
        "median_lead_min_on_hits": 42, "n_hit_with_30min_notice": 6,
    }])
    s = client.get("/historikk-api/summary").json()
    assert (s["n_hit"], s["n_false_alarm"], s["n_miss"]) == (9, 1, 3)
    assert s["precision"] == 0.9
    # Coverage is part of the contract: a rate without it cannot be read honestly.
    assert s["n_unknown"] == 7
    assert s["n_pending"] == 2
    assert s["median_lead_min_on_hits"] == 42


def test_summary_survives_an_empty_window():
    """A new environment has recorded nothing yet; that is zeroes, not a 500."""
    _reset([])
    s = client.get("/historikk-api/summary").json()
    assert s["n_hit"] == 0 and s["n_unknown"] == 0
    assert s["precision"] is None and s["recall"] is None


def test_missing_tables_report_unavailable_not_a_traceback():
    _reset([], error=RuntimeError("404 Not found: Table disruptions.varsel_decisions"))
    r = client.get("/historikk-api/decisions")
    assert r.status_code == 503
    assert "decision log" in r.json()["detail"].lower()


@contextmanager
def _deployed_as(project: str | None):
    """Run the body as if the app were deployed in `project` (ambient env restored afterwards)."""
    before = {k: os.environ.get(k) for k in ("GOOGLE_CLOUD_PROJECT", "KIX_ENVIRONMENT")}
    os.environ.pop("KIX_ENVIRONMENT", None)  # a local override would mask the project
    if project is None:
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
    else:
        os.environ["GOOGLE_CLOUD_PROJECT"] = project
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_config_names_the_non_prod_environment():
    """The page shows this behind its title: dev and prd serve the same page on near-identical
    hosts, so nothing else says which environment's numbers you are reading."""
    with _deployed_as("ent-kixavvik-dev"):
        assert client.get("/historikk-api/config").json()["environment"] == "dev"
    with _deployed_as("ent-kixavvik-prd"):
        assert client.get("/historikk-api/config").json()["environment"] is None


def test_config_survives_an_unreadable_decision_log():
    """Why it is its own endpoint and not a field on /summary: when the log cannot be read, knowing
    which environment you are on is what tells a broken deployment from a fresh one."""
    _reset([], error=RuntimeError("404 Not found: Table disruptions.varsel_decisions"))
    with _deployed_as("ent-kixavvik-dev"):
        assert client.get("/historikk-api/summary").status_code == 503
        queries_so_far = len(fake.calls)
        r = client.get("/historikk-api/config")
    assert r.status_code == 200
    assert r.json()["environment"] == "dev"
    assert len(fake.calls) == queries_so_far, "/config must not query BigQuery"


def test_results_are_cached_between_identical_requests():
    _reset([_row()])
    client.get("/historikk-api/decisions")
    client.get("/historikk-api/decisions")
    assert len(fake.calls) == 1, "the second identical request should be served from cache"


def test_different_filters_do_not_share_a_cache_entry():
    _reset([_row()])
    client.get("/historikk-api/decisions", params={"operator": "VYG"})
    client.get("/historikk-api/decisions", params={"operator": "RUT"})
    assert len(fake.calls) == 2


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} historikk checks passed.")


if __name__ == "__main__":
    _run()
