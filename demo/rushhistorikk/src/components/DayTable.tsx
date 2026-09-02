import type { ReactNode } from 'react';
import type { DayRow, IssueRow, IssueSlot, Level } from '../api';
import {
  DASH,
  ISSUE_SLOTS,
  SLOT_LABEL,
  VERDICT_HELP,
  VERDICT_LABEL,
  VERDICT_VARIANT,
  fmtDate,
  fmtPct,
  fmtShare,
  fmtTime,
  fmtWeekday,
  levelClass,
  levelLabel,
} from '../format';

/** A rush level as a ringed pill, or a bare en dash when there is no level.
 *
 *  Not a pill around a dash: the ring is the page's statement that a level exists, and an empty one
 *  reads as a fourth level. */
function LevelChip({ level }: { level: Level | null }) {
  if (!level) return <span className="rh-num">{DASH}</span>;
  return <span className={levelClass(level)}>{levelLabel(level)}</span>;
}

/** What the traveller was actually told at this issue, as a short marker plus the sentence behind
 *  it. Null when nothing was communicated and nothing stood — the ordinary quiet weekday, where a
 *  marker would be noise on every one of four cells.
 *
 *  «Sto ved lag» is the case that is easy to render as silence and must not be: a confirming
 *  follow-up pushes nothing precisely because the baseline's warning still holds, so the traveller
 *  was covered. That is also the level the verdict grades — the baseline's, not this issue's own. */
function communicated(issue: IssueRow): { text: string; title: string } | null {
  if (issue.sent) {
    const when = issue.decided_at ? ` kl. ${fmtTime(issue.decided_at)}` : '';
    if (issue.kind === 'warning') {
      return { text: 'Varsel', title: `Rushvarsel sendt${when}.` };
    }
    if (issue.kind === 'update') {
      return {
        text: 'Oppdatert',
        title: `Oppdatert rushvarsel sendt${when} — nivået hadde endret seg siden grunnvarselet.`,
      };
    }
    if (issue.kind === 'all_clear') {
      return {
        text: 'Friskmelding',
        title: `Friskmelding sendt${when} — varselet ble trukket tilbake fordi nivået falt til grønt.`,
      };
    }
    return null;
  }
  if (issue.baseline_level && issue.reason === 'no_change') {
    return {
      text: 'Sto ved lag',
      title:
        `Ingen ny push: nivået var uendret siden grunnvarselet, så varselet på ` +
        `${levelLabel(issue.baseline_level)} nivå sto fortsatt i kraft. Den reisende var altså ` +
        `dekket, og utfallet er vurdert mot det nivået — ikke mot denne utstedelsens eget.`,
    };
  }
  // Its own marker, and for the same reason the one above exists: an untimely round with a
  // standing baseline is ALSO graded against the baseline's level, so without this the cell shows
  // a verdict derived from a level it never mentions. It is not the same news, though — nothing
  // went out because the round was evaluated too late, and where a friskmelding was the one owed
  // the verdict is «bom» however calm the rush turned out.
  if (issue.baseline_level && issue.reason === 'untimely') {
    return {
      text: 'For sent',
      title:
        `Runden ble vurdert etter leveringsfristen, så ingen push kunne gå ut. Varselet på ` +
        `${levelLabel(issue.baseline_level)} nivå fra grunnvarselet sto dermed uendret, og ` +
        `utfallet er vurdert mot det nivået.`,
    };
  }
  return null;
}

/** One issue cell: the verdict first and primary, everything else on one subordinate line.
 *
 *  The hierarchy is deliberate. The verdict answers the page's question, and four same-weight
 *  fragments across four cells and hundreds of rows is a wall nobody reads. */
