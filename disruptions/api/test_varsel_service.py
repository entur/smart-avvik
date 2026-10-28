"""Checks for main._varsel_connectivity — "does a planned trip run between these stops today?".

Hermetic: the BigQuery client is stubbed before main is imported and every query is answered from a
canned row, so nothing here touches BigQuery or the network.

This is the guard-rail behind the registration check, and the thing it must never do is claim "no
service" when it simply could not ask: that would refuse or warn about journeys that are fine. Every
failure path here therefore returns None, which the caller treats as "unknown", not as "no".

Run: uv run --with fastapi --with pydantic --with google-cloud-bigquery python test_varsel_service.py
"""

import contextlib
import logging
import os
import tempfile
from types import SimpleNamespace

# --- Stub BigQuery + pin the state file BEFORE importing main/varsel (both act at import). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]
_STATE_FD, _STATE_PATH = tempfile.mkstemp(prefix="varsel_service_state_", suffix=".json")
os.close(_STATE_FD)
os.environ["VARSEL_STATE_PATH"] = _STATE_PATH

import main


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self, *args, **kwargs):
        if isinstance(self._rows, Exception):
            raise self._rows
        return list(self._rows)


class _FakeBQ:
    """Records every query it is asked, and answers with a canned row (or raises)."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[dict] = []

    def query(self, sql, job_config=None, *args, **kwargs):
        params = {p.name: list(p.values) for p in (job_config.query_parameters if job_config else [])}
        self.calls.append({"sql": sql, "params": params})
        return _FakeJob(self.rows)


def _row(forward, reverse, pair_id=0):
    return [SimpleNamespace(pair_id=pair_id, forward=forward, reverse=reverse)]


def _rows(*verdicts):
    """One result row per (pair_id, forward, reverse), for the batch checks."""
    return [
        SimpleNamespace(pair_id=pair_id, forward=forward, reverse=reverse)
        for pair_id, forward, reverse in verdicts
    ]


def _asked(fake, index=0):
    """The stop pairs one recorded job asked about, decoded from its `pairs` parameter.

    The parameter is an ARRAY<STRUCT<pair_id, from_refs, to_refs>>, so the ref arrays arrive as
    nested `ArrayQueryParameter`s rather than as plain lists. Decoding it here rather than asserting
    on the client's own objects keeps every check below about the QUESTION and not about the shape
    the BigQuery client happens to hold it in.
    """
    pairs = fake.calls[index]["params"]["pairs"]
    return [
        (list(struct.struct_values["from_refs"].values), list(struct.struct_values["to_refs"].values))
        for struct in sorted(pairs, key=lambda s: s.struct_values["pair_id"])
    ]


@contextlib.contextmanager
def _captured(level=logging.DEBUG):
    """Every record `main` writes inside the block, for the redaction and aggregation checks."""
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    sink = _Sink(level)
    logger = logging.getLogger("main")
    previous = logger.level
    logger.addHandler(sink)
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(sink)
        logger.setLevel(previous)


def _install(fake, monkeypatch):
    monkeypatch.setattr(main, "bq_client", fake)
    main._connectivity_cache.clear()
    main._connectivity_calls.clear()


def test_both_directions_are_reported(monkeypatch):
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) == (True, False)
    assert _asked(fake) == [(["A"], ["B"])]


def test_every_ref_of_a_place_is_passed_through(monkeypatch):
    """The caller hands over all refs of a merged stop; they must reach the query as arrays, or the
    check would report "no service" for a trip recorded on the other ref."""
    fake = _FakeBQ(_row(True, True))
    _install(fake, monkeypatch)
    main._varsel_connectivity(["A", "A2"], ["B", "B2"])
    assert _asked(fake) == [(["A", "A2"], ["B", "B2"])]


def test_no_shared_trip_is_a_negative_the_query_states(monkeypatch):
    """A real "these stops are not connected" is a ROW saying so, never a missing row.

    The query LEFT JOINs from its own `@pairs` parameter, so every pair it is asked about gets a row
    whether or not any trip serves it — which is why `forward=False` is the negative and an ABSENT
    row is not. Until U10 the single-pair query could only ever return one row and the code read an
    empty result set as `(False, False)`; that reading turned a response nobody could parse into a
    caveat, which is exactly what R5 says must never be rendered from a non-answer.
    """
    fake = _FakeBQ(_row(False, False))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) == (False, False)


def test_a_pair_the_job_answered_nothing_for_is_unknown(monkeypatch):
    """The other half of the rule above, and the falsification of it.

    An empty result set — or a row set missing one pair of a batch — is a job that did not answer,
    and a caller that stored it would publish "no direct service" on the strength of nothing.
    """
    fake = _FakeBQ([])
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) is None
    _install(_FakeBQ(_rows((0, True, False), (2, False, True))), monkeypatch)
    assert main._varsel_connectivity_batch(
        [(["A"], ["B"]), (["C"], ["D"]), (["E"], ["F"])]
    ) == [(True, False), None, (False, True)]


def test_a_query_failure_is_unknown_not_no_service(monkeypatch):
    fake = _FakeBQ(RuntimeError("BigQuery is down"))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) is None


def test_a_repeated_pair_is_answered_from_cache(monkeypatch):
    """A client retrying a rejected registration must not cost a query per attempt."""
    fake = _FakeBQ(_row(False, True))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) == (False, True)
    assert main._varsel_connectivity(["A"], ["B"]) == (False, True)
    assert len(fake.calls) == 1


def test_the_cache_key_ignores_ref_order(monkeypatch):
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    main._varsel_connectivity(["A2", "A"], ["B"])
    main._varsel_connectivity(["A", "A2"], ["B"])
    assert len(fake.calls) == 1


def test_a_failure_is_not_cached(monkeypatch):
    """Caching "unknown" would freeze the check off for the whole TTL after a single hiccup."""
    fake = _FakeBQ(RuntimeError("transient"))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) is None
    fake.rows = _row(True, False)
    assert main._varsel_connectivity(["A"], ["B"]) == (True, False)


def test_distinct_pairs_are_rate_limited(monkeypatch):
    """/varsel-api is unauthenticated and the cache cannot bound a caller inventing pairs. Past the
    ceiling the check reports unknown — registrations still work, unvalidated."""
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    for i in range(main._CONNECTIVITY_MAX_PER_MIN):
        assert main._varsel_connectivity([f"A{i}"], ["B"]) == (True, False)
    assert main._varsel_connectivity(["A-over-the-limit"], ["B"]) is None
    assert len(fake.calls) == main._CONNECTIVITY_MAX_PER_MIN


def test_the_rate_limit_does_not_apply_to_cached_pairs(monkeypatch):
    """Otherwise a busy minute would start returning unknown for pairs already known."""
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    main._varsel_connectivity(["A"], ["B"])
    spent = main._connectivity_calls[main._CONNECTIVITY_REQUEST_PATH]
    spent[:] = [spent[0]] * main._CONNECTIVITY_MAX_PER_MIN
    assert main._varsel_connectivity(["A"], ["B"]) == (True, False)
    assert main._varsel_connectivity(["C"], ["D"]) is None


def test_a_poller_sweep_cannot_spend_a_travellers_budget(monkeypatch):
    """`alternatives._POLLER_FETCH_MAX_PER_MIN`'s split, for the same reason and one layer over.

    The 30/min ceiling is an ABUSE-CONTROL number: /varsel-api is unauthenticated, and it bounds a
    caller who keeps inventing pairs. The webhook channel then arrived and spent the same tally --
    a servability question on every create and replace (a client may issue 120 of those a minute)
    and `notification_servability.recheck` on the poller's cadence. A bulk onboarding of an existing
    commuter base, which is exactly what that channel is for, therefore exhausted a traveller-facing
    budget as a side effect, and the degradation is silent: `None` means "could not establish", so a
    real traveller's journey registers with no no-direct-service caveat at all.

    Onboarding no longer asks at all (U10) and what is left on the second tally is the two poller
    sweeps. Two tallies, never one, and the ceilings are deliberately different -- see the
    constants.
    """
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    for i in range(main._POLLER_CONNECTIVITY_MAX_PER_MIN):
        assert main._varsel_connectivity([f"P{i}"], ["B"], path=main._CONNECTIVITY_POLLER_PATH) == (
            True,
            False,
        )
    assert main._varsel_connectivity(["P-over"], ["B"], path=main._CONNECTIVITY_POLLER_PATH) is None
    # The traveller's own budget is untouched: a full poller minute buys a full traveller minute.
    for i in range(main._CONNECTIVITY_MAX_PER_MIN):
        assert main._varsel_connectivity([f"T{i}"], ["B"]) == (True, False)
    assert main._varsel_connectivity(["T-over"], ["B"]) is None


def test_the_poller_path_is_the_smaller_share(monkeypatch):
    """A sweep issues one job a cycle; a traveller is waiting on a page."""
    del monkeypatch
    assert 0 < main._POLLER_CONNECTIVITY_MAX_PER_MIN < main._CONNECTIVITY_MAX_PER_MIN


def test_the_query_asks_both_call_sources(monkeypatch):
    """Rail and bus planned calls live in separate tables; a rail-only check would warn about every
    bus and ferry journey ever registered."""
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    main._varsel_connectivity(["A"], ["B"])
    sql = fake.calls[0]["sql"]
    assert "rail_calls_planned_today" in sql and "bus_calls_planned_today" in sql


# ---- U10: one job for many pairs -----------------------------------------------------------------


def test_one_job_answers_many_pairs_and_the_answers_are_positional(monkeypatch):
    """The unit's whole point: the sweep's BigQuery cost stops scaling with the record count.

    Falsified by asking three pairs one at a time — `fake.calls` is then 3 and the servability
    re-check goes back to a bound of four records a cycle.
    """
    fake = _FakeBQ(_rows((0, True, False), (1, False, False), (2, False, True)))
    _install(fake, monkeypatch)
    answers = main._varsel_connectivity_batch([(["A"], ["B"]), (["C"], ["D"]), (["E"], ["F"])])
    assert answers == [(True, False), (False, False), (False, True)]
    assert len(fake.calls) == 1
    assert _asked(fake) == [(["A"], ["B"]), (["C"], ["D"]), (["E"], ["F"])]


def test_a_repeated_pair_costs_one_place_in_the_job_and_is_answered_twice(monkeypatch):
    """Two subscriptions on one corridor are one question. Answers stay positional regardless."""
    fake = _FakeBQ(_rows((0, True, True), (1, False, False)))
    _install(fake, monkeypatch)
    answers = main._varsel_connectivity_batch(
        [(["A"], ["B"]), (["C"], ["D"]), (["A"], ["B"]), (["B", "A"], ["B"])]
    )
    assert answers == [(True, True), (False, False), (True, True), None]
    assert _asked(fake) == [(["A"], ["B"]), (["C"], ["D"]), (["A", "B"], ["B"])]


def test_a_cached_pair_takes_no_place_in_the_job(monkeypatch):
    """An answer about a stop pair is the same answer whoever asked — the cache is shared, so a
    sweep must not re-ask what a traveller's registration paid for a minute ago."""
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) == (True, False)
    fake.rows = _rows((0, False, True))
    assert main._varsel_connectivity_batch([(["A"], ["B"]), (["C"], ["D"])]) == [
        (True, False),
        (False, True),
    ]
    assert _asked(fake, 1) == [(["C"], ["D"])]


