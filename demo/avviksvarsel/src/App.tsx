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
  fetchRushOutlook,
  fetchStops,
  type Config,
  type Journey,
  type NewJourney,
  type RushForecast,
  type StopItem,
} from './api';
import { isPushSupported, registerServiceWorker } from './push';
import type { Brand } from './brand';
import VarselStatus from './components/VarselStatus';
import RushOutlook from './components/RushOutlook';
import JourneyCard from './components/JourneyCard';
import AddJourneyModal from './components/AddJourneyModal';
import { AvsenderFooter, EksperimentNotis, PersonvernLinje } from './components/PersonvernNotis';
// Stable per-device id, kept in localStorage. Journeys and push subscriptions are keyed to it, and
// only a REGISTRATION action may mint one — see components/deviceIdentity.ts, which owns that rule
// so test/deviceIdentity.test.mjs can hold it.
import {
  forgetDeviceId,
  isStoredDeviceId,
  mintDeviceId,
  readDeviceId,
} from './components/deviceIdentity';

const REFRESH_MS = 60_000;

export default function App({ brand }: { brand: Brand }) {
  // Nullable app state, not a mount-frozen ref: «Slett alt» sets it to null, which suspends the
  // poll and the VarselStatus flows, and the next registration mints a new one and resumes
  // everything on it. Frozen at mount, the "register a new journey" the done-notice invites would
  // write under the DELETED key and orphan the journey on the next reload.
  const [deviceId, setDeviceId] = useState<string | null>(() => readDeviceId(localStorage));
  const [config, setConfig] = useState<Config | null>(null);
  const [stops, setStops] = useState<StopItem[]>([]);
  const [journeys, setJourneys] = useState<Journey[]>([]);
  const [rushes, setRushes] = useState<RushForecast[]>([]);
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
    const id = readDeviceId(localStorage) ?? mintDeviceId(localStorage);
    setDeviceId(id);
    return id;
  }, []);

  const refreshJourneys = useCallback(async () => {
    // The id this response will be judged against. The 60 s interval and the focus/
    // visibilitychange listeners keep firing straight through «Slett alt», and the list endpoint
    // snapshots under a lock but scores each journey outside it, so it is routinely slower than
    // the single-pop DELETE: a GET dispatched just before the deletion can land after it and
    // repaint the deleted journeys directly under the «Alt er slettet» notice. It would not heal
    // either — once the id is null the rebound closure short-circuits, so no later poll clears it.
    const issuedFor = deviceId;
    if (!issuedFor) return; // deleted, or never registered — nothing to poll for, and nothing to mint
    try {
      const res = await fetchJourneys(issuedFor);
      // Storage, not state: «Slett alt» removes the id synchronously, so this is already false in
      // the sliver between the deletion and the re-render — and it covers a second tab's delete.
      if (!isStoredDeviceId(issuedFor, localStorage)) return;
      setJourneys(res.journeys);
      refreshFailures.current = 0;
      setError(null); // a recovered poll must clear an earlier error, or it sticks forever
    } catch (e) {
      // Same guard on the failure path: a poll for an id that no longer exists must not raise a
      // banner over a deletion that worked.
      if (!isStoredDeviceId(issuedFor, localStorage)) return;
      // A single failed background poll (phone briefly offline, server redeploying) isn't worth
      // a red banner over data that is at most a minute stale — surface it only when it persists.
      refreshFailures.current += 1;
      if (refreshFailures.current >= 2) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  }, [deviceId]);

  // Today's rush levels, for the panel at the top. Deliberately its own request and its own
  // silent failure: the server returns [] for every ordinary "nothing to show" reason, so an
  // outage here should leave the previous panel standing rather than raise a banner over a page
  // whose actual job — the registered journeys — is unaffected.
  //
  // Reads the id and never mints one (deviceIdentity.ts's rule: only a registration may), and with
  // no id there is nothing to ask about — the panel is a claim about THIS device's registrations,
  // so emptying it here is what clears it after «Slett alt». The post-await staleness guard is
  // refreshJourneys' verbatim: a response dispatched before the deletion must not repaint a panel
  // about registrations the server no longer has.
  const refreshRushes = useCallback(async () => {
    const issuedFor = deviceId;
    if (!issuedFor) {
      setRushes([]);
      return;
    }
    try {
      const res = await fetchRushOutlook(issuedFor);
      if (!isStoredDeviceId(issuedFor, localStorage)) return;
      setRushes(res.rushes);
    } catch {
      // keep whatever was last shown
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
    const id = readDeviceId(localStorage);
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

  // The rush panel's own first fetch, deliberately NOT folded into the load above: that effect
  // runs once by design (see its note), while this one must re-run when the device id moves — a
  // fresh registration brings the panel to life, «Slett alt» empties it — and it must never be
  // able to fail the page load or hold up the loading state.
  useEffect(() => {
    void refreshRushes();
  }, [refreshRushes]);

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
    const timer = window.setInterval(() => {
      void refreshJourneys();
      void refreshRushes();
    }, REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return;
      void refreshJourneys();
      void refreshRushes();
    };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', onVisible);
    };
  }, [refreshJourneys, refreshRushes]);

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
  //
  // Storage first, and that ordering is load-bearing: `isStoredDeviceId` is the staleness oracle
  // for every background response, and it only starts refusing once the key is gone. React state
  // follows unconditionally, so a storage that refuses `removeItem` still leaves the page showing
  // an empty list rather than journeys the server no longer has — the throw then propagates to
  // VarselStatus, which reports it in the card.
  const handleDeletedAll = useCallback(() => {
    try {
      forgetDeviceId(localStorage);
    } finally {
      setDeviceId(null);
      setJourneys([]);
      setError(null);
    }
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
            {/* Above the push controls, and below T1/T2 — which must stay on screen without
                scrolling. On a red morning this is what the traveller opened the app to find out.
                Renders nothing at all unless the server says this device is notified about a rush
                that is still ahead. */}
            <RushOutlook rushes={rushes} brand={brand} />

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
                arrives. The Rushvarsel link is Entur-branded and stays off the unbranded
                variant — matching the pushes themselves, whose unbranded click-through also
                skips /togmelding. */}
            {config?.togvarsel_notify && (
              <SmallAlertBox variant="information" title="Nytt: Rushvarsel" width="fluid">
                Varslene omfatter nå også togtrafikken i rushtiden: på hverdager får du beskjed
                når prognosen for morgen- eller ettermiddagsrushet i Oslo-området er gul eller
                rød og en av de registrerte reisene dine kan bli berørt.
                {brand.id === 'entur' && (
                  <>
                    {' '}
                    <Link href="/togmelding">Se dagens rushvarsel</Link>
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
