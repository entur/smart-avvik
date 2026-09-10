// The package entry point.
//
// One test imports this file and nothing else, which is not decoration: under Node's type stripping
// a value import of a type-only symbol is not elided and throws "does not provide an export named X"
// at RUNTIME, where `tsc` is perfectly happy. Importing every module through one file is what turns
// that into a test failure instead of a client's first deployment.

export {
  DEFAULT_MAX_BODY_BYTES,
  DEFAULT_PROCESSING_CUTOFF_MS,
  TerminalReceiverError,
  TransientReceiverError,
  process,
  receive,
  type DispatchRequest,
  type Eligibility,
  type ProcessAdapters,
  type ProcessOutcome,
  type ReceiveConfig,
  type ReceiveOutcome,
  type ReceiveRequest,
  type ReceiveResult,
} from "./client.ts";

export {
  ENVELOPE_CHECKED_FIELDS,
  EVENT_REQUIRED_FIELDS,
  EVENT_WIRE_FIELDS,
  SCHEMA_VERSION,
  parseEvent,
  type EnvelopeProblem,
  type EventPhase,
  type EventType,
  type EventWireField,
  type NotificationEvent,
  type ParseResult,
  type Subscription,
} from "./contracts.ts";

export {
  createMemoryStores,
  type AlertLedger,
  type InboxOutcome,
  type InboxRecord,
  type InboxStore,
  type SuppressionLog,
  type SuppressionRecord,
} from "./ledger.ts";

export { createNodeReceiver, type NodeReceiverOptions } from "./nodeReceiver.ts";

export {
  decideSuppression,
  defaultDisplay,
  isStillValid,
  type AppliedAlert,
  type Display,
  type DisplayResolver,
  type PolicyDecision,
  type PriorState,
  type SuppressionRule,
} from "./policy.ts";

export {
  HEADER_ID,
  HEADER_SIGNATURE,
  HEADER_TIMESTAMP,
  SIGNATURE_TOLERANCE_SECONDS,
  signBody,
  signatureHeader,
  verifySignature,
  type VerifyFailure,
  type VerifyOutcome,
  type VerifyRequest,
} from "./verify.ts";