def test_a_refused_batch_still_serves_what_the_cache_holds(monkeypatch):
    """The ceiling and a BigQuery error both answer `None` for the pairs they touched — and only
    for those. A pair already known is not un-known by somebody else's failure."""
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) == (True, False)
    fake.rows = RuntimeError("BigQuery is down")
    assert main._varsel_connectivity_batch([(["A"], ["B"]), (["C"], ["D"])]) == [(True, False), None]
    spent = main._connectivity_calls[main._CONNECTIVITY_REQUEST_PATH]
    spent[:] = [spent[0]] * main._CONNECTIVITY_MAX_PER_MIN
    assert main._varsel_connectivity_batch([(["A"], ["B"]), (["E"], ["F"])]) == [(True, False), None]


def test_a_batch_past_the_job_bound_answers_the_overflow_as_unknown(monkeypatch):
    """A caller that forgets its own bound costs its extra pairs an answer, not the process a
    multi-megabyte query parameter. Unknown leaves them due, which is the safe direction."""
    bound = main._CONNECTIVITY_MAX_PAIRS_PER_JOB
    fake = _FakeBQ(_rows(*((i, True, False) for i in range(bound))))
    _install(fake, monkeypatch)
    answers = main._varsel_connectivity_batch([([f"A{i}"], ["B"]) for i in range(bound + 3)])
    assert len(_asked(fake)) == bound
    assert answers[:bound] == [(True, False)] * bound
    assert answers[bound:] == [None, None, None]


