"""Checks for rushhistorikk_verdicts — /rushhistorikk's verdict taxonomy and stats-table metrics.

Hermetic and pure. The module under test does no I/O at all, so nothing here touches BigQuery, the
network or the wall clock; every input is a plain dataclass and every expectation is a literal.

The specification is the fourteen-row decision table and the three named populations in
docs/plans/2026-09-01-001-feat-rushhistorikk-forecast-scoring-plan.md (High-Level Technical Design,
KTD5-KTD8). It is transcribed below one test per row, named for the row, because this is the
artefact most likely to be got subtly wrong and the cheapest place to prove right: everything
above it — the SQL, the API, the page — only renders what these functions decide.

Run: uv run python test_rushhistorikk_verdicts.py  (or under pytest)
"""

from __future__ import annotations

import itertools
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import rushhistorikk_verdicts as rv

_OSLO = ZoneInfo("Europe/Oslo")

# 2026-08-31 is a Monday.
DAY = date(2026, 8, 31)
NEXT_DAY = date(2026, 9, 1)


def _at(hour: int, minute: int, *, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_OSLO)


def _issue(**overrides) -> rv.Issue:
    """A scored round record: recorded, eligible, settled, green forecast, green rush.

    Every table row below is this with the columns of that row overridden, so a test names only
    what it is about — the same shape test_togvarsel.py's _feature_row uses.
    """
    base = {
        "d": DAY,
        "issue": "0630",
        "level": "green",
        "recorded": True,
        "eligible": True,
        "outcome": rv.OUTCOME_SETTLED,
        "actual": "green",
    }
    base.update(overrides)
    return rv.Issue(**base)


# ---- The fourteen-row table, one test per row -------------------------------------------
#
# | #  | Record  | Eligible | A            | sent | L             | Standing | Verdict        |
# |  1 | absent  | -        | -            | -    | -             | -        | ikke vurdert   |
# |  2 | present | no       | -            | -    | -             | -        | ukjent         |
# |  3 | present | yes      | unresolvable | -    | -             | -        | ukjent         |
# |  4 | present | yes      | not settled  | -    | -             | -        | venter         |
# |  5 | present | yes      | known        | yes  | yellow or red | -        | grade L vs A   |
# |  6 | present | yes      | green        | yes  | green         | -        | traff          |
# |  7 | present | yes      | yel. or red  | yes  | green         | -        | falsk alarm    |
# |  8 | present | yes      | known        | no   | yellow or red | yes      | grade B vs A   |
# |  9 | present | yes      | green        | no   | green         | yes      | bom            |
# | 10 | present | yes      | yel. or red  | no   | green         | yes      | bom            |
# | 11 | present | yes      | green        | no   | green         | no       | traff          |
# | 12 | present | yes      | yel. or red  | no   | green         | no       | bom            |
# | 13 | present | yes      | green        | no   | yellow or red | no       | traff          |
# | 14 | present | yes      | yel. or red  | no   | yellow or red | no       | bom            |


def test_row_1_no_round_record_is_ikke_vurdert():
    """Row 1. The gate was closed, VAPID was absent, or the day predates the round table. Nothing
    about the levels can rescue it: no round was ever evaluated, so there is nothing to grade."""
    for level, actual in itertools.product(rv.LEVELS, rv.LEVELS):
        issue = _issue(recorded=False, level=level, actual=actual, sent=True)
        assert rv.classify(issue) == rv.IKKE_VURDERT, (level, actual)
        assert rv.population(issue) == rv.BUCKET_IKKE_VURDERT


def test_row_2_an_ineligible_day_is_ukjent():
    """Row 2. The day fails the training chain's day-level call-volume test, so its realized share
    means nothing — and eligibility is asked BEFORE the outcome, since a share computed over 40
    calls is not a rush that can be scored however cleanly it settled."""
    issue = _issue(eligible=False, level="red", sent=True, actual="green")
    assert rv.classify(issue) == rv.UKJENT
    assert rv.population(issue) == rv.BUCKET_INELIGIBLE


def test_row_3_an_unresolvable_outcome_is_ukjent():
    """Row 3. The source can never establish this rush — the batch landed with a zero denominator,
    or the ten-day retry window is spent. Distinct from row 4 on purpose: one is over, one is not."""
    issue = _issue(outcome=rv.OUTCOME_UNRESOLVABLE, actual=None, level="red", sent=True)
    assert rv.classify(issue) == rv.UKJENT
    assert rv.population(issue) == rv.BUCKET_UKJENT


def test_row_4_an_unsettled_outcome_is_venter():
    """Row 4. Not yet confirmed against last_recorded — including the provisional case, which the
    stats table excludes and the day row shows marked."""
    issue = _issue(outcome=rv.OUTCOME_PENDING, actual="yellow", level="red", sent=True)
    assert rv.classify(issue) == rv.VENTER
    assert rv.population(issue) == rv.BUCKET_VENTER


def test_row_5_a_warning_push_is_graded_on_its_own_level():
    """Row 5. A push went out saying yellow or red: what was communicated is this issue's own
    level, so that is what is graded."""
    assert rv.classify(_issue(sent=True, level="yellow", actual="yellow")) == rv.TRAFF
    assert rv.classify(_issue(sent=True, level="red", actual="red")) == rv.TRAFF
    assert rv.classify(_issue(sent=True, level="red", actual="yellow")) == rv.TRAFF_NESTEN
    assert rv.classify(_issue(sent=True, level="yellow", actual="red")) == rv.TRAFF_NESTEN
    assert rv.classify(_issue(sent=True, level="yellow", actual="green")) == rv.FALSK_ALARM
    assert rv.classify(_issue(sent=True, level="red", actual="green")) == rv.FALSK_ALARM


