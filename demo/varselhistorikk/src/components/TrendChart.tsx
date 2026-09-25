import { useState } from 'react';
import { Heading4, SubParagraph } from '@entur/typography';
import type { DailyPoint, Summary } from '../api';
import { fmtPct } from '../format';

// Plot geometry in viewBox units; the SVG scales to the card's width.
const W = 920;
const L = 44;
const R = 20;
const TOP = 12;
const H = 180;
const BAR_TOP = 204;
const BAR_H = 44;
const HEIGHT = 256;

const WEEKDAYS = ['søn', 'man', 'tir', 'ons', 'tor', 'fre', 'lør'];
const WEEKDAYS_LONG = ['søndag', 'mandag', 'tirsdag', 'onsdag', 'torsdag', 'fredag', 'lørdag'];

interface Day {
  iso: string;
  date: Date;
  point: DailyPoint | undefined;
  /** After the last labelled day: the nightly labelling has not reached it yet. */
  pending: boolean;
}

/** Every operating day in the window, labelled or not, so a gap reads as a gap. */
function daysOf(summary: Summary): Day[] {
  const byDate = new Map((summary.daily ?? []).map((d) => [d.call_date, d]));
  const last = summary.daily?.length ? summary.daily[summary.daily.length - 1].call_date : '';
  const days: Day[] = [];
  // UTC arithmetic on the ISO dates: they are calendar days, not instants.
  for (
    let t = Date.parse(`${summary.from_date}T00:00:00Z`);
    t <= Date.parse(`${summary.to_date}T00:00:00Z`);
    t += 86_400_000
  ) {
    const date = new Date(t);
    const iso = date.toISOString().slice(0, 10);
    days.push({ iso, date, point: byDate.get(iso), pending: iso > last });
  }
  return days;
}

const dot = (cx: number, cy: number, r: number) =>
  `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`;

