import { useEffect, useState } from 'react';
import { SmallAlertBox } from '@entur/alert';
import { Loader } from '@entur/loader';
import { Heading1, Paragraph } from '@entur/typography';
import DecisionCard from './components/DecisionCard';
import SummaryStrip from './components/SummaryStrip';
import {
  MIDNIGHT,
  fetchConfig,
  fetchDecisions,
  fetchFilterOptions,
  fetchMessages,
  fetchSummary,
} from './api';
import type {
  Decision,
  FilterOptions,
  Filters,
  ReplayedMessage,
  Summary,
  Verdict,
} from './api';
import {
  VERDICT_LABEL,
  alignMessages,
  isBounded,
  lineCode,
  osloDaysAgo,
  osloToday,
  timeInputValue,
} from './format';

/** What the "Utfall" dropdown can be set to: no filter, or one of the four verdicts. It maps
 *  one-to-one onto the API's `verdict`, which is exactly the four values the log records.
 *
 *  "Alle med fasit" is not in here but is its own checkbox: it is not a verdict but a widening of
 *  all four, and a dropdown makes every entry look mutually exclusive with every other. */
type UtfallFilter = Verdict | '';

// The four verdicts are built from VERDICT_LABEL rather than retyped, so the filter and the badges
// can never disagree about what a verdict is called.
const UTFALL_OPTIONS: Array<{ value: UtfallFilter; label: string }> = [
  { value: '', label: 'Alle utfall' },
  ...(Object.entries(VERDICT_LABEL) as Array<[Verdict, string]>).map(([value, label]) => ({
    value,
    label,
  })),
];

/** The vocabulary the dropdowns start with: nothing, until /tilbakeblikk-api/filters answers. Held
 *  as a value rather than `null` so every consumer below can read `.lines` without a guard. */
const NO_OPTIONS: FilterOptions = {
  day: '',
  from_time: '',
  to_time: '',
  window_start: '',
  window_end: '',
  operators: [],
  lines: [],
  from_places: [],
  to_places: [],
};

/** How far back the day picker will go — the API's own lookback bound (historikk._MAX_WINDOW_DAYS).
 *  A day further back is a 400, and a picker offering it misreports what the surface can answer. */
const MAX_WINDOW_DAYS = 180;

/** The step on both time inputs, in seconds. 15 minutes, because that is the grid the API quantizes
 *  both ends of the range to — and the quantization is not cosmetic: every parameter lands in the
 *  response cache key on a public unauthenticated endpoint, so two free-form second-resolution
 *  times are on the order of a billion distinct keys per day, each one a cache entry and a BigQuery
 *  query. A minute-resolution input the server silently snaps is a control that misreports itself,
 *  so the picker offers exactly the boundaries the server honours. */
const TIME_STEP_S = 900;

