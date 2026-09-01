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

// Registration-visible texts are hash-pinned (sha256 hex of REGISTRATION_TEXTS
// joined with '\n', first 8 chars). Editing any of those texts must update BOTH
// this pin AND CONSENT_TEXT_VERSION in src/personvern.ts — the same derived-version
// reasoning as disruptions/api/llm_message.py's PROMPT_VERSION.
const PINNED_REGISTRATION_TEXTS_HASH = 'b3186ab7';

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
});

test('personvernUrl preserves the brand across navigation', () => {
  assert.equal(personvernUrl('entur'), '/varsel/personvern/');
  assert.equal(personvernUrl('none'), '/varsel/personvern/?brand=none');
});

test('editing a registration-visible text requires bumping CONSENT_TEXT_VERSION and this pin', () => {
  assert.ok(Array.isArray(REGISTRATION_TEXTS) && REGISTRATION_TEXTS.length > 0);
  const digest = createHash('sha256')
    .update(REGISTRATION_TEXTS.join('\n'), 'utf8')
    .digest('hex')
    .slice(0, 8);
  assert.equal(
    digest,
    PINNED_REGISTRATION_TEXTS_HASH,
    `The registration-visible texts changed (sha256[:8] is now '${digest}', pinned '${PINNED_REGISTRATION_TEXTS_HASH}'). ` +
      'If the change is intended, update BOTH this pinned hash in test/personvern.test.mjs ' +
      'AND bump CONSENT_TEXT_VERSION in src/personvern.ts — a registering user consents to these exact texts.',
  );
});
