"""Checks for the shared notification wording and its two per-surface policies (U1, KTD1/KTD2).

Runs standalone (`uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery
--with google-cloud-storage --with pywebpush python test_notification_wording.py`) or under
pytest -- it imports `varsel` to prove the aliases are the shared functions, which pulls FastAPI.
Hermetic: no Vertex, no network, no BigQuery — the backends are stubbed and the budgets are reset
between checks.

The suite is in two halves. The first is a MOVE check: the wording came out of `varsel` unchanged,
so every golden string here was read off the pre-extraction module and a diff in one of them is a
different notification on a real traveller's lock screen, never a test to update. The second is the
new surface: a webhook wording is produced with no VAPID keypair and no model at all, its model
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


def _reset(*, provider="claude", webhook_delivery=True):
    llm_message._stop_name_provider = None
    llm_message._PROVIDER = provider
    llm_message._ENABLED = True
    llm_message._PROJECT = "ent-kixavvik-test"
    llm_message._STATE = llm_message._LatchState()
    nw.reset_state()
    nw._WEBHOOK_DELIVERY_ENABLED = webhook_delivery


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


def test_the_vehicle_noun_is_the_lines_mode_and_the_codespace_only_without_one():
    """KIX-25: a Ruter T-bane said «Buss», because the codespace was all the noun knew and T-bane
    and trikk ride under `RUT`. With the line's JP mode it is «T-bane»; without one (JP has not
    answered, or a mode this codebase has no noun for) the codespace rule stands."""
    import line_modes

    line_modes.reset()
    assert [nw.mode_label(o) for o in ("VYG", "NSB", "GOA", "SJN", "FLT")] == ["Tog"] * 5
    assert [nw.mode_label(o) for o in ("RUT", "KOL", None, "")] == ["Buss"] * 4
    modes = {"RUT:Line:5": "metro", "RUT:Line:12": "tram", "RUT:Line:31": "bus",
             "SKY:Line:1047": "water", "SKY:Line:X": "air"}
    line_modes.set_modes([{"id": k, "transportMode": v} for k, v in modes.items()], datetime.now(UTC))
    try:
        cases = {("RUT", "RUT:Line:5"): "T-bane", ("RUT", "RUT:Line:12"): "Trikk",
                 ("RUT", "RUT:Line:31"): "Buss", ("SKY", "SKY:Line:1047"): "Båt",
                 ("VYG", "VYG:Line:L1"): "Tog", ("SKY", "SKY:Line:X"): "Buss"}
        assert {k: nw.mode_label(*k) for k in cases} == cases
        # The other call site: the wording facts a pull entry is worded from pass the line too.
        assert nw.DepartureWordingFacts(operator="RUT", line="RUT:Line:5").mode == "T-bane"
    finally:
        line_modes.reset()


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


# ---- half two: the client surface ---------------------------------------------------------------
def test_a_webhook_wording_needs_no_vapid_keypair_and_no_model():
    """KTD4's flat requirement: a webhook wording does not depend on a VAPID keypair.

    A deployment can hold no VAPID keypair at all — `varsel.push_enabled()` False, no browser
    delivery attempted — and a notification event still has to carry a ready-to-display Norwegian title
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


def test_the_egress_kill_switch_reaches_the_notification_api_path():
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
    assert nw.spent(nw._WEBHOOK_PATH) == (0, 0), (
        "the kill switch must not burn the allowance -- otherwise it reports `rate_limited` after "
        "500 calls it never made, which is a misleading diagnosis of the control an operator "
        "reached for in an incident"
    )


def test_the_webhook_delivery_switch_is_its_own_and_defaults_off():
    """The rollout note is explicit: deterministic wording first, model wording enabled separately
    after matching and delivery qualify. Sharing `VARSEL_LLM_ENABLED` would turn model prose on for
    clients in the same deploy that turns it on for the PWA."""
    source = inspect.getsource(nw)
    assert 'os.environ.get("NOTIFICATION_WEBHOOK_LLM_ENABLED", "false")' in source
    assert nw.WEBHOOK.deliver is nw.webhook_delivery_enabled
    assert nw.BROWSER.deliver is not nw.webhook_delivery_enabled
    _reset(webhook_delivery=False)
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


# ---- scenarios 3 and 4: the client allowance and latch are its own ----------------------------
def test_a_webhook_pass_is_bounded_and_the_bound_is_reported_not_hidden():
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
    assert nw.spent(nw._WEBHOOK_PATH) == (2, 2), "a capped subject spends no allowance"


def test_webhook_exhaustion_cannot_consume_the_browser_allowance():
    """Scenario 3, the arithmetic half.

    The ledger is keyed by caller in the shape `alternatives._fetch_calls` and
    `meldinger._replay_calls` already use. The browser push path does not call this module at all,
    so its slot stays empty BY CONSTRUCTION — which is the point of the keying: it makes that a
    structural property rather than a fact somebody has to keep true, and it stops a later caller
    silently drawing the client allowance down or being drawn down by it.
    """
    _reset()
    llm_message._BACKENDS["claude"] = lambda body: '{"melding":"Toget er innstilt."}'
    try:
        for i in range(5):
            nw.write_wording(ROUTE, _facts(cancelled=True), subject_id=f"dep_{i}")
        assert nw.spent(nw._WEBHOOK_PATH) == (5, 5)
        assert nw.spent(nw._BROWSER_PATH) == (0, 0)
        # ...and once the hour is full the client surface degrades to the template rather than
        # borrowing from anywhere.
        nw._calls[nw._WEBHOOK_PATH][0].extend([nw.monotonic()] * nw._MAX_CALLS_PER_HOUR)
        wording = nw.write_wording(ROUTE, _facts(cancelled=True), subject_id="dep_last")
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert wording.status == "rate_limited" and wording.source == "deterministic"
    assert nw.spent(nw._BROWSER_PATH) == (0, 0)


def test_webhook_exhaustion_cannot_disable_the_pollers_own_backend():
    """Scenarios 3 and 4, the latch half — and the one whose failure is silent on both sides.

    A client burst that drives every backend into a configuration error must not make
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
        assert llm_message.generation_enabled(nw.WEBHOOK.state()) is False
        assert llm_message.generation_enabled() is True, "the push path is untouched"
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert nw.WEBHOOK.state() is not llm_message._STATE
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


def test_a_webhook_call_writes_no_public_audit_data():
    """R16, structurally: this module cannot reach the writers that publish.

    `varsel_decisions` feeds `/historikk` and `/meldinger`, feedback contexts feed the review
    exporter, and all three are public or semi-public surfaces. A client's input and prose must
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
    webhook subject id is opaque and service-issued, so it is safe to pass and useless to anyone
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
    assert seen["caller"] == "webhook", "the audit record is marked as this surface's"


