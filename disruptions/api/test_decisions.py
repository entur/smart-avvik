"""Checks for the alert-decision row shape — no BigQuery, no network.

The decision log's promise is that a decision can be re-examined later: what we predicted, what we
said, and — since this module started recording it — everything the message writer was shown. These
checks pin the parts that cannot be recovered afterwards if a row omits them.

Run: uv run --with pydantic python test_decisions.py  (or under pytest)
"""

import os
import re
from datetime import UTC, datetime, timedelta

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "ent-kixavvik-test")

import decisions

DEP = datetime(2026, 8, 4, 6, 12, tzinfo=UTC)  # 08:12 Europe/Oslo
DECIDED = DEP - timedelta(minutes=42)


class FakeObservation:
    cancelled = True
    at_stop = "NSR:StopPlace:509"
    at_stop_name = "Rørvik hurtigbåtkai"
    delayed_min = 7
    observed_at = DEP - timedelta(minutes=32)


class FakeLeg:
    def __init__(self, mode="rail", public_code="R11", line="VYG:Line:R11"):
        self.mode = mode
        self.public_code = public_code
        self.line = line


class FakeAlternativePrediction:
    stop_place = "NSR:StopPlace:337"
    scored = True
    # The boarding call's identity, as impact._score_candidate reads it off the resolved CallPred.
    operator = "VYG"
    jkey = "DatedServiceJourney:2_OSL-DRM_26-08-04"
    stop_order = 3


class FakeAlternative:
    departure_time = DEP + timedelta(minutes=9)
    arrival_time = DEP + timedelta(minutes=38)
    minutes_saved = 11
    arrives_before_original = True
    at_risk = False
    departure_prediction = FakeAlternativePrediction()

    def __init__(self):
        self.legs = [FakeLeg(), FakeLeg(mode="foot", public_code=None, line=None)]


class FakeImpactResponse:
    """The /impact response an alert was derived from — no lookup ran for this one.

    `original_is_best` and `alternative_subject` are absent rather than False/None on purpose: this
    module reads every field with getattr, and the default shape is what a response that never
    reached the Journey Planner looks like from here.
    """

    refreshed_at = DECIDED - timedelta(seconds=30)


class FakeAlert:
    situation_number = "NSB:SituationNumber:abc"
    summary = "Signalfeil"
    description = "Vi jobber med feilretting."
    operator = "VYG"
    line = "VYG:Line:R11"
    mode = "Tog"
    jkey = "DatedServiceJourney:1_OSL-DRM_26-08-04"
    stop_place = "NSR:StopPlace:337"
    stop_order = 10
    departure_time = DEP
    decided_at = DECIDED
    impact_probability = 0.91
    cancel_probability = 0.62
    ge10_probability = 0.44
    delay_p50_min = 3
    delay_p90_min = 9
    alert_threshold = 0.65
    alertworthy = True
    latest_observation = None
    alternative_journey = None
    impact = FakeImpactResponse()


JOURNEY = {"from_stop": "NSR:StopPlace:337", "to_stop": "NSR:StopPlace:11"}


def _row(alert=None, **kw):
    base = {
        "journey_digest": "abc123",
        "n_devices": 2,
        "pushed": True,
        "message": "Toget kan bli forsinket.",
        "message_source": "llm",
    }
    base.update(kw)
    return decisions.build_row(JOURNEY, alert or FakeAlert(), **base)


# ---- what the model was shown -----------------------------------------------------------------
def test_the_observation_quoted_to_the_traveller_is_recorded():
    """The prompt tells the model to quote `siste_observasjon` with its timestamp, so a message can
    claim "kl. 17:40 var avgangen innstilt ved X". Checking that claim against what happened is the
    point of the log, and none of the existing columns carried it.
    """
    class Alert(FakeAlert):
        latest_observation = FakeObservation()

    row = _row(Alert())
    assert row["obs_cancelled"] is True
    assert row["obs_stop_name"] == "Rørvik hurtigbåtkai"
    assert row["obs_delayed_min"] == 7
    assert row["obs_observed_at"] == FakeObservation.observed_at.isoformat()


def test_an_observation_stop_falls_back_to_its_ref():
    """`at_stop_name` is filled from the NSR registry, which reads fail-open. A row that says
    nothing about where an observation was made is worse than one naming a stop ref.
    """
    class Obs(FakeObservation):
        at_stop_name = None

    class Alert(FakeAlert):
        latest_observation = Obs()

    assert _row(Alert())["obs_stop_name"] == "NSR:StopPlace:509"


