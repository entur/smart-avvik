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
    llm_message._PROJECT, llm_message._PROVIDER = "ent-smartavvik-test", "gemini"
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


# ---- The shared translation store (docs/plans/2026-10-22-001, U3/U4) ---------------------------


class _Store:
    """`notification_store`'s three translation calls, in memory: create-if-absent, a seed of
    everything and a sync of what is newer than the watermark. Stamps are a counter."""

    def __init__(self):
        self.docs: dict[str, tuple[dict, int]] = {}
        self.stamp = 0
        self.fail: Exception | None = None
        self.read_fail: Exception | None = None
        self.seed_limit: int | None = None
        self.writes: list[tuple[str, str, str]] = []
        self.calls: list[str] = []

    def put(self, digest, en, nn):
        self.stamp += 1
        self.docs[digest] = (
            {"en": {"summary": en, "description": None}, "nn": {"summary": nn, "description": None}},
            self.stamp,
        )

    def write_situation_translation(self, digest, translations, *, version, model):
        self.calls.append("write")
        if self.fail:
            raise self.fail
        self.writes.append((digest, version, model))
        if digest not in self.docs:
            self.stamp += 1
            self.docs[digest] = (translations, self.stamp)
        return self.docs[digest][0]

    def _since(self, newer):
        rows = sorted(self.docs.items(), key=lambda item: item[1][1])
        entries = [SimpleNamespace(digest=d, translations=t) for d, (t, s) in rows if s > newer]
        return entries, max([s for _, (_, s) in rows], default=newer)

    def seed_situation_translations(self, *, limit, beat=None):
        # Newest first and bounded, as the real seed reads: `seed_limit` stands in for a store
        # holding more than the pod's CACHE_MAX.
        self.calls.append("seed")
        if self.fail:
            raise self.fail
        entries, newest = self._since(0)
        bound = min(limit, self.seed_limit) if self.seed_limit is not None else limit
        return list(reversed(entries))[:bound], newest

    def read_situation_translations(self, digests):
        self.calls.append("read")
        if self.fail or self.read_fail:
            raise self.fail or self.read_fail
        return [SimpleNamespace(digest=d, translations=self.docs[d][0]) for d in digests if d in self.docs]

    def sync_situation_translations(self, *, since, beat=None):
        self.calls.append("sync")
        if self.fail:
            raise self.fail
        return self._since(since)


@contextmanager
def _stored(store=None):
    store = store or _Store()
    with _wired() as calls:
        se.configure(store=store)
        try:
            yield store, calls
        finally:
            se.configure(store=None)


_GCO = {"GCO:1": ("Stengt vei", None, None, None, None, None)}


def test_a_translation_the_leader_wrote_is_served_by_a_follower_after_one_follow():
    """R1/R4: the leader's translation goes to the store, and a follower -- which never translates
    -- serves exactly that text once it has followed."""
    with _stored() as (store, calls):
        se.update(_GCO)
        assert calls == [("Stengt vei", None)]
        digest = se._digest("Stengt vei", None)
        assert store.writes == [(digest, se.TRANSLATION_VERSION, llm_message._GEMINI_MODEL)]

        se.reset_state()  # a different pod: nothing in memory
        se.update(_GCO, translate=False)
        assert se.nynorsk("GCO:1", "Stengt vei", None) is None
        assert se.follow() == 1
        assert se.nynorsk("GCO:1", "Stengt vei", None) == se.Translation("NN Stengt vei", None, True)
        assert se.english("GCO:1", "Stengt vei", None) == se.Translation("EN Stengt vei", None, True)
        assert len(calls) == 1, "the follower asked Gemini nothing"


def test_the_first_follow_seeds_and_later_ones_sync_from_the_watermark():
    """R2: a pod starting -- or taking the lease -- serves every stored translation before its first
    cycle; afterwards only what is newer is read."""
    store = _Store()
    store.put(se._digest("Stengt vei", None), "Road closed", "Stengd veg")
    with _stored(store) as (_, calls):
        assert se.follow() == 1
        assert se.english("GCO:9", "Stengt vei", None) == se.Translation("Road closed", None, True)
        store.put(se._digest("Ny melding", None), "New message", "Ny melding")
        assert se.follow() == 1, "only the new document"
        assert store.calls == ["seed", "sync"]
        assert calls == []


