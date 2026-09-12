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
    # BOTH halves of the description latch, for the reason `_reset_state` states one module over: a
    # test that let the descriptions land leaves `_descriptions_done` True, and the next test's
    # `ensure_table` then skips the second job entirely -- which reads exactly like the split having
    # stopped working rather than like pollution.
    decisions._descriptions_done = False
    decisions._descriptions_failed_at = None


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


def _decision_groups_view() -> str:
    """The grouping body, comments stripped.

    Since 2026-09-12 that body is a TABLE FUNCTION, `varsel_decision_groups_between(d_start, d_end)`,
    with `varsel_decision_groups` as a windowless view over it: a BigQuery view takes no parameters,
    and four of the six readers need their own window to prune the partitioned log with (R12). The
    function is the last statement but one above the best-effort marker
    (test_the_ddl_splits_structure_from_descriptions pins that the marker really partitions the
    file), so slicing from it to the end of the structural half is enough. Comments go because this
    body argues its case at length and every phrase the assertions below look for appears in the
    prose too.
    """
    sql = decisions._STRUCTURE_SQL
    start = sql.index("CREATE OR REPLACE TABLE FUNCTION `disruptions.varsel_decision_groups_between`")
    return "\n".join(
        ln for ln in sql[start:].splitlines() if not ln.strip().startswith("--")
    )


def test_the_grouping_body_exists_once_and_the_view_is_a_wrapper_over_it():
    """One body, six readers (R12, U4 step 3).

    The three /historikk queries pass their own window; the nightly labelling job,
    backfill_outcome_pushed_message.sql and message_wording_comparison.sql go through the view. What
    must never happen is a SECOND copy of the GROUP BY: the labelling job writes a verdict keyed on
    this grain and the API looks decisions up by it, so two hand-maintained copies would eventually
    disagree and the symptom is rows that never match their own verdict.

    The view's column set and the GROUP BY are deliberately unchanged by the move, because the
    registered `varsel-label-daily` scheduled query holds a COPY of `SELECT *` over this name in each
    environment -- changing its shape is an operator step in two projects, not an edit.
    """
    sql = decisions._ALL_DDL_SQL
    assert sql.count("GROUP BY journey_digest, operator, jkey, call_date, stop_order, situation_number") == 1
    body = _decision_groups_view()
    assert "WHERE DATE(decided_at) BETWEEN d_start AND d_end" in body, (
        "the function takes no partition bound, so every reader scans the whole decision log"
    )
    view = sql[sql.index("CREATE OR REPLACE VIEW `disruptions.varsel_decision_groups`") :]
    view = "\n".join(ln for ln in view.splitlines() if not ln.strip().startswith("--"))
    view = view[: view.index(";") + 1]
    assert "varsel_decision_groups_between" in view and "GROUP BY" not in view, (
        f"the view must be a thin wrapper over the function, not a second body: {view}"
    )


def test_the_historikk_queries_pass_a_window_to_the_table_function():
    """A reader that keeps using the windowless view prunes nothing, and nothing would say so: the
    page renders identically and only the bill moves. The cover is `call_date` +/- a day, derived
    from the measured lead range (-54 to +720 min) -- a too-narrow one drops cycles out of the
    aggregates and reads as a subject nobody ever alerted on, which is the failure mode
    meldinger_decisions.sql documents at length.
    """
    import pathlib

    here = pathlib.Path(__file__).parent
    for name in ("historikk_decisions.sql", "historikk_summary.sql", "historikk_filters.sql"):
        text = pathlib.Path(here / name).read_text()
        uncommented = "\n".join(
            ln for ln in text.splitlines() if not ln.strip().startswith("--")
        )
        assert "varsel_decision_groups_between" in uncommented, f"{name} still reads the view"
        assert "`disruptions.varsel_decision_groups`" not in uncommented, (
            f"{name} still reads the windowless view somewhere"
        )
        assert "DATE_SUB(@from_date, INTERVAL 1 DAY)" in uncommented, f"{name}: no lower cover"
        assert "DATE_ADD(@to_date, INTERVAL 1 DAY)" in uncommented, f"{name}: no upper cover"


def test_the_decision_log_drops_partitions_it_is_never_asked_about():
    """Unbounded, the table makes R12's bytes cap unsizeable: it would have to be chosen against a
    table that only ever grows. A year is more than twice the longest window anything asks for
    (historikk._MAX_WINDOW_DAYS is 180), and it has to reach an EXISTING table through the
    best-effort half -- where a rate limit is forgiven -- as well as a fresh one through the CREATE.
    """
    sql = decisions._ALL_DDL_SQL
    assert "partition_expiration_days=365" in decisions._STRUCTURE_SQL, "a fresh environment"
    assert "partition_expiration_days=365" in decisions._DESCRIPTIONS_SQL, (
        "dev and prd already have this table, and CREATE TABLE IF NOT EXISTS does nothing to it"
    )
    # Two SETTINGS of it (the third mention is the prose beside the ALTER): the two halves of the
    # file, and no third that could disagree with them.
    assert sql.count("partition_expiration_days=365") == 2


