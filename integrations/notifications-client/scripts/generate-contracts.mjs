#!/usr/bin/env node
// Generate `src/contracts.generated.ts` from the vendored JSON Schemas.
//
// The types are GENERATED rather than hand-written, and the generated file is COMMITTED, for the
// reason the OpenAPI document beside the schemas already states about itself: two definitions of one
// payload is how they drift. A hand-written `NotificationEvent` would go stale the first time a
// schema property is added on the Python side, and it would go stale SILENTLY -- nothing in a
// TypeScript file fails because a JSON file three directories away grew a field.
//
// `--check` re-generates in memory and exits non-zero when the committed file differs. That is the
// shape `demo/avviksvarsel/scripts/make_brand_css.py --check` already uses in this repo, and it is
// what makes the drift a test failure instead of a code review someone has to remember to do.
//
// The generator is deliberately narrow: it handles exactly the constructs the two authored schemas
// use -- objects with `properties`/`required`/`additionalProperties`, `type` as a string or an array
// of strings, `enum`, `const`, arrays with `items`, and sibling `$ref`s of the form
// `./event.schema.json#/properties/<name>`. It is not a general JSON Schema compiler and must not
// grow into one: a schema construct it cannot express should fail loudly here rather than be
// approximated in the emitted types.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = path.resolve(HERE, "..");
const CONTRACTS_DIR = path.join(PACKAGE_ROOT, "contracts");
const OUTPUT = path.join(PACKAGE_ROOT, "src", "contracts.generated.ts");

/** The schemas to emit, in order, each with the TypeScript name of its root type. */
const SCHEMAS = [
  { file: "event.schema.json", typeName: "NotificationEvent" },
  { file: "subscription.schema.json", typeName: "Subscription" },
];

function readSchema(file) {
  const full = path.join(CONTRACTS_DIR, file);
  if (!fs.existsSync(full)) {
    throw new Error(`missing vendored schema ${full}`);
  }
  return JSON.parse(fs.readFileSync(full, "utf8"));
}

const schemas = new Map(SCHEMAS.map(({ file }) => [file, readSchema(file)]));

/**
 * Resolve a sibling `$ref`.
 *
 * `subscription.schema.json` refers into `event.schema.json#/properties/<name>` for the train
 * identity fields, so the two documents cannot disagree about what an `alert_id` is. Resolving it
 * here rather than inlining a copy keeps that property: the emitted type for
 * `Subscription["alerts"]["subjects"][n]["revision"]` is derived from the event schema's own
 * declaration, not from a second statement of it.
 */
