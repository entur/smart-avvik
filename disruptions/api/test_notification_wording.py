"""Checks for the shared notification wording and its two per-surface policies (U1, KTD1/KTD2).

Runs standalone (`uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery
--with google-cloud-storage --with pywebpush python test_notification_wording.py`) or under
pytest -- it imports `varsel` to prove the aliases are the shared functions, which pulls FastAPI.
Hermetic: no Vertex, no network, no BigQuery — the backends are stubbed and the budgets are reset
between checks.

The suite is in two halves. The first is a MOVE check: the wording came out of `varsel` unchanged,
so every golden string here was read off the pre-extraction module and a diff in one of them is a
different notification on a real traveller's lock screen, never a test to update. The second is the
new surface: a partner wording is produced with no VAPID keypair and no model at all, its model
calls are charged to their own allowance and latched on their own holder, and neither can be
reached from — or reach into — the browser push path.

Section headers name U1's own test scenarios. Scenario 1 (byte-identical wording either way) is
covered three ways and the docstring on
:func:`test_varsel_reads_this_module_rather_than_a_copy_of_it` says where.
"""

import inspect
import json
import logging
import os
import pathlib
from datetime import UTC, datetime, timedelta

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "ent-kixavvik-test")

import llm_message
import notification_wording as nw
from llm_message import _ConfigurationError

DEP = datetime(2026, 7, 6, 6, 0, tzinfo=UTC)  # 08:00 Europe/Oslo

ROUTE = nw.Route(from_stop="NSR:StopPlace:A", to_stop="NSR:StopPlace:B")


def _reset(*, provider="claude", partner_delivery=True):
    llm_message._stop_name_provider = None
    llm_message._PROVIDER = provider
    llm_message._ENABLED = True
    llm_message._PROJECT = "ent-kixavvik-test"
    llm_message._STATE = llm_message._LatchState()
    nw.reset_state()
    nw._PARTNER_DELIVERY_ENABLED = partner_delivery


def _facts(**kw):
    base = {
        "situation_number": "S1",
        "summary": "Signalfeil",
        "operator": "VYG",
        "line": "VYG:Line:R10",
        "departure_time": DEP,
        "cancel_probability": 0.1,
        "delay_p50_min": 3,
        "delay_p90_min": 9,
        "impact_probability": 0.93,
    }
    base.update(kw)
    return nw.DepartureWordingFacts(**base)


# ---- half one: the wording is the SAME wording -------------------------------------------------
def test_the_template_bytes_are_the_ones_varsel_was_sending():
    """Golden strings, read off the module before the extraction.

    The whole unit is a move, so this is the check that matters most: a diff here is a different
    notification on a real lock screen. Every branch the body has is covered — the certainty, the
    cancellation percentage, the two shapes of delay interval, «i morgen», the no-SX headline, and
    each way a part can be missing.
    """
    cases = {
        "Signalfeil\nTog R10 08:00: Innstilt": {
            "transportmiddel": "Tog", "linje": "R10", "avgang_kl": "08:00",
            "innstilt": True, "situasjoner": [{"sammendrag": "Signalfeil"}],
        },
        "Signalfeil\nTog R10 08:00: 91 % sjanse for innstilling": {
            "transportmiddel": "Tog", "linje": "R10", "avgang_kl": "08:00",
            "innstilling_prosent": 91, "situasjoner": [{"sammendrag": "Signalfeil"}],
        },
        "Signalfeil\nTog R10 08:00: Typisk 3–9 min forsinket": {
            "transportmiddel": "Tog", "linje": "R10", "avgang_kl": "08:00",
            "forsinkelse_min": {"typisk": 3, "verstefall": 9},
            "situasjoner": [{"sammendrag": "Signalfeil"}],
        },
        "Signalfeil\nTog R10 08:00: Typisk 9 min forsinket": {
            "transportmiddel": "Tog", "linje": "R10", "avgang_kl": "08:00",
            "forsinkelse_min": {"typisk": 9, "verstefall": 9},
            "situasjoner": [{"sammendrag": "Signalfeil"}],
        },
        "Signalfeil\nTog R10 07:18 i morgen: Innstilt": {
            "transportmiddel": "Tog", "linje": "R10", "avgang_kl": "07:18", "i_morgen": True,
            "innstilt": True, "situasjoner": [{"sammendrag": "Signalfeil"}],
        },
        "Mulig avvik\nTog R10 08:00: Typisk 5–20 min forsinket": {
            "transportmiddel": "Tog", "linje": "R10", "avgang_kl": "08:00",
            "ingen_melding": True, "situasjoner": [],
            "forsinkelse_min": {"typisk": 5, "verstefall": 20},
        },
        "Signalfeil\nTog kl 08:00: Innstilt": {
            "transportmiddel": "Tog", "avgang_kl": "08:00", "innstilt": True,
            "situasjoner": [{"sammendrag": "Signalfeil"}],
        },
        "Signalfeil\nTog R10 08:00": {
            "transportmiddel": "Tog", "linje": "R10", "avgang_kl": "08:00",
            "situasjoner": [{"sammendrag": "Signalfeil"}],
        },
        "Signalfeil\nTypisk 5–20 min forsinket": {
            "transportmiddel": "Tog", "situasjoner": [{"sammendrag": "Signalfeil"}],
            "forsinkelse_min": {"typisk": 5, "verstefall": 20},
        },
        "": {},
    }
    for expected, message_input in cases.items():
        assert nw.deterministic_body(message_input) == expected, message_input


