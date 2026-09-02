import { useCallback, useEffect, useState } from 'react';
import { SkipToContent } from '@entur/a11y';
import { SmallAlertBox } from '@entur/alert';
import { ExpandableText } from '@entur/expand';
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
import { fetchConfig, fetchTogmelding, type KnownDeviation, type Togmelding } from './api';
import {
  CLS_LABEL,
  count,
  issueTime,
  issueTimeList,
  osloDateTime,
  relativeTime,
} from './format';
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
//
// Order on the page (design review 2026-09-01, findings 29-31): the level first, then what is
// known, then how fresh it is and how the forecast works, then the way onward. The answer used to
// be the third card, under an intro that held four clock times and the skill note.

const REFRESH_MS = 5 * 60 * 1000;
// Where the pushes that bring people here are registered — and the page's way back (finding 5:
// the only link used to be the privacy footer, which made the page a dead end).
const VARSEL_URL = '/varsel/';

// The «kjente avvik» rows grouped under one tag each (finding 3 and 20): the server already
// collapses messages saying the same thing; this puts the eleven «Kortere tog» rows under one
// «Kortere tog» tag instead of repeating it eleven times beside eleven near-identical summaries.
function groupDeviations(deviations: KnownDeviation[]): { label: string; rows: KnownDeviation[] }[] {
  const groups = new Map<string, KnownDeviation[]>();
  for (const deviation of deviations) {
    const label = deviation.planned ? 'Planlagt' : (CLS_LABEL[deviation.cls] ?? 'Avvik');
    const rows = groups.get(label) ?? [];
    rows.push(deviation);
    groups.set(label, rows);
  }
  return [...groups].map(([label, rows]) => ({ label, rows }));
}

// «11 avganger i morgenrushet, 3 i ettermiddagsrushet, 11 meldinger» — with agreement (finding 4).
function deviationCounts(deviation: KnownDeviation): string | null {
  const parts: string[] = [];
  if (deviation.n_am_rush_journeys > 0) {
    parts.push(`${count(deviation.n_am_rush_journeys, 'avgang', 'avganger')} i morgenrushet`);
  }
  if (deviation.n_pm_rush_journeys > 0) {
    parts.push(
      parts.length > 0
        ? `${deviation.n_pm_rush_journeys} i ettermiddagsrushet`
        : `${count(deviation.n_pm_rush_journeys, 'avgang', 'avganger')} i ettermiddagsrushet`,
    );
  }
  if ((deviation.n_situations ?? 1) > 1) {
    parts.push(count(deviation.n_situations!, 'melding', 'meldinger'));
  }
  return parts.length > 0 ? parts.join(', ') : null;
}

// A summary that only repeats its own tag («Buss for tog» under «Buss for tog») says nothing the
// tag has not said (finding 20) — the row then carries the counts alone.
function summaryLine(deviation: KnownDeviation, label: string): string | null {
  const summary = deviation.summary?.trim();
  if (!summary) return null;
  return summary.toLowerCase() === label.toLowerCase() ? null : summary;
}

