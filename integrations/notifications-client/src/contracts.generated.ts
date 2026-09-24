// GENERATED FILE -- do not edit by hand.
//
// Produced by `npm run generate` from the vendored JSON Schemas in `contracts/`, which are
// byte-identical copies of `contracts/notifications/v1/` in the kix-avvik repository. Run
// `npm run generate:check` to prove this file is in step with them; the test suite does.

/**
 * The top-level envelope property names, in the schema's own declaration order.
 *
 * The wire envelope is EXACTLY this set and no more. `event.schema.json` is
 * `additionalProperties: false`, so a body carrying one extra field is a body a conforming
 * receiver refuses -- and the sender's store legitimately holds three fields it must not put on
 * the wire (`client_id` and `destination_id`, its own routing; `expires_at`, a retention
 * decision that is not validity). The sender projects onto an allow-list for that reason;
 * `parseEvent` in `contracts.ts` refuses the same set from this side, so a projection that
 * regressed to a deny-list fails here rather than reaching a client's phone.
 */
export const EVENT_WIRE_FIELDS = [
  "schema_version",
  "event_id",
  "type",
  "phase",
  "alert_id",
  "revision",
  "occurred_at",
  "valid_until",
  "subscription_id",
  "subscription_version",
  "external_ref",
  "test",
  "operator",
  "line",
  "journey_ref",
  "call_date",
  "stop_place",
  "stop_order",
  "situations",
  "payload",
] as const;

export type EventWireField = (typeof EVENT_WIRE_FIELDS)[number];

export const EVENT_REQUIRED_FIELDS = [
  "schema_version",
  "event_id",
  "type",
  "occurred_at",
  "valid_until",
  "subscription_id",
  "subscription_version",
  "alert_id",
  "revision",
  "phase",
  "test",
  "operator",
  "line",
  "journey_ref",
  "call_date",
  "stop_place",
  "stop_order",
  "payload",
] as const;

/**
 * Webhook notification event. One revision of one alert about one train for one subscription.
 * Delivery is at-least-once with no ordering guarantee, so a receiver MUST apply three suppression
 * rules before it dispatches anything natively: drop a duplicate event_id; drop a revision not
 * strictly newer than the highest applied for that alert_id; and never dispatch a `predicted`
 * revision for a train already `confirmed`, whatever its revision. Duplicate suppression and
 * revision ordering within our stream are supplied as a client policy, not left as homework.
 */
