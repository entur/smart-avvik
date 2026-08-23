import { useCallback, useEffect, useRef, useState } from 'react';
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
  osloTomorrow,
  sxSummaryLabel,
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

/** The vocabulary the dropdowns start with: nothing, until /meldinger-api/filters answers. Held
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
  sx_summaries: [],
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
  // parameter. The end-time input displays that sentinel as 23:59, because the API rejects a
  // literal 00:00 end. See MIDNIGHT in api.ts.
  const [day, setDay] = useState(osloDaysAgo(1));
  const [fromTime, setFromTime] = useState(MIDNIGHT);
  const [toTime, setToTime] = useState(MIDNIGHT);

  const [utfall, setUtfall] = useState<UtfallFilter>('');
  // Open on decisions where a traveller was actually notified. The replayed wording is not the
  // wording they received, but this is the most useful population for reviewing today's prompt.
  const [resolvedOnly, setResolvedOnly] = useState(false);
  const [alertedOnly, setAlertedOnly] = useState(false);
  const [pushedOnly, setPushedOnly] = useState(true);
  const [sxOnly, setSxOnly] = useState(false);
  const [operator, setOperator] = useState('');
  // The three data-driven dropdowns. Their values are REFS — a full line ref, an NSR StopPlace ref —
  // because that is what the decision log stores and what the API filters on; the readable name is
  // only ever the label. So an unnamed stop still filters correctly, it just shows as its ref.
  const [line, setLine] = useState('');
  const [fromPlace, setFromPlace] = useState('');
  const [toPlace, setToPlace] = useState('');
  // The SIRI-SX summary, matched exactly. The value IS the text — unlike the three above it there is
  // no ref behind it, which is why the server allow-lists it against a capped vocabulary rather than
  // accepting whatever arrives.
  const [sxSummary, setSxSummary] = useState('');

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
    document.title = `Meldinger${envSuffix}`;
  }, [envSuffix]);

  // A range the API would refuse (400), caught here so the refusal reads as a hint next to the
  // control rather than as a red error box three requests later. MIDNIGHT at the end is the
  // whole-day default and is never inverted, whatever the start is.
  const rangeInvalid = toTime !== MIDNIGHT && toTime <= fromTime;

  // Which pass over the wordings is the current one. Every pass carries the number it was started
  // with and writes nothing once it is no longer current — see loadMessages. A plain `cancelled`
  // flag cannot do this job any more: the effect's cleanup only knows about the effect's own
  // fetch, and there is now a second way to start one that outlives no render at all.
  const messagesRound = useRef(0);
  // The filters that produced the decisions ON SCREEN, latched when they land. What the button
  // below must ask for is that set — not whatever the controls happen to say when it is clicked —
  // because /decisions and /messages are aligned by position, and two different filter sets are
  // two different subject lists. A ref rather than state: nothing renders from it.
  const activeFilters = useRef<Filters | null>(null);

  /** Fetch the regenerated wordings for `filters`, and show them if `round` is still the current one.
   *
   *  Shared by the filter effect and by the "hent flere" button, which is why it takes its
   *  filters as an argument instead of reading them off state: the button asks for exactly the
   *  filters behind the cards it stands next to. A filter change while a manual round is in flight
   *  bumps the round, and the reply is then dropped rather than landing one window's prose under
   *  another window's cards.
   *
   *  The previous round's wordings are deliberately left standing while a later one runs. /messages
   *  returns the WHOLE subject list every time and consults its wording cache before its budget, so
   *  a later round re-serves everything already written for free and spends the allowance further
   *  down the list — it is a superset, and blanking first would only make the page flash. */
  const loadMessages = useCallback(async (filters: Filters, round: number) => {
    setMessagesLoading(true);
    setMessagesError(null);
    try {
      const m = await fetchMessages(filters);
      if (messagesRound.current === round) setMessages(m.messages);
    } catch (e) {
      if (messagesRound.current === round) {
        setMessagesError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      if (messagesRound.current === round) setMessagesLoading(false);
    }
  }, []);

  /** Spend another request's allowance on the subjects a bound stopped last round.
   *
   *  Not a page reload, which is what the notice used to advise: a reload re-runs the window's
   *  BigQuery queries and re-mounts the page to arrive at the same cards, when the only thing that
   *  needed asking again was /messages. The wording cache is process-lifetime and consulted before
   *  the budget, so what this actually buys is the next ~1 request's worth of Vertex calls, spent
   *  on rows that have none yet. */
  const loadNextRound = () => {
    const filters = activeFilters.current;
    if (!filters || messagesLoading) return;
    void loadMessages(filters, ++messagesRound.current);
  };

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
    // Bumping the round here — before any early return below, and on the same tick the list is
    // dropped — is what abandons a manual round already in flight for the filters we just left.
    const round = ++messagesRound.current;
    activeFilters.current = null;
    setMessages([]);
    setMessagesError(null);
    // Cleared here rather than left to the abandoned round's `finally`, which no longer runs: that
    // round is no longer current, so it writes nothing at all — including "done".
    setMessagesLoading(false);

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
      sxSummary: sxSummary || undefined,
      verdict: utfall || undefined,
      alertedOnly,
      pushedOnly,
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
          // Reconciled like the rest, and it needs it MORE than the rest: this vocabulary is capped
          // at 30, so a selection can fall out of it without the window losing the decisions that
          // carry it — a narrowing that merely reshuffles which summaries are the most common is
          // enough. Left standing it would be a 400 rather than a filter that quietly did nothing.
          [sxSummary, opts.sx_summaries.some((o) => o.value === sxSummary), setSxSummary],
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
        const resolvedToTime = timeInputValue(d.to_time);
        setToTime(resolvedToTime);
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
      // Latched only now, beside the cards these filters produced, so the button can never ask for
      // a set the list on screen was not built from.
      activeFilters.current = filters;
      await loadMessages(filters, round);
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
    sxSummary,
    utfall,
    resolvedOnly,
    alertedOnly,
    pushedOnly,
    sxOnly,
  ]);

  // Position first, content as the check — see alignMessages. Null while nothing has been fetched,
  // which is what makes the card's `message` prop undefined and leaves the message block off
  // entirely rather than rendering an empty one.
  const aligned = messages.length ? alignMessages(decisions, messages) : null;
  // Subjects a bound kept us from regenerating this round. Worth counting on the page: a
  // half-filled list otherwise reads as a broken backend rather than as a budget. It is also what
  // the "hent flere" button is gated on — nothing is bounded, nothing to fetch — and the
  // count is the only thing that says whether pressing it again is worth anything.
  const boundedOut = messages.filter((m) => isBounded(m.status)).length;
  // The hover text for the SIRI-SX control: the LABEL of the selected option, not the raw stored
  // value, so it reads as the row that was picked. Undefined when nothing is selected, so React
  // omits the attribute and the label's own explanatory tooltip shows through instead of being
  // suppressed by an empty one.
  const selectedSxLabel = sxSummary
    ? sxSummaryLabel(
        options.sx_summaries.find((o) => o.value === sxSummary) ?? {
          value: sxSummary,
          n_values: 1,
          no_sx: false,
        },
      )
    : undefined;

  return (
    <main className="tb-page">
      <header className="tb-header">
        <Heading1>Meldinger{envSuffix}</Heading1>
        <Paragraph className="tb-intro">
          Hva ville vi sendt ut for disse avgangene — skrevet av dagens prompt? Velg en dag og et
          klokkeslettintervall for <strong>avgangstid</strong>, så henter siden varslene som faktisk
          gikk ut for togene som kjørte da, og kjører det lagrede skrivegrunnlaget gjennom prompten
          som gjelder nå.
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
            <span>Avgangsdag</span>
            {/* Keep the default day visible when the native field is cleared. */}
            <input
              type="date"
              value={day}
              min={osloDaysAgo(MAX_WINDOW_DAYS)}
              max={osloTomorrow()}
              onChange={(e) => setDay(e.target.value || osloDaysAgo(1))}
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
              value={toTime === MIDNIGHT ? '23:59' : toTime}
              onChange={(e) => setToTime(e.target.value === '23:59' ? MIDNIGHT : e.target.value)}
            />
          </label>
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

        {/* The five data-driven dropdowns. Every one of them is populated from
            /meldinger-api/filters over the whole window — never from the decisions on screen,
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

        {/* The operator's own SIRI-SX summary, matched exactly. Ordered MOST COMMON FIRST by the
            server and rendered in that order: the reading worth having is "which disruption texts
            dominated this window", and alphabetical free-form prose answers nothing. Capped at 30
            server-side — the vocabulary is also the allow-list, and an uncapped dropdown of
            free-form text would let every distinct string mint a cache entry and a BigQuery query
            on a public endpoint.

            `title` on the label, because the control truncates: these run to a full sentence and a
            select cannot wrap. */}
        <label
          className="tb-select tb-select--wide"
          title="Filtrer på én avviksmelding fra operatøren — de 30 vanligste i vinduet, vanligst først. «Ingen» er ikke en melding: det er avgangene ingen operatør har publisert noe om, og kortene under den viser derfor ingen SIRI-SX-linje. «Bare med SIRI-SX-melding» skjuler hele den gruppa."
        >
          <span>SIRI-SX</span>
          {/* `title` on the SELECT, not just the label, and only once something is chosen. At 12rem
              the closed control truncates, and these texts collide late: "Stopper ekstra på
              Brakerøya og Lier" against "…på Lier og Brakerøya" differ at character 19, and the two
              "Planlegger buss for tog Oslo S–…" at 30. Hovering resolves which one is selected.
              `undefined` rather than '' when nothing is chosen, so React omits the attribute and the
              label's own explanatory tooltip shows through instead of being suppressed by an empty
              one. */}
          <select
            value={sxSummary}
            title={selectedSxLabel}
            onChange={(e) => setSxSummary(e.target.value)}
          >
            <option value="">Alle</option>
            {options.sx_summaries.map((o) => (
              // Value is the option's nominated stored text; label is what a reader should see. The
              // two differ only for the no-SX option, which stands for more than one stored text and
              // is expanded server-side. See sxSummaryLabel.
              <option key={o.value} value={o.value}>
                {sxSummaryLabel(o)}
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
            title="Bare avgjørelser som krysset operatørens terskel. Skjuler dem vi valgte å tie om — som er de eneste sporene en bom etterlater seg, og som aldri har en formulering å spille av. Merk at å krysse terskelen ikke er det samme som at noen ble varslet: bruk «Bare der vi varslet» for det."
          >
            <input
              type="checkbox"
              checked={alertedOnly}
              onChange={(e) => setAlertedOnly(e.target.checked)}
            />
            <span>Bare over terskel</span>
          </label>

          <label
            className="tb-check"
            title="Bare avgjørelser en reisende faktisk fikk et varsel om. Strengere enn «Bare over terskel»: en varselverdig avgjørelse når ingen når en annen melding om samme tog vant sammenslåingen, eller når varselet for den situasjonen allerede var sendt i en tidligere syklus."
          >
            <input
              type="checkbox"
              checked={pushedOnly}
              onChange={(e) => setPushedOnly(e.target.checked)}
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
          {/* The FIRST round has nothing on screen to add to, so it gets the plain notice. A later
              one keeps the button standing — disabled, and saying what it is doing — rather than
              swapping it for a line of text and making the control the reader just pressed vanish
              from under their cursor. */}
          {messagesLoading && messages.length === 0 ? (
            <span>Skriver formuleringer på nytt …</span>
          ) : null}
          {boundedOut > 0 ? (
            <>
              <span>
                {boundedOut} av {messages.length} kort venter på en senere runde — avspilling har en
                ramme per forespørsel og en kvote per time og døgn. Hver runde fyller de neste;
                tekstene som allerede er skrevet, hentes fra hurtigbufferen og koster ingenting.
              </span>
              <button
                type="button"
                className="tb-replay-more"
                onClick={loadNextRound}
                disabled={messagesLoading}
                title="Henter formuleringene på nytt. Rader som allerede har en tekst kommer fra hurtigbufferen uten et modellkall, så rammen brukes lenger ned i lista."
              >
                {messagesLoading ? 'Henter flere …' : 'Hent flere'}
              </button>
            </>
          ) : null}
        </div>
      ) : null}

      {/* The drawing convention, stated ONCE. Every delivered wording below is drawn as the push it
          became, and a picture of a notification asserts "a phone showed this text" — true of the
          delivery and false of the words, which today's prompt wrote from the stored input rather
          than quoting what went out. Per card it would be the same sentence 200 times, which is
          how a caveat stops being read; here it qualifies the convention, where it belongs.

          Gated on `messages` as well as `decisions`: with the wording pass empty — a Vertex outage,
          which /messages is a separate endpoint precisely to survive — nothing on the page is drawn
          as a notification, and a caveat about a convention no row uses is just noise. */}
      {!loading && !error && decisions.length > 0 && messages.length > 0 ? (
        <p className="tb-caveat tb-caveat--above-list">
          Varslene under er tegnet slik de ser ut på telefonen. Ordlyden er{' '}
          <strong>skrevet på nytt av dagens prompt</strong> fra det lagrede skrivegrunnlaget — det
          er ikke teksten som faktisk gikk ut.
        </p>
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