def test_the_quoted_wording_is_the_one_that_was_pushed():
    """/historikk quotes `message` as what the traveller was sent, so the view must pick the row
    that pushed it -- not merely the most recent row that had text.

    A text check, because nothing in this repo executes SQL. The two diverge constantly: a wording
    lives in varsel._messages only while its situation stays above the alert gate, so
    _reconcile_caches forgets it the tick the subject eases off and every later cycle records the
    template again. Measured on dev 2026-08-21, on 1 790 alerted subjects: 52 cards quoted the
    template under "Skrevet av modellen" though the push carried the model's prose, 85 quoted the
    reverse, and 603 more quoted the right KIND of wording from the wrong cycle -- different
    percentages, a different replacement train. 41 % of the page was text nobody received.
    """
    view = _decision_groups_view()
    aggs = re.findall(r"ARRAY_AGG\((.*?)\)\s*\[SAFE_OFFSET\(0\)\]", view, re.DOTALL)
    wording = [a for a in aggs if "message" in a]
    assert len(wording) == 2, (
        f"expected a pushed-row aggregate and a fallback, found {len(wording)}"
    )
    assert sum("pushed" in a for a in wording) == 1, (
        "neither half selects on `pushed`: the view is back to quoting the last wording produced"
    )
    # One aggregate per half carrying BOTH columns. Two separate aggregates "picking the same row"
    # is a promise the ORDER BY cannot keep, and the way it breaks is a card showing one cycle's
    # text under another cycle's attribution -- half of the defect this fixes, reintroduced.
    for agg in wording:
        assert "STRUCT(" in agg and "message_source" in agg, (
            "message and message_source must be chosen as one struct, not aggregated separately"
        )


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


# ---- one cycle's rows are several insert REQUESTS (R12, U4 step 1) -------------------------------
#
# The streaming API refuses a request over 10 MB or 50 000 rows, and until 2026-09-12 `record` sent
# whatever a cycle produced as one call. At today's ~1 500 rows on an evening cycle that is nowhere
# near either bound; at 100 000 commuters it is 30 000+ rows of ~1 KB, i.e. one refused request and
# the whole cycle's decisions lost — the log going quiet is exactly the silent loss the counter
# beside it exists to catch, and a refused request loses everything rather than a share.
class _BatchClient:
    """Records each insert REQUEST separately, so the chunking is observable.

    `reject` names request indices whose rows BigQuery rejects per row (the `errors` return, not an
    exception); `raise_on` names indices the call raises on. Both exist because the two failure
    modes have to stay scoped to their own chunk.
    """

    def __init__(self, reject=(), raise_on=()):
        self.batches: list[list[dict]] = []
        self.timeouts: list[float | None] = []
        # The retry policy each request was made under. Recorded because the library's default is
        # a TEN-MINUTE deadline and `timeout=` bounds only one attempt of it — see
        # `test_a_chunk_cannot_retry_for_ten_minutes_behind_the_per_attempt_ceiling`.
        self.retries: list[object] = []
        self._reject = set(reject)
        self._raise = set(raise_on)

    def query(self, sql):
        return _FakeQueryJob(fail=False)

    def insert_rows_json(self, table, rows, timeout=None, retry=None):
        index = len(self.batches)
        self.batches.append(list(rows))
        self.timeouts.append(timeout)
        self.retries.append(retry)
        if index in self._raise:
            raise RuntimeError("BigQuery is down")
        if index in self._reject:
            return [{"index": i, "errors": ["no such field"]} for i in range(len(rows))]
        return []


def _rows_of(count: int, approx_bytes: int) -> list[dict]:
    """`count` rows whose serialized size is about `approx_bytes` each."""
    filler = "x" * max(approx_bytes - 40, 1)
    return [{"situation_number": f"S{i}", "summary": filler} for i in range(count)]


def test_a_batch_too_large_for_one_request_is_split_on_bytes():
    """12 000 rows of ~1 KB is ~12 MB: over the API's 10 MB request limit, so it cannot go as one
    call however few rows it is. The BYTE bound is what binds here, and it binds first."""
    client = _BatchClient()
    _with_client(client)
    try:
        rows = _rows_of(12_000, 1024)
        assert decisions.record(rows) == 12_000
        assert len(client.batches) == 3, f"expected three requests, got {len(client.batches)}"
        assert sum(len(b) for b in client.batches) == 12_000, "every row was sent exactly once"
        for batch in client.batches:
            assert len(batch) <= decisions._MAX_ROWS_PER_INSERT
            assert sum(decisions._row_bytes(r) for r in batch) <= decisions._MAX_BYTES_PER_INSERT
        assert all(t == decisions._WRITE_TIMEOUT_S for t in client.timeouts), (
            "every request keeps the poller-thread ceiling, not just the first"
        )
    finally:
        _with_client(None)


