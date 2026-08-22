import { Paragraph, SubParagraph } from '@entur/typography';
import type { Decision, ReplayedMessage } from '../api';
import {
  OUTCOME_STATE_HELP,
  OUTCOME_STATE_LABEL,
  REPLAY_STATUS_HELP,
  REPLAY_STATUS_LABEL,
  SEND_STATE_HELP,
  SEND_STATE_LABEL,
  VERDICT_LABEL,
  VERDICT_VARIANT,
  fmtDeparture,
  fmtLooks,
  fmtMin,
  fmtOutcome,
  fmtPct,
  fmtWhen,
  lineCode,
  showsDiscarded,
} from '../format';

/** The verdict badge, or an explicit "no verdict yet / ever" marker.
 *
 *  Never falls through to a verdict-looking chip when `verdict` is null: an unresolved decision
 *  that renders like a resolved one is the single most misleading thing this page could do — and on
 *  this page nearly every card is unresolved, because the default window is today and the verdict
 *  arrives from the 04:00 UTC job the night after. */
function VerdictBadge({ decision }: { decision: Decision }) {
  if (decision.verdict) {
    return (
      <span className={`tb-badge tb-badge--${VERDICT_VARIANT[decision.verdict]}`}>
        {VERDICT_LABEL[decision.verdict]}
      </span>
    );
  }
  return (
    <span className="tb-badge tb-badge--muted" title={OUTCOME_STATE_HELP[decision.outcome_state]}>
      {OUTCOME_STATE_LABEL[decision.outcome_state]}
    </span>
  );
}

/** Why this card is silent, or that it is not.
 *
 *  Rendered even for `sendt`, and that is the point rather than clutter: the page's whole claim is
 *  "this is what we would send", so a reader must be able to tell at a glance which cards stand for
 *  a message that reached a phone and which stand for one that never did. The four silent states
 *  are labelled apart because they are not interchangeable — `samlet` means the traveller WAS
 *  notified, under another situation number. */
function SendStateBadge({ decision }: { decision: Decision }) {
  const state = decision.send_state;
  if (!state) return null;
  const sent = state === 'sendt' || state === 'sendt_uten_input';
  let help = SEND_STATE_HELP[state];
  // The count only ever qualifies the two states it can explain, and it says different things on
  // each: beside a collapse it names how many siblings won, and beside a delivered push it is the
  // open `_notified` gap — the dedup key is per situation while the collapse is per cycle, so a
  // winner that flips between cycles pushes twice for one train.
  if (decision.other_pushed_on_train) {
    help +=
      state === 'samlet'
        ? ` Andre varsler om samme tog: ${decision.other_pushed_on_train}.`
        : ` Merk: ${decision.other_pushed_on_train} annet varsel gikk også ut om dette toget.`;
  }
  return (
    <span className={`tb-tag ${sent ? 'tb-tag--sent' : 'tb-tag--quiet'}`} title={help}>
      {SEND_STATE_LABEL[state]}
    </span>
  );
}

/** A header tooltip, with the cycle count appended when there is more than one.
 *
 *  Appended rather than replacing: a `title` on a non-interactive element becomes that element's
 *  accessible NAME rather than its description, so whatever the tooltip leaves out is not merely
 *  unannounced — it is replaced. Both halves therefore have to travel together. */
function withLooks(text: string, decision: Decision): string {
  const looks = fmtLooks(decision);
  return looks ? `${text} ${looks}` : text;
}

/** Decisions about a train NO SX message named carry a synthetic situation number (predict_calls.sql
 *  writes `KIX:NoSituation:<jkey>`), and their `summary` is a placeholder this codebase wrote about
 *  itself rather than anything an operator published. The same prefix is what tilbakeblikk_summary
 *  segments the third row of rates on, so the card and the strip agree by construction. */
const isNoSituation = (decision: Decision) =>
  decision.situation_number?.startsWith('KIX:NoSituation:') ?? false;