def test_the_alternative_offered_is_recorded_flat():
    """`has_alternative` exists so "how often do we actually offer one?" is a GROUP BY. Walking legs
    are dropped from alt_lines the same way the payload drops them — a traveller reads "R11", not
    "foot, R11, foot".

    The three flags beside it exist because the offer is no longer necessarily a faster journey:
    whether it actually arrives first, whether it is itself in trouble, and whether our models ever
    scored it are three different qualifications on one recommendation, and a row that records the
    suggestion without them cannot say what was really advertised.
    """
    class Alert(FakeAlert):
        alternative_journey = FakeAlternative()

    row = _row(Alert())
    assert row["has_alternative"] is True
    assert row["alt_status"] == "offered"
    assert row["alt_minutes_saved"] == 11
    assert row["alt_departure_time"] == FakeAlternative.departure_time.isoformat()
    assert row["alt_arrival_time"] == FakeAlternative.arrival_time.isoformat()
    assert row["alt_lines"] == "R11"
    assert row["alt_arrives_before_original"] is True
    assert row["alt_at_risk"] is False


def test_whether_the_alternative_was_scored_reads_through_the_prediction():
    """`scored` lives on AlternativePrediction, not on AlternativeJourney.

    A flat `getattr(alt, "scored", None)` would type-check, pass any "is it recorded?" assertion
    that accepts None, and ship a column of permanent NULLs — which reads as "nothing scored it",
    the exact opposite of the truth for the rows where our own models did have the boarding call.
    So this asserts True, not "not missing".
    """
    class Alert(FakeAlert):
        alternative_journey = FakeAlternative()

    assert _row(Alert())["alt_scored"] is True


def test_the_recommended_trains_identity_is_recorded_so_the_advice_can_be_labelled():
    """Every other decision in this table gets a nightly verdict; this one could not.

    `data/queries/label_varsel_decisions.sql` joins realized ET outcomes on
    `(operator, jkey, call_date, stop_order)`, and the row carried that key for the DISRUPTED train
    only — so "take this instead" was the single traveller-facing claim in this repo that nothing
    could ever score. The values come from the boarding call `impact._score_candidate` resolved,
    read through the departure prediction the same way `alt_scored` is.

    The operating day is deliberately not a fourth column: `DATE(alt_departure_time, 'Europe/Oslo')`
    already is it.
    """
    class Alert(FakeAlert):
        alternative_journey = FakeAlternative()

    row = _row(Alert())
    assert row["alt_operator"] == "VYG"
    assert row["alt_jkey"] == "DatedServiceJourney:2_OSL-DRM_26-08-04"
    assert row["alt_stop_order"] == 3


def test_an_unscored_alternative_names_no_train_rather_than_guessing_one():
    """The ordinary case — the prediction cache only holds journeys a live situation touches — and
    NULL is the honest answer there, not a gap to be filled.

    The temptation is right there in the row beside it: `alt_lines` carries "R11", whose ref would
    yield an operator in one line of code. It would be the Journey Planner's codespace, which
    `impact._score_candidate` refuses to gate on for the same reason, and a join key that is two
    thirds real joins to nothing — producing rows that read as a train we could not find rather than
    as a train we never scored.
    """
    class Unscored(FakeAlternativePrediction):
        scored = False
        operator = None
        jkey = None
        stop_order = None

    class Unlabelled(FakeAlternative):
        departure_prediction = Unscored()

    class Alert(FakeAlert):
        alternative_journey = Unlabelled()

    row = _row(Alert())
    assert row["alt_scored"] is False
    assert row["alt_operator"] is None
    assert row["alt_jkey"] is None
    assert row["alt_stop_order"] is None
    assert row["alt_lines"] == "R11", "the line was recorded; the join key was not invented from it"


def test_an_alternative_that_arrives_later_is_recorded_as_it_is():
    """The fallback tier offers the best remaining way there, not a faster one.

    For a cancelled train that legitimately arrives after the timetable promised, so `minutes_saved`
    is signed. Clamping it at zero, or dropping the row, would erase precisely the case the new
    selection rule exists to serve — and would make the offer look better than it was.
    """
    class Slower(FakeAlternative):
        minutes_saved = -14
        arrives_before_original = False
        at_risk = True

    class Alert(FakeAlert):
        alternative_journey = Slower()

    row = _row(Alert())
    assert row["alt_minutes_saved"] == -14
    assert row["alt_arrives_before_original"] is False
    assert row["alt_at_risk"] is True
    assert row["alt_status"] == "offered"