function IssueCell({ issue }: { issue: IssueRow | undefined }) {
  if (!issue) {
    // No forecast was issued for this slot — a failed rebuild, or a day before the slot existed.
    return <td className="rh-issue rh-issue--empty">{DASH}</td>;
  }
  const told = communicated(issue);
  return (
    <td className="rh-issue">
      <div className="rh-issue-verdict">
        <span
          className={`rh-badge rh-badge--${VERDICT_VARIANT[issue.verdict]}`}
          title={VERDICT_HELP[issue.verdict]}
        >
          {VERDICT_LABEL[issue.verdict]}
        </span>
      </div>
      <div className="rh-issue-detail">
        <LevelChip level={issue.forecast_level} />
        <span className="rh-num" title="Modellens sannsynlighet for mye trøbbel i dette rushet.">
          {fmtPct(issue.p)}
        </span>
        {told ? <span title={told.title}>{told.text}</span> : null}
        {issue.late ? (
          <span
            className="rh-tag"
            title="Prognosen ble regnet ut mer enn 10 min etter sin egen frist, så den så virkeligheten etter fristen. Holdt utenfor Presisjon, men med i Dekning."
          >
            sen
          </span>
        ) : null}
        {issue.provisional ? (
          <span
            className="rh-tag"
            title="Fasit er dagens egen foreløpige lesning og kan endre seg over natten. Ikke med i tabellen over."
          >
            foreløpig
          </span>
        ) : null}
      </div>
    </td>
  );
}

/** A realized rush: its level, the share behind it, and the provisional caveat when the same-day
 *  reading said something else. */
function RushCell({
  level,
  trouble10,
  provisionalLevel,
  children,
}: {
  level: Level | null;
  trouble10: number | null;
  provisionalLevel: Level | null;
  children?: ReactNode;
}) {
  // Only when the same-day reading DISAGREES: a provisional value that survived the night says
  // nothing a reader needs, and a tag on every row would stop being read.
  const moved = provisionalLevel !== null && provisionalLevel !== level;
  return (
    <td>
      <div className="rh-issue-detail">
        <LevelChip level={level} />
        <span
          className="rh-num"
          title="Andel avganger som var innstilt eller mer enn 10 min forsinket, av de oppløste avgangene."
        >
          {fmtShare(trouble10)}
        </span>
        {moved ? (
          <span
            className="rh-tag"
            title={`Samme dag viste siden ${levelLabel(provisionalLevel)} nivå. Fasit ble satt over natten og står nå på ${levelLabel(level)}.`}
          >
            endret
          </span>
        ) : null}
      </div>
      {children}
    </td>
  );
}

function DayCells({ day }: { day: DayRow }) {
  const bySlot = new Map<IssueSlot, IssueRow>(day.issues.map((i) => [i.issue, i]));
  return (
    <tr>
      <td>
        <span className="rh-day-date">{fmtDate(day.d)}</span>
        <span className="rh-day-weekday">{fmtWeekday(day.d)}</span>
        {day.eligible === false ? (
          <span
            className="rh-tag"
            title="Dagen har for få oppløste avganger (treningskjedens test: over 300 i både morgen- og ettermiddagsrushet) til at andelen betyr noe. Dagen får derfor «Ukjent» og teller ikke i noen av tallene."
          >
            ikke kvalifisert
          </span>
        ) : null}
      </td>
      {ISSUE_SLOTS.map((slot) => (
        <IssueCell key={slot} issue={bySlot.get(slot)} />
      ))}
      <RushCell
        level={day.am_level}
        trouble10={day.am_trouble10}
        provisionalLevel={day.am_provisional_level}
      >
        {/* The 0700 issue is scored against 07:00-09:00, not the whole morning, so both morning
            figures are shown: one column carrying only the wider window would let the two silently
            disagree about the same rush. */}
        <div className="rh-issue-detail">
          <span>07–09</span>
          <LevelChip level={day.amrest_level} />
        </div>
      </RushCell>
      <RushCell
        level={day.pm_level}
        trouble10={day.pm_trouble10}
        provisionalLevel={day.pm_provisional_level}
      />
    </tr>
  );
}

/** One row per day, newest first — the API already sorts, so this never re-sorts.
 *
 *  A real table rather than /historikk's cards: the unit of this product is a DAY's rush, and the
 *  four issues about it are only interpretable next to each other and next to the day above (KD1).
 *  It scrolls inside its own container so the page body never scrolls sideways. */
export default function DayTable({ days }: { days: DayRow[] }) {
  if (days.length === 0) return null;
  return (
    <div className="rh-scroll">
      <table className="rh-table">
        <thead>
          <tr>
            <th scope="col">Dato</th>
            {ISSUE_SLOTS.map((slot) => (
              <th key={slot} scope="col">
                {SLOT_LABEL[slot]}
              </th>
            ))}
            <th scope="col">Morgenrush</th>
            <th scope="col">Ettermiddagsrush</th>
          </tr>
        </thead>
        <tbody>
          {/* Keyed on the Oslo date, which is this response's real grain: one row per day. */}
          {days.map((day) => (
            <DayCells key={day.d} day={day} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
