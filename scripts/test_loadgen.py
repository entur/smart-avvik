#!/usr/bin/env python3
"""Checks for the load harness: the generator, the receiver and the cycle benchmark (U1, R16).

Hermetic. No GCP project, no Firestore, no BigQuery, no `gcloud`, and the only socket is a
loopback server this file starts and stops itself. Every external call is behind an injected
callable, which is also why the three scripts have the shape they have.

**A suite under `scripts/` is invisible to every CI loop above it**: ci.yml's API loop sets
`working-directory: disruptions/api`, the feedback loop sets `data/feedback`. That is exactly the
gap `data/feedback`'s own step was added to close, and the one the notification store's emulator
step greps ci.yml to keep closed. So this file greps ci.yml for its own step too
(`test_the_scripts_step_is_wired_in_ci`): deleting the step fails the suite rather than quietly
retiring it.

Run: uv run python scripts/test_loadgen.py   (or under pytest)
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import sys
import threading
import types
import urllib.request
from datetime import UTC, datetime
from typing import ClassVar

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(REPO / "disruptions" / "api") not in sys.path:
    sys.path.insert(0, str(REPO / "disruptions" / "api"))

import bench_cycle
import loadgen_receiver
import loadgen_subscriptions as gen

CI_YML = (REPO / ".github" / "workflows" / "ci.yml").read_text()

#: A stop list in `/varsel-api/stops`' shape, weighted like a real one: a few busy stations and a
#: long tail. Two of the rows share a `place_id`, because the picker collapses those and a
#: generator that did not would draw two refs of one place and report a pair count that
#: `varsel.create_journey` then canonicalizes away.
STOPS = [
    {"stop_place": f"NSR:StopPlace:{100 + i}", "stop_name": f"Stop {i}", "mode": "Tog",
     "n_calls": 1000 - i * 7, "place_id": f"NSR:StopPlace:{100 + i}"}
    for i in range(60)
] + [
    {"stop_place": "NSR:StopPlace:999", "stop_name": "Stop 0 buss", "mode": "Buss",
     "n_calls": 12, "place_id": "NSR:StopPlace:100"},
]


# ---- The generator's draw -----------------------------------------------------------------------


def test_explicit_pairs_give_exactly_that_many_pairs():
    """The plan's first scenario, and the form a pair sweep uses: `--pairs` is exact."""
    plan = gen.draw_definitions(STOPS, count=1000, pairs=200, seed=1)
    assert len(plan.subscriptions) == 1000
    assert len(plan.pairs) == 200, len(plan.pairs)
    # One subscription per definition at this form: `--pairs` overrides the ratio's derivation,
    # and distinctness is by construction -- 200 pairs x 37 window variants is 7 400 possible
    # definitions, so 1 000 distinct ones exist and the draw is required to find them.
    assert len(plan.definitions) == 1000


def test_prds_measured_ratio_reproduces_its_clustering():
    """The plan's last scenario: `--ratio prd --count 1000` gives ~820 definitions over ~580 pairs.

    Those are prd's measured 1.22 journeys per definition and 1.41 definitions per pair. R1 is
    sized on the RANGE between this and the first draft's assumption, so the harness has to be able
    to produce either — a run that quotes one has measured half the question.
    """
    plan = gen.draw_definitions(STOPS, count=1000, ratio="prd", seed=7)
    assert len(plan.subscriptions) == 1000
    assert 810 <= len(plan.definitions) <= 830, len(plan.definitions)
    assert 570 <= len(plan.pairs) <= 590, len(plan.pairs)


def test_the_plans_ratio_is_the_first_drafts_assumption_and_clusters_harder():
    plan = gen.draw_definitions(STOPS, count=1000, ratio="plan", seed=7)
    assert 330 <= len(plan.definitions) <= 360, len(plan.definitions)
    assert len(plan.pairs) < len(gen.draw_definitions(STOPS, count=1000, ratio="prd", seed=7).pairs)


def test_the_same_seed_draws_the_same_load():
    """Two runs either side of a code change must differ by the code, not by the draw."""
    a = gen.draw_definitions(STOPS, count=300, pairs=40, seed=11)
    b = gen.draw_definitions(STOPS, count=300, pairs=40, seed=11)
    c = gen.draw_definitions(STOPS, count=300, pairs=40, seed=12)
    assert a.definitions == b.definitions
    assert a.definitions != c.definitions


def _band_of(definition) -> tuple[str, tuple[int, ...]]:
    """Which configured band a drawn definition came from: its duration and weekday set."""
    return (
        str(gen._minutes(definition.end) - gen._minutes(definition.begin)),
        definition.days,
    )


