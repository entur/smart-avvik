"""The English and the nynorsk of a situation's summary and description, beside the Norwegian the
payload carries. The module keeps its first name (and its log event) from when it was English only.

Three sources, in this order, and the payload says which one it got (`en_machine_translated`,
`nn_machine_translated`):

1. The operator's own English -- the SIRI-SX `summaries`/`descriptions` entries with
   `language = 'EN'`, from the same first version as the Norwegian. Vy publishes it on every
   situation (1 230 of 1 230 in the week to 2026-09-24) and Ruter on nearly every one. No operator
   publishes nynorsk: over the 14 days to 2026-10-19 the languages were NO, EN, SV and none, so
   there is no `'NN'` extraction to thread through the SQL.
2. For the synthetic `KIX:` rows, the English and nynorsk `predict_calls.sql` writes beside its own
   Norwegian. Both reach this module as `summary_en`/`description_en`/`summary_nn`/`description_nn`
   on the prediction rows, so they are one source here.
3. A Gemini translation of the Norwegian where neither exists: GCO, MOR, NOR, FIN and TRO published
   no English at all that week (0 of 2 765), and every operator situation lacks nynorsk. ONE call
   per Norwegian text answers both languages, and what was provided still wins per language, so a
   Vy situation is served the operator's English beside a machine's nynorsk.

The translation never runs on a request path and never holds up a cycle: :func:`update` is called
when the poller swaps its prediction cache, hands what is missing to this module's own small pool and
returns. So a situation can go out once without a translation and a later read or event carries it.
It is gated on `VARSEL_LLM_PROVIDER` alone -- the egress kill switch -- and not on the two wording
delivery switches, which say which prose a traveller is SENT, not whether Vertex may be asked.

**Every pod serves the same machine translation** (the shared translation store,
docs/plans/2026-10-22-001). Only the lease holder asks Gemini; it stores each answer in Firestore's
`situation_translations` (first writer wins, and a later writer adopts what is stored), and every
pod reads that collection into the same in-memory cache through :func:`follow` -- a seed the first
time, a watermark sync after -- on startup, on every follower tick and on taking the lease. Requests
still read memory only. Without a store (tst, the suites) the module is the single-pod cache it was.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

import alternatives
import llm_message

logger = logging.getLogger(__name__)

#: The languages beside the Norwegian, in the order `update`'s tuples carry them.
LANGUAGES = ("en", "nn")
# New translations submitted per poller cycle, and the most ever in flight. Since nynorsk, a restart
# finds every live situation untranslated (~1 100 at the midnight peak, fewer distinct texts) rather
# than a few hundred bus ones; at a 120-185 s period and ~2.5 s a call over the pool this drains them
# in about half an hour, and a hung Vertex costs at most this many parked tasks rather than one per
# cycle forever.
MAX_TRANSLATIONS_PER_UPDATE = 100
WORKERS = 8
# Keyed on the Norwegian TEXT, not the situation number: an operator re-publishes one wording under
# many situation numbers. Roughly two kilobytes an entry.
CACHE_MAX = 10_000
# A text Gemini would not translate is not re-asked every cycle.
FAILURE_TTL_S = 3_600
# Wanted digests one follow fetches by id when the seed or sync did not bring them: the seed reads
# only the newest CACHE_MAX, and a live situation's text can be older than that.
FOLLOW_READ_MAX = 500
# Four fields out per call since nynorsk, two of them a long bus description.
_MAX_OUTPUT_TOKENS = 4096

_PROMPT = """\
Translate this Norwegian public-transport disruption message for travellers into English and into
Norwegian Nynorsk. Keep stop names, line names, times and numbers exactly as written. Do not add,
drop or explain anything. Where the input is already Nynorsk, the Nynorsk fields repeat it as
written. The input is JSON with `summary` and `description`; answer with `summary_en`,
`description_en`, `summary_nn` and `description_nn`, and keep a field null when its source is null
in the input."""

_SCHEMA = {
    "type": "object",
    "properties": {
        f"{field}_{language}": {"type": "string", "nullable": True}
        for language in LANGUAGES
        for field in ("summary", "description")
    },
    "required": [f"{field}_{language}" for language in LANGUAGES for field in ("summary", "description")],
}


@dataclass(frozen=True)
class Translation:
    """One language's summary and description, and whether a machine wrote them."""

    summary: str | None
    description: str | None
    machine_translated: bool


# What a stored translation was made under: the prompt, the reply schema and the model. Part of
# every digest (KTD3), so a change to any of them translates again rather than serving words the
# current prompt would not produce, and the old documents expire unread.
TRANSLATION_VERSION = hashlib.sha256(
    (_PROMPT + json.dumps(_SCHEMA, sort_keys=True) + llm_message._GEMINI_MODEL).encode("utf-8")
).hexdigest()[:8]

