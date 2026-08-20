import { Paragraph, SubParagraph } from '@entur/typography';
import type { Decision } from '../api';
import {
  OUTCOME_STATE_HELP,
  OUTCOME_STATE_LABEL,
  VERDICT_LABEL,
  VERDICT_VARIANT,
  fmtDeparture,
  fmtLooks,
  fmtMin,
  fmtOutcome,
  fmtPct,
  fmtWhen,
  lineCode,
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

/** Decisions about a train NO SX message named carry a synthetic situation number (predict_calls.sql
 *  writes `KIX:NoSituation:<jkey>`), and their `summary` is a placeholder this codebase wrote about
 *  itself rather than anything an operator published. The same prefix is what `historikk_summary.sql`
 *  segments the third row of rates on, so the card and the strip agree by construction. */
const isNoSituation = (decision: Decision) =>
  decision.situation_number?.startsWith('KIX:NoSituation:') ?? false;

export default function DecisionCard({ decision }: { decision: Decision }) {
  const alerted = decision.ever_alerted === true;
  const noSituation = isNoSituation(decision);
  return (
    <article className={`vh-card ${alerted ? 'vh-card--alerted' : ''}`}>
      <header className="vh-card-head">
        {/* One line, in timetable order: mode, line, when it leaves, and when we last had an
            opinion about it.

            Both times were labelled and stacked until 2026-08-19, because before that they were one
            unlabelled timestamp and it was read as when the message arrived — it is the departure,
            and the alert had gone out eleven hours earlier. Only ONE of them is unlabelled now, and
            it is the departure: leftmost, heaviest, sitting against the line pill exactly where a
            timetable puts it, with the second time still naming itself. That is what keeps the old
            misreading from coming back with the label.

            The scheduled time is what the passenger calls this train, so it stays in the header
            whatever the realized departure turns out to be; the realized side belongs under
            "Faktisk", where it cannot be mistaken for the train's identity. */}
        <div className="vh-train">
          <span className="vh-mode">{decision.mode ?? 'Tog'}</span>
          <span className="vh-line">{lineCode(decision.line)}</span>
          <span className="vh-departure">{fmtDeparture(decision)}</span>
          {alerted && decision.first_alert_at ? (
            <span className="vh-when">
              Varsel {fmtWhen(decision.first_alert_at, decision.call_date)}
            </span>
          ) : decision.last_decided_at ? (
            /* A decision we stayed quiet on has no alert time, and its header would otherwise
               say nothing at all about when it was taken. The last look is the one that stood —
               the closest we came to the departure and still said nothing — which is the fact a
               miss turns on. The earlier looks are in the tooltip rather than the header: how
               long we had been watching is context, not the decision. */
            <span className="vh-when" title={fmtLooks(decision)}>
              Sist vurdert {fmtWhen(decision.last_decided_at, decision.call_date)}
            </span>
          ) : null}
        </div>
        <div className="vh-card-badges">
          {/* Which side of the split this row is on. Without it the segmented rates at the top
              cannot be traced back to any particular decision, and a "100 %" chip beside a train
              looks like model skill rather than a repeated ET announcement. */}
          {/* The no-SX population's tag, for the same reason the ET one exists: without it the third
              row of segmented rates traces back to no particular decision. It names the BASIS —
              real-time data alone — rather than the absence of an operator message, which is the
              same shortening the wordings took on 2026-08-20; the fuller explanation is one hover
              away, where it costs the reader nothing. */}
          {noSituation ? (
            <span
              className="vh-tag"
              title="Ingen avviksmelding var publisert for denne avgangen. Vurderingen bygger på sanntidsdata alene — togets egen forsinkelse oppstrøms og tilstanden på linjen."
            >
              Sanntidsdata alene
            </span>
          ) : null}
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

      {/* The operator's own words about the disruption — so a no-SX row has none, and the line is
          left out entirely rather than filled with the placeholder the SQL writes. Suppressed here
          rather than blanked in the query: `varsel_decisions` is a record and keeps what it
          recorded, including the longer placeholder that rows written before 2026-08-20 carry. The
          tag above is what says which population the row is in now that this line no longer does. */}
      {decision.summary && !noSituation ? (
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