def test_windows_cluster_by_band_rather_than_spreading_uniformly():
    """KTD8's definition-half memo is keyed on (pair, window tuple, weekday set) and pays only if
    windows cluster. A uniform draw over the day would answer that question with a "no" the
    generator invented, so the BAND draw is weighted and the busiest band has to dominate."""
    plan = gen.draw_definitions(STOPS, count=600, pairs=50, seed=3)
    counts: dict[tuple[str, tuple[int, ...]], int] = {}
    for definition in plan.definitions:
        key = _band_of(definition)
        counts[key] = counts.get(key, 0) + 1
    assert max(counts.values()) / sum(counts.values()) > 0.2, counts
    assert len(counts) == len({_band_of(b) for b in gen.WINDOW_CLUSTERS}), (
        "every configured band must be reachable"
    )


def test_the_window_step_is_the_plans_own_open_question():
    """"The app's window granularity decides it: free minutes, or a picker rounded to 15 minutes."
    Until the app team answers, the harness can run either — so the answer, when it arrives, is a
    flag rather than a rewrite. It is also what makes distinct definitions possible at scale: six
    fixed bands over 200 pairs is 1 200 definitions and no more."""
    assert gen.WINDOW_STEP_MIN == 15
    rounded = gen.window_variants(gen.WINDOW_CLUSTERS[0])
    free = gen.window_variants(gen.WINDOW_CLUSTERS[0], step=1)
    assert [w[0] for w in rounded] != [w[0] for w in free]
    assert all(w[0] < w[1] for w in rounded + free), "no variant may be an overnight window"
    # The all-day band has one variant and must stay inside the day: a shifted 00:00-23:59 would be
    # refused by SubscriptionSpec.validate, and a clamped one would collapse onto its neighbour.
    all_day = next(b for b in gen.WINDOW_CLUSTERS if b.begin == "00:00")
    assert gen.window_variants(all_day) == [("00:00", "23:59", all_day.days)]


def test_a_run_larger_than_the_definition_space_is_refused_with_the_arithmetic():
    """Distinctness is by construction, so a request that cannot be distinct has to be refused
    rather than quietly deduplicated: a smaller run than the benchmark row claims would flatter
    every per-definition figure, which is the denominator R1's budget is divided by."""
    try:
        gen.draw_definitions(STOPS, count=5000, pairs=10, seed=1)
    except gen.Refused as exc:
        assert "distinct definitions cannot exist" in str(exc), exc
    else:
        raise AssertionError("an impossible definition count must be refused")


def test_two_refs_of_one_place_are_drawn_as_one_place():
    """`/varsel-api/stops` collapses co-located refs keeping the busiest, and so must the draw:
    otherwise a definition is created that `varsel.create_journey` canonicalizes onto another,
    and the pair count in the benchmark row is larger than the pair count the pass sees."""
    places = gen._weighted_places(STOPS)
    refs = [ref for ref, _ in places]
    assert "NSR:StopPlace:999" not in refs, "the quieter ref of a shared place must not be drawn"
    assert len(places) == 60


def test_a_definition_is_never_degenerate():
    plan = gen.draw_definitions(STOPS, count=500, pairs=60, seed=5)
    for definition in plan.definitions:
        assert definition.from_stop != definition.to_stop
        assert definition.begin != definition.end, "an empty window is refused by the spec"
        assert definition.days and set(definition.days) <= set(range(7))


def test_asking_for_more_pairs_than_the_stop_list_supports_refuses_rather_than_spinning():
    tiny = STOPS[:3]
    try:
        gen.draw_definitions(tiny, count=100, pairs=500, seed=1)
    except gen.Refused as exc:
        assert "--pairs" in str(exc)
    else:
        raise AssertionError("an impossible pair count must be refused, not approximated")


# ---- Refusing production -------------------------------------------------------------------------


def test_a_production_project_is_refused_before_any_request():
    """The plan's second scenario. Four ways prd arrives and all four are closed.

    The cheapest one is that `prd` is not in ENVIRONMENTS at all. The other three matter because
    prd can arrive without anybody typing it: through `--project`, through an exported
    GOOGLE_CLOUD_PROJECT the Firestore client would silently prefer, and through a `--base-url`
    (which `--via api` uses and which no project check reaches).
    """
    for kwargs in (
        {"environment": "prd"},
        {"environment": "dev", "project": "ent-kixavvik-prd"},
        {"environment": "dev", "env_project": "ent-kixavvik-prd"},
        {"environment": "dev", "base_url": "https://kix-avvik.entur.io"},
        {"environment": "dev", "base_url": "https://kix-avvik.staging.entur.io"},
    ):
        try:
            gen.resolve_target(**kwargs)
        except gen.Refused:
            continue
        raise AssertionError(f"accepted {kwargs}")
    assert "prd" not in gen.ENVIRONMENTS and "prd" not in gen.PROJECTS


def test_the_cli_exits_non_zero_on_a_refusal_and_sends_nothing():
    """Exit code, not an exception trace — and the refusal happens in `main` before the stop list
    is fetched, which is the first thing that would touch the network."""
    fetched: list[str] = []
    before = gen.fetch_stops
    gen.fetch_stops = lambda base_url, **_: fetched.append(base_url) or []  # type: ignore[assignment]
    stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        code = gen.main(["--environment", "dev", "--project", "ent-kixavvik-prd",
                         "plan", "--count", "10"])
        message = sys.stderr.getvalue()
    finally:
        sys.stderr = stderr
        gen.fetch_stops = before
    assert code == 2, code
    assert "REFUSED" in message
    assert fetched == [], "a refusal must happen before anything is fetched"