def test_a_batch_of_small_rows_is_split_on_the_row_count():
    """60 000 rows of ~100 bytes is 6 MB — under the byte bound, over the row bound. Both bounds
    are needed: either one alone lets a real cycle through that the API refuses."""
    client = _BatchClient()
    _with_client(client)
    try:
        assert decisions.record(_rows_of(60_000, 100)) == 60_000
        assert len(client.batches) == 6, f"expected six requests, got {len(client.batches)}"
        assert all(len(b) == decisions._MAX_ROWS_PER_INSERT for b in client.batches)
    finally:
        _with_client(None)


def test_one_cycle_of_todays_size_is_still_one_request():
    """The split must not turn an ordinary evening cycle (1 100-1 500 rows in prd) into several
    requests: that would multiply the counter and the log line with nothing having changed."""
    client = _BatchClient()
    _with_client(client)
    try:
        assert decisions.record(_rows_of(1_500, 1024)) == 1_500
        assert len(client.batches) == 1
    finally:
        _with_client(None)


def test_a_rejected_chunk_drops_only_its_own_rows():
    """The whole point of chunking: one refused request must not cost the cycle's other rows.

    Both failure modes, because they arrive differently — `insert_rows_json` RETURNS per-row errors
    for a schema drift and RAISES for an outage — and a chunk loop that only guards one of them
    loses the rest of the cycle to the other.
    """
    client = _BatchClient(reject={1})
    _with_client(client)
    try:
        accepted = decisions.record(_rows_of(60_000, 100))
        assert accepted == 50_000, f"only the rejected chunk's rows are lost, got {accepted}"
        assert len(client.batches) == 6, "the chunks after the rejected one were still sent"
    finally:
        _with_client(None)

    client = _BatchClient(raise_on={0})
    _with_client(client)
    try:
        accepted = decisions.record(_rows_of(60_000, 100))
        assert accepted == 50_000, f"only the raising chunk's rows are lost, got {accepted}"
        assert len(client.batches) == 6, "a raising FIRST chunk did not abandon the rest"
    finally:
        _with_client(None)


def test_a_row_larger_than_one_request_still_goes_out_alone():
    """A single oversized row must not wedge the loop or produce an empty request. It will be
    refused by BigQuery and counted, which is the honest outcome — silently dropping it here would
    make the loss invisible, and looping forever would take the poller thread with it."""
    client = _BatchClient()
    _with_client(client)
    try:
        rows = [*_rows_of(1, 100), *_rows_of(1, decisions._MAX_BYTES_PER_INSERT + 1000)]
        assert decisions.record(rows) == 2
        assert [len(b) for b in client.batches] == [1, 1]
    finally:
        _with_client(None)


class _CapturingLogger:
    """Collects the `extra={"fields": ...}` payloads a module logs, and nothing else."""

    def __init__(self):
        self.fields: list[dict] = []

    def _record(self, *_args, **kw):
        self.fields.append(dict((kw.get("extra") or {}).get("fields") or {}))

    warning = info = error = debug = _record


def test_the_failed_write_is_logged_with_its_event_field():
    """The exception path logged without `event`, so the ONE failure that loses a whole request was
    the one a query on `event="varsel_decision_write"` could not find. Every other branch of this
    write — partial, ok, stale descriptions — carries the field; the losing branch did not."""
    client = _BatchClient(raise_on={0})
    captured = _CapturingLogger()
    before = decisions.logger
    decisions.logger = captured
    _with_client(client)
    try:
        assert decisions.record(_rows_of(3, 100)) == 0
    finally:
        _with_client(None)
        decisions.logger = before
    assert any(
        f.get("event") == "varsel_decision_write" and f.get("result") == "error"
        for f in captured.fields
    ), f"no structured error record: {captured.fields}"
    assert any("chunk" in f for f in captured.fields), "the record names which chunk was lost"


# ---- a chunked write is bounded, and beats the poller's liveness seam (U4 review, 2026-09-12) ----
#
# Chunking introduced a second silence the heartbeat work (U2) had just removed. `timeout=` bounds
# ONE HTTP attempt; the call runs under the client's DEFAULT_RETRY, whose deadline is ten minutes —
# so several sequential chunks are several ten-minute waits with no progress signal, inside the
# single `varsel_notify` phase, against a `HEALTH_STALE_AFTER_S` of 600 s whose restart comes up
# with both ET planned-call tables cold.


class _Beats:
    """Counts heartbeats, and can fail on demand — the seam must never cost the write."""

    def __init__(self, raises=False):
        self.count = 0
        self._raises = raises

    def __call__(self):
        self.count += 1
        if self._raises:
            raise RuntimeError("the liveness seam is having a day")