def test_the_alternative_clause_is_the_one_varsel_was_sending():
    """Its caveats are the load-bearing part: prd shipped this template, so a caveat that reaches
    only the prompt reaches nobody."""
    base = {
        "transportmiddel": "Tog", "linje": "R10", "avgang_kl": "08:00", "innstilt": True,
        "situasjoner": [],
    }
    alt = {
        "avgang_kl": "08:24", "ankomst_kl": "09:05", "ankommer_i_tide": True,
        "egen_risiko": False, "reisevei": [{"transportmiddel": "rail", "linje": "R11"}],
    }
    def body(extra):
        return nw.deterministic_body({**base, "alternativ_reise": {**alt, **extra}})

    assert body({}) == "Tog R10 08:00: Innstilt\nAlternativ tog R11 08:24, framme kl 09:05"
    assert body({"ankommer_i_tide": False, "minutter_senere": 34}).endswith(
        "– 34 min senere enn planlagt ankomst"
    )
    assert body({"ankommer_i_tide": False}).endswith("– senere enn planlagt ankomst")
    assert body({"egen_risiko": True}).endswith("(kan også bli rammet)")
    assert body({"reisevei": []}).endswith("Alternativ kl 08:24, framme kl 09:05")
    assert body({"reisevei": [{"transportmiddel": "funicular", "linje": "F1"}]}).endswith(
        "Alternativ F1 08:24, framme kl 09:05"
    ), "a mode we have no noun for costs the noun and nothing else"
    assert body({"avgang_kl": None}).endswith("Alternativ framme kl 09:05")
    assert nw.deterministic_body({**base, "alternativ_reise": {}}) == "Tog R10 08:00: Innstilt"
    # `ankommer_i_tide` is read as `is False`, never falsily: an input predating the key says
    # nothing about which side of the deadline this lands on.
    assert "senere enn planlagt" not in nw.deterministic_body(
        {**base, "alternativ_reise": {k: v for k, v in alt.items() if k != "ankommer_i_tide"}}
    )


def test_the_title_bytes_are_the_ones_varsel_was_sending():
    """The lock-screen title states the FACT about the train; the route is only the fallback."""
    train = {"transportmiddel": "Tog", "linje": "R10", "avgang_kl": "08:00"}
    assert nw.notification_title({**train, "innstilt": True}, "A", "B") == "Tog R10 08:00 er innstilt"
    assert nw.notification_title({**train, "innstilt": True}, "A", "B", confirmed=True) == (
        "Bekreftet: Tog R10 08:00 er innstilt"
    )
    assert nw.notification_title({**train, "innstilling_prosent": 91}, "A", "B") == (
        "Tog R10 08:00 kan bli innstilt"
    )
    assert nw.notification_title(
        {**train, "forsinkelse_min": {"typisk": 3, "verstefall": 9}}, "A", "B"
    ) == "Tog R10 08:00 kan bli forsinket"
    assert nw.notification_title(train, "A", "B") == "Tog R10 08:00: mulig avvik"
    assert nw.notification_title({}, "Oslo S", "Lillehammer") == "Oslo S → Lillehammer"
    # «Bekreftet:» is earned only by a cancellation -- a second forecast is an update, not a
    # confirmation of anything.
    assert nw.notification_title(
        {**train, "innstilling_prosent": 91}, "A", "B", confirmed=True
    ) == "Tog R10 08:00 kan bli innstilt"


