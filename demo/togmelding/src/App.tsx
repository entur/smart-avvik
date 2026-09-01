import { useCallback, useEffect, useState } from 'react';
import { SkipToContent } from '@entur/a11y';
import { SmallAlertBox } from '@entur/alert';
import { Contrast } from '@entur/layout';
import { Loader } from '@entur/loader';
import { Logo } from '@entur/menu';
import {
  Heading1,
  Heading3,
  LeadParagraph,
  Link,
  Paragraph,
  SmallText,
  SubParagraph,
} from '@entur/typography';
import { fetchConfig, fetchTogmelding, type Togmelding } from './api';
import { CLS_LABEL, issueTime, issueTimeList, osloTime } from './format';
import {
  TOGMELDING_DISCLAIMER,
  TOGMELDING_PERSONVERN_HREF,
  TOGMELDING_PERSONVERN_LINK_TEXT,
  TOGMELDING_SENDER_LINE,
} from './personvern';
import IssueCard from './components/IssueCard';

// Rushvarsel: the rush-hour forecast for the Oslo local network, rendered from
// /togmelding-api/today. Entur-branded and laid out like the Avviksvarsel PWA (/varsel) on
// purpose — a traveller arrives here by tapping a rush-forecast push from that app, and two
// surfaces of one product should not look like two products.

const REFRESH_MS = 5 * 60 * 1000;

