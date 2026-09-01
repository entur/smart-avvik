// The feedback widget's view state as pure transitions — a fork of
// demo/avviksvarsel/src/components/alertFeedbackState.ts, with the wording moved from "varselet"
// to "prognosen" because this widget rates a forecast, not an alert.

import type { FeedbackRating, FeedbackStatus } from '../api';

export const MAX_FEEDBACK_COMMENT = 500;

export type FeedbackPhase =
  | 'idle'
  | 'submitting'
  | 'rated'
  | 'saved'
  | 'retryable'
  | 'throttled'
  | 'expired'
  | 'disabled';

export interface FeedbackViewState {
  phase: FeedbackPhase;
  rating: FeedbackRating | null;
  comment: string;
}

export const initialFeedbackState = (): FeedbackViewState => ({
  phase: 'idle',
  rating: null,
  comment: '',
});

export function selectRating(
  state: FeedbackViewState,
  rating: FeedbackRating,
): FeedbackViewState {
  return { ...state, rating, phase: 'submitting' };
}

export function editComment(state: FeedbackViewState, comment: string): FeedbackViewState {
  return { ...state, comment: comment.slice(0, MAX_FEEDBACK_COMMENT) };
}

export function submissionResult(
  state: FeedbackViewState,
  status: FeedbackStatus,
  hasQueuedChange = false,
): FeedbackViewState {
  if (status === 'saved') {
    return { ...state, phase: hasQueuedChange ? 'submitting' : state.comment ? 'saved' : 'rated' };
  }
  if (status === 'expired' || status === 'invalid_receipt') return { ...state, phase: 'expired' };
  if (status === 'disabled') return { ...state, phase: 'disabled' };
  if (status === 'throttled') return { ...state, phase: 'throttled' };
  return { ...state, phase: 'retryable' };
}

export function feedbackStatusText(phase: FeedbackPhase): string {
  switch (phase) {
    case 'submitting':
      return 'Sender tilbakemeldingen …';
    case 'rated':
      return 'Takk! Vurderingen er lagret.';
    case 'saved':
      return 'Takk! Kommentaren er lagret.';
    case 'retryable':
      return 'Tilbakemeldingen ble ikke lagret. Prøv igjen.';
    case 'throttled':
      return 'Det er mange tilbakemeldinger akkurat nå. Vent litt og prøv igjen.';
    case 'expired':
      return 'Prognosen er oppdatert. Denne tilbakemeldingen ble ikke lagret.';
    case 'disabled':
      return 'Tilbakemeldinger er midlertidig utilgjengelige.';
    default:
      return '';
  }
}
