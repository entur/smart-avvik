import { Fragment } from 'react';
import { Heading3, SubParagraph } from '@entur/typography';
import type { Level, MatrixRow, StatsRow } from '../api';
import { LEVEL_LABEL, SLOT_LABEL, fmtLead, fmtPct } from '../format';

/** Level order, severity ascending — the matrix's row and column order, and what makes "adjacent"
 *  a distance of one rather than a hand-written pair list. */
const LEVEL_ORDER: Level[] = ['green', 'yellow', 'red'];

/** Column labels for the matrix. Abbreviated because the grid pins those three columns at 2.1rem
 *  (see `.rh-matrix` in styles.css) and «grønt» does not fit; the full word rides on the title, and
 *  the ROW labels in the auto-sized first column are spelled out. */
const LEVEL_SHORT: Record<Level, string> = { green: 'grø', yellow: 'gul', red: 'rød' };

/** Which tint a cell gets. Distance in LEVEL_ORDER, not a lookup table: the diagonal is a hit, one
 *  step off (green/yellow, yellow/red) is near, and the two green/red corners are far.
 *
 *  Zero wins over all of it. An empty cell tinted "far" would draw the eye to a failure mode that
 *  never happened, which on a thin window is most of the grid. */
function cellClass(forecast: Level, actual: Level, count: number): string {
  if (count === 0) return 'rh-cell rh-cell--empty';
  const distance = Math.abs(LEVEL_ORDER.indexOf(forecast) - LEVEL_ORDER.indexOf(actual));
  if (distance === 0) return 'rh-cell rh-cell--hit';
  if (distance === 1) return 'rh-cell rh-cell--near';
  return 'rh-cell rh-cell--far';
}

/** Forecast level down the rows, realized level across the columns, every cell printing its count
 *  as text inside the tint.
 *
 *  The number is not redundant with the colour: the two structurally different misses — forecast
 *  red on a yellow rush, forecast yellow on a red one — sit in cells that share the `near` tint,
 *  and without the counts the grid stops distinguishing over-warning from under-warning, which is
 *  the one thing a threshold discussion needs from it. */
function ConfusionMatrix({ rows }: { rows: MatrixRow[] }) {
  const byForecast = new Map(rows.map((r) => [r.forecast, r]));
  return (
    <div
      className="rh-matrix"
      title="Rader: nivået vi meldte. Kolonner: nivået rushet faktisk fikk."
    >
      {/* The corner cell disambiguates the orientation; the labels around it name the two axes. */}
      <span className="rh-matrix-axis">meldt ↓</span>
      {LEVEL_ORDER.map((actual) => (
        <span key={`col-${actual}`} className="rh-matrix-axis" title={`ble ${LEVEL_LABEL[actual]}`}>
          {LEVEL_SHORT[actual]}
        </span>
      ))}
      {LEVEL_ORDER.map((forecast) => {
        const row = byForecast.get(forecast);
        return (
          <Fragment key={forecast}>
            <span className="rh-matrix-axis">{LEVEL_LABEL[forecast]}</span>
            {LEVEL_ORDER.map((actual) => {
              const count = row ? row[actual] : 0;
              return (
                <span
                  key={`${forecast}-${actual}`}
                  className={cellClass(forecast, actual, count)}
                  title={`Meldt ${LEVEL_LABEL[forecast]}, ble ${LEVEL_LABEL[actual]}: ${count}`}
                >
                  {count}
                </span>
              );
            })}
          </Fragment>
        );
      })}
    </div>
  );
}

/** One exclusion chip. Rendered only when its count is above zero: a row of five zeroes says
 *  nothing and pushes the figures that do off the fold. */
function Count({ label, n, title }: { label: string; n: number; title: string }) {
  if (n <= 0) return null;
  return (
    <span className="rh-count" title={title}>
      {label}: {n}
    </span>
  );
}

/** «Slik har varslene truffet»: a total row plus one row per issue slot (KD6).
 *
 *  Segmented rather than blended because the baselines push on LEVEL and the follow-ups on level
 *  CHANGE — two different decisions with two different calibrations — so a single headline moves
 *  with the mix of the two rather than with the product. */
/** One rate cell: the percentage over the count it is computed from.
 *
 *  The denominator is not decoration. Each of these three figures is over a DIFFERENT population,
 *  and on a page whose first weeks are almost entirely «ikke vurdert» a bare percentage invites the
 *  one reading that is wrong — 100 % over two bad rushes and 100 % over two hundred are the same
 *  glyph. Varslingstid already showed its own; these did not.
 *
 *  Varslingstid and Avgjørelser stay inline — one carries a differently-worded child, the other no
 *  tooltip, so folding them in would take a flag per difference. */
function RateCell({
  value,
  den,
  unit,
  title,
}: {
  value: number | null;
  den: number;
  unit: string;
  title: string;
}) {
  return (
    <td className="rh-num" title={title}>
      {fmtPct(value)}
      <span className="rh-population">
        ({den} {unit})
      </span>
    </td>
  );
}