def test_row_6_an_all_clear_on_a_green_rush_is_traff():
    """Row 6. The friskmelding was right. A green PUSH is the all-clear — the only green that ever
    pushes — and it is graded as a claim about the rush, not through `grade`."""
    issue = _issue(issue="0700", sent=True, kind="all_clear", level="green",
                   baseline_level="yellow", actual="green")
    assert rv.classify(issue) == rv.TRAFF


def test_row_7_an_all_clear_on_a_rush_that_was_not_green_is_falsk_alarm():
    """Row 7. We told the traveller it was over and it was not. The wrong direction of error, and
    the reason an all-clear is never coverage: it retracts a warning rather than issuing one."""
    for actual in ("yellow", "red"):
        issue = _issue(issue="1400", sent=True, kind="all_clear", level="green",
                       baseline_level="red", actual=actual)
        assert rv.classify(issue) == rv.FALSK_ALARM, actual


def test_row_8_an_unsent_follow_up_is_graded_on_the_standing_level():
    """Row 8, the common one — every confirming follow-up. Nothing was sent here, but the
    traveller is warned, at the BASELINE's level. That is what gets graded; see the KTD6 case
    below for why grading this issue's own forecast would credit a warning nobody received."""
    issue = _issue(issue="0700", sent=False, reason="no_change", level="yellow",
                   baseline_level="yellow", actual="yellow")
    assert rv.classify(issue) == rv.TRAFF
    swapped = _issue(issue="0700", sent=False, reason="no_change", level="red",
                     baseline_level="yellow", actual="red")
    assert rv.classify(swapped) == rv.TRAFF_NESTEN  # graded on the standing yellow, not the red
    wrong = _issue(issue="0700", sent=False, reason="no_change", level="yellow",
                   baseline_level="yellow", actual="green")
    assert rv.classify(wrong) == rv.FALSK_ALARM


def test_row_9_an_owed_friskmelding_that_never_went_out_is_bom_on_a_green_rush():
    """Row 9. Green forecast, a warning still standing, and no all-clear sent — reachable only on
    an untimely round, since before the deadline a green follow-up after a warned baseline always
    pushes. The rush being green does not redeem it: the traveller still holds a warning."""
    issue = _issue(issue="1400", sent=False, reason="untimely", level="green",
                   baseline_level="red", actual="green")
    assert rv.classify(issue) == rv.BOM


def test_row_10_an_owed_friskmelding_that_never_went_out_is_bom_on_a_bad_rush():
    """Row 10. Same owed friskmelding, bad rush. Bom either way — rows 9 and 10 agree, which is
    what makes "an owed notification did not go out" the thing being scored."""
    for actual in ("yellow", "red"):
        issue = _issue(issue="0700", sent=False, reason="untimely", level="green",
                       baseline_level="yellow", actual=actual)
        assert rv.classify(issue) == rv.BOM, actual


def test_row_11_green_forecast_green_rush_and_nothing_standing_is_traff():
    """Row 11. Nothing was communicated and nothing was warranted — the ordinary calm weekday, and
    82 % of days by KTD3's measurement. `traff`: the right class, silence included. It was its own
    verdict, `riktig stille`, until 2026-09-02, which kept the one verdict a calm month produces out
    of Presisjon's numerator and read the figure as 0 % on a good month."""
    issue = _issue(sent=False, reason="green_baseline", level="green", actual="green")
    assert rv.classify(issue) == rv.TRAFF


def test_row_12_green_forecast_on_a_bad_rush_is_bom():
    """Row 12. Green was forecast, nothing was in force, and the rush was bad: the miss the
    product exists to avoid."""
    for actual in ("yellow", "red"):
        issue = _issue(sent=False, reason="green_baseline", level="green", actual=actual)
        assert rv.classify(issue) == rv.BOM, actual


def test_row_13_a_suppressed_warning_on_a_green_rush_is_traff():
    """Row 13. The forecast said yellow or red, nothing reached the traveller and nothing needed
    to. Graded on what was communicated (nothing) rather than on the forecast — silence was right,
    whatever suppressed it — so the same `traff` as row 11, never a `falsk alarm` on a warning
    nobody received."""
    for level in ("yellow", "red"):
        issue = _issue(issue="1400", sent=False, reason="untimely", level=level, actual="green")
        assert rv.classify(issue) == rv.TRAFF, level


def test_row_14_a_suppressed_warning_on_a_bad_rush_is_bom():
    """Row 14. A warning was warranted and none was in force. Bom whatever suppressed it —
    untimely included, whose cause is infrastructural rather than a forecast error, which is why
    n_untimely is counted and shown beside the rates instead of the rows being filtered out."""
    for level, actual in itertools.product(("yellow", "red"), ("yellow", "red")):
        issue = _issue(issue="1400", sent=False, reason="untimely", level=level, actual=actual)
        assert rv.classify(issue) == rv.BOM, (level, actual)


# ---- grade, totality, and the KTD6 cases ------------------------------------------------


