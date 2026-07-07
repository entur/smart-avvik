import { useCallback, useEffect, useRef, useState } from 'react';
import { SkipToContent } from '@entur/a11y';
import { Contrast } from '@entur/layout';
import { TravelTag } from '@entur/travel';
import { Heading1, Heading3, LeadParagraph, Paragraph } from '@entur/typography';
import { PrimaryButton } from '@entur/button';
import { AddIcon } from '@entur/icons';
import { Loader } from '@entur/loader';
import { SmallAlertBox } from '@entur/alert';
import {
  createJourney,
  deleteJourney,
  fetchConfig,
  fetchJourneys,
  fetchStops,
  type Config,
  type Journey,
  type NewJourney,
  type StopItem,
} from './api';
import { isPushSupported, registerServiceWorker } from './push';
import VarselStatus from './components/VarselStatus';
import JourneyCard from './components/JourneyCard';
import AddJourneyModal from './components/AddJourneyModal';

// Stable per-device id, kept in localStorage. Journeys and push subscriptions are keyed to it.
function getDeviceId(): string {
  const key = 'avviksvarsel.device_id';
  let id = localStorage.getItem(key);
  if (!id) {
    id =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `dev-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(key, id);
  }
  return id;
}

const REFRESH_MS = 60_000;

export default function App() {
  const deviceId = useRef(getDeviceId()).current;
  const [config, setConfig] = useState<Config | null>(null);
  const [stops, setStops] = useState<StopItem[]>([]);
  const [journeys, setJourneys] = useState<Journey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  const refreshJourneys = useCallback(async () => {
    try {
      const res = await fetchJourneys(deviceId);
      setJourneys(res.journeys);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [deviceId]);

  // Initial load: config, stops, journeys. Register the service worker up front so enabling
  // push later is a single tap.
  useEffect(() => {
    document.title = 'Entur Avviksvarsel';
    if (isPushSupported()) void registerServiceWorker().catch(() => undefined);
    (async () => {
      try {
        const [cfg, stopList, journeyRes] = await Promise.all([
          fetchConfig(),
          fetchStops(),
          fetchJourneys(deviceId),
        ]);
        setConfig(cfg);
        setStops(stopList);
        setJourneys(journeyRes.journeys);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, [deviceId]);

  // Keep the live disruptions on each journey fresh: poll, and re-fetch when the app regains focus
  // (e.g. after tapping a notification).
  useEffect(() => {
    const timer = window.setInterval(() => void refreshJourneys(), REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refreshJourneys();
    };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', onVisible);
    };
  }, [refreshJourneys]);

  const handleCreate = useCallback(
    async (journey: NewJourney) => {
      const created = await createJourney(deviceId, journey);
      setJourneys((prev) => [...prev, created]);
    },
    [deviceId],
  );

  const handleDelete = useCallback(
    async (journeyId: string) => {
      await deleteJourney(journeyId, deviceId);
      setJourneys((prev) => prev.filter((j) => j.journey_id !== journeyId));
    },
    [deviceId],
  );

  return (
    <>
      <SkipToContent mainId="main-content">Gå til hovedinnhold</SkipToContent>

      <Contrast as="header" className="av-header">
        <div className="av-header-inner">
          <TravelTag transport="rail" label="Tog" />
          <Heading1 as="h1" margin="none" className="av-header-title">
            Avviksvarsel
          </Heading1>
        </div>
      </Contrast>

      <main id="main-content" className="av-main">
        <LeadParagraph className="av-intro">
          Registrer togreisene dine, så varsler vi deg når et avvik kan ramme en av dem.
        </LeadParagraph>

        {loading ? (
          <div className="av-loading">
            <Loader>Laster …</Loader>
          </div>
        ) : (
          <>
            {config && <VarselStatus config={config} deviceId={deviceId} />}

            {error && (
              <SmallAlertBox variant="negative" width="fluid">
                {error}
              </SmallAlertBox>
            )}

            <section className="av-section" aria-labelledby="mine-reiser">
              <div className="av-section-head">
                <Heading3 as="h2" id="mine-reiser" margin="none">
                  Mine reiser
                </Heading3>
                <PrimaryButton size="small" onClick={() => setModalOpen(true)}>
                  <AddIcon /> Legg til reise
                </PrimaryButton>
              </div>

              {journeys.length === 0 ? (
                <div className="av-card av-empty">
                  <Paragraph margin="none">
                    Du har ingen registrerte reiser ennå. Legg til en strekning og et tidsrom for å
                    komme i gang.
                  </Paragraph>
                </div>
              ) : (
                <ul className="av-journey-list">
                  {journeys.map((j) => (
                    <JourneyCard
                      key={j.journey_id}
                      journey={j}
                      onDelete={() => void handleDelete(j.journey_id)}
                    />
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </main>

      <AddJourneyModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        stops={stops}
        onCreate={handleCreate}
      />
    </>
  );
}
