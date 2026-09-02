// The four on-page privacy notices T1-T4 of plan 2026-08-31-001, plus the sender footer T7.
//
// Purely presentational: every word comes from src/personvern.ts, which is the single source for
// published privacy texts and the only place a wording may be edited (a registration-visible text
// also has to move CONSENT_TEXT_VERSION, which that module's test suite enforces). A component
// here must therefore never carry a Norwegian string of its own.
//
// The link is built with `personvernUrl(brandId)` rather than the LinkedText's own `href`: that
// field is the Entur default, and the PWA has no address bar, so a `?brand=none` install that
// navigated to the plain path would come back Entur-branded.

import { SmallAlertBox } from '@entur/alert';
import { ExpandableText } from '@entur/expand';
import { Link, SubParagraph } from '@entur/typography';
import type { BrandId } from '../brand';
import {
  personvernUrl,
  T1_EKSPERIMENT,
  T2_PERSONVERNLINJE,
  T3_REGISTRERING,
  T4_TITTEL,
  T4_VARSELKORT,
  T7_AVSENDER,
  type LinkedText,
} from '../personvern';

/** Prose followed by the privacy-page link. The appendix's `[text](url)` notation denotes exactly
 *  this — a rendered Link, never markdown in a string. */
function LinkedProse({ text, brandId }: { text: LinkedText; brandId: BrandId }) {
  return (
    <>
      {text.prose}{' '}
      <Link href={personvernUrl(brandId)}>{text.linkText}</Link>
    </>
  );
}

/** T1 — the experiment disclaimer at the top of the page. One line and no title (design review
 *  2026-09-01, finding 29): the sentence is its own headline, and on a phone the first screen has
 *  to reach «Legg til reise». Same SmallAlertBox shape as the togvarsel notice further down. */
export function EksperimentNotis() {
  return (
    <SmallAlertBox variant="information" width="fluid">
      {T1_EKSPERIMENT.body}
    </SmallAlertBox>
  );
}

/** T2 — the privacy line directly under the intro sentence. */
export function PersonvernLinje({ brandId }: { brandId: BrandId }) {
  return (
    <SubParagraph className="av-personvern-linje" margin="none">
      <LinkedProse text={T2_PERSONVERNLINJE} brandId={brandId} />
    </SubParagraph>
  );
}

/** T3 — what «Legg til reise» collects, shown above the button row. This is the information that
 *  makes the confirmation informed; the plan deliberately keeps registration confirmation-style,
 *  so there is no checkbox and no "ved å trykke lagre samtykker du"-phrasing here. */
export function RegistreringsNotis({ brandId }: { brandId: BrandId }) {
  return (
    <SubParagraph className="av-personvern-notis" margin="none">
      <LinkedProse text={T3_REGISTRERING} brandId={brandId} />
    </SubParagraph>
  );
}

/** T4 — what the notification card collects, behind an expandable heading (finding 41). Still
 *  rendered BEFORE the user turns push on — the fold label is on screen, the text one tap away —
 *  and still word for word the consented text; only the placement changed. */
export function VarselkortNotis() {
  return (
    <div className="av-personvern-notis av-personvern-fold">
      <ExpandableText title={T4_TITTEL} titleElement="SubParagraph">
        <SubParagraph margin="none">{T4_VARSELKORT}</SubParagraph>
      </ExpandableText>
    </div>
  );
}

/** T7 — the sender line, at the bottom of the page. Rendered in EVERY brand variant, `?brand=none`
 *  included: art. 13(1)(a) makes the controller's identity non-optional, which is why the
 *  unbranded variant no longer keeps the word «Entur» off the page. Only the page text names the
 *  sender — notifications, the manifest and the icons stay unbranded (see README, «Brands»). */
export function AvsenderFooter({ brandId }: { brandId: BrandId }) {
  return (
    <footer className="av-footer">
      <SubParagraph margin="none">
        <LinkedProse text={T7_AVSENDER} brandId={brandId} />
      </SubParagraph>
    </footer>
  );
}
