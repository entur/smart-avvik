import { useCallback, useEffect, useRef, useState } from 'react';
import { NegativeButton, PrimaryButton, SecondaryButton, TertiaryButton } from '@entur/button';
import { SmallAlertBox } from '@entur/alert';
import { Modal } from '@entur/modal';
import { Paragraph, SubParagraph } from '@entur/typography';
import type { Brand } from '../brand';
import type { Config } from '../api';
import { deleteAllData, deleteDevice, registerDevice, sendTestPush } from '../api';
import {
  applicationServerKey,
  disablePush,
  enablePush,
  getExistingSubscription,
  isIOS,
  isPushSupported,
  isStandalone,
  subscribePush,
} from '../push';
import { forgetPushIdentity, rememberPushIdentity } from '../pushIdentity';
import { T6_SLETT_ALT } from '../personvern';
import { runDeleteAll } from './deleteAllFlow';
import { isStoredDeviceId } from './deviceIdentity';
import { clearPushOff, isPushOff, markPushOff } from './pushOptOut';
import { VarselkortNotis } from './PersonvernNotis';

type State =
  | 'server-off' // server has no VAPID keys configured
  | 'unsupported' // browser lacks the Push API
  | 'ios-not-installed' // iOS Safari, not added to Home Screen (push needs standalone)
  | 'default' // permission not yet requested
  | 'granted' // subscribed and ready
  | 'denied'; // permission blocked

/**
 * Can a push reach this device right now? Reported upwards so the journey list can say so next
 * to the journeys it affects (2026-09-04) — measured in prd that day, 79 of 186 registered
 * journeys belonged to devices that had never turned notifications on, and nothing on the page
 * connected the two facts. `unknown` while the browser subscription is still being looked up, so
 * a subscribed device does not flash a warning on every load; `unavailable` where the traveller
 * can do nothing here (no VAPID keys on the server, a browser without the Push API), which the
 * card already explains and a second notice would only repeat.
 */
export type DeliveryStatus = 'unknown' | 'deliverable' | 'action-needed' | 'unavailable';

function computeState(config: Config): State {
  if (!config.push_enabled || !config.vapid_public_key) return 'server-off';
  if (isIOS() && !isStandalone()) return 'ios-not-installed';
  if (!isPushSupported()) return 'unsupported';
  return Notification.permission as State;
}

// The iOS "Share" glyph (square.and.arrow.up) drawn inline so it matches the actual button an
// iPhone user taps in Safari. Inherits the surrounding text colour via currentColor.
function ShareGlyph() {
  return (
    <svg
      className="av-ios-share-glyph"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label="Del-ikonet"
    >
      <path d="M12 3.5v11" />
      <path d="M8.2 6.8 12 3l3.8 3.8" />
      <path d="M8 8.5H7a2 2 0 0 0-2 2V19a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-8.5a2 2 0 0 0-2-2h-1" />
    </svg>
  );
}