/** A line broken wherever a day has no rate, rather than drawn straight across the gap. */
function linePath(days: Day[], x: (i: number) => number, y: (v: number) => number,
                  rate: (p: DailyPoint) => number | null): string {
  let d = '';
  let pen = false;
  days.forEach((day, i) => {
    const v = day.point ? rate(day.point) : null;
    if (v === null) {
      pen = false;
      return;
    }
    d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}

/** Presisjon and Dekning per operating day, with the alert volume under them.
 *
 *  The bars are not decoration: a day's rates swing with how few alerts it rests on, and a 100 %
 *  over three alerts next to 70 % over three hundred is only readable with the counts in view. */
export default function TrendChart({ summary }: { summary: Summary }) {
  const days = daysOf(summary);
  const lastLabelled = days.map((d) => !!d.point).lastIndexOf(true);
  const [picked, setPicked] = useState<number | null>(null);
  const sel = picked !== null && picked < days.length ? picked : lastLabelled;

  if (lastLabelled < 0) return null;

  const n = days.length;
  const sw = (W - L - R) / n;
  const x = (i: number) => L + (i + 0.5) * sw;
  const y = (v: number) => TOP + (1 - v) * H;
  const alerts = (p: DailyPoint) => p.n_hit + p.n_false_alarm;
  const maxAlerts = Math.max(1, ...days.map((d) => (d.point ? alerts(d.point) : 0)));
  const bw = sw * 0.44;
  const bar = (i: number, p: DailyPoint) => {
    const h = (BAR_H * alerts(p)) / maxAlerts;
    return `M${x(i) - bw / 2} ${BAR_TOP + BAR_H}v${-h}h${bw}v${h}Z`;
  };
  const dots = (rate: (p: DailyPoint) => number | null, r: number) =>
    days
      .map((d, i) => {
        const v = d.point ? rate(d.point) : null;
        return v === null ? '' : dot(x(i), y(v), r);
      })
      .join('');
  const firstPending = days.findIndex((d) => d.pending);
  const labelEvery = Math.ceil(n / 15);
  const s = days[sel];
  const sp = s?.point;

  return (
    <div className="vh-trend">
      <div className="vh-trend-head">
        <div>
          <Heading4 className="vh-trend-title">Presisjon og dekning over tid</Heading4>
          <SubParagraph className="vh-trend-help">
            Per døgn, blant avgjørelser med fasit. Samme filtre som tallene over.
          </SubParagraph>
        </div>
        <div className="vh-trend-legend" aria-hidden="true">
          <span>
            <svg width="28" height="10">
              <line x1="0" y1="5" x2="28" y2="5" className="vh-trend-precision" />
              <circle cx="14" cy="5" r="3.5" className="vh-trend-precision-dot" />
            </svg>
            Presisjon
          </span>
          <span>
            <svg width="28" height="10">
              <line x1="0" y1="5" x2="28" y2="5" className="vh-trend-recall" />
              <circle cx="14" cy="5" r="3.5" className="vh-trend-recall-dot" />
            </svg>
            Dekning
          </span>
          <span>
            <svg width="12" height="12">
              <rect width="12" height="12" rx="2" className="vh-trend-bar" />
            </svg>
            Varsler sendt
          </span>
        </div>
      </div>

      <div className="vh-trend-readout" aria-live="polite">
        {s ? (
          <strong>
            {WEEKDAYS_LONG[s.date.getUTCDay()]} {s.date.getUTCDate()}.{' '}
            {s.date.toLocaleDateString('nb-NO', { month: 'long', timeZone: 'UTC' })}
          </strong>
        ) : null}
        {sp ? (
          <>
            <span>
              Presisjon <strong>{fmtPct(sp.precision)}</strong>{' '}
              <span className="vh-trend-muted">
                · {sp.n_hit} av {alerts(sp)} varsler
              </span>
            </span>
            <span>
              Dekning <strong>{fmtPct(sp.recall)}</strong>{' '}
              <span className="vh-trend-muted">
                · {sp.n_hit} av {sp.n_hit + sp.n_miss} avvik
              </span>
            </span>
          </>
        ) : s ? (
          <span className="vh-trend-muted">
            {s.pending ? 'Venter på fasit — merkes i nattens kjøring.' : 'Ingen avgjørelser med fasit.'}
          </span>
        ) : null}
      </div>

      <svg
        className="vh-trend-svg"
        viewBox={`0 0 ${W} ${HEIGHT + 34}`}
        role="img"
        aria-label={`Presisjon og dekning per døgn, ${summary.from_date} til ${summary.to_date}`}
      >
        {firstPending >= 0 ? (
          <>
            <rect x={L + firstPending * sw} y={0} width={(n - firstPending) * sw} height={HEIGHT}
                  className="vh-trend-pending" />
            {(n - firstPending) * sw >= 90 ? (
              <text x={L + ((firstPending + n) / 2) * sw} y={26} textAnchor="middle"
                    className="vh-trend-axis">
                Venter på fasit
              </text>
            ) : null}
          </>
        ) : null}
        {[1, 0.75, 0.5, 0.25, 0].map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} className="vh-trend-grid" />
            <text x={L - 8} y={y(v) + 4} textAnchor="end" className="vh-trend-axis">
              {Math.round(v * 100)} %
            </text>
          </g>
        ))}
        <line x1={L} x2={W - R} y1={BAR_TOP + BAR_H} y2={BAR_TOP + BAR_H} className="vh-trend-grid" />
        <path
          d={days.map((d, i) => (d.point ? bar(i, d.point) : '')).join('')}
          className="vh-trend-bar"
        />
        {sp ? <path d={bar(sel, sp)} className="vh-trend-bar is-selected" /> : null}
        <text x={L - 8} y={BAR_TOP + 10} textAnchor="end" className="vh-trend-axis">
          {maxAlerts}
        </text>
        <line x1={x(sel)} x2={x(sel)} y1={TOP} y2={TOP + H} className="vh-trend-cursor" />
        <path d={linePath(days, x, y, (p) => p.recall)} className="vh-trend-recall" />
        <path d={dots((p) => p.recall, 3.5)} className="vh-trend-recall-dot" />
        <path d={linePath(days, x, y, (p) => p.precision)} className="vh-trend-precision" />
        <path d={dots((p) => p.precision, 3.5)} className="vh-trend-precision-dot" />
        {days.map((d, i) =>
          i % labelEvery === 0 ? (
            <text key={d.iso} x={x(i)} textAnchor="middle"
                  className={`vh-trend-axis ${i === sel ? 'is-selected' : ''}`}>
              <tspan x={x(i)} y={HEIGHT + 14}>{d.date.getUTCDate()}.</tspan>
              <tspan x={x(i)} y={HEIGHT + 29}>{WEEKDAYS[d.date.getUTCDay()]}</tspan>
            </text>
          ) : null,
        )}
        {/* One hit area per day: hover, tap and keyboard focus all move the readout above. */}
        {days.map((d, i) => (
          <rect
            key={d.iso}
            x={L + i * sw}
            y={0}
            width={sw}
            height={HEIGHT}
            className="vh-trend-hit"
            tabIndex={0}
            aria-label={`${d.iso}: ${
              d.point
                ? `presisjon ${fmtPct(d.point.precision)}, dekning ${fmtPct(d.point.recall)}`
                : d.pending
                  ? 'venter på fasit'
                  : 'ingen fasit'
            }`}
            onMouseEnter={() => setPicked(i)}
            onFocus={() => setPicked(i)}
            onClick={() => setPicked(i)}
          />
        ))}
      </svg>

      <SubParagraph className="vh-trend-help">
        Et døgn med få varsler svinger mye — les linjene sammen med søylene.
      </SubParagraph>
    </div>
  );
}
