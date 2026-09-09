// This package is only tested if CI actually enters its directory -- and only rules-tested if the
// emulator step exists.
//
// Two separate gaps, and both have precedent in this repository. A suite in a new top-level
// directory is invisible to CI's three existing loops; and an emulator-dependent suite that is not
// wrapped in `firebase emulators:exec` fails or, worse, silently skips. The Python partner-store
// suite greps its own CI step for exactly this reason, and so does this one.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

import { ALL_COLLECTIONS } from "../src/collections.ts";
import { QUERY_SPECS } from "../src/inbox.ts";
import { PACKAGE_ROOT, REPO_ROOT } from "./helpers/adapters.mjs";

const WORKFLOW = path.join(REPO_ROOT, ".github", "workflows", "ci.yml");

function workflow() {
  return fs.existsSync(WORKFLOW) ? fs.readFileSync(WORKFLOW, "utf8") : null;
}

/**
 * Just the named step's own text, from its `- name:` line to the next list item.
 *
 * Bounded, not whole-file, and the sibling package is where that mattered first: `npm run
 * verify:dist` appears in both integrations steps, so its check could pass on this step's copy of
 * the string. No line asserted HERE has a live duplicate in ci.yml today -- `npm run test:emulator`
 * appears once and this working directory once -- so the fix is pre-emptive on this side. It is
 * still the right shape: the two steps deliberately run the same commands, and the next one added
 * is the one that makes a whole-file match meaningless.
 *
 * A missing step THROWS: a regex failing against `""` reports "CI never runs the emulator" when the
 * fact is "the step was renamed", and those are different repairs.
 *
 * The end of the step is found by INDENTATION rather than by the next `- ` list item. This step is
 * the LAST one in its job, and the `docker` jobs below it open no list item at all -- so scanning
 * for one ran the slice to the end of the file, which is barely a slice. A YAML block ends at the
 * first non-blank line indented no further than the `-` that opened it.
 */
function stepText(text, name) {
  const lines = text.split("\n");
  const start = lines.findIndex((line) => line.trimStart().startsWith(`- name: ${name}`));
  if (start === -1) {
    throw new Error(`.github/workflows/ci.yml has no step named ${JSON.stringify(name)}`);
  }
  const indentOf = (line) => line.length - line.trimStart().length;
  const opened = indentOf(lines[start]);
  const step = [lines[start]];
  for (const line of lines.slice(start + 1)) {
    if (line.trim() !== "" && indentOf(line) <= opened) {
      break;
    }
    step.push(line);
  }
  return step.join("\n");
}

test("ci.yml runs this package's plain suite AND its emulator suite", () => {
  const text = workflow();
  if (text === null) {
    console.log("skipping: no .github/workflows/ci.yml -- this is a vendored copy");
    return;
  }
  const step = stepText(text, "Run Firebase notification receiver tests");
  assert.match(step, /working-directory: integrations\/firebase-notifications/);
  assert.match(step, /npm test\b/);
  assert.match(step, /npm run test:emulator/, "the rules deny assertions never run");
  assert.match(step, /npm run verify:dist/, "the declared consumption shape is built and loaded");
});

test("every port this emulator binds differs from the repo-root config's -- all THREE of them", () => {
  // CI runs both emulator steps in one job. The separation is what keeps a step clean when the
  // previous one has not finished tearing down -- observed leaving a Firestore emulator holding its
  // port -- and it is what makes a concurrent run possible at all.
  //
  // Three ports, and two of them are invisible in the root file. The hub is not named there, so it
  // takes the CLI default 4400. The Firestore WEBSOCKET port is named in neither: the CLI allocates
  // it separately from `firestore.port`, defaults it to 9150 for every project, and only shifts it
  // by probing for a free port at start-up -- a race two simultaneous starts both win. Comparing
  // against the literals is the only way to catch a collision with a port nobody wrote down.
  const mine = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "firebase.json"), "utf8"));
  assert.equal(mine.emulators.firestore.port, 8572);
  assert.equal(mine.emulators.firestore.websocketPort, 9151);
  assert.equal(mine.emulators.hub.port, 4401);

  const rootPath = path.join(REPO_ROOT, "firebase.json");
  if (!fs.existsSync(rootPath)) {
    return;
  }
  const root = JSON.parse(fs.readFileSync(rootPath, "utf8"));
  assert.notEqual(mine.emulators.firestore.port, root.emulators.firestore.port);
  assert.notEqual(mine.emulators.hub.port, root.emulators?.hub?.port ?? 4400);
  assert.notEqual(
    mine.emulators.firestore.websocketPort,
    root.emulators?.firestore?.websocketPort ?? 9150,
  );
});

test("the emulator runs under its OWN demo project id, not the repo root's", () => {
  // The Firebase CLI keys its hub locator file on the project id and warns that two instances of
  // one project "may result in unexpected behavior" -- its own words, from `writeLocatorFile`. A
  // distinct id answers that warning instead of tolerating it. The suites read the id from
  // GCLOUD_PROJECT rather than hard-coding it, so `--project` and the suite cannot disagree; the
  // emulator runs in `--single_project_mode`, where a disagreement is a refusal, not a stray write.
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "package.json"), "utf8"));
  const mine = /--project (\S+)/.exec(manifest.scripts["test:emulator"])?.[1];
  assert.equal(mine, "demo-kixpartner");

  const workflow = fs.existsSync(WORKFLOW) ? fs.readFileSync(WORKFLOW, "utf8") : null;
  if (workflow) {
    assert.match(workflow, /--project demo-kix\b/, "the root emulator step still uses demo-kix");
    assert.notEqual(mine, "demo-kix");
  }
  for (const suite of ["store", "rules"]) {
    const text = fs.readFileSync(
      path.join(PACKAGE_ROOT, "test", "emulator", `${suite}.test.mjs`),
      "utf8",
    );
    assert.match(text, /process\.env\.GCLOUD_PROJECT/, `${suite} hard-codes a project id`);
    assert.doesNotMatch(text, /"demo-kix/, `${suite} hard-codes a project id`);
  }
});

