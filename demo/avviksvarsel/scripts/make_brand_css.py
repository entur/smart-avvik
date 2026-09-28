"""Generate a brand's colour override sheet from the Entur Linje stylesheets.

WHY THIS IS GENERATED AND NOT HAND-WRITTEN. Linje's components do not read the design tokens at
runtime: each package's `dist/styles.css` defines its own `--components-<pkg>-*` variables with
LITERAL hex (~3 100 of them across the packages /varsel imports) and its rules read those. Some
120 further declarations skip variables altogether -- 31 of them `outline:2px solid #181c56`, the
focus ring on every control. So overriding `--lavender-90` reskins nothing, and a hand-written
override sheet would be a guess at which of 3 100 names matter. This walks the actual stylesheets
instead, recolours every colour literal it finds, and emits only the declarations that changed.

HOW A COLOUR IS RECOLOURED. In OKLCh, keeping L and moving only hue/chroma (see oklch.py). Entur
tuned the Linje ramps for contrast; preserving perceptual lightness is what keeps a reskin from
quietly failing WCAG on text that passed before. `--check` re-runs the generation and diffs, so a
Linje upgrade that moves a colour cannot leave a stale sheet behind unnoticed.

    uv run python demo/avviksvarsel/scripts/make_brand_css.py --report      # survey the palette
    uv run python demo/avviksvarsel/scripts/make_brand_css.py               # write src/brands/
    uv run python demo/avviksvarsel/scripts/make_brand_css.py --check       # CI-style staleness check
    uv run python demo/avviksvarsel/scripts/make_brand_css.py --brand entur # the default brand's lilac

Requires demo/avviksvarsel/node_modules (npm install) -- it reads the published stylesheets.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from css_colours import (
    COLOUR_RE,
    format_colour,
    parse,
    parse_colour,
    split_top_level,
)
from oklch import oklch_to_rgb, rgb_to_oklch

ROOT = Path(__file__).resolve().parent.parent
MODULES = ROOT / "node_modules" / "@entur"
OUT_DIR = ROOT / "src" / "brands"

# The stylesheets the app actually loads. KEEP IN STEP WITH src/main.tsx -- a package imported
# there but missing here ships un-recoloured, which looks like a half-applied brand.
SHEETS = [
    "tokens/dist/primitive.css",
    "tokens/dist/semantic.css",
    "tokens/dist/base.css",
    "tokens/dist/transport.css",
    "tokens/dist/styles.css",
    "typography/dist/styles.css",
    "layout/dist/styles.css",
    "button/dist/styles.css",
    "form/dist/styles.css",
    "dropdown/dist/styles.css",
    "chip/dist/styles.css",
    "alert/dist/styles.css",
    "modal/dist/styles.css",
    "menu/dist/styles.css",
    "a11y/dist/styles.css",
    "loader/dist/styles.css",
]


@dataclass(frozen=True)
class Band:
    """Recolour rule for one band of source hues (OKLCh degrees, `lo` inclusive, `hi` exclusive)."""

    name: str
    lo: float
    hi: float
    hue: float | None  # target hue; None keeps the source hue
    chroma: float  # multiplier applied to the source chroma


@dataclass(frozen=True)
class Brand:
    slug: str
    label: str
    bands: tuple[Band, ...]
    note: str = ""


# Below this chroma there is no hue to speak of — #ffffff, #808080, Linje's true greys — and
# rotating one would tint a neutral for no gain. Set low enough that the FAINT tints still move:
# --lavender-10 (#f0f1fa, C=0.012) is the page background and does read as violet next to petrol.
GREY_C = 0.008

# A recolouring below this OKLab distance is not visible, and emitting it would bury the
# declarations that matter under thousands of 1-bit changes to Linje's greys.
MIN_DELTA = 0.008

# `none`: the brand-neutral variant. Entur's identity is carried by the lavender/blue/ebony ramps,
# which cluster tightly at h 265-292 — primary buttons, the header band, accent text, and every
# focus ring. Those rotate to the restrained petrol /demo already uses for partner-facing surfaces
# (#16636b, h~197) at half chroma.
#
# What deliberately does NOT rotate:
#   - Status hues (red/amber/green) carry MEANING, not branding. A traveller reading "innstilt"
#     needs the red to still be red. They are pulled in slightly so they sit with the calmer
#     surroundings instead of shouting against them.
#   - Linje's TRANSPORT palette (metro purple, tram pink, the line colours) spans h 292-360 and
#     h 255-265. Folding it into the brand band would paint every mode the same teal and destroy
#     the distinction it exists to draw, so it only loses some saturation.
NONE = Brand(
    slug="none",
    label="Nøytral",
    bands=(
        Band("status red", 0.0, 55.0, None, 0.84),
        Band("status amber", 55.0, 120.0, None, 0.86),
        Band("status green", 120.0, 190.0, None, 0.82),
        Band("info cyan-blue", 190.0, 255.0, 205.0, 0.72),
        Band("transport navy", 255.0, 265.0, None, 0.75),
        Band("ENTUR lavender/blue", 265.0, 292.0, 197.0, 0.50),
        Band("transport purple/pink", 292.0, 360.0, None, 0.75),
    ),
    note="Brand-neutral: near-monochrome slate with one petrol accent, as /demo uses.",
)

# `entur`: the default brand, recoloured to the app icon's lilac (#8e57e3, h~298) since the
# 2026-09-28 «D + KI-lab» icon. Only Entur's own lavender/blue ramp moves, at a little more chroma
# because lilac is more saturated than navy; status and transport colours keep their meaning.
ENTUR = Brand(
    slug="entur",
    label="Entur KI-lab",
    bands=(Band("ENTUR lavender/blue", 265.0, 292.0, 298.0, 1.3),),
    note="Entur's navy/lavender moved to the app icon's lilac.",
)

BRANDS = {b.slug: b for b in (NONE, ENTUR)}


def recolour(rgb: tuple[int, int, int], brand: Brand) -> tuple[int, int, int]:
    L, C, h = rgb_to_oklch(rgb)
    if C < GREY_C:
        return rgb  # a true neutral: nothing to rotate
    for band in brand.bands:
        if band.lo <= h < band.hi:
            return oklch_to_rgb(L, C * band.chroma, band.hue if band.hue is not None else h)
    return rgb


def _visibly_different(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    """OKLab distance above MIN_DELTA. Guards against emitting invisible 1-bit rounding drift."""
    La, Ca, ha = rgb_to_oklch(a)
    Lb, Cb, hb = rgb_to_oklch(b)
    import math

    aa, ab = Ca * math.cos(math.radians(ha)), Ca * math.sin(math.radians(ha))
    ba, bb = Cb * math.cos(math.radians(hb)), Cb * math.sin(math.radians(hb))
    return math.dist((La, aa, ab), (Lb, ba, bb)) >= MIN_DELTA


def recolour_value(value: str, brand: Brand) -> str:
    """Rewrite every colour literal in a declaration value. Returns the value unchanged if none moved."""

    def sub(match) -> str:
        literal = match.group(0)
        parsed = parse_colour(literal)
        if parsed is None:
            return literal
        rgb, alpha = parsed
        new = recolour(rgb, brand)
        if not _visibly_different(rgb, new):
            return literal
        return format_colour(new, alpha, literal)

    return COLOUR_RE.sub(sub, value)


def scope_one(selector: str, brand_slug: str) -> str:
    """Re-target a single Linje selector at one brand. Two cases, and the difference is the point.

    ROOT TOKEN BLOCKS (`:root`, `html`, `[data-color-mode=...]`) are re-emitted as the root itself
    rather than as a descendant of it — as a descendant they would match nothing — and they must
    OUTRANK the block they replace, so the brand attribute is compounded on: `:root[data-brand=x]`
    is (0,2,0) against Linje's (0,1,0). Nothing more specific competes for those variables on the
    root element, so raising them is safe.

    EVERY OTHER RULE is prefixed inside `:where()`, which contributes ZERO specificity, so the copy
    matches at exactly the weight Linje wrote it at and wins only on source order (this sheet is
    imported last). Raising these instead FLATTENS Linje's own hierarchy, and that is not a
    theoretical concern — it shipped and was caught in the browser. `.eds-h1{color:#181c56}` scoped
    to (0,2,0) tied with Linje's `.eds-contrast .eds-h1{color:#fff}`, won on source order, and
    painted the masthead heading the same near-black as the header band behind it: an empty bar
    where the app's name should be. The recoloured rule has to lose that comparison, exactly as the
    original did.
    """
    attr = f"[data-brand='{brand_slug}']"
    s = selector.strip()
    if s in (":root", "html", ":root:root"):
        return f":root{attr}"
    if s.startswith("[data-color-mode="):
        head, _, rest = s.partition("]")
        return f":root{attr}{head}]{rest}"
    if s.startswith(":root"):
        return f":root{attr}{s[len(':root'):]}"
    if s.startswith("html"):
        return f":root{attr}{s[len('html'):]}"
    return f":where(:root{attr}) {s}"


def scope(selector_list: str, brand_slug: str) -> str:
    """Scope EVERY selector in a comma-separated list.

    Scoping only the first is the failure this function exists to avoid: Linje writes
    `.eds-h1,.eds-h2,...,.eds-h6{color:#181c56}`, and a half-scoped copy leaves `.eds-h2`-`.eds-h6`
    matching unconditionally — i.e. the alternative brand's colours land on the DEFAULT brand.
    (Also caught in the browser, on that very rule.)
    """
    return ",\n".join(scope_one(part, brand_slug) for part in split_top_level(selector_list))


def build(brand: Brand) -> tuple[str, Counter]:
    stats: Counter = Counter()
    chunks: list[str] = []
    skipped: list[str] = []

    for rel in SHEETS:
        path = MODULES / rel
        if not path.exists():
            raise SystemExit(f"missing {path} — run `npm install` in {ROOT}")
        sheet = parse(path.read_text())
        # A skipped at-rule only matters if it holds a colour this brand would have moved;
        # @keyframes carrying rgba(0,0,0,.25) is not a gap, and listing it trains the reader to
        # ignore the notice.
        skipped.extend(
            f"{rel}: {prelude}"
            for prelude, body in sheet.skipped
            if recolour_value(body, brand) != body
        )
        emitted: list[str] = []
        for rule in sheet.rules:
            recoloured = [
                (prop, recolour_value(value, brand) if COLOUR_RE.search(value) else value)
                for prop, value in rule.declarations
            ]
            touched = {
                prop
                for (prop, new), (_, old) in zip(recoloured, rule.declarations)
                if new != old
            }
            if not touched:
                continue
            # EVERY declaration of a touched property, not just the changed one. Linje writes
            # CSS FALLBACK PAIRS -- `color:#181c56; color:var(--primary-text-color)` -- where the
            # literal is dead weight for browsers without custom properties and the var() beneath
            # it is what actually applies. Emitting only the half that changed leaves the legacy
            # literal as the LAST declaration, so the copy resolves to a value no browser was ever
            # meant to use. That shipped once and was caught in the browser: the masthead heading
            # came out the same near-black as the header band behind it, because the var() it
            # inherits white from inside `.eds-contrast` had been dropped on the floor.
            changed = [(prop, value) for prop, value in recoloured if prop in touched]
            stats["declarations"] += len(changed)
            stats["rules"] += 1
            body = "".join(f"  {prop}: {value};\n" for prop, value in changed)
            block = f"{scope(rule.selector, brand.slug)} {{\n{body}}}\n"
            for prelude in reversed(rule.at_rules):
                block = (
                    f"{prelude} {{\n"
                    + "".join(f"  {line}\n" if line else "\n" for line in block.splitlines())
                    + "}\n"
                )
            emitted.append(block)
        if emitted:
            stats["sheets"] += 1
            chunks.append(f"/* ---- {rel} ---- */\n\n" + "\n".join(emitted))

    header = f"""/* GENERATED FILE — DO NOT EDIT.
 *
 * Brand "{brand.slug}" ({brand.label}) — {brand.note}
 *
 * Written by demo/avviksvarsel/scripts/make_brand_css.py from the @entur/* stylesheets in
 * node_modules. Every colour literal Linje ships was converted to OKLCh, kept at its original
 * perceptual lightness, and moved in hue/chroma; only the declarations that actually changed are
 * emitted. Regenerate after upgrading Linje:
 *
 *     uv run python demo/avviksvarsel/scripts/make_brand_css.py
 *
 * {stats["declarations"]} declarations across {stats["rules"]} rules from {stats["sheets"]} stylesheets.
 */

