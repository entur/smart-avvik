"""Checks for /historikk-api — the alert-decision log with realized outcomes.

Hermetic: BigQuery is replaced with a fake that records the SQL and parameters it was handed and
replays canned rows, so nothing here touches BigQuery or the network.

The checks that matter most are the ones about *absence*: an unresolved decision must never be
reported as a correct rejection, and "too recent to judge" must stay distinguishable from "judged,
and we could not find the train". Both would otherwise turn missing data into flattering numbers.

Run: uv run --with fastapi --with httpx --with pydantic python test_historikk.py  (or under pytest)
"""

import os
import re
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
        self.filter_rows: list[dict] = []
        self.registry_rows: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.job_configs: list = []
        self.error: Exception | None = None
        # Fail ONE query while the others answer normally — how a dependency in another project
        # (the stop registry) actually goes away.
        self.error_on: str | None = None

    def query(self, sql, job_config=None):
        # ArrayQueryParameter carries `.values`, ScalarQueryParameter `.value` -- the place filters
        # are arrays now, and reading only `.value` made every query raise AttributeError inside the
        # fake, which surfaced as unrelated tests failing on missing response keys.
        params = {
            p.name: list(p.values) if hasattr(p, "values") else p.value
            for p in (job_config.query_parameters if job_config else [])
        }
        self.calls.append((sql, params))
        self.job_configs.append(job_config)
        if self.error is not None:
            raise self.error
        if self.error_on is not None and sql == self.error_on:
            raise RuntimeError("403 Access Denied: national_stop_registry")
        # Three distinct shapes come off the same client now: decision rows, the /filters vocabulary
        # (four arrays, one row) and the NSR registry lookup behind the from/to grouping. The fake
        # answers per QUERY rather than per call — /filters and the registry both sit on the
        # VALIDATION path of /decisions and /summary, so a fake that replayed one list would hand a
        # filtered request decision rows as its allow-list and reject everything.
        if sql == historikk._FILTERS_QUERY:
            rows = self.filter_rows
        elif sql == historikk._STOP_PLACES_QUERY:
            rows = self.registry_rows
        else:
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


# The vocabulary /historikk-api/filters returns, matching the line and places _row() describes.
# One row of four arrays; the two place arrays carry a per-ref decision count, which is what decides
# a merged place's primary ref.
def _filter_row(**overrides) -> dict:
    base = {
        "operators": ["VYG"],
        "lines": ["VYG:Line:R11"],
        "from_places": [{"ref": "NSR:StopPlace:337", "n": 5}],
        "to_places": [{"ref": "NSR:StopPlace:548", "n": 5}],
    }
    base.update(overrides)
    return base


# One National Stop Register row: the name, and the parent place that says which refs are co-located.
def _place(ref, name, place_id=None) -> dict:
    return {"id": ref, "name": name, "place_id": place_id or ref}


def _reset(rows=None, error=None, filter_rows=None, registry=None):
    fake.rows = list(rows or [])
    fake.filter_rows = [_filter_row()] if filter_rows is None else list(filter_rows)
    fake.registry_rows = list(
        registry
        if registry is not None
        else [_place("NSR:StopPlace:337", "Oslo S"), _place("NSR:StopPlace:548", "Asker stasjon")]
    )
    fake.calls.clear()
    fake.job_configs.clear()
    fake.error = error
    fake.error_on = None
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


def test_sx_only_is_passed_through_and_defaults_off():
    """The "Bare med SIRI-SX-melding" checkbox.

    Off by default: decisions about trains no operator published anything about are a real
    population -- most of the log, since every planned call in the window is one -- and defaulting
    the page to hiding them is how a population disappears. The cut itself is in the SQL, on the
    `KIX:` namespace; what has to hold here is that the flag reaches it.
    """
    _reset([_row()])
    client.get("/historikk-api/decisions")
    assert fake.calls[0][1]["sx_only"] is False

    _reset([_row()])
    client.get("/historikk-api/decisions", params={"sx_only": "true"})
    assert fake.calls[0][1]["sx_only"] is True


