"""Checks for rushvarsel_verdicts — /rushhistorikk's verdict taxonomy and stats-table metrics.

Hermetic and pure. The module under test does no I/O at all, so nothing here touches BigQuery, the
network or the wall clock; every input is a plain dataclass and every expectation is a literal.

The specification is the fourteen-row decision table and the three named populations in
docs/plans/2026-09-01-001-feat-rushhistorikk-forecast-scoring-plan.md (High-Level Technical Design,
KTD5-KTD8). It is transcribed below one test per row, named for the row, because this is the
artefact most likely to be got subtly wrong and the cheapest place to prove right: everything
above it — the SQL, the API, the page — only renders what these functions decide.

Run: uv run python test_rushvarsel_verdicts.py  (or under pytest)
"""

from __future__ import annotations

import itertools
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import rushvarsel_verdicts as rv

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
# | 11 | present | yes      | green        | no   | green         | no       | riktig stille  |
# | 12 | present | yes      | yel. or red  | no   | green         | no       | bom            |
# | 13 | present | yes      | green        | no   | yellow or red | no       | riktig stille  |
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
    the reason an all-clear is kept out of Presisjon (varslet) rather than counted as a warning."""
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


def test_row_11_green_forecast_green_rush_and_nothing_standing_is_riktig_stille():
    """Row 11. Nothing was communicated and nothing was warranted — the ordinary calm weekday, and
    82 % of days by KTD3's measurement."""
    issue = _issue(sent=False, reason="green_baseline", level="green", actual="green")
    assert rv.classify(issue) == rv.RIKTIG_STILLE


def test_row_12_green_forecast_on_a_bad_rush_is_bom():
    """Row 12. Green was forecast, nothing was in force, and the rush was bad: the miss the
    product exists to avoid."""
    for actual in ("yellow", "red"):
        issue = _issue(sent=False, reason="green_baseline", level="green", actual=actual)
        assert rv.classify(issue) == rv.BOM, actual


