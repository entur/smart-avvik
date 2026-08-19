"""A very small CSS reader: walk rules and rewrite the colour literals inside them.

Deliberately not a full CSS parser. It only needs to do three things to the Entur Linje
stylesheets — find every declaration, find the colour literals in its value, and re-emit the rule
under a different selector — and those survive a brace-matching walk. Anything it does not
understand it copies or skips rather than guessing, and `parse` reports what it skipped so a Linje
upgrade that introduces a new construct is visible instead of silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# #rgb, #rrggbb, #rrggbbaa, rgb(...)/rgba(...) — the four forms Linje's build actually emits.
COLOUR_RE = re.compile(
    r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\(\s*[^)]*\)"
)


@dataclass
class Rule:
    """One style rule, with the at-rule preludes it is nested inside (outermost first)."""

    selector: str
    declarations: list[tuple[str, str]]  # (property, value), source order
    at_rules: tuple[str, ...] = ()


@dataclass
class Sheet:
    rules: list[Rule] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (prelude, body) not descended into


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep`, ignoring separators nested in (), [], '' or "".

    Needed for selector LISTS: `.a, .b` is one prelude, and scoping only its first part leaves the
    rest matching unconditionally — which for a brand override sheet means the alternative brand's
    colours leak onto the default brand.
    """
    parts: list[str] = []
    depth = 0
    quote_ch = ""
    buf: list[str] = []
    for ch in text:
        if quote_ch:
            buf.append(ch)
            if ch == quote_ch:
                quote_ch = ""
            continue
        if ch in "\"'":
            quote_ch = ch
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf.clear()
        else:
            buf.append(ch)
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _split_declarations(block: str) -> list[tuple[str, str]]:
    """Split a declaration block on top-level `;`, keeping `;` inside (), '' and "" intact."""
    out: list[tuple[str, str]] = []
    depth = quote = 0
    quote_ch = ""
    buf: list[str] = []

    def flush() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if not text or ":" not in text:
            return
        prop, _, value = text.partition(":")
        out.append((prop.strip(), value.strip()))

    for ch in block:
        if quote:
            buf.append(ch)
            if ch == quote_ch:
                quote = 0
            continue
        if ch in "\"'":
            quote, quote_ch = 1, ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == ";" and depth == 0:
            flush()
        else:
            buf.append(ch)
    flush()
    return out


# Descending into these would produce invalid CSS: a selector prefix cannot be applied to a
# keyframe stop ("0%", "from"), and @font-face/@import/@charset have no selector at all.
_OPAQUE_AT_RULES = ("keyframes", "font-face", "import", "charset", "property", "counter-style")


def parse(css: str) -> Sheet:
    sheet = Sheet()
    css = _strip_comments(css)
    _walk(css, (), sheet)
    return sheet


def _walk(css: str, at_rules: tuple[str, ...], sheet: Sheet) -> None:
    i, n = 0, len(css)
    while i < n:
        brace = css.find("{", i)
        if brace == -1:
            break
        prelude = css[i:brace].strip()
        # Find the matching close brace.
        depth, j = 1, brace + 1
        while j < n and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        body = css[brace + 1 : j - 1]
        i = j
        if not prelude:
            continue
        if prelude.startswith("@"):
            name = prelude[1:].split(None, 1)[0].lstrip("-").lower()
            # Vendor-prefixed keyframes (@-webkit-keyframes) land here too, via the lstrip.
            if any(name.endswith(k) or name == k for k in _OPAQUE_AT_RULES):
                if COLOUR_RE.search(body):
                    sheet.skipped.append((prelude, body))
                continue
            _walk(body, at_rules + (prelude,), sheet)
            continue
        decls = _split_declarations(body)
        if decls:
            sheet.rules.append(Rule(selector=prelude, declarations=decls, at_rules=at_rules))


def parse_colour(text: str) -> tuple[tuple[int, int, int], str] | None:
    """A colour literal -> ((r, g, b), alpha-suffix-as-written). None if it cannot be read."""
    text = text.strip()
    if text.startswith("#"):
        hexpart = text[1:]
        if len(hexpart) == 3:
            return tuple(int(c * 2, 16) for c in hexpart), ""  # type: ignore[return-value]
        if len(hexpart) == 6:
            return tuple(int(hexpart[k : k + 2], 16) for k in (0, 2, 4)), ""  # type: ignore[return-value]
        if len(hexpart) == 8:
            return tuple(int(hexpart[k : k + 2], 16) for k in (0, 2, 4)), hexpart[6:8]  # type: ignore[return-value]
        return None
    m = re.match(r"rgba?\(\s*([^)]*)\)", text, re.IGNORECASE)
    if not m:
        return None
    # Both the legacy comma form and the space form, with an optional /alpha or 4th argument.
    parts = [p for p in re.split(r"[,\s/]+", m.group(1).strip()) if p]
    if len(parts) < 3:
        return None
    try:
        rgb = tuple(
            round(float(p[:-1]) * 255 / 100) if p.endswith("%") else round(float(p))
            for p in parts[:3]
        )
    except ValueError:
        return None
    if not all(0 <= v <= 255 for v in rgb):
        return None
    return rgb, (parts[3] if len(parts) > 3 else "")  # type: ignore[return-value]


def format_colour(rgb: tuple[int, int, int], alpha: str, original: str) -> str:
    """Re-emit a recoloured value in the same notation the original used."""
    if original.strip().startswith("#"):
        return "#{:02x}{:02x}{:02x}{}".format(*rgb, alpha)
    return (
        f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {alpha})"
        if alpha
        else "rgb({}, {}, {})".format(*rgb)
    )
