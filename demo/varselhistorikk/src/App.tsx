import { useEffect, useState } from 'react';
import { SmallAlertBox } from '@entur/alert';
import { Loader } from '@entur/loader';
import { Heading1, Paragraph } from '@entur/typography';
import DecisionCard from './components/DecisionCard';
import SummaryStrip from './components/SummaryStrip';
import { fetchConfig, fetchDecisions, fetchSummary } from './api';
import type { Decision, Filters, Summary, Verdict } from './api';
import { VERDICT_LABEL, isoDaysAgo } from './format';

/** What the "Utfall" dropdown can be set to: no filter, or one of the four verdicts. It maps
 *  one-to-one onto the API's `verdict`, which is exactly the four values the log records.
 *
 *  "Alle med fasit" used to sit in here as a fifth option and is now its own checkbox: it is not a
 *  verdict but a widening of all four, and a dropdown makes every entry look mutually exclusive
 *  with every other. As a checkbox it also composes visibly with the other two. */
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

const WINDOW_OPTIONS = [
  { days: 7, label: '7 dager' },
  { days: 14, label: '14 dager' },
  { days: 30, label: '30 dager' },
];

export default function App() {
  const [days, setDays] = useState(14);
  const [utfall, setUtfall] = useState<UtfallFilter>('');
  // All three narrowings default OFF: the page opens on every decision it has, including the ones
  // still waiting for a verdict and the ones we stayed quiet on. Each of them is a population, and
  // a page that hides one by default measures something narrower than it says it does.
  const [resolvedOnly, setResolvedOnly] = useState(false);
  const [alertedOnly, setAlertedOnly] = useState(false);
  const [sxOnly, setSxOnly] = useState(false);
  const [operator, setOperator] = useState('');

  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [operators, setOperators] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [environment, setEnvironment] = useState<string | null>(null);

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
    document.title = `Varselhistorikk${envSuffix}`;
  }, [envSuffix]);

  useEffect(() => {
    // `cancelled` is the stale-response guard, the same shape avviksvarsel's VarselStatus uses.
    // Filters stay enabled while a request is in flight, so two changes in quick succession leave
    // two Promise.all pairs racing; without this the slower, older one wins and the page shows
    // results for a filter the user already moved off. On this page that is not just untidy — a
    // stale list beside a fresh summary makes the two disagree about the same window.
    let cancelled = false;
    // Two independent API flags now that "med fasit" is its own checkbox: `resolved_only` is the
    // judged population, `verdict` is one slice within it. Sending both is not redundant — it is
    // simply a verdict filter, which the API already narrows to judged rows anyway.
    const filters: Filters = {
      fromDate: isoDaysAgo(days),
      operator: operator || undefined,
      verdict: utfall || undefined,
      alertedOnly,
      resolvedOnly,
      sxOnly,
    };
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        // The summary describes the window as a whole and the list is filtered, so they are fetched
        // independently — a verdict filter must not be able to make precision read 100 %.
        const [d, s] = await Promise.all([fetchDecisions(filters), fetchSummary(filters)]);
        if (cancelled) return;
        setDecisions(d.decisions);
        setSummary(s);
        // Only an unfiltered-by-operator load sees the whole population. Deriving the dropdown from
        // the currently shown rows would let it shrink to just the selected operator, stranding the
        // user with no way back.
        if (!operator) {
          setOperators([...new Set(d.decisions.map((x) => x.operator).filter(Boolean))] as string[]);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          // Drop what is on screen. The rows that failed to refresh answer the PREVIOUS filter, and
          // left under the error box they read as the current one — the same misreading the stale
          // cards below caused, arrived at from the other direction.
          setDecisions([]);
          setSummary(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [days, operator, utfall, resolvedOnly, alertedOnly, sxOnly]);

  return (
    <main className="vh-page">
      <header className="vh-header">
        <Heading1>Varselhistorikk{envSuffix}</Heading1>
        <Paragraph className="vh-intro">
          Hver avgjørelse Avviksvarsel har tatt om en registrert reise — også de vi valgte å tie om
          — og hvordan det faktisk gikk. Avgjørelser uten varsel er tatt med med vilje: uten dem
          kan vi bare måle hvor ofte vi tok feil når vi varslet, aldri hvor mye vi gikk glipp av.
        </Paragraph>
      </header>

      <div className="vh-filters">
        {/* Wrapped, because what decides where a control sits on this row is the caption above it,
            and this one has none. `.vh-window` gives it the same empty caption line the selects
            get from "Utfall"/"Operatør", so the button box lands on the selects' midline rather
            than riding ~10 px above them. */}
        <div className="vh-window">
          <div className="vh-filter-group" role="group" aria-label="Tidsvindu">
            {WINDOW_OPTIONS.map((w) => (
              <button
                key={w.days}
                type="button"
                className={`vh-toggle ${days === w.days ? 'is-active' : ''}`}
                onClick={() => setDays(w.days)}
              >
                {w.label}
              </button>
            ))}
          </div>
        </div>

        <label className="vh-select">
          <span>Utfall</span>
          <select value={utfall} onChange={(e) => setUtfall(e.target.value as UtfallFilter)}>
            {UTFALL_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>

        <label className="vh-select">
          <span>Operatør</span>
          <select value={operator} onChange={(e) => setOperator(e.target.value)}>
            <option value="">Alle</option>
            {operators.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </label>

        {/* Stacked, and in this order: each one narrows a different axis — is there an answer
            yet, did we speak, was there a message to speak from — and side by side they read as
            alternatives to each other rather than as three switches that compose. */}
        <div className="vh-checks" role="group" aria-label="Avgrensninger">
          {/* Every decision the nightly job has an answer for, dropping the ones still waiting and
              the ones whose train was never found. Wider than picking a verdict, which narrows
              within exactly this population — which is why it was a poor fit for the dropdown it
              used to live in. */}
          <label
            className="vh-check"
            title="Bare avgjørelser der utfallet er kjent — vi vet hvordan det faktisk gikk. Skjuler dem som venter på fasit og dem vi aldri fant igjen i sanntidsdata."
          >
            <input
              type="checkbox"
              checked={resolvedOnly}
              onChange={(e) => setResolvedOnly(e.target.checked)}
            />
            <span>Bare med fasit</span>
          </label>

          <label
            className="vh-check"
            title="Bare avgjørelser som krysset terskelen og ble til et varsel. Skjuler dem vi valgte å tie om — som er de eneste sporene en bom etterlater seg."
          >
            <input
              type="checkbox"
              checked={alertedOnly}
              onChange={(e) => setAlertedOnly(e.target.checked)}
            />
            <span>Bare der vi varslet</span>
          </label>

          {/* The rows tagged "Sanntidsdata alene" are decisions about trains no operator published
              anything about, and they outnumber the rest several times over — every planned call in
              the window is one. On, when the question is about the alerts an operator's own message
              drove. */}
          <label
            className="vh-check"
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

      {loading ? (
        <div className="vh-loading">
          <Loader>Henter varselhistorikk …</Loader>
        </div>
      ) : null}

      {!loading && !error && decisions.length === 0 ? (
        <SmallAlertBox variant="info">
          Ingen avgjørelser i dette vinduet.
        </SmallAlertBox>
      ) : null}

      {/* Dimmed while a fetch is in flight: the list is deliberately left up rather than blanked
          (a filter change should not make the page jump), but a cold query runs ~2 s and the rows
          still standing are the OLD filter's. Without the cue, ticking a filter looks like it did
          nothing at all. */}
      <section className={`vh-list ${loading ? 'is-stale' : ''}`}>
        {/* Keyed on position, not on content, because nothing served here identifies a decision
            uniquely. The row's real grain is (journey_digest, operator, jkey, call_date,
            stop_order, situation_number), and the first two of those are exactly what the API
            redacts — so two journeys registered over the same train, or one situation matched at
            two stops, are distinct decisions that look identical from out here. Measured
            2026-08-11 in prd: 200 rows shared 152 content keys. Duplicate keys are not cosmetic
            here — React cannot pair old children with new ones, so a list that SHRINKS leaves the
            orphans in the DOM: ticking "bare der vi varslet" left the filtered-out "Varslet ikke"
            cards on screen and grew the list to 248. The index is safe because the list is
            replaced wholesale from a server-ordered response and DecisionCard holds no state. */}
        {decisions.map((d, i) => (
          <DecisionCard key={i} decision={d} />
        ))}
      </section>
    </main>
  );
}
