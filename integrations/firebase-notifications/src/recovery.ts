// The recovery sweep: work the trigger's retry window abandoned, plus a staleness cutoff of our own.
//
// Two independent ways an inbox record is left `pending` forever, and neither leaves an error
// anywhere a client would look.
//
//   1. **The trigger never fired, or gave up.** Eventarc delivery is at-least-once, not
//      exactly-once, and its retry window is finite -- the plan states 24 hours. A record written
//      during an Eventarc incident, or one whose worker exhausted the window against a store that
//      was down all night, simply sits there. Nothing retries it and nothing says so.
//   2. **The function was redeployed mid-flight**, or the worker was killed between the decision and
//      the dispatch. The decision survives; the dispatch does not.
//
// So the sweep re-runs `process()` for pending records, which is safe precisely because `process()`
// is idempotent at the decision: a record whose decision was already made replays it, and one that
// was already dispatched and settled reads as a duplicate.
//
// The staleness cutoff is what keeps the sweep from becoming the thing that never gives up. It is
// enforced INSIDE `process()` -- at entry, before any policy or dispatch -- so the sweep does not
// have to re-derive it and cannot disagree with the trigger about what "too old" means.

import {
  process as processRecord,
  type ProcessAdapters,
} from "@entur/smart-avvik-notifications-client";

// There is deliberately NO "how far back does a sweep look" constant. A pending record of any age
// is work nobody finished, and `process()` already settles one past its cutoff as `expired` on the
// first sweep that touches it -- so an ancient record leaves `pending` after one pass and is never
// re-read. A second horizon here would be a second definition of "too old", and the two would
// disagree the first time either moved.

/**
 * How recent a record may be and still be swept, in milliseconds.
 *
 * 5 minutes. A record written seconds ago is one the trigger is probably working on RIGHT NOW, and
 * sweeping it would race the trigger for no benefit: both would decide the same thing, and the
 * second would find the decision already made -- correct, but a wasted transaction on every event.
 * The sweep is the net, not the path.
 */
export const DEFAULT_SWEEP_MIN_AGE_MS = 5 * 60 * 1000;

/**
 * How many records one sweep may take.
 *
 * A sweep runs on a schedule with a deadline of its own, and an unbounded read of a backlog is how a
 * recovery mechanism becomes the outage. A bounded sweep that leaves work behind is answered by the
 * next scheduled run; an unbounded one that times out leaves ALL of it behind.
 */
export const DEFAULT_SWEEP_LIMIT = 100;

export interface SweepOptions {
  now?: () => Date;
  minAgeMs?: number;
  limit?: number;
  onRecord?(eventId: string, outcome: string): void;
  onError?(eventId: string, error: unknown): void;
}

export interface SweepResult {
  examined: number;
  dispatched: number;
  suppressed: number;
  expired: number;
  failed: number;
}

/**
 * One sweep. Never throws for a single record's failure.
 *
 * Per-record isolation, and it is the same argument the sending side makes about its own pass: one
 * unreadable document must suppress that record's work and nothing else. A sweep that propagated the
 * first failure would process the backlog in the order of its worst record.
 */
export async function runRecoverySweep(
  adapters: ProcessAdapters,
  options: SweepOptions = {},
): Promise<SweepResult> {
  // ONE clock, resolved once and used for both the query cutoff and the staleness check inside
  // `process()`. Two clocks here is a bug this repository has already paid for once, one layer up: a
  // due-gate read in one calendar and its once-per-period latch keyed in another ran the gate five
  // hours early, every day, with nothing logged as an error. Here the same shape would be a sweep
  // that SELECTS a record as old enough and then processes it as fresh -- which is precisely what
  // the first version of this function did, and what its own test caught.
  const clock = options.now ?? adapters.now ?? (() => new Date());
  const now = clock();
  const minAgeMs = options.minAgeMs ?? DEFAULT_SWEEP_MIN_AGE_MS;
  const sweptAdapters: ProcessAdapters = { ...adapters, now: clock };
  const result: SweepResult = {
    examined: 0,
    dispatched: 0,
    suppressed: 0,
    expired: 0,
    failed: 0,
  };

  const candidates = await sweptAdapters.inbox.pending({
    receivedBefore: new Date(now.getTime() - minAgeMs),
    limit: options.limit ?? DEFAULT_SWEEP_LIMIT,
  });

  for (const record of candidates) {
    result.examined += 1;
    try {
      const outcome = await processRecord(sweptAdapters, record);
      options.onRecord?.(record.eventId, outcome.outcome);
      if (outcome.outcome === "dispatched") {
        result.dispatched += 1;
      } else if (outcome.outcome === "expired") {
        result.expired += 1;
      } else {
        result.suppressed += 1;
      }
    } catch (error) {
      // A transient fault: leave the record pending and let the NEXT sweep have it. Rethrowing would
      // abandon every remaining candidate for the sake of one.
      result.failed += 1;
      options.onError?.(record.eventId, error);
    }
  }

  return result;
}
