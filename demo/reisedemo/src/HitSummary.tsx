import type { DemoJourney, DemoSide, DemoSnapshot } from './api';
import { fmtRate } from './format';

// "Slik ville varslene truffet" — the search's own verdicts, added up.
//
// The conditional in the title is load-bearing. /historikk says "har truffet" because those alerts
// were really sent to somebody's phone; nothing here ever was. This is the live alert gate
// (alert_policy.threshold_for, per operator) replayed over a holdout the models never trained on,
// which is a claim about the models and not about a delivery pipeline.
//
// COUNTED PER DEPARTURE, on the boarding stop only. A varsel is a push you get before you board,
// and varsel.py matches on the boarding call, so that is the decision this box is about. The cards
// below still show the arrival side, and it is deliberately not folded in: "will my train leave?"
// and "will I get there on time?" are two different events, and averaging them yields a precision
// that answers neither. It also keeps the grain honest — exactly one decision per journey in the
// result, so the counts here are countable in the list underneath.

/** The most informative snapshot that existed before the call — what the traveller would actually
 *  have been holding. Later ask times only exist when the SX message had arrived by then. */
function lastSnapshot(side: DemoSide): DemoSnapshot | null {
  return side.t30 ?? side.t60 ?? side.at_message;
}

interface Tally {
  hit: number;
  false_alarm: number;
  miss: number;
  correct_reject: number;
  /** Scored, but the realized outcome was never observed. Kept apart from `correct_reject` on
   *  purpose: counting an unobserved call as "went fine" manufactures precision out of a gap in
   *  the ET data — the same rule /historikk applies to its `ukjent`. */
  unresolved: number;
  /** No prediction at this ask time at all: either no SX message flagged the departure, or the
   *  message had not yet arrived this far ahead of it. */
  unscored: number;
}

const EMPTY: Tally = {
  hit: 0,
  false_alarm: 0,
  miss: 0,
  correct_reject: 0,
  unresolved: 0,
  unscored: 0,
};

const scored = (t: Tally) => t.hit + t.false_alarm + t.miss + t.correct_reject;
const alerts = (t: Tally) => t.hit + t.false_alarm;
const troubles = (t: Tally) => t.hit + t.miss;

function tally(journeys: DemoJourney[], pick: (side: DemoSide) => DemoSnapshot | null): Tally {
  const t: Tally = { ...EMPTY };
  for (const j of journeys) {
    const snap = pick(j.departure);
    if (snap === null) t.unscored += 1;
    else if (snap.verdict === null) t.unresolved += 1;
    else t[snap.verdict] += 1;
  }
  return t;
}

// The three ask times, in the order the cards show them. Each covers a different set of
// departures — a prediction 30 minutes out only exists when the message arrived at least that
// early — which is why every row below carries its own denominator instead of borrowing the
// headline's.
const ASK_TIMES = [
  {
    key: 'at_message',
    label: 'Ved melding',
    help: 'Idet avviksmeldingen kom, uten observert tilstand på toget ennå. Gjelder alle flaggede avganger.',
  },
  {
    key: 't60',
    label: '60 min før',
    help: 'Bare avganger der meldingen var ute minst en time i forveien.',
  },
  {
    key: 't30',
    label: '30 min før',
    help: 'Nærmest avgang, og best informert — men bare der meldingen rakk å komme så tidlig.',
  },
] as const;

function Tile({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="hs-tile">
      <div className="hs-tile-label">{label}</div>
      <div className="hs-tile-value">{value}</div>
      <div className="hs-tile-hint">{hint}</div>
    </div>
  );
}

/** One ask time, with its counts beside each figure.
 *
 *  Both rates, never just precision: they move in opposite directions under the same threshold, so
 *  a row showing only the first would hide exactly the trade a partner is being asked to judge.
 *  The third figure is the row's population — without it, "100 %" over three departures and
 *  "100 %" over three hundred read the same, and the later ask times are always the small ones. */
function SplitRow({ label, help, t }: { label: string; help: string; t: Tally }) {
  return (
    <div className="hs-split-row">
      <div>
        <div className="hs-split-label">{label}</div>
        <div className="hs-split-help">{help}</div>
      </div>
      <div className="hs-split-figures">
        <div className="hs-split-figure">
          <div className="hs-split-figure-label">Presisjon</div>
          <div className="hs-split-value">{fmtRate(t.hit, alerts(t))}</div>
          <div className="hs-split-hint">
            {t.hit} av {alerts(t)} varsler
          </div>
        </div>
        <div className="hs-split-figure">
          <div className="hs-split-figure-label">Dekning</div>
          <div className="hs-split-value">{fmtRate(t.hit, troubles(t))}</div>
          <div className="hs-split-hint">
            {t.hit} av {troubles(t)} avvik
          </div>
        </div>
        <div className="hs-split-figure">
          {/* Named "Målt her", not "Avganger": the tile above carries the whole search under that
              word, and two different numbers one row apart under one label is the misreading this
              box exists to avoid. */}
          <div className="hs-split-figure-label">Målt her</div>
          <div className="hs-split-value">{scored(t)}</div>
          <div className="hs-split-hint">avganger med fasit</div>
        </div>
      </div>
    </div>
  );
}

