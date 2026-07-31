import { Heading3, Paragraph, SubParagraph } from '@entur/typography';
import type { Summary } from '../api';
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

export default function SummaryStrip({ summary }: { summary: Summary }) {
  const { resolved, total, pct } = coverage(summary);
  return (
    <section className="vh-summary">
      <Heading3>Slik har varslene truffet</Heading3>

      <div className="vh-tiles">
        <Tile
          label="Presisjon"
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

      {/* Stated on the page, not just in the API docs: the number is easy to over-read, and
          whoever tunes a threshold on the strength of it needs to know what it is not. */}
      <Paragraph className="vh-caveat">
        «Dekning» måles blant avganger som faktisk ble flagget av en SX-melding, på reiser noen har
        registrert. Et avvik som aldri ble meldt, ser vi ikke — så dette er ikke dekning over alle
        avvik. «Ukjent» betyr at vi ikke fant igjen avgangen i sanntidsdata; den telles bevisst ikke
        som «riktig stille».
      </Paragraph>
    </section>
  );
}
