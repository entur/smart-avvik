// R14's three suppression rules, and the two acceptance examples they exist for.
//
// Pure functions, no store, no clock: every one of these is a literal in and a literal out. That is
// the point of extracting the rules -- the same assertions hold for a Firestore-backed receiver, a
// generic Node one, and a client who reimplements the adapter.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  decideSuppression,
  defaultDisplay,
  isStillValid,
} from "../src/policy.ts";
import { makeEvent } from "./helpers/events.mjs";

const NOTHING_APPLIED = { duplicate: false, applied: null };

test("rule 1: a duplicate event id is dropped", () => {
  const decision = decideSuppression(makeEvent({ revision: 7 }), {
    duplicate: true,
    applied: { revision: 7, phase: "predicted" },
  });
  assert.equal(decision.action, "drop");
  assert.equal(decision.rule, "duplicate_event");
});

test("rule 2: a revision not STRICTLY newer is dropped, equal included", () => {
  const applied = { revision: 5, phase: "predicted" };
  for (const revision of [1, 4, 5]) {
    const decision = decideSuppression(makeEvent({ revision }), { duplicate: false, applied });
    assert.equal(decision.action, "drop", `revision ${revision}`);
    assert.equal(decision.rule, "stale_revision", `revision ${revision}`);
  }
  assert.equal(
    decideSuppression(makeEvent({ revision: 6 }), { duplicate: false, applied }).action,
    "dispatch",
  );
});

test("AE16: a confirmed revision arriving before the forecast it supersedes wins", () => {
  // The acceptance example verbatim: the confirmation is applied, and the older forecast that
  // arrives afterwards is dropped BEFORE native dispatch. No cancelled train is ever presented as
  // an active forecast.
  const alertId = "sub_a:VYG:L1:2026-09-09:1";
  const confirmed = makeEvent({
    alert_id: alertId,
    revision: 12,
    phase: "confirmed",
    type: "journey.alert.updated",
  });
  const first = decideSuppression(confirmed, NOTHING_APPLIED);
  assert.equal(first.action, "dispatch");
  assert.deepEqual(first.next, { revision: 12, phase: "confirmed" });

  const forecast = makeEvent({ alert_id: alertId, revision: 11, phase: "predicted" });
  const second = decideSuppression(forecast, { duplicate: false, applied: first.next });
  assert.equal(second.action, "drop");
  assert.equal(second.rule, "stale_revision");
});

test("rule 3: a `predicted` for a confirmed train is dropped WHATEVER its revision", () => {
  // The half of AE16 rule 2 cannot reach. A forecast numbered HIGHER than the confirmation passes
  // the ordering test and must still be refused: the sender never mints confirmed -> predicted, so
  // such an event is a bug or a forgery, and presenting it would tell a traveller that a train we
  // know is cancelled might merely be late.
  const applied = { revision: 5, phase: "confirmed" };
  for (const revision of [6, 999, Number.MAX_SAFE_INTEGER]) {
    const decision = decideSuppression(
      makeEvent({ revision, phase: "predicted" }),
      { duplicate: false, applied },
    );
    assert.equal(decision.action, "drop", `revision ${revision}`);
    assert.equal(decision.rule, "already_confirmed", `revision ${revision}`);
  }
});

test("rule 3 does not block a newer confirmation or a withdrawal of a confirmed train", () => {
  const applied = { revision: 5, phase: "confirmed" };
  assert.equal(
    decideSuppression(makeEvent({ revision: 6, phase: "confirmed" }), {
      duplicate: false,
      applied,
    }).action,
    "dispatch",
  );
  assert.equal(
    decideSuppression(
      makeEvent({ revision: 6, phase: "withdrawn", type: "journey.alert.withdrawn" }),
      { duplicate: false, applied },
    ).action,
    "dispatch",
  );
});

test("a forecast after a WITHDRAWAL is dispatched -- rule 3 tracks the standing phase", () => {
  // The reading that would break this: "has this alert ever been confirmed?". A cancelled train can
  // be reinstated, the sender mints `withdrawn` for it, and a fresh forecast afterwards is a real
  // lifecycle transition. Under the "ever" reading that forecast, and every one after it, would be
  // silenced for the rest of the day.
  const afterWithdrawal = decideSuppression(makeEvent({ revision: 9, phase: "predicted" }), {
    duplicate: false,
    applied: { revision: 8, phase: "withdrawn" },
  });
  assert.equal(afterWithdrawal.action, "dispatch");
});

test("AE17: two subscriptions about one train each carry their own allowance", () => {
  // `alert_id`'s grain is (subscription, train), NOT the train. A commuter's morning and evening
  // journeys are two subscriptions and each has its own four-per-day allowance, exactly as two
  // /varsel registrations would; and a client serving many commuters through one receiver must not
  // have the second commuter's notification suppressed by the first commuter's revision number.
  const morning = "sub_morning:VYG:L1:2026-09-09:1";
  const evening = "sub_evening:VYG:L1:2026-09-09:1";

  const morningState = decideSuppression(
    makeEvent({ alert_id: morning, subscription_id: "sub_morning", revision: 40 }),
    NOTHING_APPLIED,
  );
  assert.equal(morningState.action, "dispatch");

  // The evening subscription's own alert about the SAME train, at a LOWER revision. Keyed on the
  // train it would be dropped as stale; keyed on (subscription, train) it is a first alert.
  const eveningState = decideSuppression(
    makeEvent({ alert_id: evening, subscription_id: "sub_evening", revision: 3 }),
    NOTHING_APPLIED,
  );
  assert.equal(eveningState.action, "dispatch");
  assert.notEqual(morning, evening);

  // And neither one's state is the other's: applying the morning's state to the evening's event is
  // the bug this grain prevents, so assert the shape that would produce it does produce it.
  const wrongGrain = decideSuppression(
    makeEvent({ alert_id: evening, revision: 3 }),
    { duplicate: false, applied: morningState.next },
  );
  assert.equal(wrongGrain.action, "drop", "keyed on the train, the second commuter is silenced");
});

test("a confirmation defaults to silent, a forecast to alert, a withdrawal to silent", () => {
  assert.equal(defaultDisplay(makeEvent({ phase: "predicted" })), "alert");
  assert.equal(defaultDisplay(makeEvent({ phase: "confirmed" })), "silent");
  assert.equal(defaultDisplay(makeEvent({ phase: "withdrawn" })), "silent");
});

test("the display default is overridable without touching the rules", () => {
  const decision = decideSuppression(makeEvent({ phase: "confirmed" }), NOTHING_APPLIED, {
    display: () => "alert",
  });
  assert.equal(decision.action, "dispatch");
  assert.equal(decision.display, "alert");
});

test("the dispatched decision carries the state to apply, so the caller cannot derive it wrongly", () => {
  const decision = decideSuppression(makeEvent({ revision: 17, phase: "confirmed" }), NOTHING_APPLIED);
  assert.deepEqual(decision.next, { revision: 17, phase: "confirmed" });
});

test("validity is bounded by valid_until, and an unreadable timestamp is not 'valid forever'", () => {
  const event = makeEvent({ valid_until: "2026-09-09T06:00:00+00:00" });
  assert.equal(isStillValid(event, new Date("2026-09-09T05:59:59Z")), true);
  assert.equal(isStillValid(event, new Date("2026-09-09T06:00:00Z")), true);
  assert.equal(isStillValid(event, new Date("2026-09-09T06:00:01Z")), false);
  assert.equal(isStillValid(makeEvent({ valid_until: "not a date" }), new Date()), false);
});