def test_grade_is_one_rule_used_twice():
    """The rule rows 5 and 8 share: equal is traff, a yellow/red swap is traff nesten, anything
    else — a graded green, or a green rush — is falsk alarm."""
    assert rv.grade("yellow", "yellow") == rv.TRAFF
    assert rv.grade("red", "red") == rv.TRAFF
    assert rv.grade("green", "green") == rv.TRAFF
    assert rv.grade("red", "yellow") == rv.TRAFF_NESTEN
    assert rv.grade("yellow", "red") == rv.TRAFF_NESTEN
    assert rv.grade("yellow", "green") == rv.FALSK_ALARM
    assert rv.grade("red", "green") == rv.FALSK_ALARM
    assert rv.grade("green", "yellow") == rv.FALSK_ALARM
    assert rv.grade("green", "red") == rv.FALSK_ALARM


def _every_reachable_issue():
    """Every combination of the columns the table reads, plus the two KTD7 exclusions and the
    fields the metrics read. Levels are drawn from the closed vocabulary only — a malformed level
    is not a reachable round, since togvarsel_notify records the level decide_issue validated."""
    for (recorded, eligible, outcome, actual, sent, level, baseline, late, reason, recon) in (
        itertools.product(
            (True, False),
            (True, False, None),
            rv.OUTCOME_STATES,
            (*rv.LEVELS, None),
            (True, False),
            rv.LEVELS,
            (None, *rv.LEVELS),
            (True, False),
            (None, "untimely", "no_change", "green_baseline", "yellow_baseline"),
            (True, False),
        )
    ):
        yield rv.Issue(
            d=DAY, issue="0700", level=level, recorded=recorded, eligible=eligible,
            outcome=outcome, actual=actual, sent=sent, baseline_level=baseline, late=late,
            reason=reason, decided_at=_at(7, 1) if sent else None, reconstructed=recon,
        )


def test_the_taxonomy_is_total_and_disjoint_and_the_counts_reconcile():
    """R13. Every reachable combination yields exactly one verdict from the closed set and never
    raises; every one lands in exactly one population bucket; and the seven bucket counts sum to
    n. Reconciliation is the property that makes the exclusion counts beside the rates readable —
    a row that silently belonged to two buckets, or to none, would make them lie."""
    issues = list(_every_reachable_issue())
    assert len(issues) > 5000  # the enumeration is real, not a two-case stub
    for issue in issues:
        verdict = rv.classify(issue)
        assert verdict in rv.VERDICTS, (issue, verdict)
        assert rv.classify(issue) == verdict  # deterministic: no hidden state
        bucket = rv.population(issue)
        assert bucket in rv.BUCKETS, (issue, bucket)
        # The unscoreable verdicts and the unscoreable buckets are the same rows, from the top
        # four rows of the table. A scored, late or untimely issue always carries a real verdict.
        unscoreable = {rv.IKKE_VURDERT, rv.UKJENT, rv.VENTER}
        assert (verdict in unscoreable) == (bucket not in rv.DELIVERED_BUCKETS), (issue, verdict)

    counts = rv.summarize(issues).total.counts
    assert counts.n == len(issues)
    assert (
        counts.n_ikke_vurdert + counts.n_ineligible + counts.n_ukjent + counts.n_venter
        + counts.n_late + counts.n_untimely + counts.n_scored
    ) == counts.n
    assert counts.n_scored > 0 and counts.n_late > 0 and counts.n_untimely > 0


def test_a_confirming_follow_up_grades_the_standing_yellow_not_its_own_red():
    """KTD6's case, and the one this whole taxonomy exists for. 12:00 pushed yellow; the 14:00
    round sends nothing while forecasting red; the rush was yellow. The traveller holds a yellow
    warning that was exactly right, so this is traff — grading the red nobody received would
    report traff nesten and mark a correct product as nearly wrong.

    classify never reads `reason`: whatever suppressed the round, the level in force is what was
    communicated. The reason is recorded for the exclusion counts, not for the grade."""
    for reason in ("no_change", "untimely"):
        issue = _issue(issue="1400", sent=False, reason=reason, level="red",
                       baseline_level="yellow", actual="yellow")
        assert rv.classify(issue) == rv.TRAFF, reason


def test_a_confirming_follow_up_whose_rush_was_green_is_falsk_alarm():
    """The same standing warning, on a rush that turned out fine: the warning in force was wrong,
    so the issue that let it stand carries that."""
    issue = _issue(issue="0700", sent=False, reason="no_change", level="yellow",
                   baseline_level="yellow", actual="green")
    assert rv.classify(issue) == rv.FALSK_ALARM


def test_the_buckets_are_resolved_in_the_tables_order():
    """The exclusions are disjoint because they are ordered, not because they cannot co-occur: a
    late, untimely, unsettled forecast on an ineligible day with no round record is all six at
    once. Each assertion below adds the next disqualifier and shows the earlier one still wins."""
    kwargs = {
        "issue": "1400", "level": "red", "sent": False, "reason": "untimely", "late": True,
        "actual": None, "outcome": rv.OUTCOME_PENDING, "eligible": False, "recorded": False,
    }
    assert rv.population(_issue(**kwargs)) == rv.BUCKET_IKKE_VURDERT
    assert rv.population(_issue(**{**kwargs, "recorded": True})) == rv.BUCKET_INELIGIBLE
    eligible = {**kwargs, "recorded": True, "eligible": True}
    assert rv.population(
        _issue(**{**eligible, "outcome": rv.OUTCOME_UNRESOLVABLE})
    ) == rv.BUCKET_UKJENT
    assert rv.population(_issue(**eligible)) == rv.BUCKET_VENTER
    settled = {**eligible, "outcome": rv.OUTCOME_SETTLED, "actual": "red"}
    assert rv.population(_issue(**settled)) == rv.BUCKET_LATE  # late outranks untimely
    assert rv.population(_issue(**{**settled, "late": False})) == rv.BUCKET_UNTIMELY
    assert rv.population(_issue(**{**settled, "late": False, "reason": None})) == rv.BUCKET_SCORED