def test_a_healthy_dev_target_resolves():
    target = gen.resolve_target("dev", env_project="ent-kixavvik-dev")
    assert target.project == "ent-kixavvik-dev"
    assert target.base_url == "https://kix-avvik.dev.entur.io"
    assert target.database == "notifications"


# ---- Direct-store seeding -------------------------------------------------------------------------


def test_a_seeded_document_is_the_one_the_api_would_have_written():
    """Review finding 1's whole point: the store mode exists because the API mode cannot reach
    100 000, and it is only worth having if what it writes is indistinguishable.

    So the document comes from `notification_store._new_subscription_doc` after `spec.validate()`
    — the API's own builder — rather than from this tool's idea of the schema. A hand-rolled
    document would drift the day a field is added, and it would drift SILENTLY: the pass reads
    documents through `_subscription_from_doc`, whose per-document guard turns an unreadable one
    into a skipped one, so the load would simply be smaller than the number in the benchmark row.
    """
    import notification_store

    now = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    plan = gen.draw_definitions(STOPS, count=3, pairs=2, seed=2)
    built = gen.store_documents(plan, destination_id="d1", daily_cap=9, now=now)
    assert [sid for sid, _ in built] == ["loadgen-000000", "loadgen-000001", "loadgen-000002"]
    for subscription_id, document in built:
        spec = notification_store.SubscriptionSpec(
            subscription_id=subscription_id,
            client_id=gen.LOADTEST_CLIENT_ID,
            destination_id="d1",
            from_stop=document["from_stop"],
            to_stop=document["to_stop"],
            begin=document["begin"],
            end=document["end"],
            days=tuple(document["days"]),
            external_ref=f"loadgen/{subscription_id}",
            test=True,
        )
        assert document == notification_store._new_subscription_doc(spec, now, daily_cap=9)
        # And it must READ BACK as a live subscription, which is what the pass asks of it.
        parsed = notification_store._subscription_from_doc(document)
        assert parsed.channel == notification_store.CHANNEL_WEBHOOK
        assert parsed.daily_cap == 9
        assert notification_store.subscription_is_live(document, now)


def test_the_seeded_documents_are_marked_test_and_belong_to_the_loadtest_client():
    """Two separate safety properties. `test: true` is what a receiver refuses before native
    dispatch, so a synthetic event cannot become a real notification even if one escapes; the
    client id is what makes teardown a query rather than a list somebody has to keep."""
    import notification_store

    now = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    plan = gen.draw_definitions(STOPS, count=5, pairs=3, seed=4)
    for _, document in gen.store_documents(plan, destination_id="d1", daily_cap=4, now=now):
        assert document["test"] is True
        assert document["client_id"] == gen.LOADTEST_CLIENT_ID
        assert document["subscription_id"].startswith(gen.SUBSCRIPTION_PREFIX)
    assert notification_store.CHANNEL_WEBHOOK == "webhook"


def test_teardown_sweeps_every_collection_a_subscription_touches():
    """A subscriptions-only sweep leaves events, deliveries and subjects behind: all four are TOP
    LEVEL carrying a subscription id precisely because a document delete removes no subcollection
    and a TTL field cascades into none, so the delivery query would keep returning loadtest rows
    for as long as their TTL lasts."""
    import notification_store

    assert set(gen.TEARDOWN_COLLECTIONS) == {"subscriptions", "events", "deliveries", "subjects"}
    # Every store collection except the two that carry no subscription or client id: feedback
    # contexts (a card render mints them, this tool never does, and their own 15-minute TTL
    # removes them) and situation translations (the poller's leader writes them per operator
    # text, this tool never does, and their 30-day TTL removes them).
    # A new collection that does carry a subscription id still fails this until it is swept.
    swept = set(notification_store.COLLECTIONS) - {
        notification_store.FEEDBACK_CONTEXTS,
        notification_store.SITUATION_TRANSLATIONS,
    }
    assert len(gen.TEARDOWN_COLLECTIONS) == len(swept)


def test_the_api_mode_is_bounded_and_the_store_mode_is_the_default_above_it():
    """Review finding 1. The create path counts a client's live subscriptions by READING them
    inside its create transaction, and the per-client rate quota tops out at 10 000 a minute — so
    API-only creation caps the harness far below Phase B's 100 000 and the gate would be
    unrunnable. `--via api` stays as a bounded mode so the create path is still exercised."""
    import notification_auth

    assert gen.MAX_VIA_API < notification_auth.QUOTA_BOUNDS["max_active_subscriptions"][1]
    assert "_create_body" in (gen.__doc__ or ""), "the reason must be stated where the mode is"
    assert gen.Plan(definitions=(), subscriptions=(), ratio="prd", seed=0, via="store").summary().count("via=store") == 1


