// The single source for every published privacy text in Avviksvarsel (plan
// 2026-08-31-001, U3/KTD9). Components, the /varsel/personvern page, the
// /togmelding parity test and the node test suite all read from here — no
// component carries its own copy of a published wording.
//
// IMPORTANT — versioned consent: a registering user consents to the texts in
// REGISTRATION_TEXTS (bottom of this file), under the version stamped on their
// journey record. Changing ANY of those texts requires bumping
// CONSENT_TEXT_VERSION *and* updating the pin in test/personvern.test.mjs.
//
// What the guard actually enforces, stated exactly, because it used to promise
// more than it kept: the pinned sha256[:8] covers CONSENT_TEXT_VERSION AND the
// texts together, and the version literal is asserted beside it in the same
// PINNED_CONSENT block. So a text edit breaks the digest, a version bump breaks
// the digest, and neither can move alone — but the guard cannot tell a bump
// from a re-pin. It puts the decision in front of whoever edits, in one block,
// with the reason attached; it does not make it for them. (Same derived-version
// reasoning as disruptions/api/llm_message.py's PROMPT_VERSION.)
//
// Texts OUTSIDE REGISTRATION_TEXTS — the privacy page T5, the T4 card, T6, T7 —
// are not consented to at registration and are deliberately not versioned. They
// have their own guards further down the suite.
//
// This module is imported directly by `node --test` via type-stripping (the
// alertFeedbackState.ts pattern): keep it free of runtime imports and of
// non-erasable TS syntax (no enums, no namespaces, no decorators).

/** Version of the registration-visible texts, persisted on each journey record
 * as `consent_text_version`. Format: YYYY-MM-DD.n — bump the date (or the
 * counter for a same-day change) whenever a text in REGISTRATION_TEXTS changes. */
export const CONSENT_TEXT_VERSION = '2026-08-31.1';

/** Canonical path of the privacy page. In-app links use the trailing-slash form
 * so the server never has to answer with a redirect. */
export const PERSONVERN_PATH = '/varsel/personvern/';

/** Structurally compatible with BrandId in src/brand.ts — declared locally so
 * this module stays importable by node's test runner with no app imports. */
export type PersonvernBrandId = 'entur' | 'none';

/** Privacy-page URL that preserves the brand across navigation (the PWA has no
 * address bar): `none` keeps its query parameter, everything else — including
 * an unknown brand — gets the plain Entur page. */
export function personvernUrl(brandId: PersonvernBrandId): string {
  return brandId === 'none' ? `${PERSONVERN_PATH}?brand=none` : PERSONVERN_PATH;
}

/** A text that renders with a trailing Link component. The plan appendix's
 * [text](url) notation denotes this structure — never literal markdown, which
 * is why no exported string may contain '[' or ']'. It carries no href on
 * purpose: the target depends on the brand the caller is rendering, so every
 * consumer builds it with `personvernUrl(brand.id)`. A stored default would be
 * a field nothing reads, and editing it would silently change nothing. */
export interface LinkedText {
  prose: string;
  linkText: string;
}

const PERSONVERN_LINK_TEXT = 'Slik behandler vi opplysningene';

/** T1 — experiment disclaimer at the top of /varsel. */
export const T1_EKSPERIMENT = {
  title: 'Dette er et eksperiment',
  body:
    'Avviksvarsel er et eksperiment fra Enturs KI-lab. Vi tester om varsler før avviket rammer ' +
    'er til nytte. Tjenesten kan endres eller avsluttes på kort varsel, og varslene er anslag ' +
    'som ikke alltid treffer — ikke planlegg reisen din på dem alene.',
};

/** T2 — privacy line under the intro. */
export const T2_PERSONVERNLINJE: LinkedText = {
  prose: 'Entur AS lagrer reisene du registrerer, slik at vi kan varsle deg.',
  linkText: PERSONVERN_LINK_TEXT,
};

