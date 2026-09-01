import { useEffect, useState } from 'react';
import { SmallAlertBox } from '@entur/alert';
import { Loader } from '@entur/loader';
import { Heading1, Heading3, Paragraph } from '@entur/typography';
import DayTable from './components/DayTable';
import HitTable from './components/HitTable';
import { fetchConfig, fetchDays } from './api';
import type { DayRow, StatsRow } from './api';
import { fmtDate } from './format';

/** The page's only control (KD4). A rush forecast is one product-wide prognosis over a fixed
 *  39-station geography, so there is no operator, line or from/to dimension to slice — /historikk's
 *  five dropdowns have nothing to be about here.
 *
 *  "Hele perioden" is a bounded 365 days server-side. The four values are also the API's
 *  allow-list: anything else is a 400 rather than one more response-cache key and one more
 *  BigQuery query on a public endpoint. */
const WINDOW_OPTIONS = [
  { days: 7, label: '7 dager' },
  { days: 14, label: '14 dager' },
  { days: 30, label: '30 dager' },
  { days: 365, label: 'Hele perioden' },
];

export default function App() {
  const [days, setDays] = useState(14);

  const [dayRows, setDayRows] = useState<DayRow[]>([]);
  const [stats, setStats] = useState<StatsRow[]>([]);
  const [recordingStarted, setRecordingStarted] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [environment, setEnvironment] = useState<string | null>(null);

  // Which deployment this is, e.g. " (dev)" — empty in prd. dev and prd serve the same page on
  // near-identical hosts, so nothing on it says which numbers you are reading. Fetched on its own,
  // outside the data effect: it never changes, and it must still show up when /days below it is
  // unreadable — which is exactly when "dev or prd?" matters most. A failed fetch reads as prd
  // rather than breaking the page.
  useEffect(() => {
    void fetchConfig()
      .then((c) => setEnvironment(c.environment))
      .catch(() => undefined);
  }, []);

  const envSuffix = environment ? ` (${environment})` : '';

  useEffect(() => {
    document.title = `Rushhistorikk${envSuffix}`;
  }, [envSuffix]);

  useEffect(() => {
    // The stale-response guard, the same shape /historikk uses. The picker stays enabled while a
    // request is in flight, so two clicks in quick succession leave two responses racing; without
    // this the slower, older one wins and the page shows a window the reader already moved off —
    // and the stats table would then describe a different population from the rows under it.
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        // One request: the rows and the stats come from one query on purpose, so the rates
        // describe exactly the population the list beside them shows.
        const res = await fetchDays(days);
        if (cancelled) return;
        setDayRows(res.days);
        setStats(res.stats);
        setRecordingStarted(res.recording_started);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          // Drop what is on screen. Rows and rates that failed to refresh answer the PREVIOUS
          // window, and left standing under an error box they read as the current one.
          setDayRows([]);
          setStats([]);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [days]);

  return (
    <main className="rh-page">
      <header className="rh-header">
        <Heading1>Rushhistorikk{envSuffix}</Heading1>
        <Paragraph className="rh-intro">
          Traff rushvarslene? Én rad per dag med de fire prognosene Rushvarsel utstedte, hva vi
          faktisk meldte til de reisende, og hva rushet endte med å bli. Både treffene og tausheten
          er tatt med: en runde som valgte å ikke varsle er det eneste sporet en bom etterlater seg.
        </Paragraph>
        {/* The five things a reader otherwise gets wrong, in prose rather than hidden in tooltips.
            Each one has a failure mode behind it: the first reads as a broken join, the second as a
            product with no warning time, the third as "we saw nothing coming", the fourth as a
            settled figure, the fifth as rates over the whole window. */}
        <ul className="rh-caveats">
          <li>
            Siden er stort sett «ikke vurdert» — og tabellen under tynn — de første ukene. Loggen over varselrunder
            starter først da denne funksjonen ble tatt i bruk
            {recordingStarted ? ` (${fmtDate(recordingStarted)})` : ''}, og historikken kan ikke
            etterfylles. Det er forventet, ikke en ødelagt kobling.
          </li>
          <li>
            Varslingstid måles fra hver utstedelses egen start (06:30 og 07:00 for morgenrushet, 15:00 for
            ettermiddagsrushet), så morgentallene ligger rundt null eller er negative av ren
            konstruksjon.
          </li>
          <li>
            «Ikke vurdert» betyr at ingen varselrunde ble vurdert: dagen er eldre enn loggen over
            varselrunder, varslingen var slått av i dette miljøet, eller skrivingen av runden
            feilet. De to siste telles for seg som «mangler runde», siden en tapt skriving ellers
            er umulig å skille fra etterslepet. Det betyr <em>ikke</em> at et varsel var unødvendig.
          </li>
          <li>
            Et foreløpig utfall («foreløpig») er dagens egen lesning og kan endre seg over natten.
          </li>
          <li>Tabellen «Slik har varslene truffet» dekker bare avklarte dager.</li>
        </ul>
      </header>

      <div className="rh-filters">
        <div className="rh-filter-group" role="group" aria-label="Tidsvindu">
          {WINDOW_OPTIONS.map((w) => (
            <button
              key={w.days}
              type="button"
              className={`rh-toggle ${days === w.days ? 'is-active' : ''}`}
              onClick={() => setDays(w.days)}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <SmallAlertBox variant="negative">
          {error}
          <br />
          Ingen varselrunder eller utfall er skrevet ennå hvis dette er et ferskt miljø —
          prognosene skrives av poller-syklusen på hverdager, og fasit settes først etter rushet.
        </SmallAlertBox>
      ) : null}

      {stats.length > 0 && !error ? <HitTable stats={stats} /> : null}

      {loading ? (
        <div className="rh-loading">
          <Loader>Henter rushhistorikk …</Loader>
        </div>
      ) : null}

      {!loading && !error && dayRows.length === 0 ? (
        <SmallAlertBox className="rh-empty" variant="info">
          Ingen prognoser ble utstedt i dette vinduet. Rushvarsel gir ingen prognose i helger og på
          helligdager, så et kort vindu kan være tomt helt uten at noe er galt.
        </SmallAlertBox>
      ) : null}

      {/* Dimmed rather than blanked while a new window loads, so the page keeps its height and a
          click on the picker visibly does something. The rows still standing are the OLD window's. */}
      <section className={`rh-days ${loading ? 'is-stale' : ''}`}>
        {dayRows.length > 0 ? <Heading3>Dag for dag</Heading3> : null}
        <DayTable days={dayRows} />
      </section>
    </main>
  );
}
