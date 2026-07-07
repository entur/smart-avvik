#!/usr/bin/env python3
"""Generate the Avviksvarsel PWA icons — a white notification bell on Entur Lavender.

Pure geometry with Pillow (no SVG toolchain needed). Regenerate with:
    uv run --with pillow python demo/avviksvarsel/scripts/make_icons.py
Outputs into demo/avviksvarsel/public/icons/: pwa-192, pwa-512, apple-touch-icon-180 (opaque,
full-bleed so the maskable/apple masks look right) and badge-72 (white glyph on transparent, for
the monochrome Android notification badge).
"""

from pathlib import Path

from PIL import Image, ImageDraw

LAVENDER_90 = (24, 28, 86, 255)  # Entur --lavender-90 #181c56
WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)
SS = 4  # supersample factor for crisp anti-aliased edges

OUT = Path(__file__).resolve().parent.parent / "public" / "icons"


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


def make(path: Path, size: int, *, background, glyph=WHITE) -> None:
    canvas = Image.new("RGBA", (size * SS, size * SS), background)
    _draw_bell(ImageDraw.Draw(canvas), size * SS, fill=glyph)
    canvas.resize((size, size), Image.LANCZOS).save(path)
    print(f"  wrote {path.name} ({size}x{size})")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    make(OUT / "pwa-192.png", 192, background=LAVENDER_90)
    make(OUT / "pwa-512.png", 512, background=LAVENDER_90)
    make(OUT / "apple-touch-icon-180.png", 180, background=LAVENDER_90)
    # Badge: monochrome white glyph on transparent (Android renders it as a mask).
    make(OUT / "badge-72.png", 72, background=TRANSPARENT)
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
