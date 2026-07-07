import type { DemoJourney, DemoSide, DemoSnapshot } from './api';
import { fmtInterval, fmtPct, fmtTime } from './format';

// One matching journey: departure (boarding stop) on the left, arrival on the right.

const VERDICT = {
  hit: { icon: '✓', label: 'Treff — varslet, og det ble trøbbel', cls: 'verdict-hit' },
  correct_reject: { icon: '✓', label: 'Riktig ro — ikke varslet, gikk fint', cls: 'verdict-ok' },
  miss: { icon: '✕', label: 'Bom — ikke varslet, men det ble trøbbel', cls: 'verdict-miss' },
  false_alarm: { icon: '!', label: 'Falsk alarm — varslet, men gikk fint', cls: 'verdict-false' },
} as const;

function lineCode(line: string | null): string {
  if (!line) return '?';
  const parts = line.split(':');
  return parts[parts.length - 1];
}

function Actual({ side }: { side: DemoSide }) {
  if (side.actual_cancelled) return <span className="chip chip-red">Innstilt</span>;
  if (side.actual_delay_min === null) {
    const future = side.scheduled_time !== null && new Date(side.scheduled_time).getTime() > Date.now();
    return <span className="chip chip-muted">{future ? 'Ikke kjørt ennå' : 'Ukjent utfall'}</span>;
  }
  if (side.actual_delay_min >= 10)
    return <span className="chip chip-red">+{side.actual_delay_min} min</span>;
  if (side.actual_delay_min >= 3)
    return <span className="chip chip-amber">+{side.actual_delay_min} min</span>;
  return <span className="chip chip-green">I rute</span>;
}

function SnapshotRow({
  label,
  snap,
  flagged,
}: {
  label: string;
  snap: DemoSnapshot | null;
  flagged: boolean;
}) {
  if (snap === null) {
    return (
      <tr className="row-unavailable">
        <td>{label}</td>
        <td colSpan={4} className="unavailable">
          {flagged ? 'meldingen var ikke kommet ennå' : '–'}
        </td>
      </tr>
    );
  }
  const verdict = snap.verdict ? VERDICT[snap.verdict] : null;
  return (
    <tr>
      <td>{label}</td>
      <td className="num">
        <span className="meter" style={{ ['--p' as string]: snap.cancel_probability }} />
        {fmtPct(snap.cancel_probability)}
      </td>
      <td className="num">{fmtInterval(snap.delay_p50_min, snap.delay_p90_min)}</td>
      <td>{snap.would_alert ? <span className="alert-badge">Varsle</span> : <span className="no-alert">–</span>}</td>
      <td>
        {verdict && (
          <span className={`verdict ${verdict.cls}`} title={verdict.label}>
            {verdict.icon}
          </span>
        )}
      </td>
    </tr>
  );
}

function Side({
  title,
  side,
  onsetTime,
  hasSituation,
  threshold,
}: {
  title: string;
  side: DemoSide;
  onsetTime: string | null;
  hasSituation: boolean;
  threshold: number;
}) {
  const flagged = side.minutes_from_onset !== null;
  return (
    <div className="side">
      <div className="side-head">
        <div>
          <span className="side-kind">{title}</span>
          <span className="side-stop">{side.stop_name ?? side.stop_place}</span>
        </div>
        <div className="side-times">
          <span className="sched">{fmtTime(side.scheduled_time)}</span>
          <Actual side={side} />
        </div>
      </div>
      {flagged ? (
        <table className="pred-table">
          <thead>
            <tr>
              <th>Prediksjon gitt</th>
              <th>Innstilt</th>
              <th>Forsinkelse</th>
              <th title="Operatørens varslingsterskel">Varsel ≥ {fmtPct(threshold)}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <SnapshotRow
              label={onsetTime ? `Ved melding ${onsetTime}` : 'Ved melding'}
              snap={side.at_message}
              flagged
            />
            <SnapshotRow label="60 min før" snap={side.t60} flagged />
            <SnapshotRow label="30 min før" snap={side.t30} flagged />
          </tbody>
        </table>
      ) : (
        <p className="not-flagged">
          {hasSituation
            ? 'Dette stoppet ble ikke flagget av meldingen — ingen prediksjon.'
            : 'Ingen SX-melding gjaldt denne avgangen — ingen prediksjon.'}
        </p>
      )}
    </div>
  );
}

export default function JourneyCard({ journey }: { journey: DemoJourney }) {
  const sit = journey.situation;
  const onsetTime = sit?.onset ? fmtTime(sit.onset) : null;
  return (
    <article className="card">
      <header className="card-head">
        <span className="mode-tag" title={journey.operator}>{journey.mode}</span>
        <span className="line-badge">{lineCode(journey.line)}</span>
        <span className="card-times">
          {fmtTime(journey.departure.scheduled_time)} → {fmtTime(journey.arrival.scheduled_time)}
        </span>
        {sit ? (
          <span className="situation" title={`${sit.situation_number} · ${sit.category ?? ''}`}>
            <span className="situation-dot" />
            {sit.summary ?? sit.situation_number}
          </span>
        ) : (
          <span className="situation situation-none">Ingen aktiv SX-melding</span>
        )}
      </header>
      <div className="sides">
        <Side title="Avgang" side={journey.departure} onsetTime={onsetTime} hasSituation={sit !== null}
              threshold={journey.alert_threshold} />
        <Side title="Ankomst" side={journey.arrival} onsetTime={onsetTime} hasSituation={sit !== null}
              threshold={journey.alert_threshold} />
      </div>
    </article>
  );
}
