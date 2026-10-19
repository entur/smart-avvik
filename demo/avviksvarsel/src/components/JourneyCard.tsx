import { IconButton, SecondaryButton } from '@entur/button';
import { DeleteIcon, UndoIcon } from '@entur/icons';
import { SmallAlertBox } from '@entur/alert';
import { Paragraph, SmallText } from '@entur/typography';
import type { Alert, AlternativeJourney, AlternativeLeg, Journey } from '../api';
import {
  CANCEL_LIKELY,
  fmtDelay,
  fmtPct,
  fmtTime,
  formatDays,
  LEG_MODE_NOUN,
  lineCode,
  withDestination,
  trainLabel,
} from '../format';
import AlertFeedback from './AlertFeedback';
import DagensReise from './DagensReise';

// "Sist observert: Asker, 18 min forsinket (kl 07:42)" — what has actually happened on the line, as
// opposed to what is predicted. Shown with its time because the observation can be up to an hour
// old; without the time "sist observert" would read as "right now".
function observationText(obs: NonNullable<Alert['latest_observation']>): string | null {
  const stop = obs.at_stop_name ?? obs.at_stop;
  if (!stop) return null;
  const state = obs.cancelled
    ? 'innstilt'
    : obs.delayed_min
      ? `${obs.delayed_min} min forsinket`
      : 'i rute';
  const when = obs.observed_at ? ` (kl ${fmtTime(obs.observed_at)})` : '';
  return `Sist observert: ${stop}, ${state}${when}`;
}

// «Vurdert kl 21:12» — the card's timestamp when nothing has been observed on the line yet. A
// disruption about tomorrow's 07:18 with no time on it at all is hard to trust (design review
// 2026-09-01, finding 33). The match is re-evaluated every poller cycle, so this dates the numbers
// on the card, not the operator's message.
function decidedText(alert: Alert): string | null {
  return alert.decided_at ? `Vurdert kl ${fmtTime(alert.decided_at)}` : null;
}

// «tog RE11» — a transit leg as a departure board names it, mirroring varsel.py's
// `_first_leg_label`: lower-case noun mid-clause, and no noun at all for a mode this codebase has
// no word for (a guessed vehicle is worse than a missing one).
function legLabel(leg: AlternativeLeg): string | null {
  const noun = leg.mode ? LEG_MODE_NOUN[leg.mode] : undefined;
  const code = withDestination(leg.public_code ?? lineCode(leg.line), leg.destination);
  if (noun && code) return `${noun} ${code}`;
  return code ?? noun ?? null;
}

// The escape route as its own box (design review 2026-09-01, finding 1). The API has carried
// `alternative_journey` on every alert since 2026-07-29 and the card never read it, so the one
// piece of advice worth opening the app for lived only inside the generated sentence and vanished
// whenever the model left it out. This is the best REMAINING way there, not necessarily a faster
// one: for a cancelled train it is legitimately the next possibility, and the arrival line says
// when that lands against the plan rather than leaving the reader to subtract two clock times.
function AlternativeBox({ alt, toName }: { alt: AlternativeJourney; toName: string }) {
  const legs = alt.legs.filter((leg) => leg.mode !== 'foot');
  const first = legs[0] ? legLabel(legs[0]) : null;
  const departure = fmtTime(alt.departure_time);
  const arrival = fmtTime(alt.arrival_time);
  if (!departure && !arrival) return null;
  const heading = departure
    ? `Ta ${first ?? 'avgangen kl'} ${departure} i stedet`
    : `Ta ${first ?? 'en annen avgang'} i stedet`;
  const late =
    alt.arrives_before_original === false
      ? alt.minutes_after_original
        ? ` – ${alt.minutes_after_original} min senere enn planlagt`
        : ' – senere enn planlagt'
      : '';
  const changes = legs
    .slice(1)
    .map((leg) => {
      const label = legLabel(leg);
      if (!label) return null;
      return leg.from_name ? `${label} fra ${leg.from_name}` : label;
    })
    .filter((text): text is string => text !== null);
  return (
    <div className="av-alternative">
      <span className="av-alternative-title">{heading}</span>
      {arrival && (
        <span className="av-alternative-line">
          Ankomst {toName} {arrival}
          {late}.
        </span>
      )}
      {changes.length > 0 && (
        <span className="av-alternative-line">Bytte underveis: {changes.join(', ')}.</span>
      )}
      {alt.at_risk && <span className="av-alternative-line">Kan også bli rammet.</span>}
    </div>
  );
}

