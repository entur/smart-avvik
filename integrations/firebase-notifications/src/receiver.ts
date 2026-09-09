// The Firebase receiver: a bare `onRequest` handler, and the retry-enabled trigger it hands to.
//
// The division of labour is the one the Firebase lifetime documentation forces. A function that has
// returned its response cannot access the CPU; work left unawaited afterwards may not run at all, or
// may resume inside a LATER request. So the request handler persists and acknowledges, and every
// side effect happens on a trigger that Cloud Functions is willing to retry.
//
// `retry: true` on the trigger is what makes that safe, and its spelling has moved across major
// versions of `firebase-functions`: v1 said `functions.runWith({ failurePolicy: true })`, v2 says
// `retry` on the event-handler options, and the v2 option is only reachable through the
// OPTIONS-OBJECT overload of `onDocumentCreated` -- the `document: string` overload has nowhere to
// put it, and passing a string is the shape every quickstart shows. Verified against the installed
// package rather than the documentation, and asserted from
// `test/functionOptions.test.mjs` off `__endpoint.eventTrigger.retry`, because a silently
// non-retrying trigger loses exactly the events a store outage was supposed to make recoverable.

import { type Request } from "firebase-functions/v2/https";
import { onDocumentCreated } from "firebase-functions/v2/firestore";
import type { Response } from "express";

import {
  DEFAULT_MAX_BODY_BYTES,
  process as processRecord,
  receive,
  type InboxRecord,
  type ProcessAdapters,
} from "@entur/kix-notifications-client";

import { COLLECTIONS } from "./collections.ts";

export interface ReceiverOptions {
  /**
   * Every secret that may sign for the destination a request arrives on, active generation first.
   *
   * A function rather than an array so a partner serving several destinations can key on the path,
   * and so the secret is read from the FUNCTIONS RUNTIME CONFIGURATION at call time. It must never
   * be compiled into a mobile bundle: a client that holds the verification secret can forge an event
   * for any commuter the partner serves, and a secret in a shipped app cannot be rotated.
   */
  secrets(request: Request): readonly string[] | Promise<readonly string[]>;
  adapters: ProcessAdapters;
  maxBodyBytes?: number;
  now?: () => Date;
  onOutcome?(outcome: { status: number; outcome: string; detail?: string }): void;
}

/**
 * The HTTP half.
 *
 * Bare `onRequest`, not `onCall` and not a framework: the body must reach the verifier as the exact
 * bytes that were signed, and anything that parses and re-serializes on the way -- which most
 * middleware does -- breaks the signature over a document nobody sent.
 */
export function createNotificationReceiver(options: ReceiverOptions) {
  const limit = options.maxBodyBytes ?? DEFAULT_MAX_BODY_BYTES;

  return async function handleRequest(request: Request, response: Response): Promise<void> {
    if (request.method !== "POST") {
      response.status(405).json({ error: "method_not_allowed" });
      return;
    }

    // The size check reads the DECLARED length and answers before touching `rawBody`, the verifier
    // or the store. Be precise about what that does and does not buy here: the Functions runtime has
    // already buffered the request before this handler is entered, so "before buffering" cannot be
    // literally true inside a managed runtime -- the honest claim is that nothing this package does
    // touches the body, computes a MAC or writes a document for an oversized request. The generic
    // `node:http` receiver in the client package IS able to refuse before reading, and its test
    // asserts the bytes were never read; that is where the literal claim lives.
    const declared = declaredBytes(request);
    if (declared !== null && declared > limit) {
      report(options, { status: 413, outcome: "too_large" });
      response.status(413).json({ outcome: "too_large" });
      return;
    }

    const body = rawBody(request);
    if (body !== null && body.byteLength > limit) {
      report(options, { status: 413, outcome: "too_large" });
      response.status(413).json({ outcome: "too_large" });
      return;
    }

    const result = await receive(
      {
        secrets: await options.secrets(request),
        inbox: options.adapters.inbox,
        maxBodyBytes: limit,
        now: options.now,
      },
      { headers: request.headers, body, declaredBytes: declared },
    );
    report(options, result);
    response.status(result.status).json({
      outcome: result.outcome,
      ...(result.detail ? { detail: result.detail } : {}),
    });
    // Nothing after the response. Not a dispatch, not a fire-and-forget promise, not a `void
    // somethingAsync()`. The inbox write above is what carries the work forward, through the trigger
    // below.
  };
}

/**
 * The trigger half: a retry-enabled `onDocumentCreated` over the inbox.
 *
 * `retry: true` means a THROWN error is redelivered for up to 24 hours. Which failures throw is
 * therefore a design decision rather than an implementation detail, and it is made in the client
 * package's `process()`: everything it decides for itself returns normally, and only an unclassified
 * fault -- a store outage being the archetype -- propagates. Its own staleness cutoff is what stops a
 * permanent bug consuming that whole window.
 */
export function createInboxWorker(
  adapters: ProcessAdapters,
  options: { region?: string; document?: string } = {},
) {
  return onDocumentCreated(
    {
      document: options.document ?? `${COLLECTIONS.inbox}/{eventId}`,
      retry: true,
      ...(options.region ? { region: options.region } : {}),
    },
    async (event) => {
      const snapshot = event.data;
      if (!snapshot) {
        // A create trigger with no data is not a state this can act on, and throwing would ask for
        // 24 hours of retries about a document that is not there.
        return;
      }
      await processRecord(adapters, snapshot.data() as InboxRecord);
    },
  );
}


function declaredBytes(request: Request): number | null {
  const header = request.headers["content-length"];
  const text = Array.isArray(header) ? header[0] : header;
  if (text === undefined || !/^[0-9]+$/.test(text)) {
    return null;
  }
  const parsed = Number(text);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

/**
 * The exact bytes, from `rawBody`.
 *
 * The Functions framework populates `request.rawBody` alongside the parsed `request.body`, and only
 * the former is what was signed. Falling back to re-serializing `request.body` would be the
 * canonical webhook bug and is deliberately NOT done: with no `rawBody` the request is refused.
 */
function rawBody(request: Request): Uint8Array | null {
  const raw = (request as unknown as { rawBody?: unknown }).rawBody;
  if (Buffer.isBuffer(raw)) {
    return raw;
  }
  if (raw instanceof Uint8Array) {
    return raw;
  }
  return null;
}

function report(
  options: ReceiverOptions,
  result: { status: number; outcome: string; detail?: string; event?: unknown },
): void {
  // PROJECTED, not passed through, and the projection is the whole point. `receive()` returns the
  // parsed event on the accepted path -- deliberately, because the caller may need it -- and handing
  // that object to a partner's logging hook would put a traveller's origin, destination, departure
  // time and the Norwegian prose of their alert into the partner's log retention on every single
  // event. The sending side's own log records are shaped to keep exactly those out; a receiver that
  // leaked them one layer down would make that work pointless.
  //
  // An allow-list rather than `delete result.event`, for the reason the sender's wire projection is
  // one: a deny-list fails open, so the next field added to `ReceiveResult` would reach the hook
  // unreviewed.
  options.onOutcome?.({
    status: result.status,
    outcome: result.outcome,
    ...(result.detail === undefined ? {} : { detail: result.detail }),
  });
}