def test_api_seeding_honours_the_clients_request_quota_and_retries_one_429():
    """Unthrottled, `--via api --count 1000` measures a rate limiter.

    The per-client default is 120 requests a minute, so a tight loop spends the run collecting
    429s: the outcomes dict would say so, but the run would have created a few dozen subscriptions.
    The sleep is the client's own quota, and a 429 is slept off ONCE (honouring `Retry-After`) and
    retried — a second one means the rate assumption is wrong, and grinding against it would hide
    that behind a long run.
    """
    import notification_auth

    assert gen.DEFAULT_REQUESTS_PER_MIN == notification_auth.PER_CLIENT_PER_MIN == 120
    slept: list[float] = []
    attempts: list[str] = []

    def _fake(base, method, path, *, key=None, body=None):
        attempts.append(body["subscription_id"])
        # The second subscription is refused once, then accepted.
        if body["subscription_id"] == "loadgen-000001" and attempts.count("loadgen-000001") == 1:
            return 429, {"retry_after": "7"}
        return 201, {}

    before = gen._api_call
    gen._api_call = _fake  # type: ignore[assignment]
    try:
        plan = gen.draw_definitions(STOPS, count=3, pairs=2, seed=1)
        outcomes = gen.seed_via_api(
            plan,
            gen.resolve_target("dev"),
            destination_id="d1",
            key="k",
            requests_per_min=60,
            sleeper=slept.append,
        )
    finally:
        gen._api_call = before
    assert outcomes == {"201": 3, "429_retried": 1}, outcomes
    assert attempts.count("loadgen-000001") == 2, "a 429 must be retried exactly once"
    # 7 s from the server's Retry-After, then 1.0 s between creates (60 / 60), none after the last.
    assert slept == [1.0, 7.0, 1.0], slept


def test_the_api_mode_says_what_it_gives_up_on_servability():
    """The create path asks BigQuery inline under a 15/min budget, well below the request rate — so
    past the first fifteen a minute `servable` is simply not established. That is a property of
    every API-seeded row rather than something a retry fixes, and the contract already tolerates it
    (null means "could not be established", never "no"). Stated where somebody reading a row full
    of nulls would look."""
    text = gen.seed_via_api.__doc__ or ""
    assert "servab" in text.lower() and "15" in text


def test_the_benchmark_row_says_which_mode_and_ratio_produced_it():
    """A store-seeded evening run at prd's ratio and an API-seeded morning run at the plan's ratio
    are two measurements of two different things; a row without its tags cannot be compared to
    anything."""
    plan = gen.draw_definitions(STOPS, count=10, pairs=3, seed=1)
    summary = gen.Plan(
        definitions=plan.definitions, subscriptions=plan.subscriptions,
        ratio="prd", seed=1, via="store", shape="morning",
    ).summary()
    for token in ("ratio=prd", "via=store", "shape=morning", "seed=1"):
        assert token in summary, summary


# ---- Browser registrations --------------------------------------------------------------------


def test_browser_registrations_are_throttled_and_say_why():
    """`POST /varsel-api/journeys` asks BigQuery for connectivity under a 30/min budget and then
    rewrites and re-uploads the WHOLE state blob under the lock the poller needs. The harness
    throttles to that rate rather than fanning out — a fan-out would stall the cycle it is trying
    to measure — and the docstring names the cost, because that cost IS R13's defect."""
    calls: list[tuple[str, str]] = []
    slept: list[float] = []
    before = gen._api_call
    gen._api_call = lambda base, method, path, **kw: calls.append((method, path)) or (200, {})  # type: ignore[assignment]
    try:
        plan = gen.draw_definitions(STOPS, count=6, pairs=2, seed=1)
        target = gen.resolve_target("dev")
        outcomes = gen.seed_browser_registrations(plan, target, devices=3, sleeper=slept.append)
    finally:
        gen._api_call = before
    assert [p for _, p in calls].count("/varsel-api/journeys") == 3
    assert outcomes == {"journey:200": 3}
    assert slept == [2.0, 2.0], slept  # 60 / 30 per minute, and none after the last
    assert gen.BROWSER_REGISTRATIONS_PER_MIN == 30
    assert "_CONNECTIVITY_MAX_PER_MIN" in pathlib.Path(gen.__file__).read_text()


def test_the_harness_never_invents_a_push_subscription():
    """`POST /varsel-api/devices` requires a real `PushSubscription` with an endpoint, so a device
    record could only be seeded by inventing one — which the poller would then try to POST to — or
    by holding a real browser's. Neither is a thing a load harness may do.

    A journey under an unknown device id is a legal registration and is the majority shape in prd
    anyway (79 of 186 journeys on 2026-09-04), so the harness loads passes 1a/1b/1c and the decision
    rows and sends nothing to any push service. What it therefore does NOT load is loop 2's
    fan-out, and the docstring says so rather than letting a benchmark row imply otherwise.
    """
    calls: list[tuple[str, str]] = []
    before = gen._api_call
    gen._api_call = lambda base, method, path, **kw: calls.append((method, path)) or (200, {})  # type: ignore[assignment]
    try:
        plan = gen.draw_definitions(STOPS, count=4, pairs=2, seed=1)
        gen.seed_browser_registrations(plan, gen.resolve_target("dev"), devices=2, sleeper=lambda _: None)
    finally:
        gen._api_call = before
    assert not [p for _, p in calls if p.startswith("/varsel-api/devices")], calls
    source = pathlib.Path(gen.__file__).read_text()
    assert "endpoint" in source, "the reason must be stated where somebody would add the call back"