/** T3 — registration text in «Legg til reise», above the buttons. */
export const T3_REGISTRERING: LinkedText = {
  prose:
    'Vi lagrer holdeplassene, ukedagene og tidsrommet du velger her. Vi trenger dem for å vite ' +
    'hvilke avvik som gjelder deg. Du kan slette reisen når som helst.',
  linkText: PERSONVERN_LINK_TEXT,
};

/** T4 — notification card. Deliberately claims only that «Skru av» deletes the
 * varselabonnement: the technical key survives «Skru av» (in localStorage and
 * on every journey record) and is deleted by «Slett alt». */
export const T4_VARSELKORT =
  'For å sende varsler lagrer vi et varselabonnement for denne nettleseren. Det brukes bare ' +
  'til å varsle om reisene dine, og slettes når du trykker «Skru av». Den tekniske nøkkelen ' +
  'som knytter reisene til nettleseren slettes med «Slett alt». Innholdet i varselet er ' +
  'kryptert, slik at tjenesten som frakter det ikke kan lese det.';

/** T5 — the privacy page at /varsel/personvern/. */
export const T5_PAGE_HEADING = 'Personvern i Avviksvarsel';

export const T5_INTRO =
  'Avviksvarsel er et eksperiment fra Enturs KI-lab. Denne siden forklarer hvilke opplysninger ' +
  'vi behandler når du bruker tjenesten.';

export interface PersonvernSection {
  title: string;
  body: string;
}

export const T5_SECTIONS: readonly PersonvernSection[] = [
  {
    title: 'Behandlingsansvarlig',
    body: 'Entur AS, Postboks 1800, 0048 Oslo. Personvernombudet nås på personvern@entur.org.',
  },
  {
    title: 'Hva vi lagrer',
    body:
      'Reisene du registrerer: holdeplass du reiser fra, holdeplass du reiser til, ' +
      'transportmiddel, tidsrom og ukedager — sammen med en teknisk nøkkel som knytter dem til ' +
      'nettleseren din. Slår du på varsler, lagrer vi i tillegg et varselabonnement for ' +
      'nettleseren. Når vi sender et varsel, lagres det som ble sendt, sammen med den tekniske ' +
      'nøkkelen, i driftsloggene våre; det samme gjelder som kvittering når du bruker ' +
      '«Slett alt». Vi lagrer ikke navn, e-postadresse eller telefonnummer, og du trenger ingen ' +
      'bruker for å bruke tjenesten.',
  },
  {
    title: 'Hvorfor',
    body:
      'For å varsle deg når et avvik kan ramme en reise du har registrert, og for å forbedre ' +
      'varslene.',
  },
  {
    title: 'Rettslig grunnlag',
    body:
      'Registrering av reiser og varsler behandles på grunnlag av ditt samtykke, jf. ' +
      'personvernforordningen art. 6 nr. 1 bokstav a. Du gir samtykket ved å registrere en ' +
      'reise, og trekker det tilbake ved å slette den. Forbedring av varslene skjer på grunnlag ' +
      'av vår berettigede interesse i å vite om tjenesten er til nytte, jf. art. 6 nr. 1 ' +
      'bokstav f.',
  },
  {
    title: 'Hvem som kan se opplysningene',
    body:
      'Ansatte i Entur som arbeider med tjenesten kan se hvilke reiser som er registrert, for å ' +
      'vurdere om varslene treffer. Vurderingene av varslene — med strekning og avgangstid, men ' +
      'uten noe som identifiserer deg eller enheten din — vises også på åpne evalueringssider. ' +
      'Google Cloud drifter tjenesten på Enturs vegne, i EU. Varselteksten skrives av en ' +
      'språkmodell hos Google i EU, som får vite hvilken strekning og avgang varselet gjelder. ' +
      'Tilbakemeldinger du sender behandles i analyseverktøyet PostHog i EU, på Enturs vegne. ' +
      'Tjenesten som leverer varselet til telefonen din ser ikke innholdet — det er kryptert.',
  },
  {
    title: 'Hvor lenge',
    body:
      'Reiser du har registrert lagres til du sletter dem. Varselabonnementet slettes når du ' +
      'skrur av varsler eller bruker «Slett alt». Loggen over varsler vi har vurdert å sende ' +
      'lagres så lenge eksperimentet pågår; den identifiserer ikke enheten din. Driftslogger ' +
      'slettes automatisk etter en fast periode og omfattes ikke av «Slett alt». ' +
      'Tilbakemeldinger du sender lagres i inntil 45 dager.',
  },
  {
    title: 'Rettighetene dine',
    body:
      'Du kan slette reisene dine og varselabonnementet direkte i appen, med «Slett alt» eller ' +
      'reise for reise. Du har også rett til innsyn, retting, begrensning, protest og ' +
      'dataportabilitet, jf. art. 15 til 21. Fordi tjenesten ikke vet hvem du er, må du bruke ' +
      'enheten du registrerte reisene fra for at vi skal kunne finne dem.',
  },
  {
    title: 'Klage',
    body:
      'Mener du at vi behandler opplysningene dine i strid med regelverket, kan du klage til ' +
      'Datatilsynet.',
  },
  {
    title: 'Overføring ut av EØS',
    body: 'Behandlingen skjer i EU/EØS.',
  },
];