export default function HitTable({ stats }: { stats: StatsRow[] }) {
  const total = stats.find((r) => r.slot === 'total');
  return (
    <section className="rh-summary">
      <Heading3>Slik har varslene truffet</Heading3>

      <div className="rh-scroll">
        <table className="rh-table">
          <thead>
            <tr>
              <th scope="col">Tidspunkt</th>
              <th scope="col">Presisjon (samlet)</th>
              <th scope="col">Presisjon (varslet)</th>
              <th scope="col">Dekning</th>
              <th scope="col">Varslingstid</th>
              <th scope="col">Avgjørelser</th>
              <th scope="col">
                Treffmatrise <span className="rh-population">meldt ↓ / ble →</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {stats.map((row) => (
              <tr key={row.slot} className={row.slot === 'total' ? 'rh-row-total' : ''}>
                {/* Each row states the population its Presisjon figures are over — three of the
                    five figures use different denominators, and a reader who assumes one across
                    the row reads the page wrong. */}
                {/* A real row header, so a screen reader announces which slot each figure belongs
                    to. `.rh-row-label` undoes .rh-table th's uppercase/nowrap, which is what makes
                    a th usable here at all. */}
                <th scope="row" className="rh-row-label">
                  <div>{SLOT_LABEL[row.slot]}</div>
                  {/* "utstedelser", not "dager": on a slot row the two happen to coincide (one
                      issue per slot per day), but the total row counts four issues per day, so
                      "dager" would overstate the window fourfold on exactly the row a reader
                      trusts most. */}
                  <span className="rh-population">
                    {row.n_scored} avklarte, kvalifiserte utstedelser
                  </span>
                </th>
                <RateCell
                  value={row.presisjon_samlet}
                  den={row.n_scored}
                  unit="vurdert"
                  title="Andel av de vurderte utstedelsene med utfallet «Traff»."
                />
                <RateCell
                  value={row.presisjon_varslet}
                  den={row.presisjon_varslet_den}
                  unit="varslet"
                  title="«Traff» + «Traff nesten» blant de vurderte utstedelsene der et varsel sto i kraft. Friskmeldinger er holdt utenfor — en friskmelding er ikke et varsel."
                />
                <RateCell
                  value={row.dekning}
                  den={row.dekning_den}
                  unit={row.slot === "total" ? "rush" : "utstedelser"}
                  title="Andel av de gule og røde rushene vi hadde et varsel i kraft for. Videre grunnlag enn Presisjon: sene utstedelser og runder utenfor frist er med."
                />
                <td
                  className="rh-num"
                  title="Median tid fra pushen til rushet startet. Målt fra rushets egen start, så morgentallene ligger rundt null."
                >
                  {fmtLead(row.varslingstid_min)}{' '}
                  <span className="rh-population">({row.varslingstid_n} varsler)</span>
                </td>
                <td className="rh-num">{row.avgjorelser}</td>
                <td>
                  <ConfusionMatrix rows={row.matrix} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* §14a's measured bad-day rate per forecast level. Without it the diagonal reads as a defect:
          it compares two differently-scaled categoricals — forecast red lands on 13 % of days,
          realized red on 6,4 % — so a red forecast that is perfectly calibrated still sits in
          «traff nesten» more often than in «traff». */}
      <p className="rh-calibration">
        Kalibrering (§14a, hele måleperioden): grønt varsel ≈ 6 % dårlige dager, gult ≈ 21 %, rødt
        ≈ 55 %. Et velkalibrert rødt varsel havner derfor oftere i «traff nesten» enn i «traff» —
        det er forventet, ikke en feil.
      </p>

      <SubParagraph className="rh-population">
        Presisjon-tallene gjelder avklarte, kvalifiserte dager. Dekning bruker et videre grunnlag:
        sene utstedelser og runder utenfor frist er tatt med, fordi varselet nådde den reisende
        likevel.
      </SubParagraph>

      {/* What the rates above leave out, from the total row. Counted rather than hidden: a rate over
          three days and a rate over three hundred are different claims. */}
      {total ? (
        <div className="rh-counts">
          <Count
            label="Venter"
            n={total.n_venter}
            title="Fasit for rushet er ikke satt ennå. Nattjobben forsøker på nytt i ti dager."
          />
          <Count
            label="Ukjent"
            n={total.n_ukjent}
            title="Fasit kan ikke lenger fastslås, eller dagen er ikke kvalifisert. Ikke regnet som «riktig stille»."
          />
          <Count
            label="Ikke vurdert"
            n={total.n_ikke_vurdert - total.n_ikke_vurdert_etter_start}
            title="Ingen varselrunde ble vurdert, og dagen er eldre enn runde-loggen: den forventede etterslepet fra før siden fantes."
          />
          {/* Its own chip, because the two causes are not the same news. Inside the backlog count
              a lost write is invisible during exactly the weeks the backlog is large. */}
          <Count
            label="Mangler runde"
            n={total.n_ikke_vurdert_etter_start}
            title="Ingen varselrunde ble vurdert selv om runde-loggen dekket dagen: varslingen var slått av i dette miljøet, eller skrivingen av runden feilet."
          />
          <Count
            label="Sen utstedelse"
            n={total.n_late}
            title="Prognosen ble regnet ut mer enn 10 min etter sin egen frist og så dermed virkeligheten etter fristen. Utenfor Presisjon, med i Dekning."
          />
          <Count
            label="Utenfor frist"
            n={total.n_untimely}
            title="Runden ble vurdert etter sin leveringsfrist, så ingen push kunne gå ut i tid. Utenfor Presisjon, med i Dekning."
          />
        </div>
      ) : null}
    </section>
  );
}