export default function VarselStatus({
  config,
  deviceId,
  ensureDeviceId,
  onDeletedAll,
  brand,
  onDeliveryStatus,
}: {
  config: Config;
  /** Null right after «Slett alt», and for a visitor who has never registered anything. */
  deviceId: string | null;
  /** Returns the stored device id, minting and persisting one if there is none. Turning push on
   *  is a registration action, so it may mint; no background path may. */
  ensureDeviceId: () => string;
  /** Last step of «Slett alt»: App forgets the id and everything keyed to it. */
  onDeletedAll: () => void;
  brand: Brand;
  /** See `DeliveryStatus`. Called whenever the answer changes. */
  onDeliveryStatus?: (status: DeliveryStatus) => void;
}) {
  const [state, setState] = useState<State>(() => computeState(config));
  // null = not yet looked up. Only the `granted` state has anything to look up; every other
  // state knows it is not subscribed without asking the browser.
  const [subscribed, setSubscribed] = useState<boolean | null>(() =>
    computeState(config) === 'granted' ? null : false,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // `loading` on an Entur button shows a spinner but does not disable it, so a double tap would
  // otherwise start two teardowns racing each other. A ref, not the busy state: state updates are
  // batched, and two taps in one tick would both read the stale value.
  //
  // It doubles as `runDeleteAll`'s suspension gate (`DeleteAllGate` is `{ current: boolean }` for
  // exactly this): the flow closes it synchronously and reopens it in a `finally`, so "a teardown
  // is running" is one flag rather than two that can disagree.
  const deleting = useRef(false);

  // What the service worker needs to renew the subscription by itself on `pushsubscriptionchange`
  // (pushIdentity.ts). Written after every successful registration, so it follows a brand change
  // and a rotated key. Best-effort: a browser that refuses IndexedDB simply gets no unattended
  // renewal, which is where every browser was before 2026-09-04.
  const remember = useCallback(
    (id: string) => {
      if (!config.vapid_public_key) return;
      void rememberPushIdentity({
        device_id: id,
        brand: brand.id,
        application_server_key: applicationServerKey(config.vapid_public_key),
      }).catch(() => undefined);
    },
    [config.vapid_public_key, brand],
  );

  // Reconcile the button state with the actual subscription (e.g. after reload, or if the user
  // granted permission earlier). Re-registering the device is cheap and keeps the server's copy
  // of the subscription fresh across browser subscription rotations.
  useEffect(() => {
    if (state !== 'granted') return;
    let cancelled = false;
    const vapidPublicKey = config.vapid_public_key;
    (async () => {
      let sub = await getExistingSubscription();
      if (cancelled) return;
      // This is a background effect, not one of the busy-gated handlers, so «Slett alt» has to
      // suspend it explicitly: the lookup above can already be in flight when the user confirms,
      // and a write landing here would recreate the record, with a live push subscription,
      // seconds after the dialog said «Alt er slettet».
      if (deleting.current) return;
      // Silent repair (2026-09-04): permission is granted but the browser holds no subscription —
      // the push service retired it, or storage was cleared. Subscribing again needs no gesture
      // once permission is granted, so asking the user to tap «Slå på varsler» a second time only
      // left devices dark until they happened to notice. NOT after «Skru av»: that leaves the
      // permission granted too, and undoing the user's choice on their next visit is the one
      // thing this must never do — pushOptOut.ts is that memory. And never without a stored
      // device id: with none there are no journeys to alert on, and minting one here would
      // recreate the identity «Slett alt» erased.
      if (
        !sub &&
        vapidPublicKey &&
        !isPushOff(localStorage) &&
        isStoredDeviceId(deviceId, localStorage)
      ) {
        try {
          sub = await subscribePush(vapidPublicKey);
        } catch {
          sub = null;
        }
        if (cancelled || deleting.current) return;
      }
      setSubscribed(Boolean(sub));
      // Re-registering also refreshes the stored brand, so opening the app under a different
      // ?brand= moves this device's notifications with it rather than stranding them. With no
      // device id there is nothing to refresh — and minting one on this background path would
      // recreate the record «Slett alt» just deleted.
      //
      // The second guard closes the sliver the first cannot: the lookup can also resolve just
      // *after* the teardown settles, while this closure still holds the deleted id and React has
      // not yet re-rendered with the null one. `isStoredDeviceId` reads storage, which «Slett
      // alt» clears synchronously, so it is already false there.
      if (sub && isStoredDeviceId(deviceId, localStorage)) {
        void registerDevice(deviceId, sub.toJSON(), brand.id)
          .then(() => remember(deviceId))
          .catch(() => undefined);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [state, deviceId, brand, config.vapid_public_key, remember]);

  const enable = useCallback(async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const subscription = await enablePush(config.vapid_public_key!);
      const id = ensureDeviceId();
      await registerDevice(id, subscription, brand.id);
      clearPushOff(localStorage);
      remember(id);
      setSubscribed(true);
      setState('granted');
      setNotice('Varsler er slått på.');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setState(computeState(config));
    } finally {
      setBusy(false);
    }
  }, [config, ensureDeviceId, brand, remember]);

  const test = useCallback(async () => {
    if (!deviceId) return; // unreachable: the button renders only when `deliverable`, i.e. with an id
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await sendTestPush(deviceId);
      setNotice('Testvarsel sendt — det dukker opp om et øyeblikk.');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [deviceId]);

  const disable = useCallback(async () => {
    if (!deviceId) return; // unreachable: the button renders only when `deliverable`, i.e. with an id
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await disablePush();
      await deleteDevice(deviceId);
      // Both memories, or the choice is undone: the silent repair would re-subscribe on the next
      // visit (permission stays granted), and the worker would re-register on the next rotation.
      markPushOff(localStorage);
      void forgetPushIdentity().catch(() => undefined);
      setSubscribed(false);
      setState('default');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [deviceId]);

  // Server first: see deleteAllFlow.ts. On failure nothing client-side has moved, so the stored
  // id — the only handle on the data still held server-side — is intact for a retry.
  //
  // The same try/catch/finally shape as `enable`/`test`/`disable`, and for a sharper reason than
  // symmetry: the last step reaches `localStorage.removeItem`, which a locked-down profile can
  // refuse. Without the `finally`, that throw skipped `setBusy(false)` and left the confirm modal
  // open, undismissable (its dismiss is gated on not-busy) and unretryable for the session — after
  // the server-side deletion had already succeeded. `deleting.current` is released by
  // `runDeleteAll`'s own `finally`, so it survives the same throw.
  const deleteEverything = useCallback(async () => {
    if (!deviceId || deleting.current) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const outcome = await runDeleteAll(
        {
          deleteServerData: () => deleteAllData(deviceId),
          unsubscribePush: disablePush,
          forgetDevice: () => {
            // The worker's renewal record goes with the id it names — kept, a later
            // `pushsubscriptionchange` would re-register the device this erasure just removed.
            void forgetPushIdentity().catch(() => undefined);
            clearPushOff(localStorage);
            setSubscribed(false);
            setState('default');
            onDeletedAll();
          },
        },
        // Closes the window in which the subscription-reconciliation effect could re-register
        // this device. Set synchronously inside the call, before the first await.
        deleting,
      );
      if (outcome.status === 'deleted') setNotice(T6_SLETT_ALT.doneNotice);
      else setError(outcome.message);
    } catch (e) {
      // Only reachable from `forgetDevice`: everything before it is handled inside the flow. The
      // server data is gone at this point, so this reports rather than offers a retry.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      // Closed either way: an error belongs in the card, where it is readable and where tapping
      // «Slett alt» again is the retry.
      setConfirmDelete(false);
      setBusy(false);
    }
  }, [deviceId, onDeletedAll]);

  // `subscribed` tracks the BROWSER's push subscription; this tracks whether that subscription is
  // one the server can actually deliver to, which additionally needs a device id. The two come
  // apart after a «Slett alt» whose unsubscribe step failed (it is best-effort by design — see
  // deleteAllFlow.ts): on the next load the id is gone but the subscription is not, so the card
  // would announce «Varsler er på for denne enheten» over a «Send testvarsel» and a «Skru av»
  // that both early-return on the null id and do nothing at all. Falling back to the unfinished-
  // setup branch is honest and actionable: «Slå på varsler» mints an id and re-registers the
  // subscription that is already there.
  const deliverable = subscribed === true && deviceId !== null;

  const deliveryStatus: DeliveryStatus =
    state === 'server-off' || state === 'unsupported'
      ? 'unavailable'
      : state === 'granted' && subscribed === null
        ? 'unknown'
        : deliverable
          ? 'deliverable'
          : 'action-needed';
  useEffect(() => {
    onDeliveryStatus?.(deliveryStatus);
  }, [deliveryStatus, onDeliveryStatus]);

  return (
    // The id is the target of the journey list's «Slå på varsler» link (App.tsx), shown there when
    // journeys exist that no push can reach.
    <div className="av-card av-status" id="varsel-status">
      {state === 'server-off' && (
        <SmallAlertBox variant="information" width="fluid">
          Varsler er ikke aktivert på tjeneren ennå. Du kan registrere reiser, men får ikke
          push-varsel før nøklene er på plass.
        </SmallAlertBox>
      )}

      {state === 'unsupported' && (
        <SmallAlertBox variant="information" width="fluid">
          Nettleseren din støtter dessverre ikke push-varsel. Prøv Chrome på Android, eller
          installer appen på en iPhone med iOS 16.4 eller nyere.
        </SmallAlertBox>
      )}

      {state === 'ios-not-installed' && (
        <SmallAlertBox variant="information" title="Slå på varsler på iPhone" width="fluid">
          <Paragraph margin="none">
            På iPhone må appen ligge på Hjem-skjermen før den kan varsle deg:
          </Paragraph>
          <ol className="av-ios-steps">
            <li>
              Trykk <strong>Del</strong>-knappen <ShareGlyph /> nederst i Safari.
            </li>
            <li>
              Velg <strong>«Legg til på Hjem-skjerm»</strong>.
            </li>
            <li>Åpne appen fra Hjem-skjermen, og slå på varsler her.</li>
          </ol>
        </SmallAlertBox>
      )}

      {state === 'default' && (
        <>
          <Paragraph margin="none">
            Slå på varsler for å få beskjed når et avvik kan ramme en av reisene dine.
          </Paragraph>
          <div className="av-status-actions">
            <PrimaryButton onClick={enable} loading={busy}>
              Slå på varsler
            </PrimaryButton>
          </div>
        </>
      )}

      {state === 'granted' && (
        <>
          {deliverable ? (
            <SmallAlertBox variant="success" width="fluid">
              Varsler er på for denne enheten.
            </SmallAlertBox>
          ) : (
            <Paragraph margin="none">Fullfør oppsettet for å få varsler på denne enheten.</Paragraph>
          )}
          <div className="av-status-actions">
            {deliverable ? (
              <>
                <SecondaryButton onClick={test} loading={busy}>
                  Send testvarsel
                </SecondaryButton>
                <TertiaryButton onClick={disable} disabled={busy}>
                  Skru av
                </TertiaryButton>
              </>
            ) : (
              <PrimaryButton onClick={enable} loading={busy}>
                Slå på varsler
              </PrimaryButton>
            )}
          </div>
        </>
      )}

      {state === 'denied' && (
        <SmallAlertBox variant="warning" title="Varsler er blokkert" width="fluid">
          Skru dem på igjen i nettleser- eller systeminnstillingene for å få varsel.
        </SmallAlertBox>
      )}

      {/* T4 — what enabling notifications stores, and what «Skru av» does and does not delete.
          Shown where the actions it names exist: BEFORE the user taps «Slå på varsler» (the moment
          the subscription would be collected) and after. In the blocked / unsupported / not-installed
          states nothing can be collected and no «Skru av» is rendered, so the text pointed at
          buttons that were not there (design review 2026-09-01, finding 34). */}
      {(state === 'default' || state === 'granted') && <VarselkortNotis />}

      {notice && (
        <SubParagraph className="av-status-note" role="status">
          {notice}
        </SubParagraph>
      )}
      {error && (
        <SmallAlertBox variant="negative" width="fluid">
          {error}
        </SmallAlertBox>
      )}

      {/* Withdrawal has to be as easy as consent, so this sits outside every `state === …` branch
          — nested in the subscribed one it would be hidden from exactly the user who registered
          journeys and never turned push on. It is gated only on there being an id: without one
          nothing is stored, because a journey cannot exist without the id it is keyed to. */}
      {deviceId && (
        <div className="av-status-actions av-status-danger">
          <TertiaryButton onClick={() => setConfirmDelete(true)} disabled={busy}>
            {T6_SLETT_ALT.buttonText}
          </TertiaryButton>
        </div>
      )}

      {/* Renders through a portal (Linje's Modal wraps @reach/dialog), so sitting inside the card
          costs it nothing in layout and keeps it next to the button that opens it. */}
      <Modal
        open={confirmDelete}
        onDismiss={() => {
          if (!busy) setConfirmDelete(false);
        }}
        size="medium"
        title={T6_SLETT_ALT.dialogTitle}
      >
        <div className="av-form">
          <Paragraph margin="none">{T6_SLETT_ALT.dialogBody}</Paragraph>
          <div className="av-form-actions">
            <SecondaryButton onClick={() => setConfirmDelete(false)} disabled={busy}>
              {T6_SLETT_ALT.cancelText}
            </SecondaryButton>
            <NegativeButton onClick={deleteEverything} loading={busy} disabled={busy}>
              {T6_SLETT_ALT.confirmText}
            </NegativeButton>
          </div>
        </div>
      </Modal>
    </div>
  );
}