def test_only_a_non_browser_surface_carries_the_llm_caller_marker():
    """`llm_message` gates its prompt/reply DEBUG record on `not caller`, so a truthy marker on the
    browser silently switches off the `/varsel` message debugging dev deliberately leaves on.

    `WordingPolicy.name` is this module's own log label; `WordingPolicy.caller` is what crosses into
    `llm_message`, and only a non-browser surface has one. They were one field until 2026-09-07,
    which made `BROWSER` pass `caller="browser"` -- truthy, and read by the gate as "suppress"."""
    assert nw.BROWSER.name == "browser"
    assert nw.BROWSER.caller is None, "the browser must not mark itself as a non-browser surface"
    assert nw.WEBHOOK.caller == "webhook"

    # The gate itself, so the two halves cannot drift: a browser record keeps `message`/`reply`, a
    # client record drops them, and neither carries `replay` (that word means /meldinger).
    assert "caller" not in llm_message._log_fields(False, nw.BROWSER.caller, event="llm_message")
    assert llm_message._log_fields(False, nw.WEBHOOK.caller, event="llm_message")["caller"] == (
        "webhook"
    )


# ---- scenario 6: a client generation writes no prompt record ------------------------------------
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


def test_a_webhook_wording_writes_no_prompt_record_even_with_prompt_logging_on():
    """Scenario 6, end to end through `write_wording` rather than through `llm_message` directly.

    `VARSEL_LLM_LOG_PROMPTS` is "true" in dev (`helm/kix-avvik/env/values-kub-ent-dev.yaml`), which
    is exactly the environment this rollout starts in, and `/varsel` message debugging needs it left
    on — so the suppression cannot be an environment variable and is in code, keyed on the
    `caller` marker this surface's policy carries. The record it suppresses is the whole
    `message_input`: origin, destination and departure time (R16). The INFO record survives minus
    the prose, because provider/model/result/hashed-subject are what an operator diagnoses with.

    Checked HERE as well as in `test_llm_message` because the coupling is what breaks: the
    suppression is `llm_message`'s, and whether it applies depends on `WEBHOOK.caller` being passed
    through `write_wording`. A test of either half alone passes with the two disconnected.
    """
    _reset()
    prose = "Toget er innstilt."
    llm_message._BACKENDS["claude"] = lambda body: json.dumps({"melding": prose})
    previous = llm_message._LOG_PROMPTS
    llm_message._LOG_PROMPTS = True
    try:
        client = _llm_log_fields(
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
    assert not [f for f in client if f["event"] == "llm_message_prompt"]
    assert not any("message_input" in f for f in client)
    assert not any("message" in f for f in client), "the prose must not reach the log either"
    assert [f["caller"] for f in client if f["event"] == "llm_message"] == ["webhook"]
    assert [f for f in browser if f["event"] == "llm_message_prompt"], (
        "the poller's own debugging record must be untouched"
    )
    assert any(f.get("message") == prose for f in browser)
    assert not any("caller" in f for f in browser)


# ---- scenario 5: the gate is not widened -------------------------------------------------------
def test_no_module_in_the_api_directory_widens_the_alert_gate():
    """KTD2: the client gate is `alertworthy`, full stop — one gate, read by both surfaces.

    The branch this unit was extracted from carried a second function beside the gate that widened
    it by a declared cancellation anywhere on the ridden segment. KTD2 refuses it by name: it makes
    a webhook recipient see alerts a `/varsel` user does not, which R3 forbids, and nothing in the
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


# ---- U2 of the pull read: the third policy, PULL --------------------------------------------------
def test_a_pull_wording_is_charged_to_its_own_allowance_and_to_nobody_elses():
    """R4, the arithmetic half, for the third surface: a public read that words on demand must not
    be able to draw the webhook channel's or the browser's allowance down, nor be drawn down by
    them. Same keying, one more slot -- and the slot must be SEEDED, since an unseeded path is a
    `KeyError` on the first call rather than a refusal."""
    _reset()
    llm_message._BACKENDS["claude"] = lambda body: '{"melding":"Toget er innstilt."}'
    try:
        for i in range(3):
            wording = nw.write_wording(
                ROUTE, _facts(cancelled=True), policy=nw.PULL, subject_id=f"pair_{i}"
            )
            assert wording.source == "llm", wording.status
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert nw.spent(nw._PULL_PATH) == (3, 3)
    assert nw.spent(nw._WEBHOOK_PATH) == (0, 0)
    assert nw.spent(nw._BROWSER_PATH) == (0, 0)


def test_the_pull_allowance_drains_over_minutes_rather_than_in_one_burst():
    """The pull path is the only one with a per-minute ceiling: `WORDINGS_PER_MIN` (120) under an
    hour of `WORDINGS_PER_HOUR` (2000), so a burst from a public endpoint cannot spend the whole
    hour in its first seconds. The browser and webhook policies keep the module's two numbers and
    no per-minute ceiling at all -- the call past the minute's allowance is refused on pull and
    nowhere else.

    All three moved 4x on 2026-09-22, with the snapshot term in `wording_key`: a wording is
    reusable for one prediction cycle rather than for a situation's 12 h life, which was measured
    at 3.93 wordings per subject against 1. The RATIO is what this pins rather than the absolute
    numbers -- the minute has to stay a small fraction of the hour whatever the hour is, or the
    burst protection is gone."""
    _reset()
    assert (nw.WORDINGS_PER_MIN, nw.WORDINGS_PER_HOUR, nw.WORDINGS_PER_DAY) == (120, 2000, 10000)
    assert nw.WORDINGS_PER_MIN * 10 <= nw.WORDINGS_PER_HOUR <= nw.WORDINGS_PER_DAY, (
        "a minute's allowance has grown into a sizeable share of the hour; a single burst can now "
        "leave the rest of the hour on the template, which is what the per-minute ceiling exists "
        "to prevent"
    )
    t = nw.monotonic()
    for _ in range(nw.WORDINGS_PER_MIN):
        assert nw._spend(nw._PULL_PATH, now=t)
    assert not nw._spend(nw._PULL_PATH, now=t), "the call past the minute's allowance"
    assert nw.spent(nw._PULL_PATH)[0] == nw.WORDINGS_PER_MIN < nw.WORDINGS_PER_HOUR, (
        "the hour still has room; it is the minute that bound"
    )
    assert nw._spend(nw._PULL_PATH, now=t + 61.0), "a minute later the window has moved on"
    for path in (nw._WEBHOOK_PATH, nw._BROWSER_PATH):
        for _ in range(nw.WORDINGS_PER_MIN + 1):
            assert nw._spend(path, now=t), f"no per-minute ceiling binds {path}"
    # ...and end to end: the refusal is reported as `rate_limited`, the status the operations
    # document already names, rather than as a new word.
    _reset()
    llm_message._BACKENDS["claude"] = lambda body: '{"melding":"Toget er innstilt."}'
    try:
        now = nw.monotonic()
        nw._calls[nw._PULL_PATH].minute.extend([now] * nw.WORDINGS_PER_MIN)
        wording = nw.write_wording(ROUTE, _facts(cancelled=True), policy=nw.PULL, subject_id="p")
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert wording.status == "rate_limited" and wording.source == "deterministic"


def test_pull_exhaustion_cannot_disable_any_other_surfaces_backend():
    """R4, the latch half. A configuration error discovered through the pull holder latches only
    it: the module-global holder (the poller's) and the webhook channel's still read True."""
    _reset()

    def unserved(_body):
        raise _ConfigurationError("model not enabled in this project")

    llm_message._BACKENDS["claude"] = unserved
    try:
        for i in range(3):
            wording = nw.write_wording(ROUTE, _facts(), policy=nw.PULL, subject_id=f"pair_{i}")
            assert wording.source == "deterministic"
        assert llm_message.generation_enabled(nw.PULL.state()) is False
        assert llm_message.generation_enabled() is True, "the push path is untouched"
        assert llm_message.generation_enabled(nw.WEBHOOK.state()) is True, (
            "the webhook channel's holder is untouched"
        )
    finally:
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert nw.PULL.state() is not llm_message._STATE
    assert nw.PULL.state() is not nw.WEBHOOK.state()


def test_reset_state_clears_the_pull_holder_and_the_pull_ledger():
    """`reset_state` iterates `_POLICIES`, so the pull holder is cleared by being ON the tuple; a
    test that drove it off would otherwise leave every later pull wording a template for thirty
    minutes with nothing saying why."""
    _reset()
    before = nw.PULL.state()
    assert before is not None
    nw._spend(nw._PULL_PATH)
    nw._spend(nw._PULL_PATH)
    assert nw.spent(nw._PULL_PATH) == (2, 2)
    nw.reset_state()
    assert nw.spent(nw._PULL_PATH) == (0, 0)
    assert nw.PULL.state() is not before, "the latch holder must be rebuilt, not reused"
    assert nw.PULL in nw._POLICIES


def test_a_pull_wording_writes_no_prompt_record_and_no_definition_digest():
    """R11, both halves through `write_wording`. The pull policy carries a truthy `caller`, so the
    prompt/reply DEBUG record is suppressed in code with `VARSEL_LLM_LOG_PROMPTS` on and the INFO
    record drops the prose; and the read hands the SALTED pair digest as the subject id, so the
    `journey_digest` the model-call record carries is not `key_digest` of the definition's
    `journey_key` -- the unsalted digest `/historikk`'s rows and the browser's records join on."""
    import alternatives

    _reset()
    assert nw.PULL.caller, "a falsy marker would switch the suppression off"
    assert llm_message._log_fields(False, nw.PULL.caller, event="llm_message")["caller"] == "pull"
    prose = "Toget er innstilt."
    llm_message._BACKENDS["claude"] = lambda body: json.dumps({"melding": prose})
    previous = llm_message._LOG_PROMPTS
    llm_message._LOG_PROMPTS = True
    subject = alternatives.pair_digest("NSR:StopPlace:A", "NSR:StopPlace:B")
    try:
        pull = _llm_log_fields(
            lambda: nw.write_wording(ROUTE, _facts(), policy=nw.PULL, subject_id=subject)
        )
    finally:
        llm_message._LOG_PROMPTS = previous
        llm_message._BACKENDS["claude"] = llm_message._claude_write
    assert not [f for f in pull if f["event"] == "llm_message_prompt"]
    assert not any("message_input" in f for f in pull)
    assert not any("message" in f for f in pull), "the prose must not reach the log either"
    records = [f for f in pull if f["event"] == "llm_message"]
    assert [f["caller"] for f in records] == ["pull"]
    definition = "NSR:StopPlace:A|NSR:StopPlace:B|07:00|09:00|0,1,2,3,4"
    assert records[0]["journey_digest"] == llm_message.key_digest(subject)
    assert records[0]["journey_digest"] != llm_message.key_digest(definition)


# ---- KTD9: the wording key both surfaces build ---------------------------------------------------


class _Alt:
    """An `alternatives.AlternativeJourney` in the four fields the identity reads. A stand-in, not a
    stub of convenience: `alternative_identity` is duck-typed precisely so this module never imports
    the poller's model, and a test that used the real class would not check that."""

    def __init__(self, dep, arr, at_risk=False, in_time=True):
        self.departure_time, self.arrival_time = dep, arr
        self.at_risk, self.arrives_before_original = at_risk, in_time


class _Snapshot:
    """Stands in for the `/impact` response the alert was scored against -- `wording_key` reads
    nothing off it but `refreshed_at`."""

    def __init__(self, refreshed_at):
        self.refreshed_at = refreshed_at


SNAPSHOT = DEP - timedelta(minutes=20)


class _Winner:
    def __init__(self, situation="S1", departure=DEP, cancelled=False, snapshot=SNAPSHOT):
        self.situation_number, self.departure_time, self.cancelled = situation, departure, cancelled
        self.impact = None if snapshot is None else _Snapshot(snapshot)


def test_the_wording_key_is_the_place_pair_and_everything_the_wording_states():
    """KTD9's shape, in one function so the poller and the pull read cannot come to disagree.

    The PAIR and not the journey definition: what a wording says is a function of the trip's two
    ends and the disrupted train, never of the window or weekday set one commuter registered. Every
    other element is something the text states out loud, so two keys differing in one of them are
    two different sentences.
    """
    pair = ("NSR:StopPlace:A", "NSR:StopPlace:B")
    base = nw.wording_key(pair, _Winner(), None, tomorrow=False)
    assert base[0] == pair and base[1] == "S1" and base[2] == DEP.isoformat()
    assert base[-2] == llm_message.PROMPT_VERSION, "a reworded brief is a different writer"
    assert base[-1] == SNAPSHOT.isoformat(), "the snapshot the figures came from"

    for label, other in (
        ("a different pair", nw.wording_key(("NSR:StopPlace:A", "NSR:StopPlace:C"), _Winner(), None, tomorrow=False)),
        ("a different situation", nw.wording_key(pair, _Winner(situation="S2"), None, tomorrow=False)),
        ("a different departure", nw.wording_key(pair, _Winner(departure=DEP + timedelta(hours=1)), None, tomorrow=False)),
        ("a declared cancellation", nw.wording_key(pair, _Winner(cancelled=True), None, tomorrow=False)),
        ("«i morgen»", nw.wording_key(pair, _Winner(), None, tomorrow=True)),
        ("an alternative", nw.wording_key(pair, _Winner(), _Alt(DEP, DEP + timedelta(minutes=30)), tomorrow=False)),
        # 2026-09-22. The one element that is not something the sentence NAMES: it is every figure
        # the sentence quotes. `varsel._webhook_payload` publishes `probabilities` and `delay` live
        # beside the prose, so a wording reused across a rescoring read «89 % sjanse for
        # innstilling» next to `cancellation: 0.1737` on the journey-alert read.
        ("a rescoring", nw.wording_key(pair, _Winner(snapshot=SNAPSHOT + timedelta(minutes=5)), None, tomorrow=False)),
    ):
        assert other != base, f"{label} must not share a cache entry with the plain reading"

    # And the same inputs are the same key, which is the half that makes it a CACHE.
    assert nw.wording_key(pair, _Winner(), None, tomorrow=False) == base

    # A reworded brief invalidates every entry, which is what stops a prompt edit showing
    # yesterday's answer under today's instructions. Patched on the module the helper reads through,
    # not on a copy: `prompt_version` imports `llm_message` at call time for exactly this.
    before = llm_message.PROMPT_VERSION
    llm_message.PROMPT_VERSION = "deadbeef"
    try:
        assert nw.wording_key(pair, _Winner(), None, tomorrow=False) != base
    finally:
        llm_message.PROMPT_VERSION = before


def test_the_alternative_identity_is_the_advice_and_not_the_rescoring():
    """Moved from `varsel` with its reasoning intact: minute resolution because every wording renders
    %H:%M, and both booleans because each is stated out loud. A key that moved with the continuous
    re-scoring would spend an LLM call per cycle producing byte-identical prose."""
    dep, arr = DEP, DEP + timedelta(minutes=30)
    base = nw.alternative_identity(_Alt(dep, arr))
    assert nw.alternative_identity(_Alt(dep.replace(second=41), arr)) == base, (
        "a realtime nudge inside the minute is the same advice"
    )
    assert nw.alternative_identity(_Alt(dep + timedelta(minutes=1), arr)) != base
    assert nw.alternative_identity(_Alt(dep, arr, at_risk=True)) != base, (
        "«kan også bli rammet» is in the text"
    )
    assert nw.alternative_identity(_Alt(dep, arr, in_time=False)) != base, (
        "arriving later than planned is in the text"
    )
    assert nw.alternative_identity(_Alt(None, None)) == (None, None, False, True)


def test_both_surfaces_build_their_key_here_rather_than_each_keeping_a_copy():
    """The plan's stop condition, from this side: `varsel` must not import `notification_pull` to
    share the shape, and neither may keep its own copy. Checked over the identifiers rather than the
    prose, so the paragraphs explaining the rule do not trip it."""
    import notification_pull
    import varsel

    assert varsel._alternative_identity is nw.alternative_identity
    assert "notification_wording.wording_key" in inspect.getsource(varsel._message_key)
    assert "notification_wording.wording_key" in inspect.getsource(notification_pull._wording_key)
    assert "varsel" not in _code_names(inspect.getsource(nw.wording_key)), (
        "the leaf reached back into the poller"
    )


# ---- the morning digest about trains taken out of the plan (plan 2026-09-23-001, U4) -------------
def test_the_digest_names_the_trains_soonest_first_and_says_why_they_are_missing():
    """R7's acceptance example, word for word: one sentence per fact, and no route line -- the
    caller adds that, because its names are a traveller's free text."""
    title, wording = nw.removed_digest(
        [("R22 08:00", False), ("R23 08:06", False), ("R23 08:36", False)], weekday=2
    )
    assert title == "3 tog er tatt ut av rutetabellen i dag"
    assert wording == (
        "R22 08:00, R23 08:06 og R23 08:36 går ikke i dag. "
        "De går vanligvis på onsdager, men finnes ikke i dagens rutetabell."
    )


def test_the_digest_names_six_trains_and_counts_the_rest():
    trains = [(f"L1 {6 + i // 2:02d}:{(i % 2) * 30:02d}", False) for i in range(38)]
    title, wording = nw.removed_digest(trains, weekday=0)
    assert title.startswith("38 tog ")
    assert wording.startswith(
        "L1 06:00, L1 06:30, L1 07:00, L1 07:30, L1 08:00, L1 08:30 og 32 til går ikke i dag."
    )
    assert "på mandager" in wording


def test_one_train_reads_in_the_singular_and_a_bus_is_named():
    """The user's 2026-09-23 choice: a closure's trains are included, and the digest says when Vy
    has set up a bus. All of them covered reads as the card's own sentence; some, by name."""
    _title, one = nw.removed_digest([("L2 07:10", True)], weekday=6)
    assert one == (
        "L2 07:10 går ikke i dag. Toget går vanligvis på søndager, men finnes ikke i dagens "
        "rutetabell. Vy har satt opp buss for tog."
    )
    _title, some = nw.removed_digest(
        [("L2 07:10", True), ("R13 07:40", False), ("L2 08:10", True)], weekday=3
    )
    assert some.endswith("Vy har satt opp buss for L2 07:10 og L2 08:10."), some


def test_a_train_label_names_the_destination_beside_the_line_and_only_there():
    assert nw.train_label("Tog", "RE10", "08:00", "Hamar") == "Tog RE10 Hamar 08:00"
    assert nw.train_label("Tog", "RE10", "08:00") == "Tog RE10 08:00"
    # No line, no destination: «Tog kl 08:00», never «Tog Hamar 08:00».
    assert nw.train_label("Tog", None, "08:00", "Hamar") == "Tog kl 08:00"
    assert (
        nw.train_label_from_input(
            {"transportmiddel": "Tog", "linje": "RE10", "destinasjon": "Hamar", "avgang_kl": "08:00"}
        )
        == "Tog RE10 Hamar 08:00"
    )
    # And the fact reaches the input every surface words from -- push, pull, webhook, card.
    message_input = nw.build_message_input(
        nw.Route("NSR:StopPlace:337", "NSR:StopPlace:451"),
        nw.DepartureWordingFacts(
            situation_number="X", operator="VYG", line="VYG:Line:RE10", destination="Hamar",
            departure_time=datetime(2026, 9, 24, 6, 0, tzinfo=UTC), cancelled=True,
            cancel_probability=1.0,
        ),
    )
    assert message_input["destinasjon"] == "Hamar"
    assert nw.notification_title(message_input, "Oslo S", "Hamar").startswith("Tog RE10 Hamar 08:00")


# ---- The one-sentence summary (the favourites row, 2026-09-25) ------------------------------------


def _mi(hhmm, *, line="L1", verb="delay", tomorrow=False, mode="Tog"):
    """A message input as `llm_message.build_message_input` shapes it, by the title's own branches."""
    mi = {"transportmiddel": mode, "linje": line, "avgang_kl": hhmm}
    if verb == "cancelled":
        mi["innstilt"] = True
    elif verb == "likely":
        mi["innstilling_prosent"] = 80
    elif verb == "delay":
        mi["forsinkelse_min"] = {"typisk": 8, "verstefall": 20}
    if tomorrow:
        mi["i_morgen"] = True
    return mi


#: The operator English the fixtures' situations carry; any other text has none.
_ENGLISH = {"Signalfeil ved Asker": "Signal failure at Asker", "Signalfeil ved Asker.": "Signal failure at Asker.",
            "Buss for tog": "Rail replacement bus"}


def _sit(number, summary="Signalfeil ved Asker", has_situation=True):
    """One of the payload's `situations`, as `varsel._webhook_situations` builds it."""
    return {"situation_number": number, "summary": summary, "summary_en": _ENGLISH.get(summary),
            "description": None, "has_situation": has_situation}


def _nb(trains):
    return nw.alerts_summary(trains)[0]


def _en(trains):
    return nw.alerts_summary(trains)[1]


def _nn(trains):
    return nw.alerts_summary(trains)[2]


def test_the_summary_leads_with_a_cause_every_train_names_and_counts_per_outcome():
    """The cause is operator TEXT every train carries: the matcher keeps one train per situation,
    so the numbers differ by construction and only the operator repeating itself can be shared."""
    trains = [
        (_mi("07:18", verb="cancelled"), [_sit("S1")]),
        (_mi("07:48", verb="cancelled"), [_sit("S2"), _sit("S6", "Ta neste tog")]),
        (_mi("08:18", verb="likely"), [_sit("S3")]),
        (_mi("08:48"), [_sit("S4")]),
        (_mi("09:18", verb="none"), [_sit("S5")]),
    ]
    assert _nb(trains) == (
        "Signalfeil ved Asker: 2 tog er innstilt, 1 kan bli innstilt, 1 kan bli forsinket og 1 med mulig avvik"
    )
    assert _en(trains) == (
        "Signal failure at Asker: 2 trains are cancelled, 1 may be cancelled, 1 may be delayed and 1 with "
        "possible disruption"
    )


def test_without_a_shared_cause_the_worst_train_is_named_and_the_rest_counted():
    # The operator said something different about the third train, so no text is every train's,
    # and the lead is the WORST (first in the order handed in), not the soonest.
    trains = [
        (_mi("08:18", verb="cancelled"), [_sit("S2")]),
        (_mi("07:18"), [_sit("S1")]),
        (_mi("07:48"), [_sit("S3", "Ta neste tog")]),
    ]
    assert _nb(trains) == "Tog L1 08:18 er innstilt, 2 til kan bli forsinket"
    assert _en(trains) == "Train L1 08:18 is cancelled, 2 more may be delayed"
    trains = [
        (_mi("08:18", verb="cancelled"), [_sit("S2")]),
        (_mi("07:18", verb="likely"), [_sit("S1")]),
        (_mi("07:48"), [_sit("S3", "Ta neste tog")]),
        (_mi("09:48", verb="none"), [_sit("S4")]),
    ]
    assert _nb(trains) == (
        "Tog L1 08:18 er innstilt, 1 til kan bli innstilt, 1 kan bli forsinket og 1 med mulig avvik"
    )
    assert _en(trains) == (
        "Train L1 08:18 is cancelled, 1 more may be cancelled, 1 may be delayed and 1 with possible disruption"
    )
    # A second train with the worst outcome: naming one of them says nothing the other does not,
    # so the counts stand alone (the owner's example, 2026-09-25).
    trains = [
        (_mi("11:33", verb="cancelled"), [_sit("S1")]),
        (_mi("07:03", verb="cancelled"), [_sit("S2", "Innstilt Asker–Skøyen")]),
        (_mi("07:42", verb="cancelled"), [_sit("S3", "Ta andre tog")]),
        (_mi("09:35", verb="cancelled"), [_sit("S4", "Buss for tog")]),
        (_mi("09:50", verb="likely"), [_sit("S5", "Innstilt Skøyen–Asker")]),
        (_mi("09:05"), [_sit("S6", "Forsinkelser på Drammenbanen")]),
        (_mi("10:20"), [_sit("S7", "Toget står")]),
        (_mi("13:20"), [_sit("S8", "Minibuss for tog")]),
    ]
    assert _nb(trains) == "4 tog er innstilt, 1 kan bli innstilt og 3 kan bli forsinket"
    assert _en(trains) == "4 trains are cancelled, 1 may be cancelled and 3 may be delayed"


def test_a_placeholder_summary_never_leads_and_a_long_or_partial_one_falls_back():
    # A synthetic situation every train names: its summary is ours, and `has_situation` says so.
    placeholder = _sit("KIX:NoSituation:VYG:L1", "Mulig avvik", has_situation=False)
    trains = [(_mi("07:18", verb="none"), [placeholder]), (_mi("07:48", verb="none"), [placeholder])]
    assert _nb(trains) == "2 tog med mulig avvik"
    assert _en(trains) == "2 trains with possible disruption"
    # Over the length bound the cause is not cut, it is not used.
    long = "x" * (nw.CAUSE_MAX_CHARS + 1)
    trains = [(_mi("07:18"), [_sit("S1", long)]), (_mi("07:48", verb="none"), [_sit("S1", long)])]
    assert _nb(trains) == "Tog L1 07:18 kan bli forsinket, 1 til med mulig avvik"
    assert _en(trains) == "Train L1 07:18 may be delayed, 1 more with possible disruption"
    at_bound = [(_mi("07:18"), [_sit("S1", "x" * nw.CAUSE_MAX_CHARS)]), (_mi("07:48"), [_sit("S2", "x" * nw.CAUSE_MAX_CHARS)])]
    assert _nb(at_bound) == "x" * nw.CAUSE_MAX_CHARS + ": 2 tog kan bli forsinket"
    # A cause with no operator English: the English line takes the no-cause branch rather than
    # quote the Norwegian inside it.
    assert _en(at_bound) == "2 trains may be delayed"
    # The first text on EVERY train leads, in the worst train's payload order; a trailing full
    # stop is dropped so the colon follows the cause. A placeholder on one train beside operator
    # text (a no-SX reading collapsed under a published one) neither leads nor blocks.
    trains = [
        (_mi("07:18"), [_sit("S_only_here", "Ta neste tog"), _sit("S1", "Signalfeil ved Asker.")]),
        (_mi("07:48"), [_sit("KIX:NoSituation:VYG:t2", "Mulig avvik", has_situation=False),
                        _sit("S2", "Signalfeil ved Asker")]),
    ]
    assert _nb(trains) == "Signalfeil ved Asker: 2 tog kan bli forsinket"
    assert _en(trains) == "Signal failure at Asker: 2 trains may be delayed"


def test_advice_and_status_phrases_never_lead_as_a_cause():
    """Vy attaches «Ta neste tog» beside the cancellation message of each train it cancels, so it is
    the text two cancelled trains most often share -- and «Ta neste tog: 2 tog er innstilt» puts
    the advice before the fact. The stop-list drops it whole, case and full stop aside, and the
    next shared text still leads."""
    advice = _sit("S_advice", "Ta neste tog.")
    trains = [
        (_mi("07:18", verb="cancelled"), [_sit("S1", "Innstilt Stabekk–Ski"), advice]),
        (_mi("07:48", verb="cancelled"), [_sit("S2", "Innstilt Lysaker–Ski"), _sit("S_advice2", "TA NESTE TOG")]),
    ]
    assert _nb(trains) == "2 tog er innstilt"
    assert _en(trains) == "2 trains are cancelled"
    for phrase in nw.CAUSE_STOP_PHRASES:
        assert phrase == phrase.lower().strip().rstrip("."), phrase  # stored normalized, or never matched
        shared = [(_mi("07:18"), [_sit("S1", phrase.capitalize())]), (_mi("07:48", verb="none"), [_sit("S2", phrase)])]
        assert _nb(shared) == "Tog L1 07:18 kan bli forsinket, 1 til med mulig avvik", phrase
        assert _en(shared) == "Train L1 07:18 may be delayed, 1 more with possible disruption", phrase
    # A replacement-transport notice is not advice and does lead.
    trains = [
        (_mi("07:18", verb="cancelled"), [_sit("S1", "Buss for tog"), advice]),
        (_mi("07:48", verb="cancelled"), [_sit("S2", "Buss for tog"), advice]),
    ]
    assert _nb(trains) == "Buss for tog: 2 tog er innstilt"
    assert _en(trains) == "Rail replacement bus: 2 trains are cancelled"


def test_tomorrows_trains_are_counted_apart_and_the_noun_follows_the_mode():
    """«i dag» is said exactly when «i morgen» is (the owner's call, 2026-09-25): a sentence about
    one day needs no day, and one about two must name both."""
    trains = [
        (_mi("07:18", verb="cancelled"), [_sit("S1")]),
        (_mi("07:18", verb="cancelled", tomorrow=True), [_sit("S1")]),
        (_mi("07:48", tomorrow=True), [_sit("S1")]),
    ]
    assert _nb(trains) == "Signalfeil ved Asker: 1 tog er innstilt i dag og 2 i morgen"
    assert _en(trains) == "Signal failure at Asker: 1 train is cancelled today and 2 tomorrow"
    assert _nb(trains[1:]) == (
        "Signalfeil ved Asker: 1 tog er innstilt og 1 kan bli forsinket i morgen"
    )
    assert _en(trains[1:]) == "Signal failure at Asker: 1 train is cancelled and 1 may be delayed tomorrow"
    # The owner's example: a shared worst outcome, so counts alone, today's tagged beside tomorrow's.
    many = [(_mi(f"{6 + n // 4:02d}:{15 * (n % 4):02d}", verb="cancelled"), [_sit(f"S{n}", f"Innstilt {n}")])
            for n in range(14)]
    many += [(_mi("07:00", verb="cancelled", tomorrow=True), [_sit("T1", "Innstilt t1")]),
             (_mi("08:00", verb="cancelled", tomorrow=True), [_sit("T2", "Innstilt t2")])]
    assert _nb(many) == "14 tog er innstilt i dag og 2 i morgen"
    assert _en(many) == "14 trains are cancelled today and 2 tomorrow"
    # A named lead follows the same rule in both directions: the lead says «i dag» when the rest
    # mentions tomorrow, and the rest says «i dag» when the lead is tomorrow's train.
    assert _nb([(_mi("08:18", verb="cancelled"), [_sit("S2", "Ta neste tog")]), trains[2]]) == (
        "Tog L1 08:18 er innstilt i dag, 1 til kan bli forsinket i morgen"
    )
    assert _en([(_mi("08:18", verb="cancelled"), [_sit("S2", "Ta neste tog")]), trains[2]]) == (
        "Train L1 08:18 is cancelled today, 1 more may be delayed tomorrow"
    )
    assert _nb([
        (_mi("08:18", verb="cancelled", tomorrow=True), [_sit("S2", "Ta neste tog")]),
        (_mi("07:18"), [_sit("S1")]),
        (_mi("07:48"), [_sit("S3", "Ta andre tog")]),
    ]) == "Tog L1 08:18 i morgen er innstilt, 2 til kan bli forsinket i dag"
    assert _en([
        (_mi("08:18", verb="cancelled", tomorrow=True), [_sit("S2", "Ta neste tog")]),
        (_mi("07:18"), [_sit("S1")]),
        (_mi("07:48"), [_sit("S3", "Ta andre tog")]),
    ]) == "Train L1 08:18 tomorrow is cancelled, 2 more may be delayed today"
    assert _nb([(_mi("08:18", verb="cancelled"), [_sit("S2", "Ta neste tog")]), (_mi("09:18"), [_sit("S1")])]) == (
        "Tog L1 08:18 er innstilt, 1 til kan bli forsinket"
    )
    assert _en([(_mi("08:18", verb="cancelled"), [_sit("S2", "Ta neste tog")]), (_mi("09:18"), [_sit("S1")])]) == (
        "Train L1 08:18 is cancelled, 1 more may be delayed"
    )
    bus = [(_mi("07:18", mode="Buss", line="31"), [_sit("S1")]), (_mi("07:48", mode="Buss", line="31"), [_sit("S1")])]
    assert _nb(bus) == "Signalfeil ved Asker: 2 busser kan bli forsinket"
    assert _en(bus) == "Signal failure at Asker: 2 buses may be delayed"
    assert _nb([trains[0], bus[1]]) == "Signalfeil ved Asker: 1 avgang er innstilt og 1 kan bli forsinket"
    assert _en([trains[0], bus[1]]) == "Signal failure at Asker: 1 departure is cancelled and 1 may be delayed"
    one_bus = (_mi("07:18", mode="Buss", line="31", verb="cancelled"), [_sit("S1")])
    assert _nb([one_bus, bus[1]]) == "Signalfeil ved Asker: 1 buss er innstilt og 1 kan bli forsinket"
    assert _en([one_bus, bus[1]]) == "Signal failure at Asker: 1 bus is cancelled and 1 may be delayed"
    metro = [(_mi(hhmm, mode="T-bane", line="5"), [_sit("S1")]) for hhmm in ("07:18", "07:48")]
    assert _nb(metro) == "Signalfeil ved Asker: 2 T-baner kan bli forsinket", "KIX-25"
    assert _en(metro) == "Signal failure at Asker: 2 metro trains may be delayed"


def test_one_train_is_its_own_title_and_no_train_is_nothing_to_say():
    """The counts and the entries' titles come from one branch (`outcome_verb`), so a client
    rendering both never sees a summary count a train under a verb its title does not use."""
    assert nw.alerts_summary([]) == (None, None, None)
    for mi in (_mi("07:18", verb="cancelled"), _mi("07:18", verb="likely"), _mi("07:18"), _mi("07:18", verb="none"),
               _mi("07:18", tomorrow=True)):
        title = nw.notification_title(mi, "A", "B")
        assert nw.outcome_verb(mi) in title, title
        assert _nb([(mi, [_sit("S1")])]) == title
    assert _nb([(_mi("07:18", tomorrow=True), [_sit("S1")])]) == "Tog L1 07:18 i morgen kan bli forsinket"
    # The English title names the train from the same fields, with the mode noun translated.
    english = ["Train L1 07:18 is cancelled", "Train L1 07:18 may be cancelled", "Train L1 07:18 may be delayed",
               "Train L1 07:18: possible disruption", "Train L1 07:18 tomorrow may be delayed"]
    for mi, title in zip((_mi("07:18", verb="cancelled"), _mi("07:18", verb="likely"), _mi("07:18"),
                          _mi("07:18", verb="none"), _mi("07:18", tomorrow=True)), english, strict=True):
        assert _en([(mi, [_sit("S1")])]) == title
    assert _en([(_mi("07:18", line=None), [_sit("S1")])]) == "Train at 07:18 may be delayed"
    assert _en([(_mi("07:18", line=None, mode="Ferje"), [_sit("S1")])]) == "at 07:18 may be delayed"


def test_a_train_that_never_reaches_the_destination_has_its_own_certain_wording():
    """«kjører ikke helt fram», never «er innstilt» (it leaves) and never a forecast verb (ET has
    said so). Ranked next to a cancellation in the summary, confirmed like one in the title, and
    stated verbatim on the template's detail line."""
    mi = {"transportmiddel": "Tog", "linje": "R14", "destinasjon": "Asker", "avgang_kl": "10:59",
          "kommer_ikke_fram": True}
    assert nw.outcome_verb(mi) == "kjører ikke helt fram"
    assert nw.notification_title(mi, "Oslo S", "Asker") == "Tog R14 Asker 10:59 kjører ikke helt fram"
    assert nw.notification_title(mi, "Oslo S", "Asker", confirmed=True).startswith("Bekreftet: ")
    assert nw.DESTINATION_CANCELLED in nw.deterministic_body(mi)
    assert _en([(mi, [_sit("S1")])]) == "Train R14 Asker 10:59 does not run all the way"
    # Worst-first: after a cancellation, ahead of every forecast.
    trains = [(_mi("08:00"), [_sit("S1")]), (mi, [_sit("S2", summary="Innstilt Skøyen-Asker")]),
              (_mi("09:00", verb="cancelled"), [_sit("S3", summary="Innstilt Skøyen-Asker")])]
    assert nw._VERB_ORDER.index("kjører ikke helt fram") == 1
    assert "kjører ikke helt fram" in _nb(trains)


def test_the_nynorsk_line_is_the_same_sentence_in_nynorsk_words():
    """`_NN`'s words through the one `_summary`. Nynorsk names a vehicle as bokmål does, so a title
    differs only in its verb and «i morgon», and a weak participle agrees in the plural."""
    nynorsk = ["Tog L1 07:18 er innstilt", "Tog L1 07:18 kan bli innstilt", "Tog L1 07:18 kan bli forseinka",
               "Tog L1 07:18: mogleg avvik", "Tog L1 07:18 i morgon kan bli forseinka"]
    for mi, title in zip((_mi("07:18", verb="cancelled"), _mi("07:18", verb="likely"), _mi("07:18"),
                          _mi("07:18", verb="none"), _mi("07:18", tomorrow=True)), nynorsk, strict=True):
        assert _nn([(mi, [_sit("S1")])]) == title
    assert _nn([(_mi("07:18", line=None), [_sit("S1")])]) == "Tog kl 07:18 kan bli forseinka"
    short = {"transportmiddel": "Tog", "linje": "R14", "destinasjon": "Asker", "avgang_kl": "10:59",
             "kommer_ikke_fram": True}
    assert _nn([(short, [_sit("S1")])]) == "Tog R14 Asker 10:59 køyrer ikkje heilt fram"
    trains = [(_mi(hhmm, verb=verb), [_sit(f"S{i}", f"Melding {i}")])
              for i, (hhmm, verb) in enumerate((("07:03", "cancelled"), ("07:42", "cancelled"),
                                                ("09:50", "likely"), ("09:51", "likely"), ("09:05", "delay")))]
    assert _nn(trains) == "2 tog er innstilte, 2 kan bli innstilte og 1 kan bli forseinka"
    trains = [(_mi("08:18", verb="cancelled"), [_sit("S2")]), (_mi("07:18"), [_sit("S1")]),
              (_mi("07:48", tomorrow=True), [_sit("S3", "Ta neste tog")])]
    assert _nn(trains) == "Tog L1 08:18 er innstilt i dag, 1 til kan bli forseinka i dag og 1 i morgon"


def test_the_nynorsk_cause_is_the_situations_own_nynorsk_or_none():
    """Chosen on the Norwegian and quoted from `summary_nn`, as English quotes `summary_en`; with no
    nynorsk yet (it is a machine's, and lands a cycle later) the line names the trains without it."""
    bus = [(_mi(hhmm, mode="Buss", line="31"), [_sit("S1")]) for hhmm in ("07:18", "07:48")]
    assert _nn(bus) == "2 bussar kan bli forseinka"
    translated = [(mi, [dict(situations[0], summary_nn="Signalfeil ved Asker stasjon")]) for mi, situations in bus]
    assert _nn(translated) == "Signalfeil ved Asker stasjon: 2 bussar kan bli forseinka"
    assert _nb(translated) == "Signalfeil ved Asker: 2 busser kan bli forsinket"
    placeholder = _sit("KIX:NoSituation:VYG:L1", "Mulig avvik", has_situation=False)
    assert _nn([(_mi("07:18", verb="none"), [placeholder]), (_mi("07:48", verb="none"), [placeholder])]) == (
        "2 tog med mogleg avvik"
    )


def _msg(number, summary, description, summary_en=None, description_en=None, has_situation=True, *,
         summary_nn=None, description_nn=None):
    """One of the payload's `situations`, operator text in every language."""
    return {"situation_number": number, "summary": summary, "description": description,
            "summary_en": summary_en, "description_en": description_en,
            "summary_nn": summary_nn, "description_nn": description_nn, "has_situation": has_situation}


def test_situations_text_drops_vys_superseded_forecast_and_says_the_cause_once():
    # The partner's screenshot (2026-10-18): the forecast left open beside the fact.
    forecast = _msg("S1", "Forsinkelse forventes",
                    "Vi forventer at toget blir forsinket fra Skien. Dette skyldes at togsettet vi trenger er forsinket.",
                    "Delay expected",
                    "We expect the train to be delayed from Skien. This is due to the train set we need being delayed.")
    fact = _msg("S2", "Forsinket",
                "Toget kjørte 40 minutter forsinket fra Skien. Dette skyldes at togsettet vi trenger er forsinket.",
                "Delayed",
                "The train departed 40 minutes late from Skien. This is due to the train set we need being delayed.")
    assert nw.situations_text([forecast, fact]) == (
        "Toget kjørte 40 minutter forsinket fra Skien. Dette skyldes at togsettet vi trenger er forsinket.",
        "The train departed 40 minutes late from Skien. This is due to the train set we need being delayed.",
        None,
    )
    # Alone, the forecast is the news and stays.
    assert nw.situations_text([forecast])[0].startswith("Vi forventer")


def test_situations_text_drops_advice_beside_a_fact_and_keeps_messages_that_differ_in_a_place():
    advice = _msg("S1", "Ta neste tog", "Ta neste tog mellom Høn og Sandvika.",
                  "Take the next train", "Take the next train between Høn and Sandvika.")
    other = _msg("S2", "Ta andre tog", "Ta andre tog fra Asker, Sandvika og Lysaker.")
    fact = _msg("S3", "Ny kjørerute mellom Asker og Lysaker",
                "Toget stopper ikke på Høn og Stabekk. Det skyldes at toget er forsinket.",
                "New route", "The train does not stop at Høn and Stabekk.")
    ours = _msg("KIX:NoSituation:t1", "Mulig avvik", "Skrevet av oss.", has_situation=False)
    # The untranslated advice is gone, so it no longer blanks the English line.
    assert nw.situations_text([advice, ours, other, fact])[:2] == (
        "Toget stopper ikke på Høn og Stabekk. Det skyldes at toget er forsinket.",
        "The train does not stop at Høn and Stabekk.",
    )
    # Another mode is not a train the alternative departure can name, so it stays.
    bus = _msg("S4", "Ta Ruters buss", "Ta Ruters buss mellom Asker og Sandvika.")
    assert nw.situations_text([advice, fact, bus])[0].endswith("Ta Ruters buss mellom Asker og Sandvika.")
    # Advice alone is all the operator said, so it stays.
    assert nw.situations_text([advice, ours, other])[:2] == (
        "Ta neste tog mellom Høn og Sandvika. Ta andre tog fra Asker, Sandvika og Lysaker.", None)
    # «11. oktober» and «kl. 14:00» are not sentence breaks, so a different date is not cut down to
    # «Ny oppdatering kommer 12.» by the shared «oktober kl. 14:00.».
    a = _msg("S1", "Stengt: Lysaker–Stabekk", "Lysaker–Stabekk er stengt. Ny oppdatering kommer 11. oktober kl. 14:00.")
    b = _msg("S2", "Stengt: Lysaker–Oslo S", "Lysaker–Oslo S er stengt. Ny oppdatering kommer 12. oktober kl. 14:00.")
    assert nw.situations_text([a, b])[0] == (
        "Lysaker–Stabekk er stengt. Ny oppdatering kommer 11. oktober kl. 14:00. "
        "Lysaker–Oslo S er stengt. Ny oppdatering kommer 12. oktober kl. 14:00."
    )


def test_situations_text_never_mixes_languages_and_is_none_without_an_operator_message():
    with_en = _msg("S1", "Signalfeil", "Signalfeil ved Asker.", "Signal failure", "Signal failure at Asker.")
    no_description = _msg("S2", "Innstilt Asker–Skøyen", None, "Cancelled Asker–Skøyen")
    untranslated = _msg("S3", "Buss for tog", "Vi har bestilt buss.")
    # A message with no description contributes its summary, as a sentence.
    assert nw.situations_text([with_en, no_description]) == (
        "Signalfeil ved Asker. Innstilt Asker–Skøyen.", "Signal failure at Asker. Cancelled Asker–Skøyen.", None)
    # One kept message without English: no English line rather than Norwegian inside it.
    assert nw.situations_text([with_en, untranslated]) == ("Signalfeil ved Asker. Vi har bestilt buss.", None, None)
    kix = _msg("KIX:ETCancellation:t1", "Innstilt", "Innstilt.", has_situation=False, summary_nn="Innstilt",
               description_nn="Innstilt.")
    assert nw.situations_text([kix]) == (None, None, None)
    assert nw.situations_text([]) == (None, None, None)
    # Nynorsk is judged on its own: the same kept messages, the same all-or-nothing rule.
    with_nn = _msg("S4", "Signalfeil", "Signalfeil ved Asker.", description_nn="Det er signalfeil ved Asker.")
    bus_nn = _msg("S5", "Innstilt Asker–Skøyen", None, summary_nn="Innstilt Asker–Skøyen")
    assert nw.situations_text([with_nn, bus_nn])[2] == "Det er signalfeil ved Asker. Innstilt Asker–Skøyen."
    assert nw.situations_text([with_nn, untranslated])[2] is None


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
