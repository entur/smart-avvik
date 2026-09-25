"""situation_english: the operator's English first, a Gemini translation only where there is none.

Hermetic: `_translate` (the one Vertex call) is replaced by a recorder and the pool by one that runs
each task inline, so a submission and its landing are one step.
"""

import logging
from contextlib import contextmanager
from types import SimpleNamespace

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
        return se.English(f"EN {summary}" if summary else None, f"EN {description}" if description else None, True)

    saved = (se._translate, se._translation_enabled, se._pool)
    se._translate, se._translation_enabled, se._pool = recorder, (lambda: enabled), _Inline()
    se.reset_state()
    try:
        yield calls
    finally:
        se._translate, se._translation_enabled, se._pool = saved
        se.reset_state()


def test_the_operators_english_is_served_as_it_stands_and_nothing_is_translated():
    with _wired() as calls:
        se.update({"NSB:1": ("Innstilt", "Toget er innstilt.", "Cancelled", "The train is cancelled.")})
        assert se.english("NSB:1", "Innstilt", "Toget er innstilt.") == se.English(
            "Cancelled", "The train is cancelled.", False
        )
        assert calls == []


def test_a_situation_with_no_english_is_translated_once_per_text_and_shared_by_every_number_using_it():
    with _wired() as calls:
        assert se.english("GCO:1", "Stengt vei", None) is None
        se.update({"GCO:1": ("Stengt vei", None, None, None), "GCO:2": ("Stengt vei", None, None, None)})
        assert calls == [("Stengt vei", None)]
        assert se.english("GCO:1", "Stengt vei", None) == se.English("EN Stengt vei", None, True)
        assert se.english("GCO:2", "Stengt vei", None) == se.English("EN Stengt vei", None, True)
        # A later cycle asks nothing again.
        se.update({"GCO:1": ("Stengt vei", None, None, None)})
        assert len(calls) == 1


def test_english_covering_only_part_of_the_norwegian_translates_the_whole_pair():
    with _wired() as calls:
        se.update({"GOA:1": ("Forsinket", "Buss 100 er forsinket.", "Delayed", None)})
        assert calls == [("Forsinket", "Buss 100 er forsinket.")]
        found = se.english("GOA:1", "Forsinket", "Buss 100 er forsinket.")
        assert found.machine_translated and found.summary == "EN Forsinket"


def test_the_egress_kill_switch_stops_every_translation_but_not_the_operators_english():
    with _wired(enabled=False) as calls:
        se.update({"NSB:1": ("Innstilt", None, "Cancelled", None), "GCO:1": ("Stengt vei", None, None, None)})
        assert calls == []
        assert se.english("NSB:1", "Innstilt", None) == se.English("Cancelled", None, False)
        assert se.english("GCO:1", "Stengt vei", None) is None


def test_a_failed_translation_is_not_asked_again_within_its_ttl():
    def refuse(summary, description):
        raise ValueError("no")

    logging.getLogger("situation_english").setLevel(logging.CRITICAL)
    with _wired(translate=refuse) as calls:
        se.update({"GCO:1": ("Stengt vei", None, None, None)})
        se.update({"GCO:1": ("Stengt vei", None, None, None)})
        assert len(calls) == 1
        assert se.english("GCO:1", "Stengt vei", None) is None
        se._failed[next(iter(se._failed))] -= se.FAILURE_TTL_S
        se.update({"GCO:1": ("Stengt vei", None, None, None)})
        assert len(calls) == 2


def test_one_update_submits_at_most_its_ceiling():
    class _Parked:
        def submit(self, fn, *args):
            pass  # never lands: every submission stays in flight

    with _wired() as calls:
        se._pool = _Parked()
        se.update({f"GCO:{i}": (f"Tekst {i}", None, None, None) for i in range(3 * se.MAX_TRANSLATIONS_PER_UPDATE)})
        assert len(se._inflight) == se.MAX_TRANSLATIONS_PER_UPDATE
        # And a hung backend does not let the next cycle park another batch.
        se.update({f"GCO:{i}": (f"Tekst {i}", None, None, None) for i in range(3 * se.MAX_TRANSLATIONS_PER_UPDATE)})
        assert len(se._inflight) == se.MAX_TRANSLATIONS_PER_UPDATE
        assert calls == []


def test_a_test_process_never_translates_even_with_a_project_and_a_provider():
    import llm_message

    saved = (llm_message._PROJECT, llm_message._PROVIDER)
    llm_message._PROJECT, llm_message._PROVIDER = "ent-kixavvik-test", "gemini"
    try:
        assert se._translation_enabled() is False
    finally:
        llm_message._PROJECT, llm_message._PROVIDER = saved


def test_the_payload_situations_carry_the_english_and_say_where_it_came_from():
    def alert(number, summary, description):
        return SimpleNamespace(situation_number=number, summary=summary, description=description, has_situation=True)

    with _wired():
        se.update({
            "NSB:1": ("Innstilt", "Toget er innstilt.", "Cancelled", "The train is cancelled."),
            "GCO:1": ("Stengt vei", None, None, None),
        })
        out = varsel._webhook_situations(
            [alert("NSB:1", "Innstilt", "Toget er innstilt."), alert("GCO:1", "Stengt vei", None), alert("X:1", "Ny", None)]
        )
    assert [(s["summary_en"], s["description_en"], s["en_machine_translated"]) for s in out] == [
        ("Cancelled", "The train is cancelled.", False),
        ("EN Stengt vei", None, True),
        (None, None, None),
    ]
    assert out[0]["summary"] == "Innstilt" and out[0]["description"] == "Toget er innstilt."


def _run():
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} situation English checks passed.")


if __name__ == "__main__":
    _run()
