"""Checks for the alert-decision row shape — no BigQuery, no network.

The decision log's promise is that a decision can be re-examined later: what we predicted, what we
said, and — since this module started recording it — everything the message writer was shown. These
checks pin the parts that cannot be recovered afterwards if a row omits them.

Run: uv run --with pydantic python test_decisions.py  (or under pytest)
"""

import os
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


class FakeAlternative:
    departure_time = DEP + timedelta(minutes=9)
    arrival_time = DEP + timedelta(minutes=38)
    minutes_saved = 11

    def __init__(self):
        self.legs = [FakeLeg(), FakeLeg(mode="foot", public_code=None, line=None)]


class FakeImpactResponse:
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
    """
    class Alert(FakeAlert):
        alternative_journey = FakeAlternative()

    row = _row(Alert())
    assert row["has_alternative"] is True
    assert row["alt_minutes_saved"] == 11
    assert row["alt_departure_time"] == FakeAlternative.departure_time.isoformat()
    assert row["alt_arrival_time"] == FakeAlternative.arrival_time.isoformat()
    assert row["alt_lines"] == "R11"


def test_no_alternative_is_recorded_as_false_not_null():
    """NULL would be ambiguous between "we looked and found nothing" and "this predates the column".
    The measured answer today is "effectively never", which only means something if absence is
    explicit.
    """
    assert _row()["has_alternative"] is False
    assert "alt_minutes_saved" not in _row()


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
    """
    row = _row()
    assert row["message_input"] is None
    assert row["message_status"] is None
    assert row["decided_at"] == DECIDED.isoformat()


# ---- the columns the /historikk API must never serve -------------------------------------------
def test_the_row_still_carries_no_device_or_journey_id():
    """The privacy rule the table was designed around: a record of what was said about a train, not
    of where a person commutes. message_input does carry origin/destination/time — which is why the
    API redacts it, and why this check exists next to it.
    """
    row = _row(message_input='{"reise": {"fra": "Oslo S", "til": "Drammen"}}')
    assert "device_id" not in row
    assert "journey_id" not in row


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