def test_the_write_beats_the_heartbeat_once_per_chunk():
    """A chunk boundary is a progress point in exactly /health's sense: the staleness rule means
    "no STEP has finished", and a cycle's rows are now several steps. Without a beat between them
    a big cycle reads as a wedged poller — and the restart is into a cold start, which is the one
    failure mode the beat exists to prevent."""
    before = decisions._MAX_ROWS_PER_INSERT
    client = _BatchClient()
    beats = _Beats()
    _with_client(client)
    decisions._MAX_ROWS_PER_INSERT = 1
    try:
        assert decisions.record(_rows_of(3, 100), beat=beats) == 3
        assert len(client.batches) == 3, "control: the rows really did go as three requests"
        assert beats.count == 3, (
            f"expected one beat per chunk, got {beats.count} for {len(client.batches)} requests"
        )
    finally:
        decisions._MAX_ROWS_PER_INSERT = before
        _with_client(None)


def test_a_chunk_that_failed_still_beats_because_the_thread_is_still_moving():
    """The beat is in the loop body, not on the success path. A chunk that failed SLOWLY is the
    most interesting reading there is and it is still evidence the poller is alive — the same rule
    `main._phase` follows by beating from its `finally`."""
    before = decisions._MAX_ROWS_PER_INSERT
    client = _BatchClient(raise_on={0, 1})
    beats = _Beats()
    _with_client(client)
    decisions._MAX_ROWS_PER_INSERT = 1
    try:
        assert decisions.record(_rows_of(2, 100), beat=beats) == 0
        assert beats.count == 2, f"a failing chunk skipped its beat: {beats.count}"
    finally:
        decisions._MAX_ROWS_PER_INSERT = before
        _with_client(None)


def test_a_raising_heartbeat_never_costs_the_cycles_rows():
    """`record` promises never to raise and its caller's ledger rollback depends on that promise:
    a beat that escaped would unwind `varsel._record_decisions` before the rollback ran, losing the
    very rows the return value exists to report. The seam is guarded here as well as inside it."""
    before = decisions._MAX_ROWS_PER_INSERT
    client = _BatchClient()
    beats = _Beats(raises=True)
    captured = _CapturingLogger()
    logger_before = decisions.logger
    decisions.logger = captured
    _with_client(client)
    decisions._MAX_ROWS_PER_INSERT = 1
    try:
        assert decisions.record(_rows_of(2, 100), beat=beats) == 2, (
            "a broken liveness seam must not lose rows that BigQuery accepted"
        )
        assert beats.count == 2
    finally:
        decisions._MAX_ROWS_PER_INSERT = before
        _with_client(None)
        decisions.logger = logger_before
    assert any(f.get("result") == "beat_failed" for f in captured.fields), captured.fields


def test_a_write_with_no_beat_wired_behaves_exactly_as_it_did():
    """Absent is a no-op, like every other seam in this package: a deployment that wired nothing
    (and every suite that calls `record` directly) must be byte-for-byte what it was."""
    client = _BatchClient()
    _with_client(client)
    try:
        assert decisions.record(_rows_of(3, 100)) == 3
    finally:
        _with_client(None)


def test_a_chunk_cannot_retry_for_ten_minutes_behind_the_per_attempt_ceiling():
    """`timeout=` bounds one HTTP attempt; the library's DEFAULT_RETRY bounds the CALL at ten
    minutes, and `insert_rows_json`'s signature defaults to it. So the 60 s ceiling beside it was
    never the ceiling — several chunks could each spend ten minutes, on the poller thread.

    Only the DEADLINE is narrowed: the predicate stays the library's, so exactly the same errors are
    retried. The last assertion is what keeps this from going vacuous — if the guarded import ever
    stops resolving, `_WRITE_RETRY` is None and every other check here still passes."""
    assert decisions._BQ_DEFAULT_RETRY is not None, (
        "google-cloud-bigquery is installed in CI, so the fallback must not be what is under test"
    )
    assert decisions._BQ_DEFAULT_RETRY.timeout > decisions._WRITE_TIMEOUT_S, (
        "control: the library default is what needed narrowing"
    )
    assert decisions._WRITE_RETRY is not None
    assert decisions._WRITE_RETRY.timeout == decisions._WRITE_TIMEOUT_S
    assert decisions._WRITE_RETRY._predicate is decisions._BQ_DEFAULT_RETRY._predicate, (
        "narrowing the deadline must not change WHICH errors are retried"
    )

    client = _BatchClient()
    _with_client(client)
    try:
        assert decisions.record(_rows_of(3, 100)) == 3
        assert client.retries == [decisions._WRITE_RETRY], (
            "every request carries the narrowed policy, not the library's ten-minute default"
        )
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
