import { useEffect, useRef, useState } from 'react';
import { SecondaryButton } from '@entur/button';
import { TextArea } from '@entur/form';
import { DislikeIcon, LikeIcon } from '@entur/icons';
import { SmallText } from '@entur/typography';
import {
  submitFeedback,
  type FeedbackRating,
  type FeedbackResponse,
  type FeedbackSubmission,
} from '../api';
import {
  editComment,
  feedbackStatusText,
  initialFeedbackState,
  MAX_FEEDBACK_COMMENT,
  selectRating,
  submissionResult,
  type FeedbackViewState,
} from './alertFeedbackState';

interface Snapshot {
  rating: FeedbackRating;
  comment: string;
}

const snapshotKey = (snapshot: Snapshot) => `${snapshot.rating}\u0000${snapshot.comment}`;

export default function AlertFeedback({ contextId }: { contextId: string }) {
  const [view, setView] = useState<FeedbackViewState>(initialFeedbackState);
  const responseId = useRef(crypto.randomUUID());
  const receipt = useRef<string | null>(null);
  const acceptedRevision = useRef(0);
  const desired = useRef<Snapshot | null>(null);
  const pending = useRef<(FeedbackSubmission & { key: string }) | null>(null);
  const sending = useRef(false);
  const mounted = useRef(true);

  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );

  async function pump(): Promise<void> {
    if (sending.current) return;
    let next = pending.current;
    if (!next) {
      const wanted = desired.current;
      if (!wanted) return;
      next = {
        context_id: contextId,
        response_id: responseId.current,
        event_id: crypto.randomUUID(),
        revision: acceptedRevision.current + 1,
        rating: wanted.rating,
        comment: wanted.comment || null,
        receipt: receipt.current,
        key: snapshotKey(wanted),
      };
      pending.current = next;
    }
    sending.current = true;
    if (mounted.current) setView((state) => ({ ...state, phase: 'submitting' }));

    let result: FeedbackResponse;
    try {
      result = await submitFeedback(next);
    } catch {
      result = { status: 'downstream' };
    }
    sending.current = false;
    if (!mounted.current) return;

    if (result.status !== 'saved') {
      setView((state) => submissionResult(state, result.status));
      return;
    }

    acceptedRevision.current = Math.max(acceptedRevision.current, result.revision ?? next.revision);
    if (result.receipt) receipt.current = result.receipt;
    pending.current = null;
    const queued = desired.current !== null && snapshotKey(desired.current) !== next.key;
    setView((state) => submissionResult(state, result.status, queued));
    if (queued) void pump();
  }

  function queue(snapshot: Snapshot) {
    desired.current = snapshot;
    void pump();
  }

  function rate(rating: FeedbackRating) {
    const updated = selectRating(view, rating);
    setView(updated);
    queue({ rating, comment: updated.comment });
  }

  function updateComment(comment: string) {
    setView((state) => editComment(state, comment));
  }

  function saveComment() {
    if (!view.rating || view.comment.length > MAX_FEEDBACK_COMMENT) return;
    queue({ rating: view.rating, comment: view.comment });
  }

  function retry() {
    void pump();
  }

  const status = feedbackStatusText(view.phase);
  const retryable = view.phase === 'retryable' || view.phase === 'throttled';
  const unavailable = view.phase === 'expired' || view.phase === 'disabled';

  return (
    <div className="av-feedback">
      <SmallText as="p" className="av-feedback-question">
        Var dette varselet nyttig?
      </SmallText>
      <div className="av-feedback-ratings" role="group" aria-label="Vurder dette varselet">
        <SecondaryButton
          size="small"
          type="button"
          aria-label="Ja, varselet var nyttig"
          aria-pressed={view.rating === 'up'}
          disabled={unavailable}
          onClick={() => rate('up')}
        >
          <LikeIcon aria-hidden="true" /> Ja
        </SecondaryButton>
        <SecondaryButton
          size="small"
          type="button"
          aria-label="Nei, varselet var ikke nyttig"
          aria-pressed={view.rating === 'down'}
          disabled={unavailable}
          onClick={() => rate('down')}
        >
          <DislikeIcon aria-hidden="true" /> Nei
        </SecondaryButton>
      </div>

      {view.rating && (
        <div className="av-feedback-comment">
          <TextArea
            label="Kommentar (valgfritt)"
            value={view.comment}
            maxLength={MAX_FEEDBACK_COMMENT}
            rows={3}
            disabled={unavailable}
            onChange={(event) => updateComment(event.target.value)}
            aria-describedby={`feedback-warning-${contextId}`}
          />
          <SmallText id={`feedback-warning-${contextId}`} className="av-feedback-help">
            Ikke skriv personopplysninger. {view.comment.length}/{MAX_FEEDBACK_COMMENT} tegn.
          </SmallText>
          <SecondaryButton
            size="small"
            type="button"
            disabled={unavailable || sending.current}
            onClick={saveComment}
          >
            Lagre kommentar
          </SecondaryButton>
        </div>
      )}

      <div className="av-feedback-status" aria-live="polite" aria-atomic="true">
        {status && <SmallText>{status}</SmallText>}
        {retryable && (
          <SecondaryButton size="small" type="button" onClick={retry}>
            Prøv igjen
          </SecondaryButton>
        )}
        {view.phase === 'expired' && (
          <SecondaryButton size="small" type="button" onClick={() => location.reload()}>
            Oppdater varselet
          </SecondaryButton>
        )}
      </div>
    </div>
  );
}