def test_varsel_reads_this_module_rather_than_a_copy_of_it():
    """Scenario 1: one implementation, not two that agree today.

    `main` injects `varsel._deterministic_body` into `/meldinger` and two suites assert push bodies
    against it, so the alias has to BE this function rather than resemble it — otherwise the two
    could drift and every wording comparison in the decision log would be measuring the drift.

    Identity is the strongest form scenario 1 can take: two names for one object cannot yield
    different bytes for one `message_input`, so there is nothing left to compare. The bytes
    themselves are pinned by the two golden-string checks above, read off the module BEFORE the
    extraction, and the assembled push is pinned end to end by
    `test_varsel_api.test_delivery_off_still_writes_the_wording_but_pushes_the_template`, which
    asserts the body a stubbed sender received against `varsel._deterministic_body` over the very
    object the writer was prompted with — that is "`varsel`'s old path" in full.
    """
    import varsel

    assert varsel._deterministic_body is nw.deterministic_body
    assert varsel._notification_title is nw.notification_title
    assert varsel._train_label is nw.train_label
    assert varsel._delay_phrase is nw.delay_phrase
    assert varsel.mode_label is nw.mode_label


def test_the_vehicle_noun_is_derived_from_the_codespace():
    assert [nw.mode_label(o) for o in ("VYG", "NSB", "GOA", "SJN", "FLT")] == ["Tog"] * 5
    assert [nw.mode_label(o) for o in ("RUT", "KOL", None, "")] == ["Buss"] * 4


def test_the_leg_noun_is_lower_case_because_it_lands_mid_clause():
    """«Alternativ tog R11 08:24» is what bokmål asks for; the alert's own label keeps its capital
    because it heads its line. Case belongs at the one call site whose position is fixed."""
    assert nw._LEG_MODE_NOUN["rail"] == "tog"
    assert nw._LEG_MODE_NOUN["metro"] == "T-bane", "an abbreviation, not a common noun"
    assert nw.train_label("Tog", "R10", "08:00") == "Tog R10 08:00"
    assert nw.train_label("Tog", None, "08:00") == "Tog kl 08:00"
    assert nw.train_label(None, "R10", "08:00") == "R10 08:00"
    assert nw.train_label(None, None, "08:00") == "kl 08:00"


# ---- the typed message input --------------------------------------------------------------------
def test_the_typed_facts_produce_the_one_shared_message_input_document():
    """One builder, and it must stay one: `/meldinger` replays stored documents and asserts
    `message_input_json(json.loads(s)) == s`, so a second builder emitting the same keys in a
    different order breaks replay silently rather than loudly."""
    _reset()
    payload = nw.build_message_input(ROUTE, _facts(), now=DEP - timedelta(hours=1))
    assert payload["avgang_kl"] == "08:00"
    assert payload["linje"] == "R10"
    assert payload["transportmiddel"] == "Tog"
    assert payload["situasjoner"] == [{"sammendrag": "Signalfeil", "beskrivelse": None}]
    assert payload["forsinkelse_min"] == {"typisk": 3, "verstefall": 9}
    serialized = llm_message.message_input_json(payload)
    assert llm_message.message_input_json(json.loads(serialized)) == serialized


def test_a_declared_cancellation_carries_no_probability_and_no_delay():
    """The 2026-08-05 rule travels with the document, not with the surface: a certainty replaces
    every conditional figure rather than sitting beside it."""
    _reset()
    payload = nw.build_message_input(ROUTE, _facts(cancelled=True, cancel_probability=1.0))
    assert payload["innstilt"] is True
    for key in ("forsinkelse_min", "innstilling_prosent", "sannsynlighet_prosent"):
        assert key not in payload
    assert nw.deterministic_body(payload).endswith("Innstilt")