export interface NotificationEvent {
  /**
   * Refuse a shape your receiver does not know rather than mis-parse it. Every client step on our
   * side fails closed the same way.
   */
  schema_version: "1";
  /**
   * Immutable, and deterministic over (alert_id, revision) so a re-run of the same decision mints
   * the same id and your inbox recognises the duplicate. It is NOT a capability: it must never
   * become a lookup key or a bearer credential.
   */
  event_id: string;
  /**
   * A new revision is minted only on a lifecycle transition -- first warning, predicted to
   * confirmed, or withdrawal. A changed probability, delay figure or wording mints NOTHING, at any
   * size: there is no materiality threshold, because minting on any changed figure would turn
   * every ingestion cycle into a fresh revision and a second notification about the same train.
   */
  type: "journey.alert.created" | "journey.alert.updated" | "journey.alert.withdrawn";
  /**
   * `predicted` is a forecast from our models. `confirmed` is a cancellation the operator's own
   * real-time feed has already declared -- a fact, not a forecast, which is why its probabilities
   * are 1.0 and its delay figures are null (every one of them meant "if it runs"). `withdrawn`
   * means the FORECAST changed, not that normal service is guaranteed.
   */
  phase: "predicted" | "confirmed" | "withdrawn";
  /**
   * The canonical per-train subject key, and the only key the supplied policy deduplicates and
   * orders on. The grain is (subscription, train): a client serves many commuters through one
   * receiver, and a per-train-only key would make the receiver's own revision rule suppress the
   * SECOND commuter's notification about the same disrupted train. The train's own fields below
   * ride along for display and for your joins; they are not the identity.
   */
  alert_id: string;
  /**
   * Strictly increasing per alert_id. Monotonicity is structural rather than a property of our
   * clock: it is seeded from a UTC instant and then clamped to max(stored + 1, seed) inside the
   * transaction, so it survives an autumn DST fall-back and an NTP step backwards. A revision
   * formatted from a local timestamp would go backwards for an hour every October and a receiver
   * ordering on it would silently discard every event for those subjects, a confirmed cancellation
   * included.
   */
  revision: number;
  /**
   * When we decided this revision, UTC.
   */
  occurred_at: string;
  /**
   * How long this event is worth acting on: the earlier of a 60-minute horizon from occurred_at
   * and the train's EXPECTED departure -- its aimed time plus the delay we predict for it, which
   * is also what decides whether the train can still be caught, so a train running 25 minutes late
   * stays valid past its aimed time. An expected departure less than two minutes ahead of
   * occurred_at when the event is minted -- already past included -- is floored to two minutes
   * ahead, so an observation made moments before the train leaves is still attempted once;
   * valid_until is never earlier than occurred_at plus two minutes. WHERE EACH TERM IS ENFORCED,
   * stated rather than implied -- those two are applied to the value you receive, while the
   * subscription's own lifetime is enforced at dispatch instead: we re-read the live subscription
   * immediately before every attempt and stop sending once it has ended. So for a subscription
   * that expires inside this horizon you may hold an event whose valid_until outlives it by up to
   * an hour. Nothing further is sent for it, and the value bounds YOUR dispatch window rather than
   * asserting the subscription still exists. Past valid_until the event is expired rather than
   * failed. The retry INTERVAL is deliberately not published as a constant: it is one attempt per
   * notification pass, and the pass runs on SIRI ingestion changes -- measured at a ~296 s period
   * on 2026-09-08, which is a measurement rather than a schedule.
   */
  valid_until: string;
  subscription_id: string;
  /**
   * The subscription version this event was authorised by. Deletion or expiry stops later claims,
   * and we recheck eligibility immediately before dispatch -- but a request already in flight may
   * complete, so your receiver rechecks the recipient mapping too.
   */
  subscription_version: number;
  /**
   * Your own opaque reference from the subscription, echoed unchanged.
   */
  external_ref?: string;
  /**
   * True for a synthetic test event. It rides INSIDE the signed body, so a receiver can refuse it
   * before native dispatch; it is not a header a proxy can strip. Only subscriptions marked
   * test-only at creation can produce one, and that marker is immutable.
   */
  test: boolean;
  /**
   * Codespace of the operator, e.g. VYG.
   */
  operator: string;
  line: string;
  /**
   * The dated service journey reference. A Vy train missing from today's timetable (only ever on
   * the public journey-alerts read, never in an event) carries the synthetic
   * `RemovedJourney:<train number>_<yyyy-mm-dd>` instead, which resolves nowhere else.
   */
  journey_ref: string;
  call_date: string;
  /**
   * The boarding stop place this alert is about -- the subscription's origin, or the ref of the
   * same physical place the feed recorded the call on.
   */
  stop_place: string;
  stop_order: number;
  /**
   * The contributing SIRI-SX situation numbers. Several situations about one disruption are
   * collapsed to ONE event per train, so this can name more than one. A synthetic `KIX:`-prefixed
   * number means there is no operator message behind the alert: either the real-time feed declared
   * the cancellation with no situation published, or the alert rests on real-time data alone.
   * `KIX:PlanRemoved:` (a Vy train missing from today's timetable) never appears: no event is
   * minted for one, and the public journey-alerts read lists it instead.
   */
  situations?: string[];
  /**
   * The facts plus the Norwegian prose. Normal receipt needs no second request to us.
   */
  payload: {
    notification: {
      /**
       * The fact about the train, e.g. «Tog L1 07:18 er innstilt». Norwegian, and identical to
       * what /varsel would have pushed for the same journey.
       */
      title: string;
      /**
       * The route on its own first line, then the wording. Norwegian. Never varies by branding:
       * the wording is what our own feedback loop records, and text that varied per variant would
       * split every wording comparison along a second axis.
       */
      body: string;
      /**
       * An optional deep link or label the client may render.
       */
      action?: { [key: string]: unknown } | null;
      [key: string]: unknown;
    };
    /**
     * The affected call, as both producers actually emit it: `stop_place`, `stop_order`,
     * `aimed_departure_time` and `departs_tomorrow`. AIMED, never expected -- the real-time
     * estimate for this call is what the probabilities and the delay figures beside it are derived
     * from, and restating it here as a second time would invite a receiver to display a figure the
     * forecast has already superseded. Boarding and alighting permissions are not carried either:
     * they gate the MATCH upstream (calling at a stop is not the same as being usable from it --
     * long-distance runs are routinely sold boarding-only over their local stretch), so a call you
     * receive is one the traveller may board and alight at, and repeating the flags would publish
     * a rule instead of its result.
     */
    call?: { [key: string]: unknown };
    /**
     * Impact and cancellation probabilities. Both are 1.0 and the delay figures are null when
     * `phase` is `confirmed`.
     */
    probabilities?: { [key: string]: unknown };
    /**
     * Delay figures, or null for a confirmed cancellation.
     */
    delay?: { [key: string]: unknown } | null;
    effect?: string | null;
    /**
     * The contributing situations, each with `situation_number`, the Norwegian `summary` and
     * `description`, their English `summary_en` and `description_en`, `en_machine_translated` and
     * `has_situation`. The English is the operator's own where they published one (Vy does on
     * every situation), the service's own for a `KIX:` situation (whose Norwegian it also wrote),
     * and otherwise a machine translation of the Norwegian, with `en_machine_translated` true --
     * present that as a translation, not as the operator's words. A translation can land a poller
     * cycle after the situation first appears, and until one does all three English fields are
     * null.
     */
    situations?: { [key: string]: unknown }[];
    /**
     * The best remaining way there, which is not necessarily a faster one: for a cancelled train
     * it legitimately arrives later than the timetable promised, so read `arrives_before_original`
     * rather than assuming a saving.
     */
    alternative_journey?: { [key: string]: unknown } | null;
    [key: string]: unknown;
  };
}

