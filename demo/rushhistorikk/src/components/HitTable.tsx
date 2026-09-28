import { Heading3, SubParagraph } from '@entur/typography';
import type { CalibrationRow, Coverage, Level, MatrixRow, StatsRow } from '../api';
import { LEVEL_LABEL, SLOT_LABEL, fmtPct } from '../format';
import { Calibration, CoverageView } from './ForecastViews';

/** Level order, severity ascending — the matrix's row and column order. */
const LEVEL_ORDER: Level[] = ['green', 'yellow', 'red'];

/** A cell's classes carry both levels: `.rh-cell--meldt-*` sets the colour of the lower-left half
 *  (the row, what we communicated) and `.rh-cell--ble-*` the upper-right half (the column, what the
 *  rush became); `.rh-cell` paints the split. On the diagonal the two are one colour (`--hit`), and
 *  a zero is faded rather than greyed (`--empty`) — see `.rh-matrix` in styles.css for why. */
function cellClass(forecast: Level, actual: Level, count: number): string {
  const classes = ['rh-cell', `rh-cell--meldt-${forecast}`, `rh-cell--ble-${actual}`];
  if (forecast === actual) classes.push('rh-cell--hit');
  if (count === 0) classes.push('rh-cell--empty');
  return classes.join(' ');
}

/** The qualifiers printed beside «Varsler sendt», in the order a reader reconciles the count with
 *  the matrix. First the pushes on days whose outcome has not settled: in the count and not yet in
 *  the matrix, which is why a same-day view's count exceeds its matrix — the first question the
 *  page got asked. Then the pushes reconstructed rounds would have sent, which are in neither. */
function sentNotes(row: Pick<StatsRow, 'varsler_venter' | 'varsler_rekonstruert'>): string[] {
  const notes: string[] = [];
  if (row.varsler_venter > 0) notes.push(`${row.varsler_venter} venter`);
  if (row.varsler_rekonstruert > 0) notes.push(`+${row.varsler_rekonstruert} rekonstruert`);
  return notes;
}

/** Forecast down the rows, realized level across the columns, no axis labels: the levels are
 *  painted into the cells, and the header's «meldt ↓ / ble →» plus the legend under the table carry
 *  the orientation.
 *
 *  The number is not redundant with the colour: colour says WHICH two levels met, the count how
 *  often — and the two structurally different misses (forecast red on a yellow rush, forecast
 *  yellow on a red one) are the same two colours in swapped halves, so the grid keeps
 *  distinguishing over-warning from under-warning, which is the one thing a threshold discussion
 *  needs from it. */