def test_sx_only_does_not_share_a_cache_entry_with_the_unfiltered_window():
    """Same trap as `resolved_only`: a narrower answer served to a caller who asked for everything.

    Worth its own check rather than trusting the pattern -- the cache key is a positional tuple, so
    a flag added to the params list and forgotten in the key fails silently, and for two minutes
    whichever request landed first decides what both callers see."""
    _reset([_row()])
    client.get("/historikk-api/decisions")
    client.get("/historikk-api/decisions", params={"sx_only": "true"})
    assert len(fake.calls) == 2


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


def test_the_channel_filter_reaches_both_endpoints_and_is_allow_listed():
    """U16's filter, and the two properties that make it safe on a shared page.

    It is a POPULATION filter — which decisions exist — so it reaches `/summary` exactly as
    operator, line and the two places do, and the rates then describe the slice the list beside
    them shows. `verdict`, `alerted_only` and `resolved_only` narrow by OUTCOME and still must not.

    And the vocabulary is bounded, checked against `decisions.CHANNELS` rather than against the
    window: the value lands in the response-cache key, so an arbitrary string would be one cache
    entry and one BigQuery query apiece — `_checked_operator`'s reasoning. Against the CONSTANT and
    not the window, because "no webhook route this week" has to be an empty page rather than a 400.
    """
    _reset([_row()])
    client.get("/historikk-api/decisions", params={"channel": "webhook"})
    assert fake.calls[0][1]["channel"] == "webhook"

    _reset([{"n_hit": 1}])
    client.get("/historikk-api/summary", params={"channel": "both"})
    assert fake.calls[0][1]["channel"] == "both", (
        "the channel narrows the population, so the rates must be over the same slice"
    )

    _reset([_row()])
    bad = client.get("/historikk-api/decisions", params={"channel": "sms"})
    assert bad.status_code == 400
    assert "channel must be one of" in bad.json()["detail"]
    assert fake.calls == [], "a rejected channel must not reach BigQuery"


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
    """The response names a ROUTE (2026-08-22) but still never the journey's own identifiers.

    from_name/to_name are served deliberately -- see the PRIVACY note atop historikk_decisions.sql
    and the fact it rests on, that prd's registrations are test users. What must not follow them out
    is journey_digest, the join key to the LLM audit log, or the raw stop refs: those reach the
    process and are popped in _with_place_names, so a ref appearing here means the pop was lost.
    This asserts the whole key set, so a future field addition fails here rather than shipping.
    """
    _reset([_row()])
    d = client.get("/historikk-api/decisions").json()["decisions"][0]
    assert not {"journey_digest", "from_stop", "to_stop", "stop_place"} & set(d)
    assert {"from_name", "to_name"} <= set(d)
    assert set(d) == {
        "situation_number", "summary", "description", "operator", "line", "mode",
        "from_name", "to_name", "call_date",
        "departure_time", "n_decisions", "first_decided_at", "last_decided_at",
        "ever_alerted", "first_alert_at", "alert_lead_min",
        "max_impact_probability", "final_impact_probability", "final_cancel_probability",
        "final_delay_p50_min", "final_delay_p90_min", "alert_threshold", "et_cancelled",
        "pushed_any", "n_devices", "channel", "n_subscriptions",
        "message", "message_source", "actual_cancelled",
        "actual_dep_delay_min", "actual_trouble", "verdict", "outcome_state",
    }, "`channel` and `n_subscriptions` joined the set on 2026-09-13 (U16); nothing else did"


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

    Measured over 14 days of prd on 2026-08-11: 145 min blended, out of 177 relayed and 15
    predicted, because 133 of the 156 hit trains were cancellations ET had already published. A
    reader who takes the blended figure for the models' own warning time is wrong by hours, in the
    flattering direction — so each segment carries its own, and the page shows all three.

    Counted per TRAIN, unlike everything else here: `n_hit_trains` is its denominator, and it must
    not be confused with `n_hit`, which counts situations. The two differ by more than a factor of
    three in this window.
    """
    _reset([{
        "n_hit": 514, "median_lead_min_on_hits": 145, "n_hit_with_30min_notice": 124,
        "n_hit_trains": 156,
        "n_hit_predicted": 59, "median_lead_min_on_hits_predicted": 15,
        "n_hit_with_30min_notice_predicted": 5, "n_hit_trains_predicted": 19,
        "n_hit_relayed": 445, "median_lead_min_on_hits_relayed": 177,
        "n_hit_with_30min_notice_relayed": 116, "n_hit_trains_relayed": 133,
    }])
    s = client.get("/historikk-api/summary").json()
    assert s["median_lead_min_on_hits"] == 145
    assert s["predicted"]["median_lead_min_on_hits"] == 15
    assert s["relayed"]["median_lead_min_on_hits"] == 177
    assert s["predicted"]["n_hit_with_30min_notice"] == 5
    assert s["relayed"]["n_hit_with_30min_notice"] == 116
    # The lead figures are per train and the verdict counts are per situation. Serving the train
    # denominator is what keeps "5" beside "59" from reading as the same population.
    assert s["n_hit_trains"] == 156
    assert s["predicted"]["n_hit_trains"] == 19
    assert s["relayed"]["n_hit_trains"] == 133
    assert s["n_hit"] == 514


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


def test_filters_lists_the_windows_vocabulary_with_readable_names():
    """The dropdowns' contents, and the reason they are their own endpoint.

    /decisions is capped and ordered by departure, so options derived from it would omit anything
    that only ran early in the window — and, once a filter were applied, would shrink to the
    selection. The refs are the values; the names are display only.
    """
    _reset([_row()])
    r = client.get("/historikk-api/filters")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operators"] == ["VYG"]
    assert body["lines"] == ["VYG:Line:R11"]
    assert body["from_places"] == [{"ref": "NSR:StopPlace:337", "name": "Oslo S", "n_refs": 1}]
    assert body["to_places"] == [
        {"ref": "NSR:StopPlace:548", "name": "Asker stasjon", "n_refs": 1}
    ]


def test_place_options_fall_back_to_the_ref_when_the_registry_cannot_name_it():
    """A dropdown showing NSR refs is strictly better than one that fails to render, and the ref is
    the filter value either way, so an unnamed stop still filters correctly."""
    _reset([_row()], registry=[])
    body = client.get("/historikk-api/filters").json()
    assert body["from_places"] == [{"ref": "NSR:StopPlace:337", "name": None, "n_refs": 1}]


def test_place_options_are_sorted_by_the_name_the_reader_sees():
    """The SQL sorts by ref, which puts NSR:StopPlace:337 next to :338 and their names nowhere near
    each other. The endpoint re-sorts on the label — and puts the refs it could not name last,
    rather than interleaving them where their own text happens to fall."""
    _reset(
        [_row()],
        filter_rows=[
            _filter_row(from_places=[{"ref": f"NSR:StopPlace:{i}", "n": 1} for i in (1, 2, 3)])
        ],
        registry=[_place("NSR:StopPlace:1", "Ås"), _place("NSR:StopPlace:2", "Asker")],
    )
    body = client.get("/historikk-api/filters").json()
    assert [pl["name"] for pl in body["from_places"]] == ["Asker", "Ås", None]
    assert body["from_places"][-1]["ref"] == "NSR:StopPlace:3"


def test_an_unreadable_stop_registry_leaves_the_filters_usable():
    """The registry lives in another project behind an allAuthenticatedUsers grant, so it is the one
    dependency here that can go away on its own. It must cost names and grouping, never the page:
    the refs are still the filter values and still work."""
    _reset([_row()])
    fake.error_on = historikk._STOP_PLACES_QUERY
    try:
        r = client.get("/historikk-api/filters")
    finally:
        fake.error_on = None
    assert r.status_code == 200, r.text
    assert r.json()["from_places"] == [{"ref": "NSR:StopPlace:337", "name": None, "n_refs": 1}]


def test_co_located_refs_are_one_row_and_filtering_matches_every_one_of_them():
    """The bug this page shipped with, and the reason it was worse than a missing label.

    A station's rail and bus sides are separate NSR refs under one parent, and a registration lands
    on whichever one it was made against. Measured on dev 2026-08-21, Asker stasjon was BOTH
    NSR:StopPlace:418 (rail, 659 decisions) and NSR:StopPlace:3247 (bus, 183) -- offered as two rows,
    one of them a bare ref because main's picker cache had collapsed the other away. Naming it alone
    would have produced two identical "Asker stasjon" rows, which is the 2026-08-03 picker bug; so
    they merge, AND the merged row must filter on both refs, or the row would answer for 183 of the
    station's 842 decisions and silently drop the rest.
    """
    _reset(
        [_row()],
        filter_rows=[
            _filter_row(
                from_places=[
                    {"ref": "NSR:StopPlace:418", "n": 659},
                    {"ref": "NSR:StopPlace:3247", "n": 183},
                ]
            )
        ],
        registry=[
            _place("NSR:StopPlace:418", "Asker stasjon", "NSR:StopPlace:59616"),
            _place("NSR:StopPlace:3247", "Asker stasjon", "NSR:StopPlace:59616"),
        ],
    )
    body = client.get("/historikk-api/filters").json()
    assert body["from_places"] == [
        {"ref": "NSR:StopPlace:418", "name": "Asker stasjon", "n_refs": 2}
    ], "one row per physical place, nominating the busiest ref"

    client.get("/historikk-api/decisions", params={"from_place": "NSR:StopPlace:418"})
    _, params = next(c for c in fake.calls if c[0] == historikk._DECISIONS_QUERY)
    assert params["from_place_refs"] == ["NSR:StopPlace:3247", "NSR:StopPlace:418"], (
        "the merged row must match BOTH sides of the station, not just the one it is named after"
    )


def test_one_parent_place_can_hold_two_different_stops():
    """Grouping needs the NAME as well as the parent. Oslo S and Jernbanetorget share a parent and
    are not the same place; merging them would let a filter for one answer with the other."""
    _reset(
        [_row()],
        filter_rows=[
            _filter_row(
                from_places=[
                    {"ref": "NSR:StopPlace:337", "n": 9},
                    {"ref": "NSR:StopPlace:58366", "n": 4},
                ]
            )
        ],
        registry=[
            _place("NSR:StopPlace:337", "Oslo S", "NSR:StopPlace:59872"),
            _place("NSR:StopPlace:58366", "Jernbanetorget", "NSR:StopPlace:59872"),
        ],
    )
    body = client.get("/historikk-api/filters").json()
    assert [(pl["name"], pl["n_refs"]) for pl in body["from_places"]] == [
        ("Jernbanetorget", 1),
        ("Oslo S", 1),
    ]


def test_unnamed_refs_are_never_merged_into_a_named_neighbour():
    """An unnamed ref cannot be told from its neighbour on screen, so folding it into one would hide
    a ref behind a row nobody can read -- and would quietly widen what that row filters on."""
    _reset(
        [_row()],
        filter_rows=[
            _filter_row(
                from_places=[
                    {"ref": "NSR:StopPlace:418", "n": 9},
                    {"ref": "NSR:StopPlace:3247", "n": 4},
                ]
            )
        ],
        registry=[_place("NSR:StopPlace:418", "Asker stasjon", "NSR:StopPlace:59616")],
    )
    body = client.get("/historikk-api/filters").json()
    assert [(pl["ref"], pl["name"], pl["n_refs"]) for pl in body["from_places"]] == [
        ("NSR:StopPlace:418", "Asker stasjon", 1),
        ("NSR:StopPlace:3247", None, 1),
    ]


def test_line_and_place_filters_reach_the_query():
    _reset([_row()])
    r = client.get(
        "/historikk-api/decisions",
        params={
            "line": "VYG:Line:R11",
            "from_place": "NSR:StopPlace:337",
            "to_place": "NSR:StopPlace:548",
        },
    )
    assert r.status_code == 200, r.text
    # The first call is the allow-list lookup; the decisions query is the one that carries them.
    _, params = next(c for c in fake.calls if c[0] == historikk._DECISIONS_QUERY)
    assert params["line"] == "VYG:Line:R11"
    # Arrays, not scalars: a chosen place is every ref standing for it. Singletons here because
    # neither of these stops is co-located with another in the fixture.
    assert params["from_place_refs"] == ["NSR:StopPlace:337"]
    assert params["to_place_refs"] == ["NSR:StopPlace:548"]


def test_the_population_filters_also_narrow_the_summary():
    """operator/line/from_place/to_place narrow WHICH decisions, so the rates have to describe the
    same slice the list beside them shows. verdict/alerted_only/resolved_only narrow by OUTCOME and
    deliberately never reach /summary — filtering to hits would make precision read 100 %."""
    _reset([{"n_hit": 1, "n_false_alarm": 0, "n_miss": 0, "n_correct_reject": 0}])
    r = client.get("/historikk-api/summary", params={"line": "VYG:Line:R11"})
    assert r.status_code == 200, r.text
    _, params = next(c for c in fake.calls if c[0] == historikk._SUMMARY_QUERY)
    assert params["line"] == "VYG:Line:R11"
    # Empty array, never NULL: BigQuery has no NULL array parameter, so emptiness is how the SQL
    # spells "no filter" -- and a NULL here would make the predicate match nothing at all.
    assert params["from_place_refs"] == []
    assert "verdict" not in params


def test_a_value_no_decision_in_the_window_carries_is_rejected():
    """The data-driven filters are checked against /filters' own answer, the same reason
    _checked_operator checks the codespace: the value lands in the response-cache key on a public
    endpoint, so arbitrary strings would mint one cache entry and one BigQuery query apiece.

    A 400 rather than an empty list, because "this line is not in the window" and "it is, and your
    other filters excluded everything" are different answers — the page reconciles its selections
    against /filters when the window moves rather than sending one it knows is gone.
    """
    _reset([_row()])
    bad = client.get("/historikk-api/decisions", params={"line": "VYG:Line:R99"})
    assert bad.status_code == 400
    assert "Unknown line" in bad.json()["detail"]

    _reset([_row()])
    bad = client.get("/historikk-api/decisions", params={"from_place": "NSR:StopPlace:999"})
    assert bad.status_code == 400
    assert "Unknown from_place" in bad.json()["detail"]


def test_an_unfiltered_request_spends_no_query_on_the_allow_list():
    """The default load is the common one, and it needs no vocabulary to validate. Checking it
    unconditionally would double every page load's BigQuery cost for nothing."""
    _reset([_row()])
    client.get("/historikk-api/decisions")
    assert [c[0] for c in fake.calls] == [historikk._DECISIONS_QUERY]


