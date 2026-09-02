import { Heading3, Paragraph, SmallText } from '@entur/typography';
import type { Issue } from '../api';
import {
  capitalize,
  issueTime,
  LEVEL_ADJ,
  LEVEL_LABEL,
  levelClass,
  levelSentence,
  osloTime,
} from '../format';
import ForecastFeedback from './ForecastFeedback';

// One issued forecast. The level is carried by a Linje validation colour on the card's accent bar
// and on the badge (mint / canary / coral, via the semantic tokens — see styles.css): the badge
// keeps dark ink on a light tint at all three levels, which a filled canary swatch cannot do.
//
// The HEADING carries the answer — «Morgenrushet 07:00–09:00 — gult nivå» (design review
// 2026-09-01, findings 24 and 25): heading navigation is how a screen-reader user scans a page, and
// the level stood in no heading, only in a badge marked aria-hidden. The badge stays decorative now
// that the heading says the word; the issue time, which used to lead the heading, is a small line
// under the reasons — it matters for telling a baseline from its follow-up, not for the answer.
// The server mirrors the heading in the feedback snapshot (togvarsel._feedback_snapshot).
export default function IssueCard({ issue, onRefresh }: { issue: Issue; onRefresh: () => void }) {
  return (
    <section className={`tm-card tm-issue tm-level-${levelClass(issue.level)}`}>
      <Heading3 as="h2" margin="none">
        {capitalize(issue.scope)} — {LEVEL_ADJ[issue.level] ?? issue.level} nivå
      </Heading3>

      <div className="tm-issue-level">
        <span className="tm-badge" aria-hidden="true">
          {LEVEL_LABEL[issue.level] ?? '?'}
        </span>
        <Paragraph margin="none" className="tm-issue-verdict">
          {levelSentence(issue.level, issue.p)}
        </Paragraph>
      </div>

      <ul className="tm-reasons">
        {issue.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>

      <SmallText as="p" className="tm-muted tm-issue-when">
        Prognose kl. {issueTime(issue.issue)}
        {issue.late && `, beregnet i etterkant kl. ${osloTime(issue.computed_at)}`}.
      </SmallText>

      {issue.feedback_enabled && issue.feedback_context_id && (
        <ForecastFeedback
          key={issue.feedback_context_id}
          contextId={issue.feedback_context_id}
          onRefresh={onRefresh}
        />
      )}
    </section>
  );
}
