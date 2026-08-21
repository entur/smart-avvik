import { useEffect, useState } from 'react';
import {
  fetchConfig,
  fetchJourneys,
  fetchMeta,
  type DemoHighlight,
  type DemoJourneysResponse,
  type DemoMeta,
} from './api';
import { CANCEL_LIKELY, fmtDate, fmtDateTime, fromLocalInput, toLocalInput } from './format';
import HitSummary from './HitSummary';
import JourneyCard from './JourneyCard';
import StopCombobox from './StopCombobox';

export default function App() {
  const [meta, setMeta] = useState<DemoMeta | null>(null);
  const [metaError, setMetaError] = useState<string | null>(null);
  const [fromStop, setFromStop] = useState('');
  const [toStop, setToStop] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [onlyFlagged, setOnlyFlagged] = useState(false);
  const [result, setResult] = useState<DemoJourneysResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [environment, setEnvironment] = useState<string | null>(null);

  // Which deployment this is, e.g. " (dev)" — empty in prd. All three deployments serve this page
  // from the same image on near-identical hosts, and a partner being shown dev numbers as if they
  // were prod is the failure this prevents. Fetched apart from the dataset metadata, which can fail
  // on its own; a failed config fetch reads as prd rather than breaking the page.
  const envSuffix = environment ? ` (${environment})` : '';

  useEffect(() => {
    document.title = `Reiseprediksjon under avvik${envSuffix} — demo`;
  }, [envSuffix]);

  useEffect(() => {
    fetchConfig().then((c) => setEnvironment(c.environment), () => undefined);
    fetchMeta().then(setMeta, (e) => setMetaError(String(e.message ?? e)));
    // Shareable links: /demo/?from=NSR:StopPlace:694&to=...&start=2026-04-14T19:30&end=...
    // (start/end in Oslo wall time, as in the inputs). Searches on load when complete.
    const p = new URLSearchParams(window.location.search);
    const from = p.get('from') ?? '';
    const to = p.get('to') ?? '';
    const startVal = p.get('start') ?? '';
    const endVal = p.get('end') ?? '';
    if (from) setFromStop(from);
    if (to) setToStop(to);
    if (startVal) setStart(startVal);
    if (endVal) setEnd(endVal);
    if (from && to && startVal && endVal) void search(from, to, startVal, endVal);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function search(from = fromStop, to = toStop, startVal = start, endVal = end) {
    if (!from || !to || !startVal || !endVal) return;
    setLoading(true);
    setError(null);
    const share = new URLSearchParams({ from, to, start: startVal, end: endVal });
    window.history.replaceState(null, '', `?${share}`);
    try {
      setResult(await fetchJourneys(from, to, fromLocalInput(startVal), fromLocalInput(endVal)));
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  function applyHighlight(h: DemoHighlight) {
    // Bracket the scenario: a little before the first affected departure, capped at four
    // hours so the result list stays presentable.
    const startMs = new Date(h.window_start).getTime() - 15 * 60_000;
    const endMs = Math.min(new Date(h.window_end).getTime(), startMs + 4 * 3600_000);
    const startVal = toLocalInput(new Date(startMs).toISOString());
    const endVal = toLocalInput(new Date(endMs).toISOString());
    setFromStop(h.from_stop);
    setToStop(h.to_stop);
    setStart(startVal);
    setEnd(endVal);
    void search(h.from_stop, h.to_stop, startVal, endVal);
  }

  const journeys = result
    ? onlyFlagged
      ? result.journeys.filter((j) => j.situation !== null)
      : result.journeys
    : [];

  return (
    <div className="page">
      <header className="masthead">
        <h1>Reiseprediksjon under avvik{envSuffix}</h1>
        <p>
          Hvor godt treffer prediksjonene for forsinkelser og innstillinger? Velg en strekning og
          et tidsrom fra evalueringsperioden, og sammenlign det modellene meldte — idet
          avviksmeldingen kom, 60 og 30 minutter før avgang — med det som faktisk skjedde.
        </p>
        {meta && (
          <p className="data-note">
            Datagrunnlag: tog (Vy/NSB, Go-Ahead) og buss {fmtDate(meta.window_start)} –{' '}
            {fmtDate(meta.window_end)} — modellenes holdout pluss de siste dagene (buss
            oppdateres daglig, til og med i går); alle tall er utenfor treningsdataene. Varsel
            når P(innstilt eller ≥ 10 min forsinket) krysser operatørens varslingsterskel
            (standard {Math.round(meta.alert_threshold * 100)} %; terskelen vises på hvert kort).
          </p>
        )}
      </header>

      {metaError && <div className="error">Kunne ikke laste demodata: {metaError}</div>}

      {meta && (
        <section className="controls">
          <div className="control scenario-control">
            <label htmlFor="scenario">Eksempelscenario</label>
            <select
              id="scenario"
              defaultValue=""
              onChange={(e) => {
                const h = meta.highlights[Number(e.target.value)];
                if (h) applyHighlight(h);
              }}
            >
              <option value="" disabled>
                Velg en hendelse …
              </option>
              {meta.highlights.map((h, i) => (
                <option key={h.situation_number} value={i}>
                  {fmtDateTime(h.window_start)} — [{h.operator}] {h.summary ?? h.situation_number} (
                  {h.n_cancelled} innstilte, {h.n_delayed10} forsinkede)
                </option>
              ))}
            </select>
          </div>
          <form
            className="query-row"
            onSubmit={(e) => {
              e.preventDefault();
              void search();
            }}
          >
            <div className="control">
              <label htmlFor="from">Fra</label>
              <StopCombobox
                id="from"
                stops={meta.stops}
                value={fromStop}
                onChange={setFromStop}
                placeholder="Søk etter stopp …"
              />
            </div>
            <button
              type="button"
              className="swap"
              title="Bytt om fra/til"
              onClick={() => {
                setFromStop(toStop);
                setToStop(fromStop);
              }}
            >
              ⇄
            </button>
            <div className="control">
              <label htmlFor="to">Til</label>
              <StopCombobox
                id="to"
                stops={meta.stops}
                value={toStop}
                onChange={setToStop}
                placeholder="Søk etter stopp …"
              />
            </div>
            <div className="control">
              <label htmlFor="start">Avgang fra</label>
              <input
                id="start"
                type="datetime-local"
                value={start}
                min={`${meta.window_start}T00:00`}
                max={`${meta.window_end}T23:59`}
                onChange={(e) => setStart(e.target.value)}
                required
              />
            </div>
            <div className="control">
              <label htmlFor="end">Avgang til</label>
              <input
                id="end"
                type="datetime-local"
                value={end}
                min={`${meta.window_start}T00:00`}
                max={`${meta.window_end}T23:59`}
                onChange={(e) => setEnd(e.target.value)}
                required
              />
            </div>
            <button type="submit" className="primary" disabled={loading}>
              {loading ? 'Søker …' : 'Finn reiser'}
            </button>
          </form>
          <label className="toggle">
            <input
              type="checkbox"
              checked={onlyFlagged}
              onChange={(e) => setOnlyFlagged(e.target.checked)}
            />
            Vis bare reiser med SX-melding
          </label>
        </section>
      )}

      {error && <div className="error">{error}</div>}

      {result && !error && (
        <section className="results">
          <HitSummary journeys={result.journeys} />
          {journeys.length === 0 ? (
            <p className="empty">Ingen reiser i valgt tidsrom.</p>
          ) : (
            journeys.map((j) => (
              // Bus jkeys repeat per operating day, so the ref alone is not unique in a
              // multi-day window — key on ref + departure time.
              <JourneyCard key={`${j.journey_ref}|${j.departure.scheduled_time}`} journey={j} />
            ))
          )}
          <footer className="legend">
            <span>
              <span className="verdict verdict-hit">✓</span> treff (varslet, ble trøbbel)
            </span>
            <span>
              <span className="verdict verdict-ok">✓</span> riktig ro (ikke varslet, gikk fint)
            </span>
            <span>
              <span className="verdict verdict-miss">✕</span> bom (ikke varslet, ble trøbbel)
            </span>
            <span>
              <span className="verdict verdict-false">!</span> falsk alarm (varslet, gikk fint)
            </span>
            <span>
              <span className="varsel-driver">42 %</span> tallet varselet nevnte
            </span>
            <span className="legend-note">
              Forsinkelse vises som typisk–verstefall (p50–p90). «Trøbbel» = innstilt eller ≥ 10
              min forsinket. Varselteksten nevner bare den ene av de to: innstillings­sannsynligheten
              når den er over {Math.round(CANCEL_LIKELY * 100)} %, ellers forsinkelsen.
            </span>
          </footer>
        </section>
      )}
    </div>
  );
}
