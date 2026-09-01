import { useCallback, useEffect, useRef, useState } from 'react';
import { NegativeButton, PrimaryButton, SecondaryButton, TertiaryButton } from '@entur/button';
import { SmallAlertBox } from '@entur/alert';
import { Modal } from '@entur/modal';
import { Paragraph, SubParagraph } from '@entur/typography';
import type { Brand } from '../brand';
import type { Config } from '../api';
import { deleteAllData, deleteDevice, registerDevice, sendTestPush } from '../api';
import { disablePush, enablePush, getExistingSubscription, isIOS, isPushSupported, isStandalone } from '../push';
import { T6_SLETT_ALT } from '../personvern';
import { runDeleteAll } from './deleteAllFlow';
import { isStoredDeviceId } from './deviceIdentity';
import { VarselkortNotis } from './PersonvernNotis';

type State =
  | 'server-off' // server has no VAPID keys configured
  | 'unsupported' // browser lacks the Push API
  | 'ios-not-installed' // iOS Safari, not added to Home Screen (push needs standalone)
  | 'default' // permission not yet requested
  | 'granted' // subscribed and ready
  | 'denied'; // permission blocked

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
}) {
  const [state, setState] = useState<State>(() => computeState(config));
  const [subscribed, setSubscribed] = useState(false);
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

  // Reconcile the button state with the actual subscription (e.g. after reload, or if the user
  // granted permission earlier). Re-registering the device is cheap and keeps the server's copy
  // of the subscription fresh across browser subscription rotations.
  useEffect(() => {
    if (state !== 'granted') return;
    let cancelled = false;
    getExistingSubscription().then((sub) => {
      if (cancelled) return;
      // This is a background effect, not one of the busy-gated handlers, so «Slett alt» has to
      // suspend it explicitly: the lookup above can already be in flight when the user confirms,
      // and the server's device upsert has no tombstone — a write landing here would recreate the
      // record, with a live push subscription, seconds after the dialog said «Alt er slettet».
      if (deleting.current) return;
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
        void registerDevice(deviceId, sub.toJSON(), brand.id).catch(() => undefined);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [state, deviceId, brand]);

  const enable = useCallback(async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const subscription = await enablePush(config.vapid_public_key!);
      await registerDevice(ensureDeviceId(), subscription, brand.id);
      setSubscribed(true);
      setState('granted');
      setNotice('Varsler er slått på.');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setState(computeState(config));
    } finally {
      setBusy(false);
    }
  }, [config, ensureDeviceId, brand]);

  const test = useCallback(async () => {
    if (!deviceId) return; // only reachable while subscribed, which implies an id
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
    if (!deviceId) return; // only reachable while subscribed, which implies an id
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await disablePush();
      await deleteDevice(deviceId);
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

  return (
    <div className="av-card av-status">
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
          {subscribed ? (
            <SmallAlertBox variant="success" width="fluid">
              Varsler er på for denne enheten.
            </SmallAlertBox>
          ) : (
            <Paragraph margin="none">Fullfør oppsettet for å få varsler på denne enheten.</Paragraph>
          )}
          <div className="av-status-actions">
            {subscribed ? (
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
          Du har blokkert varsler for denne siden. Skru dem på igjen i nettleser- eller
          systeminnstillingene for å få avviksvarsel.
        </SmallAlertBox>
      )}

      {/* T4 — what enabling notifications stores, and what «Skru av» does and does not delete.
          Outside every `state === …` branch on purpose: this has to be readable *before* the user
          taps «Slå på varsler», which is the moment the subscription would be collected, and it
          stays true afterwards. Nested in the subscribed branch it would only ever appear once the
          collection it describes had already happened. */}
      <VarselkortNotis />

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
