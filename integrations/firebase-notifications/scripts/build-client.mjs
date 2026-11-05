#!/usr/bin/env node
// Build the sibling client package, installing its dependencies first if they are missing.
//
// This package consumes `@entur/smart-avvik-notifications-client` through an npm `file:` link, which
// SYMLINKS the sibling directory and installs nothing inside it. So the sibling's own `tsc` and
// `@types/node` are not here, and `npm ci` in this package does not put them there: building the
// sibling needs the sibling's own `npm ci` to have run.
//
// Leaving that as an ordering convention would be the wrong kind of implicit. It works in CI, where
// the client's step runs first, and fails on a developer's laptop with `TS2688: Cannot find type
// definition file for 'node'` -- an error that names nothing about a missing install in a directory
// one level up. So the check is here, once, and it is cheap: a directory test on the happy path.

import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CLIENT = path.resolve(HERE, "..", "..", "notifications-client");

if (!existsSync(CLIENT)) {
  console.error(
    `expected the client package beside this one at ${CLIENT} -- vendor both, as siblings`,
  );
  process.exit(1);
}

function run(command, args, cwd) {
  const result = spawnSync(command, args, { cwd, stdio: "inherit", shell: process.platform === "win32" });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

if (!existsSync(path.join(CLIENT, "node_modules"))) {
  console.log("installing the client package's dependencies (its node_modules is absent)");
  run("npm", ["ci", "--no-fund", "--no-audit"], CLIENT);
}

run("npm", ["run", "build"], CLIENT);
