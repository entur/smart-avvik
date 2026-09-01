import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';

import * as personvern from '../src/personvern.ts';
import {
  CONSENT_TEXT_VERSION,
  REGISTRATION_TEXTS,
  T1_EKSPERIMENT,
  T4_VARSELKORT,
  T5_PAGE_HEADING,
  T5_SECTIONS,
  T6_SLETT_ALT,
  T7_AVSENDER,
  personvernUrl,
} from '../src/personvern.ts';

// THE CONSENT PIN. One block, two values, and they are deliberately not
// independent: the digest is sha256[:8] over CONSENT_TEXT_VERSION *followed by*
// every entry of REGISTRATION_TEXTS, joined with '\n'. So editing a
// registration-visible text breaks the digest, and bumping the version breaks
// it too — neither can move on its own, and the only place that compiles again
// is right here, where the version literal sits next to the hash.
//
// What it cannot do is prove the editor bumped rather than merely re-pinned;
// nothing local can. What it does is put that choice in front of them, in one
// block, with the reason attached — the same derived-version reasoning as
// disruptions/api/llm_message.py's PROMPT_VERSION.
const PINNED_CONSENT = {
  version: '2026-08-31.1',
  hash: '0d049fa7',
};

/** sha256[:8] of the exact corpus a registering user consents to, version included. */
const consentDigest = () =>
  createHash('sha256')
    .update([CONSENT_TEXT_VERSION, ...REGISTRATION_TEXTS].join('\n'), 'utf8')
    .digest('hex')
    .slice(0, 8);

const collectStrings = (value, out = []) => {
  if (typeof value === 'string') {
    out.push(value);
  } else if (Array.isArray(value)) {
    for (const item of value) collectStrings(item, out);
  } else if (value && typeof value === 'object') {
    for (const item of Object.values(value)) collectStrings(item, out);
  }
  return out;
};

test('no exported text contains a bracket placeholder (AE7)', () => {
  const exported = Object.entries(personvern).filter(([, v]) => typeof v !== 'function');
  const strings = collectStrings(exported.map(([, v]) => v));
  assert.ok(
    strings.length >= 25,
    `expected the full text corpus to be exported as strings, found only ${strings.length}`,
  );
  for (const text of strings) {
    assert.ok(
      !text.includes('[') && !text.includes(']'),
      `exported text contains '[' or ']' — the appendix's [text](url) notation denotes a rendered Link, never literal brackets: ${text}`,
    );
  }
});

test('CONSENT_TEXT_VERSION is a dated version string', () => {
  assert.match(CONSENT_TEXT_VERSION, /^\d{4}-\d{2}-\d{2}\.\d+$/);
});

test('required phrases: sender line, experiment disclaimer, and all nine T5 sections', () => {
  assert.ok(T7_AVSENDER.prose.includes('Entur AS'), 'the sender line must name Entur AS');
  assert.ok(T1_EKSPERIMENT.body.includes('eksperiment'), 'T1 must say this is an experiment');
  assert.equal(T5_PAGE_HEADING, 'Personvern i Avviksvarsel');
  assert.deepEqual(
    T5_SECTIONS.map((section) => section.title),
    [
      'Behandlingsansvarlig',
      'Hva vi lagrer',
      'Hvorfor',
      'Rettslig grunnlag',
      'Hvem som kan se opplysningene',
      'Hvor lenge',
      'Rettighetene dine',
      'Klage',
      'Overføring ut av EØS',
    ],
  );
  for (const section of T5_SECTIONS) {
    assert.ok(section.body.length > 0, `T5 section «${section.title}» must have a body`);
  }
});