def test_place_filters_do_not_share_a_cache_entry():
    """Same trap the sx_only and resolved_only checks guard: the cache key is a positional tuple, so
    a parameter added to the params list and forgotten in the key silently serves one caller's
    narrower answer to another for two minutes."""
    _reset([_row(), _row(situation_number="S2")], filter_rows=[
        _filter_row(
            from_places=[
                {"ref": "NSR:StopPlace:337", "n": 9},
                {"ref": "NSR:StopPlace:548", "n": 4},
            ]
        )
    ])
    client.get("/historikk-api/decisions", params={"from_place": "NSR:StopPlace:337"})
    client.get("/historikk-api/decisions", params={"from_place": "NSR:StopPlace:548"})
    client.get("/historikk-api/decisions", params={"to_place": "NSR:StopPlace:548"})
    # Distinct places in the default registry fixture, so these really are three different filters
    # and not one merged row asked for twice.
    decision_calls = [c for c in fake.calls if c[0] == historikk._DECISIONS_QUERY]
    assert len(decision_calls) == 3
    # ... and the allow-list itself is keyed on the window alone, so all three shared one lookup.
    assert len([c for c in fake.calls if c[0] == historikk._FILTERS_QUERY]) == 1


def test_the_refs_are_selected_but_only_resolved_names_are_served():
    """The 2026-08-22 reversal, and the line that survived it.

    The per-row redaction of the traveller's endpoints is gone: a card names the route. The refs
    themselves are not what leaves the process — the SQL selects them (both UNION arms, or the two
    sides disagree about a column and the UNION stops compiling), and _with_place_names pops them
    and puts NSR names in their place.

    Pinned because the failure is silent in the direction that matters: forget the pop and
    extra="forbid" raises, but resolve names WITHOUT selecting the refs and every card quietly reads
    "Ukjent → Ukjent", which looks like a registry outage rather than a missing column.
    """
    _reset([_row()])
    d = client.get(
        "/historikk-api/decisions", params={"from_place": "NSR:StopPlace:337"}
    ).json()["decisions"][0]
    assert not {"journey_digest", "from_stop", "to_stop"} & set(d)
    assert d["from_name"] and d["to_name"]

    # Both UNION arms must project them. Comments stripped -- the file discusses these columns at
    # length, which is the point.
    body = "\n".join(
        ln for ln in historikk._DECISIONS_QUERY.splitlines() if not ln.strip().startswith("--")
    )
    # Only the two top-level arms, identified by the column that is unique to them: the file also
    # holds a `NOT EXISTS (SELECT 1 ...)`, whose projection is a literal and has no columns at all.
    projections = [
        p
        for p in re.findall(r"\bSELECT\b(.*?)\bFROM\b", body, re.DOTALL)
        if "situation_number" in p
    ]
    assert len(projections) == 2, f"expected both UNION arms, found {len(projections)}"
    for projection in projections:
        assert "from_stop" in projection and "to_stop" in projection

    # journey_digest is the one that is never PROJECTED. It is still used, and has to be: the
    # anti-join between the two arms matches on it. Using a redacted column as a predicate is not
    # publishing it, which is the same distinction historikk_decisions.sql draws about the refs.
    for projection in projections:
        assert "journey_digest" not in projection


