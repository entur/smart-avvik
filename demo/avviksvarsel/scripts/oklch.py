"""sRGB <-> OKLCh, plus a gamut-fitting recolour — the colour maths behind make_brand_css.py.

OKLCh is the space the brand recolouring works in because its L is perceptual: keeping L and
moving only hue/chroma preserves the contrast relationships Entur tuned the Linje ramps for, so a
reskin cannot quietly break the WCAG contrast of body text on a card. Doing the same in HSL or by
hand does not hold that property — HSL "lightness" is a different quantity per hue, so a violet
and a teal at the same HSL L land at visibly different perceived brightness.

Reference: Björn Ottosson, "A perceptual color space for image processing" (2020).
"""

from __future__ import annotations

import math

# ---- sRGB transfer function ------------------------------------------------------------------


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


# ---- OKLab -------------------------------------------------------------------------------------


def _linear_srgb_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _oklab_to_linear_srgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_**3, m_**3, s_**3
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


# ---- public API ----------------------------------------------------------------------------


def rgb_to_oklch(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """(0-255, 0-255, 0-255) -> (L 0-1, C, h degrees)."""
    r, g, b = (_srgb_to_linear(v / 255) for v in rgb)
    L, a, bb = _linear_srgb_to_oklab(r, g, b)
    return L, math.hypot(a, bb), math.degrees(math.atan2(bb, a)) % 360


def oklch_to_rgb(L: float, C: float, h: float) -> tuple[int, int, int]:
    """(L, C, h) -> (0-255, 0-255, 0-255), chroma-reduced until it fits sRGB.

    Out-of-gamut is the ordinary case, not an edge case: rotating a dark violet to teal at the same
    chroma lands outside sRGB more often than not. Clipping each channel independently would shift
    both hue and lightness (the two things this module exists to control), so the fit walks chroma
    down instead and leaves L and h alone.
    """
    lo, hi = 0.0, C
    for _ in range(24):
        mid = (lo + hi) / 2
        rad = math.radians(h)
        lin = _oklab_to_linear_srgb(L, mid * math.cos(rad), mid * math.sin(rad))
        if all(-1e-6 <= v <= 1 + 1e-6 for v in lin):
            lo = mid
        else:
            hi = mid
    rad = math.radians(h)
    lin = _oklab_to_linear_srgb(L, lo * math.cos(rad), lo * math.sin(rad))
    return tuple(max(0, min(255, round(_linear_to_srgb(min(max(v, 0.0), 1.0)) * 255))) for v in lin)