export default function HitSummary({ journeys }: { journeys: DemoJourney[] }) {
  // The headline reads the last prediction before departure; the split below reads each ask time
  // on its own. Computed over the whole search, not the filtered list: "vis bare reiser med
  // SX-melding" is a view filter on the cards, and the two rates are identical either way (an
  // unflagged departure can only land in `unscored`), so following it would move nothing but the
  // totals — and a headline that twitches when you tick a checkbox invites the wrong reading.
  const last = tally(journeys, lastSnapshot);

  // Departures nothing scored that turned out to be trouble anyway. Not misses — no model ever saw
  // them — but they are the honest edge of the coverage figure above, and hiding them would let
  // "dekning" read as coverage of all trouble on the stretch. Two ways to land here, which is why
  // the note names the STOP rather than the journey: no message at all, or a message that named the
  // journey without naming its boarding call.
  // (Live serving has scored unflagged rail calls since 2026-08-19; this holdout predates that.)
  const unflaggedTrouble = journeys.filter(
    (j) => lastSnapshot(j.departure) === null && j.departure.actual_trouble === true,
  ).length;

  return (
    <section className="hs-summary">
      <h2 className="hs-head">Slik ville varslene truffet</h2>
      <p className="hs-sub">
        Ingen varsler ble sendt den gangen. Dette er varslingsterskelen spilt av på nytt over de{' '}
        {journeys.length} avgangene i søket — målt på siste prediksjon før avgang, på
        påstigningsstoppet.
      </p>

      {scored(last) === 0 ? (
        // Three tiles of dashes would read as a broken page rather than as the quiet stretch this
        // usually is: a window where nothing was flagged is the normal case, not a failure.
        <p className="hs-empty">
          Ingen av avgangene i søket har en prediksjon med kjent utfall — ingenting å måle her.
        </p>
      ) : (
        <>
          <div className="hs-tiles">
            <Tile
              label="Presisjon"
              value={fmtRate(last.hit, alerts(last))}
              hint={`${last.hit} av ${alerts(last)} varsler traff`}
            />
            <Tile
              label="Dekning"
              value={fmtRate(last.hit, troubles(last))}
              hint={`${last.miss} avvik gikk uten varsel`}
            />
            <Tile
              label="Avganger i søket"
              value={String(journeys.length)}
              hint={`${scored(last)} med prediksjon og fasit`}
            />
          </div>

          <div className="hs-counts">
            <span className="hs-count hs-count--hit">Traff {last.hit}</span>
            <span className="hs-count hs-count--false">Falsk alarm {last.false_alarm}</span>
            <span className="hs-count hs-count--miss">Bom {last.miss}</span>
            <span className="hs-count hs-count--ok">Riktig ro {last.correct_reject}</span>
            {last.unresolved > 0 && (
              <span className="hs-count hs-count--muted">Ukjent utfall {last.unresolved}</span>
            )}
            {last.unscored > 0 && (
              <span className="hs-count hs-count--muted">Uten prediksjon {last.unscored}</span>
            )}
          </div>

          {unflaggedTrouble > 0 && (
            <p className="hs-note">
              {unflaggedTrouble} av avgangene uten prediksjon ble likevel forstyrrelser. Ingen
              avviksmelding flagget påstigningsstoppet, så modellene scoret dem aldri — de teller
              verken som bom eller i dekningen over.
            </p>
          )}

          {/* Collapsed by default, and a native <details>: the disclosure semantics, the keyboard
              handling and "find on page opens it" come for free, and there is no state here worth
              lifting. Collapsed because the tiles are the answer and this is the working — three
              rows of figures unfurled would push the actual journey cards off the screen. */}
          <details className="hs-split">
            <summary className="hs-split-summary">
              <span className="hs-split-head">Hvordan tallene endrer seg fram mot avgang</span>
            </summary>
            {ASK_TIMES.map((a) => (
              <SplitRow key={a.key} label={a.label} help={a.help} t={tally(journeys, (s) => s[a.key])} />
            ))}
            <p className="hs-caveat">
              De tre radene dekker ulike avganger, så de kan ikke leses rett opp og ned: en
              prediksjon 30 minutter før avgang finnes bare når meldingen kom minst så tidlig, og de
              meldingene som kommer sent er ofte de vanskeligste. «Dekning» måles bare blant
              avganger der påstigningsstoppet faktisk ble flagget av en avviksmelding — et avvik
              ingen meldte fra om, ser modellene ikke i dette datasettet, så dette er ikke dekning
              over alle avvik. «Ukjent utfall» betyr at avgangen ikke ble gjenfunnet i
              sanntidsdata; den telles bevisst ikke som «riktig ro». Alle tall gjelder
              påstigningsstoppet — ankomstsiden på kortene under måles ikke her.
            </p>
          </details>
        </>
      )}
    </section>
  );
}
