import { useEffect, useState } from 'react';
import { SmallAlertBox } from '@entur/alert';
import { Loader } from '@entur/loader';
import { Heading1, Paragraph } from '@entur/typography';
import DecisionCard from './components/DecisionCard';
import SummaryStrip from './components/SummaryStrip';
import { fetchConfig, fetchDecisions, fetchSummary } from './api';
import type { Decision, Filters, Summary, Verdict } from './api';
import { VERDICT_LABEL, isoDaysAgo } from './format';

// Built from VERDICT_LABEL rather than retyped, so the filter and the badges can never disagree
// about what a verdict is called.
const VERDICT_OPTIONS: Array<{ value: Verdict | ''; label: string }> = [
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
  const [verdict, setVerdict] = useState<Verdict | ''>('');
  const [alertedOnly, setAlertedOnly] = useState(false);
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
    const filters: Filters = {
      fromDate: isoDaysAgo(days),
      operator: operator || undefined,
      verdict: verdict || undefined,
      alertedOnly,
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
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [days, operator, verdict, alertedOnly]);

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

        <label className="vh-select">
          <span>Utfall</span>
          <select value={verdict} onChange={(e) => setVerdict(e.target.value as Verdict | '')}>
            {VERDICT_OPTIONS.map((o) => (
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

        <label className="vh-check">
          <input
            type="checkbox"
            checked={alertedOnly}
            onChange={(e) => setAlertedOnly(e.target.checked)}
          />
          <span>Bare der vi varslet</span>
        </label>
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

      <section className="vh-list">
        {decisions.map((d) => (
          <DecisionCard
            key={`${d.situation_number}-${d.line}-${d.call_date}-${d.departure_time}`}
            decision={d}
          />
        ))}
      </section>
    </main>
  );
}