def test_a_no_sx_departure_never_quotes_our_own_placeholder_as_an_operator():
    """`situasjoner` is documented to the model as the OPERATOR'S words. The no-SX path fills the
    summary with text this codebase wrote about itself, and leaving it there would have a writer
    relay our placeholder as though an operator had published it."""
    _reset()
    payload = nw.build_message_input(
        ROUTE, _facts(has_situation=False, summary="Ingen situasjonsmelding er publisert")
    )
    assert payload["ingen_melding"] is True
    assert payload["situasjoner"] == []
    assert nw.deterministic_body(payload).startswith("Mulig avvik")


# ---- half two: the partner surface ---------------------------------------------------------------
def test_a_partner_wording_needs_no_vapid_keypair_and_no_model():
    """KTD4's flat requirement: a partner wording does not depend on a VAPID keypair.

    A deployment can hold no VAPID keypair at all — `varsel.push_enabled()` False, no browser
    delivery attempted — and a partner event still has to carry a ready-to-display Norwegian title
    and body (R7). So does a deployment whose model backend is unreachable. The template is free
    and always available, which is what makes both true rather than aspirational.

    "`check_and_notify` a no-op" is what this said when it was written, and U2 (2026-09-08) made it
    false in the direction this test wanted: that pass now matches, writes and records for a
    recipient on any channel, and only browser DELIVERY depends on the keypair.
    """
    import varsel

    previous = (varsel._VAPID_PRIVATE_KEY, varsel._VAPID_PUBLIC_KEY)
    varsel._VAPID_PRIVATE_KEY = varsel._VAPID_PUBLIC_KEY = None
    _reset(provider="off")
    try:
        assert varsel.push_enabled() is False
        wording = nw.write_wording(ROUTE, _facts(cancelled=True), subject_id="dep_abc")
    finally:
        varsel._VAPID_PRIVATE_KEY, varsel._VAPID_PUBLIC_KEY = previous
    assert wording.title == "Tog R10 08:00 er innstilt"
    assert wording.body.endswith("Innstilt")
    assert wording.source == "deterministic"


def test_the_egress_kill_switch_reaches_the_partner_path():
    """`VARSEL_LLM_PROVIDER=off` is the switch an operator reaches for when traveller data must
    stop leaving for Vertex. It is process-wide by design, and a surface that could opt out of it
    would make it a lie — so it is checked here rather than assumed from the shared module."""
    _reset(provider="off")
    called = []
    llm_message._BACKENDS["claude"] = lambda body: called.append(body) or '{"melding":"x"}'
    try:
        wording = nw.write_wording(ROUTE, _facts(), subject_id="dep_abc")
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert called == [], "no backend may be asked"
    assert wording.source == "deterministic" and wording.status == "disabled"
    assert nw.spent(nw._PARTNER_PATH) == (0, 0), (
        "the kill switch must not burn the allowance -- otherwise it reports `rate_limited` after "
        "500 calls it never made, which is a misleading diagnosis of the control an operator "
        "reached for in an incident"
    )


def test_the_partner_delivery_switch_is_its_own_and_defaults_off():
    """The rollout note is explicit: deterministic wording first, model wording enabled separately
    after matching and delivery qualify. Sharing `VARSEL_LLM_ENABLED` would turn model prose on for
    partners in the same deploy that turns it on for the PWA."""
    source = inspect.getsource(nw)
    assert 'os.environ.get("NOTIFICATION_PARTNER_LLM_ENABLED", "false")' in source
    assert nw.PARTNER.deliver is nw.partner_delivery_enabled
    assert nw.BROWSER.deliver is not nw.partner_delivery_enabled
    _reset(partner_delivery=False)
    called = []
    llm_message._BACKENDS["claude"] = lambda body: called.append(body) or '{"melding":"x"}'
    try:
        wording = nw.write_wording(ROUTE, _facts(), subject_id="dep_abc")
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert called == [], "delivery off means the model is not asked at all on this surface"
    assert wording.source == "deterministic" and wording.status == "not_delivered"


