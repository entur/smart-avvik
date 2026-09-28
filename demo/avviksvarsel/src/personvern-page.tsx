// The privacy page at /varsel/personvern/ — the SECOND Vite entry (see vite.config.ts), not a
// client route: the app has no router and the API's static mount has no SPA fallback, so a client
// route would 404 on the direct hits this page exists for (a link from a push, a pasted URL).
//
// Every published word on this page comes from src/personvern.ts. Nothing here composes, shortens
// or re-words a privacy text: the module is the single source that the node suite, the /togmelding
// parity test and the consent-version hash are all pinned to, and a copy living in this file would
// be outside every one of those guards.

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

// Entur Linje. This is main.tsx's import block VERBATIM — same packages, same order — and it has
// to be, even though this page renders four of them. Not symmetry: correctness.
//
// A second entry turns the build into an MPA, and Rollup then hoists the modules the two entries
// SHARE into a common chunk linked BEFORE each entry's own. Import only what this page renders and
// the split falls where the two lists diverge: tokens/typography/layout/menu and the brand sheet
// go in the shared chunk, while button/form/dropdown/chip/alert/modal/a11y/loader stay in main's —
// i.e. AFTER the brand sheet, in the app's own document.
//
// That is fatal for the reskin, because ~80 of the generated brand rules are written
// `:where(:root[data-brand='none']) .eds-button--variant-tertiary` — zero-specificity scope, so
// they tie EXACTLY with Linje's own `.eds-button--variant-tertiary` and win on source order alone.
// (The :where() is deliberate: without it the +1 attribute would outrank and flatten Linje's own
// `.eds-contrast .eds-h1` hierarchy — see scripts/make_brand_css.py.) Reverse the order and the
// tie flips: measured on the first build of this page, brand.css carried
// `.eds-button--variant-tertiary{color:#002c2d}` and main.css re-asserted Linje's `#181c56`
// afterwards. Nothing errors, no test fails, and /varsel's buttons quietly go back to Entur navy
// on the unbranded variant.
//
// Keeping the lists identical makes every Linje sheet shared, so the brand sheet is last within
// the shared chunk exactly as it was in the single-entry build, and main's own chunk holds
// nothing but src/styles.css — `.av-*` classes only, which cannot tie with a `.eds-*` rule. The
// bytes are not wasted either: a reader arrives here from the app, which has already cached this
// chunk. KEEP THIS BLOCK IN STEP WITH main.tsx.
import '@entur/tokens/dist/base.css';
import '@entur/tokens/dist/styles.css';
import '@entur/typography/dist/styles.css';
import '@entur/layout/dist/styles.css';
import '@entur/button/dist/styles.css';
import '@entur/form/dist/styles.css';
import '@entur/dropdown/dist/styles.css';
import '@entur/chip/dist/styles.css';
import '@entur/alert/dist/styles.css';
import '@entur/modal/dist/styles.css';
import '@entur/expand/dist/styles.css';
import '@entur/menu/dist/styles.css';
import '@entur/a11y/dist/styles.css';
import '@entur/loader/dist/styles.css';

// Brand overrides LAST, for the reason main.tsx gives: they win on specificity
// (`:root[data-brand=…]`) — or, for the `:where()`-scoped rules, on source order — but only over
// rules that already exist. index.html has set data-brand from the same parameter before first
// paint, so the palette is right from the first frame.
import './brands/entur.css';
import './brands/none.css';

import { Contrast } from '@entur/layout';
import { Logo } from '@entur/menu';
import { Heading1, Heading2, LeadParagraph, Link, Paragraph, SubParagraph } from '@entur/typography';
import { applyBrand, resolveBrand } from './brand';
import {
  consentTextDate,
  T5_INTRO,
  T5_LAST_UPDATED_LABEL,
  T5_PAGE_HEADING,
  T5_SECTIONS,
} from './personvern';
import './personvern-page.css';

const brand = resolveBrand();
applyBrand(brand);

// The static <title> in personvern/index.html carries the same words so the tab is right before
// the bundle runs; setting it again from the module is what keeps the two from drifting. No
// environment suffix here, unlike the app: that would cost a /config round trip on a page whose
// whole value is that it renders when nothing else does.
document.title = T5_PAGE_HEADING;

// Where «Tilbake til Smarte reisevarsler» goes. The brand has to survive the hop — an installed PWA has
// no address bar to put it back with — so the unbranded variant carries its parameter along,
// exactly as personvernUrl() does in the other direction. BASE_URL is Vite's '/varsel/'.
const appBase = import.meta.env.BASE_URL;
const appHref = brand.id === 'none' ? `${appBase}?brand=none` : appBase;

function PersonvernPage() {
  return (
    <>
      <Contrast as="header" className="pv-header">
        <div className="pv-header-inner">
          {/* Same rule as the app masthead: the Entur mark is the loudest branding on the page and
              the unbranded variant drops it outright. The sender is named in the page text, which
              is what art. 13(1)(a) is actually about. */}
          {brand.showEnturLogo && <Logo />}
          <Heading1 as="h1" margin="none" className="pv-header-title">
            {T5_PAGE_HEADING}
          </Heading1>
        </div>
      </Contrast>

      <main id="main-content" className="pv-main">
        {/* Mandatory, and at the top rather than the bottom: the page is inside the PWA's scope,
            so an installed app opens it in-app with no browser back button — a reader who cannot
            get back is stranded in a standalone window. */}
        <Paragraph className="pv-back" margin="none">
          <Link href={appHref}>Tilbake til Smarte reisevarsler</Link>
        </Paragraph>

        <LeadParagraph className="pv-intro">{T5_INTRO}</LeadParagraph>

        {/* The date every registration is stamped with (CONSENT_TEXT_VERSION), shown as the
            page's own «Sist oppdatert» so the two cannot disagree (design review 2026-09-01,
            finding 44). */}
        <SubParagraph className="pv-updated" margin="none">
          {T5_LAST_UPDATED_LABEL} {consentTextDate()}.
        </SubParagraph>

        {/* Each section title is a real <h2>, never bold body text: this page is a list of ten
            topics people arrive wanting one of, and heading navigation is how a screen-reader user
            reaches «Rettighetene dine» without listening to «Hva vi lagrer» first. The body keeps
            its line breaks (controller and DPO are two art. 13 items on two lines), and a section's
            links are real links: «du kan klage til Datatilsynet» without saying where was half an
            answer (finding 43). */}
        {T5_SECTIONS.map((section) => (
          <section key={section.title} className="pv-section">
            <Heading2 as="h2" margin="none">
              {section.title}
            </Heading2>
            <Paragraph margin="none" className="pv-body">
              {section.body}
            </Paragraph>
            {section.links && (
              <ul className="pv-links">
                {section.links.map((link) => (
                  <li key={link.href}>
                    <Link href={link.href}>{link.text}</Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}
      </main>
    </>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PersonvernPage />
  </StrictMode>,
);
