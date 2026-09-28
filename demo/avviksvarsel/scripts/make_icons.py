"""Generate the Smarte reisevarsler PWA icons and favicons — one set per brand.

DEFAULT BRAND (Entur). The «D + KI-lab» tile from the 2026-09-28 icon proposal: the service name
«Smarte reisevarsler» where Entur's logo has «EN», Entur's coral bar under it, the sender line
«Et [ENTUR] eksperiment» with entur.no's wordmark, and a canary «KI-lab» band across the top-right
corner, on Linje's data lilac so it is not mistaken for the Entur app. Text is Nationale, drawn from
its glyph outlines (fontTools) so the vector favicon needs no font, and rasterised with Pillow.

BRAND "none" (?brand=none). The unbranded variant needs its own set, or an install made from it
would sit on the phone under the Entur mark and push a notification carrying it — the two places
a page-level reskin does not reach. It reuses the bell glyph already drawn here for the Android
badge, on the petrol accent /demo uses: a notification app, saying nothing about who runs it.

Regenerate with (after `npm ci` in demo/avviksvarsel, which brings the Nationale font files):
    uv run --with pillow --with fonttools --with svg.path python demo/avviksvarsel/scripts/make_icons.py

Outputs into demo/avviksvarsel/public/:
  icons/pwa-192, icons/pwa-512, icons/apple-touch-icon-180  — the tile, opaque and full-bleed.
  icons/pwa-512-maskable                                    — the tile at 80 % inside the circular
      safe zone Android may crop a maskable icon to.
  favicon.svg (+ favicon-32, favicon-16)                    — name and bar only (vector + PNG).
  the same seven with `-dev` before the extension           — «KI-lab dev» on a white band, served
      in their place by the dev deployment (main.py, `_DEV_VARSEL_ASSETS`).
  icons/badge-72                                            — white bell glyph on transparent, the
      monochrome Android notification badge (a "notification" glyph, not the app icon — left alone).
  the same six, suffixed `-none`                            — the unbranded set.
"""

from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)
# The unbranded icon tile. This is /demo's petrol accent rather than the #002c2d the brand sheet
# recolours the header band to: that one is nearly black, which reads as a smudge at 48 px on a
# home screen. The tab favicon and the app icon are looked at, not matched against the masthead.
PETROL = (22, 99, 107, 255)  # /demo --accent #16636b
SS = 4  # supersample factor for crisp anti-aliased edges

OUT = Path(__file__).resolve().parent.parent / "public"
ICONS = OUT / "icons"
FONTS = OUT.parent / "node_modules" / "@entur" / "typography" / "fonts"

# Linje colours for the Entur tile.
LILAC = "#8e57e3"  # data-default-lilac: tells the icon apart from the Entur app's navy
CORAL = "#ff5959"  # the Entur logo's underline
CANARY = "#ffca28"  # the KI-lab corner band
NAVY = "#181c56"  # Entur blue, the band's text

# The ENTUR wordmark from entur.no's header (viewBox 0 0 62 19; EN's baseline is y=13).
WORDMARK = [
    ("M0 0v13.006h9.392v-2.203H2.439V7.658h6.169V5.455H2.439V2.203h6.953V0z", "#fff"),
    ("M0 18h22.983v-2H0z", CORAL),
    ("M20.543 0v7.871L12.163 0H11.9v13.006h2.44V5.277l8.38 7.73h.262V0zM35.467 7.657h-3.642V18.46h-2.439V7.657h-3.66V5.454h9.74z", "#fff"),
    ("M42.872 18.691c-.825 0-1.574-.13-2.248-.39a4.929 4.929 0 0 1-1.725-1.102 4.877 4.877 0 0 1-1.106-1.724c-.261-.675-.392-1.427-.392-2.256V5.454h2.44v7.765c0 .651.104 1.19.313 1.617.209.426.467.764.775 1.012.308.25.64.421.994.516.354.095.67.142.95.142.278 0 .594-.047.949-.142.354-.095.685-.267.993-.516.308-.248.566-.586.775-1.012.21-.427.314-.966.314-1.617V5.454h2.44v7.765c0 .83-.128 1.581-.384 2.256a4.787 4.787 0 0 1-1.098 1.724 4.934 4.934 0 0 1-1.725 1.101c-.673.26-1.428.391-2.265.391M53.57 12.153h2.78c.509 0 .928-.065 1.258-.196.33-.13.59-.302.782-.515.191-.213.321-.453.39-.72.07-.266.105-.535.105-.808 0-.32-.05-.616-.148-.888a1.906 1.906 0 0 0-.451-.711 2.16 2.16 0 0 0-.782-.48c-.319-.118-.703-.178-1.155-.178H53.57v4.496zm0 2.167v4.14h-2.439V5.454h5.795c.652 0 1.248.11 1.789.329.541.22 1.004.527 1.387.924.384.397.684.87.9 1.421.214.551.322 1.152.322 1.804 0 .486-.064.956-.192 1.412a4.795 4.795 0 0 1-.532 1.235 3.718 3.718 0 0 1-.838.95 2.952 2.952 0 0 1-1.09.56l3.175 4.371h-2.91l-2.952-4.14H53.57z", "#fff"),
]
WORDMARK_W = 62.0

