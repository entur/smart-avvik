import { useCallback, useEffect, useRef, useState } from 'react';
import { SkipToContent } from '@entur/a11y';
import { Contrast } from '@entur/layout';
import { Logo } from '@entur/menu';
import { Heading1, Heading3, LeadParagraph, Link, Paragraph } from '@entur/typography';
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
import type { Brand } from './brand';
import VarselStatus from './components/VarselStatus';
import JourneyCard from './components/JourneyCard';
import AddJourneyModal from './components/AddJourneyModal';
import { AvsenderFooter, EksperimentNotis, PersonvernLinje } from './components/PersonvernNotis';

// Stable per-device id, kept in localStorage. Journeys and push subscriptions are keyed to it.
//
// Reading and minting are deliberately two functions, and only a REGISTRATION action may mint —
// adding a journey, or turning push on. «Slett alt» deletes the id while the 60 s poll and the
// focus/visibilitychange listeners keep running, so a single getter that minted for every caller
// would put a fresh id back in localStorage seconds after the deletion (the focus listener fires
// the moment the user returns to the tab) and quietly recreate the identity the user just erased.
const DEVICE_ID_KEY = 'avviksvarsel.device_id';

function readDeviceId(): string | null {
  return localStorage.getItem(DEVICE_ID_KEY);
}

