"""situation_english: provided English and nynorsk first, a Gemini translation only where there is none.

Hermetic: `_translate` (the one Vertex call) is replaced by a recorder and the pool by one that runs
each task inline, so a submission and its landing are one step.
"""

import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import llm_message
import situation_english as se
import varsel


class _Inline:
    def submit(self, fn, *args):
        fn(*args)


@contextmanager
def _wired(*, enabled=True, translate=None):
    calls = []

    def recorder(summary, description):
        calls.append((summary, description))
        if translate is not None:
            return translate(summary, description)
        return {
            language: se.Translation(
                f"{language.upper()} {summary}" if summary else None,
                f"{language.upper()} {description}" if description else None,
                True,
            )
            for language in se.LANGUAGES
        }

    saved = (se._translate, se._translation_enabled, se._pool)
    se._translate, se._translation_enabled, se._pool = recorder, (lambda: enabled), _Inline()
    se.reset_state()
    try:
        yield calls
    finally:
        se._translate, se._translation_enabled, se._pool = saved
        se.reset_state()


def test_text_provided_in_both_languages_is_served_as_it_stands_and_nothing_is_translated():
    """A `KIX:` row: predict_calls.sql writes its English and nynorsk beside its Norwegian."""
    with _wired() as calls:
        se.update({"KIX:1": ("Innstilt avgang", "Avgangen er innstilt.", "Cancelled departure",
                             "The departure is cancelled.", "Innstilt avgang", "Avgangen er innstilt.")})
        assert se.english("KIX:1", "Innstilt avgang", "Avgangen er innstilt.") == se.Translation(
            "Cancelled departure", "The departure is cancelled.", False
        )
        assert se.nynorsk("KIX:1", "Innstilt avgang", "Avgangen er innstilt.") == se.Translation(
            "Innstilt avgang", "Avgangen er innstilt.", False
        )
        assert calls == []


def test_the_operators_english_wins_over_the_machines_beside_a_machine_nynorsk():
    """No operator publishes nynorsk, so a Vy situation is translated all the same; its English
    stays the operator's although the same call answered English too."""
    with _wired() as calls:
        se.update({"NSB:1": ("Innstilt", "Toget er innstilt.", "Cancelled", "The train is cancelled.", None, None)})
        assert calls == [("Innstilt", "Toget er innstilt.")]
        assert se.english("NSB:1", "Innstilt", "Toget er innstilt.") == se.Translation(
            "Cancelled", "The train is cancelled.", False
        )
        assert se.nynorsk("NSB:1", "Innstilt", "Toget er innstilt.") == se.Translation(
            "NN Innstilt", "NN Toget er innstilt.", True
        )


def test_a_followers_update_serves_the_operators_english_and_translates_nothing():
    """KTD12: only the lease holder translates, or N pods would translate the same situations. A
    follower still swaps in the operator's own English, or its journey-alerts would carry none where
    the leader's carry it -- and it serves no nynorsk on an operator situation at all."""
    with _wired() as calls:
        se.update(
            {"NSB:1": ("Innstilt", None, "Cancelled", None, None, None),
             "GCO:1": ("Stengt vei", None, None, None, None, None)},
            translate=False,
        )
        assert se.english("NSB:1", "Innstilt", None) == se.Translation("Cancelled", None, False)
        assert se.nynorsk("NSB:1", "Innstilt", None) is None
        assert se.english("GCO:1", "Stengt vei", None) is None
        assert calls == []


def test_a_situation_with_no_translation_is_translated_once_per_text_and_shared_by_every_number_using_it():
    with _wired() as calls:
        assert se.english("GCO:1", "Stengt vei", None) is None
        se.update({"GCO:1": ("Stengt vei", None, None, None, None, None),
                   "GCO:2": ("Stengt vei", None, None, None, None, None)})
        assert calls == [("Stengt vei", None)]
        for number in ("GCO:1", "GCO:2"):
            assert se.english(number, "Stengt vei", None) == se.Translation("EN Stengt vei", None, True)
            assert se.nynorsk(number, "Stengt vei", None) == se.Translation("NN Stengt vei", None, True)
        # A later cycle asks nothing again.
        se.update({"GCO:1": ("Stengt vei", None, None, None, None, None)})
        assert len(calls) == 1


def test_english_covering_only_part_of_the_norwegian_translates_the_whole_pair():
    with _wired() as calls:
        se.update({"GOA:1": ("Forsinket", "Buss 100 er forsinket.", "Delayed", None, None, None)})
        assert calls == [("Forsinket", "Buss 100 er forsinket.")]
        found = se.english("GOA:1", "Forsinket", "Buss 100 er forsinket.")
        assert found.machine_translated and found.summary == "EN Forsinket"