def test_resolve_outcome_state_gives_up_after_the_retry_window():
    """R21's ten days, in one place so the API does not re-invent it. Unresolvable settles
    immediately; an unsettled day inside the window is still being retried (venter); past it the
    attempts have given up (ukjent) — /historikk's own venter-vs-ukjent distinction."""
    assert rv.RETRY_DAYS == 10
    assert rv.resolve_outcome_state(settled=True, unresolvable=False, d=DAY, today=DAY) == (
        rv.OUTCOME_SETTLED
    )
    assert rv.resolve_outcome_state(settled=False, unresolvable=True, d=DAY, today=DAY) == (
        rv.OUTCOME_UNRESOLVABLE
    )
    inside = date(2026, 9, 10)  # ten days on
    assert rv.resolve_outcome_state(settled=False, unresolvable=False, d=DAY, today=inside) == (
        rv.OUTCOME_PENDING
    )
    past = date(2026, 9, 11)
    assert rv.resolve_outcome_state(settled=False, unresolvable=False, d=DAY, today=past) == (
        rv.OUTCOME_UNRESOLVABLE
    )


def test_the_labels_cover_every_verdict():
    """The Norwegian display labels live here and the front-end mirrors them, the way
    demo/varselhistorikk/src/format.ts mirrors /historikk's four. A verdict added without a label
    would render as a raw enum on the page."""
    assert set(rv.VERDICT_LABEL) == set(rv.VERDICTS)
    assert rv.VERDICT_LABEL[rv.TRAFF] == "Traff"
    assert rv.VERDICT_LABEL[rv.TRAFF_NESTEN] == "Traff nesten"
    assert rv.VERDICT_LABEL[rv.FALSK_ALARM] == "Falsk alarm"
    assert rv.VERDICT_LABEL[rv.BOM] == "Bom"
    assert rv.VERDICT_LABEL[rv.VENTER] == "Venter"
    assert rv.VERDICT_LABEL[rv.UKJENT] == "Ukjent"
    assert rv.VERDICT_LABEL[rv.IKKE_VURDERT] == "Ikke vurdert"
    # Folded into traff on 2026-09-02. Reintroducing it takes Presisjon back to 0 % on a calm month,
    # so it has to be deliberate rather than a leftover that crept back.
    assert "riktig_stille" not in rv.VERDICTS and not hasattr(rv, "RIKTIG_STILLE")


# ---- The metrics ------------------------------------------------------------------------


def test_no_round_record_enters_no_denominator():
    """Row 1 is counted and enters nothing else: defaulting those to bom would manufacture failures
    out of a deployment setting, and counting them in any rate would too. Nothing is sent without a
    round, so Varsler sendt is zero here by construction rather than by a rule."""
    stats = rv.summarize([_issue(recorded=False, level="red", actual="red")])
    row = stats.total
    assert row.counts.n == 1 and row.counts.n_ikke_vurdert == 1
    assert row.varsler_sendt == 0
    assert row.presisjon is None and row.presisjon_den == 0
    assert row.dekning is None and row.dekning_den == 0
    assert row.varslingstid_min is None and row.varslingstid_n == 0


def test_a_late_forecast_leaves_presisjon_and_stays_in_dekning():
    """KTD7. A late forecast saw post-cutoff reality, so it is excluded from Presisjon — but a
    rush nobody was warned about is a coverage miss whatever contaminated the forecast, and
    dropping it from both halves of that ratio would hide exactly the misses."""
    issue = _issue(sent=True, level="yellow", actual="yellow", late=True,
                   decided_at=_at(6, 35))
    row = rv.summarize([issue]).total
    assert rv.classify(issue) == rv.TRAFF  # it still carries a verdict, and the page shows it
    assert row.counts.n_late == 1 and row.counts.n_scored == 0
    assert row.presisjon is None
    assert row.dekning == 1.0 and row.dekning_den == 1 and row.dekning_num == 1
    assert row.matrix["yellow"]["yellow"] == 0  # the matrix is the scored population
    assert row.varsler_sendt == 1  # the push went out, late or not


def test_an_untimely_round_leaves_presisjon_and_stays_in_dekning():
    """KTD7 again, the other exclusion: infrastructural rather than a forecast error, and a rush
    it left unwarned is still uncovered."""
    issue = _issue(issue="1400", sent=False, reason="untimely", level="red", actual="red")
    row = rv.summarize([issue]).total
    assert rv.classify(issue) == rv.BOM
    assert row.counts.n_untimely == 1 and row.counts.n_scored == 0
    assert row.presisjon is None
    assert row.dekning == 0.0 and row.dekning_den == 1 and row.dekning_num == 0
    assert row.varsler_sendt == 0  # an untimely round never pushes