export default function App() {
  const [melding, setMelding] = useState<Togmelding | null>(null);
  const [environment, setEnvironment] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(async () => {
    try {
      // Replaced wholesale on every poll. The vanilla page this replaced compared the raw
      // response text first, to avoid rebuilding a card under a half-typed feedback comment;
      // here the widget's state is keyed on the context_id instead (see ForecastFeedback), which
      // the server keeps byte-stable while the forecast is unchanged.
      setMelding(await fetchTogmelding());
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // Which deployment answered — its own endpoint, touching no BigQuery, so the page still says
    // "(dev)" on a day the forecast itself cannot be built. Failure leaves it unlabelled, which
    // is the prd rendering: a stray "(dev)" on the traveller-facing host is worse than none here.
    void fetchConfig()
      .then((config) => setEnvironment(config.environment ?? null))
      .catch(() => undefined);
  }, [load]);

  // Poll, and re-fetch when the page regains focus — this page is typically opened from a push
  // notification and then left sitting on a phone, where a stale level is worse than none.
  useEffect(() => {
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void load();
    };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', onVisible);
    };
  }, [load]);

  const envSuffix = environment ? ` (${environment})` : '';

  useEffect(() => {
    document.title = `Rushvarsel${envSuffix}`;
  }, [envSuffix]);

  const overdue = melding?.overdue_issues ?? [];
  const issues = melding?.issues ?? [];
  // Newest first: the reader's question is "what does it say now", and the follow-up that
  // superseded a baseline is the one that answers it.
  const newestFirst = [...issues].reverse();

  return (
    <>
      <SkipToContent mainId="main-content">Gå til hovedinnhold</SkipToContent>

      <Contrast as="header" className="tm-header">
        <div className="tm-header-inner">
          <Logo />
          <Heading1 as="h1" margin="none" className="tm-header-title">
            Rushvarsel{envSuffix}
          </Heading1>
        </div>
      </Contrast>

      <main id="main-content" className="tm-main">
        {/* Deliberately does not open with the page's own name. It used to read «Rushtidsvarsel
            for lokaltogene …» — the phrase the product name was then drawn from, so keeping it
            would have the intro restate the heading instead of adding to it. It says what the
            forecast covers instead. */}
        <LeadParagraph className="tm-intro">
          Hvordan lokaltogene i Oslo-området (Vy) ligger an i morgen- og ettermiddagsrushet. Nye
          prognoser kl. 06:30, 07:00, 12:00 og 14:00 på hverdager; kl. 07:00 og 14:00 erstatter de
          to tidligere.
          {melding?.generated_at && ` Sist oppdatert ${osloTime(melding.generated_at)}.`}
        </LeadParagraph>

        {/* The published experiment disclaimer, after the intro and before the forecast — the
            placement plan 2026-08-31-001 (U8) chose for this page, kept through the move onto
            Linje, and now the same SmallAlertBox /varsel renders T1 in. Above the loading branch,
            so the caveat is on screen without scrolling or a tap whatever the API is doing. */}
        <SmallAlertBox variant="information" title={TOGMELDING_DISCLAIMER.title} width="fluid">
          {TOGMELDING_DISCLAIMER.body}
        </SmallAlertBox>

        {error && (
          <SmallAlertBox variant="negative" width="fluid">
            Fikk ikke hentet togmeldingen. Prøv igjen om litt.
          </SmallAlertBox>
        )}

        {loading ? (
          <div className="tm-loading">
            <Loader>Laster …</Loader>
          </div>
        ) : (
          melding && (
            <>
              {!melding.forecast_day ? (
                <section className="tm-card">
                  <Heading3 as="h2" margin="none">
                    Ingen prognose i dag
                  </Heading3>
                  <Paragraph margin="none" className="tm-muted">
                    {melding.no_forecast_reason}
                  </Paragraph>
                </section>
              ) : issues.length === 0 && overdue.length > 0 ? (
                // Due but not issued: technical trouble, honestly distinct from "not yet due".
                <section className="tm-card">
                  {melding.next_issue ? (
                    <>
                      <Heading3 as="h2" margin="none">
                        Prognosen kl. {issueTime(overdue[0])} er forsinket
                      </Heading3>
                      <Paragraph margin="none" className="tm-muted">
                        Teknisk trøbbel — prognosen er ikke beregnet ennå. Neste forsøk kommer
                        fortløpende.
                      </Paragraph>
                    </>
                  ) : (
                    <>
                      <Heading3 as="h2" margin="none">
                        Ingen prognose tilgjengelig i dag
                      </Heading3>
                      <Paragraph margin="none" className="tm-muted">
                        Teknisk feil hindret beregningen av dagens prognoser (kl.{' '}
                        {issueTimeList(overdue)}).
                      </Paragraph>
                    </>
                  )}
                </section>
              ) : (
                issues.length === 0 &&
                // Nothing to show, for one of two opposite reasons the reader must be able to
                // tell apart: the day's forecasts are all behind us (their rush has ended and
                // visible_issues retired them), or the next one is still to come.
                (melding.rushes_over ? (
                  <section className="tm-card">
                    <Heading3 as="h2" margin="none">
                      Dagens rush er over
                    </Heading3>
                    <Paragraph margin="none" className="tm-muted">
                      Nytt varsel til morgenrushet neste hverdag, kl. 06:30.
                    </Paragraph>
                  </section>
                ) : (
                  <section className="tm-card">
                    <Heading3 as="h2" margin="none">
                      Neste prognose kommer{' '}
                      {melding.next_issue ? `kl. ${issueTime(melding.next_issue)}` : 'snart'}
                    </Heading3>
                  </section>
                ))
              )}

              {issues.length > 0 && overdue.length > 0 && (
                <SmallAlertBox variant="information" width="fluid">
                  Prognosen kl. {issueTimeList(overdue, ' og kl. ')} mangler (teknisk trøbbel).
                </SmallAlertBox>
              )}

              {newestFirst.map((issue) => (
                <IssueCard key={issue.issue} issue={issue} onRefresh={() => void load()} />
              ))}

              {melding.forecast_day &&
                melding.pm_outlook &&
                !issues.some((issue) => issue.issue >= '1200') && (
                  <section className="tm-card">
                    <Heading3 as="h2" margin="none">
                      Ettermiddagsrushet
                    </Heading3>
                    <Paragraph margin="none">{melding.pm_outlook}</Paragraph>
                  </section>
                )}

              {melding.known_deviations.length > 0 && (
                <section className="tm-card">
                  <Heading3 as="h2" margin="none">
                    Kjente avvik i dag
                  </Heading3>
                  <ul className="tm-kjente">
                    {melding.known_deviations.map((deviation, index) => (
                      <li key={`${deviation.cls}-${index}`} className="tm-kjent">
                        <span className="tm-tag">
                          {deviation.planned ? 'Planlagt' : (CLS_LABEL[deviation.cls] ?? 'Avvik')}
                        </span>
                        <span>{deviation.summary}</span>
                        {(deviation.n_am_rush_journeys > 0 ||
                          deviation.n_pm_rush_journeys > 0) && (
                          <SmallText as="p" className="tm-muted tm-kjent-count">
                            {[
                              deviation.n_am_rush_journeys > 0 &&
                                `${deviation.n_am_rush_journeys} avganger i morgenrushet`,
                              deviation.n_pm_rush_journeys > 0 &&
                                `${deviation.n_pm_rush_journeys} i ettermiddagsrushet`,
                            ]
                              .filter(Boolean)
                              .join(', ')}
                          </SmallText>
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {melding.skill_note && (
                <SmallText as="p" className="tm-muted tm-skill">
                  {melding.skill_note}
                </SmallText>
              )}
            </>
          )
        )}
      </main>

      {/* T7 — the sender line. Outside <main> and outside the loading branch: art. 13(1)(a) makes
          the controller's identity non-optional, so it must be there on a day the forecast is
          not. It is the only route from this page to the privacy page. */}
      <footer className="tm-footer">
        <SubParagraph margin="none">
          {TOGMELDING_SENDER_LINE}{' '}
          <Link href={TOGMELDING_PERSONVERN_HREF}>{TOGMELDING_PERSONVERN_LINK_TEXT}</Link>
        </SubParagraph>
      </footer>
    </>
  );
}