def test_the_cache_keeps_the_newest_when_a_batch_overflows_it(monkeypatch):
    """Bounded in ONE pass, and the survivors are the newest.

    Evicting the single oldest entry per insertion — which is what this did while a job asked about
    one pair — is quadratic once a sweep inserts thousands, and it would spend that cost on the
    poller's own thread. The bound is patched down here so the check is about the POLICY rather
    than about how long it takes to fill eight thousand entries.
    """
    monkeypatch.setattr(main, "_CONNECTIVITY_CACHE_MAX", 4)
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    for i in range(3):
        main._varsel_connectivity([f"old{i}"], ["B"])
    fake.rows = _rows((0, False, True), (1, False, True), (2, False, True))
    main._varsel_connectivity_batch([([f"new{i}"], ["B"]) for i in range(3)])
    assert len(main._connectivity_cache) == 4
    kept = {key[0][0] for key in main._connectivity_cache}
    assert {"new0", "new1", "new2"} <= kept, kept
    assert "old0" not in kept and "old1" not in kept


def test_the_cache_holds_both_sweeps_and_the_request_path(monkeypatch):
    """The number itself: a sweep must not evict the other sweep, and neither the traveller's."""
    del monkeypatch
    assert main._CONNECTIVITY_CACHE_MAX >= 2 * main._CONNECTIVITY_MAX_PAIRS_PER_JOB