function mintDeviceId(): string {
  const id =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `dev-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  localStorage.setItem(DEVICE_ID_KEY, id);
  return id;
}

const REFRESH_MS = 60_000;

export default function App({ brand }: { brand: Brand }) {
  // Nullable app state, not a mount-frozen ref: «Slett alt» sets it to null, which suspends the
  // poll and the VarselStatus flows, and the next registration mints a new one and resumes
  // everything on it. Frozen at mount, the "register a new journey" the done-notice invites would
  // write under the DELETED key and orphan the journey on the next reload.
  const [deviceId, setDeviceId] = useState<string | null>(() => readDeviceId());
  const [config, setConfig] = useState<Config | null>(null);
  const [stops, setStops] = useState<StopItem[]>([]);
  const [journeys, setJourneys] = useState<Journey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const notificationParams = new URLSearchParams(location.search);
  const notificationJourney = notificationParams.get('journey');
  const notificationSituation = notificationParams.get('situation');

  const refreshFailures = useRef(0);
  const handledNotification = useRef<string | null>(null);

  // Mint on demand, from a registration action only. Reads localStorage rather than state, so a
  // second tab's id is adopted instead of a competing one being created.
  const ensureDeviceId = useCallback((): string => {
    const id = readDeviceId() ?? mintDeviceId();
    setDeviceId(id);
    return id;
  }, []);

  const refreshJourneys = useCallback(async () => {
    if (!deviceId) return; // deleted, or never registered — nothing to poll for, and nothing to mint
    try {
      const res = await fetchJourneys(deviceId);
      setJourneys(res.journeys);
      refreshFailures.current = 0;
      setError(null); // a recovered poll must clear an earlier error, or it sticks forever
    } catch (e) {
      // A single failed background poll (phone briefly offline, server redeploying) isn't worth
      // a red banner over data that is at most a minute stale — surface it only when it persists.
      refreshFailures.current += 1;
      if (refreshFailures.current >= 2) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  }, [deviceId]);

  // Initial load: config, stops, journeys. Register the service worker up front so enabling
  // push later is a single tap.
  //
  // Runs once, not per device id: config and stops do not depend on it, and re-running when
  // «Slett alt» or a fresh registration moves the id would throw the whole page back into its
  // loading state. The id-dependent half is the journey poll above.
  useEffect(() => {
    if (isPushSupported()) void registerServiceWorker().catch(() => undefined);
    const id = readDeviceId();
    (async () => {
      try {
        const [cfg, stopList, journeyRes] = await Promise.all([
          fetchConfig(),
          fetchStops(),
          id ? fetchJourneys(id) : Promise.resolve({ journeys: [] as Journey[] }),
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
  }, []);

  // A notification can launch a cold PWA, so the target card does not exist until the initial
  // journey request completes. Move both viewport and keyboard focus once it has rendered.
  useEffect(() => {
    if (loading || !notificationJourney) return;
    const notificationKey = `${notificationJourney}|${notificationSituation ?? ''}`;
    if (handledNotification.current === notificationKey) return;
    handledNotification.current = notificationKey;
    const target = document.getElementById(`journey-${encodeURIComponent(notificationJourney)}`);
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target.focus({ preventScroll: true });
  }, [loading, journeys, notificationJourney]);

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

  // Which deployment this is, e.g. " (dev)" — empty in prd. The installed app's home-screen name and
  // icon come from the manifest, which is baked into the one image every environment shares, so the
  // server's answer is the only thing that can tell a dev install from a prd one on the same phone.
  const envSuffix = config?.environment ? ` (${config.environment})` : '';

  useEffect(() => {
    document.title = `${brand.documentTitle}${envSuffix}`;
  }, [brand, envSuffix]);

  const handleCreate = useCallback(
    async (journey: NewJourney) => {
      // One of the two actions allowed to mint an id — this is the path that brings the app back
      // to life after «Slett alt», in the same session and with no reload.
      const created = await createJourney(ensureDeviceId(), journey);
      setJourneys((prev) => [...prev, created]);
    },
    [ensureDeviceId],
  );

  const handleDelete = useCallback(
    async (journeyId: string) => {
      if (!deviceId) return; // no id, no journeys — the list this button sits in is empty
      await deleteJourney(journeyId, deviceId);
      setJourneys((prev) => prev.filter((j) => j.journey_id !== journeyId));
    },
    [deviceId],
  );

  // Last step of «Slett alt», reached only after the server confirmed. The only place the stored
  // id is removed — never on the failure path, where it is exactly what a retry needs.
  const handleDeletedAll = useCallback(() => {
    localStorage.removeItem(DEVICE_ID_KEY);
    setDeviceId(null);
    setJourneys([]);
    setError(null);
  }, []);

  return (
    <>
      <SkipToContent mainId="main-content">Gå til hovedinnhold</SkipToContent>

      <Contrast as="header" className="av-header">
        <div className="av-header-inner">
          {/* The Entur mark is the loudest piece of branding on the page, so an unbranded
              variant drops it outright rather than substituting a neutral one. */}
          {brand.showEnturLogo && <Logo />}
          <Heading1 as="h1" margin="none" className="av-header-title">
            {brand.heading}
            {envSuffix}
          </Heading1>
        </div>
      </Contrast>

      <main id="main-content" className="av-main">
        {/* T1 and T2 sit above and below the intro, before the loading branch, so the experiment
            caveat and the privacy line are on screen without scrolling or a single tap — which is
            the acceptance criterion, and the point of giving art. 13 information at collection. */}
        <EksperimentNotis />

        <LeadParagraph className="av-intro">
          Registrer kollektivreisene dine, så varsler vi deg når et avvik kan ramme en av dem.
        </LeadParagraph>

        <PersonvernLinje brandId={brand.id} />

        {loading ? (
          <div className="av-loading">
            <Loader>Laster …</Loader>
          </div>
        ) : (
          <>
            {config && (
              <VarselStatus
                config={config}
                deviceId={deviceId}
                ensureDeviceId={ensureDeviceId}
                onDeletedAll={handleDeletedAll}
                brand={brand}
              />
            )}

            {/* Shown only where the server actually delivers rush-forecast pushes
                (TOGVARSEL_NOTIFY_ENABLED via /config): existing subscribers opted into
                train-specific avviksvarsel, so a new push class gets announced before it
                arrives. The Togmeldingen link is Entur-branded and stays off the unbranded
                variant — matching the pushes themselves, whose unbranded click-through also
                skips /togmelding. */}
            {config?.togvarsel_notify && (
              <SmallAlertBox variant="information" title="Nytt: rushvarsel" width="fluid">
                Varslene omfatter nå også togtrafikken i rushtiden: på hverdager får du beskjed
                når prognosen for morgen- eller ettermiddagsrushet i Oslo-området er gul eller
                rød og en av de registrerte reisene dine kan bli berørt.
                {brand.id === 'entur' && (
                  <>
                    {' '}
                    <Link href="/togmelding">Se dagens togmelding</Link>
                  </>
                )}
              </SmallAlertBox>
            )}

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
                      notificationSituation={
                        j.journey_id === notificationJourney ? notificationSituation : null
                      }
                      onDelete={() => void handleDelete(j.journey_id)}
                    />
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </main>

      {/* Unconditional on brand. `?brand=none` strips the logo, the palette and the typeface, but
          art. 13(1)(a) requires the controller's identity, so this one line names Entur in every
          variant — the one place the unbranded page says the word. */}
      <AvsenderFooter brandId={brand.id} />

      <AddJourneyModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        stops={stops}
        onCreate={handleCreate}
        brandId={brand.id}
      />
    </>
  );
}
