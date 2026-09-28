import { Heading4 } from '@entur/typography';
import type { CalibrationRow, Coverage, Level } from '../api';

const LEVEL_TITLE: Record<Level, string> = {
  green: 'Grønt varsel',
  yellow: 'Gult varsel',
  red: 'Rødt varsel',
};

/** §14a's measured bad-rush rate per forecast level over the whole training period — what a level
 *  promises in the long run, printed so a short window's figure has something to be read against. */
const LONG_RUN: Record<Level, string> = { green: '6 %', yellow: '21 %', red: '55 %' };

const pct = (num: number, den: number) => Math.round((100 * num) / den);

/** «Når vi sa grønt, gult, rødt»: per forecast level, how often the rush turned bad. A good
 *  forecast is not one that always hits but one whose colour means what it promises. */
export function Calibration({ rows }: { rows: CalibrationRow[] }) {
  if (rows.every((r) => r.n === 0)) return null;
  return (
    <div>
      <Heading4>Når vi sa grønt, gult eller rødt – hvor ofte ble rushet dårlig?</Heading4>
      <p className="rh-view-help">
        Alle fire prognosetidspunkter, avklarte dager. Dårlig betyr at rushet ble gult eller rødt.
        Rutenettet viser andelen som 100 prognoser.
      </p>
      <div className="rh-cal">
        {rows.map((r) => {
          const share = r.n > 0 ? pct(r.n_bad, r.n) : null;
          return (
            <div className="rh-cal-card" key={r.level}>
              <span>
                <span className={`rh-level rh-level--${r.level}`}>{LEVEL_TITLE[r.level]}</span>
              </span>
              {share === null ? (
                <span className="rh-cal-foot">Ingen prognoser på dette nivået i vinduet.</span>
              ) : (
                <>
                  <span className="rh-big">
                    {r.n_bad} av {r.n}
                  </span>
                  <span>ble dårlige ({share} %)</span>
                  <span
                    className="rh-cal-grid"
                    role="img"
                    aria-label={`${share} av 100 ble dårlige`}
                  >
                    {Array.from({ length: 100 }, (_, k) => (
                      <span key={k} className={k < share ? 'is-bad' : undefined} />
                    ))}
                  </span>
                  <span className="rh-cal-foot">
                    Modellen sa i snitt{' '}
                    {r.mean_p === null ? '–' : `${Math.round(100 * r.mean_p)} %`} · over tid ca.{' '}
                    {LONG_RUN[r.level]}
                  </span>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

const SEGMENTS = [
  { key: 'bad_first', cls: 'first', label: 'Dårlig, varslet ved første prognose' },
  { key: 'bad_late', cls: 'late', label: 'Dårlig, varslet ved oppdateringen' },
  { key: 'bad_missed', cls: 'missed', label: 'Dårlig, ikke varslet' },
  { key: 'calm_warned', cls: 'calm-warned', label: 'Rolig, men varslet' },
  { key: 'calm_quiet', cls: 'calm-quiet', label: 'Rolig og stille' },
] as const;

/** «Ble du varslet – og hva kostet det?»: the window per RUSH, from a commuter's side. */
export function CoverageView({ c }: { c: Coverage }) {
  if (c.rushes === 0) return null;
  const perDay = (c.calm_warned / c.days).toFixed(1).replace('.', ',');
  return (
    <div>
      <Heading4>Ble du varslet – og hva kostet det?</Heading4>
      <p className="rh-view-help">
        Per rush: fikk du beskjed før et dårlig rush, og hvor mange varsler fikk du om rush som gikk
        fint? Varslet betyr at det gikk ut et rushvarsel kl. 06:30 eller 12:00, ellers ved
        oppdateringen kl. 07:00 eller 14:00 – eller at det ville gått ut, der runden er
        rekonstruert.
      </p>
      <div className="rh-cov">
        <div className="rh-cov-card">
          <span className="rh-cov-label">Dårlige rush</span>
          <span className="rh-big">
            {c.bad === 0 ? 'Ingen i vinduet' : `${c.bad_first + c.bad_late} av ${c.bad} varslet`}
          </span>
          {c.bad > 0 ? (
            <span>
              {c.bad_first} ved første prognose, {c.bad_late} ved oppdateringen, {c.bad_missed}{' '}
              uten varsel.
            </span>
          ) : null}
        </div>
        <div className="rh-cov-card">
          <span className="rh-cov-label">Prisen</span>
          <span className="rh-big">{c.calm_warned} ekstra varsler</span>
          <span>
            om rush som gikk fint – omtrent {perDay} per hverdag. {c.calm_quiet} av{' '}
            {c.calm_quiet + c.calm_warned} rolige rush var stille.
          </span>
        </div>
      </div>
      <div
        className="rh-cov-bar"
        role="img"
        aria-label={SEGMENTS.map((s) => `${s.label}: ${c[s.key]}`).join(', ')}
      >
        {SEGMENTS.map((s) =>
          c[s.key] > 0 ? (
            <span
              key={s.key}
              className={`rh-seg--${s.cls}`}
              style={{ width: `${(100 * c[s.key]) / c.rushes}%` }}
              title={`${s.label}: ${c[s.key]}`}
            />
          ) : null,
        )}
      </div>
      <div className="rh-cov-legend">
        {SEGMENTS.map((s) => (
          <span key={s.key}>
            <i className={`rh-seg--${s.cls}`} />
            {s.label} ({c[s.key]})
          </span>
        ))}
      </div>
    </div>
  );
}
