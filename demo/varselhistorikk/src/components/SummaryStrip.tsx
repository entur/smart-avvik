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

/** One side of the split, with its counts beside each figure.
 *
 *  All three, not just precision: they answer different questions and routinely move in opposite
 *  directions, since a threshold raised until precision looks respectable buys it straight out of
 *  recall. A segment showing only the first would hide exactly that trade — and it is the
 *  `predicted` segment, the one a threshold is actually tuned on, where they diverge most.
 *
 *  Lead time is here because it splits hardest of the three, and the blended tile above hides it
 *  completely: measured over 14 days of prd, the median predicted train warned the traveller 15
 *  minutes ahead against the relayed segment's 177. A reader who takes the tile's figure for the
 *  models' warning time is wrong by hours, in the flattering direction.
 *
 *  It is also the only figure on this page counted per DEPARTURE rather than per situation, which
 *  is why its hint names its own denominator instead of borrowing the one above it.
 *
 *  The counts are not decoration: "100 %" over two alerts and "100 %" over two hundred are
 *  different claims, and one of these two segments is usually the small one. Which one is small
 *  moves the blended figures at the top all on its own. Each rate carries its OWN denominator —
 *  alerts sent for precision, disruptions that arrived for recall — because they are different
 *  populations of the same segment, and a single count under both would fit neither. */
function SplitRow({ label, help, segment }: { label: string; help: string; segment: Segment }) {
  const alerts = segment.n_hit + segment.n_false_alarm;
  const troubles = segment.n_hit + segment.n_miss;
  return (
    <div className="vh-split-row">
      <div>
        <div className="vh-split-label">{label}</div>
        <SubParagraph className="vh-split-help">{help}</SubParagraph>
      </div>
      <div className="vh-split-figures">
        <div className="vh-split-figure">
          <SubParagraph className="vh-split-figure-label">Presisjon</SubParagraph>
          <div className="vh-split-value">{fmtPct(segment.precision)}</div>
          <SubParagraph className="vh-split-hint">
            {segment.n_hit} av {alerts} varsler
          </SubParagraph>
        </div>
        <div className="vh-split-figure">
          <SubParagraph className="vh-split-figure-label">Dekning</SubParagraph>
          <div className="vh-split-value">{fmtPct(segment.recall)}</div>
          <SubParagraph className="vh-split-hint">
            {segment.n_hit} av {troubles} avvik
          </SubParagraph>
        </div>
        {/* Counted per departure, not per varsel, so the hint names its own denominator — a bare
            "5 med 30+ min" beside "59 av 97 varsler" invites subtracting one from the other. */}
        <div className="vh-split-figure">
          <SubParagraph className="vh-split-figure-label">Varslingstid</SubParagraph>
          <div className="vh-split-value">{fmtMin(segment.median_lead_min_on_hits)}</div>
          <SubParagraph className="vh-split-hint">
            {segment.n_hit_with_30min_notice} av {segment.n_hit_trains} avganger
          </SubParagraph>
        </div>
      </div>
    </div>
  );
}

/** The blended rates, and what they are made of.
 *
 *  Deliberately in this order. The headline stays blended because that is the question the page
 *  exists to answer — could a traveller trust what we sent? — and relaying a cancellation ET has
 *  already published is most of the app's value, not a cheat: nobody carries the ET feed on their
 *  phone. But the two populations behave nothing alike (a declared cancellation is scored 1.0 by
 *  construction, and cannot be missed), so the blended numbers move with the MIX and can swing
 *  without the app having changed. Showing them without showing the mix is the misreading this
 *  section prevents. It applies to both: the relayed segment carries perfect recall by
 *  construction too, so the headline coverage figure is diluted the same way the headline
 *  precision is. */
function SegmentSplit({ summary }: { summary: Summary }) {
  const { predicted, relayed } = summary;
  const segmented =
    predicted.n_hit + predicted.n_false_alarm + predicted.n_miss + predicted.n_correct_reject +
    relayed.n_hit + relayed.n_false_alarm + relayed.n_miss + relayed.n_correct_reject;

  return (
    <section className="vh-split">
      <Heading4 className="vh-split-head">Hva de samlede tallene består av</Heading4>

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
          {summary.n_unsegmented} avgjørelser er fra før vi begynte å skille, og telles bare i de
          samlede tallene.
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
          hint={`${summary.n_hit_with_30min_notice} av ${summary.n_hit_trains} avganger fikk 30+ min`}
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

      <SegmentSplit summary={summary} />

      {/* Stated on the page, not just in the API docs: the number is easy to over-read, and
          whoever tunes a threshold on the strength of it needs to know what it is not. */}
      <Paragraph className="vh-caveat">
        «Dekning» måles blant avganger som faktisk ble flagget av en SX-melding, på reiser noen har
        registrert. Et avvik som aldri ble meldt, ser vi ikke — så dette er ikke dekning over alle
        avvik. «Ukjent» betyr at vi ikke fant igjen avgangen i sanntidsdata; den telles bevisst ikke
        som «riktig stille». «Varslingstid» telles per avgang, ikke per melding: operatørene sender
        ofte flere meldinger om samme tog, og den reisende får ett varsel. Alle de tre samlede
        tallene blander to ulike populasjoner og flytter seg med blandingsforholdet mellom dem —
        varslingstiden aller mest, siden en innstilling ofte er meldt i sanntidsdata timer før
        avgang. Les dem sammen med oppdelingen over.
      </Paragraph>
    </section>
  );
}