def test_an_alternative_missing_the_new_fields_records_null_not_an_error():
    """This module reads the Alert by duck typing, on the poller thread, behind a push.

    A journey object from an older shape — or a renamed field — must cost the columns it describes
    and nothing else. Attribute access would raise here and take the whole cycle's decision log with
    it (it fails open, so the symptom would be silence, not a stack trace).
    """
    class Bare:
        departure_time = DEP + timedelta(minutes=9)
        arrival_time = DEP + timedelta(minutes=38)
        minutes_saved = 4
        legs = ()

    class Alert(FakeAlert):
        alternative_journey = Bare()

    row = _row(Alert())
    assert row["alt_arrives_before_original"] is None
    assert row["alt_at_risk"] is None
    assert row["alt_scored"] is None
    # Read through the same absent departure prediction: a nested attribute access would raise here.
    assert (row["alt_operator"], row["alt_jkey"], row["alt_stop_order"]) == (None, None, None)
    assert row["has_alternative"] is True


def test_no_alternative_is_recorded_as_false_not_null():
    """NULL would be ambiguous between "we looked and found nothing" and "this predates the column".

    That ambiguity is not hypothetical, and this pin is why it stays bounded. The 673 rows written
    before `_alternative_fields` existed (2026-08-05) carry NULL, and on 2026-08-06 a reader counted
    them as "no alternative offered" — producing a 0 % baseline for the old rule out of rows that
    had never been instrumented, and a two-day "jump" that never happened. Every row this function
    writes from here on says FALSE when it means FALSE, so the ambiguous population can only be the
    one that predates it.
    """
    assert _row()["has_alternative"] is False
    assert "alt_minutes_saved" not in _row()


# ---- what became of the lookup ------------------------------------------------------------------
def test_every_decision_says_what_became_of_the_alternative_lookup():
    """`alt_status` is written on every row, including the ~83 % that never reach the lookup gate.

    Those are most of the denominator: an offer rate computed without them counts only the decisions
    a lookup was issued for, and the 2026-08-06 selection change moves the numerator sharply. A
    figure whose denominator shifts underneath it measures the log, not the advice.
    """
    class Alert(FakeAlert):
        alertworthy = False
        impact_probability = 0.11

    assert _row(Alert())["alt_status"] == "not_worth_lookup"
    assert _row()["alt_status"] == "not_worth_lookup"


def test_a_lookup_that_found_nothing_is_not_a_lookup_that_never_ran():
    """The distinction the column exists for. Both rows carry no alternative and are identical in
    every other column, and only one of them is evidence about the selection rule.

    `alternative_subject` is set by `impact._best_remaining_journey` once it is past its early
    returns, so its presence is the honest marker that a query actually went out.
    """
    class Looked(FakeImpactResponse):
        alternative_subject = ("VYG", "DatedServiceJourney:1_OSL-DRM_26-08-04")

    class Alert(FakeAlert):
        impact = Looked()

    assert _row(Alert())["alt_status"] == "none_found"
    assert _row(Alert())["has_alternative"] is False


def test_staying_on_your_own_train_is_a_verdict_not_an_absence():
    """"Bli sittende" is a claim; "vi vet ikke" is not, and both leave the same empty column.

    `original_is_best` is only ever True when a real pool was compared against, so recording it
    separately is what keeps a deliberate silence out of the "nothing was found" bucket.
    """
    class StayPut(FakeImpactResponse):
        original_is_best = True
        alternative_subject = ("VYG", "DatedServiceJourney:1_OSL-DRM_26-08-04")

    class Alert(FakeAlert):
        impact = StayPut()

    assert _row(Alert())["alt_status"] == "suppressed"


def test_a_backfilled_alternative_is_read_off_the_alert_not_the_response():
    """`match_journey` back-fills one trip's lookup onto every alert it produced.

    So an alert whose worst reading came from a collapsed stop ref carries advice its OWN /impact
    response does not — reading the response instead recorded `has_alternative=true` beside a
    message input with no `alternativ_reise` (fixed 2026-08-05, and the same trap one column over).
    """
    class Alert(FakeAlert):
        alternative_journey = FakeAlternative()
        impact = FakeImpactResponse()  # no subject: this response issued no lookup

    row = _row(Alert())
    assert row["alt_status"] == "offered"
    assert row["alt_departure_time"] == FakeAlternative.departure_time.isoformat()


