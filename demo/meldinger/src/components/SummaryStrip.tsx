import { Heading3, Heading4, Paragraph, SubParagraph } from '@entur/typography';
import type { Segment, Summary } from '../api';
import { fmtMin, fmtPct } from '../format';

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="tb-tile">
      <SubParagraph className="tb-tile-label">{label}</SubParagraph>
      <div className="tb-tile-value">{value}</div>
      {hint ? <SubParagraph className="tb-tile-hint">{hint}</SubParagraph> : null}
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
 *  is why its hint names its own denominator instead of borrowing the one above it. Note it comes
 *  whole from the nightly job — unlike the cards' `alert_lead_min`, which is window-scoped — so it
 *  stays comparable with /historikk's.
 *
 *  The counts are not decoration: "100 %" over two alerts and "100 %" over two hundred are
 *  different claims, and one of these segments is usually the small one. Which one is small moves
 *  the blended figures at the top all on its own. */
function SplitRow({ label, help, segment }: { label: string; help: string; segment: Segment }) {
  const alerts = segment.n_hit + segment.n_false_alarm;
  const troubles = segment.n_hit + segment.n_miss;
  return (
    <div className="tb-split-row">
      <div>
        <div className="tb-split-label">{label}</div>
        <SubParagraph className="tb-split-help">{help}</SubParagraph>
      </div>
      <div className="tb-split-figures">
        <div className="tb-split-figure">
          <SubParagraph className="tb-split-figure-label">Presisjon</SubParagraph>
          <div className="tb-split-value">{fmtPct(segment.precision)}</div>
          <SubParagraph className="tb-split-hint">
            {segment.n_hit} av {alerts} varsler
          </SubParagraph>
        </div>
        <div className="tb-split-figure">
          <SubParagraph className="tb-split-figure-label">Dekning</SubParagraph>
          <div className="tb-split-value">{fmtPct(segment.recall)}</div>
          <SubParagraph className="tb-split-hint">
            {segment.n_hit} av {troubles} avvik
          </SubParagraph>
        </div>
        {/* Counted per departure, not per varsel, so the hint names its own denominator — a bare
            "5 med 30+ min" beside "59 av 97 varsler" invites subtracting one from the other. */}
        <div className="tb-split-figure">
          <SubParagraph className="tb-split-figure-label">Varslingstid</SubParagraph>
          <div className="tb-split-value">{fmtMin(segment.median_lead_min_on_hits)}</div>
          <SubParagraph className="tb-split-hint">
            {segment.n_hit_with_30min_notice} av {segment.n_hit_trains} avganger
          </SubParagraph>
        </div>
      </div>
    </div>
  );
}

const rowTotal = (s: Segment | undefined) =>
  s ? s.n_hit + s.n_false_alarm + s.n_miss + s.n_correct_reject : 0;

/** The blended rates, and what they are made of.
 *
 *  The headline stays blended because that is the question the app is judged on — could a traveller
 *  trust what we sent? — and relaying a cancellation ET has already published is most of its value,
 *  not a cheat: nobody carries the ET feed on their phone. But the populations behave nothing alike
 *  (a declared cancellation is scored 1.0 by construction, and cannot be missed), so the blended
 *  numbers move with the MIX and can swing without the app having changed. Showing them without
 *  showing the mix is the misreading this section prevents, and it applies to every figure: the
 *  relayed segment carries perfect recall by construction too.
 *
 *  The third row is decisions about trains NO message named at all, scored from real-time data
 *  alone — strictly less information than the forecasts, and nothing published to relay. It is the
 *  row that answers whether we can warn people about something the operator has not warned them
 *  about. The fourth (2026-09-23) is trains Vy took out of the day's timetable without publishing a
 *  cancellation: served at 1.0 like the relayed row, with nothing published to relay. */