/** The regenerated wording, or the reason there is none.
 *
 *  Three things this block does that /historikk's message block does not, and each of them is why
 *  the page exists:
 *
 *  1. The text is GENERATED, not quoted. The stored wording is deliberately absent from this API
 *     (R12): quoting the pushed text beside one regenerated from a possibly different cycle is the
 *     mismatch the 2026-08-21 fix removed from /historikk, where 41 % of cards quoted text nobody
 *     received.
 *  2. The template arm sits beneath it — the same stored input, the other writer. That comparison is
 *     what makes a regenerated wording judgeable at a glance instead of merely readable.
 *  3. Every non-`ok` status renders as itself. A badge over an empty quote cannot be told apart from
 *     "the model wrote nothing", and two of the statuses (`ungrounded`, `rejected`) are precisely
 *     what a prompt edit produces — so those two show the discarded reply, which is the text a
 *     developer iterating on the prompt actually needs to read.
 */
function ReplayBlock({ message }: { message: ReplayedMessage | null }) {
  if (message === null) {
    // Alignment failed: /messages came back, but this card's subject matched no entry at its own
    // position and more than one (or none) by content. See alignMessages — the redacted
    // journey_digest is why a content key can be ambiguous at all.
    return (
      <div className="tb-replay">
        <SubParagraph className="tb-replay-note">
          Fikk ikke koblet en formulering til dette kortet. Last siden på nytt.
        </SubParagraph>
      </div>
    );
  }

  if (message.status === 'no_input') return null;

  return (
    <div className="tb-replay">
      {message.status === 'ok' && message.message ? (
        <blockquote className="tb-message">
          {/* pre-line, exactly as on /historikk and for the same reason: the wording is written as
              several lines (summary, the train's own news, then the alternative — see
              varsel._deterministic_body's "\n".join), and without it the browser collapses the
              newlines to spaces and the parts run together into one sentence that reads as
              nonsense. */}
          <span className="tb-message-text">{message.message}</span>
        </blockquote>
      ) : (
        <div className="tb-status">
          <span
            className={`tb-status-badge tb-status-badge--${message.status}`}
            title={REPLAY_STATUS_HELP[message.status]}
          >
            {REPLAY_STATUS_LABEL[message.status]}
          </span>
          {REPLAY_STATUS_HELP[message.status] ? (
            <SubParagraph className="tb-status-help">
              {REPLAY_STATUS_HELP[message.status]}
            </SubParagraph>
          ) : null}
          {/* The reply the model actually wrote, on the two statuses where one exists and was
              thrown away. Marked as discarded rather than quoted like a message: it was never
              delivered and never stored on a decision row. */}
          {showsDiscarded(message.status) && message.discarded ? (
            <blockquote className="tb-discarded">
              <SubParagraph className="tb-discarded-label">Forkastet svar</SubParagraph>
              <span className="tb-message-text">{message.discarded}</span>
            </blockquote>
          ) : null}
        </div>
      )}

    </div>
  );
}

/** The card's second timestamp: when the message went out, or — failing that — when we last looked
 *  and stayed quiet.
 *
 *  Two cases now, where the `decided_at` axis needed three. There, `would_send_at` was the first
 *  alert-worthy cycle the WINDOW contained, which was not necessarily the subject's first
 *  opportunity, so the card had to label those apart or narrowing the picker would silently move
 *  the claimed send time later. Here it is the cycle a push actually went out on — a fact about the
 *  subject, not about the reader's selection — so «Varsel kl.» can simply be true. */
function DepartureWhen({ decision }: { decision: Decision }) {
  if (decision.would_send_at) {
    const devices = decision.n_devices ?? 0;
    return (
      <span
        className="tb-when"
        title={withLooks(
          'Varselet gikk ut på dette tidspunktet' +
            (devices > 1 ? ` — til ${devices} mottakere` : '') +
            '. Teksten under er den samme situasjonen skrevet på nytt av dagens prompt, ' +
            'ikke det som faktisk ble sendt.',
          decision,
        )}
      >
        Varsel {fmtWhen(decision.would_send_at, decision.call_date)}
      </span>
    );
  }
  if (decision.last_decided_at) {
    /* Nothing was delivered for this subject, so there is no send time to state. The last look is
       the one that stood: the closest we came to the departure and still said nothing, which is the
       fact a miss turns on. The badge beside it says WHICH kind of silence this was. */
    return (
      <span className="tb-when" title={fmtLooks(decision)}>
        Sist vurdert {fmtWhen(decision.last_decided_at, decision.call_date)}
      </span>
    );
  }
  return null;
}