test('T4 scopes «Skru av» to the varselabonnement and reserves key deletion for «Slett alt»', () => {
  assert.ok(T4_VARSELKORT.includes('varselabonnement'), 'T4 must name the varselabonnement');
  assert.ok(T4_VARSELKORT.includes('«Skru av»'), 'T4 must name the «Skru av» action');
  const sentences = T4_VARSELKORT.split('.').map((sentence) => sentence.trim());
  const keySentence = sentences.find((sentence) => sentence.includes('tekniske nøkkelen'));
  assert.ok(keySentence, 'T4 must have a sentence about the technical key');
  assert.ok(
    keySentence.includes('«Slett alt»'),
    'the technical key is deleted by «Slett alt», and T4 must say so',
  );
  assert.ok(
    !keySentence.includes('Skru av'),
    'T4 must not claim «Skru av» deletes the technical key — it survives in localStorage and on every journey record',
  );
});

test('T6 dialog stays enumerative: it names the three things «Slett alt» deletes', () => {
  for (const named of ['reisene', 'varselabonnementet', 'nøkkelen']) {
    assert.ok(
      T6_SLETT_ALT.dialogBody.includes(named),
      `the T6 dialog body must name '${named}' — the enumeration is what keeps «Alt er slettet» true`,
    );
  }
  // Inclusion alone is only half the guard (T4 above already pairs the two). An enumeration is
  // kept honest at BOTH ends: «Slett alt» reaches the journeys, the varselabonnement and the
  // technical key, and nothing else. The operational logs, the BigQuery decision log and any
  // feedback already sent all SURVIVE it — see the survival table in
  // docs/personvern/2026-08-31-avviksvarsel-personvern-tekster-beslutninger.md §3 — so a dialog
  // that grew a reassuring extra noun would promise an erasure the endpoint does not perform.
  const survives = {
    logg: 'the operational logs (push sends, the deletion receipt) and the BigQuery decision log',
    tilbakemelding: 'feedback already sent (PostHog, and the BigQuery contexts)',
    beslutning: 'the BigQuery decision log, which is keyed on the journey definition',
  };
  const body = T6_SLETT_ALT.dialogBody.toLowerCase();
  for (const [word, what] of Object.entries(survives)) {
    assert.ok(
      !body.includes(word),
      `the T6 dialog body mentions '${word}', but «Slett alt» does not erase ${what}. ` +
        'The dialog may only claim what the endpoint actually deletes — journeys, the ' +
        'varselabonnement, the technical key — or «Alt er slettet» becomes a false promise.',
    );
  }
});

test('personvernUrl preserves the brand across navigation', () => {
  assert.equal(personvernUrl('entur'), '/varsel/personvern/');
  assert.equal(personvernUrl('none'), '/varsel/personvern/?brand=none');
});

test('editing a registration-visible text requires bumping CONSENT_TEXT_VERSION and this pin', () => {
  assert.ok(Array.isArray(REGISTRATION_TEXTS) && REGISTRATION_TEXTS.length > 0);
  // Both halves are asserted. Pinning only the digest let a text edit go green on a re-pinned
  // hash alone, leaving two materially different wordings stamped with one version — exactly
  // what the version exists to prevent.
  assert.equal(
    CONSENT_TEXT_VERSION,
    PINNED_CONSENT.version,
    `CONSENT_TEXT_VERSION is '${CONSENT_TEXT_VERSION}', pinned '${PINNED_CONSENT.version}'. ` +
      'A version bump is intended whenever a registration-visible text changes: update ' +
      'PINNED_CONSENT.version and PINNED_CONSENT.hash together.',
  );
  const digest = consentDigest();
  assert.equal(
    digest,
    PINNED_CONSENT.hash,
    `The consent corpus changed (sha256[:8] of CONSENT_TEXT_VERSION + REGISTRATION_TEXTS is now ` +
      `'${digest}', pinned '${PINNED_CONSENT.hash}'). A registering user consents to these exact ` +
      'texts under that exact version, so: if a TEXT changed, bump CONSENT_TEXT_VERSION in ' +
      'src/personvern.ts and update BOTH values in PINNED_CONSENT. If only the version moved, ' +
      'update PINNED_CONSENT.version and this hash. Never re-pin the hash alone.',
  );
});
