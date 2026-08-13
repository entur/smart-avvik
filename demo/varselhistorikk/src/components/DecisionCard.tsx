import { Paragraph, SubParagraph } from '@entur/typography';
import type { Decision } from '../api';
import {
  OUTCOME_STATE_HELP,
  OUTCOME_STATE_LABEL,
  VERDICT_LABEL,
  VERDICT_VARIANT,
  fmtDate,
  fmtLooks,
  fmtMin,
  fmtOutcome,
  fmtPct,
  fmtTime,
  fmtWhen,
} from '../format';

/** The verdict badge, or an explicit "no verdict yet / ever" marker.
 *
 *  Never falls through to a verdict-looking chip when `verdict` is null: an unresolved decision
 *  that renders like a resolved one is the single most misleading thing this page could do. */
function VerdictBadge({ decision }: { decision: Decision }) {
  if (decision.verdict) {
    return (
      <span className={`vh-badge vh-badge--${VERDICT_VARIANT[decision.verdict]}`}>
        {VERDICT_LABEL[decision.verdict]}
      </span>
    );
  }
  return (
    <span
      className="vh-badge vh-badge--muted"
      title={OUTCOME_STATE_HELP[decision.outcome_state]}
    >
      {OUTCOME_STATE_LABEL[decision.outcome_state]}
    </span>
  );
}

export default function DecisionCard({ decision }: { decision: Decision }) {
  const alerted = decision.ever_alerted === true;
  return (
    <article className={`vh-card ${alerted ? 'vh-card--alerted' : ''}`}>
      <header className="vh-card-head">
        <div>
          <span className="vh-mode">{decision.mode ?? 'Tog'}</span>
          <span className="vh-line">{decision.line ?? '–'}</span>
          {/* Two times, each labelled. They were one unlabelled timestamp until 2026-08-13, and it
              was read as when the message arrived — it is the departure, and the alert had gone out
              eleven hours earlier.

              The scheduled time is what the passenger calls this train, so it stays in the header
              whatever the realized departure turns out to be; the realized side belongs under
              "Faktisk", where it cannot be mistaken for the train's identity. */}
          <div className="vh-times">
            <span className="vh-when">
              <span className="vh-time-label">Rutetid</span>
              {fmtDate(decision.call_date)} kl. {fmtTime(decision.departure_time)}
            </span>
            {alerted && decision.first_alert_at ? (
              <span className="vh-when">
                <span className="vh-time-label">Varslet</span>
                {fmtWhen(decision.first_alert_at, decision.call_date)}
              </span>
            ) : decision.last_decided_at ? (
              /* A decision we stayed quiet on has no alert time, and its header would otherwise
                 say nothing at all about when it was taken. The last look is the one that stood —
                 the closest we came to the departure and still said nothing — which is the fact a
                 miss turns on. The earlier looks are in the tooltip rather than the header: how
                 long we had been watching is context, not the decision. */
              <span className="vh-when" title={fmtLooks(decision)}>
                <span className="vh-time-label">Sist vurdert</span>
                {fmtWhen(decision.last_decided_at, decision.call_date)}
              </span>
            ) : null}
          </div>
        </div>
        <div className="vh-card-badges">
          {/* Which side of the split this row is on. Without it the segmented rates at the top
              cannot be traced back to any particular decision, and a "100 %" chip beside a train
              looks like model skill rather than a repeated ET announcement. */}
          {decision.et_cancelled ? (
            <span
              className="vh-tag"
              title="Avgangen var allerede meldt innstilt i sanntidsdata (SIRI-ET) da vi bestemte oss. Sannsynlighetene er 1,0 per definisjon — vi videreformidlet et faktum."
            >
              Alt meldt i ET
            </span>
          ) : null}
          <VerdictBadge decision={decision} />
        </div>
      </header>

      {decision.summary ? (
        <Paragraph className="vh-situation">
          <span className="vh-dot" aria-hidden="true" />
          {decision.summary}
        </Paragraph>
      ) : null}

      {/* What the traveller was actually sent. The whole point of persisting the log: the wording
          is otherwise gone the moment the process restarts. */}
      {alerted && decision.message ? (
        <blockquote className="vh-message">
          {decision.message}
          <SubParagraph className="vh-message-source">
            {decision.message_source === 'llm' ? 'Skrevet av modellen' : 'Standardformulering'}
            {decision.pushed_any ? '' : ' · ikke levert'}
            {decision.n_devices && decision.n_devices > 1
              ? ` · ${decision.n_devices} enheter`
              : ''}
          </SubParagraph>
        </blockquote>
      ) : null}

      <dl className="vh-facts">
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
