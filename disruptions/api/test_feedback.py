from __future__ import annotations

import json
import uuid

import feedback


def _body(**updates):
    value = {
        "context_id": "a" * 32,
        "response_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "revision": 1,
        "rating": "down",
        "comment": "Uklart",
    }
    value.update(updates)
    return feedback.parse(json.dumps(value).encode())


def test_strict_validation_rejects_extra_and_oversized_comment():
    for update in ({"device_id": "secret"}, {"comment": "x" * 501}, {"rating": "maybe"}):
        try:
            _body(**update)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid public payload was accepted")


def test_capture_is_allowlisted_and_personless(monkeypatch):
    monkeypatch.setenv("POSTHOG_HOST", "https://eu.i.posthog.com")
    captured = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return None

    def fake_open(request, timeout):
        captured.update(json.loads(request.data))
        assert request.full_url == "https://eu.i.posthog.com/i/v0/e/"
        assert timeout == 3
        return Response()

    monkeypatch.setattr(feedback, "_token", lambda: "phc_test")
    monkeypatch.setattr(feedback, "urlopen", fake_open)
    body = _body()
    feedback._capture(body, environment="dev", context_date="2026-08-26", submitted_at="now")
    assert captured["properties"]["distinct_id"].startswith("fb_")
    assert captured["properties"]["distinct_id"] != str(body.response_id)
    assert captured["properties"]["$process_person_profile"] is False
    assert captured["properties"]["$insert_id"] == str(body.event_id)
    forbidden = {"device_id", "journey_id", "wording", "route", "prompt", "user_agent"}
    assert forbidden.isdisjoint(captured["properties"])
    # The $ip field must be OCCUPIED, not omitted and not null. Measured against the real project
    # 2026-08-28: PostHog reads an absent, null or empty $ip as "not supplied" and stamps the socket
    # address, while a valid value survives — so `del properties["$ip"]` or a None here would put the
    # caller's real address in a store with no per-event deletion, and the project's "discard client
    # IP data" setting did not suppress it.
    assert captured["properties"]["$ip"] == "0.0.0.0"


def test_receipt_binds_response_to_context(monkeypatch):
    monkeypatch.setenv("VARSEL_FEEDBACK_RECEIPT_SECRET", "s" * 32)
    from datetime import UTC, datetime, timedelta
    body = _body()
    receipt = feedback._receipt(
        body.response_id, body.context_id, "2026-08-26", datetime.now(UTC) + timedelta(hours=1)
    )
    assert feedback._verify(receipt, body.response_id, body.context_id) == "2026-08-26"
    assert not feedback._verify(receipt, uuid.uuid4(), body.context_id)


def test_revision_requires_a_well_formed_receipt(monkeypatch):
    monkeypatch.setattr(feedback, "enabled", lambda: True)
    body = _body(revision=2)
    assert feedback.submit(body, environment="dev") == {"status": "invalid_receipt"}
    body.receipt = "not-a-receipt"
    assert feedback.submit(body, environment="dev") == {"status": "invalid_receipt"}


def test_enabled_requires_explicit_flag_and_fixed_eu_host(monkeypatch):
    monkeypatch.setattr(feedback, "_token", lambda: "phc_test")
    monkeypatch.setattr(feedback, "_secret", lambda: b"s" * 32)
    monkeypatch.setattr(feedback.feedback_contexts, "enabled", lambda: True)
    monkeypatch.setenv("POSTHOG_HOST", "https://eu.i.posthog.com")
    assert not feedback.enabled()
    monkeypatch.setenv("VARSEL_FEEDBACK_ENABLED", "true")
    assert feedback.enabled()
    monkeypatch.setenv("POSTHOG_HOST", "https://example.invalid")
    assert not feedback.enabled()


def test_submit_persists_context_before_capture_and_returns_receipt(monkeypatch):
    order = []
    monkeypatch.setattr(feedback, "enabled", lambda: True)
    monkeypatch.setattr(feedback.feedback_contexts, "resolve", lambda context_id: {"displayed_text": "Hei"})
    monkeypatch.setattr(feedback.feedback_contexts, "persist", lambda context_id, **kwargs: (order.append("bq") or True, None))
    monkeypatch.setattr(feedback, "_capture", lambda *args, **kwargs: order.append("posthog"))
    monkeypatch.setattr(feedback, "_claim", lambda context_id: True)
    monkeypatch.setenv("VARSEL_FEEDBACK_RECEIPT_SECRET", "s" * 32)
    result = feedback.submit(_body(), environment="dev")
    assert result["status"] == "saved"
    assert result["receipt"]
    assert order == ["bq", "posthog"]


def test_submit_stops_before_posthog_when_context_persistence_fails(monkeypatch):
    monkeypatch.setattr(feedback, "enabled", lambda: True)
    monkeypatch.setattr(feedback.feedback_contexts, "resolve", lambda context_id: {"displayed_text": "Hei"})
    monkeypatch.setattr(feedback.feedback_contexts, "persist", lambda context_id, **kwargs: (False, "downstream"))
    monkeypatch.setattr(feedback, "_claim", lambda context_id: True)
    monkeypatch.setattr(feedback, "_capture", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("called")))
    assert feedback.submit(_body(), environment="dev") == {"status": "downstream"}


def test_unknown_context_does_not_consume_budget(monkeypatch):
    monkeypatch.setattr(feedback, "enabled", lambda: True)
    monkeypatch.setattr(feedback.feedback_contexts, "resolve", lambda context_id: None)
    monkeypatch.setattr(
        feedback, "_claim", lambda context_id: (_ for _ in ()).throw(AssertionError("claimed"))
    )
    assert feedback.submit(_body(), environment="dev") == {"status": "expired"}


def test_signed_revision_survives_display_lease_and_reuses_context_date(monkeypatch):
    from datetime import UTC, datetime, timedelta

    first = _body()
    receipt = feedback._receipt(
        first.response_id,
        first.context_id,
        "2026-08-26",
        datetime.now(UTC) + timedelta(hours=1),
    )
    captured = {}
    monkeypatch.setattr(feedback, "enabled", lambda: True)
    monkeypatch.setattr(feedback, "_claim", lambda context_id: True)
    monkeypatch.setattr(
        feedback.feedback_contexts,
        "resolve",
        lambda context_id: (_ for _ in ()).throw(AssertionError("resolved")),
    )
    monkeypatch.setattr(feedback, "_capture", lambda body, **kwargs: captured.update(kwargs))
    revised = _body(
        response_id=str(first.response_id), revision=2, receipt=receipt, comment="Bedre"
    )
    assert feedback.submit(revised, environment="dev")["status"] == "saved"
    assert captured["context_date"] == "2026-08-26"