_lock = threading.Lock()
_provided: dict[str, dict[str, Translation]] = {language: {} for language in LANGUAGES}
_translated: OrderedDict[str, dict[str, Translation]] = OrderedDict()
_failed: dict[str, float] = {}
_inflight: set[str] = set()
_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="situation-english")
# The shared store (`notification_store`, injected by main under the store backend, KTD8) and how
# far this pod has read it: None until the first follow, which seeds rather than syncs.
_store: Any | None = None
_watermark: Any | None = None
# Per language, the digests of the latest update's texts that need a machine translation -- what
# the coverage record counts against (KTD7).
_wanted: dict[str, set[str]] = {language: set() for language in LANGUAGES}
# The leader's translations whose store write failed, kept so a later leader update writes them
# again from memory -- otherwise they never reach the store and no follower serves them.
_unstored: dict[str, dict[str, Translation]] = {}


def configure(*, store: Any | None) -> None:
    """Wire the shared store, or None for memory only (tst, the suites). A new store is read from
    scratch: the watermark belongs to the store it was read from."""
    global _store, _watermark
    with _lock:
        _store = store
        _watermark = None
        _unstored.clear()


def _digest(summary: str | None, description: str | None) -> str:
    return hashlib.sha256(
        json.dumps([TRANSLATION_VERSION, summary, description], ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _translation_enabled() -> bool:
    # `generation_enabled`'s first test, and deliberately only that: its latch is the WORDING
    # holder's, and a wording backend latched off says nothing about whether Gemini can translate.
    # Never from a test process: several suites set GOOGLE_CLOUD_PROJECT and reach `update` through
    # the prediction refresh, and a developer's ADC would then call Vertex from a test.
    return (
        llm_message._PROVIDER in ("auto", *llm_message._BACKENDS)
        and bool(llm_message._PROJECT)
        and not alternatives._in_a_test_process()
    )


def _lookup(language: str, situation_number: str, summary: str | None, description: str | None) -> Translation | None:
    with _lock:
        found = _provided[language].get(situation_number)
        if found is not None:
            return found
        machine = _translated.get(_digest(summary, description))
        # `.get`: a stored entry written under a different language set must not raise on a read.
        return machine.get(language) if machine else None


def english(situation_number: str, summary: str | None, description: str | None) -> Translation | None:
    """The English for one situation, or None when there is none yet. Never blocks."""
    return _lookup("en", situation_number, summary, description)


def nynorsk(situation_number: str, summary: str | None, description: str | None) -> Translation | None:
    """The nynorsk for one situation, or None when there is none yet. Never blocks."""
    return _lookup("nn", situation_number, summary, description)


def update(texts: dict[str, tuple[str | None, ...]], *, translate: bool = True) -> None:
    """Swap in this generation's texts -- `situation_number -> (summary, description, summary_en,
    description_en, summary_nn, description_nn)` -- and submit a translation for each Norwegian text
    some language does not cover.

    Per language: where the provided text covers every Norwegian field present, it is used as it
    stands. Where it does not, the WHOLE pair is translated rather than the missing half, so one
    language's two fields never come from two sources under one `*_machine_translated`.

    `translate=False` is a follower pod's load (zero-downtime KTD12): it swaps in the provided texts,
    which are on the table every pod reads, and submits nothing -- otherwise every replica would ask
    Gemini for the same situations. Machine translations are therefore the lease holder's to make; a
    follower serves them once :func:`follow` has read them from the store.
    """
    provided: dict[str, dict[str, Translation]] = {language: {} for language in LANGUAGES}
    missing: dict[str, tuple[str | None, str | None]] = {}
    wanted: dict[str, set[str]] = {language: set() for language in LANGUAGES}
    for number, (summary, description, *given) in texts.items():
        uncovered: list[str] = []
        for i, language in enumerate(LANGUAGES):
            given_summary, given_description = given[2 * i], given[2 * i + 1]
            if (summary is None or given_summary) and (description is None or given_description):
                if given_summary or given_description:
                    provided[language][number] = Translation(given_summary, given_description, False)
            else:
                uncovered.append(language)
        if uncovered and (summary or description):
            key = _digest(summary, description)
            for language in uncovered:
                wanted[language].add(key)
            missing.setdefault(key, (summary, description))
    now = monotonic()
    submit: list[tuple[str, tuple[str | None, str | None]]] = []
    restore: list[tuple[str, dict[str, Translation]]] = []
    with _lock:
        global _provided, _wanted
        _provided = provided
        _wanted = wanted
        if translate and _translation_enabled():
            for key in [k for k, at in _failed.items() if now - at >= FAILURE_TTL_S]:
                del _failed[key]
            for key, pair in missing.items():
                if len(_inflight) >= MAX_TRANSLATIONS_PER_UPDATE:
                    break
                if key in _translated or key in _inflight or key in _failed:
                    continue
                _inflight.add(key)
                submit.append((key, pair))
        if translate and _store is not None:
            # A store write is not a Gemini call, so the egress switch does not gate the retry.
            for key, result in list(_unstored.items()):
                if len(restore) >= MAX_TRANSLATIONS_PER_UPDATE:
                    break
                if key not in _inflight:
                    _inflight.add(key)
                    restore.append((key, result))
    for key, pair in submit:
        _pool.submit(_translate_one, key, *pair)
    for key, result in restore:
        _pool.submit(_store_one, key, result)
    if submit:
        logger.info(
            "Submitted %d situation translations",
            len(submit),
            extra={"fields": {"event": "situation_english", "submitted": len(submit), "missing": len(missing)}},
        )
    _log_coverage("leader" if translate else "follower")


def _log_coverage(role: Literal["leader", "follower", "follow"]) -> None:
    """KTD7: per language, the texts needing a machine translation and how many this pod holds. A
    follower reading short of the leader for long is a stalled sync; a log, not a gauge (D3)."""
    with _lock:
        needing = {language: len(_wanted[language]) for language in LANGUAGES}
        covered = {
            language: sum(
                1
                for digest in _wanted[language]
                if digest in _translated and _translated[digest].get(language) is not None
            )
            for language in LANGUAGES
        }
    logger.info(
        "Situation translation coverage",
        extra={
            "fields": {
                "event": "situation_english_coverage",
                "role": role,
                "needing": needing,
                "covered": covered,
            }
        },
    )


def _translate_one(key: str, summary: str | None, description: str | None) -> None:
    try:
        result = _translate(summary, description)
    except Exception as exc:  # noqa: BLE001 - any failure means "no translation yet", never a raise on the pool
        # The class only: the texts are an operator's public words, but a log line is not where the
        # payload's prose belongs.
        logger.warning(
            "Situation translation failed (%s)",
            type(exc).__name__,
            extra={"fields": {"event": "situation_english", "result": "error", "error": type(exc).__name__}},
        )
        with _lock:
            _inflight.discard(key)
            _failed[key] = monotonic()
        return
    _store_one(key, result)


def _store_one(key: str, result: dict[str, Translation]) -> None:
    """Store one translation and cache what the store holds. On a store failure, cache this one
    and keep it in `_unstored` for the next leader update to write again. Never raises."""
    store = _store
    if store is not None:
        # First writer wins (KTD4): the store hands back what it holds, which is this answer or an
        # earlier leader's, and that is what this pod caches -- the text every other pod syncs.
        try:
            result = _from_stored(
                store.write_situation_translation(
                    key,
                    {
                        language: {"summary": t.summary, "description": t.description}
                        for language, t in result.items()
                    },
                    version=TRANSLATION_VERSION,
                    model=llm_message._GEMINI_MODEL,
                )
            ) or result
        except Exception as exc:  # noqa: BLE001 - a store failure keeps the translation in memory (R5)
            logger.warning(
                "Could not store a situation translation (%s)",
                type(exc).__name__,
                extra={"fields": {"event": "situation_english", "result": "store_error", "error": type(exc).__name__}},
            )
            with _lock:
                _unstored[key] = result
                # ponytail: drops the oldest past CACHE_MAX; a store down that long loses those writes.
                while len(_unstored) > CACHE_MAX:
                    del _unstored[next(iter(_unstored))]
        else:
            with _lock:
                _unstored.pop(key, None)
    _remember({key: result})


def _from_stored(translations: Any) -> dict[str, Translation] | None:
    """A stored `translations` map as this module's entries, or None when it is not one."""
    if not isinstance(translations, dict):
        return None
    out = {
        language: Translation(fields.get("summary"), fields.get("description"), True)
        for language, fields in translations.items()
        if isinstance(fields, dict)
    }
    return out or None


def _remember(entries: dict[str, dict[str, Translation]]) -> None:
    with _lock:
        for key, result in entries.items():
            _inflight.discard(key)
            _failed.pop(key, None)
            _translated[key] = result
            _translated.move_to_end(key)
        while len(_translated) > CACHE_MAX:
            _translated.popitem(last=False)


def follow(*, beat: Callable[[], None] | None = None) -> int:
    """Read the store's translations into this pod's memory: a seed the first time, a watermark
    sync after. Returns how many entries were new or changed -- the sync's overlap re-reads the last
    120 s every tick, and those are not counted, logged or moved in the LRU. Never raises (R5): a
    failed read keeps what this pod holds, and the next call tries again from the same watermark.
    After the seed or sync, the digests the latest update wanted and this pod still lacks are read
    by id (at most FOLLOW_READ_MAX).

    Called by main in the lifespan and on every follower tick (`follow_last_build`), and on taking
    the lease, so a new leader does not translate again what another leader stored (AE2).
    """
    global _watermark
    with _lock:
        store, watermark = _store, _watermark
    if store is None:
        return 0
    try:
        if watermark is None:
            entries, newest = store.seed_situation_translations(limit=CACHE_MAX, beat=beat)
        else:
            entries, newest = store.sync_situation_translations(since=watermark, beat=beat)
    except Exception as exc:  # noqa: BLE001 - never a reason to fail a tick, the lifespan or an acquisition
        logger.warning(
            "Could not read the stored situation translations (%s)",
            type(exc).__name__,
            extra={"fields": {"event": "situation_english", "result": "follow_error", "error": type(exc).__name__}},
        )
        return 0
    # The seed comes newest first; applied oldest first, so the newest is the last the LRU evicts.
    stored = {
        entry.digest: found
        for entry in (reversed(entries) if watermark is None else entries)
        if (found := _from_stored(entry.translations)) is not None
    }
    with _lock:
        absent = sorted(
            digest for digest in set().union(*_wanted.values()) if digest not in _translated and digest not in stored
        )[:FOLLOW_READ_MAX]
    if absent:
        # What the latest update wants and neither memory nor this read brought: the seed's newest
        # CACHE_MAX can miss a live text stored earlier.
        try:
            for entry in store.read_situation_translations(absent):
                if (found := _from_stored(entry.translations)) is not None:
                    stored[entry.digest] = found
        except Exception as exc:  # noqa: BLE001 - what the seed or sync read still applies
            logger.warning(
                "Could not read the wanted situation translations (%s)",
                type(exc).__name__,
                extra={"fields": {"event": "situation_english", "result": "follow_error", "error": type(exc).__name__}},
            )
    with _lock:
        # In the store already: nothing left for the leader to write again.
        for digest in stored:
            _unstored.pop(digest, None)
        # Changed, not merely absent: a leader that kept its own translation after a failed store
        # write must still adopt the stored one when it reads it (KTD4).
        applied = {digest: found for digest, found in stored.items() if _translated.get(digest) != found}
    if applied:
        _remember(applied)
    with _lock:
        if _store is store and newest is not None:
            _watermark = newest
    if applied:
        _log_coverage("follow")
    return len(applied)


def _translate(summary: str | None, description: str | None) -> dict[str, Translation]:
    """One Gemini call, every language. Raises on anything that is not a translation of every field
    present into every language."""
    client = llm_message._gemini_client()
    response = client.models.generate_content(
        model=llm_message._GEMINI_MODEL,
        contents=json.dumps({"summary": summary, "description": description}, ensure_ascii=False),
        config={
            "system_instruction": _PROMPT,
            "max_output_tokens": _MAX_OUTPUT_TOKENS,
            # `llm_message._gemini_write` explains both: thinking spends the output budget, and 3.7
            # Flash refuses "minimal".
            "thinking_config": {"thinking_level": "low"},
            "response_mime_type": "application/json",
            "response_schema": _SCHEMA,
        },
    )
    reply = json.loads(response.text or "")
    out: dict[str, Translation] = {}
    for language in LANGUAGES:
        target_summary, target_description = reply.get(f"summary_{language}"), reply.get(f"description_{language}")
        for source, target in ((summary, target_summary), (description, target_description)):
            if source and not (isinstance(target, str) and target.strip()):
                raise ValueError(f"a Norwegian field came back untranslated ({language})")
        out[language] = Translation(
            target_summary.strip() if summary and target_summary else None,
            target_description.strip() if description and target_description else None,
            True,
        )
    return out


def shutdown() -> None:
    """Stop the pool without waiting, so a task parked on a Vertex call cannot hold the process open."""
    _pool.shutdown(wait=False, cancel_futures=True)


def reset_state() -> None:
    """Forget everything -- for the suites, which share one process. The injected store stays; the
    watermark goes with the memory, so the next :func:`follow` seeds."""
    global _provided, _wanted, _watermark
    with _lock:
        _provided = {language: {} for language in LANGUAGES}
        _wanted = {language: set() for language in LANGUAGES}
        _watermark = None
        _translated.clear()
        _failed.clear()
        _inflight.clear()
        _unstored.clear()
