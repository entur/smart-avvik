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

## `real-pull-71006de2.*`

The public read for the same registered definition, fetched 32 minutes after the event above
(`as_of` 19:50Z against the event's 19:18Z):

    GET /notification-api/v1/journey-alerts?from_stop=NSR:StopPlace:440&to_stop=NSR:StopPlace:398
        &begin=21:00&end=23:59&days=0&days=1&days=2&days=3&days=4&days=5&days=6&wait=15

`.wire.json` is the response body as served, `.pretty.json` the same indented. Read it beside the
push sample: the train, the call, the situations and `phase` agree, while the figures are live
(`typical_min` 44 against the event's mint-time 51, `impact` 0.9925 against 0.8938) and the entry
carries `expected_departure_time`, `severity`, `wording` and `alternative` status fields the event
does not. The notification text also differs («44–90 min» with a Finse observation against
«51–90 min» with a Gol one): the notification pass re-words a train as observations move, and a
revision is minted only on a lifecycle transition, so the read is identical to what the event would
say *now*, not to the event the receiver holds from an earlier cycle. See the contract's
"Byte-identity with the event" section for the boundary.

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