function SegmentSplit({ summary }: { summary: Summary }) {
  const { predicted, relayed, no_sx: noSx, removed } = summary;
  const segmented =
    rowTotal(predicted) + rowTotal(relayed) + rowTotal(noSx) + rowTotal(removed);

  return (
    // Collapsed by default, and native <details> rather than a useState + aria-expanded button: the
    // disclosure semantics, the keyboard handling and the "find on page opens it" behaviour come
    // for free, and this section has no state of its own worth lifting.
    //
    // Collapsed is a deliberate default, not a demotion. This is the mix the tiles are made of,
    // which is what you open when a figure moves and you need to know whether the app changed or
    // the population did. Three sets of three figures unfurled above the first card pushed the
    // actual decisions off the screen.
    <details className="tb-split">
      <summary className="tb-split-summary">
        <Heading4 className="tb-split-head">Hva de samlede tallene består av</Heading4>
      </summary>

      {segmented === 0 ? (
        // Rows of dashes would look like a broken query rather than like a young column.
        <SubParagraph className="tb-split-help">
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
          {/* Only when it has rows. Until the no-SX path has produced any, an empty third row of
              dashes would read as a broken segment rather than as one nothing has landed in yet —
              the same reason the whole section hides itself when nothing is segmented at all. */}
          {rowTotal(noSx) > 0 ? (
            <SplitRow
              label="Ingen melding fra operatøren"
              help="Ingen avviksmelding var publisert; vurderingen bygger på sanntidsdata alene. Dette er svaret på om vi kan varsle om noe operatøren ikke har varslet om."
              segment={noSx}
            />
          ) : null}
          {/* The fourth row (2026-09-23), shown on the same condition as the third. A train taken
              out of the plan is served at 1.0 like a relayed cancellation, but nothing was
              published, so it is not «Videreformidlet fra ET» either. */}
          {rowTotal(removed) > 0 ? (
            <SplitRow
              label="Tatt ut av rutetabellen"
              help="Toget manglet i dagens rutetabell, og ingen innstilling var publisert. Vi meldte det som sikkert — ingen prøve på modellene."
              segment={removed}
            />
          ) : null}
        </>
      )}

      {summary.n_unsegmented > 0 ? (
        <SubParagraph className="tb-split-note">
          {summary.n_unsegmented} avgjørelser er fra før vi begynte å skille, og telles bare i de
          samlede tallene.
        </SubParagraph>
      ) : null}

      {/* What the headline figures are NOT — stated on the page, not just in the API docs, because
          each of them is easy to over-read and whoever tunes a threshold on one needs the caveat.
          It sits INSIDE the disclosure, with the split it is about. */}
      <Paragraph className="tb-caveat">
        «Dekning» måles blant avganger som faktisk ble flagget av en SX-melding, på reiser noen har
        registrert. Et avvik som aldri ble meldt, ser vi ikke — så dette er ikke dekning over alle
        avvik. «Ukjent» betyr at vi ikke fant igjen avgangen i sanntidsdata; den telles bevisst ikke
        som «riktig stille». «Varslingstid» telles per avgang, ikke per melding, og er
        hentet for hele avgjørelsens levetid — som varslingstiden på kortene nå også er. Alle de
        samlede tallene blander fire ulike populasjoner og flytter seg med
        blandingsforholdet mellom dem. Les dem sammen med oppdelingen over — og med hvor mange av
        avgjørelsene som i det hele tatt har fasit.
      </Paragraph>
    </details>
  );
}

export default function SummaryStrip({ summary }: { summary: Summary }) {
  const { n_subjects: subjects, n_labelled: labelled } = summary;
  return (
    <section className="tb-summary">
      <Heading3>Slik har varslene truffet i vinduet</Heading3>

      <div className="tb-tiles">
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
          value={String(subjects)}
          hint={`${labelled} med fasit (${fmtPct(subjects === 0 ? 0 : labelled / subjects)})`}
        />
      </div>

      <div className="tb-verdict-counts">
        <span className="tb-count tb-count--success">Traff {summary.n_hit}</span>
        <span className="tb-count tb-count--warning">Falsk alarm {summary.n_false_alarm}</span>
        <span className="tb-count tb-count--negative">Bom {summary.n_miss}</span>
        <span className="tb-count tb-count--neutral">Riktig stille {summary.n_correct_reject}</span>
        <span className="tb-count tb-count--muted">Ukjent {summary.n_unknown}</span>
        <span className="tb-count tb-count--muted">Venter {summary.n_pending}</span>
      </div>

      <SegmentSplit summary={summary} />
    </section>
  );
}