def test_the_no_filter_array_test_is_null_safe():
    """A text check, because nothing in this repo executes SQL -- and this defect is invisible
    without executing it.

    An EMPTY array parameter from the Python client serializes as {'arrayValues': []}, and BigQuery
    evaluates ARRAY_LENGTH of that as NULL, not 0. So `ARRAY_LENGTH(@refs) = 0` -- the obvious way to
    spell "no filter" -- is NULL on every unfiltered request, the ANDed predicate drops every row,
    and the whole page reads zero decisions. It shipped that way for one commit on 2026-08-21. A dry
    run validates it and `bq --parameter=refs:ARRAY<STRING>:[]` returns 0, because the CLI encodes an
    empty array differently from the client library, so neither of the cheap checks catches it.
    """
    for name, sql in (("decisions", historikk._DECISIONS_QUERY), ("summary", historikk._SUMMARY_QUERY)):
        bare = re.findall(r"\(ARRAY_LENGTH\(@\w+\)\s*= 0 OR", sql)
        assert not bare, f"{name}: {len(bare)} null-unsafe emptiness test(s); use IFNULL(..., 0)"
        assert "IFNULL(ARRAY_LENGTH(" in sql, f"{name}: no array emptiness test found at all"


# ---- KTD10: the webhook channel is invisible on this page ------------------------------------


