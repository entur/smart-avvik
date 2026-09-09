// This package is only tested if CI actually enters its directory.
//
// The repository's CI runs its suites through three loops -- `disruptions/api`, `data/feedback` and
// `demo/avviksvarsel` -- and a suite in a NEW top-level directory is invisible to all of them. That
// gap has cost this repository twice already: a suite that nobody ran, in a job whose green tick
// meant lint only. So the step is asserted from inside the package it runs, exactly as the Firestore
// emulator step is asserted from inside `test_partner_store.py`. Deleting the step fails the suite
// rather than quietly halving the build.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

import { PACKAGE_ROOT, REPO_ROOT } from "./helpers/events.mjs";

const WORKFLOW = path.join(REPO_ROOT, ".github", "workflows", "ci.yml");

function workflow() {
  if (!fs.existsSync(WORKFLOW)) {
    return null;
  }
  return fs.readFileSync(WORKFLOW, "utf8");
}

/**
 * Just the named step's own text, from its `- name:` line to the next list item.
 *
 * Asserting against the WHOLE file is what this check looked like first, and it was worth nothing
 * for the line it cared about most: `npm run verify:dist` appears TWICE in ci.yml -- here and in the
 * Firebase receiver's step, which legitimately needs it too -- so removing it from this package's
 * step left the assertion matching the sibling's copy and the suite green, silently losing the
 * KTD11 declared-consumption-shape build this file exists to guard. The same bounded-slice
 * technique `disruptions/api/test_poller_refresh.py` uses on its own source inspections.
 *
 * A missing step THROWS rather than yielding an empty slice: a regex failing against `""` reports
 * "the workflow does not run verify:dist" when the real fact is "the step was renamed", and those
 * are different repairs.
 *
 * The end of the step is found by INDENTATION, not by the next `- ` list item, and the difference is
 * not academic. Scanning for the next list item ran this step's slice on through the comment block
 * introducing the next one, and ran the LAST step's slice to the end of the file -- the `docker`
 * jobs below it open no list item, so there was nothing to stop at. Both slices then covered text
 * belonging to somebody else, which is the whole defect being fixed. A YAML block ends at the first
 * non-blank line indented no further than the `-` that opened it; comments in this file sit at
 * exactly that column, which is what closes the first case.
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

test("ci.yml runs this package's suite from this package's directory", () => {
  const text = workflow();
  if (text === null) {
    console.log("skipping: no .github/workflows/ci.yml -- this is a vendored copy");
    return;
  }
  const step = stepText(text, "Run notification client tests");
  assert.match(step, /working-directory: integrations\/notifications-client/);
  assert.match(step, /npm test\b/);
  assert.match(step, /npm run verify:dist/, "the declared consumption shape is built and loaded");
});

test("ci.yml runs the Firebase receiver's suite and its emulator suite", () => {
  // Asserted from here as well as from the sibling package, because this package is the one a
  // reader reaches first and because a step deleted from the workflow should fail more than one
  // thing. The sibling's own copy is what makes the claim self-sufficient when it is vendored
  // separately.
  const text = workflow();
  if (text === null) {
    return;
  }
  const step = stepText(text, "Run Firebase notification receiver tests");
  assert.match(step, /working-directory: integrations\/firebase-notifications/);
  assert.match(step, /npm run test:emulator/);
});

test("the image never ships either integrations/ or contracts/", () => {
  // `.dockerignore` enumerates BY NAME -- there is no wildcard for "everything the API does not
  // import" -- so a new top-level directory is included in the build context until someone lists
  // it. These two are a partner's vendored package and the authored schemas; nothing in the image
  // reads either, and the build context is copied whole.
  const dockerignore = path.join(REPO_ROOT, ".dockerignore");
  if (!fs.existsSync(dockerignore)) {
    return;
  }
  const lines = fs
    .readFileSync(dockerignore, "utf8")
    .split("\n")
    .map((line) => line.split("#")[0].trim());
  assert.ok(lines.includes("integrations/"), "integrations/ is not in .dockerignore");
  assert.ok(lines.includes("contracts/"), "contracts/ is not in .dockerignore");
});

test("the pretest guard fails on an empty glob", () => {
  // Copied from `demo/avviksvarsel`, and the reason it exists is the same gap one layer down: a
  // `node --test test/*.test.mjs` over an empty directory exits 0 in some shells and reports
  // nothing, so a package whose tests were renamed or lost would report success.
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "package.json"), "utf8"));
  assert.match(manifest.scripts.pretest, /No test\/\*\.test\.mjs files found/);
  assert.match(manifest.scripts.pretest, /process\.exit\(1\)/);
  assert.equal(manifest.scripts.test, "node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON test/*.test.mjs");
});

test("the package declares no `type`, which is what makes both module shapes work", () => {
  // Not a stylistic omission. With no `type`, Node's syntax detection loads a .ts source carrying
  // `import` as ESM -- which `node --test` needs -- while treating dist/*.js as CommonJS, which the
  // published artifact needs. An explicit "commonjs" (which `npm install` will add if allowed)
  // disables that detection and every .ts import fails at runtime with "Cannot use import statement
  // outside a module"; an explicit "module" breaks the CommonJS emit instead. Both were observed.
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "package.json"), "utf8"));
  assert.equal(manifest.type, undefined);
  assert.equal(manifest.main, "./dist/index.js");
  assert.equal(manifest.types, "./dist/index.d.ts");
  assert.equal(manifest.engines.node, ">=24");
});

test("a lockfile is committed, because the CI step runs `npm ci`", () => {
  assert.ok(fs.existsSync(path.join(PACKAGE_ROOT, "package-lock.json")));
});