export default function App() {
  const [melding, setMelding] = useState<Togmelding | null>(null);
  const [environment, setEnvironment] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  // For «oppdatert for 8 minutter siden»: ticks once a minute so the relative time ages between
  // polls instead of jumping at each one.
  const [now, setNow] = useState(() => new Date());

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
      setNow(new Date());
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
    const tick = window.setInterval(() => setNow(new Date()), 60_000);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void load();
    };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', onVisible);
    return () => {
      window.clearInterval(timer);
      window.clearInterval(tick);
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
  const deviationGroups = groupDeviations(melding?.known_deviations ?? []);

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
        <nav className="tm-nav" aria-label="Til Avviksvarsel">
          <SmallText>
            <Link href={VARSEL_URL}>‹ Til Avviksvarsel</Link>
          </SmallText>
        </nav>

        {/* One sentence on what the forecast covers and one on when it changes. The four clock
            times and the skill note that used to be in here are under «Om prognosen» below. It
            still does not open with the page's own name (see the 2026-09-01 rename). */}
        <LeadParagraph className="tm-intro">
          Hvordan lokaltogene i Oslo-området (Vy) ligger an i morgen- og ettermiddagsrushet.
          Oppdateres før begge rushene på hverdager.
        </LeadParagraph>

        {/* The published experiment disclaimer, after the intro and before the forecast — the
            placement plan 2026-08-31-001 (U8) chose for this page: above the loading branch, so the
            caveat is on screen without scrolling or a tap whatever the API is doing. One line and no
            title since 2026-09-02, so the answer below it is still on the first screen. */}
        <SmallAlertBox variant="information" width="fluid">
          {TOGMELDING_DISCLAIMER.body}
        </SmallAlertBox>

        {error && (
          <SmallAlertBox variant="negative" width="fluid">
            Fikk ikke hentet rushvarselet. Prøv igjen om litt.
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

              {deviationGroups.length > 0 && (
                <section className="tm-card">
                  <Heading3 as="h2" margin="none">
                    Kjente avvik i dag
                  </Heading3>
                  <ul className="tm-kjente">
                    {deviationGroups.map((group) => (
                      <li key={group.label} className="tm-kjent-group">
                        <span className="tm-tag">{group.label}</span>
                        <ul className="tm-kjent-rows">
                          {group.rows.map((deviation, index) => {
                            const summary = summaryLine(deviation, group.label);
                            const counts = deviationCounts(deviation);
                            return (
                              <li key={`${deviation.cls}-${index}`} className="tm-kjent">
                                {summary && <span>{summary}</span>}
                                {counts && (
                                  <SmallText as="p" className="tm-muted tm-kjent-count">
                                    {counts}
                                  </SmallText>
                                )}
                              </li>
                            );
                          })}
                        </ul>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {/* Freshness is the whole point of a forecast, and «Sist oppdatert 22:14» looked the
                  same today and last week (finding 32): relative time first, the date in brackets.
                  The schedule and the skill note fold under «Om prognosen» (findings 30 and 9 —
                  rates in the note, never multipliers). */}
              <div className="tm-meta">
                {melding.generated_at && (
                  <SmallText as="p" className="tm-muted tm-updated">
                    Oppdatert {relativeTime(melding.generated_at, now)} (
                    {osloDateTime(melding.generated_at)}).
                  </SmallText>
                )}
                <ExpandableText title="Om prognosen" titleElement="SubParagraph">
                  <Paragraph margin="none">
                    Nye prognoser kl. 06:30 og 07:00 for morgenrushet og kl. 12:00 og 14:00 for
                    ettermiddagsrushet, på hverdager. Prognosen kl. 07:00 erstatter den fra
                    06:30, og den kl. 14:00 erstatter den fra 12:00.
                  </Paragraph>
                  {melding.skill_note && <Paragraph margin="none">{melding.skill_note}</Paragraph>}
                </ExpandableText>
              </div>

              {/* The way onward (finding 5): this is the page people share, and it had no route to
                  the registration that produces the pushes it shows. */}
              <Paragraph margin="none" className="tm-cta">
                <Link href={VARSEL_URL}>Få rushvarselet som varsel på telefonen</Link> — registrer
                reisene dine i Avviksvarsel.
              </Paragraph>
            </>
          )
        )}
      </main>

      {/* T7 — the sender line. Outside <main> and outside the loading branch: art. 13(1)(a) makes
          the controller's identity non-optional, so it must be there on a day the forecast is
          not. */}
      <footer className="tm-footer">
        <SubParagraph margin="none">
          {TOGMELDING_SENDER_LINE}{' '}
          <Link href={TOGMELDING_PERSONVERN_HREF}>{TOGMELDING_PERSONVERN_LINK_TEXT}</Link>
        </SubParagraph>
      </footer>
    </>
  );
}
