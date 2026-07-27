"""Generate the Avviksvarsel PWA icons and favicon — the Entur logo mark on Entur Lavender.

The home-screen / PWA / favicon artwork is the official Entur "EN" logo (white wordmark + coral
underline, i.e. @entur/icons' LogoNegativeIcon) on the app's Lavender-90 theme colour, so /varsel
reads as Entur-branded. The Entur logo paths are pure straight-line geometry, so we render them
faithfully with Pillow (no SVG toolchain needed) — same approach as before.

Regenerate with:
    uv run --with pillow python demo/avviksvarsel/scripts/make_icons.py

Outputs into demo/avviksvarsel/public/:
  icons/pwa-192, icons/pwa-512, icons/apple-touch-icon-180  — Entur logo, opaque, full-bleed so
      the maskable/apple masks look right.
  favicon.svg (+ favicon-32, favicon-16)                    — browser-tab favicon (vector + PNG).
  icons/badge-72                                            — white bell glyph on transparent, the
      monochrome Android notification badge (a "notification" glyph, not the app icon — left alone).
"""

import re
from pathlib import Path

from PIL import Image, ImageDraw

LAVENDER_90 = (24, 28, 86, 255)  # Entur --lavender-90 #181c56
WHITE = (255, 255, 255, 255)
CORAL = (255, 89, 89, 255)  # Entur logo underline #FF5959
TRANSPARENT = (0, 0, 0, 0)
SS = 4  # supersample factor for crisp anti-aliased edges

OUT = Path(__file__).resolve().parent.parent / "public"
ICONS = OUT / "icons"

# The Entur logo mark, straight from @entur/icons' LogoNegativeIcon (viewBox 0 0 16 16).
# All commands are lines (M/L/H/V + relative variants), so each path is a simple filled polygon.
LOGO_E = "M0 2h6.45v1.567H1.674v2.312h4.236v1.566H1.675v2.237H6.45v1.566H0z"
LOGO_N = "M14.107 2h1.676v9.248h-.18L9.848 5.752v5.496H8.173V2h.179l5.755 5.597z"
LOGO_BAR = "M0 14.799 15.783 14.799 15.783 13.377 0 13.377z"
# Logo bounding box within the 16×16 viewBox (bar reaches y=14.799; letters start at y=2, x=0).
LOGO_X0, LOGO_Y0, LOGO_W, LOGO_H = 0.0, 2.0, 15.783, 12.799


def _parse_path(d: str) -> list[tuple[float, float]]:
    """Parse a straight-line SVG path (M/L/H/V + relative) into absolute (x, y) vertices."""
    tokens = re.findall(r"[MmLlHhVvZz]|-?\d*\.?\d+", d)
    pts: list[tuple[float, float]] = []
    x = y = 0.0
    cmd = ""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            continue
        if cmd in ("M", "L"):
            x, y = float(tokens[i]), float(tokens[i + 1])
            i += 2
            cmd = "L"  # implicit lineto for subsequent coordinate pairs
        elif cmd in ("m", "l"):
            x, y = x + float(tokens[i]), y + float(tokens[i + 1])
            i += 2
            cmd = "l"
        elif cmd == "H":
            x = float(tokens[i])
            i += 1
        elif cmd == "h":
            x += float(tokens[i])
            i += 1
        elif cmd == "V":
            y = float(tokens[i])
            i += 1
        elif cmd == "v":
            y += float(tokens[i])
            i += 1
        else:  # a stray number under Z/z — shouldn't happen for these paths
            i += 1
            continue
        pts.append((x, y))
    return pts


def _draw_logo(draw: ImageDraw.ImageDraw, s: int, width_frac: float) -> None:
    """Draw the Entur logo mark centred, spanning `width_frac` of an s×s canvas."""
    target_w = width_frac * s
    sc = target_w / LOGO_W
    ox = (s - target_w) / 2
    oy = (s - LOGO_H * sc) / 2

    def tf(pts):
        return [(ox + (px - LOGO_X0) * sc, oy + (py - LOGO_Y0) * sc) for px, py in pts]

    draw.polygon(tf(_parse_path(LOGO_E)), fill=WHITE)
    draw.polygon(tf(_parse_path(LOGO_N)), fill=WHITE)
    draw.polygon(tf(_parse_path(LOGO_BAR)), fill=CORAL)


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


def make_logo(path: Path, size: int, *, width_frac: float, background=LAVENDER_90) -> None:
    canvas = Image.new("RGBA", (size * SS, size * SS), background)
    _draw_logo(ImageDraw.Draw(canvas), size * SS, width_frac)
    canvas.resize((size, size), Image.LANCZOS).save(path)
    print(f"  wrote {path.relative_to(OUT.parent)} ({size}x{size})")


def make_bell(path: Path, size: int, *, background, glyph=WHITE) -> None:
    canvas = Image.new("RGBA", (size * SS, size * SS), background)
    _draw_bell(ImageDraw.Draw(canvas), size * SS, fill=glyph)
    canvas.resize((size, size), Image.LANCZOS).save(path)
    print(f"  wrote {path.relative_to(OUT.parent)} ({size}x{size})")


def make_favicon_svg(path: Path, *, box: int = 32, width_frac: float = 0.72) -> None:
    """A crisp vector favicon: the Entur logo on a full-bleed Lavender tile."""
    sc = width_frac * box / LOGO_W
    tx = (box - width_frac * box) / 2
    ty = (box - LOGO_H * sc) / 2 - LOGO_Y0 * sc
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {box} {box}">
  <rect width="{box}" height="{box}" fill="#181c56"/>
  <g transform="translate({tx:.3f} {ty:.3f}) scale({sc:.4f})">
    <path fill="#FF5959" d="{LOGO_BAR}"/>
    <path fill="#FFF" d="{LOGO_E}"/>
    <path fill="#FFF" d="{LOGO_N}"/>
  </g>
</svg>
"""
    path.write_text(svg)
    print(f"  wrote {path.relative_to(OUT.parent)} (vector)")


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    # Home-screen / PWA icons (0.58 width keeps the mark inside the Android maskable safe zone).
    make_logo(ICONS / "pwa-192.png", 192, width_frac=0.58)
    make_logo(ICONS / "pwa-512.png", 512, width_frac=0.58)
    make_logo(ICONS / "apple-touch-icon-180.png", 180, width_frac=0.58)
    # Favicon: tighter (0.72) since the browser shows it tiny and un-masked.
    make_favicon_svg(OUT / "favicon.svg")
    make_logo(OUT / "favicon-32.png", 32, width_frac=0.72)
    make_logo(OUT / "favicon-16.png", 16, width_frac=0.72)
    # Notification badge: monochrome white bell on transparent (Android renders it as a mask).
    make_bell(ICONS / "badge-72.png", 72, background=TRANSPARENT)
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