def test_a_model_wording_replaces_the_body_and_never_the_title():
    """The title is the fact about the train and is ours; only the body is written.

    A model-written title would be an unbounded string on the surface a lock screen is scanned by,
    and it would break the «Bekreftet:» replacement, which is keyed on the title's own shape.
    """
    _reset()
    llm_message._BACKENDS["claude"] = lambda body: '{"melding":"Toget er innstilt."}'
    try:
        wording = nw.write_wording(ROUTE, _facts(cancelled=True), subject_id="dep_abc")
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert wording.source == "llm" and wording.body == "Toget er innstilt."
    assert wording.title == "Tog R10 08:00 er innstilt"


def test_every_model_failure_falls_back_to_the_template():
    """A notification is never lost to a wording problem — every path here has a free answer."""
    _reset()

    def raiser(_body):
        raise RuntimeError("Vertex is unreachable")

    for backend, expected in (
        (raiser, "error"),
        (lambda _b: "", "rejected"),
        (lambda _b: '{"melding":"Det er 47 % sjanse for innstilling."}', "ungrounded"),
    ):
        _reset()
        llm_message._BACKENDS["claude"] = backend
        try:
            wording = nw.write_wording(ROUTE, _facts(), subject_id="dep_abc")
        finally:
            llm_message._BACKENDS["claude"] = llm_message._claude_write
        assert wording.source == "deterministic", expected
        assert wording.status == expected
        assert wording.body == nw.deterministic_body(wording.message_input)


# ---- scenarios 3 and 4: the partner allowance and latch are its own ----------------------------
def test_a_partner_pass_is_bounded_and_the_bound_is_reported_not_hidden():
    """A busy morning must not spend the surface's whole hourly allowance in one sweep, and a
    bounded subject must say so — a template wording with no explanation is indistinguishable from
    a deployment that does not deliver model prose at all."""
    _reset()
    llm_message._BACKENDS["claude"] = lambda body: '{"melding":"Toget er innstilt."}'
    try:
        budget = nw.PassBudget(limit=2)
        statuses = [
            nw.write_wording(ROUTE, _facts(cancelled=True), subject_id=f"dep_{i}", budget=budget).status
            for i in range(4)
        ]
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert statuses == ["ok", "ok", "capped", "capped"]
    assert nw.spent(nw._PARTNER_PATH) == (2, 2), "a capped subject spends no allowance"


def test_partner_exhaustion_cannot_consume_the_browser_allowance():
    """Scenario 3, the arithmetic half.

    The ledger is keyed by caller in the shape `alternatives._fetch_calls` and
    `meldinger._replay_calls` already use. The browser push path does not call this module at all,
    so its slot stays empty BY CONSTRUCTION — which is the point of the keying: it makes that a
    structural property rather than a fact somebody has to keep true, and it stops a later caller
    silently drawing the partner allowance down or being drawn down by it.
    """
    _reset()
    llm_message._BACKENDS["claude"] = lambda body: '{"melding":"Toget er innstilt."}'
    try:
        for i in range(5):
            nw.write_wording(ROUTE, _facts(cancelled=True), subject_id=f"dep_{i}")
        assert nw.spent(nw._PARTNER_PATH) == (5, 5)
        assert nw.spent(nw._BROWSER_PATH) == (0, 0)
        # ...and once the hour is full the partner surface degrades to the template rather than
        # borrowing from anywhere.
        nw._calls[nw._PARTNER_PATH][0].extend([nw.monotonic()] * nw._MAX_CALLS_PER_HOUR)
        wording = nw.write_wording(ROUTE, _facts(cancelled=True), subject_id="dep_last")
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert wording.status == "rate_limited" and wording.source == "deterministic"
    assert nw.spent(nw._BROWSER_PATH) == (0, 0)


def test_partner_exhaustion_cannot_disable_the_pollers_own_backend():
    """Scenarios 3 and 4, the latch half — and the one whose failure is silent on both sides.

    A partner burst that drives every backend into a configuration error must not make
    `generation_enabled()` read False on the push path for the next thirty minutes: every real
    traveller's push would fall back to the template and the poller would spend a message attempt
    per subject on each `None`, with nothing traveller-facing reporting it.
    """
    _reset()

    def unserved(_body):
        raise _ConfigurationError("model not enabled in this project")

    llm_message._BACKENDS["claude"] = unserved
    try:
        for i in range(3):
            assert nw.write_wording(ROUTE, _facts(), subject_id=f"dep_{i}").source == "deterministic"
        assert llm_message.generation_enabled(nw.PARTNER.state()) is False
        assert llm_message.generation_enabled() is True, "the push path is untouched"
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert nw.PARTNER.state() is not llm_message._STATE
    assert nw.BROWSER.state() is None, "the browser surface works against the module-global holder"