/**
 * Webhook notification subscription. A client registration: one commuter's recurring journey plus
 * a callback destination. It is /varsel's own registration shape (origin place, destination place,
 * weekdays, a daily boarding window in Europe/Oslo) so that a webhook subscription and a browser
 * registration with the same definition see the same trains and cost one match and one wording
 * between them. Authored here and rendered as OpenAPI by the notification API rather than
 * redefined there. The recipient behind a subscription is a commuter on a monthly pass, not a
 * ticket-holder for one departure, which is why the grain is a recurring window and why the daily
 * cap is per subscription.
 */
export interface Subscription {
  /**
   * Caller-supplied, which is what makes a create safely retryable: the same id with the same
   * definition returns the stored record and writes nothing, while the same id with a different
   * definition is refused rather than silently overwriting a live subscription.
   */
  subscription_id: string;
  /**
   * Read-only. Taken from the authenticated credential, never from the request body: a client may
   * manage only its own subscriptions, and accepting this field from the caller would make that a
   * request-shaped choice.
   */
  client_id?: string;
  /**
   * Which provisioned callback destination this subscription's events are delivered to. One per
   * environment per client in the pilot. Signing secrets are per destination, so this is also what
   * decides which secret an attempt is signed under.
   */
  destination_id: string;
  /**
   * Origin, a National Stop Register stop place. Canonicalized on read: refs the registry has
   * collapsed onto one physical place must land on one journey key, or the same physical trip
   * earns two matches, two wordings and two notifications.
   */
  from_stop: string;
  /**
   * Destination, a National Stop Register stop place. Must differ from from_stop.
   */
  to_stop: string;
  /**
   * Start of the daily boarding window, HH:MM in Europe/Oslo.
   */
  begin: string;
  /**
   * End of the daily boarding window, HH:MM in Europe/Oslo. Must be strictly after begin:
   * overnight windows are rejected here exactly as they are in /varsel, so a client cannot
   * register a window /varsel would not.
   */
  end: string;
  /**
   * Weekdays the window applies on, 0 = Monday.
   */
  days: number[];
  /**
   * The client's own opaque reference, echoed in every event so normal receipt needs no second
   * request to us. Opaque to us: never parsed, never logged, never used as a key.
   */
  external_ref?: string;
  /**
   * Marks the subscription as test-only. IMMUTABLE -- changing it requires delete and re-create --
   * because it rides inside the signed event body and the receiver refuses a test event before
   * native dispatch unless the adapter has opted into an isolated test recipient map. A
   * subscription that could be flipped live would turn every previously-signed test event into a
   * real notification.
   */
  test?: boolean;
  /**
   * Read-only, monotonic per subscription, bumped by every amendment and by the tombstone. An
   * amendment may be conditioned on it, and an event carries the version it was authorised by so a
   * delete or amendment racing an emission rejects the obsolete one.
   */
  version?: number;
  /**
   * Read-only. `deleted` is a tombstone written in a single transaction that stops routing; the
   * events, deliveries and subjects behind it are then removed by a retryable cleanup task with an
   * auditable completion state, and the tombstone's own route is redacted the moment that
   * completes.
   */
  status?: "active" | "deleted";
  /**
   * Whether any planned direct trip runs between the two places. `null` means the answer could not
   * be established -- an outage, a timeout -- and must never render as an all-clear. A
   * subscription no direct trip can serve carries the caveat and is NEVER refused: weekend-only
   * and seasonal routes are why a refusal would be wrong.
   */
  servable?: boolean | null;
  /**
   * How fresh the servability answer is. Missing or stale data never reads as an all-clear.
   */
  servable_checked_at?: string | null;
  /**
   * Read-only. The earlier of a 180-day maximum lifetime from creation and a 60-day inactivity
   * horizon from the last client interaction. Both are enforced on READ as well as by the store's
   * TTL, because TTL deletion runs up to ~24 hours late and a subscription that kept alerting for
   * a day past its published expiry would be a broken promise rather than a slow cleanup.
   */
  expires_at?: string;
  created_at?: string;
  updated_at?: string;
  /**
   * Current alert state: what stands right now about this subscription, read from the notification
   * service's own committed alert subjects rather than re-evaluated. Present on both GET routes;
   * `null` on the response to a create or replace, which does not read it -- so `null` means "not
   * included in this response" and never "no standing alerts". The latter is `n_active: 0` with
   * `truncated: false`.
   */
  alerts?: {
    /**
     * When this state was read.
     */
    as_of: string;
    /**
     * Standing alerts -- predicted or confirmed. A withdrawn alert is not one and is not counted.
     */
    n_active: number;
    /**
     * How many of those are confirmed cancellations rather than forecasts.
     */
    n_confirmed: number;
    /**
     * True when the read was bounded by its ceiling or could not be completed. The counts are then
     * LOWER BOUNDS: `n_active: 0` beside this means "we could not establish it", never "all
     * clear". On a list page it is set on every row whenever that page's single query was bounded,
     * because a subscription crowded out of the page would otherwise read exactly like a calm one
     * -- ask about it individually and the read is scoped to it alone.
     */
    truncated: boolean;
    /**
     * The worst few standing alerts, worst first: a confirmed cancellation ahead of a forecast,
     * then most recently changed, ties broken on `alert_id` so the order cannot flip between two
     * requests about unchanged data. Deliberately capped at the number of notifications one
     * subscription can receive in an Oslo day -- your client's daily cap, 4 by default and never
     * fewer than 4 -- because a longer list would enumerate what that cap has already suppressed;
     * the counts above carry the rest, so nothing is hidden, only unenumerated. `maxItems` is the
     * largest daily cap an operator can set, not the default, so a raised cap never makes a
     * response violate this schema.
     */
    subjects: ({
      alert_id: string;
      /**
       * A withdrawn alert does not stand and is never listed.
       */
      phase: "predicted" | "confirmed";
      revision: number;
      /**
       * The highest revision about this train that was delivered to you, or null if none has been
       * -- an alert we hold and you do not.
       */
      delivered_revision: number | null;
      operator: string;
      line: string;
      journey_ref: string;
      call_date: string;
      stop_place: string;
      stop_order: number;
      updated_at: string | null;
    })[];
  } | null;
}
