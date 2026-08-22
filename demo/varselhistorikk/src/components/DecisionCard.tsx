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

/** Whether this card draws the wording as the notification it was.
 *
 *  Read twice — to choose the framing, and by the header to decide whether it still has to carry
 *  the route. Shared rather than restated because the two must not drift: if they disagreed the
 *  route would print twice on one card or vanish from it, and both failures look like a rendering
 *  bug rather than a predicate that moved. */
const showsNotification = (decision: Decision) =>
  decision.ever_alerted === true && !!decision.message && decision.pushed_any === true;

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
          {/* The route is the one element that comes and goes, and it is a DEDUPLICATION rather
              than a demotion: a drawn push is titled with the journey, so on those cards the header
              would print it a second line above its own copy. Everywhere else — a decision we
              stayed quiet on, a wording written but never delivered — the header keeps it, because
              nothing else on the card would say where the train is going. */}
          {showsNotification(decision) ? null : (
            <span className="vh-route">
              {decision.from_name} → {decision.to_name}
            </span>
          )}
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
              Allerede meldt i SIRI-ET
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
      {(decision.summary || decision.description) && !noSituation ? (
        <Paragraph className="vh-situation">
          <span className="vh-situation-label">SIRI-SX</span>
          {/* Joined inline rather than stacked: on this card the two fields are one quotation, and
              the arm is reference material — a two-line block gives it the shape of a message,
              which is the shape the wording below it is meant to own. */}
          <span className="vh-situation-text">
            {[decision.summary, decision.description].filter(Boolean).join(' — ')}
          </span>
        </Paragraph>
      ) : null}

      {/* What the traveller was actually sent. The whole point of persisting the log: the wording
          is otherwise gone the moment the process restarts.

          Drawn as the notification it WAS, which is a stronger claim here than the same drawing
          makes on /tilbakeblikk and a true one: that page replays a wording through today's prompt
          and has to caveat that nobody received it, while this column is the text that went out —
          the 2026-08-21 view fix exists precisely to keep it the PUSHED row rather than the last
          one produced. So no caveat, and none is wanted: a notice saying "this may not be what was
          sent" would contradict the column.

          Titled with the journey, because that is what varsel._notification_payload really sends.
          The route reached this card on 2026-08-22 — see the PRIVACY note atop
          historikk_decisions.sql — and the header gives it up on these rows rather than printing it
          a second line above its own copy. */}
      {alerted && decision.message ? (
        <div className="vh-wording-block">
          {showsNotification(decision) ? (
            <div className="vh-notif-field">
              <div className="vh-notif">
                <div className="vh-notif-head">
                  {/* aria-hidden because a screen reader would otherwise announce the Entur mark as
                      the Norwegian article "en". The app name beside it carries the same identity
                      in words. */}
                  <span className="vh-notif-mark" aria-hidden="true">
                    EN
                  </span>
                  <span className="vh-notif-app">Avviksvarsel</span>
                </div>
                <p className="vh-notif-title">
                  {decision.from_name} → {decision.to_name}
                </p>
                {/* The wording is written as several lines (summary, the train's own news, then the
                    alternative -- see varsel._deterministic_body's "\n".join). Without pre-line the
                    browser collapses those newlines to spaces and the three parts run together into
                    one sentence that reads as nonsense. */}
                <p className="vh-notif-body">{decision.message}</p>
              </div>
            </div>
          ) : (
            // Written, never delivered. The frame is a claim that a phone showed this, so a wording
            // that reached nobody does not get one — the same rule /tilbakeblikk applies to its
            // undelivered states, and the line below says so in words as it always did.
            <p className="vh-wording">{decision.message}</p>
          )}
          {/* OUTSIDE the frame, deliberately: who wrote the wording and how many devices got it are
              facts about the record, and a phone showed neither of them. Inside the drawing they
              would be part of what we claim the traveller saw. */}
          <SubParagraph className="vh-message-source">
            {decision.message_source === 'llm' ? 'Skrevet av modellen' : 'Standardformulering'}
            {decision.pushed_any ? '' : ' · ikke levert'}
            {decision.n_devices && decision.n_devices > 1
              ? ` · ${decision.n_devices} enheter`
              : ''}
          </SubParagraph>
        </div>
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