test("the rules file names every collection the code writes", () => {
  // Also asserted in the emulator suite, and deliberately here too: this suite runs in the plain CI
  // step, so a collection added without a rule fails the build even if the emulator step is skipped.
  const rules = fs.readFileSync(path.join(PACKAGE_ROOT, "firestore.rules"), "utf8");
  for (const collection of ALL_COLLECTIONS) {
    assert.match(rules, new RegExp(`match /${collection}/`), collection);
  }
  assert.match(rules, /allow read, write: if false;/);
});

function declaredIndexes() {
  const indexes = JSON.parse(
    fs.readFileSync(path.join(PACKAGE_ROOT, "firestore.indexes.json"), "utf8"),
  );
  return indexes.indexes.map((index) => ({
    key: `${index.collectionGroup}|${index.fields
      .map((field) => `${field.fieldPath}:${field.order}`)
      .join(",")}`,
    index,
  }));
}

function specKey(spec) {
  return `${spec.collection}|${spec.fields
    .map((field) => `${field.fieldPath}:${field.order}`)
    .join(",")}`;
}

test("every query in the module has a checked-in index", () => {
  // A static check, because it is the only kind available: the Firestore emulator tracks NO
  // composite indexes, so a query missing its production index answers happily in every test in
  // this package and throws FAILED_PRECONDITION on the partner's first deployed sweep. The same
  // shape of guard the sending side uses (`test_every_query_in_the_module_has_a_checked_in_index`),
  // and it is worth something here only because the query is BUILT from the spec it is checked
  // against -- a spec that merely described the query could go stale the first time the query moved.
  const declared = new Set(declaredIndexes().map((entry) => entry.key));
  for (const spec of QUERY_SPECS) {
    if (spec.fields.length < 2) {
      continue; // one field: Firestore's automatic single-field index serves it
    }
    assert.ok(
      declared.has(specKey(spec)),
      `${spec.name} needs an index on ${specKey(spec)} in firestore.indexes.json`,
    );
  }
});

test("every checked-in index belongs to a query the module actually runs", () => {
  // The reverse direction. An index declared for a query that no longer exists is not free: it is
  // paid for on every write to that collection, forever, and nothing else in the repository would
  // ever mention it again.
  const needed = new Set(QUERY_SPECS.filter((spec) => spec.fields.length >= 2).map(specKey));
  for (const { key, index } of declaredIndexes()) {
    assert.ok(
      ALL_COLLECTIONS.includes(index.collectionGroup),
      `${index.collectionGroup} is not a collection this package writes`,
    );
    assert.ok(needed.has(key), `${key} is declared but no query in the module needs it`);
  }
});

test("every index entry has only the keys the deploy accepts", () => {
  // `firebase deploy --only firestore:indexes` validates against the real API, which the emulator
  // never sees -- the emulator does not read this file's shape at all. An extra key here (a
  // `_comment`, say) is rejected at deploy time and nowhere earlier; the repo-root index file
  // carries none for the same reason.
  for (const { index } of declaredIndexes()) {
    assert.deepEqual(
      Object.keys(index).sort(),
      ["collectionGroup", "fields", "queryScope"],
    );
    assert.equal(index.queryScope, "COLLECTION");
    assert.ok(index.fields.length > 0, "an index with no fields is not an index");
  }
});

test("the pretest guard fails on an empty glob", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "package.json"), "utf8"));
  assert.match(manifest.scripts.pretest, /No test\/\*\.test\.mjs files found/);
  assert.match(manifest.scripts.pretest, /process\.exit\(1\)/);
});

test("the package declares no `type`, and a lockfile is committed for `npm ci`", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "package.json"), "utf8"));
  assert.equal(manifest.type, undefined);
  assert.equal(manifest.main, "./dist/index.js");
  assert.equal(manifest.types, "./dist/index.d.ts");
  assert.equal(manifest.engines.node, ">=24");
  assert.ok(fs.existsSync(path.join(PACKAGE_ROOT, "package-lock.json")));
});

test("the emulator files run SERIALLY, because they share one database", () => {
  // `node --test` parallelises test FILES by default, and the emulator runs in
  // `--single_project_mode`: a distinct project id does not give a distinct database, so both files
  // write into one data space and this file's truncation races the other's writes. The symptom is a
  // test asserting one decision document finding four -- which reads as a ledger bug, not a fixture
  // bug. Reproducible in roughly one run in three before the flag.
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "package.json"), "utf8"));
  assert.match(manifest.scripts["test:emulator"], /--test-concurrency=1/);
});

test("the emulator suite is only ever pointed at a demo- project", () => {
  // A `demo-` project id is what makes the run credential-free: the SDKs skip Application Default
  // Credentials and the project can reach no real GCP service. A real project id here would make a
  // test run capable of writing to production.
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "package.json"), "utf8"));
  assert.match(manifest.scripts["test:emulator"], /--project demo-/);
  assert.match(manifest.scripts["test:emulator"], /--config firebase\.json/);
});