# A shape is (svg path d, affine matrix (a, b, c, d, e, f) as in SVG, fill). Every drawing below is
# a list of shapes in a 100x100 box, so the same list is written as SVG and rasterised as PNG.
Shape = tuple[str, tuple[float, ...], str]


def _mul(m, n):
    """m then n, i.e. the SVG matrix of `transform="n m"` applied to a point: n(m(p))."""
    a, b, c, d, e, f = n
    A, B, C, D, E, F = m
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D, a * E + c * F + e, b * E + d * F + f)


def _font(name: str):
    from fontTools.ttLib import TTFont

    path = FONTS / f"Entur-Nationale-{name}.woff"
    if not path.exists():
        raise SystemExit(f"{path} is missing: run `npm ci` in demo/avviksvarsel first")
    return TTFont(path)


class _Face:
    """Nationale glyph outlines as SVG paths. No kerning: at icon sizes it is invisible."""

    def __init__(self, name: str):
        from fontTools.pens.svgPathPen import SVGPathPen

        font = _font(name)
        self.upm = font["head"].unitsPerEm
        self.cmap = font.getBestCmap()
        self.hmtx = font["hmtx"]
        glyphs = font.getGlyphSet()
        self.paths = {}
        for g in set(self.cmap.values()):
            pen = SVGPathPen(glyphs)
            glyphs[g].draw(pen)
            self.paths[g] = pen.getCommands()

    def width(self, text: str, size: float, spacing: float = 0.0) -> float:
        return sum(self.hmtx[self.cmap[ord(ch)]][0] * size / self.upm + spacing for ch in text)

    def shapes(self, text: str, x: float, baseline: float, size: float, fill: str, m=None, spacing=0.0) -> list[Shape]:
        out, k = [], size / self.upm
        for ch in text:
            g = self.cmap[ord(ch)]
            if self.paths[g]:
                glyph = (k, 0, 0, -k, x, baseline)  # font units are y-up
                out.append((self.paths[g], _mul(glyph, m) if m else glyph, fill))
            x += self.hmtx[g][0] * k + spacing
        return out


def entur_tile(*, sender: bool = True, band: str = "KI-lab", band_fill: str = CANARY) -> list[Shape]:
    """The «D + KI-lab» icon: the service name where Entur's logo has «EN», the coral bar, the
    sender line «Et [ENTUR] eksperiment» and a canary «KI-lab» band across the top-right corner.

    Laid out as the design sketch does it (x=14, a 72-wide block, pushed 5 down for the band);
    `sender=False` is the favicon, where the sender line would be under a pixel tall. The dev
    deployment's set says «KI-lab dev» on a white band, so a dev install is never mistaken for prd.
    """
    bold, medium = _Face("Demibold"), _Face("Medium")
    x, w, lh, lines = 14.0, 72.0, 14.0, ("Smarte", "reisevarsler")
    block = len(lines) * lh + 4 + 3 + (12 if sender else 0)
    y = (100 - block) / 2 + 5
    shapes: list[Shape] = [("M0 0H100V100H0Z", (1, 0, 0, 1, 0, 0), LILAC)]
    # One size for both name lines, shrunk until the longer one fits the bar.
    size = lh * min(1.0, min(w / bold.width(t, lh) for t in lines))
    for t in lines:
        y += lh
        shapes += bold.shapes(t, x, y - 2, size, "#fff")
    y += 3
    shapes.append((f"M{x} {y}h{w}v3h-{w}Z", (1, 0, 0, 1, 0, 0), CORAL))
    y += 3
    if sender:
        # Words and wordmark are scaled together so the line is never wider than the bar.
        base, fs, ws, gap = y + 10.5, 9.0, 11 / 19, 2.5
        lead, trail = medium.width("Et", fs), medium.width("eksperiment", fs)
        k = min(1.0, w / (lead + gap + WORDMARK_W * ws + gap + trail))
        shapes += medium.shapes("Et", x, base, fs * k, "#fff")
        mx, s2 = x + (lead + gap) * k, ws * k
        shapes += [(d, (s2, 0, 0, s2, mx, base - 13 * s2), fill) for d, fill in WORDMARK]
        shapes += medium.shapes("eksperiment", mx + (WORDMARK_W * ws + gap) * k, base, fs * k, "#fff")
    m = (0.7071, 0.7071, -0.7071, 0.7071, 76, 24)  # translate(76 24) rotate(45)
    shapes.append(("M-60 -8H60V8H-60Z", m, band_fill))
    # The band's visible run across the corner is ~68 units; keep a longer label clear of its ends.
    fs = min(10.0, 10 * 42 / (bold.width(band, 10, 0.6) - 0.6))
    label = bold.width(band, fs, 0.6) - 0.6
    shapes += bold.shapes(band, -label / 2, 0.38 * fs, fs, NAVY, m, 0.6)
    return shapes


