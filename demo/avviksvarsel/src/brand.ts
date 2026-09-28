// Which brand this page renders in.
//
// `?brand=none` selects the unbranded variant, for showing the concept to an audience that should
// not read it as an Entur product. Anything else — no parameter, a typo, a brand not built yet —
// is Entur, which is what /varsel has always been and stays by default.
//
// A brand is FOUR things, and leaving any one of them out still reads as Entur:
//   1. colour   — src/brands/<id>.css, mostly generated (see scripts/make_brand_css.py)
//   2. typeface — Linje sets Entur's proprietary Nationale on :root; the brand sheet overrides it
//   3. logo     — Linje's <Logo/> in the masthead
//   4. identity — tab title, PWA manifest, home-screen icon, and the icon on a push notification
//
// Adding an operator brand is an entry here plus a `Brand` in make_brand_css.py — not a rewrite.
// Note that `none` is a REMOVAL and needs no assets; a real operator brand needs their logo and
// colours, and permission to serve them.

export type BrandId = 'entur' | 'none';

export interface Brand {
  id: BrandId;
  /** Masthead heading. */
  heading: string;
  /** Tab title and PWA name. The environment suffix is appended by the caller. */
  documentTitle: string;
  /** Linje's Entur <Logo/> beside the heading. */
  showEnturLogo: boolean;
  /** Path to this brand's PWA manifest, so an install keeps the brand it was made from. */
  manifest: string;
  faviconSvg: string;
  faviconPng32: string;
  faviconPng16: string;
  appleTouchIcon: string;
  /** <meta name="theme-color"> — the phone's status bar behind the header band. Match the band. */
  themeColor: string;
}

// Vite's base is '/varsel/' (see vite.config.ts); index.html writes the Entur defaults literally.
const B = import.meta.env.BASE_URL;

const BRANDS: Record<BrandId, Brand> = {
  entur: {
    id: 'entur',
    heading: 'Smarte reisevarsler',
    documentTitle: 'Entur Smarte reisevarsler',
    showEnturLogo: true,
    manifest: `${B}manifest.webmanifest`,
    faviconSvg: `${B}favicon.svg`,
    faviconPng32: `${B}favicon-32.png`,
    faviconPng16: `${B}favicon-16.png`,
    appleTouchIcon: `${B}icons/apple-touch-icon-180.png`,
    themeColor: '#181c56', // --lavender-90, the Contrast band
  },
  none: {
    id: 'none',
    heading: 'Smarte reisevarsler',
    documentTitle: 'Smarte reisevarsler',
    showEnturLogo: false,
    manifest: `${B}manifest-none.webmanifest`,
    faviconSvg: `${B}favicon-none.svg`,
    faviconPng32: `${B}favicon-none-32.png`,
    faviconPng16: `${B}favicon-none-16.png`,
    appleTouchIcon: `${B}icons/apple-touch-icon-180-none.png`,
    themeColor: '#002c2d', // what the generated sheet recolours the Contrast band to
  },
};

export const DEFAULT_BRAND: BrandId = 'entur';

/** The brand named by `?brand=`, or Entur for absent, unknown or malformed values. */
export function resolveBrand(search: string = location.search): Brand {
  const requested = new URLSearchParams(search).get('brand');
  return (requested && BRANDS[requested as BrandId]) || BRANDS[DEFAULT_BRAND];
}

/**
 * Point the document's head at this brand's assets.
 *
 * Unconditional, including for Entur: index.html carries the Entur assets so the default paints
 * without waiting for the bundle, and re-applying them here is what stops the two copies drifting
 * into disagreement. `data-brand` is normally already set by the inline script in index.html —
 * setting it again corrects an unknown slug (`?brand=vy` today) back to what is actually rendering.
 */
export function applyBrand(brand: Brand): void {
  document.documentElement.dataset.brand = brand.id;

  const setLink = (selector: string, href: string) => {
    document.head.querySelector<HTMLLinkElement>(selector)?.setAttribute('href', href);
  };
  // The manifest is read when the user installs, not at load, so swapping it here is in time.
  setLink('link[rel="manifest"]', brand.manifest);
  setLink('link[rel="icon"][type="image/svg+xml"]', brand.faviconSvg);
  setLink('link[rel="icon"][sizes="32x32"]', brand.faviconPng32);
  setLink('link[rel="icon"][sizes="16x16"]', brand.faviconPng16);
  setLink('link[rel="apple-touch-icon"]', brand.appleTouchIcon);
  document.head
    .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    ?.setAttribute('content', brand.themeColor);
}