function resolveRef(ref) {
  const [file, pointer] = ref.split("#");
  const document = schemas.get(file.replace(/^\.\//, ""));
  if (!document) {
    throw new Error(`unresolvable $ref target: ${ref}`);
  }
  let node = document;
  for (const segment of pointer.split("/").filter(Boolean)) {
    node = node[segment.replace(/~1/g, "/").replace(/~0/g, "~")];
    if (node === undefined) {
      throw new Error(`unresolvable $ref pointer: ${ref}`);
    }
  }
  return node;
}

const PRIMITIVES = {
  string: "string",
  integer: "number",
  number: "number",
  boolean: "boolean",
  null: "null",
};

function typeExpression(node, indent) {
  if (node.$ref) {
    return typeExpression(resolveRef(node.$ref), indent);
  }
  if (node.const !== undefined) {
    return JSON.stringify(node.const);
  }
  if (node.enum) {
    return node.enum.map((value) => JSON.stringify(value)).join(" | ");
  }
  const declared = Array.isArray(node.type) ? node.type : [node.type];
  const parts = [];
  for (const kind of declared) {
    if (kind === "object") {
      parts.push(objectExpression(node, indent));
    } else if (kind === "array") {
      const item = node.items ? typeExpression(node.items, indent) : "unknown";
      // Parenthesised because a union item type would otherwise bind only its first member to the
      // array: `string | null[]` is not `(string | null)[]`.
      parts.push(/[|&]/.test(item) ? `(${item})[]` : `${item}[]`);
    } else if (kind in PRIMITIVES) {
      parts.push(PRIMITIVES[kind]);
    } else if (kind === undefined) {
      parts.push("unknown");
    } else {
      throw new Error(`unsupported schema type: ${JSON.stringify(kind)}`);
    }
  }
  return parts.join(" | ");
}

function objectExpression(node, indent) {
  if (!node.properties) {
    // An object with no declared properties is a deliberate escape hatch in these schemas --
    // `payload.call`, `payload.probabilities` -- whose inner shape is documented in prose because it
    // mirrors whatever the models produce. Emitting an index signature says exactly that, where
    // inventing property names would be a claim the schema does not make.
    return "{ [key: string]: unknown }";
  }
  const pad = " ".repeat(indent + 2);
  const required = new Set(node.required ?? []);
  const lines = [];
  for (const [name, child] of Object.entries(node.properties)) {
    if (child.description) {
      lines.push(`${pad}/**`);
      for (const line of wrap(child.description, 96 - pad.length)) {
        lines.push(`${pad} * ${line}`);
      }
      lines.push(`${pad} */`);
    }
    const optional = required.has(name) ? "" : "?";
    lines.push(`${pad}${quoteKey(name)}${optional}: ${typeExpression(child, indent + 2)};`);
  }
  if (node.additionalProperties !== false) {
    lines.push(`${pad}[key: string]: unknown;`);
  }
  return `{\n${lines.join("\n")}\n${" ".repeat(indent)}}`;
}

function quoteKey(name) {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name) ? name : JSON.stringify(name);
}

function wrap(text, width) {
  const words = text.replace(/\s+/g, " ").trim().split(" ");
  const lines = [];
  let line = "";
  for (const word of words) {
    if (line && line.length + 1 + word.length > width) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function render() {
  const out = [];
  out.push("// GENERATED FILE -- do not edit by hand.");
  out.push("//");
  out.push("// Produced by `npm run generate` from the vendored JSON Schemas in `contracts/`, which are");
  out.push("// byte-identical copies of `contracts/notifications/v1/` in the smart-avvik repository. Run");
  out.push("// `npm run generate:check` to prove this file is in step with them; the test suite does.");
  out.push("");
  const event = schemas.get("event.schema.json");
  out.push("/**");
  out.push(" * The top-level envelope property names, in the schema's own declaration order.");
  out.push(" *");
  out.push(" * The wire envelope is EXACTLY this set and no more. `event.schema.json` is");
  out.push(" * `additionalProperties: false`, so a body carrying one extra field is a body a conforming");
  out.push(" * receiver refuses -- and the sender's store legitimately holds three fields it must not put on");
  out.push(" * the wire (`client_id` and `destination_id`, its own routing; `expires_at`, a retention");
  out.push(" * decision that is not validity). The sender projects onto an allow-list for that reason;");
  out.push(" * `parseEvent` in `contracts.ts` refuses the same set from this side, so a projection that");
  out.push(" * regressed to a deny-list fails here rather than reaching a client's phone.");
  out.push(" */");
  out.push(
    `export const EVENT_WIRE_FIELDS = [\n${Object.keys(event.properties)
      .map((name) => `  ${JSON.stringify(name)},`)
      .join("\n")}\n] as const;`,
  );
  out.push("");
  out.push("export type EventWireField = (typeof EVENT_WIRE_FIELDS)[number];");
  out.push("");
  out.push(
    `export const EVENT_REQUIRED_FIELDS = [\n${(event.required ?? [])
      .map((name) => `  ${JSON.stringify(name)},`)
      .join("\n")}\n] as const;`,
  );
  out.push("");
  for (const { file, typeName } of SCHEMAS) {
    const schema = schemas.get(file);
    out.push("/**");
    for (const line of wrap(`${schema.title}. ${schema.description ?? ""}`, 96)) {
      out.push(` * ${line}`);
    }
    out.push(" */");
    out.push(`export interface ${typeName} ${objectExpression(schema, 0)}`);
    out.push("");
  }
  return `${out.join("\n").replace(/\n+$/, "")}\n`;
}

const rendered = render();
if (process.argv.includes("--check")) {
  const current = fs.existsSync(OUTPUT) ? fs.readFileSync(OUTPUT, "utf8") : "";
  if (current !== rendered) {
    console.error(
      `${path.relative(PACKAGE_ROOT, OUTPUT)} is out of step with contracts/. Run: npm run generate`,
    );
    process.exit(1);
  }
  console.log(`${path.relative(PACKAGE_ROOT, OUTPUT)} is in step with contracts/`);
} else {
  fs.writeFileSync(OUTPUT, rendered);
  console.log(`wrote ${path.relative(PACKAGE_ROOT, OUTPUT)}`);
}