def scaled(shapes: list[Shape], k: float) -> list[Shape]:
    """Everything but the background scaled by k about the centre — the maskable safe zone."""
    m = (k, 0, 0, k, 50 * (1 - k), 50 * (1 - k))
    return shapes[:1] + [(d, _mul(t, m), fill) for d, t, fill in shapes[1:]]


def write_svg(path: Path, shapes: list[Shape]) -> None:
    body = "\n".join(
        f'  <path fill="{fill}" transform="matrix({" ".join(f"{v:.4g}" for v in t)})" d="{d}"/>' for d, t, fill in shapes
    )
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">\n{body}\n</svg>\n')
    print(f"  wrote {path.relative_to(OUT.parent)} (vector)")


def _subpaths(d: str, steps: int = 12):
    """Flatten a path into its subpaths' vertex lists (curves and arcs sampled)."""
    from svg.path import Close, Line, Move, parse_path

    polys: list[list[complex]] = []
    for seg in parse_path(d):
        if isinstance(seg, Move):
            polys.append([seg.end])
        elif isinstance(seg, (Line, Close)):
            polys[-1].append(seg.end)
        else:
            polys[-1] += [seg.point(i / steps) for i in range(1, steps + 1)]
    return polys


def write_png(path: Path, shapes: list[Shape], size: int) -> None:
    from PIL import ImageChops

    px = size * SS
    canvas = Image.new("RGBA", (px, px))
    for d, (a, b, c, dd, e, f), fill in shapes:
        # Even-odd fill: XOR each subpath in, which is what punches the counters out of «R», «e», «a».
        mask = Image.new("1", (px, px))
        for poly in _subpaths(d):
            if len(poly) < 3:
                continue
            one = Image.new("1", (px, px))
            pts = [((a * p.real + c * p.imag + e) * px / 100, (b * p.real + dd * p.imag + f) * px / 100) for p in poly]
            ImageDraw.Draw(one).polygon(pts, fill=1)
            mask = ImageChops.logical_xor(mask, one)
        canvas.paste(ImageColor.getrgb(fill), mask=mask)
    canvas.resize((size, size), Image.LANCZOS).save(path)
    print(f"  wrote {path.relative_to(OUT.parent)} ({size}x{size})")


def _draw_bell(draw: ImageDraw.ImageDraw, s: int, fill=WHITE) -> None:
    """Draw a centred notification bell filling the central ~60 % (maskable-safe) of an s×s canvas."""
    cx = s / 2
    r = 0.16 * s  # dome radius / body half-width at the top
    dome_cy = 0.40 * s
    flare_bottom = 0.62 * s
    flare_half = 0.24 * s

    # Domed top (upper half-circle).
    draw.pieslice([cx - r, dome_cy - r, cx + r, dome_cy + r], 180, 360, fill=fill)
    # Flaring body (trapezoid from the dome down to the mouth).
    draw.polygon(
        [
            (cx - r, dome_cy),
            (cx + r, dome_cy),
            (cx + flare_half, flare_bottom),
            (cx - flare_half, flare_bottom),
        ],
        fill=fill,
    )
    # Mouth rim (rounded bar, a touch wider than the flare).
    rim_half = flare_half + 0.02 * s
    draw.rounded_rectangle(
        [cx - rim_half, flare_bottom - 0.015 * s, cx + rim_half, 0.66 * s],
        radius=0.03 * s,
        fill=fill,
    )
    # Top knob.
    k = 0.045 * s
    draw.ellipse([cx - k, 0.20 * s - k, cx + k, 0.20 * s + k], fill=fill)
    # Clapper.
    c = 0.05 * s
    draw.ellipse([cx - c, 0.72 * s - c, cx + c, 0.72 * s + c], fill=fill)


def make_bell(path: Path, size: int, *, background, glyph=WHITE) -> None:
    canvas = Image.new("RGBA", (size * SS, size * SS), background)
    _draw_bell(ImageDraw.Draw(canvas), size * SS, fill=glyph)
    canvas.resize((size, size), Image.LANCZOS).save(path)
    print(f"  wrote {path.relative_to(OUT.parent)} ({size}x{size})")