# ---- the row and the table have to agree ---------------------------------------------------------
_TYPES = "STRING|BOOL|INT64|FLOAT64|TIMESTAMP|DATE"


def _created_columns(table: str) -> set[str]:
    """Columns the CREATE TABLE statement for `table` defines, read from the DDL the app runs."""
    body = re.search(
        rf"CREATE TABLE IF NOT EXISTS `{table}`\s*\((.*?)\)\s*PARTITION BY",
        decisions._ALL_DDL_SQL,
        re.DOTALL,
    )
    assert body is not None, f"no CREATE TABLE found for {table}"
    return set(re.findall(rf"^\s*(\w+)\s+(?:{_TYPES})\b", body.group(1), re.MULTILINE))


def _added_columns(table: str) -> set[str]:
    """Columns the ALTER ... ADD COLUMN IF NOT EXISTS block adds to an existing `table`.

    Statements are separated on their line-initial keyword, NOT on ';': the descriptions in this
    file carry semicolons of their own ("Minutes late at that stop; null if not recorded."), which
    is why validate_varsel_sql.sh splits with a quote-aware tokenizer rather than a sed. The
    scoping matters — there is a second ADD COLUMN block, on the outcomes table.
    """
    added: set[str] = set()
    for statement in re.split(r"^(?=CREATE |ALTER |EXECUTE )", decisions._ALL_DDL_SQL, flags=re.MULTILINE):
        if statement.startswith(f"ALTER TABLE `{table}`") and "ADD COLUMN" in statement:
            # Anchored to the line and followed by a type, so the prose about ADD COLUMN in the
            # comments around these statements is not read as a column named "does".
            added.update(
                re.findall(
                    rf"^\s*ADD COLUMN IF NOT EXISTS\s+(\w+)\s+(?:{_TYPES})\b", statement, re.MULTILINE
                )
            )
    return added


def test_every_recorded_field_is_a_column_of_the_decision_table():
    """The silent failure this guards: `decisions.py` writes a column the DDL never added.

    `insert_rows_json` does not raise for it — it returns per-row errors — so the whole cycle's
    decisions vanish and the only trace is "N of M rows rejected by BigQuery" once a minute. Every
    field any branch of build_row can produce is checked, not just the ones a default alert carries.
    """
    class Alert(FakeAlert):
        latest_observation = FakeObservation()
        alternative_journey = FakeAlternative()

    recorded = set(_row(Alert())) | set(_row())
    assert {"alt_operator", "alt_jkey", "alt_stop_order"} <= recorded, (
        "the fixture alert must still exercise the branch that writes the recommendation's join "
        "key, or this guard silently stops covering those columns"
    )
    missing = recorded - _created_columns("disruptions.varsel_decisions")
    assert not missing, f"recorded but never created: {sorted(missing)}"


def test_a_new_column_reaches_the_environments_that_already_have_the_table():
    """CREATE TABLE IF NOT EXISTS does nothing where the table exists, so a column added only to the
    definition never appears in dev or prd — and every write is then rejected for it.

    Checked for the whole alternative block rather than the four new columns alone: the rule is the
    file's, not this change's.
    """
    created = _created_columns("disruptions.varsel_decisions")
    added = _added_columns("disruptions.varsel_decisions")
    alt_columns = {name for name in created if name.startswith("alt_") or name == "has_alternative"}
    assert alt_columns <= added, f"in the CREATE but not in any ALTER: {sorted(alt_columns - added)}"
    # The 2026-08-07 pair, pinned by name: dev's and prd's decision tables both already exist, so
    # these two reach a live environment through the ALTER block or not at all — and the symptom of
    # not at all is every row of every cycle rejected by BigQuery, logged once a minute and nowhere
    # else. "message_deterministic IS NOT NULL dates a post-change row" rests on this too.
    assert {"message_llm", "message_deterministic"} <= added
    assert added <= created, f"added but never created: {sorted(added - created)}"


