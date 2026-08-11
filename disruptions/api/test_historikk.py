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
from pydantic import ValidationError

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
        "et_cancelled": False,
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


def test_resolved_only_asks_for_the_judged_population():
    """"Alle med fasit" is its own flag, not a fifth verdict.

    It must reach the query as `resolved_only` and leave `verdict` unset: the two narrow different
    ways round, and routing it through `verdict` would both fail validation and, if it did not,
    mean something else entirely.
    """
    _reset([_row()])
    client.get("/historikk-api/decisions", params={"resolved_only": "true"})
    params = fake.calls[0][1]
    assert params["resolved_only"] is True
    assert params["verdict"] is None

    # Default off: the page opens on everything, including the decisions still awaiting a verdict.
    _reset([_row()])
    client.get("/historikk-api/decisions")
    assert fake.calls[0][1]["resolved_only"] is False


def test_resolved_only_does_not_share_a_cache_entry_with_the_unfiltered_window():
    """The narrower answer must not be served to a caller who asked for everything.

    Same window, same operator — only this flag differs, so it has to be part of the cache key or
    whichever request lands first decides what both callers see for the next two minutes.
    """
    _reset([_row()])
    client.get("/historikk-api/decisions")
    client.get("/historikk-api/decisions", params={"resolved_only": "true"})
    assert len(fake.calls) == 2


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
        "final_delay_p50_min", "final_delay_p90_min", "alert_threshold", "et_cancelled",
        "pushed_any", "n_devices", "message", "message_source", "actual_cancelled",
        "actual_dep_delay_min", "actual_trouble", "verdict", "outcome_state",
    }


def test_the_alternative_columns_are_not_published():
    """`varsel_decisions` grew alt_status and three flags about the journey we recommended
    (2026-08-06). None of them belongs on this page, and the check is not about privacy alone.

    `Decision` is `extra="forbid"` precisely so the SQL and the model cannot drift apart column by
    column: a future SELECT that picks up a new column fails loudly here instead of quietly widening
    what an unauthenticated public host serves. This asserts both halves — the response does not
    carry them, and the model refuses one if a query ever starts returning it.
    """
    _reset([_row()])
    d = client.get("/historikk-api/decisions").json()["decisions"][0]
    assert not {
        "alt_status", "has_alternative", "alt_minutes_saved", "alt_arrives_before_original",
        "alt_at_risk", "alt_scored", "alt_lines", "message_input",
    } & set(d)

    try:
        historikk.Decision(**_row(), alt_status="offered")
        raise AssertionError("Decision must stay extra='forbid' — it is what keeps the SELECT honest")
    except ValidationError:
        pass

    # The outcomes table has no alternative columns at all, so neither query can serve one by
    # accident; if that ever changes, the model above is the second line of defence, not the first.
    assert "alt_" not in historikk._DECISIONS_QUERY
    assert "alt_" not in historikk._SUMMARY_QUERY


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


def test_summary_splits_relayed_certainties_from_predicted_ones():
    """The blended precision is a mixture, and the page has to be able to say of what.

    A decision on a call ET had already declared cancelled is served at probability 1.0 by
    construction: a real hit for the traveller, a gimme for the models. Quoted alone the blended
    figure therefore moves with the MIX of the two populations — more relayed certainties in a
    window raises it with nothing about the app having changed.
    """
    _reset([{
        "n_hit": 10, "n_false_alarm": 2, "n_miss": 3, "n_correct_reject": 40,
        "precision": 10 / 12, "recall": 10 / 13,
        "n_hit_predicted": 6, "n_false_alarm_predicted": 2, "n_miss_predicted": 3,
        "n_correct_reject_predicted": 40, "precision_predicted": 0.75, "recall_predicted": 6 / 9,
        "n_hit_relayed": 4, "n_false_alarm_relayed": 0, "n_miss_relayed": 0,
        "n_correct_reject_relayed": 0, "precision_relayed": 1.0, "recall_relayed": 1.0,
        "n_unsegmented": 0,
    }])
    s = client.get("/historikk-api/summary").json()
    # The headline stays blended — it answers "was the traveller told the truth", which is the
    # question the page exists for.
    assert s["precision"] == 10 / 12
    # And it lies between the two segments it is a mixture of, which is exactly the point.
    assert s["predicted"]["precision"] == 0.75
    assert s["relayed"]["precision"] == 1.0
    assert s["predicted"]["precision"] < s["precision"] < s["relayed"]["precision"]
    # Nothing is dropped by the split: every labelled verdict lands in one segment or the other.
    assert s["predicted"]["n_hit"] + s["relayed"]["n_hit"] == s["n_hit"]
    assert s["predicted"]["n_miss"] + s["relayed"]["n_miss"] == s["n_miss"]


