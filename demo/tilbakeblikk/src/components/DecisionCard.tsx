import { Paragraph, SubParagraph } from '@entur/typography';
import type { Decision, ReplayedMessage } from '../api';
import {
  OUTCOME_STATE_HELP,
  OUTCOME_STATE_LABEL,
  REPLAY_STATUS_HELP,
  REPLAY_STATUS_LABEL,
  VERDICT_LABEL,
  VERDICT_VARIANT,
  WINDOW_SCOPE_HELP,
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

/** Marks a figure computed over the cycles INSIDE the selected window.
 *
 *  Not decoration. /historikk's card figures describe a decision's whole life; these describe the
 *  slice of it the picker above selected, and the verdict badge one line up still describes the
 *  whole. Unmarked they read as /historikk's, and «Varslet ikke» beside «Traff» then reads as a
 *  bug in the labelling job rather than as the window doing its job. */
function WindowScope() {
  return (
    <span className="tb-scope" title={WINDOW_SCOPE_HELP}>
      vindu
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

  const stalePrompt =
    message.prompt_version !== null && message.prompt_version !== message.current_prompt_version;

  return (
    <div className="tb-replay">
      {message.status === 'ok' && message.message ? (
        <blockquote className="tb-message">
          {/* pre-line, exactly as on /historikk and for the same reason: the wording is written as
              several lines (summary, the train's own news, then the alternative — see
              varsel._deterministic_body's "\n".join), and without it the browser collapses the
              newlines to spaces and the parts run together into one sentence that reads as
              nonsense. It applies to BOTH arms below — the template is joined the same way. */}
          <span className="tb-message-text">{message.message}</span>
          <SubParagraph className="tb-message-source">
            Skrevet nå av {message.provider ?? 'modellen'}
            {message.model ? ` · ${message.model}` : ''}
          </SubParagraph>
        </blockquote>
      ) : (
        <div className="tb-status">
          <span
            className={`tb-status-badge tb-status-badge--${message.status}`}
            title={REPLAY_STATUS_HELP[message.status]}
          >
            {REPLAY_STATUS_LABEL[message.status]}
          </span>
          <SubParagraph className="tb-status-help">
            {REPLAY_STATUS_HELP[message.status]}
          </SubParagraph>
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

      {/* The other writer, on the same input. Present whenever an input was replayable — including
          when generation was bounded out, where it is the only text on the card. */}
      {message.deterministic_message ? (
        <blockquote className="tb-message tb-message--template">
          <span className="tb-message-text">{message.deterministic_message}</span>
          <SubParagraph className="tb-message-source">
            Standardformulering · samme grunnlag, andre skribent
          </SubParagraph>
        </blockquote>
      ) : null}

      {stalePrompt ? (
        <SubParagraph
          className="tb-prompt-diff"
          title={
            'Den opprinnelige teksten ble skrevet under en annen systemprompt enn den som svarte ' +
            'nå. Forskjellen over kan skyldes promptendringen — men også bare tilfeldig variasjon: ' +
            'hver formulering er ett enkelt trekk fra en ikke-deterministisk dekoder, og siden ' +
            'skiller ikke de to.'
          }
        >
          Annen prompt-versjon da: {message.prompt_version} → nå: {message.current_prompt_version}
        </SubParagraph>
      ) : null}
    </div>
  );
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
            as. The second time names itself, and on this page it has to name itself carefully: it
            is when a message COULD have been sent, from a cycle inside the chosen window. Nothing
            was sent from here. */}
        <div className="tb-train">
          <span className="tb-mode">{decision.mode ?? 'Tog'}</span>
          <span className="tb-line">{lineCode(decision.line)}</span>
          <span className="tb-departure">{fmtDeparture(decision)}</span>
          {decision.would_send_at ? (
            decision.first_in_window ? (
              <span
                className="tb-when"
                title={withLooks(
                  'Første varselverdige syklus for dette toget — tidspunktet meldingen under ' +
                    'kunne vært sendt på. Ingenting ble sendt herfra.',
                  decision,
                )}
              >
                Varsel {fmtWhen(decision.would_send_at, decision.call_date)}
              </span>
            ) : (
              /* The subject was already alerting before the window opened, so this is the earliest
                 cycle the WINDOW contains and not the first opportunity. Labelled apart rather than
                 called "Varsel": narrowing the picker would otherwise silently move the claimed
                 send time later, and the reader would have no way to see it happen. */
              <span
                className="tb-when"
                title={withLooks(
                  'Toget varslet allerede før vinduet åpnet, så dette er den tidligste syklusen ' +
                    'inne i vinduet — ikke den første muligheten. Snevrer du vinduet inn, flytter ' +
                    'tidspunktet seg.',
                  decision,
                )}
              >
                Tidligste i vinduet {fmtWhen(decision.would_send_at, decision.call_date)}
                <WindowScope />
              </span>
            )
          ) : decision.last_decided_at ? (
            /* Nothing in the window was replayable — a decision we stayed quiet on has no
               alert-worthy cycle at all. The last look is the one that stood: the closest we came
               to the departure and still said nothing, which is the fact a miss turns on. */
            <span className="tb-when" title={fmtLooks(decision)}>
              Sist vurdert {fmtWhen(decision.last_decided_at, decision.call_date)}
              <WindowScope />
            </span>
          ) : null}
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
          <dt>
            Vurdering <WindowScope />
          </dt>
          <dd>
            {alerted ? 'Varslet' : 'Varslet ikke'}
            {' · '}
            {fmtPct(decision.max_impact_probability)} mot terskel{' '}
            {fmtPct(decision.alert_threshold)}
          </dd>
        </div>
        <div>
          <dt>
            Varslingstid <WindowScope />
          </dt>
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