# ---- The receiver --------------------------------------------------------------------------------


def _serve(record_path: pathlib.Path, **kwargs):
    """Start the real receiver on an ephemeral loopback port; return (server, record, thread)."""
    from http.server import ThreadingHTTPServer

    record = loadgen_receiver.Record(**kwargs)
    lock = threading.Lock()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), loadgen_receiver.make_handler(record, lock, record_path)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, record, thread


def _post(port: int, delivery_id: str) -> tuple[int, str | None]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook", data=b'{"x":1}', method="POST"
    )
    request.add_header("webhook-id", delivery_id)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers.get("Retry-After")
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return exc.code, exc.headers.get("Retry-After")


def test_the_receiver_records_a_refusal_with_its_retry_after():
    """The plan's third scenario. A 429 with `Retry-After: 30` is what exercises the per-delivery
    `retry_not_before`, and "the backoff works" is not a claim to make from reading the code —
    which means the harness has to be able to produce the refusal and to record that it did.

    Latency 0 here: what is under test is the record, and a real 2 s sleep per POST would put
    seconds on every CI run for nothing.
    """
    import urllib.error  # noqa: F401 - _post catches HTTPError

    path = pathlib.Path("/tmp/kix_loadgen_receiver_test.json")
    server, record, _ = _serve(path, latency_s=0.0, refuse_every=3, refuse_status=429, retry_after=30)
    port = server.server_address[1]
    try:
        statuses = [_post(port, f"d{i}") for i in range(1, 7)]
    finally:
        server.shutdown()
        server.server_close()
    assert [s for s, _ in statuses] == [200, 200, 429, 200, 200, 429], statuses
    assert [h for s, h in statuses if s == 429] == ["30", "30"]
    summary = record.as_dict()
    assert summary["accepted"] == 4 and summary["refused"] == 2
    assert summary["distinct_deliveries"] == 6 and summary["duplicate_posts"] == 0
    assert summary["configured"] == {
        "latency_s": 0.0, "refuse_every": 3, "refuse_status": 429, "retry_after": 30
    }
    assert json.loads(path.read_text())["refused"] == 2, "the record file is what bench reads"
    path.unlink(missing_ok=True)


def test_concurrent_posts_leave_a_record_file_that_parses():
    """A real race this suite caught: the record was written after the response and outside the
    lock, so two handler threads' `write_text` calls interleaved and the file failed to parse
    ("Extra data"). A capacity run is exactly the concurrent case, and the file is the measurement.

    Twelve posts across four threads against a threading server, then the file has to be JSON and
    has to agree with the in-memory record.
    """
    from concurrent.futures import ThreadPoolExecutor

    path = pathlib.Path("/tmp/kix_loadgen_receiver_concurrent.json")
    path.unlink(missing_ok=True)
    server, record, _ = _serve(path, latency_s=0.0)
    port = server.server_address[1]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: _post(port, f"c{i}"), range(12)))
    finally:
        server.shutdown()
        server.server_close()
    written = json.loads(path.read_text())  # the assertion IS that this parses
    assert written["accepted"] == 12 == record.as_dict()["accepted"]
    assert written["distinct_deliveries"] == 12
    assert not list(path.parent.glob(path.name + ".tmp")), "the temp file must be renamed away"
    path.unlink(missing_ok=True)


def test_the_record_write_is_serialized_and_atomic_by_construction():
    """The check above is a smoke test and cannot be more than that: the race it was written for
    needs two threads inside the same few microseconds, and it does not reproduce on demand — with
    the fix reverted it still passed three runs out of three here. So the invariant is asserted
    where it IS deterministic: the write happens inside the lock, and it goes through the rename.

    Both halves matter and they fix different things. Inside the lock is what stops two writers
    interleaving into one file. The rename is what stops a reader — and a capacity run's record is
    read while the run is going — catching a half-written one.
    """
    source = pathlib.Path(loadgen_receiver.__file__).read_text()
    body = source[source.index("def do_POST") : source.index("def do_GET")]
    inside = body[body.index("with lock:") :]
    assert "_write_atomically(record_path" in inside, (
        "the record write must be inside `with lock:` -- outside it, two handler threads "
        "interleave and the file stops parsing"
    )
    assert "write_text(json.dumps" not in body, "a direct write bypasses the rename"

    # And the helper itself: no temp file left behind, and the content is the payload.
    path = pathlib.Path("/tmp/kix_loadgen_atomic.json")
    path.unlink(missing_ok=True)
    loadgen_receiver._write_atomically(path, {"accepted": 3})
    loadgen_receiver._write_atomically(path, {"accepted": 4})
    assert json.loads(path.read_text()) == {"accepted": 4}
    assert not list(path.parent.glob(path.name + ".tmp"))
    path.unlink(missing_ok=True)