export default function App() {
  // The window: one Oslo day, and a from/to time of day. MIDNIGHT at either end means the default —
  // the start of the day, and "the rest of the day" — and travels to the API as an omitted
  // parameter, because an <input type="time"> cannot express "the FOLLOWING midnight" and the API
  // rejects a literal 00:00 end. See MIDNIGHT in api.ts.
  const [day, setDay] = useState(osloToday());
  const [fromTime, setFromTime] = useState(MIDNIGHT);
  const [toTime, setToTime] = useState(MIDNIGHT);

  const [utfall, setUtfall] = useState<UtfallFilter>('');
  // All three narrowings default OFF: the page opens on every decision it has, including the ones
  // still waiting for a verdict and the ones we stayed quiet on. Each of them is a population, and
  // a page that hides one by default measures something narrower than it says it does.
  const [resolvedOnly, setResolvedOnly] = useState(false);
  const [alertedOnly, setAlertedOnly] = useState(false);
  const [sxOnly, setSxOnly] = useState(false);
  const [operator, setOperator] = useState('');
  // The three data-driven dropdowns. Their values are REFS — a full line ref, an NSR StopPlace ref —
  // because that is what the decision log stores and what the API filters on; the readable name is
  // only ever the label. So an unnamed stop still filters correctly, it just shows as its ref.
  const [line, setLine] = useState('');
  const [fromPlace, setFromPlace] = useState('');
  const [toPlace, setToPlace] = useState('');

  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [options, setOptions] = useState<FilterOptions>(NO_OPTIONS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [environment, setEnvironment] = useState<string | null>(null);

  // The regenerated wordings, fetched SECOND and held apart from the decisions on purpose: a Vertex
  // outage must empty this and leave every card, filter and statistic on the page intact.
  const [messages, setMessages] = useState<ReplayedMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [messagesError, setMessagesError] = useState<string | null>(null);

  // Which deployment this is, e.g. " (dev)" — empty in prd. dev and prd serve the same page on
  // near-identical hosts, so nothing on it says which numbers you are reading. Fetched on its own,
  // outside the filter effect: it never changes, and it must still show up when the decision log
  // below it is unreadable. A failed fetch reads as prd rather than breaking the page.
  useEffect(() => {
    void fetchConfig()
      .then((c) => setEnvironment(c.environment))
      .catch(() => undefined);
  }, []);

  const envSuffix = environment ? ` (${environment})` : '';

  useEffect(() => {
    document.title = `Tilbakeblikk${envSuffix}`;
  }, [envSuffix]);

  // A range the API would refuse (400), caught here so the refusal reads as a hint next to the
  // control rather than as a red error box three requests later. MIDNIGHT at the end is the
  // whole-day default and is never inverted, whatever the start is.
  const rangeInvalid = toTime !== MIDNIGHT && toTime <= fromTime;

  useEffect(() => {
    // `cancelled` is the stale-response guard, the same shape avviksvarsel's VarselStatus uses.
    // Filters stay enabled while a request is in flight, so two changes in quick succession leave
    // two request chains racing; without this the slower, older one wins and the page shows results
    // for a filter the user already moved off. On this page that is not just untidy — a stale list
    // beside a fresh summary makes the two disagree about the same window, and a stale WORDING
    // would be quoted under another train entirely.
    let cancelled = false;
    // Set when a stale selection is being cleared below: the state change re-runs this effect
    // immediately, so the spinner must stay up rather than blink off between the two passes.
    let rerunPending = false;

    // Wordings belong to the window and filters that produced them. Dropped before anything is
    // fetched, so a card can never carry the previous filter's prose while its own figures refresh.
    setMessages([]);
    setMessagesError(null);

    if (rangeInvalid) {
      setError('«Til kl.» må være seinere enn «Fra kl.». 00:00 i til-feltet betyr resten av døgnet.');
      setDecisions([]);
      setSummary(null);
      setLoading(false);
      return;
    }

    // Not named `window`: shadowing the DOM global inside an effect is a trap waiting for the first
    // line of code here that needs the real one.
    const dateWindow = { day, fromTime, toTime };
    // Two independent API flags: `resolved_only` is the judged population, `verdict` is one slice
    // within it. Sending both is not redundant — it is simply a verdict filter, which the API
    // already narrows to judged rows anyway.
    const filters: Filters = {
      ...dateWindow,
      operator: operator || undefined,
      line: line || undefined,
      fromPlace: fromPlace || undefined,
      toPlace: toPlace || undefined,
      verdict: utfall || undefined,
      alertedOnly,
      resolvedOnly,
      sxOnly,
    };
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        // The dropdown vocabulary FIRST, and awaited rather than raced with the data. It is also the
        // API's allow-list: a value it does not know is a 400, not an empty page, so narrowing the
        // window off a selected line (or place) has to clear that selection before anything asks for
        // rows with it. Fetched in parallel, the 400 would arrive first and flash an error box on a
        // filter change the user never made a mistake with. Server-side it is one cached query per
        // window shared by every filter combination, so the extra hop is a warm 200 in practice.
        const opts = await fetchFilterOptions(dateWindow);
        if (cancelled) return;
        setOptions(opts);

        // Reconcile, then bail: clearing a selection re-enters this effect with it gone.
        const gone = [
          [operator, opts.operators.includes(operator), setOperator],
          [line, opts.lines.includes(line), setLine],
          [fromPlace, opts.from_places.some((pl) => pl.ref === fromPlace), setFromPlace],
          [toPlace, opts.to_places.some((pl) => pl.ref === toPlace), setToPlace],
        ] as const;
        const stale = gone.filter(([value, present]) => value && !present);
        if (stale.length) {
          rerunPending = true;
          stale.forEach(([, , clear]) => clear(''));
          return;
        }

        // The summary describes the window as a whole and the list is filtered, so they are fetched
        // independently — a verdict filter must not be able to make precision read 100 %.
        const [d, s] = await Promise.all([fetchDecisions(filters), fetchSummary(filters)]);
        if (cancelled) return;
        setDecisions(d.decisions);
        setSummary(s);

        // The RESOLVED window back into the two inputs. `step={900}` keeps the picker on the grid,
        // but a typed 14:07 still reaches the API and is silently floored to 14:00 — and a control
        // claiming a minute the data does not cover is the misreport the whole quantization story
        // exists to avoid. Setting an identical string is a no-op in React, so this re-runs the
        // effect only when the server actually moved a boundary, and it converges: a quantized time
        // quantizes to itself.
        setFromTime(timeInputValue(d.from_time));
        setToTime(timeInputValue(d.to_time));
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          // Drop what is on screen. The rows that failed to refresh answer the PREVIOUS filter, and
          // left under the error box they read as the current one.
          setDecisions([]);
          setSummary(null);
        }
        return;
      } finally {
        if (!cancelled && !rerunPending) setLoading(false);
      }

      // Wordings second, and never in the same try: /messages is the only endpoint here that calls
      // Vertex, and it must be able to fail on its own. The cards are already on screen by now.
      if (cancelled) return;
      setMessagesLoading(true);
      try {
        const m = await fetchMessages(filters);
        if (!cancelled) setMessages(m.messages);
      } catch (e) {
        if (!cancelled) setMessagesError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setMessagesLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    day,
    fromTime,
    toTime,
    rangeInvalid,
    operator,
    line,
    fromPlace,
    toPlace,
    utfall,
    resolvedOnly,
    alertedOnly,
    sxOnly,
  ]);

  // Position first, content as the check — see alignMessages. Null while nothing has been fetched,
  // which is what makes the card's `message` prop undefined and leaves the message block off
  // entirely rather than rendering an empty one.
  const aligned = messages.length ? alignMessages(decisions, messages) : null;
  // Subjects a bound kept us from regenerating this round. Worth counting on the page: a
  // half-filled list otherwise reads as a broken backend rather than as a budget, and the fix
  // (reload — the cache is consulted before the budget, so the next round starts further down) is
  // not something a reader would guess.
  const boundedOut = messages.filter((m) => isBounded(m.status)).length;

  return (
    <main className="tb-page">
      <header className="tb-header">
        <Heading1>Tilbakeblikk{envSuffix}</Heading1>
        <Paragraph className="tb-intro">
          Hva ville dagens prompt skrevet om avvikene vi allerede har vurdert? Siden plukker et
          vindu av virkelige varselsykluser — én dag, et klokkeslettintervall — og kjører det lagrede
          skrivegrunnlaget gjennom prompten som gjelder nå. Ingenting sendes til noen; kortene viser
          samme avgjørelser og samme fasit som <span className="tb-nowrap">/historikk</span>, men med
          en formulering som er skrevet på nytt.
        </Paragraph>
        {/* R9, on the page and not in a tooltip. Without it, a reader who reloads and sees the same
            text concludes the output is deterministic — it is not; it is cached. And two rows that
            word themselves differently look like evidence about the prompt when they may be nothing
            but the decoder. */}
        <Paragraph className="tb-note">
          <strong>Én formulering er ett enkelt trekk.</strong> Hver tekst under er skrevet én gang av
          en ikke-deterministisk dekoder og hurtiglagret per rad og prompt-versjon. Laster du siden
          på nytt og får samme tekst, er det hurtigbufferen — ikke et bevis på at modellen er
          forutsigbar. En forskjell mellom to kort, eller mellom to prompt-versjoner, kan like gjerne
          være tilfeldig variasjon som en effekt av prompten. Siden skiller ikke de to, og bør ikke
          leses som om den gjorde det.
        </Paragraph>
      </header>

      <div className="tb-filters">
        {/* The window, on a line of its own: one day and a time range, all Europe/Oslo, and all
            three captioned — so unlike /historikk's button group this control needs no phantom
            caption to sit on the selects' midline.

            `step={TIME_STEP_S}` is the load-bearing attribute here: it makes the picker offer
            exactly the 15-minute boundaries the API honours, instead of minutes it would silently
            snap. */}
        <div className="tb-window">
          <label className="tb-select">
            <span>Dag</span>
            {/* Falling back to today when the field is cleared rather than allowing an empty value:
                an omitted `day` means today to the API, and a blank box above today's data is a
                control that misreports itself. */}
            <input
              type="date"
              value={day}
              min={osloDaysAgo(MAX_WINDOW_DAYS)}
              max={osloToday()}
              onChange={(e) => setDay(e.target.value || osloToday())}
            />
          </label>
          <label className="tb-select">
            <span>Fra kl.</span>
            <input
              type="time"
              step={TIME_STEP_S}
              value={fromTime}
              onChange={(e) => setFromTime(e.target.value || MIDNIGHT)}
            />
          </label>
          <label className="tb-select">
            <span>Til kl.</span>
            <input
              type="time"
              step={TIME_STEP_S}
              value={toTime}
              onChange={(e) => setToTime(e.target.value || MIDNIGHT)}
            />
          </label>
          <span className="tb-window-hint">
            Vinduet gjelder når avgjørelsen ble tatt, ikke når toget kjørte. Intervallet er
            halvåpent, og <strong>00:00 i «Til kl.» betyr resten av døgnet</strong>.
          </span>
        </div>

        <label className="tb-select">
          <span>Utfall</span>
          <select value={utfall} onChange={(e) => setUtfall(e.target.value as UtfallFilter)}>
            {UTFALL_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>

        {/* The four data-driven dropdowns. Every one of them is populated from
            /tilbakeblikk-api/filters over the whole window — never from the decisions on screen,
            which are capped and ordered by departure, so a line that only ran early in the window
            would drop out of its own filter and a selection would shrink the list it came from to
            just itself. "Alle" is always first, and always reachable. */}
        <label className="tb-select">
          <span>Operatør</span>
          <select value={operator} onChange={(e) => setOperator(e.target.value)}>
            <option value="">Alle</option>
            {options.operators.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </label>

        {/* Value is the full ref, label the short code — the same reduction the cards use, so the
            dropdown and the pill on a row name a line alike. Two codespaces can share a short code,
            which is why the ref and not the label is what travels to the API. */}
        <label className="tb-select">
          <span>Linje</span>
          <select value={line} onChange={(e) => setLine(e.target.value)}>
            <option value="">Alle</option>
            {options.lines.map((l) => (
              <option key={l} value={l}>
                {lineCode(l)}
              </option>
            ))}
          </select>
        </label>

        {/* Fra/Til are the REGISTERED journey's endpoints, not the situation's geography: they
            narrow to the decisions taken on journeys somebody registered between those two places.
            The value is the NSR ref, so a stop the server could not name still filters correctly —
            it just shows as its ref. */}
        <label className="tb-select tb-select--wide">
          <span>Fra</span>
          <select value={fromPlace} onChange={(e) => setFromPlace(e.target.value)}>
            <option value="">Alle</option>
            {options.from_places.map((pl) => (
              <option key={pl.ref} value={pl.ref}>
                {pl.name ?? pl.ref}
              </option>
            ))}
          </select>
        </label>

        <label className="tb-select tb-select--wide">
          <span>Til</span>
          <select value={toPlace} onChange={(e) => setToPlace(e.target.value)}>
            <option value="">Alle</option>
            {options.to_places.map((pl) => (
              <option key={pl.ref} value={pl.ref}>
                {pl.name ?? pl.ref}
              </option>
            ))}
          </select>
        </label>

        {/* In this order, because each one narrows a different axis: is there an answer yet, did
            we speak, was there a message to speak from. They sit side by side on a line of their
            own — see `.tb-checks` — because at the end of a row of dropdowns they read as three
            more alternatives to pick between rather than as three switches that compose. */}
        <div className="tb-checks" role="group" aria-label="Avgrensninger">
          <label
            className="tb-check"
            title="Bare avgjørelser der utfallet er kjent — vi vet hvordan det faktisk gikk. Skjuler dem som venter på fasit og dem vi aldri fant igjen i sanntidsdata. I et vindu fra i dag er det nesten alt."
          >
            <input
              type="checkbox"
              checked={resolvedOnly}
              onChange={(e) => setResolvedOnly(e.target.checked)}
            />
            <span>Bare med fasit</span>
          </label>

          <label
            className="tb-check"
            title="Bare avgjørelser som krysset terskelen inne i vinduet og ble til et varsel. Skjuler dem vi valgte å tie om — som er de eneste sporene en bom etterlater seg, og som aldri har en formulering å spille av."
          >
            <input
              type="checkbox"
              checked={alertedOnly}
              onChange={(e) => setAlertedOnly(e.target.checked)}
            />
            <span>Bare der vi varslet</span>
          </label>

          <label
            className="tb-check"
            title="Skjuler avgjørelser der ingen avviksmelding var publisert — dem vi selv har laget et situasjonsnummer for (KIX:), både de som er vurdert fra sanntidsdata alene og innstillinger videreformidlet fra ET."
          >
            <input type="checkbox" checked={sxOnly} onChange={(e) => setSxOnly(e.target.checked)} />
            <span>Bare med SIRI-SX-melding</span>
          </label>
        </div>
      </div>

      {error ? (
        <SmallAlertBox variant="negative">
          {error}
          <br />
          Ingen avgjørelser er logget ennå hvis dette er et ferskt miljø — loggen fylles av
          poller-syklusen, og fasit hentes først natten etter.
        </SmallAlertBox>
      ) : null}

      {summary && !error ? <SummaryStrip summary={summary} /> : null}

      {/* The wordings failed, the cards did not. Its own notice above the list, and deliberately
          not `variant="negative"`: everything the page shows below is still correct, and only the
          regenerated half is missing. */}
      {messagesError ? (
        <SmallAlertBox variant="warning">
          Fikk ikke hentet formuleringene: {messagesError}
          <br />
          Kortene under står uendret — avgjørelser og formuleringer hentes fra hvert sitt endepunkt,
          nettopp for at et problem med modellen ikke skal ta med seg resten av siden.
        </SmallAlertBox>
      ) : null}

      {loading ? (
        <div className="tb-loading">
          <Loader>Henter avgjørelser …</Loader>
        </div>
      ) : null}

      {!loading && !error && decisions.length === 0 ? (
        <SmallAlertBox variant="info">Ingen avgjørelser i dette vinduet.</SmallAlertBox>
      ) : null}

      {!loading && !error && decisions.length > 0 ? (
        <div className="tb-replay-status">
          {messagesLoading
            ? 'Skriver formuleringer på nytt …'
            : boundedOut > 0
              ? `${boundedOut} av ${messages.length} kort venter på en senere runde — avspilling har ` +
                'en ramme per forespørsel og per minutt. Last siden på nytt for å fylle flere; ' +
                'tekstene som allerede er skrevet, hentes fra hurtigbufferen.'
              : null}
        </div>
      ) : null}

      {/* Dimmed while a fetch is in flight: the list is deliberately left up rather than blanked
          (a filter change should not make the page jump), but a cold query runs ~2 s and the rows
          still standing are the OLD filter's. Without the cue, ticking a filter looks like it did
          nothing at all. */}
      <section className={`tb-list ${loading ? 'is-stale' : ''}`}>
        {/* Keyed on position, not on content, because nothing served here identifies a decision
            uniquely. The row's real grain is (journey_digest, operator, jkey, call_date,
            stop_order, situation_number), and the first of those is exactly what the API redacts —
            so two journeys registered over the same train, or one situation matched at two stops,
            are distinct decisions that look identical from out here. Measured 2026-08-11 in prd on
            /historikk: 200 rows shared 152 content keys. Duplicate keys are not cosmetic — React
            cannot pair old children with new ones, so a list that SHRINKS leaves the orphans in the
            DOM: ticking "bare der vi varslet" left the filtered-out cards on screen and grew the
            list to 248. The index is safe because the list is replaced wholesale from a
            server-ordered response and DecisionCard holds no state. */}
        {decisions.map((d, i) => (
          <DecisionCard key={i} decision={d} message={aligned ? aligned[i] : undefined} />
        ))}
      </section>
    </main>
  );
}
