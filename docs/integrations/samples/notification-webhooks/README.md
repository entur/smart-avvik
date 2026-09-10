# Sample webhook events (dev, 2026-09-10)

Two events delivered to a test receiver in the **dev** environment, for developers writing a
consumer of the notification webhooks API (`../../notification-api.md`). Nothing here is a
credential: the signature header is safe to publish, the `whsec_` secret that produces it is handed
over out of band and is not in this repository.

## `real-event-71006de2.*`

A real `journey.alert.created` event minted by the poller for the watch Voss stasjon → Arna stasjon
(Bergensbanen F4 22:06, forecast 51–90 min late, `phase: predicted`). `.wire.json` is the exact body
that was signed and sent (1758 bytes, UTF-8, sorted keys, no whitespace), reconstructed from the
stored event through the delivery pass's own serializer (`webhook_delivery.event_body` over
`wire_event`). `.pretty.json` is the same document indented for reading. The `[dev]` prefix in the
notification title is the dev environment's label; prd sends none.

## `test-event-3ed29acb.*`

A synthetic event from `POST /test-events`, captured verbatim at the receiver: `.headers.json` is
the HTTP request as received (through a Cloudflare tunnel, hence the `cf-*` headers) and
`.wire.json` is the raw body. The three signing headers are the ones to verify:

    webhook-id         the event_id; stable across retries
    webhook-timestamp  Unix seconds; fresh per attempt
    webhook-signature  "v1,<base64 HMAC-SHA256>", one entry per live signing generation

The MAC is HMAC-SHA256 over `webhook-id.webhook-timestamp.<raw body bytes>` with the destination's
secret. Verify over the raw bytes before parsing. A test event says so inside the signed body
(`"test": true`) and carries a `KIX:Test:` journey reference; its route line is stop refs rather
than names, by design.

## What verified them

The vendored TypeScript package `integrations/notifications-client` (`receive()` /
`createNodeReceiver`) accepted and dispatched both. `contracts/notifications/v1/event.schema.json`
is the envelope schema (`additionalProperties: false` on the envelope, open sub-objects under
`payload`) and `contracts/notifications/v1/signing-vectors.json` carries clock-free signing vectors.
A `confirmed` (cancellation) sample is still to be captured from a real delivery.