def test_an_ineligible_day_is_excluded_from_everything():
    """KTD7's third exclusion, and the widest: the day's realized share means nothing, so it
    reaches neither Presisjon nor Dekning nor Varslingstid."""
    issue = _issue(eligible=False, sent=True, level="red", actual="red", decided_at=_at(6, 31))
    row = rv.summarize([issue]).total
    assert row.counts.n_ineligible == 1
    assert row.presisjon is None
    assert row.dekning is None and row.dekning_den == 0
    assert row.varslingstid_min is None and row.varslingstid_n == 0
    assert row.varsler_sendt == 1  # the push still went out; the day's share is what means nothing


def test_a_correct_all_clear_is_a_justified_push_and_covers_the_friskmelding_that_was_owed():
    """A yellow warning stood and the rush turned green, so a friskmelding was OWED — that is a
    situation in Dekning's denominator — and the one that went out said the right class, so it is
    justified in Presisjon and covers the situation. Until 2026-09-02's second redefinition an
    all-clear was kept out of Dekning entirely, on the grounds that it retracts rather than warns;
    counting notifications rather than warnings makes a right retraction a hit like any other."""
    all_clear = _issue(issue="0700", sent=True, kind="all_clear", level="green",
                       baseline_level="yellow", actual="green", decided_at=_at(7, 1))
    row = rv.summarize([all_clear]).total
    assert rv.classify(all_clear) == rv.TRAFF
    assert rv.due(all_clear) and rv.justified(all_clear)
    assert row.presisjon == 1.0 and row.presisjon_den == 1
    assert row.dekning == 1.0 and row.dekning_den == 1 and row.dekning_num == 1
    assert row.varsler_sendt == 1


def test_a_wrong_all_clear_is_an_unjustified_push_and_no_situation_at_all():
    """The other half. A red warning stood and the rush stayed red, so NOTHING was owed at 14:00 —
    the traveller already held the right warning — and Dekning has no situation to count; the
    push that retracted it is Presisjon's failure. The red rush itself is covered, or not, at the
    12:00 issue that warned about it."""
    all_clear = _issue(issue="1400", sent=True, kind="all_clear", level="green",
                       baseline_level="red", actual="red", decided_at=_at(14, 1))
    row = rv.summarize([all_clear]).total
    assert rv.classify(all_clear) == rv.FALSK_ALARM
    assert not rv.due(all_clear) and not rv.justified(all_clear)
    assert row.presisjon == 0.0 and row.presisjon_den == 1
    assert row.dekning is None and row.dekning_den == 0


def test_a_missed_friskmelding_is_a_miss_and_a_confirmed_warning_is_no_situation():
    """Two silent follow-ups under a standing warning, told apart by the rush. The rush turned
    green: a friskmelding was owed and nothing went out — one situation, uncovered. The rush
    stayed yellow: nothing was owed, so the confirming silence is in neither rate, whatever the
    day table grades it."""
    owed = _issue(issue="0700", sent=False, reason="no_change", level="yellow",
                  baseline_level="yellow", actual="green")
    confirmed = _issue(d=NEXT_DAY, issue="0700", sent=False, reason="no_change", level="yellow",
                       baseline_level="yellow", actual="yellow")
    assert rv.due(owed) and not rv.due(confirmed)
    row = rv.summarize([owed, confirmed]).total
    assert row.dekning == 0.0 and row.dekning_den == 1 and row.dekning_num == 0
    assert row.presisjon is None and row.presisjon_den == 0  # nothing was pushed
    assert row.counts.n_scored == 2  # both are scored, both sit in the matrix


def test_an_escalation_where_a_friskmelding_was_owed_covers_nothing():
    """The one edge that makes Dekning's numerator ask for a JUSTIFIED push rather than any push:
    yellow standing, the follow-up escalates to red, the rush turns green. A friskmelding was owed
    and an escalation went out — a push, but not the notification the situation called for."""
    escalation = _issue(issue="0700", sent=True, kind="update", level="red",
                        baseline_level="yellow", actual="green", decided_at=_at(7, 1))
    row = rv.summarize([escalation]).total
    assert rv.due(escalation) and not rv.justified(escalation)
    assert row.dekning == 0.0 and row.dekning_den == 1
    assert row.presisjon == 0.0 and row.presisjon_den == 1


def test_dekning_counts_issues_on_a_slot_row_and_rushes_on_the_total_row():
    """A baseline that warned a yellow rush is one situation covered; its confirming follow-up is
    no situation, so the 0700 row is empty rather than restating the baseline. A second bad rush
    on the same day, or the same rush on another day, is another situation — on the total row a
    RUSH each, so the total is the number of rushes that owed a push, not the slot rows added up."""
    baseline = _issue(issue="0630", sent=True, level="yellow", actual="yellow",
                      decided_at=_at(6, 31))
    follow_up = _issue(issue="0700", sent=False, reason="no_change", level="yellow",
                       baseline_level="yellow", actual="yellow")
    stats = rv.summarize([baseline, follow_up])
    assert stats.total.dekning_den == 1 and stats.total.dekning_num == 1
    assert stats.total.dekning == 1.0
    by_slot = {row.slot: row for row in stats.slots}
    assert by_slot["0630"].dekning_den == 1 and by_slot["0630"].dekning == 1.0
    assert by_slot["0700"].dekning is None and by_slot["0700"].dekning_den == 0
    assert by_slot["1200"].dekning is None  # nothing in that slot at all

    pm = _issue(issue="1200", sent=False, reason="green_baseline", level="green", actual="red")
    assert rv.summarize([baseline, follow_up, pm]).total.dekning_den == 2
    tomorrow = _issue(d=NEXT_DAY, issue="0630", sent=False, reason="green_baseline",
                      level="green", actual="red")
    assert rv.summarize([baseline, follow_up, tomorrow]).total.dekning_den == 2

    # Three rushes owed a push and one got it. Each has one owing issue, so the slot rows happen
    # to add up to the same three here — see the next test for where they do not.
    rows = rv.summarize([baseline, follow_up, pm, tomorrow])
    assert rows.total.dekning_den == 3 and rows.total.dekning_num == 1
    assert sum(r.dekning_den for r in rows.slots) == 3


