// A generic Node receiver: the same policy and the same wire contract, with no Firebase anywhere.
//
// Shipped rather than kept as a test fixture, for two reasons. It is the reference a partner who is
// not on Firebase integrates against -- the Firebase adapter is one deployment shape, not the
// contract -- and it is the only place the "413 before buffering" rule can be demonstrated
// LITERALLY. A managed Functions runtime has already buffered the body before a handler is entered,
// so there the rule can only be honoured in the handler's own behaviour; here the socket is
// destroyed the moment the accumulated bytes pass the ceiling, and the test that watches it is
// watching bytes that were never read.
//
// It runs `process()` inline after answering, which a serverless function must never do -- see
// `client.ts` on why. It is sound here for the reason it is unsound there: this is a long-lived
// process that keeps the CPU after the response, so the work is a normal background task rather
// than something that may resume inside a later request. The recommended shape for a busy receiver
// is still a queue; this one is deliberately small.

import type { IncomingMessage, ServerResponse } from "node:http";

import {
  DEFAULT_MAX_BODY_BYTES,
  process as processRecord,
  receive,
  type ProcessAdapters,
  type ReceiveResult,
} from "./client.ts";
import type { InboxRecord } from "./ledger.ts";

export interface NodeReceiverOptions {
  /**
   * Secrets for the destination a request arrived on.
   *
   * Takes the request so a receiver serving several destinations can key on the path. Returning an
   * empty array is how an unknown destination is expressed; `receive()` answers it 401, identically
   * to a bad signature, so the endpoint is not an oracle for which destinations exist.
   */
  secrets(request: IncomingMessage): readonly string[];
  adapters: ProcessAdapters;
  maxBodyBytes?: number;
  now?: () => Date;
  /** Called with every terminal outcome, for logging and for tests. */
  onOutcome?(result: ReceiveResult, record: InboxRecord | null): void;
  onError?(error: unknown): void;
}

/**
 * A `node:http` request listener implementing the published decision order.
 *
 * Returns a promise per request so a caller can await the whole pipeline; the HTTP response is sent
 * before the trigger half runs either way.
 */
export function createNodeReceiver(options: NodeReceiverOptions) {
  const limit = options.maxBodyBytes ?? DEFAULT_MAX_BODY_BYTES;

  return async function handle(
    request: IncomingMessage,
    response: ServerResponse,
  ): Promise<void> {
    if (request.method !== "POST") {
      answer(response, 405, { error: "method_not_allowed" });
      request.resume();
      return;
    }

    const declared = parseContentLength(request.headers["content-length"]);
    if (declared !== null && declared > limit) {
      // Answered from the header alone. The body is never read: `destroy()` after the response so
      // the peer gets the status rather than a reset connection.
      answer(response, 413, { error: "too_large" });
      request.destroy();
      options.onOutcome?.({ status: 413, outcome: "too_large" }, null);
      return;
    }

    let body: Uint8Array | null;
    try {
      body = await readBounded(request, limit);
    } catch (error) {
      options.onError?.(error);
      answer(response, 400, { error: "unreadable_body" });
      return;
    }
    if (body === null) {
      // The stream passed the ceiling mid-flight. Nothing further was read and the socket is gone.
      answer(response, 413, { error: "too_large" });
      options.onOutcome?.({ status: 413, outcome: "too_large" }, null);
      return;
    }

    const result = await receive(
      {
        secrets: options.secrets(request),
        inbox: options.adapters.inbox,
        maxBodyBytes: limit,
        now: options.now,
      },
      { headers: request.headers, body, declaredBytes: declared },
    );
    answer(response, result.status, {
      outcome: result.outcome,
      ...(result.detail ? { detail: result.detail } : {}),
    });

    if (result.outcome !== "accepted" || !result.event) {
      options.onOutcome?.(result, null);
      return;
    }
    const record = await options.adapters.inbox.get(result.event.event_id);
    options.onOutcome?.(result, record);
    if (!record) {
      return;
    }
    try {
      await processRecord(options.adapters, record);
    } catch (error) {
      // In a long-lived process the retry is the recovery sweep, not a redelivery. Swallowing it
      // here would leave the record `pending`, which is exactly what the sweep looks for.
      options.onError?.(error);
    }
  };
}

function parseContentLength(value: string | string[] | undefined): number | null {
  const text = Array.isArray(value) ? value[0] : value;
  if (text === undefined || !/^[0-9]+$/.test(text)) {
    return null;
  }
  const parsed = Number(text);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

/**
 * Read at most `limit` bytes, and destroy the connection rather than read the `limit + 1`th.
 *
 * Returns `null` when the ceiling is passed. The chunks already accumulated are dropped: keeping
 * them would mean an attacker chooses how much memory a refused request costs, which is the thing
 * the ceiling exists to bound.
 */
function readBounded(request: IncomingMessage, limit: number): Promise<Uint8Array | null> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let total = 0;
    request.on("data", (chunk: Buffer) => {
      total += chunk.length;
      if (total > limit) {
        chunks.length = 0;
        request.destroy();
        resolve(null);
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => resolve(Buffer.concat(chunks)));
    request.on("error", (error) => reject(error));
  });
}

function answer(response: ServerResponse, status: number, body: unknown): void {
  if (response.writableEnded) {
    return;
  }
  const text = JSON.stringify(body);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": String(Buffer.byteLength(text, "utf8")),
  });
  response.end(text);
}
