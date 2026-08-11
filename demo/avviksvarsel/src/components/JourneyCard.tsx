import { IconButton } from '@entur/button';
import { DeleteIcon } from '@entur/icons';
import { SmallAlertBox } from '@entur/alert';
import { Paragraph, SmallText } from '@entur/typography';
import type { Alert, Journey } from '../api';
import { CANCEL_LIKELY, fmtDelay, fmtPct, fmtTime, formatDays } from '../format';

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

// One situation hitting the journey. When a written message exists it leads — it is what the phone's
// notification said, so the list should not contradict it. The detail line underneath follows the
// push word for word (varsel.py's `_deterministic_body`): whichever driver dominates, either the
// cancellation probability or the typical delay interval, and nothing else. It deliberately does NOT
// show `impact_probability`, the composite alerting score: the push quotes p_cancel, so putting the
// score here printed two different percentages for the same train in the same alert box, one line
// apart. `impact_probability` is still what the gate gets compared against, which is why the
// below-threshold note below is phrased as a threshold rather than restated as a probability.
function AlertLine({ alert }: { alert: Alert }) {
  const parts: string[] = [];
  if (alert.departure_time) parts.push(`${alert.mode ?? 'Tog'} kl ${fmtTime(alert.departure_time)}`);
  // A departure ET has already declared cancelled is stated, never scored: "91 % sjanse for
  // innstilling" for a train the journey planner no longer offers sends the traveller looking for
  // it (2026-08-05). Word for word varsel.py's `_deterministic_body`, like the two below it.
  const driver = alert.cancelled
    ? 'Innstilt'
    : alert.cancel_probability >= CANCEL_LIKELY
      ? `${fmtPct(alert.cancel_probability)} sjanse for innstilling`
      : fmtDelay(alert.delay_p50_min, alert.delay_p90_min);
  if (driver) parts.push(driver);
  const observation = alert.latest_observation ? observationText(alert.latest_observation) : null;
  return (
    <SmallAlertBox variant={alert.alertworthy ? 'negative' : 'warning'} width="fluid">
      {alert.message ? (
        <span className="av-alert-message">{alert.message}</span>
      ) : (
        alert.summary && <span className="av-alert-summary">{alert.summary}</span>
      )}
      <span className="av-alert-detail">{parts.join(' · ')}</span>
      {observation && <span className="av-alert-observed">{observation}</span>}
    </SmallAlertBox>
  );
}

export default function JourneyCard({
  journey,
  onDelete,
}: {
  journey: Journey;
  onDelete: () => void;
}) {
  const fromName = journey.from_name ?? journey.from_stop;
  const toName = journey.to_name ?? journey.to_stop;
  return (
    <li className="av-card av-journey">
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
            <AlertLine
              key={`${a.situation_number}|${a.line}|${a.departure_time}`}
              alert={a}
            />
          ))}
        </div>
      )}
    </li>
  );
}
