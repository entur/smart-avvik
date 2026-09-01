// The published privacy texts /togmelding shows: the experiment disclaimer (T1's togmelding
// wording) and the sender footer (T7). A guarded DUPLICATE of demo/avviksvarsel/src/personvern.ts,
// which is the single source for every published privacy text.
//
// Why duplicated rather than imported. This is its own npm app, and the image's node stage copies
// only `demo/togmelding/` into its build directory (root Dockerfile), so a relative import reaching
// into `../avviksvarsel/src/` would resolve on a laptop and fail the image build — the worst shape
// of breakage, since it passes locally. Until 2026-09-01 the reason was different (the page was
// committed vanilla HTML with no build step at all) but the conclusion and the guard are the same
// (plan 2026-08-31-001, KTD8): `disruptions/api/test_personvern_texts.py` resolves these constants
// out of BOTH modules and asserts they are identical. Edit both, or that suite fails.
//
// No brand variants here, unlike /varsel: `?brand=none` installs are never linked to this page
// (togvarsel_notify.build_payload keeps their click-through inside /varsel), so the href is the
// plain path and the sender is always named.

export const TOGMELDING_SENDER_LINE = 'Tjenesten leveres av Entur AS.';

export const TOGMELDING_PERSONVERN_LINK_TEXT = 'Slik behandler vi opplysningene';

/** Trailing-slash form, so the server never has to answer with a redirect. */
export const TOGMELDING_PERSONVERN_HREF = '/varsel/personvern/';

export const TOGMELDING_DISCLAIMER = {
  title: 'Dette er et eksperiment',
  body:
    'Togmeldingen er et eksperiment fra Enturs KI-lab. Prognosene er anslag som ikke alltid ' +
    'treffer — ikke planlegg reisen din på dem alene. Tjenesten kan endres eller avsluttes på ' +
    'kort varsel.',
};
