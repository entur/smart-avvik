import { IconButton } from '@entur/button';
import { DeleteIcon } from '@entur/icons';
import { SmallAlertBox } from '@entur/alert';
import { Paragraph, SmallText } from '@entur/typography';
import type { Alert, Journey } from '../api';
import { fmtDelay, fmtPct, fmtTime, formatDays } from '../format';

function AlertLine({ alert }: { alert: Alert }) {
  const parts: string[] = [];
  if (alert.departure_time) parts.push(`Tog kl ${fmtTime(alert.departure_time)}`);
  parts.push(`${fmtPct(alert.cancel_probability)} sjanse for innstilling`);
  const delay = fmtDelay(alert.delay_typ_min);
  if (delay) parts.push(delay);
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
