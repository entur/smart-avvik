// The wire envelope: the generated types, plus the runtime check that a body IS one.
//
// `contracts.generated.ts` is derived from `contracts/event.schema.json` and carries the property
// names and the required set. This module adds the only thing a generated type cannot: a check at
// RUNTIME, because a TypeScript type is erased before a single byte arrives from the network.

import {
  EVENT_REQUIRED_FIELDS,
  EVENT_WIRE_FIELDS,
  type NotificationEvent,
} from "./contracts.generated.ts";

export {
  EVENT_REQUIRED_FIELDS,
  EVENT_WIRE_FIELDS,
  type EventWireField,
  type NotificationEvent,
  type Subscription,
} from "./contracts.generated.ts";

/**
 * The only `schema_version` this build understands.
 *
 * Refusing an unknown shape is the published contract in both directions -- the schema's own
 * description says so, and every client step on the sending side fails closed the same way. A
 * receiver that mis-parses a version it does not know is worse than one that refuses it: the
 * refusal is visible, the mis-parse becomes a notification with a missing figure in it.
 */
export const SCHEMA_VERSION = "1";

/** The event lifecycle phase. `withdrawn` means the FORECAST changed, not that service is safe. */
export type EventPhase = NotificationEvent["phase"];

/** The event type. A new revision is minted only on a lifecycle transition. */
export type EventType = NotificationEvent["type"];

export type EnvelopeProblem =
  | "not_json"
  | "not_object"
  | "unknown_schema_version"
  | "unknown_field"
  | "missing_field"
  | "wrong_type";

export type ParseResult =
  | { ok: true; event: NotificationEvent }
  | { ok: false; problem: EnvelopeProblem; detail: string };

const PHASES: readonly string[] = ["predicted", "confirmed", "withdrawn"];
const TYPES: readonly string[] = [
  "journey.alert.created",
  "journey.alert.updated",
  "journey.alert.withdrawn",
];

/**
 * How each declared property is checked at runtime.
 *
 * Hand-written, and pinned against the generated tuples by
 * `test/wireContract.test.mjs`: a schema property with no entry here, or an entry naming a property
 * the schema does not declare, fails that test. So this table cannot silently fall behind the
 * schema, and it does not have to restate the property NAMES -- those come from the generated
 * tuple, which is what the allow-list below iterates.
 */
const KINDS: Record<string, (value: unknown) => boolean> = {
  schema_version: (value) => value === SCHEMA_VERSION,
  event_id: isNonEmptyString,
  type: (value) => typeof value === "string" && TYPES.includes(value),
  phase: (value) => typeof value === "string" && PHASES.includes(value),
  alert_id: isNonEmptyString,
  revision: (value) => isInteger(value) && (value as number) >= 1,
  occurred_at: isNonEmptyString,
  valid_until: isNonEmptyString,
  subscription_id: isNonEmptyString,
  subscription_version: (value) => isInteger(value) && (value as number) >= 1,
  external_ref: (value) => typeof value === "string",
  test: (value) => typeof value === "boolean",
  operator: isNonEmptyString,
  line: (value) => typeof value === "string",
  journey_ref: isNonEmptyString,
  call_date: isNonEmptyString,
  stop_place: isNonEmptyString,
  stop_order: (value) => isInteger(value) && (value as number) >= 0,
  situations: (value) => Array.isArray(value) && value.every((item) => typeof item === "string"),
  payload: isPayload,
};

function isNonEmptyString(value: unknown): boolean {
  return typeof value === "string" && value.length > 0;
}

function isInteger(value: unknown): boolean {
  return typeof value === "number" && Number.isInteger(value);
}

function isPayload(value: unknown): boolean {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const notification = (value as { notification?: unknown }).notification;
  if (typeof notification !== "object" || notification === null) {
    return false;
  }
  const { title, body } = notification as { title?: unknown; body?: unknown };
  return typeof title === "string" && typeof body === "string";
}

/**
 * Parse the exact bytes that were signed into an event, or say why they are not one.
 *
 * Takes BYTES, not a parsed object, and that is the point rather than a convenience: a receiver that
 * re-serializes a parsed document and then verifies the result is the canonical webhook bug -- the
 * signature would cover a document nobody sent. The caller verifies the bytes and then hands the
 * same bytes here.
 *
 * The unknown-field refusal is the half of the contract that is easy to leave out. The schema is
 * `additionalProperties: false`, so an extra top-level field is a body a conforming receiver
 * refuses; the sending side projects onto an allow-list for exactly that reason and its store holds
 * three fields it must never put on the wire. Accepting them here would hide a regression on the
 * sender that a client running a stricter verifier would then be the first to discover -- in
 * production, on every event.
 */
export function parseEvent(bytes: Uint8Array | string): ParseResult {
  const text = typeof bytes === "string" ? bytes : Buffer.from(bytes).toString("utf8");
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    return { ok: false, problem: "not_json", detail: (error as Error).message };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { ok: false, problem: "not_object", detail: `body is ${describe(parsed)}` };
  }
  const body = parsed as Record<string, unknown>;
  // Version first, so a future shape is refused as a version rather than reported as a pile of
  // unknown fields -- the second reading sends a reader looking for a bug on our side.
  if (body.schema_version !== SCHEMA_VERSION) {
    return {
      ok: false,
      problem: "unknown_schema_version",
      detail: `schema_version ${JSON.stringify(body.schema_version)}`,
    };
  }
  const allowed = new Set<string>(EVENT_WIRE_FIELDS);
  for (const key of Object.keys(body)) {
    if (!allowed.has(key)) {
      return { ok: false, problem: "unknown_field", detail: key };
    }
  }
  for (const key of EVENT_REQUIRED_FIELDS) {
    if (!(key in body)) {
      return { ok: false, problem: "missing_field", detail: key };
    }
  }
  for (const [key, value] of Object.entries(body)) {
    const check = KINDS[key];
    if (check && !check(value)) {
      return { ok: false, problem: "wrong_type", detail: `${key} is ${describe(value)}` };
    }
  }
  return { ok: true, event: body as unknown as NotificationEvent };
}

/** The runtime kind table, exported so the wire-contract test can pin it against the schema. */
export const ENVELOPE_CHECKED_FIELDS = Object.keys(KINDS);

function describe(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "an array";
  return typeof value;
}
