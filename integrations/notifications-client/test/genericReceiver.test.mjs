// A generic Node receiver, over a real socket, with no Firebase anywhere.
//
// This is the unit's Success Criterion in executable form: the same policy and the same wire
// contract, on `node:http`. It is also the only place "413 BEFORE buffering" can be demonstrated
// literally rather than argued -- a managed Functions runtime has already buffered the body before
// the handler is entered, so there the rule survives only as handler behaviour. Here the connection
// is destroyed the moment the accumulated bytes pass the ceiling, and the assertion is that the
// remaining megabytes were never read.

import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { once } from "node:events";

import { createMemoryStores } from "../src/ledger.ts";
import { createNodeReceiver } from "../src/nodeReceiver.ts";
import { TEST_SECRET, makeEvent } from "./helpers/events.mjs";
import { NOW, signedRequest } from "./helpers/signed.mjs";

async function withReceiver(overrides, body) {
  const stores = createMemoryStores();
  const dispatched = [];
  const errors = [];
  const settled = [];
  const adapters = {
    inbox: stores.inbox,
    ledger: stores.ledger,
    suppressions: stores.suppressions,
    async recipients() {
      return ["device-1"];
    },
    async eligibility() {
      return { ok: true };
    },
    async dispatch(request) {
      dispatched.push(request);
    },
    now: () => NOW,
    ...overrides,
  };
  const handler = createNodeReceiver({
    secrets: (request) => (request.url === "/hooks/known" ? [TEST_SECRET] : []),
    adapters,
    now: () => NOW,
    maxBodyBytes: 4096,
    onError: (error) => errors.push(error),
    onOutcome: (result) => settled.push(result.outcome),
  });
  const server = http.createServer((request, response) => {
    handler(request, response).catch((error) => errors.push(error));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  try {
    return await body({ server, stores, dispatched, errors, settled, port: server.address().port });
  } finally {
    server.close();
    await once(server, "close");
  }
}

function post({ port, path = "/hooks/known", headers = {}, body = "", chunks = null }) {
  return new Promise((resolve, reject) => {
    const request = http.request(
      { host: "127.0.0.1", port, path, method: "POST", headers },
      (response) => {
        const parts = [];
        response.on("data", (chunk) => parts.push(chunk));
        response.on("end", () =>
          resolve({ status: response.statusCode, body: Buffer.concat(parts).toString("utf8") }),
        );
      },
    );
    request.on("error", (error) => resolve({ error }));
    if (chunks) {
      let written = 0;
      const writeNext = () => {
        if (written >= chunks.count) {
          request.end();
          return;
        }
        written += 1;
        request.write(chunks.chunk, (error) => {
          if (error) {
            resolve({ error, written });
            return;
          }
          setImmediate(writeNext);
        });
      };
      writeNext();
    } else {
      request.end(body);
    }
    request.setTimeout(5000, () => {
      request.destroy();
      reject(new Error("timeout"));
    });
  });
}

test("a signed event is accepted and dispatched end to end, with no Firebase", async () => {
  await withReceiver({}, async ({ port, dispatched, stores }) => {
    const request = signedRequest();
    const response = await post({
      port,
      headers: { ...request.headers, "content-length": request.body.byteLength },
      body: request.body,
    });
    assert.equal(response.status, 200);
    assert.match(response.body, /"outcome":"accepted"/);
    assert.equal(dispatched.length, 1);
    assert.equal(stores.state.inbox.get(request.event.event_id).status, "dispatched");
  });
});

test("an oversized Content-Length is refused from the header alone", async () => {
  await withReceiver({}, async ({ port, stores }) => {
    const response = await post({
      port,
      headers: { "content-length": "100000" },
      body: Buffer.alloc(10),
    });
    assert.equal(response.status, 413);
    assert.equal(stores.state.inbox.size, 0);
  });
});

test("a chunked body past the ceiling is cut off mid-stream: the rest is never read", async () => {
  // No Content-Length at all, so the header check cannot help. The receiver counts bytes as they
  // arrive and destroys the connection at the ceiling. The assertion is on `bytesRead`: a receiver
  // that buffered the whole request before deciding would have read every megabyte offered.
  const offered = 64 * 64 * 1024;
  await withReceiver({}, async ({ port, stores, server }) => {
    const sockets = [];
    server.on("request", (request) => sockets.push(request.socket));
    const result = await post({
      port,
      headers: { "transfer-encoding": "chunked" },
      chunks: { chunk: Buffer.alloc(64 * 1024, 0x41), count: 64 },
    });
    // Either the 413 arrives or the socket is torn down first; both are correct, and both mean the
    // body was refused. What must NOT happen is a 200.
    assert.notEqual(result.status, 200);
    assert.equal(sockets.length, 1);
    const read = sockets[0].bytesRead;
    assert.ok(read > 0, "the receiver read the request line and some body");
    assert.ok(
      read < offered / 4,
      `the receiver read ${read} bytes of the ${offered} offered -- it did not stop early`,
    );
    assert.equal(stores.state.inbox.size, 0);
  });
});

test("an unknown destination is 401 and writes nothing", async () => {
  await withReceiver({}, async ({ port, stores }) => {
    const request = signedRequest();
    const response = await post({
      port,
      path: "/hooks/unknown",
      headers: { ...request.headers, "content-length": request.body.byteLength },
      body: request.body,
    });
    assert.equal(response.status, 401);
    assert.equal(stores.state.inbox.size, 0);
  });
});

test("the three suppression rules hold over the socket exactly as they do in the policy suite", async () => {
  await withReceiver({}, async ({ port, dispatched }) => {
    const alertId = "sub_a:VYG:L1:2026-09-09:1";
    const send = async (event) => {
      const request = signedRequest({ event });
      return post({
        port,
        headers: { ...request.headers, "content-length": request.body.byteLength },
        body: request.body,
      });
    };

    const confirmed = makeEvent({ alert_id: alertId, revision: 12, phase: "confirmed" });
    assert.equal((await send(confirmed)).status, 200);
    // Rule 1: the same event again.
    assert.match((await send(confirmed)).body, /"outcome":"duplicate"/);
    // Rule 2: an older revision.
    await send(makeEvent({ alert_id: alertId, revision: 11, phase: "predicted" }));
    // Rule 3: a NEWER forecast for a confirmed train.
    await send(makeEvent({ alert_id: alertId, revision: 99, phase: "predicted" }));

    assert.equal(dispatched.length, 1);
    assert.equal(dispatched[0].event.phase, "confirmed");
  });
});

test("a GET is refused without reading a body", async () => {
  await withReceiver({}, async ({ port }) => {
    const response = await new Promise((resolve) => {
      const request = http.request(
        { host: "127.0.0.1", port, path: "/hooks/known", method: "GET" },
        (result) => {
          result.resume();
          result.on("end", () => resolve({ status: result.statusCode }));
        },
      );
      request.end();
    });
    assert.equal(response.status, 405);
  });
});
