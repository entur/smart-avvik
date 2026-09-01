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
} from './feedbackState';

// «Var denne prognosen nyttig?» — the /varsel feedback pump, applied to one issued forecast.
//
// The server minted the context_id this card carries (one per issued forecast), so a rating joins
// the exact rendering without any identifier of the visitor; response_id is fresh per mount,
// exactly as in the PWA. Submissions are serialized per widget: while a request is in flight the
// latest desired (rating, comment) is remembered and sent when it returns, and a retry of an
// unchanged snapshot keeps its event_id so the server can treat it as idempotent.
//
// The half-typed comment surviving the page's 5-minute poll is React's doing rather than an
// explicit cache: IssueCard keys this component on contextId, and the server's fingerprint dedup
// keeps that id byte-stable between forecast changes — so a poll that changes nothing re-renders
// the card without remounting the widget. A forecast that really did change mints a new id, and the
// remount is then the correct behaviour: the comment was about the old number.

interface Snapshot {
  rating: FeedbackRating;
  comment: string;
}

const snapshotKey = (snapshot: Snapshot) => `${snapshot.rating}\u0000${snapshot.comment}`;

interface Props {
  contextId: string;
  /** Re-fetch the payload: an expired context is re-minted server-side under a NEW id, so the
   *  changed payload re-renders this card with a live widget (gentler than a full reload). */
  onRefresh: () => void;
}

export default function ForecastFeedback({ contextId, onRefresh }: Props) {
  const [view, setView] = useState<FeedbackViewState>(initialFeedbackState);
  const responseId = useRef(crypto.randomUUID());
  const receipt = useRef<string | null>(null);
  const acceptedRevision = useRef(0);
  const desired = useRef<Snapshot | null>(null);
  const pending = useRef<(FeedbackSubmission & { key: string }) | null>(null);
  const sending = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function pump(): Promise<void> {
    if (sending.current) return;
    const wanted = desired.current;
    // Reuse a pending submission only while it still matches what the user wants — a kept event_id
    // makes the retry idempotent server-side, a changed snapshot gets a fresh one.
    let next =
      pending.current && wanted && pending.current.key === snapshotKey(wanted)
        ? pending.current
        : null;
    if (pending.current && !next) pending.current = null;
    if (!next) {
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

  function saveComment() {
    if (!view.rating || view.comment.length > MAX_FEEDBACK_COMMENT) return;
    queue({ rating: view.rating, comment: view.comment });
  }

  const status = feedbackStatusText(view.phase);
  const retryable = view.phase === 'retryable' || view.phase === 'throttled';
  const unavailable = view.phase === 'expired' || view.phase === 'disabled';

  return (
    <div className="tm-feedback">
      <SmallText as="p" className="tm-feedback-question">
        Var denne prognosen nyttig?
      </SmallText>
      <div className="tm-feedback-ratings" role="group" aria-label="Vurder denne prognosen">
        <SecondaryButton
          size="small"
          type="button"
          aria-label="Ja, prognosen var nyttig"
          aria-pressed={view.rating === 'up'}
          disabled={unavailable}
          onClick={() => rate('up')}
        >
          <LikeIcon aria-hidden="true" /> Ja
        </SecondaryButton>
        <SecondaryButton
          size="small"
          type="button"
          aria-label="Nei, prognosen var ikke nyttig"
          aria-pressed={view.rating === 'down'}
          disabled={unavailable}
          onClick={() => rate('down')}
        >
          <DislikeIcon aria-hidden="true" /> Nei
        </SecondaryButton>
      </div>

      {view.rating && (
        <div className="tm-feedback-comment">
          <TextArea
            label="Kommentar (valgfritt)"
            value={view.comment}
            maxLength={MAX_FEEDBACK_COMMENT}
            rows={3}
            disabled={unavailable}
            onChange={(event) => setView((state) => editComment(state, event.target.value))}
            aria-describedby={`feedback-warning-${contextId}`}
          />
          <SmallText id={`feedback-warning-${contextId}`} className="tm-feedback-help">
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

      <div className="tm-feedback-status" aria-live="polite" aria-atomic="true">
        {status && <SmallText>{status}</SmallText>}
        {retryable && (
          <SecondaryButton size="small" type="button" onClick={() => void pump()}>
            Prøv igjen
          </SecondaryButton>
        )}
        {view.phase === 'expired' && (
          <SecondaryButton size="small" type="button" onClick={onRefresh}>
            Oppdater
          </SecondaryButton>
        )}
      </div>
    </div>
  );
}