def test_the_receiver_counts_a_re_post_as_a_duplicate_not_as_a_second_delivery():
    """At-least-once is the published contract, so a duplicate is not a failure — it is the number
    U8's lease has to keep to one per worker crash rather than one per attempt."""
    server, record, _ = _serve(None, latency_s=0.0)
    port = server.server_address[1]
    try:
        _post(port, "same")
        _post(port, "same")
    finally:
        server.shutdown()
        server.server_close()
    summary = record.as_dict()
    assert summary["accepted"] == 2
    assert summary["distinct_deliveries"] == 1 and summary["duplicate_posts"] == 1


def test_the_receiver_record_carries_no_event_content():
    """A record file goes into a benchmark row. An event payload carries a traveller's origin,
    destination, departure time and the wording, so none of it may be in there."""
    server, record, _ = _serve(None, latency_s=0.0)
    port = server.server_address[1]
    try:
        _post(port, "d1")
    finally:
        server.shutdown()
        server.server_close()
    serialized = json.dumps(record.as_dict())
    for forbidden in ("NSR:", "payload", "body", "signature", "webhook-signature"):
        assert forbidden not in serialized, forbidden


def test_the_receiver_docstring_requires_a_public_https_endpoint_and_offers_no_bypass():
    """Review finding 2. The sender validates scheme, port and every resolved address on every
    attempt, and the tempting fix for a `localhost` receiver is a bypass switch that would then
    exist in the code production runs. There is none; the harness is documented to sit behind a
    tunnel instead, and `test_webhook_sender` pins the absence on the other side."""
    text = loadgen_receiver.__doc__ or ""
    assert "https" in text and "443" in text and "tunnel" in text.lower()
    # And no OPTION or environment read anywhere in the harness looks like one. A prose mention in
    # a docstring is the point of the docstring, so only an actual flag or variable counts.
    for name, path in (("receiver", loadgen_receiver.__file__), ("generator", gen.__file__)):
        for line in pathlib.Path(path).read_text().split("\n"):
            if not ("add_argument" in line or "environ" in line or "getenv" in line):
                continue
            assert not re.search(
                r"allow[_-]?local|insecure|no[_-]?verify|skip[_-]?validation|allow[_-]?private",
                line,
                re.IGNORECASE,
            ), f"{name}: {line.strip()}"


# ---- The benchmark -------------------------------------------------------------------------------


def _record(event: str, **fields) -> dict:
    return {"jsonPayload": {"event": event, **fields}}


def test_two_cycles_give_one_row_per_phase_with_wall_cpu_and_rss():
    """The plan's fourth scenario."""
    records = []
    for cycle, (wall, cpu, rss) in enumerate(((1000, 40, 4096), (3000, 120, -2048))):
        records.append(_record("poller_phase", phase="varsel_notify", ms=wall, cpu_ms=cpu,
                               rss_delta_bytes=rss, result="ok"))
        records.append(_record("poller_phase", phase="disruptions_sql", ms=9000, cpu_ms=30,
                               result="ok"))
        records.append(_record("poller_cycle", ms=50_000 + cycle * 1000))
    summary = bench_cycle.summarise(records)
    assert summary["cycles"] == {"n": 2, "p50_s": 50.5, "max_s": 51.0}
    varsel = summary["phases"]["varsel_notify"]
    assert varsel["wall_ms"] == {"n": 2, "p50": 2000.0, "max": 3000.0}
    assert varsel["cpu_ms"] == {"n": 2, "p50": 80.0, "max": 120.0}
    assert varsel["rss_delta_bytes"]["n"] == 2
    # A phase whose record has no RSS reading reports n=0 and a null, never 0 bytes: /proc exists
    # in the container and not on a laptop, and "absent" must not read as "allocated nothing".
    assert summary["phases"]["disruptions_sql"]["rss_delta_bytes"] == {"n": 0, "p50": None, "max": None}
    rendered = bench_cycle.render(summary, tags={"env": "dev", "ratio": "prd", "via": "store"})
    assert "varsel_notify" in rendered and "ratio=prd" in rendered and "via=store" in rendered


def test_an_erroring_phase_is_counted_rather_than_dropped():
    """A step that fails SLOWLY is the most interesting reading there is, and `_phase` records it
    from `finally` for exactly that reason. A summary that silently averaged it in with the
    successes would hide it; one that dropped it would hide it better."""
    summary = bench_cycle.summarise([
        _record("poller_phase", phase="notification_webhooks", ms=28_000, cpu_ms=90, result="error"),
        _record("poller_phase", phase="notification_webhooks", ms=1_400, cpu_ms=50, result="ok"),
    ])
    phase = summary["phases"]["notification_webhooks"]
    assert phase["errors"] == 1
    assert phase["wall_ms"]["max"] == 28_000.0