function ConfusionMatrix({ rows }: { rows: MatrixRow[] }) {
  const byForecast = new Map(rows.map((r) => [r.forecast, r]));
  return (
    <div
      className="rh-matrix"
      title="Rader: nivået vi meldte. Kolonner: nivået rushet faktisk fikk."
    >
      {LEVEL_ORDER.map((forecast) => {
        const row = byForecast.get(forecast);
        return LEVEL_ORDER.map((actual) => {
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
        });
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
/** One rate cell. Just the percentage: the denominators («12 varsler», «9 rush») were shown beside
 *  it until 2026-09-02 and dropped at the product owner's request, along with the row's scored
 *  count. What each rate is over is in its tooltip and in the footnote under the table. */
function RateCell({ value, title }: { value: number | null; title: string }) {
  return (
    <td className="rh-num" title={title}>
      {fmtPct(value)}
    </td>
  );
}

export default function HitTable({
  stats,
  calibration,
  coverage,
}: {
  stats: StatsRow[];
  calibration: CalibrationRow[];
  coverage: Coverage | null;
}) {
  const total = stats.find((r) => r.slot === 'total');
  return (
    <section className="rh-summary">
      <Heading3>Slik har varslene truffet</Heading3>

      <div className="rh-scroll">
        <table className="rh-table">
          <thead>
            <tr>
              <th scope="col">Tidspunkt</th>
              <th scope="col">Presisjon</th>
              <th scope="col">Dekning</th>
              <th scope="col">Varsler sendt</th>
              <th scope="col">
                Treffmatrise <span className="rh-population">meldt ↓ / ble →</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {stats.map((row) => (
              <tr key={row.slot} className={row.slot === 'total' ? 'rh-row-total' : ''}>
                {/* A real row header, so a screen reader announces which slot each figure belongs
                    to. `.rh-row-label` undoes .rh-table th's uppercase/nowrap, which is what makes
                    a th usable here at all. */}
                <th scope="row" className="rh-row-label">
                  {SLOT_LABEL[row.slot]}
                </th>
                <RateCell
                  value={row.presisjon}
                  title="Av varslene som gikk ut: andelen som var berettiget. Et gult eller rødt varsel på et rush som ble gult eller rødt – uansett hvilket av de to – eller en friskmelding på et rush som ble grønt. Stille dager er ikke med: de er ikke varsler."
                />
                <RateCell
                  value={row.dekning}
                  title={
                    row.slot === 'total'
                      ? 'Av rushene der et varsel burde gått ut: andelen som fikk et riktig varsel ved minst én av sine to utstedelser. Et rush som først ble varslet kl 07:00 eller 14:00 er dekket her; tidsradene viser hvor det glapp. Videre grunnlag enn Presisjon: sene utstedelser og runder utenfor frist er med.'
                      : 'Av situasjonene der et varsel burde gått ut ved dette tidspunktet: andelen der et riktig varsel gikk ut. En situasjon er et gult eller rødt rush uten stående varsel, eller et grønt rush med et stående varsel (friskmelding skyldig). Videre grunnlag enn Presisjon: sene utstedelser og runder utenfor frist er med.'
                  }
                />
                <td
                  className="rh-num"
                  title="Antall push som gikk ut i vinduet, friskmeldinger medregnet. Én per runde, uansett hvor mange enheter den nådde — og uavklarte dager er med: en push er sendt uansett hva dagen ble. «N venter» er push på dager som ennå ikke har fasit: de teller her, men står ikke i treffmatrisen før dagen er avklart. «+N rekonstruert» er push rekonstruerte runder ville ha sendt; de gikk aldri ut."
                >
                  {row.varsler_sendt}
                  {sentNotes(row).length > 0 ? (
                    <>
                      {' '}
                      <span className="rh-population">({sentNotes(row).join(', ')})</span>
                    </>
                  ) : null}
                </td>
                <td>
                  <ConfusionMatrix rows={row.matrix} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* What the split means. The swatch is the under-warning the page exists to find — meldt
          yellow, ble red — and the sentence is the one place the encoding is spelled out, since the
          axis labels are gone. */}
      <p className="rh-matrix-legend">
        <span
          className="rh-cell rh-cell--meldt-yellow rh-cell--ble-red rh-matrix-swatch"
          aria-hidden="true"
        />
        Treffmatrisen: nedre venstre halvdel er nivået vi meldte, øvre høyre nivået rushet ble. Én
        farge betyr at de var like.
      </p>

      {/* §14a's measured bad-day rate per forecast level. Without it the diagonal reads as a defect:
          it compares two differently-scaled categoricals — forecast red lands on 13 % of days,
          realized red on 6,4 % — so a red forecast that is perfectly calibrated still sits in
          «traff nesten» more often than in «traff». */}
      <p className="rh-calibration">
        Kalibrering (§14a, hele måleperioden): grønt varsel ≈ 6 % dårlige dager, gult ≈ 21 %, rødt
        ≈ 55 %. Et velkalibrert rødt varsel havner derfor oftere i «traff nesten» enn i «traff» —
        det er forventet, ikke en feil, og begge regnes som berettiget i Presisjon.
      </p>

      <SubParagraph className="rh-population">
        Presisjon: av varslene som gikk ut på avklarte, kvalifiserte dager, andelen som var
        berettiget. Dekning: av situasjonene der et varsel burde gått ut, andelen der det gjorde det
        – per rush på totalraden, per utstedelse på de fire tidsradene, og sene utstedelser og
        runder utenfor frist er tatt med, fordi et varsel som nådde den reisende teller likevel. En
        stille dag inngår i ingen av dem. «Varsler sendt» teller alle push i vinduet, uavklarte
        dager medregnet – de står som «N venter» ved tallet, og er grunnen til at tallet kan være
        høyere enn summen i treffmatrisen.
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
            title="Fasit kan ikke lenger fastslås, eller dagen er ikke kvalifisert. Ikke regnet som «traff»."
          />
          <Count
            label="Rekonstruert"
            n={total.n_reconstructed}
            title="Runder uten registrering, rekonstruert fra prognoseloggen: det regelverket ville ha avgjort med varsling på og i tide. De teller i ratene over – siden skal bedømme modellen – men ikke i «Varsler sendt»."
          />
          {/* Its own chip, because the two causes of a reconstruction are not the same news. Inside
              the backlog count a lost write is invisible during exactly the weeks the backlog is
              large. */}
          <Count
            label="Mangler runde"
            n={total.n_reconstructed_after_start}
            title="Rekonstruert selv om runde-loggen dekket dagen: varslingen var slått av i dette miljøet, eller skrivingen av runden feilet."
          />
          <Count
            label="Ikke vurdert"
            n={total.n_ikke_vurdert}
            title="Ingen varselrunde ble registrert, og den kunne heller ikke rekonstrueres (prognosen mangler tidsstempel, eller regelverket avviste raden)."
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

      <div className="rh-views">
        <Calibration rows={calibration} />
        {coverage ? <CoverageView c={coverage} /> : null}
      </div>
    </section>
  );
}
