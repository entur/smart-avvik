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
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic

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


_lock = threading.Lock()
_provided: dict[str, dict[str, Translation]] = {language: {} for language in LANGUAGES}
_translated: OrderedDict[str, dict[str, Translation]] = OrderedDict()
_failed: dict[str, float] = {}
_inflight: set[str] = set()
_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="situation-english")


def _digest(summary: str | None, description: str | None) -> str:
    return hashlib.sha256(json.dumps([summary, description], ensure_ascii=False).encode("utf-8")).hexdigest()


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
        return machine[language] if machine else None


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
    Gemini for the same situations. Machine translations are therefore the lease holder's alone, and
    a follower serves a text without one as untranslated: since nynorsk, every operator situation's
    `summary_nn` on a follower.
    """
    provided: dict[str, dict[str, Translation]] = {language: {} for language in LANGUAGES}
    missing: dict[str, tuple[str | None, str | None]] = {}
    for number, (summary, description, *given) in texts.items():
        covered = True
        for i, language in enumerate(LANGUAGES):
            given_summary, given_description = given[2 * i], given[2 * i + 1]
            if (summary is None or given_summary) and (description is None or given_description):
                if given_summary or given_description:
                    provided[language][number] = Translation(given_summary, given_description, False)
            else:
                covered = False
        if not covered and (summary or description):
            missing.setdefault(_digest(summary, description), (summary, description))
    now = monotonic()
    submit: list[tuple[str, tuple[str | None, str | None]]] = []
    with _lock:
        global _provided
        _provided = provided
        if not translate or not _translation_enabled():
            return
        for key in [k for k, at in _failed.items() if now - at >= FAILURE_TTL_S]:
            del _failed[key]
        for key, pair in missing.items():
            if len(_inflight) >= MAX_TRANSLATIONS_PER_UPDATE:
                break
            if key in _translated or key in _inflight or key in _failed:
                continue
            _inflight.add(key)
            submit.append((key, pair))
    for key, pair in submit:
        _pool.submit(_translate_one, key, *pair)
    if submit:
        logger.info(
            "Submitted %d situation translations",
            len(submit),
            extra={"fields": {"event": "situation_english", "submitted": len(submit), "missing": len(missing)}},
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
    with _lock:
        _inflight.discard(key)
        _failed.pop(key, None)
        _translated[key] = result
        _translated.move_to_end(key)
        while len(_translated) > CACHE_MAX:
            _translated.popitem(last=False)


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
    """Forget everything -- for the suites, which share one process."""
    global _provided
    with _lock:
        _provided = {language: {} for language in LANGUAGES}
        _translated.clear()
        _failed.clear()
        _inflight.clear()
