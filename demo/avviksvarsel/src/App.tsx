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
import VarselStatus, { type DeliveryStatus } from './components/VarselStatus';
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
// How long a deleted journey can be brought back before the DELETE is actually sent (design
// review 2026-09-01, finding 7). Long enough to read the notice and reach the button, short enough
// that the registration — and the consent it rests on — is gone before anyone forgets they removed it.
const UNDO_MS = 8_000;

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
  // Journeys the user has deleted whose DELETE has not been sent yet — rendered as an undo notice
  // in place of the card. The timers live in a ref: they must survive re-renders and be cancellable.
  const [pendingDeletes, setPendingDeletes] = useState<ReadonlySet<string>>(() => new Set());
  const deleteTimers = useRef(new Map<string, number>());
  // Whether a push can reach this device, as VarselStatus reports it. The journey list uses it to
  // say — next to the journeys it affects — that they will not alert until notifications are on
  // (2026-09-04). The card itself explains how; this only points there.
  const [deliveryStatus, setDeliveryStatus] = useState<DeliveryStatus>('unknown');
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
  //
  // Not asked at all where /config says this deployment does not serve rushvarsel
  // (SHOW_RUSHVARSEL off, or notifications off): /rushvarsel-api 404s wholesale there, and while
  // the catch below would leave the panel empty anyway, a failing request named rushvarsel-api on
  // every poll is itself a mention of a product that is meant to be invisible here.
  const refreshRushes = useCallback(async () => {
    const issuedFor = deviceId;
    if (!issuedFor || !config?.togvarsel_notify) {
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
  }, [deviceId, config?.togvarsel_notify]);

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
  // fresh registration brings the panel to life, «Slett alt» empties it — and when /config lands,
  // which is what decides whether the panel is asked for at all. It must never be able to fail
  // the page load or hold up the loading state.
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

  // The deletion itself, after the undo window. The 60 s poll keeps repainting the list from the
  // server meanwhile, which is fine: the card stays rendered as its undo notice for as long as the
  // id is in `pendingDeletes`, and leaves both the list and that set here.
  const commitDelete = useCallback(async (journeyId: string, forDevice: string) => {
    deleteTimers.current.delete(journeyId);
    try {
      await deleteJourney(journeyId, forDevice);
      setJourneys((prev) => prev.filter((j) => j.journey_id !== journeyId));
    } catch (e) {
      // The card comes back with the error over it — the registration still exists server-side.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPendingDeletes((prev) => {
        const next = new Set(prev);
        next.delete(journeyId);
        return next;
      });
    }
  }, []);

  // Delete with an undo window (design review 2026-09-01, finding 7): three identical bins sit
  // close together on a phone, and a delete also withdraws the consent the registration rests on.
  // Nothing reaches the server until the window has passed; «Angre» cancels the timer.
  const handleDelete = useCallback(
    (journeyId: string) => {
      if (!deviceId) return; // no id, no journeys — the list this button sits in is empty
      if (deleteTimers.current.has(journeyId)) return;
      const forDevice = deviceId;
      setPendingDeletes((prev) => new Set(prev).add(journeyId));
      deleteTimers.current.set(
        journeyId,
        window.setTimeout(() => void commitDelete(journeyId, forDevice), UNDO_MS),
      );
    },
    [deviceId, commitDelete],
  );

  const handleUndoDelete = useCallback((journeyId: string) => {
    const timer = deleteTimers.current.get(journeyId);
    if (timer !== undefined) window.clearTimeout(timer);
    deleteTimers.current.delete(journeyId);
    setPendingDeletes((prev) => {
      const next = new Set(prev);
      next.delete(journeyId);
      return next;
    });
  }, []);

  // A pending delete must not be lost to the app being backgrounded or closed inside its window:
  // on pagehide the remaining ones are sent at once (deleteJourney uses `keepalive`).
  useEffect(() => {
    const flush = () => {
      const forDevice = deviceId;
      if (!forDevice) return;
      for (const [journeyId, timer] of [...deleteTimers.current]) {
        window.clearTimeout(timer);
        void commitDelete(journeyId, forDevice);
      }
    };
    window.addEventListener('pagehide', flush);
    return () => window.removeEventListener('pagehide', flush);
  }, [deviceId, commitDelete]);

  // Last step of «Slett alt», reached only after the server confirmed. The only place the stored
  // id is removed — never on the failure path, where it is exactly what a retry needs.
  //
  // Storage first, and that ordering is load-bearing: `isStoredDeviceId` is the staleness oracle
  // for every background response, and it only starts refusing once the key is gone. React state
  // follows unconditionally, so a storage that refuses `removeItem` still leaves the page showing
  // an empty list rather than journeys the server no longer has — the throw then propagates to
  // VarselStatus, which reports it in the card.
  const handleDeletedAll = useCallback(() => {
    // Whatever was inside an undo window is gone with everything else; the timers would only
    // DELETE journeys the server no longer has.
    for (const timer of deleteTimers.current.values()) window.clearTimeout(timer);
    deleteTimers.current.clear();
    setPendingDeletes(new Set());
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
            the acceptance criterion, and the point of giving art. 13 information at collection.
            Both are one line each since 2026-09-02: measured on a 375×812 phone, «Legg til reise»
            sat 1 026 px down, below 191 of the page's 214 words (design review 2026-09-01, finding
            29). The task now comes first; the push controls follow it. */}
        <EksperimentNotis />

        <LeadParagraph className="av-intro">
          Registrer reisene dine, så varsler vi deg når et avvik kan ramme en av dem.
        </LeadParagraph>

        <PersonvernLinje brandId={brand.id} />

        {loading ? (
          <div className="av-loading">
            <Loader>Laster …</Loader>
          </div>
        ) : (
          <>
            {/* Above the journeys, and below T1/T2 — which must stay on screen without
                scrolling. On a red morning this is what the traveller opened the app to find out.
                Renders nothing at all unless the server says this device is notified about a rush
                that is still ahead. */}
            <RushOutlook rushes={rushes} brand={brand} />

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
                {/* Medium and full-width on a phone (finding 27): the page's one action was a
                    133×32 px small button beside the heading. */}
                <PrimaryButton className="av-add-journey" onClick={() => setModalOpen(true)}>
                  <AddIcon /> Legg til reise
                </PrimaryButton>
              </div>

              {/* Journeys exist, and nothing can alert on them from this device. Registering a
                  journey never required notifications to be on — the page works as a live
                  dashboard without them — so this is the one place the two facts meet: measured
                  in prd on 2026-09-04, 79 of 186 registered journeys were on devices that had
                  never turned notifications on, and the page said nothing about it. Not shown
                  while the subscription is still being looked up, nor where the traveller can do
                  nothing about it (the card says why in those states). The link lands on the card
                  below, which knows the right instruction for this device — a button here would
                  have to repeat its iPhone/blocked/default branching. */}
              {journeys.length > 0 && deliveryStatus === 'action-needed' && (
                <SmallAlertBox variant="warning" title="Reisene dine gir ingen varsler ennå" width="fluid">
                  Varsler er ikke slått på for denne enheten. Du ser avvikene her når du åpner
                  appen, men får ingen beskjed når de oppstår.{' '}
                  <Link href="#varsel-status">Slå på varsler</Link>
                </SmallAlertBox>
              )}

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
                      pendingDelete={pendingDeletes.has(j.journey_id)}
                      onDelete={() => handleDelete(j.journey_id)}
                      onUndo={() => handleUndoDelete(j.journey_id)}
                    />
                  ))}
                </ul>
              )}
            </section>

            {/* The push controls, below the task they serve (finding 41): turning notifications on
                is the second step, and the card explaining the mechanism stood between the reader
                and the first. */}
            {config && (
              <VarselStatus
                config={config}
                deviceId={deviceId}
                ensureDeviceId={ensureDeviceId}
                onDeletedAll={handleDeletedAll}
                brand={brand}
                onDeliveryStatus={setDeliveryStatus}
              />
            )}

            {/* Shown only where the server actually delivers rush-forecast pushes
                (TOGVARSEL_NOTIFY_ENABLED via /config): existing subscribers opted into
                train-specific avviksvarsel, so a new push class gets announced before it
                arrives. The Rushvarsel link is Entur-branded and stays off the unbranded
                variant — matching the pushes themselves, whose unbranded click-through also
                skips /rushvarsel. */}
            {config?.togvarsel_notify && (
              <SmallAlertBox variant="information" title="Nytt: Rushvarsel" width="fluid">
                Varslene omfatter nå også togtrafikken i rushtiden: på hverdager får du beskjed
                når prognosen for morgen- eller ettermiddagsrushet i Oslo-området er{' '}
                {config.togvarsel_red_only ? 'rød' : 'gul eller rød'} og en av de registrerte
                reisene dine kan bli berørt.
                {brand.id === 'entur' && (
                  <>
                    {' '}
                    <Link href="/rushvarsel">Se dagens rushvarsel</Link>
                  </>
                )}
              </SmallAlertBox>
            )}
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