def test_row_13_a_suppressed_warning_on_a_green_rush_is_riktig_stille():
    """Row 13. The forecast said yellow or red, nothing reached the traveller and nothing needed
    to. Graded on what was communicated (nothing) rather than on the forecast — silence was right,
    whatever suppressed it."""
    for level in ("yellow", "red"):
        issue = _issue(issue="1400", sent=False, reason="untimely", level=level, actual="green")
        assert rv.classify(issue) == rv.RIKTIG_STILLE, level


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
    for (recorded, eligible, outcome, actual, sent, level, baseline, late, reason) in (
        itertools.product(
            (True, False),
            (True, False, None),
            rv.OUTCOME_STATES,
            (*rv.LEVELS, None),
            (True, False),
            rv.LEVELS,
            (None, *rv.LEVELS),
            (True, False),
            (None, "untimely", "no_change", "green_baseline"),
        )
    ):
        yield rv.Issue(
            d=DAY, issue="0700", level=level, recorded=recorded, eligible=eligible,
            outcome=outcome, actual=actual, sent=sent, baseline_level=baseline, late=late,
            reason=reason, decided_at=_at(7, 1) if sent else None,
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


def test_population_class_names_the_three_named_populations():
    """scored / delivered_only / excluded, the three-way reading the metric table uses. `late` and
    `untimely` are the two buckets that are delivered but not scored (KTD7)."""
    scored = _issue(sent=True, level="red", actual="red")
    assert rv.population_class(scored) == "scored"
    assert rv.population_class(_issue(sent=True, level="red", actual="red", late=True)) == (
        "delivered_only"
    )
    untimely = _issue(sent=False, reason="untimely", level="red", actual="red")
    assert rv.population_class(untimely) == "delivered_only"
    assert rv.population_class(_issue(eligible=False)) == "excluded"
    assert rv.population_class(_issue(recorded=False)) == "excluded"


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
    assert rv.VERDICT_LABEL[rv.RIKTIG_STILLE] == "Riktig stille"
    assert rv.VERDICT_LABEL[rv.VENTER] == "Venter"
    assert rv.VERDICT_LABEL[rv.UKJENT] == "Ukjent"
    assert rv.VERDICT_LABEL[rv.IKKE_VURDERT] == "Ikke vurdert"


# ---- The metrics ------------------------------------------------------------------------


def test_no_round_record_enters_no_denominator():
    """Row 1 is in Avgjørelser and in nothing else: defaulting those to bom would manufacture
    failures out of a deployment setting, and counting them anywhere else would too."""
    stats = rv.summarize([_issue(recorded=False, level="red", actual="red", sent=True)])
    row = stats.total
    assert row.avgjorelser == 1
    assert row.counts.n_ikke_vurdert == 1
    assert row.presisjon_samlet is None and row.presisjon_samlet_den == 0
    assert row.presisjon_varslet is None and row.presisjon_varslet_den == 0
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
    assert row.presisjon_samlet is None and row.presisjon_varslet is None
    assert row.dekning == 1.0 and row.dekning_den == 1 and row.dekning_num == 1
    assert row.matrix["yellow"]["yellow"] == 0  # the matrix is the scored population


def test_an_untimely_round_leaves_presisjon_and_stays_in_dekning():
    """KTD7 again, the other exclusion: infrastructural rather than a forecast error, and a rush
    it left unwarned is still uncovered."""
    issue = _issue(issue="1400", sent=False, reason="untimely", level="red", actual="red")
    row = rv.summarize([issue]).total
    assert rv.classify(issue) == rv.BOM
    assert row.counts.n_untimely == 1 and row.counts.n_scored == 0
    assert row.presisjon_samlet is None and row.presisjon_varslet is None
    assert row.dekning == 0.0 and row.dekning_den == 1 and row.dekning_num == 0


def test_an_ineligible_day_is_excluded_from_everything():
    """KTD7's third exclusion, and the widest: the day's realized share means nothing, so it
    reaches neither Presisjon nor Dekning nor Varslingstid."""
    issue = _issue(eligible=False, sent=True, level="red", actual="red", decided_at=_at(6, 31))
    row = rv.summarize([issue]).total
    assert row.counts.n_ineligible == 1
    assert row.presisjon_samlet is None and row.presisjon_varslet is None
    assert row.dekning is None and row.dekning_den == 0
    assert row.varslingstid_min is None and row.varslingstid_n == 0
    assert row.avgjorelser == 1  # still a decision that was taken


def test_a_correct_all_clear_is_not_a_correct_warning():
    """The exclusion that is easiest to get backwards. A right friskmelding is traff and belongs
    in Presisjon (samlet), but it is not a warning — counting it as one would let a month of 20
    right friskmeldinger and 20 wrong red warnings read 50 %."""
    all_clear = _issue(issue="0700", sent=True, kind="all_clear", level="green",
                       baseline_level="yellow", actual="green")
    row = rv.summarize([all_clear]).total
    assert rv.classify(all_clear) == rv.TRAFF
    assert row.presisjon_samlet == 1.0 and row.presisjon_samlet_den == 1
    assert row.presisjon_varslet is None and row.presisjon_varslet_den == 0


def test_a_wrong_all_clear_does_not_cover_a_bad_rush():
    """The other half. Counting a wrong friskmelding in Dekning's numerator would credit "all
    clear" with having warned about a red rush."""
    all_clear = _issue(issue="1400", sent=True, kind="all_clear", level="green",
                       baseline_level="red", actual="red")
    row = rv.summarize([all_clear]).total
    assert rv.classify(all_clear) == rv.FALSK_ALARM
    assert row.dekning == 0.0 and row.dekning_den == 1 and row.dekning_num == 0
    assert row.presisjon_varslet is None  # and it is not a warning here either
    assert row.presisjon_samlet == 0.0


def test_dekning_counts_a_rush_once_on_the_total_row():
    """The total row counts RUSHES, not issues: a confirming follow-up is notified only because
    its baseline pushed, so per-issue counting makes the 0700 and 1400 rows restate the
    baseline's coverage — and adding the slot rows up would double the day."""
    baseline = _issue(issue="0630", sent=True, level="yellow", actual="yellow",
                      decided_at=_at(6, 31))
    follow_up = _issue(issue="0700", sent=False, reason="no_change", level="yellow",
                       baseline_level="yellow", actual="yellow")
    stats = rv.summarize([baseline, follow_up])
    assert stats.total.dekning_den == 1 and stats.total.dekning_num == 1
    assert stats.total.dekning == 1.0
    by_slot = {row.slot: row for row in stats.slots}
    assert by_slot["0630"].dekning_den == 1 and by_slot["0700"].dekning_den == 1
    assert by_slot["1200"].dekning is None  # nothing in that slot at all

    # Two rushes on one day are two denominators; the same rush over two days is two as well.
    pm = _issue(issue="1200", sent=False, reason="green_baseline", level="green", actual="red")
    assert rv.summarize([baseline, follow_up, pm]).total.dekning_den == 2
    tomorrow = _issue(d=NEXT_DAY, issue="0630", sent=False, reason="green_baseline",
                      level="green", actual="red")
    assert rv.summarize([baseline, follow_up, tomorrow]).total.dekning_den == 2


def test_presisjon_samlet_reads_zero_on_a_window_of_only_riktig_stille():
    """The degenerate reading the plan's Open Questions flags, pinned so it cannot change
    silently. traff requires a NOTIFICATION, and a green rush correctly gets none — so a calm
    month reads 0 % over a non-empty denominator. The default is to keep the specified definition
    and label the denominator on the page; if that is ever revisited, this test is the record of
    what the figure used to mean."""
    calm = [
        _issue(d=DAY, issue=slot, sent=False, reason="green_baseline", level="green",
               actual="green")
        for slot in rv.SLOTS
    ]
    row = rv.summarize(calm).total
    assert all(rv.classify(i) == rv.RIKTIG_STILLE for i in calm)
    assert row.presisjon_samlet == 0.0
    assert row.presisjon_samlet_den == 4  # non-empty: 0 %, not "no data"
    assert row.presisjon_varslet is None  # nothing was communicated, so nothing to be right about


def test_every_rate_is_none_on_an_empty_denominator():
    """R17. A rate whose denominator is empty is null and renders as an en dash, never 0 % —
    counts default to 0 and the nine matrix cells exist and are zero."""
    for row in rv.summarize([]).rows:
        assert row.presisjon_samlet is None and row.presisjon_varslet is None
        assert row.dekning is None and row.varslingstid_min is None
        assert row.avgjorelser == 0 and row.counts.n == 0
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


def test_avgjorelser_counts_every_issue_in_the_window():
    """The recorded population: every issue, whatever became of it. Four per weekday whether or
    not anything was sent, which is exactly why it is not a notification-volume figure."""
    issues = [
        _issue(issue="0630", recorded=False),
        _issue(issue="0700", eligible=False),
        _issue(issue="1200", outcome=rv.OUTCOME_PENDING),
        _issue(issue="1400", sent=True, level="red", actual="red", decided_at=_at(14, 5)),
    ]
    stats = rv.summarize(issues)
    assert stats.total.avgjorelser == 4
    assert [row.avgjorelser for row in stats.slots] == [1, 1, 1, 1]


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