def test_no_notification_store_field_or_collection_name_appears_in_this_pages_sql():
    """KTD10 as it stands after U16, as a grep over the UNCOMMENTED SQL: a query that cannot name a
    column cannot leak it however the code above it drifts.

    **What changed on 2026-09-13 and what did not.** Webhook routes ARE in `varsel_decisions` now
    (KTD4): the pages are behind Entur SSO since 2026-09-10, and at a hundred thousand commuters
    leaving client routes unscored would have meant leaving the product unscored. What is
    unchanged, and is the whole of what this guard is about, is the GRAIN: a row is one journey
    DEFINITION's view of one situation on one train, and the client side contributes a `channel`
    value and a COUNT. No subscription, client, destination, event or delivery identity reaches the
    decision log or either page.

    So the ban is on the names of Firestore's collections and identifying fields, matched as WHOLE
    WORDS -- `n_subscriptions` is a count and is asserted to be present below, while a bare
    `subscriptions` would be the collection this page must never join to. Over the uncommented text
    on purpose: these files' own headers discuss what they refuse to select, so a grep over the raw
    file would match an explanation.
    """
    import pathlib
    import re

    forbidden = (
        "client_id",
        "external_ref",
        "destination_id",
        "subscription_id",
        "callback_url",
        "alert_id",
        "subscriptions",
        "events",
        "deliveries",
        "alert_subjects",
    )
    here = pathlib.Path(__file__).parent
    files = sorted(here.glob("historikk_*.sql"))
    assert files, "the page's queries must be found for this guard to mean anything"
    for path in files:
        code = "\n".join(
            re.sub(r"--.*$", "", line) for line in path.read_text().splitlines()
        )
        for name in forbidden:
            # Whole words: the identifying names are banned, a column that merely ends in one is
            # not. `\b` does not match between `n_` and `subscriptions` (an underscore is a word
            # character), which is exactly the distinction -- `n_subscriptions` is a count and
            # `subscriptions` is the collection.
            assert not re.search(rf"\b{name}\b", code), (
                f"{path.name} names {name!r}: the decision log records webhook routes at DEFINITION "
                "grain (KTD4) and carries no client identity of any kind"
            )
    counts = "\n".join(
        re.sub(r"--.*$", "", line) for line in (here / "historikk_decisions.sql").read_text().splitlines()
    )
    assert "n_subscriptions" in counts, (
        "control: the guard must still admit the client-side COUNT, or it is passing because the "
        "column it is meant to distinguish from the collection is not there at all"
    )