def test_a_rush_missed_at_the_baseline_and_caught_at_the_follow_up_is_covered_on_the_total_row():
    """Both issues owed a warning — nothing stood at 06:30, and the silent baseline left nothing
    standing at 07:00 — and the 07:00 one delivered. The total row counts the RUSH: it was warned,
    late, so it reads 100 % there and the lateness is Varslingstid's to show (the last push per
    rush, here the 07:00 one at about -1 min). The slot rows keep their own issue: the 06:30 row
    holds the miss, the 07:00 row the hit. Missed at both, the rush is one miss on the total row
    and two on the slot rows."""
    silent = _issue(issue="0630", sent=False, reason="green_baseline", level="green", actual="red")
    caught = _issue(issue="0700", sent=True, kind="warning", level="red", baseline_level="green",
                    actual="red", decided_at=_at(7, 1))
    stats = rv.summarize([silent, caught])
    assert rv.due(silent) and rv.due(caught) and rv.justified(caught)
    assert stats.total.dekning == 1.0 and stats.total.dekning_den == 1
    assert round(stats.total.varslingstid_min) == -1  # told, but only as the rush began
    by_slot = {row.slot: row for row in stats.slots}
    assert by_slot["0630"].dekning == 0.0 and by_slot["0630"].dekning_den == 1
    assert by_slot["0700"].dekning == 1.0 and by_slot["0700"].dekning_den == 1
    assert stats.total.presisjon == 1.0 and stats.total.presisjon_den == 1  # the one push

    also_missed = _issue(issue="0700", sent=False, reason="untimely", level="red",
                         baseline_level="green", actual="red")
    both = rv.summarize([silent, also_missed])
    assert both.total.dekning == 0.0 and both.total.dekning_den == 1
    assert sum(r.dekning_den for r in both.slots) == 2

    # The afternoon pair works the same way: silent at 12:00, caught at 14:00.
    pm_silent = _issue(issue="1200", sent=False, reason="green_baseline", level="green",
                       actual="red")
    pm_caught = _issue(issue="1400", sent=True, kind="warning", level="yellow",
                       baseline_level="green", actual="red", decided_at=_at(14, 1))
    pm = rv.summarize([pm_silent, pm_caught]).total
    assert pm.dekning == 1.0 and pm.dekning_den == 1 and round(pm.varslingstid_min) == 59


def test_presisjon_is_justified_pushes_over_pushes_and_a_level_swap_is_justified():
    """Of the pushes that went out, how many said the right class. A red push on a yellow rush is
    `traff nesten` in the day table and justified here — the traveller was warned, and which of
    the two levels is the matrix's business. A yellow push on a green rush is not. Silence is not a
    push and enters neither half, however the day table grades it."""
    issues = [
        _issue(issue="0630", sent=True, level="red", actual="yellow", decided_at=_at(6, 31)),
        _issue(d=NEXT_DAY, issue="0630", sent=True, level="yellow", actual="red",
               decided_at=_at(6, 31, day=NEXT_DAY)),
        _issue(issue="1200", sent=True, level="yellow", actual="green", decided_at=_at(12, 1)),
        _issue(issue="1400", sent=False, reason="green_baseline", level="green", actual="green"),
        _issue(d=NEXT_DAY, issue="1200", sent=False, reason="green_baseline", level="green",
               actual="red"),
    ]
    row = rv.summarize(issues).total
    assert [rv.classify(i) for i in issues[:3]] == [rv.TRAFF_NESTEN, rv.TRAFF_NESTEN, rv.FALSK_ALARM]
    assert row.presisjon_den == 3 and row.presisjon_num == 2
    assert row.presisjon == 2 / 3
    assert row.counts.n_scored == 5  # the two silent issues are scored, just not pushes


def test_a_calm_window_has_nothing_to_rate_and_no_pushes():
    """Four green forecasts on a green day are four right calls in the day table and NOTHING in
    the stats table: no push went out, so Presisjon has no denominator, and none was owed, so
    Dekning has none either. Both read an en dash. The first 2026-09-02 redefinition made them
    100 % (traff over every scored issue) to cure a 0 % that the earlier `riktig stille` exclusion
    produced; the second makes the rates about notifications, which a calm day has none of."""
    calm = [
        _issue(d=DAY, issue=slot, sent=False, reason="green_baseline", level="green",
               actual="green")
        for slot in rv.SLOTS
    ]
    row = rv.summarize(calm).total
    assert all(rv.classify(i) == rv.TRAFF for i in calm)
    assert not any(rv.due(i) for i in calm)
    assert row.presisjon is None and row.presisjon_den == 0
    assert row.dekning is None and row.dekning_den == 0
    assert row.varsler_sendt == 0
    assert row.counts.n_scored == 4  # the matrix still holds them, on its green/green cell
    assert row.matrix["green"]["green"] == 4