def test_a_leader_that_followed_does_not_translate_what_the_store_holds():
    """R3, AE2: after a handoff the new leader has followed, so its first cycle submits nothing for a
    text another leader already translated."""
    store = _Store()
    store.put(se._digest("Stengt vei", None), "Road closed", "Stengd veg")
    with _stored(store) as (_, calls):
        se.follow()
        se.update(_GCO)
        assert calls == []


def test_a_second_writer_adopts_the_stored_translation():
    """KTD4: a leader that translated a text another leader stored in the meantime serves and keeps
    the STORED text, so every pod converges on one wording (R4)."""
    store = _Store()
    store.put(se._digest("Stengt vei", None), "Stored road closed", "Stored stengd veg")
    with _stored(store) as (_, calls):
        se.update(_GCO)  # no follow first: the race
        assert calls == [("Stengt vei", None)]
        assert se.english("GCO:1", "Stengt vei", None) == se.Translation("Stored road closed", None, True)
        assert se.nynorsk("GCO:1", "Stengt vei", None) == se.Translation("Stored stengd veg", None, True)


def test_a_failing_store_degrades_to_memory_and_never_raises():
    """R5, AE3: the leader still translates into memory, and a follow returns normally."""
    logging.getLogger("situation_english").setLevel(logging.CRITICAL)
    store = _Store()
    store.fail = RuntimeError("firestore down")
    with _stored(store) as (_, calls):
        se.update(_GCO)
        assert len(calls) == 1
        assert se.english("GCO:1", "Stengt vei", None) == se.Translation("EN Stengt vei", None, True)
        assert se.follow() == 0
        assert se.follow() == 0, "and the next follow tries the seed again"
        assert store.calls == ["write", "seed", "seed"]


def test_a_re_read_is_not_applied_again_but_a_different_stored_text_is():
    """The sync's overlap re-reads documents every tick; those are not applied, counted or logged
    again. A leader that kept its own translation because the store write failed still adopts the
    stored one when it reads it, so the pods converge (KTD4)."""
    logging.getLogger("situation_english").setLevel(logging.CRITICAL)
    store = _Store()
    with _stored(store) as (_, calls):
        store.fail = RuntimeError("write lost")
        se.update(_GCO)  # translated, kept in memory, not stored
        store.fail = None
        assert se.english("GCO:1", "Stengt vei", None) == se.Translation("EN Stengt vei", None, True)
        store.put(se._digest("Stengt vei", None), "Another leader's", "Ein annan")
        assert se.follow() == 1
        assert se.english("GCO:1", "Stengt vei", None) == se.Translation("Another leader's", None, True)
        store.stamp += 1  # the same documents again, as the overlap re-reads them
        se._watermark = 0
        assert se.follow() == 0
        assert len(calls) == 1


def test_a_failed_store_write_is_retried_by_the_next_leader_update_without_asking_gemini_again():
    """A leader whose write failed would otherwise hold the only copy: no follower could serve it,
    and the leader never translates it again. The next update writes it from memory."""
    logging.getLogger("situation_english").setLevel(logging.CRITICAL)
    store = _Store()
    with _stored(store) as (_, calls):
        store.fail = RuntimeError("write lost")
        se.update(_GCO)
        store.fail = None
        digest = se._digest("Stengt vei", None)
        assert digest not in store.docs
        se.update(_GCO)
        assert digest in store.docs, "the retry landed"
        assert store.docs[digest][0]["nn"]["summary"] == "NN Stengt vei"
        assert len(calls) == 1, "from memory, not a second Gemini call"
        se.update(_GCO)
        assert store.calls.count("write") == 2, "and once stored, never written again"


def test_a_retried_write_adopts_the_text_another_leader_stored_meanwhile():
    """KTD4 on the retry: the store already holds another leader's text, so the write hands that
    back and this leader serves it -- no follow needed for the pods to converge."""
    logging.getLogger("situation_english").setLevel(logging.CRITICAL)
    store = _Store()
    with _stored(store) as (_, calls):
        store.fail = RuntimeError("write lost")
        se.update(_GCO)
        store.fail = None
        store.put(se._digest("Stengt vei", None), "Another leader's", "Ein annan")
        se.update(_GCO)
        assert se.english("GCO:1", "Stengt vei", None) == se.Translation("Another leader's", None, True)
        assert len(calls) == 1