/** One replayed decision.
 *
 *  `message` is three-valued on purpose: `undefined` while /messages has not answered (or could not
 *  be reached — the two endpoints are independent, and a Vertex outage must leave the card intact),
 *  a ReplayedMessage once it has, and `null` when the answer could not be aligned to this card. */
export default function DecisionCard({
  decision,
  message,
}: {
  decision: Decision;
  message?: ReplayedMessage | null;
}) {
  const alerted = decision.ever_alerted === true;
  const noSituation = isNoSituation(decision);
  return (
    <article className={`tb-card ${alerted ? 'tb-card--alerted' : ''}`}>
      <header className="tb-card-head">
        {/* One line, in timetable order: mode, line, when it leaves, and when the replayed message
            could have gone out.

            Only the departure is unlabelled — leftmost, heaviest, against the line pill exactly
            where a timetable puts it — because that is the one an unlabelled timestamp gets read
            as. The second time names itself: it is when the message actually went out. The wording
            below it is regenerated rather than quoted, which the replay block says in its own
            words — the timestamp is a fact about the push, the text is not. */}
        <div className="tb-train">
          <span className="tb-mode">{decision.mode ?? 'Tog'}</span>
          <span className="tb-line">{lineCode(decision.line)}</span>
          <span className="tb-departure">{fmtDeparture(decision)}</span>
          <DepartureWhen decision={decision} />
        </div>
        <div className="tb-card-badges">
          {/* Which side of the split this row is on. Without it the segmented rates at the top
              cannot be traced back to any particular decision, and a "100 %" chip beside a train
              looks like model skill rather than a repeated ET announcement. The no-SX tag names the
              BASIS — real-time data alone — rather than the absence of an operator message; the
              fuller explanation is one hover away, where it costs the reader nothing. */}
          {noSituation ? (
            <span
              className="tb-tag"
              title="Ingen avviksmelding var publisert for denne avgangen. Vurderingen bygger på sanntidsdata alene — togets egen forsinkelse oppstrøms og tilstanden på linjen."
            >
              Sanntidsdata alene
            </span>
          ) : null}
          {decision.et_cancelled ? (
            <span
              className="tb-tag"
              title="Avgangen var allerede meldt innstilt i sanntidsdata (SIRI-ET) da vi bestemte oss. Sannsynlighetene er 1,0 per definisjon — vi videreformidlet et faktum."
            >
              Alt meldt i ET
            </span>
          ) : null}
          <SendStateBadge decision={decision} />
          <VerdictBadge decision={decision} />
        </div>
      </header>

      {/* The operator's own words about the disruption — so a no-SX row has none, and the line is
          left out entirely rather than filled with the placeholder the SQL writes. Suppressed here
          rather than blanked in the query: `varsel_decisions` is a record and keeps what it
          recorded, including the longer placeholder that rows written before 2026-08-20 carry. */}
      {decision.summary && !noSituation ? (
        <Paragraph className="tb-situation">
          <span className="tb-dot" aria-hidden="true" />
          {decision.summary}
        </Paragraph>
      ) : null}

      {message !== undefined ? <ReplayBlock message={message} /> : null}

      <dl className="tb-facts">
        <div>
          <dt>Vurdering</dt>
          <dd>
            {alerted ? 'Varslet' : 'Varslet ikke'}
            {' · '}
            {fmtPct(decision.max_impact_probability)} mot terskel{' '}
            {fmtPct(decision.alert_threshold)}
          </dd>
        </div>
        <div>
          <dt>Varslingstid</dt>
          <dd>{alerted ? fmtMin(decision.alert_lead_min) : '–'}</dd>
        </div>
        <div>
          <dt>Faktisk</dt>
          <dd>{fmtOutcome(decision)}</dd>
        </div>
      </dl>
    </article>
  );
}