/** T6 — the «Slett alt» flow: button, confirm dialog, done-notice. The dialog
 * body enumerates exactly what is deleted (journeys, the varselabonnement, the
 * technical key) — the scope that keeps «Alt er slettet» true while operational
 * logs and the decision log survive. */
export const T6_SLETT_ALT = {
  buttonText: 'Slett alt',
  dialogTitle: 'Slette alt?',
  dialogBody:
    'Dette sletter de registrerte reisene dine, varselabonnementet og den tekniske nøkkelen ' +
    'til denne nettleseren. Det kan ikke angres.',
  cancelText: 'Avbryt',
  confirmText: 'Slett alt',
  doneNotice: 'Alt er slettet. Du kan registrere en ny reise når som helst.',
};

/** T7 — sender line in the footer, rendered in all brand variants (also on
 * /togmelding, where the parity test asserts the duplicated copy matches). */
export const T7_AVSENDER: LinkedText = {
  prose: 'Tjenesten leveres av Entur AS.',
  linkText: PERSONVERN_LINK_TEXT,
};

// Strings shared with the committed /togmelding page
// (disruptions/api/togmelding_static/index.html). That page has no build step,
// so it carries duplicated copies; a Python parity test (plan U8) reads both
// files and asserts they match.
export const TOGMELDING_SENDER_LINE = T7_AVSENDER.prose;
export const TOGMELDING_PERSONVERN_HREF = PERSONVERN_PATH;
export const TOGMELDING_DISCLAIMER = {
  title: 'Dette er et eksperiment',
  body:
    'Togmeldingen er et eksperiment fra Enturs KI-lab. Prognosene er anslag som ikke alltid ' +
    'treffer — ikke planlegg reisen din på dem alene. Tjenesten kan endres eller avsluttes på ' +
    'kort varsel.',
};

/** The texts a registering user sees and consents to: the on-page notices T1
 * and T2 plus the registration text T3 in «Legg til reise». The test suite pins
 * sha256[:8] of CONSENT_TEXT_VERSION followed by this array, joined with '\n' —
 * so changing any entry breaks the digest, and the pin block that has to be
 * touched to green it again is the one holding the version literal. */
export const REGISTRATION_TEXTS: readonly string[] = [
  T1_EKSPERIMENT.title,
  T1_EKSPERIMENT.body,
  T2_PERSONVERNLINJE.prose,
  T2_PERSONVERNLINJE.linkText,
  T3_REGISTRERING.prose,
  T3_REGISTRERING.linkText,
];
