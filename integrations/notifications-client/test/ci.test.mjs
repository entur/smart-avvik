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

test("ci.yml runs this package's suite from this package's directory", () => {
  const text = workflow();
  if (text === null) {
    console.log("skipping: no .github/workflows/ci.yml -- this is a vendored copy");
    return;
  }
  assert.match(text, /working-directory: integrations\/notifications-client/);
  assert.match(text, /npm run verify:dist/, "the declared consumption shape is built and loaded");
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
  assert.match(text, /working-directory: integrations\/firebase-notifications/);
  assert.match(text, /npm run test:emulator/);
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