def _code_names(source: str) -> set[str]:
    """Every identifier some Python SOURCE uses, with comments and string literals removed.

    A plain substring search over the text would flag the paragraphs that explain why a name is
    absent — and the explanation is worth more than the check, so the check is what adapts. What
    matters is what the code can *reach*, which is exactly its identifiers. That also makes the two
    checks below self-consistent with their own prose: this file names `is_material` in a docstring
    and in a test name, and neither is a name the tree can call.

    Takes source TEXT rather than a module, because scenario 5 reads files this process must not
    import: `main` constructs a BigQuery client at import unless it is stubbed first, and importing
    every module in the directory to inspect it would be a far larger claim than the check needs.
    """
    import io
    import tokenize

    names: set[str] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME:
            names.add(token.string)
    return names


def test_a_partner_call_writes_no_public_audit_data():
    """R16, structurally: this module cannot reach the writers that publish.

    `varsel_decisions` feeds `/historikk` and `/meldinger`, feedback contexts feed the review
    exporter, and all three are public or semi-public surfaces. A partner's input and prose must
    not reach any of them, and the way to guarantee that is for the module to be unable to name the
    writers — checked over the module's identifiers, so the paragraphs explaining the rule do not
    trip it.
    """
    names = _code_names(inspect.getsource(nw))
    for forbidden in (
        "decisions",
        "_record_decisions",
        "feedback_contexts",
        "feedback",
        "meldinger",
        "historikk",
        "varsel",
        "push_enabled",
        "webpush",
    ):
        assert forbidden not in names, f"notification_wording reaches {forbidden!r}"


def test_the_subject_id_is_what_is_handed_to_the_writer_as_its_correlation_key():
    """`varsel` passes `journey_key`, which is literally `from|to|begin|end|days` — a home-to-work
    pattern with times and weekdays — and hashes it before logging for exactly that reason. A
    partner subject id is opaque and service-issued, so it is safe to pass and useless to anyone
    who reads it."""
    _reset()
    seen = {}

    def capture(message_input, *, journey_key="", situation_number="", **kw):
        seen["journey_key"] = journey_key
        seen["caller"] = kw.get("caller")
        return llm_message.MessageResult("Toget er innstilt.", "claude", "m", "ok")

    original = llm_message.write_message_detailed
    llm_message.write_message_detailed = capture
    try:
        nw.write_wording(ROUTE, _facts(), subject_id="dep_9f2c")
    finally:
        llm_message.write_message_detailed = original
    assert seen["journey_key"] == "dep_9f2c"
    assert seen["caller"] == "partner", "the audit record is marked as this surface's"


def test_only_a_non_browser_surface_carries_the_llm_caller_marker():
    """`llm_message` gates its prompt/reply DEBUG record on `not caller`, so a truthy marker on the
    browser silently switches off the `/varsel` message debugging dev deliberately leaves on.

    `WordingPolicy.name` is this module's own log label; `WordingPolicy.caller` is what crosses into
    `llm_message`, and only a non-browser surface has one. They were one field until 2026-09-07,
    which made `BROWSER` pass `caller="browser"` -- truthy, and read by the gate as "suppress"."""
    assert nw.BROWSER.name == "browser"
    assert nw.BROWSER.caller is None, "the browser must not mark itself as a non-browser surface"
    assert nw.PARTNER.caller == "partner"

    # The gate itself, so the two halves cannot drift: a browser record keeps `message`/`reply`, a
    # partner record drops them, and neither carries `replay` (that word means /meldinger).
    assert "caller" not in llm_message._log_fields(False, nw.BROWSER.caller, event="llm_message")
    assert llm_message._log_fields(False, nw.PARTNER.caller, event="llm_message")["caller"] == (
        "partner"
    )


