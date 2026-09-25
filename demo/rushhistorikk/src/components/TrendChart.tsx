import { useState } from 'react';
import { Heading4, SubParagraph } from '@entur/typography';
import type { TrendPoint } from '../api';
import { fmtPct } from '../format';

// Plot geometry in viewBox units; the SVG scales to the section's width. A near-copy of
// /historikk's chart (demo/varselhistorikk), since each front-end is built from its own directory.
const W = 920;
const L = 44;
const R = 20;
const TOP = 12;
const H = 180;
const BAR_TOP = 204;
const BAR_H = 44;
const HEIGHT = 256;

/** ISO week number of a Monday given as an ISO date. */
function isoWeek(iso: string): number {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 3 - ((d.getUTCDay() + 6) % 7)); // the week's Thursday
  const yearStart = Date.UTC(d.getUTCFullYear(), 0, 1);
  return Math.ceil(((d.getTime() - yearStart) / 86_400_000 + 1) / 7);
}

function weekSpan(iso: string): string {
  const start = new Date(`${iso}T00:00:00Z`);
  const end = new Date(start.getTime() + 4 * 86_400_000); // Friday: rush forecasts are weekdays only
  const fmt = (d: Date) => d.toLocaleDateString('nb-NO', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  return `${fmt(start)}–${fmt(end)}`;
}

const dot = (cx: number, cy: number, r: number) =>
  `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`;

/** Presisjon and Dekning per week, the total row's figures over each week alone, with the pushes
 *  behind Presisjon as bars. Weekly because a day has at most four rounds: a daily rate would be
 *  over zero, one or two pushes. */
export default function TrendChart({ trend }: { trend: TrendPoint[] }) {
  const [picked, setPicked] = useState<number | null>(null);
  const rated = (p: TrendPoint) => p.presisjon !== null || p.dekning !== null;
  const lastRated = trend.map(rated).lastIndexOf(true);
  if (trend.length < 2 || lastRated < 0) return null;

  const sel = picked !== null && picked < trend.length ? picked : lastRated;
  const n = trend.length;
  const sw = (W - L - R) / n;
  const x = (i: number) => L + (i + 0.5) * sw;
  const y = (v: number) => TOP + (1 - v) * H;
  const maxPushes = Math.max(1, ...trend.map((p) => p.presisjon_den));
  const bw = Math.min(sw * 0.44, 28);
  const bar = (i: number, p: TrendPoint) => {
    const h = (BAR_H * p.presisjon_den) / maxPushes;
    return `M${x(i) - bw / 2} ${BAR_TOP + BAR_H}v${-h}h${bw}v${h}Z`;
  };
  const line = (rate: (p: TrendPoint) => number | null) => {
    let d = '';
    let pen = false;
    trend.forEach((p, i) => {
      const v = rate(p);
      if (v === null) {
        pen = false;
        return;
      }
      d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const dots = (rate: (p: TrendPoint) => number | null) =>
    trend.map((p, i) => (rate(p) === null ? '' : dot(x(i), y(rate(p) as number), 3.5))).join('');
  // Trailing weeks with nothing rated yet and outcomes still pending: the fasit has not landed.
  let firstPending = n;
  while (firstPending > 0 && !rated(trend[firstPending - 1]) && trend[firstPending - 1].n_venter > 0) {
    firstPending -= 1;
  }
  const labelEvery = Math.ceil(n / 15);
  const s = trend[sel];

  return (
    <div className="rh-trend">
      <div className="rh-trend-head">
        <div>
          <Heading4 className="rh-trend-title">Presisjon og dekning over tid</Heading4>
          <SubParagraph className="rh-trend-help">
            Per uke, samme regler som totalraden over. Dekning teller rush.
          </SubParagraph>
        </div>
        <div className="rh-trend-legend" aria-hidden="true">
          <span>
            <svg width="28" height="10">
              <line x1="0" y1="5" x2="28" y2="5" className="rh-trend-precision" />
              <circle cx="14" cy="5" r="3.5" className="rh-trend-precision-dot" />
            </svg>
            Presisjon
          </span>
          <span>
            <svg width="28" height="10">
              <line x1="0" y1="5" x2="28" y2="5" className="rh-trend-recall" />
              <circle cx="14" cy="5" r="3.5" className="rh-trend-recall-dot" />
            </svg>
            Dekning
          </span>
          <span>
            <svg width="12" height="12">
              <rect width="12" height="12" rx="2" className="rh-trend-bar" />
            </svg>
            Varsler vurdert
          </span>
        </div>
      </div>

      <div className="rh-trend-readout" aria-live="polite">
        <strong>
          Uke {isoWeek(s.week_start)} <span className="rh-trend-muted">({weekSpan(s.week_start)})</span>
        </strong>
        {rated(s) ? (
          <>
            <span>
              Presisjon <strong>{fmtPct(s.presisjon)}</strong>{' '}
              <span className="rh-trend-muted">
                · {s.presisjon_num} av {s.presisjon_den} varsler
              </span>
            </span>
            <span>
              Dekning <strong>{fmtPct(s.dekning)}</strong>{' '}
              <span className="rh-trend-muted">
                · {s.dekning_num} av {s.dekning_den} rush
              </span>
            </span>
          </>
        ) : (
          <span className="rh-trend-muted">
            {s.n_venter > 0 ? 'Venter på fasit.' : 'Ingen varsler og ingen rush som krevde varsel.'}
          </span>
        )}
      </div>

      <svg
        className="rh-trend-svg"
        viewBox={`0 0 ${W} ${HEIGHT + 34}`}
        role="img"
        aria-label="Presisjon og dekning per uke"
      >
        {firstPending < n ? (
          <>
            <rect x={L + firstPending * sw} y={0} width={(n - firstPending) * sw} height={HEIGHT}
                  className="rh-trend-pending" />
            {(n - firstPending) * sw >= 90 ? (
              <text x={L + ((firstPending + n) / 2) * sw} y={26} textAnchor="middle" className="rh-trend-axis">
                Venter på fasit
              </text>
            ) : null}
          </>
        ) : null}
        {[1, 0.75, 0.5, 0.25, 0].map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} className="rh-trend-grid" />
            <text x={L - 8} y={y(v) + 4} textAnchor="end" className="rh-trend-axis">
              {Math.round(v * 100)} %
            </text>
          </g>
        ))}
        <line x1={L} x2={W - R} y1={BAR_TOP + BAR_H} y2={BAR_TOP + BAR_H} className="rh-trend-grid" />
        <path d={trend.map((p, i) => bar(i, p)).join('')} className="rh-trend-bar" />
        <path d={bar(sel, s)} className="rh-trend-bar is-selected" />
        <text x={L - 8} y={BAR_TOP + 10} textAnchor="end" className="rh-trend-axis">
          {maxPushes}
        </text>
        <line x1={x(sel)} x2={x(sel)} y1={TOP} y2={TOP + H} className="rh-trend-cursor" />
        <path d={line((p) => p.dekning)} className="rh-trend-recall" />
        <path d={dots((p) => p.dekning)} className="rh-trend-recall-dot" />
        <path d={line((p) => p.presisjon)} className="rh-trend-precision" />
        <path d={dots((p) => p.presisjon)} className="rh-trend-precision-dot" />
        {trend.map((p, i) =>
          i % labelEvery === 0 ? (
            <text key={p.week_start} x={x(i)} y={HEIGHT + 18} textAnchor="middle"
                  className={`rh-trend-axis ${i === sel ? 'is-selected' : ''}`}>
              uke {isoWeek(p.week_start)}
            </text>
          ) : null,
        )}
        {/* One hit area per week: hover, tap and keyboard focus all move the readout above. */}
        {trend.map((p, i) => (
          <rect
            key={p.week_start}
            x={L + i * sw}
            y={0}
            width={sw}
            height={HEIGHT}
            className="rh-trend-hit"
            tabIndex={0}
            aria-label={`Uke ${isoWeek(p.week_start)}: ${
              rated(p) ? `presisjon ${fmtPct(p.presisjon)}, dekning ${fmtPct(p.dekning)}` : 'ingen rate'
            }`}
            onMouseEnter={() => setPicked(i)}
            onFocus={() => setPicked(i)}
            onClick={() => setPicked(i)}
          />
        ))}
      </svg>

      <SubParagraph className="rh-trend-help">
        En uke har bare noen få varsler — les linjene sammen med søylene. Første og siste uke i
        vinduet kan være ufullstendige.
      </SubParagraph>
    </div>
  );
}
