import { Heading3, Heading4, Paragraph, SubParagraph } from '@entur/typography';
import type { Segment, Summary } from '../api';
import { fmtMin, fmtPct } from '../format';

/** How much of the window actually has a verdict. A rate computed over a fifth of the decisions
 *  is a different claim from one computed over almost all of them, and the reader cannot tell
 *  which they are looking at unless we say so. */
function coverage(s: Summary): { resolved: number; total: number; pct: number } {
  const resolved = s.n_hit + s.n_false_alarm + s.n_miss + s.n_correct_reject;
  const total = resolved + s.n_unknown + s.n_pending;
  return { resolved, total, pct: total === 0 ? 0 : resolved / total };
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="vh-tile">
      <SubParagraph className="vh-tile-label">{label}</SubParagraph>
      <div className="vh-tile-value">{value}</div>
      {hint ? <SubParagraph className="vh-tile-hint">{hint}</SubParagraph> : null}
    </div>
  );
}

/** One side of the split, with its counts beside its rate.
 *
 *  The counts are not decoration: "100 %" over two alerts and "100 %" over two hundred are
 *  different claims, and one of these two segments is usually the small one. Which one is small
 *  moves the blended figure at the top all on its own. */
function SplitRow({ label, help, segment }: { label: string; help: string; segment: Segment }) {
  const alerts = segment.n_hit + segment.n_false_alarm;
  return (
    <div className="vh-split-row">
      <div>
        <div className="vh-split-label">{label}</div>
        <SubParagraph className="vh-split-help">{help}</SubParagraph>
      </div>
      <div className="vh-split-figure">
        <div className="vh-split-value">{fmtPct(segment.precision)}</div>
        <SubParagraph className="vh-split-hint">
          {segment.n_hit} av {alerts} varsler
          {segment.n_miss > 0 ? ` · ${segment.n_miss} bom` : ''}
        </SubParagraph>
      </div>
    </div>
  );
}

/** The blended precision, and what it is made of.
 *
 *  Deliberately in this order. The headline stays blended because that is the question the page
 *  exists to answer — could a traveller trust what we sent? — and relaying a cancellation ET has
 *  already published is most of the app's value, not a cheat: nobody carries the ET feed on their
 *  phone. But the two populations behave nothing alike (a declared cancellation is scored 1.0 by
 *  construction), so the blended number moves with the MIX and can swing without the app having
 *  changed. Showing it without showing the mix is the misreading this section prevents. */
function PrecisionSplit({ summary }: { summary: Summary }) {
  const { predicted, relayed } = summary;
  const segmented =
    predicted.n_hit + predicted.n_false_alarm + predicted.n_miss + predicted.n_correct_reject +
    relayed.n_hit + relayed.n_false_alarm + relayed.n_miss + relayed.n_correct_reject;

  return (
    <section className="vh-split">
      <Heading4 className="vh-split-head">Hva den samlede presisjonen består av</Heading4>

      {segmented === 0 ? (
        // Two empty rows of dashes would look like a broken query rather than like a young column.
        <SubParagraph className="vh-split-help">
          Ingen avgjørelser i dette vinduet er merket ennå. Skillet ble innført 5. august 2026;
          eldre avgjørelser kan ikke plasseres i ettertid.
        </SubParagraph>
      ) : (
        <>
          <SplitRow
            label="Vi så det komme"
            help="Modellene flagget avgangen mens den fortsatt bare var et varsel. Det er her terskelen skal justeres."
            segment={predicted}
          />
          <SplitRow
            label="Videreformidlet fra ET"
            help="Avgangen var allerede meldt innstilt i sanntidsdata. Ekte verdi for den reisende, men ingen prøve på modellene."
            segment={relayed}
          />
        </>
      )}

      {summary.n_unsegmented > 0 ? (
        <SubParagraph className="vh-split-note">
          {summary.n_unsegmented} avgjørelser er fra før vi begynte å skille, og telles bare i den
          samlede presisjonen.
        </SubParagraph>
      ) : null}
    </section>
  );
}

export default function SummaryStrip({ summary }: { summary: Summary }) {
  const { resolved, total, pct } = coverage(summary);
  return (
    <section className="vh-summary">
      <Heading3>Slik har varslene truffet</Heading3>

      <div className="vh-tiles">
        <Tile
          label="Presisjon (samlet)"
          value={fmtPct(summary.precision)}
          hint={`${summary.n_hit} av ${summary.n_hit + summary.n_false_alarm} varsler traff`}
        />
        <Tile
          label="Dekning"
          value={fmtPct(summary.recall)}
          hint={`${summary.n_miss} avvik gikk uten varsel`}
        />
        <Tile
          label="Varslingstid"
          value={fmtMin(summary.median_lead_min_on_hits)}
          hint={`${summary.n_hit_with_30min_notice} varsler kom 30+ min før avgang`}
        />
        <Tile
          label="Avgjørelser"
          value={String(total)}
          hint={`${resolved} med fasit (${fmtPct(pct)})`}
        />
      </div>

      <div className="vh-verdict-counts">
        <span className="vh-count vh-count--success">Traff {summary.n_hit}</span>
        <span className="vh-count vh-count--warning">Falsk alarm {summary.n_false_alarm}</span>
        <span className="vh-count vh-count--negative">Bom {summary.n_miss}</span>
        <span className="vh-count vh-count--neutral">Riktig stille {summary.n_correct_reject}</span>
        <span className="vh-count vh-count--muted">Ukjent {summary.n_unknown}</span>
        <span className="vh-count vh-count--muted">Venter {summary.n_pending}</span>
      </div>

      <PrecisionSplit summary={summary} />

      {/* Stated on the page, not just in the API docs: the number is easy to over-read, and
          whoever tunes a threshold on the strength of it needs to know what it is not. */}
      <Paragraph className="vh-caveat">
        «Dekning» måles blant avganger som faktisk ble flagget av en SX-melding, på reiser noen har
        registrert. Et avvik som aldri ble meldt, ser vi ikke — så dette er ikke dekning over alle
        avvik. «Ukjent» betyr at vi ikke fant igjen avgangen i sanntidsdata; den telles bevisst ikke
        som «riktig stille». Den samlede presisjonen blander to ulike populasjoner, og flytter seg
        med blandingsforholdet mellom dem — les den sammen med oppdelingen over.
      </Paragraph>
    </section>
  );
}