def test_the_rates_reconcile_with_the_pushes_and_never_exceed_their_denominators():
    """Over every reachable issue: Presisjon's denominator is exactly the scored pushes, so it can
    never exceed Varsler sendt plus the reconstructed pushes; both numerators are bounded by their
    denominators; and a Dekning hit is always a push, never a silence."""
    issues = list(_every_reachable_issue())
    row = rv.summarize(issues).total
    scored_pushes = [i for i in issues if rv.population(i) == rv.BUCKET_SCORED and i.sent]
    assert row.presisjon_den == len(scored_pushes) > 0
    assert row.presisjon_den <= row.varsler_sendt + row.varsler_rekonstruert
    assert 0 < row.presisjon_num <= row.presisjon_den
    assert 0 < row.dekning_num <= row.dekning_den
    delivered = [i for i in issues if rv.population(i) in rv.DELIVERED_BUCKETS]
    # Every enumerated issue is the same rush (DAY, am), so the total row's Dekning is one rush,
    # owed and covered; the 0700 slot row is the per-issue identity.
    assert (row.dekning_den, row.dekning_num) == (1, 1)
    slot = next(r for r in rv.summarize(issues).slots if r.slot == "0700")
    assert slot.dekning_den == sum(1 for i in delivered if rv.due(i)) > 0
    assert slot.dekning_num == sum(1 for i in delivered if rv.due(i) and i.sent and rv.justified(i))
    for i in scored_pushes:
        # For a push on a settled, eligible day the predicate and the verdict table agree.
        assert rv.justified(i) == (rv.classify(i) in {rv.TRAFF, rv.TRAFF_NESTEN}), i


def test_every_rate_is_none_on_an_empty_denominator():
    """R17. A rate whose denominator is empty is null and renders as an en dash, never 0 % —
    counts default to 0 and the nine matrix cells exist and are zero."""
    for row in rv.summarize([]).rows:
        assert row.presisjon is None
        assert row.dekning is None and row.varslingstid_min is None
        assert row.varsler_sendt == 0 and row.counts.n == 0
        assert set(row.matrix) == set(rv.LEVELS)
        assert all(set(cells) == set(rv.LEVELS) for cells in row.matrix.values())
        assert sum(sum(cells.values()) for cells in row.matrix.values()) == 0
    assert [row.slot for row in rv.summarize([]).rows] == ["total", *rv.SLOTS]


def test_the_confusion_matrix_is_forecast_against_realized_over_scored_issues():
    """R16. Nine cells, forecast level down and realized across, each carrying its count — and
    over the SCORED population only, so a late or unsettled issue cannot enter it."""
    issues = [
        _issue(issue="0630", sent=True, level="red", actual="yellow", decided_at=_at(6, 31)),
        _issue(issue="0700", sent=True, level="red", actual="yellow", decided_at=_at(7, 1)),
        _issue(issue="1200", sent=False, reason="green_baseline", level="green", actual="green"),
        _issue(issue="1400", sent=True, level="yellow", actual="red", late=True,
               decided_at=_at(14, 1)),
    ]
    row = rv.summarize(issues).total
    assert row.matrix["red"]["yellow"] == 2
    assert row.matrix["green"]["green"] == 1
    assert row.matrix["yellow"]["red"] == 0  # late: delivered, not scored
    assert sum(sum(cells.values()) for cells in row.matrix.values()) == 3
    per_slot = {r.slot: r for r in rv.summarize(issues).slots}
    assert per_slot["0630"].matrix["red"]["yellow"] == 1


def test_varsler_sendt_counts_the_pushes_and_nothing_else():
    """The notification-volume figure: one per round that pushed, friskmeldinger included, over
    every issue in the window — a push on a day that never settled still reached a phone. Not four
    per weekday, which is what `Avgjørelser` was until 2026-09-02, and what made it useless for the
    fatigue question the page exists to inform."""
    issues = [
        _issue(issue="0630", sent=True, level="yellow", actual="yellow", decided_at=_at(6, 31)),
        # the confirming follow-up: warned, but nothing was sent
        _issue(issue="0700", sent=False, reason="no_change", level="yellow",
               baseline_level="yellow", actual="yellow"),
        # pushed, and the day has not settled: still a push
        _issue(issue="1200", sent=True, level="red", actual=None, outcome=rv.OUTCOME_PENDING,
               decided_at=_at(12, 1)),
        # a friskmelding is a notification too
        _issue(issue="1400", sent=True, kind="all_clear", level="green", baseline_level="red",
               actual="green", decided_at=_at(14, 1)),
    ]
    stats = rv.summarize(issues)
    assert stats.total.varsler_sendt == 3
    assert [row.varsler_sendt for row in stats.slots] == [1, 0, 1, 1]
    assert stats.total.counts.n == 4  # the recorded population is still every issue