def test_no_service_check_record_carries_a_stop_ref(monkeypatch):
    """R11 one module over, and the close of the webhook channel's fourth carrier.

    `main._varsel_connectivity` wrote both refs into the MESSAGE of its success and failure records,
    under `/varsel`'s event name — so a webhook subscription's origin and destination reached Cloud
    Logging although no `notification_*` line carried them. The pair rides as
    `alternatives.pair_digest` now, salted per process.
    """
    fake = _FakeBQ(_rows((0, True, False), (1, False, False)))
    _install(fake, monkeypatch)
    with _captured() as records:
        main._varsel_connectivity_batch([(["NSR:StopPlace:337"], ["NSR:StopPlace:548"])])
        main._varsel_connectivity_batch(
            [(["NSR:StopPlace:1"], ["NSR:StopPlace:2"]), (["NSR:StopPlace:3"], ["NSR:StopPlace:4"])]
        )
        fake.rows = RuntimeError("BigQuery is down")
        main._varsel_connectivity_batch([(["NSR:StopPlace:5"], ["NSR:StopPlace:6"])])
    assert records, "no record was captured, so this check proves nothing"
    for record in records:
        rendered = record.getMessage() + repr(getattr(record, "fields", {}))
        assert "NSR:" not in rendered, rendered


def test_a_batch_writes_one_record_and_not_one_per_pair(monkeypatch):
    """A sweep asking about thousands of pairs must not be thousands of INFO lines a cycle. Each
    answer is stored on the record it is about, which is where a reader should look for it."""
    fake = _FakeBQ(_rows(*((i, True, False) for i in range(12))))
    _install(fake, monkeypatch)
    with _captured() as records:
        main._varsel_connectivity_batch([([f"A{i}"], ["B"]) for i in range(12)])
    checks = [r for r in records if getattr(r, "fields", {}).get("event") == "varsel_service_check"]
    assert len(checks) == 1
    assert checks[0].fields["result"] == "batch"
    assert checks[0].fields["asked"] == 12 and checks[0].fields["established"] == 12


