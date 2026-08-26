import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
  editComment,
  feedbackStatusText,
  initialFeedbackState,
  MAX_FEEDBACK_COMMENT,
  selectRating,
  submissionResult,
} from '../src/components/alertFeedbackState.ts';

test('selecting either rating submits immediately and retains it after success', () => {
  for (const rating of ['up', 'down']) {
    const submitting = selectRating(initialFeedbackState(), rating);
    assert.equal(submitting.phase, 'submitting');
    assert.equal(submitting.rating, rating);
    const rated = submissionResult(submitting, 'saved');
    assert.equal(rated.phase, 'rated');
    assert.equal(rated.rating, rating);
  }
});

test('comment editing is bounded and a saved comment keeps the rating', () => {
  let state = submissionResult(selectRating(initialFeedbackState(), 'up'), 'saved');
  state = editComment(state, 'x'.repeat(MAX_FEEDBACK_COMMENT + 20));
  assert.equal(state.comment.length, MAX_FEEDBACK_COMMENT);
  state = submissionResult({ ...state, phase: 'submitting' }, 'saved');
  assert.equal(state.phase, 'saved');
  assert.equal(state.rating, 'up');
});

test('changing rating keeps the same response state and queues a newer save', () => {
  const first = submissionResult(selectRating(initialFeedbackState(), 'down'), 'saved');
  const changed = selectRating(first, 'up');
  assert.equal(changed.rating, 'up');
  assert.equal(changed.phase, 'submitting');
});

test('server outcomes map to retryable and unavailable states with Norwegian status', () => {
  const sending = selectRating(initialFeedbackState(), 'down');
  for (const status of ['downstream', 'throttled', 'expired', 'disabled', 'invalid_receipt']) {
    const result = submissionResult(sending, status);
    assert.ok(feedbackStatusText(result.phase).length > 0);
    assert.equal(result.rating, 'down');
  }
  assert.equal(submissionResult(sending, 'downstream').phase, 'retryable');
  assert.equal(submissionResult(sending, 'throttled').phase, 'throttled');
  assert.equal(submissionResult(sending, 'expired').phase, 'expired');
  assert.equal(submissionResult(sending, 'invalid_receipt').phase, 'expired');
  assert.equal(submissionResult(sending, 'disabled').phase, 'disabled');
});

test('a successful earlier request leaves the view submitting when a newer change is queued', () => {
  const sending = selectRating(initialFeedbackState(), 'up');
  assert.equal(submissionResult(sending, 'saved', true).phase, 'submitting');
});

test('feedback identifiers and receipts are never stored in persistent browser storage', () => {
  const source = readFileSync(new URL('../src/components/AlertFeedback.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /localStorage|sessionStorage|document\.cookie/);
  assert.match(source, /useRef\(crypto\.randomUUID\(\)\)/);
});

test('feedback POST whitelists the strict backend contract', () => {
  const source = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8');
  assert.match(source, /const \{ context_id, response_id, event_id, revision, rating, comment, receipt \}/);
  assert.doesNotMatch(source, /jsonPost\(feedback\)/);
});
