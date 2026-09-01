import { Link, Paragraph, SmallText } from '@entur/typography';
import type { RushForecast } from '../api';
import { RUSH_NAME } from '../format';
import type { Brand } from '../brand';

// Today's Oslo rush-hour levels, at the top of the page — but only for a device the server
// actually sends these forecasts to. /togmelding-api/outlook decides that (its own registrations,
// the Oslo geography, the delivery flag) and returns [] otherwise, so an empty list is the
// ordinary state and renders nothing at all: no heading, no empty card, no explanation of a
// feature this traveller does not have.
//
// A rush is dropped once its window has ended rather than when the traveller's own window has,
// so a 07:00–08:00 registration still reads the morning's level at 08:15 with the trains still
// late. The card shows the level and links onward; the reasons, the percentage's history and the
// "kjente avvik" list live on /togmelding, which is one tap away.
//
// The level colours are Linje validation tokens (see styles.css), the same three the level cards
// on /togmelding use, so the two surfaces cannot drift into different greens. The level TEXT below
// is a third copy of the same three sentences (togvarsel._LEVEL_TEXT and
// demo/togmelding/src/format.ts are the other two) — the mirror test pins the /togmelding one,
// because that is what the persisted feedback snapshot has to match; reword this one with them.

const LEVEL_LABEL: Record<string, string> = {
  green: 'GRØNT',
  yellow: 'GULT',
  red: 'RØDT',
};

const LEVEL_TEXT: Record<string, string> = {
  green: 'liten fare for forstyrrelser',
  yellow: 'noe usikkert',
  red: 'stor fare for forstyrrelser',
};

export default function RushOutlook({
  rushes,
  brand,
}: {
  rushes: RushForecast[];
  brand: Brand;
}) {
  if (rushes.length === 0) return null;

  return (
    <section className="av-card av-rush" aria-labelledby="rush-outlook">
      <SmallText as="h2" id="rush-outlook" className="av-rush-head">
        Togtrafikken i Oslo-området i dag
      </SmallText>

      <ul className="av-rush-list">
        {rushes.map((rush) => (
          <li key={rush.rush} className={`av-rush-row av-rush-${rush.level}`}>
            <span className="av-rush-badge">{LEVEL_LABEL[rush.level] ?? '?'}</span>
            <div className="av-rush-text">
              <Paragraph margin="none" className="av-rush-name">
                {RUSH_NAME[rush.rush] ?? rush.scope}
              </Paragraph>
              <SmallText className="av-muted">
                {LEVEL_TEXT[rush.level] ?? rush.level} — {rush.scope}
              </SmallText>
            </div>
          </li>
        ))}
      </ul>

      {/* Entur-branded only, exactly like the pushes: the unbranded variant's click-through
          deliberately never leaves /varsel, so it gets the levels without the link. */}
      {brand.id === 'entur' && (
        <SmallText>
          <Link href="/togmelding">Se hele rushvarselet</Link>
        </SmallText>
      )}
    </section>
  );
}
