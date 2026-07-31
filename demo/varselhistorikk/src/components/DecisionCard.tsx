import { Paragraph, SubParagraph } from '@entur/typography';
import type { Decision } from '../api';
import {
  OUTCOME_STATE_HELP,
  OUTCOME_STATE_LABEL,
  VERDICT_LABEL,
  VERDICT_VARIANT,
  fmtDate,
  fmtMin,
  fmtOutcome,
  fmtPct,
  fmtTime,
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
          <span className="vh-when">
            {fmtDate(decision.call_date)} kl. {fmtTime(decision.departure_time)}
          </span>
        </div>
        <VerdictBadge decision={decision} />
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
