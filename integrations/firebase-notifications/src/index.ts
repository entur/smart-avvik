// The package entry point.
//
// One test imports this file and nothing else. Under Node's type stripping a value import of a
// type-only symbol is not elided and throws at RUNTIME, where `tsc` is perfectly happy; importing
// every module through one file is what turns that into a test failure rather than a client's first
// deployment.

export {
  ALL_COLLECTIONS,
  COLLECTIONS,
  encodeDocumentId,
  type CollectionName,
} from "./collections.ts";
export { QUERY_SPECS, createFirestoreInbox, createFirestoreSuppressionLog } from "./inbox.ts";
export { createFirestoreLedger, encodeAlertId } from "./ledger.ts";
export {
  createInboxWorker,
  createNotificationReceiver,
  type ReceiverOptions,
} from "./receiver.ts";
export {
  DEFAULT_SWEEP_LIMIT,
  DEFAULT_SWEEP_MIN_AGE_MS,
  runRecoverySweep,
  type SweepOptions,
  type SweepResult,
} from "./recovery.ts";
