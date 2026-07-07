import { useCallback, useEffect, useState } from 'react';
import { PrimaryButton, SecondaryButton, TertiaryButton } from '@entur/button';
import { SmallAlertBox } from '@entur/alert';
import { Paragraph, SubParagraph } from '@entur/typography';
import type { Config } from '../api';
import { deleteDevice, registerDevice, sendTestPush } from '../api';
import { disablePush, enablePush, getExistingSubscription, isIOS, isPushSupported, isStandalone } from '../push';

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

export default function VarselStatus({ config, deviceId }: { config: Config; deviceId: string }) {
  const [state, setState] = useState<State>(() => computeState(config));
  const [subscribed, setSubscribed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Reconcile the button state with the actual subscription (e.g. after reload, or if the user
  // granted permission earlier). Re-registering the device is cheap and keeps the server's copy
  // of the subscription fresh across browser subscription rotations.
  useEffect(() => {
    if (state !== 'granted') return;
    let cancelled = false;
    getExistingSubscription().then((sub) => {
      if (cancelled) return;
      setSubscribed(Boolean(sub));
      if (sub) void registerDevice(deviceId, sub.toJSON()).catch(() => undefined);
    });
    return () => {
      cancelled = true;
    };
  }, [state, deviceId]);

  const enable = useCallback(async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const subscription = await enablePush(config.vapid_public_key!);
      await registerDevice(deviceId, subscription);
      setSubscribed(true);
      setState('granted');
      setNotice('Varsler er slått på.');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setState(computeState(config));
    } finally {
      setBusy(false);
    }
  }, [config, deviceId]);

  const test = useCallback(async () => {
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
        <SmallAlertBox variant="information" title="Legg appen til på Hjem-skjerm" width="fluid">
          For å få varsler på iPhone: trykk <strong>Del</strong>-knappen nederst, velg{' '}
          <strong>«Legg til på Hjem-skjerm»</strong>, og åpne appen derfra. Da kan du slå på
          varsler.
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
    </div>
  );
}
