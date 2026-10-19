"""The English of a situation's summary and description, beside the Norwegian the payload carries.

Three sources, in this order, and the payload says which one it got (`en_machine_translated`):

1. The operator's own English -- the SIRI-SX `summaries`/`descriptions` entries with
   `language = 'EN'`, from the same first version as the Norwegian. Vy publishes it on every
   situation (1 230 of 1 230 in the week to 2026-09-24) and Ruter on nearly every one.
2. For the synthetic `KIX:` rows, the English `predict_calls.sql` writes beside its own Norwegian.
   Both reach this module as `summary_en`/`description_en` on the prediction rows, so they are one
   source here.
3. A Gemini translation of the Norwegian where neither exists: GCO, MOR, NOR, FIN and TRO published
   no English at all that week (0 of 2 765).

The translation never runs on a request path and never holds up a cycle: :func:`update` is called
when the poller swaps its prediction cache, hands what is missing to this module's own small pool and
returns. So a situation can go out once without English and a later read or event carries it.
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

# New translations submitted per poller cycle, and the most ever in flight. A restart finds a few
# hundred bus situations with no English; at ~300 s a cycle this drains them within the hour, and a
# hung Vertex costs at most this many parked tasks rather than one per cycle forever.
MAX_TRANSLATIONS_PER_UPDATE = 25
WORKERS = 4
# Keyed on the Norwegian TEXT, not the situation number: an operator re-publishes one wording under
# many situation numbers. Roughly a kilobyte an entry.
CACHE_MAX = 10_000
# A text Gemini would not translate is not re-asked every cycle.
FAILURE_TTL_S = 3_600
_MAX_OUTPUT_TOKENS = 2048

_PROMPT = """\
Translate this Norwegian public-transport disruption message into English for travellers.
Keep stop names, line names, times and numbers exactly as written. Do not add, drop or explain
anything. The input is JSON with `summary` and `description`; answer with the same two fields, and
keep a field null when it is null in the input."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "nullable": True},
        "description": {"type": "string", "nullable": True},
    },
    "required": ["summary", "description"],
}


@dataclass(frozen=True)
class English:
    summary: str | None
    description: str | None
    machine_translated: bool


_lock = threading.Lock()
_operator: dict[str, English] = {}
_translated: OrderedDict[str, English] = OrderedDict()
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


def english(situation_number: str, summary: str | None, description: str | None) -> English | None:
    """The English for one situation, or None when there is none yet. Never blocks."""
    with _lock:
        found = _operator.get(situation_number)
        if found is not None:
            return found
        return _translated.get(_digest(summary, description))


def update(
    texts: dict[str, tuple[str | None, str | None, str | None, str | None]],
    *,
    translate: bool = True,
) -> None:
    """Swap in this generation's texts -- `situation_number -> (summary, description, summary_en,
    description_en)` -- and submit a translation for each Norwegian text with no English.

    Where the operator's English covers every Norwegian field present, it is used as it stands. Where
    it does not, the WHOLE pair is translated rather than the missing half, so the two English fields
    never come from two sources under one `en_machine_translated`.

    `translate=False` is a follower pod's load (zero-downtime KTD12): it swaps in the operator's own
    English, which is on the table every pod reads, and submits nothing -- otherwise every replica
    would ask Gemini for the same ~400 situations a day. Machine translations are therefore the
    lease holder's alone, and a follower serves a text without one as untranslated.
    """
    operator: dict[str, English] = {}
    missing: dict[str, tuple[str | None, str | None]] = {}
    for number, (summary, description, summary_en, description_en) in texts.items():
        if (summary is None or summary_en) and (description is None or description_en):
            if summary_en or description_en:
                operator[number] = English(summary_en, description_en, False)
        elif summary or description:
            missing.setdefault(_digest(summary, description), (summary, description))
    now = monotonic()
    submit: list[tuple[str, tuple[str | None, str | None]]] = []
    with _lock:
        global _operator
        _operator = operator
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
    except Exception as exc:  # noqa: BLE001 - any failure means "no English yet", never a raise on the pool
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


def _translate(summary: str | None, description: str | None) -> English:
    """One Gemini call. Raises on anything that is not a translation of every field present."""
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
    summary_en, description_en = reply.get("summary"), reply.get("description")
    for source, target in ((summary, summary_en), (description, description_en)):
        if source and not (isinstance(target, str) and target.strip()):
            raise ValueError("a Norwegian field came back untranslated")
    return English(
        summary_en.strip() if summary and summary_en else None,
        description_en.strip() if description and description_en else None,
        True,
    )


def shutdown() -> None:
    """Stop the pool without waiting, so a task parked on a Vertex call cannot hold the process open."""
    _pool.shutdown(wait=False, cancel_futures=True)


def reset_state() -> None:
    """Forget everything -- for the suites, which share one process."""
    global _operator
    with _lock:
        _operator = {}
        _translated.clear()
        _failed.clear()
        _inflight.clear()
