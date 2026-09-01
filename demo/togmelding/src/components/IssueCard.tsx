import { Heading3, Paragraph, SmallText } from '@entur/typography';
import type { Issue } from '../api';
import { issueTime, LEVEL_LABEL, levelClass, levelSentence, osloTime } from '../format';
import ForecastFeedback from './ForecastFeedback';

// One issued forecast. The level is carried by a Linje validation colour on the card's accent bar
// and on the badge (mint / canary / coral, via the semantic tokens — see styles.css): the badge
// keeps dark ink on a light tint at all three levels, which a filled canary swatch cannot do.
export default function IssueCard({ issue, onRefresh }: { issue: Issue; onRefresh: () => void }) {
  return (
    <section className={`tm-card tm-issue tm-level-${levelClass(issue.level)}`}>
      <Heading3 as="h2" margin="none">
        Kl. {issueTime(issue.issue)} — {issue.scope}
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

      {issue.late && (
        <SmallText as="p" className="tm-muted">
          Beregnet i etterkant, kl. {osloTime(issue.computed_at)}.
        </SmallText>
      )}

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