def test_a_reconstructed_round_is_scored_like_a_recorded_one_and_counted_apart():
    """The flag changes no verdict and no bucket — the page judges the model, and the policy is a
    pure function of the forecast — but a would-have-been push is not a sent one, and the counts
    say how much of a rate rests on replay."""
    real = _issue(sent=True, level="yellow", actual="yellow", decided_at=_at(6, 31))
    replayed = _issue(d=NEXT_DAY, sent=True, level="yellow", actual="yellow",
                      decided_at=_at(6, 31, day=NEXT_DAY), reconstructed=True,
                      after_recording_started=True)
    assert rv.classify(replayed) == rv.classify(real) == rv.TRAFF
    assert rv.population(replayed) == rv.population(real) == rv.BUCKET_SCORED
    row = rv.summarize([real, replayed]).total
    assert row.presisjon == 1.0 and row.presisjon_den == 2
    assert row.matrix["yellow"]["yellow"] == 2
    assert row.varslingstid_n == 2  # the replayed push time counts: it is when the forecast existed
    assert row.varsler_sendt == 1 and row.varsler_rekonstruert == 1
    assert row.counts.n_reconstructed == 1 and row.counts.n_reconstructed_after_start == 1


# ---- Varslingstid -----------------------------------------------------------------------


def test_lead_minutes_is_measured_from_each_issues_own_scope_start():
    """KTD8. The reference point is NOTIFY_SPECS.scope_start_h — 6.5 / 7.0 / 15.0 / 15.0 — which
    is each model's own label window, so 0700 leads from 07:00 and not from 06:30. Positive is
    warning time: the morning issues land at or just after their rush begins, while 1200 leads by
    about three hours. That is a product finding worth seeing, not a number to massage."""
    assert round(rv.lead_minutes("1200", _at(12, 1))) == 179  # 12:01 against a 15:00 rush
    assert round(rv.lead_minutes("0700", _at(7, 1))) == -1
    assert round(rv.lead_minutes("0630", _at(6, 31))) == -1
    assert round(rv.lead_minutes("1400", _at(14, 1))) == 59
    assert rv.lead_minutes("0630", None) is None  # a round that never pushed has no lead
    # Seconds survive: the figure is minutes, not a truncated clock reading.
    assert rv.lead_minutes("0630", _at(6, 0)) == 30.0


def test_the_scope_starts_are_the_ones_togvarsel_notify_pushes_on():
    """The cheap guard on the one constant this module copies. Nothing here imports
    togvarsel_notify (it pulls in fastapi and the deployment label), so the parity is checked
    against its source text — the same grep idiom test_togvarsel.py uses for the page's rendering.
    A scope start that moved in the policy and not here would silently re-base every lead."""
    src = (Path(rv.__file__).with_name("togvarsel_notify.py")).read_text()
    specs = re.findall(
        r'_NotifySpec\("(\d{4})",\s*[\d.]+,\s*[\d.]+,\s*(?:None|"\d{4}"),\s*([\d.]+),', src
    )
    assert len(specs) == len(rv.SLOTS), specs  # the parse found the table, not noise
    assert {key: float(start) for key, start in specs} == rv.SCOPE_START_H


def test_varslingstid_is_the_median_lead_of_that_slots_own_pushes():
    """A slot row takes the median lead of its own pushes, over pushes on settled eligible days —
    so a suppressed round contributes nothing, and neither does a push on a day that never
    settled."""
    issues = [
        _issue(issue="1200", sent=True, level="yellow", actual="yellow", decided_at=_at(12, 0)),
        _issue(d=NEXT_DAY, issue="1200", sent=True, level="red", actual="red",
               decided_at=_at(12, 20, day=NEXT_DAY)),
        # suppressed: no push, no lead
        _issue(issue="0630", sent=False, reason="green_baseline", level="green", actual="green"),
        # a push on an unsettled day: not in the population
        _issue(d=NEXT_DAY, issue="0630", sent=True, level="red", actual=None,
               outcome=rv.OUTCOME_PENDING, decided_at=_at(6, 31, day=NEXT_DAY)),
    ]
    per_slot = {row.slot: row for row in rv.summarize(issues).slots}
    assert per_slot["1200"].varslingstid_n == 2
    assert per_slot["1200"].varslingstid_min == 170.0  # median of 180 and 160
    assert per_slot["0630"].varslingstid_n == 0 and per_slot["0630"].varslingstid_min is None


def test_the_total_rows_varslingstid_uses_the_last_push_per_rush():
    """The requirement's own phrasing, and it is not an aggregate of the slot rows: the last push
    is the wording the traveller was left holding, so a rush warned twice contributes once."""
    issues = [
        _issue(issue="1200", sent=True, level="yellow", actual="yellow", decided_at=_at(12, 0)),
        _issue(issue="1400", sent=True, level="red", actual="red", decided_at=_at(14, 0)),
    ]
    stats = rv.summarize(issues)
    per_slot = {row.slot: row for row in stats.slots}
    assert per_slot["1200"].varslingstid_min == 180.0
    assert per_slot["1400"].varslingstid_min == 60.0
    assert stats.total.varslingstid_n == 1  # one rush, one last push
    assert stats.total.varslingstid_min == 60.0  # the 14:00 push, NOT the median of 180 and 60
    assert stats.total.varslingstid_min != 120.0


def test_an_unknown_issue_key_is_refused_loudly():
    """The four slots are a closed vocabulary, as they are in togvarsel_notify.decide_issue. A
    fifth would silently fall out of every slot row while still counting in the total."""
    for call in (
        lambda: rv.lead_minutes("0800", _at(8, 1)),
        lambda: rv.summarize([_issue(issue="0800")]),
    ):
        try:
            call()
        except ValueError:
            continue
        raise AssertionError("an unknown issue key was accepted")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