def test_the_three_steps_that_are_not_phases_are_read_and_summed():
    """`varsel_webhook_load` runs INSIDE `varsel_notify`, before pass 1a, so its `ms` appears in no
    phase breakdown and a reader would have to know to look for it. `notification_emit` carries
    KTD2's switch trigger as a number (`deferred` or `capped` above zero on two consecutive
    cycles) and `webhook_delivery_round` carries the drain. All three are counters and wall clock
    only -- nothing here may name a stop ref, a destination or a client."""
    summary = bench_cycle.summarise([
        _record("varsel_webhook_load", result="ok", n=1200, ms=430, skipped=2),
        _record("varsel_webhook_load", result="ok", n=1201, ms=610, skipped=0),
        _record("notification_emit", result="ok", created=180, deferred=0, capped=4),
        _record("webhook_delivery_round", result="ok", ready=200, sent=190, failed=1, retryable=9),
    ])
    load = summary["steps"]["varsel_webhook_load"]
    assert load["records"] == 2, "how many records"
    assert load["wall_ms"]["max"] == 610.0 and load["wall_ms"]["p50"] == 520.0
    # `n` is the record's OWN field (subscriptions loaded), which must survive beside the record
    # count rather than being overwritten by it.
    assert load["n"]["total"] == 2401 and load["skipped"]["total"] == 2
    assert summary["steps"]["notification_emit"]["capped"]["total"] == 4
    assert summary["steps"]["webhook_delivery_round"]["sent"]["total"] == 190
    rendered = bench_cycle.render(summary, tags={})
    assert "notification_emit" in rendered and "capped=4" in rendered
    # Every field read is a counter name, never anything identifying.
    for fields in bench_cycle.EVENT_COUNTERS.values():
        assert all(f in ("n", "skipped", "created", "deferred", "capped", "withdrawn",
                         "unchanged", "ready", "sent", "failed", "retryable") for f in fields)


def test_the_log_filter_names_the_event_field_the_logger_actually_writes():
    """`jsonPayload.event`, never `jsonPayload.fields.event`. logging_setup lifts the `fields` dict
    onto the payload root, so the nested form matches nothing — and reports it as "no cycles in
    the window", which reads like a quiet poller rather than a wrong filter."""
    clause = bench_cycle.log_filter()
    for event in bench_cycle.BENCH_EVENTS:
        assert f'jsonPayload.event="{event}"' in clause, event
    assert "jsonPayload.fields" not in clause
    assert 'resource.labels.namespace_name="kix-avvik"' in clause


def test_the_benchmark_reads_the_cluster_project_and_never_the_app_one():
    """App logs live in ent-kub-{dev,prd}, namespace kix-avvik. `gcloud logging read
    --project=ent-kixavvik-dev` returns NOTHING for them, silently, which is why the map is in the
    code rather than in somebody's shell history."""
    assert bench_cycle.CLUSTER_PROJECTS["dev"] == "ent-kub-dev"
    assert bench_cycle.CLUSTER_PROJECTS["prd"] == "ent-kub-prd"
    for project in bench_cycle.CLUSTER_PROJECTS.values():
        assert not project.startswith("ent-kixavvik-")


def test_the_benchmark_says_the_prometheus_series_cannot_be_scraped():
    """`kix_*` is in Thanos behind Grafana and not in Google Managed Prometheus (checked
    2026-09-11). Somebody will try; the output says so where they would look."""
    rendered = bench_cycle.render(bench_cycle.summarise([]), tags={})
    assert "THANOS" in rendered and "Cloud Trace" in rendered


def test_a_failed_gcloud_read_raises_rather_than_reporting_an_empty_window():
    """An empty result and a failed command look identical in a table of zeros."""
    class _Completed:
        returncode = 1
        stdout = ""
        stderr = "ERROR: (gcloud.logging.read) PERMISSION_DENIED"

    try:
        bench_cycle.read_records("ent-kub-dev", "2h", runner=lambda *a, **k: _Completed())
    except RuntimeError as exc:
        # Both branches land here: the `gcloud is not on PATH` guard raises the same class, which
        # is the honest answer on a machine that cannot run the read at all.
        assert "PERMISSION_DENIED" in str(exc) or "gcloud" in str(exc), exc
    else:
        raise AssertionError("a failed read must raise")


def test_the_profile_report_ranks_by_self_time_and_divides_by_definitions():
    """KTD17 asks where the INTERPRETER is. Cumulative time double-counts every caller of the
    function that actually holds it, so the ranking is `tottime`."""
    class _Stats:
        total_tt = 2.0
        stats: ClassVar[dict] = {
            ("/a/impact.py", 10, "_trip_cells"): (100, 100, 1.5, 1.9, {}),
            ("/a/varsel.py", 20, "match_journey"): (50, 50, 0.4, 1.95, {}),
            ("/a/x.py", 1, "noise"): (1, 1, 0.1, 0.1, {}),
        }

    report = bench_cycle.profile_report(_Stats(), definitions=1000, top=2)
    assert report["ms_per_definition"] == 2.0
    assert [row["function"].split("(")[-1] for row in report["top_by_self_time"]] == [
        "_trip_cells)", "match_journey)"
    ]