def _bell_svg_paths(box: int) -> str:
    """The bell glyph as SVG, geometry matching `_draw_bell` on an s x s canvas."""
    s = float(box)
    cx, r = s / 2, 0.16 * s
    dome_cy, flare_bottom, flare_half = 0.40 * s, 0.62 * s, 0.24 * s
    rim_half, k, c = flare_half + 0.02 * s, 0.045 * s, 0.05 * s
    return f"""  <path fill="#FFF" d="M{cx - r:.2f} {dome_cy:.2f} a{r:.2f} {r:.2f} 0 0 1 {2 * r:.2f} 0 z"/>
  <path fill="#FFF" d="M{cx - r:.2f} {dome_cy:.2f} L{cx + r:.2f} {dome_cy:.2f} L{cx + flare_half:.2f} {flare_bottom:.2f} L{cx - flare_half:.2f} {flare_bottom:.2f} z"/>
  <rect fill="#FFF" x="{cx - rim_half:.2f}" y="{flare_bottom - 0.015 * s:.2f}" width="{2 * rim_half:.2f}" height="{0.66 * s - flare_bottom + 0.015 * s:.2f}" rx="{0.03 * s:.2f}"/>
  <circle fill="#FFF" cx="{cx:.2f}" cy="{0.20 * s:.2f}" r="{k:.2f}"/>
  <circle fill="#FFF" cx="{cx:.2f}" cy="{0.72 * s:.2f}" r="{c:.2f}"/>"""


def make_bell_favicon_svg(path: Path, *, box: int = 32) -> None:
    """A crisp vector favicon for the unbranded variant: the bell on a full-bleed petrol tile."""
    fill = "#{:02x}{:02x}{:02x}".format(*PETROL[:3])
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {box} {box}">\n'
        f'  <rect width="{box}" height="{box}" fill="{fill}"/>\n'
        f"{_bell_svg_paths(box)}\n</svg>\n"
    )
    print(f"  wrote {path.relative_to(OUT.parent)} (vector)")


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)

    print("Entur (default brand):")
    tile = entur_tile()
    # Home-screen / PWA icons: full-bleed, since iOS and Android draw their own rounded mask.
    write_png(ICONS / "pwa-192.png", tile, 192)
    write_png(ICONS / "pwa-512.png", tile, 512)
    write_png(ICONS / "apple-touch-icon-180.png", tile, 180)
    # Maskable: Android may crop to a circle of radius 40, which the full tile's text overruns.
    write_png(ICONS / "pwa-512-maskable.png", scaled(tile, 0.8), 512)
    # Favicon: name and bar only; the sender line is under a pixel at 32 px.
    favicon = entur_tile(sender=False)
    write_svg(OUT / "favicon.svg", favicon)
    write_png(OUT / "favicon-32.png", favicon, 32)
    write_png(OUT / "favicon-16.png", favicon, 16)

    # The same set for the dev deployment, which main.py serves under the names above when
    # environment_label() is "dev" -- the image is shared, so the URLs have to be too.
    print("Entur, dev deployment:")
    dev = {"band": "KI-lab dev", "band_fill": "#ffffff"}
    tile = entur_tile(**dev)
    write_png(ICONS / "pwa-192-dev.png", tile, 192)
    write_png(ICONS / "pwa-512-dev.png", tile, 512)
    write_png(ICONS / "apple-touch-icon-180-dev.png", tile, 180)
    write_png(ICONS / "pwa-512-maskable-dev.png", scaled(tile, 0.8), 512)
    favicon = entur_tile(sender=False, **dev)
    write_svg(OUT / "favicon-dev.svg", favicon)
    write_png(OUT / "favicon-dev-32.png", favicon, 32)
    write_png(OUT / "favicon-dev-16.png", favicon, 16)
    # Notification badge: monochrome white bell on transparent (Android renders it as a mask).
    # Brand-independent -- Android draws it as a silhouette, so there is no colour to brand.
    make_bell(ICONS / "badge-72.png", 72, background=TRANSPARENT)

    print('brand "none" (unbranded):')
    for name, size in (("pwa-192", 192), ("pwa-512", 512), ("apple-touch-icon-180", 180)):
        make_bell(ICONS / f"{name}-none.png", size, background=PETROL)
    make_bell_favicon_svg(OUT / "favicon-none.svg")
    make_bell(OUT / "favicon-none-32.png", 32, background=PETROL)
    make_bell(OUT / "favicon-none-16.png", 16, background=PETROL)

    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