def test_lead_time_is_reported_per_segment_not_only_blended():
    """The blended varslingstid is the relayed population wearing the whole page's name.

    Measured over 14 days of prd on 2026-08-11: 89 min blended, out of 134 relayed and 6 predicted,
    because 445 of 514 hits were cancellations ET had already published. A reader who takes the
    blended figure for the models' own warning time is wrong by two hours, in the flattering
    direction — so each segment carries its own, and the page shows all three.
    """
    _reset([{
        "n_hit": 514, "median_lead_min_on_hits": 89, "n_hit_with_30min_notice": 347,
        "n_hit_predicted": 59, "median_lead_min_on_hits_predicted": 6,
        "n_hit_with_30min_notice_predicted": 16,
        "n_hit_relayed": 445, "median_lead_min_on_hits_relayed": 134,
        "n_hit_with_30min_notice_relayed": 326,
    }])
    s = client.get("/historikk-api/summary").json()
    assert s["median_lead_min_on_hits"] == 89
    assert s["predicted"]["median_lead_min_on_hits"] == 6
    assert s["relayed"]["median_lead_min_on_hits"] == 134
    assert s["predicted"]["n_hit_with_30min_notice"] == 16
    assert s["relayed"]["n_hit_with_30min_notice"] == 326


def test_a_negative_lead_survives_and_a_segment_without_hits_has_none():
    """Two ways this figure could quietly lie, and neither is hypothetical.

    A negative median is REAL: the first alert followed the aimed departure, which is still
    deliverable because an alert is dropped by the train's predicted departure, not its aimed one.
    Flooring it at 0 would hide the one segment where it happens. And a segment with no hits has no
    lead time at all — 0 min there would read as "we alerted exactly at departure", which is the
    opposite of "we never alerted", the same trap `precision` avoids by staying null.
    """
    _reset([{"n_hit_predicted": 3, "median_lead_min_on_hits_predicted": -4}])
    s = client.get("/historikk-api/summary").json()
    assert s["predicted"]["median_lead_min_on_hits"] == -4
    assert s["relayed"]["median_lead_min_on_hits"] is None
    assert s["relayed"]["n_hit_with_30min_notice"] == 0


def test_decisions_from_before_the_flag_join_neither_segment():
    """Unassignable is its own bucket, the same rule as `ukjent` vs `correct_reject`.

    Rows written before 2026-08-05 carry no flag, and which segment they belong to cannot be
    recovered. Defaulting them to 'predicted' would be right for most of them and would bury the
    ones worth finding: decisions where ET had already published and we alerted anyway — or did not.
    """
    _reset([{"n_hit": 9, "n_false_alarm": 1, "precision": 0.9, "n_unsegmented": 10}])
    s = client.get("/historikk-api/summary").json()
    assert s["n_unsegmented"] == 10
    assert s["predicted"]["n_hit"] == 0 and s["relayed"]["n_hit"] == 0
    # None, not 0.0: a rate over no decisions is unknown, and 0 % would read as "we got them wrong".
    assert s["predicted"]["precision"] is None and s["relayed"]["precision"] is None


def test_a_relayed_certainty_is_marked_on_the_row():
    """The segmented rates above must be traceable to individual decisions, or a reader cannot
    check them. Also what keeps a 100 % chip beside a train from reading as model skill."""
    _reset([_row(et_cancelled=True, final_cancel_probability=1.0, max_impact_probability=1.0)])
    d = client.get("/historikk-api/decisions").json()["decisions"][0]
    assert d["et_cancelled"] is True

    _reset([_row(et_cancelled=None)])
    assert client.get("/historikk-api/decisions").json()["decisions"][0]["et_cancelled"] is None


def test_summary_survives_an_empty_window():
    """A new environment has recorded nothing yet; that is zeroes, not a 500."""
    _reset([])
    s = client.get("/historikk-api/summary").json()
    assert s["n_hit"] == 0 and s["n_unknown"] == 0
    assert s["precision"] is None and s["recall"] is None
    assert s["n_unsegmented"] == 0
    assert s["predicted"]["n_hit"] == 0 and s["predicted"]["precision"] is None
    assert s["relayed"]["n_hit"] == 0 and s["relayed"]["precision"] is None


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