def test_a_wanted_text_the_seed_did_not_reach_is_read_by_id():
    """The seed reads only the newest CACHE_MAX; a live situation's older text is fetched by digest
    so a restarted pod still serves it."""
    store = _Store()
    store.put(se._digest("Stengt vei", None), "Road closed", "Stengd veg")
    store.put(se._digest("Ny melding", None), "New message", "Ny melding")
    store.seed_limit = 1  # the seed brings only the newest, not the wanted older one
    with _stored(store) as (_, calls):
        se.update(_GCO, translate=False)
        assert se.follow() == 2
        assert se.english("GCO:1", "Stengt vei", None) == se.Translation("Road closed", None, True)
        assert store.calls == ["seed", "read"]
        se.follow()
        assert store.calls == ["seed", "read", "sync"], "nothing is read by id once it is held"
        assert calls == []


def test_a_failing_read_by_id_keeps_what_the_seed_applied():
    logging.getLogger("situation_english").setLevel(logging.CRITICAL)
    store = _Store()
    store.put(se._digest("Stengt vei", None), "Road closed", "Stengd veg")
    store.put(se._digest("Ny melding", None), "New message", "Ny melding")
    store.seed_limit = 1
    store.read_fail = RuntimeError("firestore down")
    with _stored(store):
        se.update(_GCO, translate=False)
        assert se.follow() == 1
        assert se.english("GCO:9", "Ny melding", None) == se.Translation("New message", None, True)
        assert se.english("GCO:1", "Stengt vei", None) is None
        assert se._watermark is not None, "the seed counts: the next follow syncs"


def test_the_seed_is_applied_oldest_first_so_the_lru_evicts_the_oldest():
    """The seed comes newest first; inserted in that order, the next entry would evict the NEWEST."""
    saved = se.CACHE_MAX
    se.CACHE_MAX = 3
    store = _Store()
    texts = [f"Tekst {i}" for i in range(5)]
    for text in texts[:4]:
        store.put(se._digest(text, None), f"EN {text}", f"NN {text}")
    try:
        with _stored(store):
            se.follow()  # seeds the newest 3 of 4
            store.put(se._digest(texts[4], None), "EN Tekst 4", "NN Tekst 4")
            se.follow()  # one more evicts one
            held = [t for t in texts if se.english("X", t, None) is not None]
            assert held == texts[2:], held
    finally:
        se.CACHE_MAX = saved


def test_without_a_store_follow_does_nothing():
    """tst and every suite without the store backend: memory only, as before."""
    with _wired() as calls:
        assert se.follow() == 0
        se.update(_GCO)
        assert len(calls) == 1


def test_a_prompt_or_model_change_is_a_new_key():
    """KTD3, AE4: the digest carries the translation version, so a changed prompt, schema or model
    translates again rather than serving words the current prompt would not produce."""
    before = se._digest("Stengt vei", None)
    saved = se.TRANSLATION_VERSION
    se.TRANSLATION_VERSION = "changed"
    try:
        assert se._digest("Stengt vei", None) != before
    finally:
        se.TRANSLATION_VERSION = saved
    assert len(se.TRANSLATION_VERSION) == 8


def test_coverage_is_logged_per_language_with_the_pods_role():
    """U4, KTD7: how many texts need a machine translation and how many this pod holds, per
    language, so a follower whose sync stalled reads differently from the leader."""
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            fields = getattr(record, "fields", {})
            if fields.get("event") == "situation_english_coverage":
                records.append(fields)

    handler = _Capture()
    logger = logging.getLogger("situation_english")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with _stored():
            se.update({
                "NSB:1": ("Innstilt", None, "Cancelled", None, None, None),  # needs nynorsk only
                "GCO:1": ("Stengt vei", None, None, None, None, None),       # needs both
                "KIX:1": ("Ny", None, "New", None, "Ny", None),              # needs neither
            })
            assert records[-1]["role"] == "leader"
            assert records[-1]["needing"] == {"en": 1, "nn": 2}
            assert records[-1]["covered"] == {"en": 1, "nn": 2}

            se.reset_state()
            se.update({"GCO:1": ("Stengt vei", None, None, None, None, None)}, translate=False)
            assert records[-1]["role"] == "follower"
            assert records[-1]["covered"] == {"en": 0, "nn": 0}
            se.follow()
            assert records[-1]["covered"] == {"en": 1, "nn": 1}, "and again after a follow applied something"
    finally:
        logger.removeHandler(handler)


def _run():
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} situation translation checks passed.")


if __name__ == "__main__":
    _run()