def test_the_profile_mode_fences_the_network_before_it_imports_anything():
    """It ran unfenced once and the report proved it: four TLS handshakes and 1 616 socket reads,
    with `_ssl.do_handshake` and `BufferedReader.read` at the top of a "where is the interpreter"
    ranking.

    `alternatives` and `webhook_sender` both allow a real request unless their variable is falsy OR
    the process looks like a test process — and they decide that from `sys.argv[0]`, which under
    `bench_cycle.py profile` is not a test name. So the fences have to be SET, before the modules
    are imported, and the two remaining writes (a Web Push, a decision row) stubbed.
    """
    source = pathlib.Path(bench_cycle.__file__).read_text()
    body = source[source.index("def run_profile") :]
    body = body[: body.index("\n# ---- CLI")]
    for fence in ('"KIX_ALLOW_JP_NETWORK", "0"', '"KIX_ALLOW_WEBHOOK_NETWORK", "0"',
                  '"VARSEL_LLM_PROVIDER"] = "off"'):
        assert fence in body, fence
    assert "_send_push" in body and "decisions.record" in body
    # Before the imports, or the module-level `_in_a_test_process()` reads have already happened.
    assert body.index("KIX_ALLOW_JP_NETWORK") < body.index("import test_varsel_api")


# ---- CI wiring ------------------------------------------------------------------------------------


def test_the_scripts_step_is_wired_in_ci():
    """A suite in a directory no CI loop enters is this repo's most expensive recurring bug: it
    prints a passing count while running nothing. The API loop sets `working-directory:
    disruptions/api` and the feedback loop sets `data/feedback`, so neither can see this file —
    deleting the step below must fail this suite rather than quietly retiring it."""
    assert "working-directory: scripts" in CI_YML, (
        "ci.yml has no step that runs scripts/test_*.py; this suite would never run"
    )
    step = CI_YML[CI_YML.index("working-directory: scripts") :]
    step = step[: step.index("- name:", 10)] if "- name:" in step[10:] else step
    assert "test_*.py" in step, step[:400]


def test_the_store_is_bound_to_the_resolved_project_not_to_ambient_credentials():
    """The four production refusals judge an ARGUMENT; none of them reaches the Firestore client.

    `notification_store.firestore_client()` builds `firestore.Client(database=...)` with no project,
    so without this binding the project comes from ambient ADC. A developer with
    GOOGLE_CLOUD_PROJECT=ent-kixavvik-prd exported -- which this repo's own test suites show is a
    normal thing to have exported -- would see "Seeding in dev" on stderr while synthetic
    subscriptions landed in PRODUCTION, where the poller turns them into real push notifications and
    real webhook deliveries. The refusals would all have passed.

    Both halves are pinned: the environment variable (which also covers notification_auth, reading
    through the same client) and the client actually handed to the store.
    """
    import loadgen_subscriptions as lg

    target = lg.resolve_target("dev", env_project=None)
    built: dict = {}
    before = os.environ.get("GOOGLE_CLOUD_PROJECT")
    os.environ["GOOGLE_CLOUD_PROJECT"] = "ent-kixavvik-prd"  # the stale shell variable

    class _FakeClient:
        def __init__(self, project=None, database=None):
            built["project"] = project
            built["database"] = database

    fake_firestore = types.SimpleNamespace(Client=_FakeClient)
    google_cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    prior = getattr(google_cloud, "firestore", None)
    google_cloud.firestore = fake_firestore
    sys.modules["google.cloud.firestore"] = fake_firestore

    configured: dict = {}
    store = types.ModuleType("notification_store")
    store.configure = lambda **kw: configured.update(kw)
    prior_store = sys.modules.get("notification_store")
    sys.modules["notification_store"] = store
    try:
        lg.bind_store(target)
        assert os.environ["GOOGLE_CLOUD_PROJECT"] == target.project, (
            "the stale production variable survived binding: "
            f"{os.environ['GOOGLE_CLOUD_PROJECT']!r}"
        )
        assert built["project"] == target.project, (
            f"the Firestore client was built on {built['project']!r}, not the resolved project"
        )
        assert configured["client"] is not None, "the store was left to build its own client"
    finally:
        if prior_store is not None:
            sys.modules["notification_store"] = prior_store
        else:
            sys.modules.pop("notification_store", None)
        sys.modules.pop("google.cloud.firestore", None)
        if prior is not None:
            google_cloud.firestore = prior
        if before is None:
            os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        else:
            os.environ["GOOGLE_CLOUD_PROJECT"] = before


def _run() -> None:
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} load-harness checks passed.")


if __name__ == "__main__":
    _run()
