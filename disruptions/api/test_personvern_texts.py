"""Guards on the published privacy texts (plan 2026-08-31-001, U8 — R11, R12, KTD2, KTD8).

Three things nothing else in this repo detects, all of them silent failures:

1. **Text parity.** `/togmelding` is a committed, self-contained vanilla-HTML page with no build
   step, so it cannot import `demo/avviksvarsel/src/personvern.ts` — the single source of every
   published privacy text. It therefore carries DUPLICATED copies of the sender line, the privacy
   href and the experiment disclaimer, and the duplication is guarded rather than shared (KTD8).
   The extraction below asserts every expected constant was actually found *before* it compares
   anything: a regex over a TypeScript export breaks the day somebody reformats the file or moves
   to a template literal, and a guard that then iterates zero extracted strings passes vacuously.
   This repo has already paid for a suite that silently never ran.

2. **Brand-script parity.** `demo/avviksvarsel/personvern/index.html` is a second Vite entry and
   repeats index.html's inline brand-detection script verbatim (Vite has no HTML include). That
   copy is the only thing that keeps `?brand=none` correct on the privacy page — on the one page
   whose job is telling an unbranded visitor who the controller is — and nothing else notices when
   the two drift.

3. **The EU/Google claims.** The published page states that Google Cloud operates the service in
   the EU, that the alert text is written by a language model at Google in the EU, and that
   feedback is processed by PostHog in the EU. None of that follows from a code default: the code
   default provider is `auto` (which would try Claude), and the effective Gemini location resolves
   as `VERTEX_GEMINI_LOCATION or VERTEX_LOCATION or "eu"` (`llm_message.py`) while dev and prd both
   carry `VERTEX_LOCATION: global`. The claims rest on Helm values, so the Helm values are pinned
   here — and pinned EXPLICITLY, because "eu wherever set" would stay green while a deleted Gemini
   pin quietly moved processing to `global`.

4. **Bracket guard.** No bracketed placeholder text (`[…]`) may reach the static page.

Reads files only: no FastAPI, no BigQuery, no network, no new dependencies.

Run: uv run python test_personvern_texts.py  (or pytest)
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

PERSONVERN_TS = _REPO / "demo" / "avviksvarsel" / "src" / "personvern.ts"
TOGMELDING_HTML = _REPO / "disruptions" / "api" / "togmelding_static" / "index.html"
VARSEL_INDEX_HTML = _REPO / "demo" / "avviksvarsel" / "index.html"
PERSONVERN_INDEX_HTML = _REPO / "demo" / "avviksvarsel" / "personvern" / "index.html"
HELM_ENV_DIR = _REPO / "helm" / "kix-avvik" / "env"

# The constants /togmelding duplicates. Named here so a rename in personvern.ts fails loudly
# instead of dropping a text out of the guard.
_SHARED_CONSTANTS = ("TOGMELDING_SENDER_LINE", "TOGMELDING_PERSONVERN_HREF", "TOGMELDING_DISCLAIMER")


class ExtractionError(AssertionError):
    """The TypeScript source could not be parsed — the guard must fail, never pass vacuously."""


# --- A very small TypeScript literal reader -----------------------------------------------------
# personvern.ts is deliberately import-free and literal-only (it is type-stripped by node --test),
# so resolving `export const X = 'a' + 'b'`, `export const Y = OTHER.field` and an object literal's
# fields is enough. Anything else raises rather than guesses.

_QUOTES = "'\"`"
_OPENERS = "([{"
_CLOSERS = ")]}"


def _skip_string(source: str, i: int) -> int:
    """Index just past the string literal starting at `i`."""
    quote = source[i]
    i += 1
    while i < len(source):
        if source[i] == "\\":
            i += 2
            continue
        if source[i] == quote:
            return i + 1
        i += 1
    raise ExtractionError(f"unterminated string literal in {PERSONVERN_TS.name}")


def _declaration(source: str, name: str) -> str:
    """The right-hand side of `[export] const <name> = …;`, brackets and strings respected."""
    match = re.search(rf"^(?:export )?const {re.escape(name)}\b[^=\n]*=\s*", source, re.MULTILINE)
    if match is None:
        raise ExtractionError(
            f"`const {name}` was not found in {PERSONVERN_TS.name}. The /togmelding parity guard "
            "extracts the shared texts by name; a rename or a reformat must fail here rather than "
            "leave the duplicated copies unguarded."
        )
    i, depth = match.end(), 0
    while i < len(source):
        ch = source[i]
        if ch in _QUOTES:
            i = _skip_string(source, i)
            continue
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
        elif ch == ";" and depth == 0:
            return source[match.end() : i]
        i += 1
    raise ExtractionError(f"`const {name}` in {PERSONVERN_TS.name} has no terminating `;`")


def _split_top_level(expr: str, sep: str) -> list[str]:
    parts, start, depth, i = [], 0, 0, 0
    while i < len(expr):
        ch = expr[i]
        if ch in _QUOTES:
            i = _skip_string(expr, i)
            continue
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(expr[start:i])
            start = i + 1
        i += 1
    parts.append(expr[start:])
    return parts


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "`": "`"}


def _unquote(token: str) -> str:
    body, out, i = token[1:-1], [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt == "u" and body[i + 2 : i + 3] == "{":
                end = body.index("}", i)
                out.append(chr(int(body[i + 3 : end], 16)))
                i = end + 1
                continue
            if nxt == "u":
                out.append(chr(int(body[i + 2 : i + 6], 16)))
                i += 6
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def _object_field(expr: str, field: str) -> str:
    expr = expr.strip()
    if not expr.startswith("{"):
        raise ExtractionError(f"expected an object literal for `.{field}`, got: {expr[:60]!r}")
    for entry in _split_top_level(expr[1:-1], ","):
        key, sep, value = entry.partition(":")
        if sep and key.strip().strip("'\"") == field:
            return value
    raise ExtractionError(f"object literal has no `{field}` field: {expr[:60]!r}")


def _resolve(expr: str, source: str, depth: int = 0) -> str:
    """Resolve a string-valued TS expression: literals, `+` concatenation, `NAME`, `NAME.field`."""
    if depth > 8:
        raise ExtractionError(f"reference chain too deep resolving {expr[:60]!r}")
    terms = _split_top_level(expr, "+")
    if len(terms) > 1:
        return "".join(_resolve(term, source, depth + 1) for term in terms)
    token = terms[0].strip()
    if token[:1] in _QUOTES:
        return _unquote(token)
    reference = re.fullmatch(r"([A-Za-z_$][\w$]*)(?:\.([A-Za-z_$][\w$]*))?", token)
    if reference is None:
        raise ExtractionError(
            f"cannot resolve {token[:60]!r} in {PERSONVERN_TS.name} — the parity guard reads "
            "string literals, `+` concatenation and plain references only."
        )
    value = _declaration(source, reference.group(1))
    if reference.group(2):
        value = _object_field(value, reference.group(2))
    return _resolve(value, source, depth + 1)


def _shared_texts() -> dict[str, str]:
    """Every published string /togmelding duplicates, keyed by a label used in failure messages.

    Fails loudly if ANY expected constant is missing or unparseable — the whole point: a guard
    that iterates an empty dict is a guard that has been switched off without anybody noticing.
    """
    source = PERSONVERN_TS.read_text(encoding="utf-8")
    missing = [name for name in _SHARED_CONSTANTS if f"const {name}" not in source]
    if missing:
        raise ExtractionError(
            f"{PERSONVERN_TS.name} no longer declares {', '.join(missing)}. These are the strings "
            "the committed /togmelding page duplicates (it has no build step and cannot import "
            "the module), so losing them silently unguards the duplication."
        )
    texts = {
        "TOGMELDING_SENDER_LINE": _resolve(_declaration(source, "TOGMELDING_SENDER_LINE"), source),
        "TOGMELDING_PERSONVERN_HREF": _resolve(
            _declaration(source, "TOGMELDING_PERSONVERN_HREF"), source
        ),
        "TOGMELDING_DISCLAIMER.title": _resolve(
            _object_field(_declaration(source, "TOGMELDING_DISCLAIMER"), "title"), source
        ),
        "TOGMELDING_DISCLAIMER.body": _resolve(
            _object_field(_declaration(source, "TOGMELDING_DISCLAIMER"), "body"), source
        ),
    }
    for label, value in texts.items():
        if not isinstance(value, str) or len(value.strip()) < 10:
            raise ExtractionError(
                f"{label} resolved to {value!r} — too short to be a published text. The extraction "
                "is broken; treat this as an unguarded duplication, not as a passing check."
            )
    return texts


def _normalized(html: str) -> str:
    """Collapse whitespace runs so markup may wrap freely without defeating a `contains` check."""
    return re.sub(r"\s+", " ", html)


def _static_markup(html: str) -> str:
    """The hand-written markup between `<body>` and the page's inline script.

    The page's JS is full of legitimate `[`/`]` (array literals, indexing); the published texts
    live in this region, and the bracket guard applies to them.
    """
    body = html.index("<body>")
    return html[body : html.index("<script>", body)]


def _brand_script(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    blocks = [
        block for block in re.findall(r"<script>(.*?)</script>", html, re.DOTALL) if "dataset.brand" in block
    ]
    assert len(blocks) == 1, (
        f"{path} has {len(blocks)} inline brand-detection scripts, expected exactly 1 — the "
        "parity guard cannot tell which copy is authoritative."
    )
    return blocks[0]


# --- Tests --------------------------------------------------------------------------------------


def test_the_shared_text_extraction_finds_every_expected_constant():
    """The guard on the guard: extraction failure must be an error, never a vacuous pass."""
    texts = _shared_texts()
    assert set(texts) == {
        "TOGMELDING_SENDER_LINE",
        "TOGMELDING_PERSONVERN_HREF",
        "TOGMELDING_DISCLAIMER.title",
        "TOGMELDING_DISCLAIMER.body",
    }
    assert texts["TOGMELDING_PERSONVERN_HREF"].startswith("/"), (
        "TOGMELDING_PERSONVERN_HREF must resolve to a path; got "
        f"{texts['TOGMELDING_PERSONVERN_HREF']!r}"
    )


def test_the_togmelding_page_carries_the_shared_privacy_texts_verbatim():
    """KTD8: /togmelding duplicates these strings because it cannot import the module."""
    texts = _shared_texts()
    page = _normalized(TOGMELDING_HTML.read_text(encoding="utf-8"))
    for label, value in texts.items():
        if label == "TOGMELDING_PERSONVERN_HREF":
            continue
        assert _normalized(value) in page, (
            f"{TOGMELDING_HTML.name} does not contain {label} from {PERSONVERN_TS.name}:\n"
            f"  {value!r}\n"
            "The two copies must match verbatim — /togmelding has no build step and cannot import "
            "the text module, so this test is the only thing keeping them in step (KTD8)."
        )


def test_the_togmelding_page_links_to_the_privacy_page():
    """R11: the same sender/privacy footer as /varsel, pointing at the canonical path."""
    href = _shared_texts()["TOGMELDING_PERSONVERN_HREF"]
    page = _normalized(TOGMELDING_HTML.read_text(encoding="utf-8"))
    assert f'href="{href}"' in page, (
        f"{TOGMELDING_HTML.name} has no link to {href} (TOGMELDING_PERSONVERN_HREF in "
        f"{PERSONVERN_TS.name}). The footer's «Slik behandler vi opplysningene» link is how a "
        "/togmelding visitor reaches the privacy page at all."
    )


def test_the_togmelding_privacy_texts_carry_no_placeholder_brackets():
    """Acceptance criterion: no bracketed placeholder text reaches production."""
    markup = _static_markup(TOGMELDING_HTML.read_text(encoding="utf-8"))
    for bracket in "[]":
        assert bracket not in markup, (
            f"{TOGMELDING_HTML.name}'s static markup contains {bracket!r} — an unresolved "
            "placeholder from the plan's appendix must never ship."
        )


def test_the_privacy_page_repeats_the_brand_script_byte_for_byte():
    """`?brand=none` on /varsel/personvern/ depends on this duplicated inline script.

    Vite has no HTML include, so the second entry document carries its own copy. Without it the
    unbranded visitor gets the Entur palette on the one page that names the controller — and the
    drift is invisible until somebody loads that URL with the parameter.
    """
    root = _brand_script(VARSEL_INDEX_HTML)
    copy = _brand_script(PERSONVERN_INDEX_HTML)
    assert root == copy, (
        f"The inline brand-detection script in {PERSONVERN_INDEX_HTML.relative_to(_REPO)} has "
        f"drifted from {VARSEL_INDEX_HTML.relative_to(_REPO)}. Vite cannot share it, so the copies "
        "must be kept identical by hand; otherwise `?brand=none` paints the Entur palette on the "
        f"privacy page.\n--- index.html ---\n{root}\n--- personvern/index.html ---\n{copy}"
    )


# --- Claim guards on the Helm values (KTD2, R12) -------------------------------------------------


def _env_files() -> list[Path]:
    files = sorted(HELM_ENV_DIR.glob("values-kub-ent-*.yaml"))
    assert len(files) >= 3, (
        f"expected the dev/tst/prd values files under {HELM_ENV_DIR.relative_to(_REPO)}, found "
        f"{[f.name for f in files]}"
    )
    return files


def _pinned(path: Path, key: str) -> str | None:
    """The single value a values file sets for `key`, ignoring comments; None if it sets none."""
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(rf"\s*{re.escape(key)}:\s*(\S.*?)\s*", line)
        if match:
            values.append(match.group(1).strip().strip("\"'"))
    assert len(values) <= 1, f"{path.name} sets {key} {len(values)} times: {values}"
    return values[0] if values else None


def test_every_environment_pins_gemini_as_the_llm_provider():
    """Protects: «Varselteksten skrives av en språkmodell hos Google i EU» (T5).

    The code default is `provider="auto"`, which would try Claude first. The published sentence
    names Google, so every environment must name Gemini.
    """
    for path in _env_files():
        assert _pinned(path, "VARSEL_LLM_PROVIDER") == "gemini", (
            f"{path.name} does not pin VARSEL_LLM_PROVIDER: gemini. The published privacy page at "
            "/varsel/personvern/ states «Varselteksten skrives av en språkmodell hos Google i EU»; "
            "the code default is `auto`, so that sentence is true only while this value is pinned. "
            "Change the published text before changing this value."
        )


def test_every_environment_explicitly_pins_the_gemini_location_to_eu():
    """Protects: «... hos Google i EU» and «Google Cloud drifter tjenesten ... i EU» (T5).

    Explicitly, not «eu wherever set»: `llm_message.py` resolves the location as
    `VERTEX_GEMINI_LOCATION or VERTEX_LOCATION or "eu"`, and dev and prd both carry
    `VERTEX_LOCATION: global`. Deleting the Gemini pin would move ML processing to the global
    endpoint — where Vertex documents that «you can't control or know which region your ML
    processing requests are sent to» — while a laxer guard stayed green.
    """
    for path in _env_files():
        assert _pinned(path, "VERTEX_GEMINI_LOCATION") == "eu", (
            f"{path.name} does not explicitly set VERTEX_GEMINI_LOCATION: eu. The published "
            "privacy page states that the alert text is written by a language model at Google in "
            "the EU and that processing happens in the EU/EØS. Without this explicit pin the "
            "location falls back to VERTEX_LOCATION — which is `global` in dev and prd — and the "
            "published sentence becomes false. Change the published text before changing this."
        )


def test_production_pins_posthog_to_the_eu_ingestion_host():
    """Protects: «Tilbakemeldinger du sender behandles i analyseverktøyet PostHog i EU» (T5)."""
    prd = HELM_ENV_DIR / "values-kub-ent-prd.yaml"
    assert _pinned(prd, "POSTHOG_HOST") == "https://eu.i.posthog.com", (
        f"{prd.name} does not pin POSTHOG_HOST: https://eu.i.posthog.com. The published privacy "
        "page states that feedback is processed by PostHog in the EU; the US ingestion host would "
        "make that sentence false and would move the transfer out of the EØS claim beside it."
    )


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} privacy-text checks passed.")


if __name__ == "__main__":
    _run()