// One situation hitting the journey. When a written message exists it leads — it is what the phone's
// notification said, so the list should not contradict it. The detail line follows the push word for
// word (varsel.py's `_deterministic_body`): whichever driver dominates, either the cancellation
// probability or the typical delay interval, and nothing else. It deliberately does NOT show
// `impact_probability`, the composite alerting score: the push quotes p_cancel, so putting the score
// here printed two different percentages for the same train in the same alert box, one line apart.
// `impact_probability` is still what the gate gets compared against, which is why the
// below-threshold note is phrased as a threshold rather than restated as a probability.
function AlertLine({ alert, toName }: { alert: Alert; toName: string }) {
  const parts: string[] = [];
  if (alert.departure_time) parts.push(trainLabel(alert.mode, alert.line, alert.departure_time, alert.destination));
  // A departure ET has already declared cancelled is stated, never scored: "91 % sjanse for
  // innstilling" for a train the journey planner no longer offers sends the traveller looking for
  // it (2026-08-05). Word for word varsel.py's `_deterministic_body`, like the two below it.
  // A departure that leaves but never reaches the traveller's stop is as certain, and said
  // verbatim as notification_wording.DESTINATION_CANCELLED.
  const driver = alert.cancelled
    ? 'Innstilt'
    : alert.destination_cancelled
      ? 'Kjører ikke helt fram'
      : alert.cancel_probability >= CANCEL_LIKELY
      ? `${fmtPct(alert.cancel_probability)} sjanse for innstilling`
      : fmtDelay(alert.delay_p50_min, alert.delay_p90_min);
  if (driver) parts.push(driver);
  const observation = alert.latest_observation ? observationText(alert.latest_observation) : null;
  const stamp = observation ?? decidedText(alert);
  return (
    <div className="av-alert">
      <SmallAlertBox variant={alert.alertworthy ? 'negative' : 'warning'} width="fluid">
        {alert.message ? (
          /* The wording is written as several lines (see varsel._deterministic_body's "\n".join, and
             the model is asked for the same shape), and the card quotes the text the push carries.
             Without pre-line the browser collapses those newlines to spaces and the parts run
             together into one sentence. */
          <span className="av-alert-message">{alert.message}</span>
        ) : (
          alert.summary && <span className="av-alert-summary">{alert.summary}</span>
        )}
        {/* Only under the operator's summary. Beside a written message the line said the same thing
            twice — «Tog L1 07:18 er innstilt …» over «Tog L1 07:18 · Innstilt» (design review
            2026-09-01, finding 18): the wording names the train and its state by prompt rule. Under
            a bare summary it is still the only line that says which train. The server mirrors this
            rule in the feedback snapshot (varsel._journey_model). */}
        {!alert.message && parts.length > 0 && (
          <span className="av-alert-detail">{parts.join(' · ')}</span>
        )}
        {alert.alternative_journey && (
          <AlternativeBox alt={alert.alternative_journey} toName={toName} />
        )}
        {stamp && <span className="av-alert-observed">{stamp}</span>}
      </SmallAlertBox>
      {/* Below the box, not inside it (finding 37): red signals urgency, and a feedback widget is
          administration — inside the panel it inherited the alarm. */}
      {alert.feedback_enabled && alert.feedback_context_id && (
        <AlertFeedback key={alert.feedback_context_id} contextId={alert.feedback_context_id} />
      )}
    </div>
  );
}