def test_every_query_is_bounded_by_maximum_bytes_billed():
    """/historikk ran with NO cap at all until 2026-09-12, while /meldinger and /rushhistorikk --
    the same shape of page over the same table -- both capped at 2 GiB. The cap is not a pruning
    detector: a correctly pruned read of the decision log is a few MB, and the whole log is ~1.5 GB.
    It is a blast-radius bound on a public endpoint sharing its project quota with the poller, and
    what it catches is a query that starts reading something big -- the ET tables a mistyped join
    can reach are ~10 TiB for a two-year scan, three orders of magnitude above this.

    Every query, not the decision rows alone: /filters and the stop-registry lookup sit on the
    validation path of every request, so an uncapped one is reachable exactly as often."""
    _reset([_row()])
    client.get("/historikk-api/decisions")
    assert fake.job_configs, "no query was made"
    for job_config in fake.job_configs:
        assert job_config is not None, "a query ran with no job config, so with no cap"
        assert job_config.maximum_bytes_billed == historikk._MAX_BYTES_BILLED


def test_the_bytes_cap_is_one_constant_shared_with_meldinger():
    """R12 asks for the cap to be DERIVED from the measured thinned 14-day scan, and a derivation
    has to live in one place or the next reader guesses which copy is authoritative. The two pages
    ask the same question of the same table through the same grouping, so a cap that differed
    between them would be an accident. /rushhistorikk reads other tables and keeps its own.

    The value itself has deliberately not moved yet: the derivation waits on a measurement of the
    pruned, thinned log that only the load harness can take, and a cap derived from today's 1.5 GB
    would turn the log's growth into refusals on the page with the longest window."""
    import meldinger

    assert meldinger._MAX_BYTES_BILLED is historikk._MAX_BYTES_BILLED


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