def test_a_certainty_is_marked_as_one():
    """A decision on a call ET had already declared cancelled relayed a fact rather than predicting
    one, and both probabilities are 1.0 by construction. It is a real hit for the traveller and a
    gimme for the models, so the log has to be segmentable on it: unmarked, the two populations are
    indistinguishable afterwards and one precision figure is quoted for both (2026-08-05).
    """
    class Alert(FakeAlert):
        cancelled = True
        impact_probability = 1.0
        cancel_probability = 1.0

    assert _row(Alert())["et_cancelled"] is True


def test_an_ordinary_prediction_is_not_marked_a_certainty():
    """False, not NULL: "we predicted this" has to be distinguishable from "this row predates the
    column", or the whole precision figure has to be thrown away rather than segmented."""
    assert _row()["et_cancelled"] is False


def test_the_message_input_is_stored_verbatim():
    """Verbatim, not reconstructed: the row must stay faithful as build_message_input evolves."""
    row = _row(message_input='{"reise": {"fra": "Oslo S"}}')
    assert row["message_input"] == '{"reise": {"fra": "Oslo S"}}'


# ---- why the wording has the source it does ---------------------------------------------------
def test_message_provenance_is_recorded():
    row = _row(llm_provider="gemini", llm_model="gemini-3.5-flash", message_status="ok",
               prompt_version="6110a432")
    assert row["llm_provider"] == "gemini"
    assert row["llm_model"] == "gemini-3.5-flash"
    assert row["message_status"] == "ok"
    assert row["prompt_version"] == "6110a432"


def test_a_row_without_llm_context_is_still_valid():
    """The keyword arguments default to None so a caller with no LLM context — a test, a future
    surface — produces a valid row rather than inventing provenance.

    The two wording columns are in that contract too: a caller that passes neither still writes the
    columns, as NULLs, rather than a row BigQuery rejects for a missing field.
    """
    row = _row()
    assert row["message_input"] is None
    assert row["message_status"] is None
    assert (row["message_llm"], row["message_deterministic"]) == (None, None)
    assert row["decided_at"] == DECIDED.isoformat()


def test_both_wordings_are_recorded_beside_the_one_that_was_sent():
    """`message` answers "what did the traveller get"; these two answer "what was it chosen between".

    Separate questions since 2026-08-07, when VARSEL_LLM_ENABLED became a send-time selector instead
    of deciding whether a wording was written at all: a delivery-off deployment (prd) writes the
    model's text and pushes the template, so the row must hold both or the comparison the split
    exists for can only ever be made on dev's traffic.

    The row below is exactly that case, and it is what makes the R7 read work: "written, withheld by
    policy" is `message_source = 'deterministic' AND message_llm IS NOT NULL`, which no row could
    match while `message_source` was keyed off whether an LLM wording existed.
    """
    row = _row(
        message="Signalfeil\nTog kl 08:12: 62 % sjanse for innstilling",
        message_source="deterministic",
        message_llm="Toget ditt kan bli innstilt. Vurder å reise tidligere.",
        message_deterministic="Signalfeil\nTog kl 08:12: 62 % sjanse for innstilling",
    )
    assert row["message_llm"] == "Toget ditt kan bli innstilt. Vurder å reise tidligere."
    assert row["message"] == row["message_deterministic"]
    assert row["message_source"] == "deterministic", "the source describes the SEND, not the cache"


# ---- the columns the /historikk API must never serve -------------------------------------------
def test_the_row_still_carries_no_device_or_journey_id():
    """The privacy rule the table was designed around: a record of what was said about a train, not
    of where a person commutes. message_input does carry origin/destination/time — which is why the
    API redacts it, and why this check exists next to it.
    """
    row = _row(message_input='{"reise": {"fra": "Oslo S", "til": "Drammen"}}')
    assert "device_id" not in row
    assert "journey_id" not in row


# ---- the DDL is two halves, and only one of them can stop the log --------------------------------
class _FakeQueryJob:
    def __init__(self, fail: bool):
        self._fail = fail

    def result(self, timeout=None):
        if self._fail:
            raise RuntimeError(
                "Exceeded rate limits: too many table update operations for this table"
            )


class _HalfFailingClient:
    """A client that serves the structural DDL and fails the description refresh.

    Modelled on the real 2026-08-07 failure: BigQuery caps table-metadata updates at 5 per 10 s per
    table, and the SET OPTIONS block trips it while every CREATE/ADD COLUMN above succeeds.
    """

    def __init__(self):
        self.queries: list[str] = []

    def query(self, sql):
        self.queries.append(sql)
        return _FakeQueryJob(fail=sql.lstrip().startswith(decisions._BEST_EFFORT_MARKER))