def test_the_egress_kill_switch_stops_every_translation_but_not_the_operators_english():
    with _wired(enabled=False) as calls:
        se.update({"NSB:1": ("Innstilt", None, "Cancelled", None, None, None),
                   "GCO:1": ("Stengt vei", None, None, None, None, None)})
        assert calls == []
        assert se.english("NSB:1", "Innstilt", None) == se.Translation("Cancelled", None, False)
        assert se.nynorsk("NSB:1", "Innstilt", None) is None
        assert se.english("GCO:1", "Stengt vei", None) is None


def test_a_failed_translation_is_not_asked_again_within_its_ttl():
    def refuse(summary, description):
        raise ValueError("no")

    logging.getLogger("situation_english").setLevel(logging.CRITICAL)
    with _wired(translate=refuse) as calls:
        se.update({"GCO:1": ("Stengt vei", None, None, None, None, None)})
        se.update({"GCO:1": ("Stengt vei", None, None, None, None, None)})
        assert len(calls) == 1
        assert se.english("GCO:1", "Stengt vei", None) is None
        se._failed[next(iter(se._failed))] -= se.FAILURE_TTL_S
        se.update({"GCO:1": ("Stengt vei", None, None, None, None, None)})
        assert len(calls) == 2


def test_one_update_submits_at_most_its_ceiling():
    class _Parked:
        def submit(self, fn, *args):
            pass  # never lands: every submission stays in flight

    texts = {f"GCO:{i}": (f"Tekst {i}", None, None, None, None, None) for i in range(3 * se.MAX_TRANSLATIONS_PER_UPDATE)}
    with _wired() as calls:
        se._pool = _Parked()
        se.update(texts)
        assert len(se._inflight) == se.MAX_TRANSLATIONS_PER_UPDATE
        # And a hung backend does not let the next cycle park another batch.
        se.update(texts)
        assert len(se._inflight) == se.MAX_TRANSLATIONS_PER_UPDATE
        assert calls == []


def test_a_test_process_never_translates_even_with_a_project_and_a_provider():
    saved = (llm_message._PROJECT, llm_message._PROVIDER)
    llm_message._PROJECT, llm_message._PROVIDER = "ent-kixavvik-test", "gemini"
    try:
        assert se._translation_enabled() is False
    finally:
        llm_message._PROJECT, llm_message._PROVIDER = saved


def test_one_reply_answers_both_languages_and_one_missing_field_refuses_the_whole_reply():
    """The real `_translate` over a stubbed client: a field present in the Norwegian must come back
    in every language, or nothing lands -- one language's two fields never come from two sources."""

    def client(reply):
        respond = lambda **_: SimpleNamespace(text=json.dumps(reply))
        return lambda: SimpleNamespace(models=SimpleNamespace(generate_content=respond))

    saved = llm_message._gemini_client
    try:
        llm_message._gemini_client = client({"summary_en": " Road closed ", "description_en": None,
                                             "summary_nn": "Stengd veg", "description_nn": "ignored"})
        assert se._translate("Stengt vei", None) == {
            "en": se.Translation("Road closed", None, True),
            "nn": se.Translation("Stengd veg", None, True),
        }
        llm_message._gemini_client = client({"summary_en": "Road closed", "description_en": None,
                                             "summary_nn": "", "description_nn": None})
        try:
            se._translate("Stengt vei", None)
        except ValueError:
            pass
        else:
            raise AssertionError("an untranslated nynorsk field was accepted")
    finally:
        llm_message._gemini_client = saved


def test_the_payload_situations_carry_both_translations_and_say_where_they_came_from():
    def alert(number, summary, description):
        return SimpleNamespace(situation_number=number, summary=summary, description=description, has_situation=True,
                               report_type=None)

    with _wired():
        se.update({
            "NSB:1": ("Innstilt", "Toget er innstilt.", "Cancelled", "The train is cancelled.", None, None),
            "GCO:1": ("Stengt vei", None, None, None, None, None),
        })
        out = varsel._webhook_situations(
            [alert("NSB:1", "Innstilt", "Toget er innstilt."), alert("GCO:1", "Stengt vei", None), alert("X:1", "Ny", None)]
        )
    assert [(s["summary_en"], s["description_en"], s["en_machine_translated"]) for s in out] == [
        ("Cancelled", "The train is cancelled.", False),
        ("EN Stengt vei", None, True),
        (None, None, None),
    ]
    assert [(s["summary_nn"], s["description_nn"], s["nn_machine_translated"]) for s in out] == [
        ("NN Innstilt", "NN Toget er innstilt.", True),
        ("NN Stengt vei", None, True),
        (None, None, None),
    ]
    assert out[0]["summary"] == "Innstilt" and out[0]["description"] == "Toget er innstilt."


def _run():
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} situation translation checks passed.")


if __name__ == "__main__":
    _run()