"""
    if skipped:
        header += "/* At-rules left alone (a selector prefix cannot be applied inside them):\n"
        header += "".join(f" *   {s}\n" for s in sorted(set(skipped)))
        header += " */\n\n"
    return header + "\n".join(chunks), stats


def report(brand: Brand) -> None:
    """Print every distinct colour Linje ships, with its OKLCh and what it becomes."""
    seen: Counter = Counter()
    for rel in SHEETS:
        path = MODULES / rel
        if not path.exists():
            raise SystemExit(f"missing {path} — run `npm install` in {ROOT}")
        for rule in parse(path.read_text()).rules:
            for _, value in rule.declarations:
                for literal in COLOUR_RE.findall(value):
                    seen[literal.lower()] += 1

    rows = []
    for literal, count in seen.items():
        parsed = parse_colour(literal)
        if parsed is None:
            rows.append((999.0, literal, count, "UNPARSED", ""))
            continue
        rgb, alpha = parsed
        L, C, h = rgb_to_oklch(rgb)
        new = recolour(rgb, brand)
        band = "neutral (left alone)" if C < GREY_C else next(
            (b.name for b in brand.bands if b.lo <= h < b.hi), "-"
        )
        out = (
            format_colour(new, alpha, literal)
            if _visibly_different(rgb, new)
            else "(unchanged)"
        )
        rows.append((h, literal, count, f"L={L:.2f} C={C:.3f} h={h:6.1f}  {band:<22}", out))

    print(f"{len(rows)} distinct colour literals across {len(SHEETS)} stylesheets\n")
    print(f"{'source':<26}{'n':>5}  {'oklch / band':<48}{'-> neutral'}")
    for h, literal, count, desc, new in sorted(rows):
        print(f"{literal:<26}{count:>5}  {desc:<48}{new}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brand", default="none", choices=sorted(BRANDS))
    ap.add_argument("--report", action="store_true", help="survey the palette instead of writing")
    ap.add_argument("--check", action="store_true", help="fail if the committed sheet is stale")
    args = ap.parse_args()

    brand = BRANDS[args.brand]
    if args.report:
        report(brand)
        return

    css, stats = build(brand)
    target = OUT_DIR / f"{brand.slug}.generated.css"
    if args.check:
        current = target.read_text() if target.exists() else ""
        if current != css:
            print(f"STALE: {target.relative_to(ROOT.parent.parent)} differs from a fresh run.")
            print("Regenerate with: uv run python demo/avviksvarsel/scripts/make_brand_css.py")
            raise SystemExit(1)
        print(f"up to date: {target.name} ({stats['declarations']} declarations)")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(css)
    size = len(css.encode())
    print(
        f"wrote {target.relative_to(ROOT)} — {stats['declarations']} declarations, "
        f"{stats['rules']} rules, {size / 1024:.1f} KiB"
    )


if __name__ == "__main__":
    main()