def test_the_sweeps_seam_widens_with_the_flag_and_never_establishes_a_pair_with_no_plan(monkeypatch):
    """KTD15 on `varsel._service_verdicts`, the one seam BOTH servability sweeps run on, so the
    browser caveat and the webhook `servable` stay one answer. With TRIP_PLANS_ENABLED on a pair no
    direct trip serves is servable when a held plan has a watched trip with a change (R3), and not
    when the plan has none; with no plan the transfer half is unestablished, so the answer is None,
    which neither sweep writes -- the record keeps its last verdict and is re-asked until the plan
    lands. With the flag off the direct answer passes through untouched (AE6)."""
    from datetime import UTC, datetime, timedelta

    import trips
    import varsel

    today = datetime.now(UTC).astimezone(varsel._OSLO).date()
    dep = datetime.now(UTC) + timedelta(hours=1)

    def plan(*operators):
        legs = tuple(
            trips.PlanLeg(
                operator=op, refs=((op, f"j{i}"),), line=None, public_code=None,
                mode="rail" if op == "VYG" else "bus", board_stop_place=f"S{i}",
                alight_stop_place=f"S{i + 1}", board_quay=None, alight_quay=None,
                board_position=0, alight_position=1, aimed_departure=dep + timedelta(minutes=20 * i),
                aimed_arrival=dep + timedelta(minutes=20 * i + 10), service_date=today,
            )
            for i, op in enumerate(operators)
        )
        return trips.DayPlan(trips=(trips.PlanTrip(legs=legs),))

    plans = {("A", "B", today): plan("RUT", "VYG"), ("A", "D", today): plan("RUT", "RUT")}
    asked: list = []
    monkeypatch.setattr(varsel, "_connectivity_batch_provider", lambda pairs: [
        (to_refs == ("C",), False) for _from_refs, to_refs in pairs
    ])
    monkeypatch.setattr(varsel, "_trip_plans", lambda f, t, d: asked.append((f, t)) or plans.get((f, t, d)))
    pairs = [("A", "B"), ("A", "C"), ("A", "D"), ("A", "E")]
    before = os.environ.pop("TRIP_PLANS_ENABLED", None)
    try:
        assert varsel._service_verdicts(pairs) == [(False, False), (True, False), (False, False), (False, False)]
        assert asked == [], "the flag off reads no plan"
        os.environ["TRIP_PLANS_ENABLED"] = "true"
        assert varsel._service_verdicts(pairs) == [(True, False), (True, False), (False, False), None]
        assert ("A", "C") not in asked, "a direct answer needs no plan"
    finally:
        os.environ.pop("TRIP_PLANS_ENABLED", None)
        if before is not None:
            os.environ["TRIP_PLANS_ENABLED"] = before


class _MonkeyPatch:
    """Minimal monkeypatch shim so the tests run standalone (without pytest)."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        mp = _MonkeyPatch()
        try:
            t(mp)
        finally:
            mp.undo()
        print(f"  ok  {t.__name__}")
    os.unlink(_STATE_PATH)
    print(f"\n{len(tests)} planned-service checks passed.")
