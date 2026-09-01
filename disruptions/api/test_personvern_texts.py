"""Guards on the published privacy texts (plan 2026-08-31-001, U8 — R11, R12, KTD2, KTD8).

Four things nothing else in this repo detects, all of them silent failures:

1. **Text parity.** `/togmelding` cannot import `demo/avviksvarsel/src/personvern.ts` — the single
   source of every published privacy text. It therefore carries DUPLICATED copies of the sender
   line, its link text, the privacy href and the experiment disclaimer, and the duplication is
   guarded rather than shared (KTD8). The REASON changed on 2026-09-01 and the arrangement did not:
   the page used to be a committed vanilla-HTML file with no build step, and is now its own npm app
   (`demo/togmelding/`) whose Docker stage copies only its own directory — so a relative import
   into `../avviksvarsel/src/` would resolve on a laptop and fail the image build, which is worse
   than a duplicate. Both copies are now TypeScript modules, so this compares RESOLVED CONSTANTS
   rather than searching markup for a substring: strictly tighter, since a text present but never
   rendered no longer passes. What the markup search did carry is kept as its own check — that the
   app actually renders each constant (see below).
   The extraction asserts every expected constant was actually found *before* it compares
   anything: a regex over a TypeScript export breaks the day somebody reformats the file or moves
   to a template literal, and a guard that then iterates zero extracted strings passes vacuously.
   This repo has already paid for a suite that silently never ran.

2. **Second-entry parity, in both places it was duplicated.** The privacy page is a second Vite
   entry, and Vite shares nothing between entries — so it repeats index.html's inline
   brand-detection script verbatim (`personvern/index.html`) AND main.tsx's `@entur/*` stylesheet
   import block verbatim (`personvern-page.tsx`). Both copies exist only to keep `?brand=none`
   correct on the one page whose job is telling an unbranded visitor who the controller is; both
   drift invisibly. The import block is the nastier of the two, because divergence does not just
   fail to reskin the privacy page — it moves a Linje sheet after `brands/none.css` in the shared
   chunk and un-reskins the whole app, with nothing red anywhere.

3. **The EU/Google claims.** The published page states that Google Cloud operates the service in
   the EU, that the alert text is written by a language model at Google in the EU, and that
   feedback is processed by PostHog in the EU. None of that follows from a code default: the code
   default provider is `auto` (which would try Claude), and the effective Gemini location resolves
   as `VERTEX_GEMINI_LOCATION or VERTEX_LOCATION or "eu"` (`llm_message.py`) while dev and prd both
   carry `VERTEX_LOCATION: global`. The claims rest on Helm values, so the Helm values are pinned
   here — and pinned EXPLICITLY, because "eu wherever set" would stay green while a deleted Gemini
   pin quietly moved processing to `global`.

4. **Bracket guard.** No bracketed placeholder text (`[…]`) may reach a published string. Applied
   to the resolved constants rather than to a slice of markup — the old page's JS was full of
   legitimate brackets, so the guard read the hand-written region between `<body>` and the first
   `<script>`; in a JSX app there is no such region, and the constants are where a placeholder
   from the plan's appendix would actually survive.

Reads files only: no FastAPI, no BigQuery, no network, no new dependencies.

Run: uv run python test_personvern_texts.py  (or pytest)
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

PERSONVERN_TS = _REPO / "demo" / "avviksvarsel" / "src" / "personvern.ts"
# /togmelding's own copy, and the app sources that have to render it. togmelding_static/ is a build
# output now (gitignored, minified, absent in a fresh checkout), so nothing here may read it.
TOGMELDING_TS = _REPO / "demo" / "togmelding" / "src" / "personvern.ts"
TOGMELDING_SRC = _REPO / "demo" / "togmelding" / "src"
VARSEL_INDEX_HTML = _REPO / "demo" / "avviksvarsel" / "index.html"
PERSONVERN_INDEX_HTML = _REPO / "demo" / "avviksvarsel" / "personvern" / "index.html"
MAIN_TSX = _REPO / "demo" / "avviksvarsel" / "src" / "main.tsx"
PERSONVERN_PAGE_TSX = _REPO / "demo" / "avviksvarsel" / "src" / "personvern-page.tsx"
HELM_ENV_DIR = _REPO / "helm" / "kix-avvik" / "env"

# The constants /togmelding duplicates. Named here so a rename on either side fails loudly
# instead of dropping a text out of the guard.
_SHARED_CONSTANTS = (
    "TOGMELDING_SENDER_LINE",
    "TOGMELDING_PERSONVERN_LINK_TEXT",
    "TOGMELDING_PERSONVERN_HREF",
    "TOGMELDING_DISCLAIMER",
)


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


def _shared_texts(path: Path = PERSONVERN_TS) -> dict[str, str]:
    """Every published string /togmelding duplicates, keyed by a label used in failure messages.

    Takes the module path so ONE extractor reads both copies — the /varsel source of truth and
    /togmelding's duplicate. A second, differently-written reader would be a third place for the
    vocabulary to drift.

    Fails loudly if ANY expected constant is missing or unparseable — the whole point: a guard
    that iterates an empty dict is a guard that has been switched off without anybody noticing.
    """
    source = path.read_text(encoding="utf-8")
    missing = [name for name in _SHARED_CONSTANTS if f"const {name}" not in source]
    if missing:
        raise ExtractionError(
            f"{path.relative_to(_REPO)} no longer declares {', '.join(missing)}. These are the "
            "strings /togmelding duplicates (its own npm app, whose Docker stage copies only its "
            "own directory, so it cannot import the /varsel module), so losing them silently "
            "unguards the duplication."
        )
    texts = {
        "TOGMELDING_SENDER_LINE": _resolve(_declaration(source, "TOGMELDING_SENDER_LINE"), source),
        "TOGMELDING_PERSONVERN_LINK_TEXT": _resolve(
            _declaration(source, "TOGMELDING_PERSONVERN_LINK_TEXT"), source
        ),
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


def _togmelding_sources() -> str:
    """Every TypeScript source of the /togmelding app, concatenated.

    Used to assert the app RENDERS what its text module declares. Reads the sources and never
    `togmelding_static/`: that is a Vite output now — gitignored, minified, and absent in a fresh
    checkout, so a guard pointed at it would pass vacuously on any machine that has not built the
    app. Refuses an empty read for the usual reason.
    """
    sources = sorted(
        path for path in TOGMELDING_SRC.rglob("*") if path.suffix in (".ts", ".tsx")
    )
    if len(sources) < 4:
        raise ExtractionError(
            f"found only {len(sources)} TypeScript sources under "
            f"{TOGMELDING_SRC.relative_to(_REPO)} — the app has more than that, so this is a bad "
            "path or a moved app, not a real list."
        )
    return "\n".join(path.read_text(encoding="utf-8") for path in sources)


def _entur_style_imports(path: Path) -> list[str]:
    """The `@entur/*` stylesheet specifiers a Vite entry imports, in source order.

    Order is the whole point (see the test below), so this returns a list and never a set.
    Refuses to return a short list: an extraction that quietly finds nothing would turn the
    parity assertion into `[] == []`, which is the vacuous pass this module exists to avoid.
    """
    source = path.read_text(encoding="utf-8")
    specifiers = re.findall(r"^import\s+['\"](@entur/[^'\"]+\.css)['\"];", source, re.MULTILINE)
    if len(specifiers) < 5:
        raise ExtractionError(
            f"found only {len(specifiers)} `@entur/*` stylesheet imports in "
            f"{path.relative_to(_REPO)}: {specifiers}. Both Vite entries import the whole Linje "
            "sheet set, so this is an extraction failure (a reformat, a different import form), "
            "not a real list — treat it as an unguarded duplication rather than a passing check."
        )
    return specifiers


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
    """The guard on the guard: extraction failure must be an error, never a vacuous pass.

    Run over BOTH modules, because either one going unparseable unguards the duplication.
    """
    for path in (PERSONVERN_TS, TOGMELDING_TS):
        texts = _shared_texts(path)
        assert set(texts) == {
            "TOGMELDING_SENDER_LINE",
            "TOGMELDING_PERSONVERN_LINK_TEXT",
            "TOGMELDING_PERSONVERN_HREF",
            "TOGMELDING_DISCLAIMER.title",
            "TOGMELDING_DISCLAIMER.body",
        }, path
        assert texts["TOGMELDING_PERSONVERN_HREF"].startswith("/"), (
            f"TOGMELDING_PERSONVERN_HREF in {path.relative_to(_REPO)} must resolve to a path; got "
            f"{texts['TOGMELDING_PERSONVERN_HREF']!r}"
        )


def test_the_togmelding_app_duplicates_the_shared_privacy_texts_verbatim():
    """KTD8: /togmelding duplicates these strings because it cannot import the module.

    Its own npm app since 2026-09-01, and the image's node stage copies only `demo/togmelding/`
    into the build — so a relative import into `../avviksvarsel/src/` would resolve on a laptop and
    fail in CI. Same duplication as when the page was vanilla HTML, and this is still the only
    thing keeping the two copies in step.
    """
    source = _shared_texts(PERSONVERN_TS)
    copy = _shared_texts(TOGMELDING_TS)
    for label, value in source.items():
        assert copy[label] == value, (
            f"{TOGMELDING_TS.relative_to(_REPO)} disagrees with {PERSONVERN_TS.relative_to(_REPO)} "
            f"on {label}:\n"
            f"  /varsel:     {value!r}\n"
            f"  /togmelding: {copy[label]!r}\n"
            "Edit both, or neither (KTD8)."
        )


def test_the_togmelding_app_renders_every_text_it_declares():
    """A declared-but-unrendered text would pass the parity check above and ship nothing.

    That risk is new: the old guard searched the page's markup, so presence in the file WAS
    presence on the page. Two TypeScript modules can agree perfectly while the app renders
    neither, so the constants are checked for actually being referenced.
    """
    rendered = _togmelding_sources()
    for name in _SHARED_CONSTANTS:
        uses = rendered.count(name)
        assert uses >= 2, (
            f"{name} is declared in {TOGMELDING_TS.relative_to(_REPO)} but referenced "
            f"{uses - 1} time(s) elsewhere in {TOGMELDING_SRC.relative_to(_REPO)} — a published "
            "privacy text that nothing renders is the failure this guard exists for."
        )


def test_the_togmelding_app_links_to_the_privacy_page():
    """R11: the same sender/privacy footer as /varsel, pointing at the canonical path."""
    href = _shared_texts(TOGMELDING_TS)["TOGMELDING_PERSONVERN_HREF"]
    assert href == _shared_texts(PERSONVERN_TS)["TOGMELDING_PERSONVERN_HREF"]
    rendered = _togmelding_sources()
    assert "href={TOGMELDING_PERSONVERN_HREF}" in rendered, (
        f"nothing in {TOGMELDING_SRC.relative_to(_REPO)} links to {href} via "
        "TOGMELDING_PERSONVERN_HREF. The footer's «Slik behandler vi opplysningene» link is how a "
        "/togmelding visitor reaches the privacy page at all — and a literal path written out "
        "instead of the constant would drift out from under the parity guard above."
    )


def test_the_togmelding_privacy_texts_carry_no_placeholder_brackets():
    """Acceptance criterion: no bracketed placeholder text reaches production.

    Applied to the resolved constants, not to a slice of markup: the vanilla page's JS was full of
    legitimate brackets, so the old guard read only the hand-written region before the first
    `<script>`. A JSX app has no such region — and the constants are where an unresolved appendix
    placeholder would actually survive, on both sides of the duplication.
    """
    for path in (PERSONVERN_TS, TOGMELDING_TS):
        for label, value in _shared_texts(path).items():
            for bracket in "[]":
                assert bracket not in value, (
                    f"{label} in {path.relative_to(_REPO)} contains {bracket!r} — an unresolved "
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


def test_the_privacy_page_repeats_the_linje_stylesheet_imports_in_the_same_order():
    """`?brand=none` also depends on this second duplication — the Linje import block.

    `personvern-page.tsx` renders four Linje components and imports all thirteen sheets anyway,
    deliberately: a second entry makes the build an MPA, and Rollup hoists what the two entries
    SHARE into a common chunk linked BEFORE each entry's own. Diverge the lists and the split
    falls at the divergence — the sheets only `main.tsx` imports land in main's chunk, i.e. AFTER
    `brands/none.css`. About 80 generated brand rules are `:where()`-scoped, so they tie exactly
    with Linje's own selectors and win on source order alone; reverse the order and the tie flips.

    Nothing errors, no other test fails, and `/varsel` quietly goes back to Entur navy on the
    unbranded variant. `personvern-page.tsx` documents all of that and then guards it with a
    comment — the same "keep this in step" the inline brand script had before it got the parity
    test above. This is that test for the other half.
    """
    root = _entur_style_imports(MAIN_TSX)
    copy = _entur_style_imports(PERSONVERN_PAGE_TSX)
    assert root == copy, (
        f"The `@entur/*` stylesheet import block in {PERSONVERN_PAGE_TSX.relative_to(_REPO)} has "
        f"drifted from {MAIN_TSX.relative_to(_REPO)}. Both entries must import the same sheets in "
        "the same order: any divergence moves a Linje sheet out of the shared chunk and after "
        "brands/none.css, which silently un-reskins `?brand=none` (the :where()-scoped brand rules "
        "win on source order alone). Nothing will fail at build time and no page will error.\n"
        f"--- main.tsx ---\n{chr(10).join(root)}\n"
        f"--- personvern-page.tsx ---\n{chr(10).join(copy)}"
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


# The only two values that keep «Varselteksten skrives av en språkmodell hos Google i EU» honest.
#
# `gemini` makes it true. `off` is admitted because it can only ever make the sentence
# OVER-disclose: no backend is asked at all (`llm_message.generation_enabled()` is False, every
# push carries the deterministic template), so the published claim describes egress that is not
# happening — a reader is told more than occurs, never less. And `off` is the documented egress
# kill switch (CLAUDE.md: "«egress» must stop → VARSEL_LLM_PROVIDER=off"). A guard that turned red
# the moment somebody pulled it would be exactly backwards: it would make the incident response
# fail CI, which is when nobody has attention to spare for arguing with a test.
#
# `auto` and `claude` stay out. Both can reach Anthropic — served through Vertex at
# `VERTEX_LOCATION`, which dev and prd set to `global` — so either makes the sentence name the
# wrong company AND the wrong jurisdiction, in the under-disclosing direction.
_HONEST_LLM_PROVIDERS = ("gemini", "off")


def test_every_environment_pins_gemini_as_the_llm_provider():
    """Protects: «Varselteksten skrives av en språkmodell hos Google i EU» (T5).

    The code default is `provider="auto"`, which would try Claude first. The published sentence
    names Google, so every environment must name Gemini — or `off`, which asks nobody. See
    `_HONEST_LLM_PROVIDERS`.
    """
    for path in _env_files():
        assert _pinned(path, "VARSEL_LLM_PROVIDER") in _HONEST_LLM_PROVIDERS, (
            f"{path.name} pins VARSEL_LLM_PROVIDER: {_pinned(path, 'VARSEL_LLM_PROVIDER')!r}, "
            f"expected one of {_HONEST_LLM_PROVIDERS}. The published privacy page at "
            "/varsel/personvern/ states «Varselteksten skrives av en språkmodell hos Google i EU»; "
            "the code default is `auto`, so that sentence is true only while this value is pinned. "
            "`off` is allowed — it asks no model, so the sentence over-discloses rather than "
            "becoming false, and it is the documented egress kill switch. Anything else "
            "(`auto`, `claude`) can reach Anthropic at VERTEX_LOCATION, which dev and prd set to "
            "`global`: change the published text before changing this value."
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