export default function JourneyCard({
  journey,
  notificationSituation,
  pendingDelete,
  onDelete,
  onUndo,
}: {
  journey: Journey;
  notificationSituation: string | null;
  /** The card has been deleted by the user and the deletion is inside its undo window. */
  pendingDelete: boolean;
  onDelete: () => void;
  onUndo: () => void;
}) {
  const fromName = journey.from_name ?? journey.from_stop;
  const toName = journey.to_name ?? journey.to_stop;
  const id = `journey-${encodeURIComponent(journey.journey_id)}`;

  // Deletion is deferred behind an undo window (App.tsx): three identical bins sit close together
  // on a phone, and a delete also withdraws the consent the registration rests on, so undo is faster
  // for those who meant it and a rescue for those who did not (design review 2026-09-01, finding 7).
  if (pendingDelete) {
    return (
      <li id={id} className="av-card av-journey av-journey-deleted" role="status">
        <SmallText as="span">
          Reisen {fromName} → {toName} slettes.
        </SmallText>
        <SecondaryButton size="small" type="button" onClick={onUndo}>
          <UndoIcon aria-hidden="true" /> Angre
        </SecondaryButton>
      </li>
    );
  }

  // Opened from a notification about one of these alerts. A thin stripe and an eyebrow, in place of
  // the thick ring that competed with the red fill (finding 35) — the fill is the semantic signal.
  const fromPush =
    notificationSituation !== null &&
    journey.alerts.some((alert) => alert.situation_number === notificationSituation);

  return (
    <li
      id={id}
      className={`av-card av-journey${fromPush ? ' av-journey-from-push' : ''}`}
      tabIndex={-1}
    >
      <div className="av-journey-head">
        <div className="av-route">
          <span className="av-route-stop">{fromName}</span>
          <span className="av-route-arrow" aria-hidden="true">
            →
          </span>
          <span className="av-route-stop">{toName}</span>
        </div>
        <IconButton aria-label={`Slett reise ${fromName} til ${toName}`} onClick={onDelete}>
          <DeleteIcon />
        </IconButton>
      </div>

      <div className="av-journey-meta">
        <span className="av-chip">{formatDays(journey.days)}</span>
        <span className="av-chip">
          {journey.begin}–{journey.end}
        </span>
      </div>

      {/* The «I dag» strip, between the chips and the alerts. Absent (null, or a server that
          predates the field) the card is exactly what it was before the strip existed. */}
      {journey.today && <DagensReise today={journey.today} />}

      {/* The warning replaces "ingen aktive avvik" rather than joining it: it is the reason there are
          none, and the reassuring line on its own is what let a journey watching the wrong stop look
          healthy for days. The server only sends it when nothing is matching. */}
      {journey.warning ? (
        <SmallAlertBox variant="warning" width="fluid">
          {journey.warning}
        </SmallAlertBox>
      ) : journey.alerts.length === 0 ? (
        <SmallText className="av-muted">Ingen aktive avvik akkurat nå.</SmallText>
      ) : (
        <div className="av-alerts" aria-live="polite">
          {fromPush && (
            <SmallText as="p" className="av-alert-eyebrow">
              Fra varselet ditt
            </SmallText>
          )}
          <Paragraph as="p" margin="none" className="av-alerts-lead">
            {journey.alerts.length === 1
              ? '1 aktivt avvik kan ramme denne reisen:'
              : `${journey.alerts.length} aktive avvik kan ramme denne reisen:`}
          </Paragraph>
          {/* Keyed on the TRAIN, not on the situation. one_alert_per_train collapses several SX
              messages about one departure, but the converse is ordinary: one situation covers a
              line for hours, so two catchable departures on it are two alerts sharing a
              situation_number. Keyed on that alone they are duplicates, and React then cannot pair
              old children with new ones across the 60 s refresh — a list that shrinks (a train
              walks out of `catchable_after`) leaves the orphan line on the card. Same defect found
              on /historikk 2026-08-11. */}
          {journey.alerts.map((a) => (
            <AlertLine key={`${a.situation_number}|${a.line}|${a.departure_time}`} alert={a} toName={toName} />
          ))}
        </div>
      )}
    </li>
  );
}
