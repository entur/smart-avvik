import { IconButton } from '@entur/button';
import { DeleteIcon } from '@entur/icons';
import { SmallAlertBox } from '@entur/alert';
import { Paragraph, SmallText } from '@entur/typography';
import type { Alert, Journey } from '../api';
import { CANCEL_LIKELY, TROUBLE_DELAY_MIN, fmtDelay, fmtPct, fmtTime, formatDays } from '../format';

// One situation hitting the journey. The lead number is the alerting score the push gate reads —
// P(innstilling eller >= 10 min forsinkelse), the same quantity and wording /demo shows — followed
// by the dominant driver. A match below the operator's threshold is listed but not pushed, and says
// so, so the list explains rather than contradicts the phone's notifications.
function AlertLine({ alert }: { alert: Alert }) {
  const parts: string[] = [];
  if (alert.departure_time) parts.push(`${alert.mode ?? 'Tog'} kl ${fmtTime(alert.departure_time)}`);
  parts.push(
    `${fmtPct(alert.impact_probability ?? alert.cancel_probability)} sjanse for innstilling eller ≥ ${TROUBLE_DELAY_MIN} min forsinkelse`,
  );
  const driver =
    alert.cancel_probability >= CANCEL_LIKELY
      ? 'mest sannsynlig innstilt'
      : fmtDelay(alert.delay_p50_min, alert.delay_p90_min);
  if (driver) parts.push(driver);
  if (!alert.alertworthy) parts.push(`under varslingsterskelen (${fmtPct(alert.alert_threshold)})`);
  return (
    <SmallAlertBox variant={alert.alertworthy ? 'negative' : 'warning'} width="fluid">
      {alert.summary && (
        <span className="av-alert-summary">{alert.summary}</span>
      )}
      <span className="av-alert-detail">{parts.join(' · ')}</span>
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

      {journey.alerts.length === 0 ? (
        <SmallText className="av-muted">Ingen aktive avvik akkurat nå.</SmallText>
      ) : (
        <div className="av-alerts" aria-live="polite">
          <Paragraph as="p" margin="none" className="av-alerts-lead">
            {journey.alerts.length === 1
              ? '1 aktivt avvik kan ramme denne reisen:'
              : `${journey.alerts.length} aktive avvik kan ramme denne reisen:`}
          </Paragraph>
          {journey.alerts.map((a) => (
            <AlertLine key={a.situation_number} alert={a} />
          ))}
        </div>
      )}
    </li>
  );
}