# ---- scenario 6: a partner generation writes no prompt record ------------------------------------
class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _llm_log_fields(call):
    """The `extra={"fields": ...}` bodies a call emitted from `llm_message`, DEBUG included.

    The same shape as `test_llm_message._log_fields_of`, restated rather than imported: importing
    that suite would run its import-time environment pinning inside this process, and the two files
    pin different things.
    """
    handler = _Collect()
    logger = llm_message.logger
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        call()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return [r.fields for r in handler.records if isinstance(getattr(r, "fields", None), dict)]


def test_a_partner_wording_writes_no_prompt_record_even_with_prompt_logging_on():
    """Scenario 6, end to end through `write_wording` rather than through `llm_message` directly.

    `VARSEL_LLM_LOG_PROMPTS` is "true" in dev (`helm/kix-avvik/env/values-kub-ent-dev.yaml`), which
    is exactly the environment this rollout starts in, and `/varsel` message debugging needs it left
    on — so the suppression cannot be an environment variable and is in code, keyed on the
    `caller` marker this surface's policy carries. The record it suppresses is the whole
    `message_input`: origin, destination and departure time (R16). The INFO record survives minus
    the prose, because provider/model/result/hashed-subject are what an operator diagnoses with.

    Checked HERE as well as in `test_llm_message` because the coupling is what breaks: the
    suppression is `llm_message`'s, and whether it applies depends on `PARTNER.caller` being passed
    through `write_wording`. A test of either half alone passes with the two disconnected.
    """
    _reset()
    prose = "Toget er innstilt."
    llm_message._BACKENDS["claude"] = lambda body: json.dumps({"melding": prose})
    previous = llm_message._LOG_PROMPTS
    llm_message._LOG_PROMPTS = True
    try:
        partner = _llm_log_fields(
            lambda: nw.write_wording(ROUTE, _facts(), subject_id="dep_abc")
        )
        # The browser policy, on the same input, is the control: it is the traffic the flag exists
        # for, and it must be untouched.
        browser = _llm_log_fields(
            lambda: nw.write_wording(
                ROUTE, _facts(), policy=nw.BROWSER, subject_id="jkey|A|B"
            )
        )
    finally:
        llm_message._LOG_PROMPTS = previous
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert not [f for f in partner if f["event"] == "llm_message_prompt"]
    assert not any("message_input" in f for f in partner)
    assert not any("message" in f for f in partner), "the prose must not reach the log either"
    assert [f["caller"] for f in partner if f["event"] == "llm_message"] == ["partner"]
    assert [f for f in browser if f["event"] == "llm_message_prompt"], (
        "the poller's own debugging record must be untouched"
    )
    assert any(f.get("message") == prose for f in browser)
    assert not any("caller" in f for f in browser)


# ---- scenario 5: the gate is not widened -------------------------------------------------------
def test_no_module_in_the_api_directory_widens_the_alert_gate():
    """KTD2: the partner gate is `alertworthy`, full stop — one gate, read by both surfaces.

    The branch this unit was extracted from carried a second function beside the gate that widened
    it by a declared cancellation anywhere on the ridden segment. KTD2 refuses it by name: it makes
    a partner recipient see alerts a `/varsel` user does not, which R3 forbids, and nothing in the
    decision log can score the difference. Reintroducing it would be SILENT — every existing check
    here and in `test_varsel` would keep passing, because a widening only ever adds subjects.

    Scoped to the Python sources under `disruptions/api/`, and over IDENTIFIERS rather than raw
    text, for two reasons that are the same reason: the plan document legitimately names the
    function in order to forbid it, and this docstring and this test's own name do too. A substring
    search would fail on the prohibition instead of on the thing prohibited.
    """
    api_dir = pathlib.Path(__file__).resolve().parent
    sources = sorted(api_dir.glob("*.py"))
    assert len(sources) > 30, f"the glob found only {len(sources)} modules; check the path"
    offenders = [
        path.name
        for path in sources
        if "is_material" in _code_names(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"the widened gate is back in {offenders}"


def _run():
    logging.getLogger("notification_wording").setLevel(logging.CRITICAL)
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        _reset()
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} notification wording checks passed.")


if __name__ == "__main__":
    _run()