def _with_client(client):
    decisions._bq_client = client
    decisions._table_ready = False
    decisions._ensure_failed_at = None


def test_the_ddl_splits_structure_from_descriptions():
    """The marker has to actually partition the file, or the split silently becomes a no-op."""
    assert decisions._BEST_EFFORT_MARKER in decisions._ALL_DDL_SQL
    assert "CREATE TABLE IF NOT EXISTS" in decisions._STRUCTURE_SQL
    assert "ALTER COLUMN" not in decisions._STRUCTURE_SQL, (
        "a SET OPTIONS statement above the marker still spends a metadata slot on the blocking half"
    )
    assert "ALTER COLUMN" in decisions._DESCRIPTIONS_SQL


def test_the_marker_appears_exactly_once():
    """`str.partition` splits on the FIRST occurrence, so a second marker would move the boundary
    and quietly drop structural statements into the forgiven half."""
    assert decisions._ALL_DDL_SQL.count(decisions._BEST_EFFORT_MARKER) == 1


def test_no_column_description_exceeds_bigquerys_1024_char_cap():
    """The 2026-08-07 prd outage, pinned — the second one this file has caused.

    BigQuery rejects a field description over 1024 characters, and `CREATE TABLE IF NOT EXISTS`
    validates the DDL even when the table already exists, so an over-long description 400s the
    structure job on EVERY startup and ensure_table then reports the table as not ready. Decisions
    stop being recorded in every environment at once, silently, while pushes keep going out.

    Prose in this file is load-bearing documentation and keeps growing, no .sql here is executed by
    any test, and CI has no GCP credentials — so nothing but this check stands between a wordsmithing
    edit and a dead decision log. It cost 1040 characters to find that out.
    """
    # Both forms carry the cap: the typed column in a CREATE body, and the ALTER COLUMN refresh
    # below the marker. The second only fails a forgiven job, but a description that can never be
    # applied is still a description nobody will ever read.
    patterns = (
        (
            r'(\w+)\s+(?:STRING|BOOL|INT64|FLOAT64|NUMERIC|TIMESTAMP|DATE)\s+'
            r'OPTIONS\s*\(\s*description\s*=\s*"((?:[^"\\]|\\.)*)"'
        ),
        r'ALTER COLUMN\s+(\w+)\s+SET OPTIONS\s*\(\s*description\s*=\s*"((?:[^"\\]|\\.)*)"',
    )
    found, over = 0, []
    for pattern in patterns:
        for name, desc in re.findall(pattern, decisions._ALL_DDL_SQL, re.DOTALL):
            found += 1
            if len(desc) > 1024:
                over.append((name, len(desc)))
    # Guard the guard: a regex that silently matches nothing would pass forever.
    assert found > 20, f"description scan matched only {found} columns — the pattern has drifted"
    assert not over, f"descriptions over BigQuery's 1024-char cap: {over}"


def test_a_failed_description_refresh_does_not_stop_the_log():
    """The 2026-08-07 outage, pinned.

    ensure_table used to send the whole file as ONE job, so the rate-limited SET OPTIONS block left
    _table_ready False and record() wrote nothing — a cosmetic description edit taking down the
    entire alert-decision log on the next deploy.
    """
    client = _HalfFailingClient()
    _with_client(client)
    try:
        assert decisions.ensure_table() is True, "structure succeeded; the table is usable"
        assert decisions._ensure_failed_at is None, "a description failure must not arm the backoff"
        assert len(client.queries) == 2, "structure and descriptions are separate jobs"
        assert not client.queries[0].lstrip().startswith(decisions._BEST_EFFORT_MARKER)
    finally:
        _with_client(None)


def test_a_failed_structural_ddl_still_stops_the_log():
    """The forgiveness is scoped to descriptions — a missing table must still block recording."""

    class _AllFailingClient(_HalfFailingClient):
        def query(self, sql):
            self.queries.append(sql)
            return _FakeQueryJob(fail=True)

    client = _AllFailingClient()
    _with_client(client)
    try:
        assert decisions.ensure_table() is False
        assert decisions._ensure_failed_at is not None, "a structural failure arms the backoff"
        assert len(client.queries) == 1, "the description half is not attempted after structure fails"
        assert decisions.record([{"situation_number": "x"}]) == 0
    finally:
        _with_client(None)


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} decision-row checks passed.")


if __name__ == "__main__":
    _run()
