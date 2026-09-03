"""The /rushhistorikk verdict taxonomy and stats-table metrics — pure functions, no I/O.

Rushvarsel issues four forecasts per weekday (togvarsel.py) and notifies on some of them
(togvarsel_notify.py). /rushhistorikk scores those decisions: what was communicated at each issue,
and whether the rush bore it out. This module is the ONE place that decision is expressed. The SQL
above it returns facts and nothing else, and the page below it renders what these functions say —
so "traff" cannot come to mean two different things in two files (KTD5).

Three ideas carry the whole module, and each is easy to get backwards.

**The verdict grades what was COMMUNICATED, not what this issue forecast** (KD2, KTD6). A follow-up
that confirms its baseline sends nothing, yet the traveller is warned — at the baseline's level,
which is what gets graded. Grading the follow-up's own forecast would credit a red warning nobody
received: 12:00 pushes yellow, the 14:00 round is suppressed with a red forecast, the rush is
yellow, and the yellow the traveller actually holds was exactly right. Symmetrically, silence is
scoreable both ways — `traff` when nothing was warranted and `bom` when something was.

**`traff` means the right CLASS was communicated, silence included** (2026-09-02). A green forecast
on a green rush is a hit, exactly as a yellow warning on a yellow rush is: the traveller was told
the right thing, and "nothing" is a thing to be told. It was a separate verdict, `riktig stille`,
until then. `bom` is unchanged: silence on a bad rush is still the miss the product exists to avoid.

**The two rates are precision and recall over NOTIFICATIONS, not over issues** (later on
2026-09-02, the product owner's second redefinition of the day). Presisjon asks "of the pushes we
sent, how many were justified" — a yellow or red push on a rush that turned yellow or red, whichever
of the two it was (`traff` and `traff_nesten` alike), or a friskmelding on a rush that turned green.
Dekning asks "of the situations where a push was owed, how many got the right one" — `due` names
those situations: a bad rush with no warning standing (a warning was owed) and a green rush with a
warning standing (a friskmelding was owed). Silence enters neither rate: a green forecast on a green
day is `traff` in the day table and nothing in the stats table, which is why a calm month reads an
en dash rather than 100 % or 0 %. Before this Presisjon was `traff` over every scored issue, silence
included — a fine accuracy figure and not the question "were the notifications justified" the
table is read for. The verdicts themselves did not move; only what the two rates count did.

**A round nobody recorded is reconstructed upstream, and this module cannot tell** (2026-09-02) —
by design. `rushhistorikk.reconstruct_rounds` replays togvarsel_notify.decide_issue over the
forecast log for issues with no round row, at each forecast's own computed_at, and hands the result
in as an ordinary recorded Issue with `reconstructed=True`. Nothing here branches on the flag: the
page exists to judge the model, and for that the forecast plus the policy IS the input, so a
reconstructed round is classified and bucketed exactly like one that ran. The flag is counted
(`n_reconstructed`, and `n_reconstructed_after_start` for the lost-write detector) and it keeps a
would-have-been push out of Varsler sendt, which says "sent" and means it.

**Every recorded issue gets exactly one verdict** (R13), including the states R7-R11 do not name:
no round record is `ikke_vurdert` (the notify gate was off, VAPID was absent, or the day predates
the round table — defaulting those to `bom` would manufacture failures out of a deployment
setting), an unsettled outcome is `venter`, and an unresolvable one or an ineligible day is
`ukjent`. :func:`classify` is the fourteen-row table from the plan, in that order, first match
wins — the order IS the specification, and the tests are one per row.

**Three populations, and a figure states which it uses** (KTD7). `scored` (record present, day
eligible, outcome settled and resolvable, forecast not late, round not untimely) is Presisjon's and
the confusion matrix's; `delivered` adds late and untimely back, because a rush nobody was warned
about is a coverage miss whatever contaminated the forecast, and dropping it from both halves of
Dekning would hide exactly the misses; `recorded` is every issue, and is what the exclusion counts
partition and what Varsler sendt counts pushes across — a push is a push whatever became of its
day. The seven exclusion buckets are resolved in the table's order so they are disjoint and sum to
n — segment, never filter.

Three consequences of counting notifications are stated here because they read as arbitrary
otherwise. A confirming follow-up — nothing sent, the baseline's warning still standing on a rush
that stayed bad — is in NEITHER rate: no push went out, and none was owed. A wrong friskmelding on
a rush that stayed bad is a Presisjon failure only: the situation called for no push at all (the
traveller already held the right warning), so Dekning has no situation to count, while the push
that retracted it is unjustified. And Dekning's total row counts RUSHES, not issues (product
owner, 2026-09-02): a rush missed at 06:30 and caught at 07:00 reads 100 % there — the traveller
was told, late, and how late is Varslingstid's to say (it takes the LAST push per rush on that
row, so the catch's own lead is what the API reports; the page dropped the column the same day
and keeps only the rates, Varsler sendt and the matrix). The slot rows still count their own issue, so the
06:30 row keeps the miss. The total's Presisjon needs no such lifting: a push is a push whichever
issue sent it.

Nothing here reads the clock, BigQuery or the network, and nothing is imported from main, varsel,
togvarsel or togvarsel_notify (the package rule). The one constant copied from
togvarsel_notify.NOTIFY_SPECS — the scope starts Varslingstid measures from — is guarded by a
source-text parity check in the test suite rather than by an import.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from statistics import median
from zoneinfo import ZoneInfo

_OSLO = ZoneInfo("Europe/Oslo")

# togvarsel's level vocabulary, in severity order. Used for the forecast level, the level a
# standing warning leaves in force, and the realized level — all three are this same scale, which
# is what makes `grade` one rule rather than three (KTD3: realized level is non-green exactly when
# the training label is true, so the two scales compare).
LEVELS: tuple[str, ...] = ("green", "yellow", "red")
_WARNING_LEVELS = frozenset({"yellow", "red"})

# The four weekday issues, in issue order. A "slot" is an issue key: the stats table carries one
# row per slot plus a total (KD6), because the baselines push on level and the follow-ups push on
# level CHANGE, so a blended figure moves with the mix rather than with the product.
SLOTS: tuple[str, ...] = ("0630", "0700", "1200", "1400")

# Which rush each issue is about. Two issues per rush, and the total row aggregates Dekning and
# Varslingstid per rush rather than per issue.
RUSH_BY_SLOT: dict[str, str] = {"0630": "am", "0700": "am", "1200": "pm", "1400": "pm"}

# Which realized window each issue is scored against (KTD4). 0700's model is trained on
# 07:00-09:00 (`amrest`), so scoring it against the whole morning would hand it thirty minutes
# that had already happened when it was issued, inflating its skill. The API resolves `Issue.actual`
# through this map; it is published here so there is one statement of it.
SCOPE_BY_SLOT: dict[str, str] = {"0630": "am", "0700": "amrest", "1200": "pm", "1400": "pm"}

# The Oslo fractional hour each issue's scored window STARTS — a copy of
# togvarsel_notify.NOTIFY_SPECS' scope_start_h, and identical to each model's own label window, so
# there is no second source to choose between (KTD8). Note 0700 leads from 07:00 and not from
# 06:30: a 07:01 push therefore leads by about -1 minute, not -31.
SCOPE_START_H: dict[str, float] = {"0630": 6.5, "0700": 7.0, "1200": 15.0, "1400": 15.0}

# ---- Verdicts ---------------------------------------------------------------------------

TRAFF = "traff"
TRAFF_NESTEN = "traff_nesten"
FALSK_ALARM = "falsk_alarm"
BOM = "bom"
VENTER = "venter"
UKJENT = "ukjent"
IKKE_VURDERT = "ikke_vurdert"

VERDICTS: tuple[str, ...] = (TRAFF, TRAFF_NESTEN, FALSK_ALARM, BOM, VENTER, UKJENT, IKKE_VURDERT)

# The Norwegian display labels live here and the front-end mirrors them, the way
# demo/varselhistorikk/src/format.ts mirrors /historikk's four verdicts. Retyping them page-side is
# how a rename ships to one surface only.
VERDICT_LABEL: dict[str, str] = {
    TRAFF: "Traff",
    TRAFF_NESTEN: "Traff nesten",
    FALSK_ALARM: "Falsk alarm",
    BOM: "Bom",
    VENTER: "Venter",
    UKJENT: "Ukjent",
    IKKE_VURDERT: "Ikke vurdert",
}

# ---- Outcome resolution -----------------------------------------------------------------

OUTCOME_SETTLED = "settled"  # confirmed against last_recorded — the only scoreable state
OUTCOME_PENDING = "pending"  # not settled yet, still inside the retry window (provisional included)
OUTCOME_UNRESOLVABLE = "unresolvable"  # the source can never establish it, or the retries gave up
OUTCOME_STATES: tuple[str, ...] = (OUTCOME_SETTLED, OUTCOME_PENDING, OUTCOME_UNRESOLVABLE)

# Resolution gives up after ten days, matching label_varsel_decisions.sql's retry window.
RETRY_DAYS = 10


def resolve_outcome_state(
    *, settled: bool, unresolvable: bool, d: date, today: date, retry_days: int = RETRY_DAYS
) -> str:
    """The realized outcome's state, from the outcome row's two flags plus the day it is about.

    Kept here rather than in the API so the retry window has one home, and pure because `today` is
    passed in. `unresolvable` wins over everything: the batch landed and the resolved denominator
    was zero, so re-running an immutable query for ten days would burn the window to reach the same
    NULL. An unsettled day inside the window is still being retried (`venter` on the page); past
    it the attempts have given up, which is /historikk's own venter-vs-ukjent distinction.
    """
    if unresolvable:
        return OUTCOME_UNRESOLVABLE
    if settled:
        return OUTCOME_SETTLED
    if (today - d).days > retry_days:
        return OUTCOME_UNRESOLVABLE
    return OUTCOME_PENDING


# ---- The population buckets -------------------------------------------------------------
#
# Resolved in the decision table's own order, first match wins, so the seven are disjoint and
# reconcile: their counts sum to n, which is the property that makes the exclusion counts beside
# the rates readable at all.

BUCKET_IKKE_VURDERT = "ikke_vurdert"
BUCKET_INELIGIBLE = "ineligible"
BUCKET_UKJENT = "ukjent"
BUCKET_VENTER = "venter"
BUCKET_LATE = "late"
BUCKET_UNTIMELY = "untimely"
BUCKET_SCORED = "scored"

BUCKETS: tuple[str, ...] = (
    BUCKET_IKKE_VURDERT, BUCKET_INELIGIBLE, BUCKET_UKJENT, BUCKET_VENTER,
    BUCKET_LATE, BUCKET_UNTIMELY, BUCKET_SCORED,
)
# The `delivered` population: scored, plus the two exclusions KTD7 keeps in Dekning.
DELIVERED_BUCKETS: frozenset[str] = frozenset({BUCKET_SCORED, BUCKET_LATE, BUCKET_UNTIMELY})
DELIVERED_ONLY: frozenset[str] = frozenset({BUCKET_LATE, BUCKET_UNTIMELY})


@dataclass(frozen=True)
class Issue:
    """One (day, issue) row: the forecast, the round record, and the realized outcome.

    Exactly what rushhistorikk_days.sql returns per (d, issue), resolved into the shape the
    taxonomy reads. Defaults are the SAFE ones — no round record, nothing settled — so a caller
    that forgets a field gets `ikke_vurdert` and no rate contribution rather than a phantom
    decision counted as real.
    """

    d: date  # Oslo date of the rush this issue is about
    issue: str  # the issue key / slot: one of SLOTS
    # L: the level this issue forecast. Take it off the ROUND record where there is one — that
    # is the level the decision was made on — falling back to togvarsel_issued's for an issue
    # with no round. The two agree by construction (both are first-write-wins on one Issue),
    # so the fallback is about a missing row rather than about a disagreement.
    level: str | None = None
    late: bool = False  # togvarsel_issued.late — the forecast saw post-cutoff reality
    recorded: bool = False  # a round exists: recorded in togvarsel_rounds, or reconstructed (below)
    sent: bool = False  # a push was ISSUED here (the policy decision, not delivery)
    kind: str | None = None  # warning | update | all_clear when sent; unmapped from the record
    reason: str | None = None  # untimely | no_change | green_baseline when suppressed; unmapped
    # (plus yellow_baseline on rows from the red-only window, 2026-09-02/03 — see togvarsel_notify)
    baseline_level: str | None = None  # B: the level a standing warning leaves in force
    decided_at: datetime | None = None  # when the round was evaluated — the push time
    # When the forecast was computed (togvarsel_issued.computed_at). Read by the API's
    # reconstruction only — it is the instant a round would have been evaluated at — never by the
    # taxonomy, which grades what the round record says.
    computed_at: datetime | None = None
    eligible: bool | None = None  # the training chain's DAY-level test; None = not established yet
    outcome: str = OUTCOME_PENDING  # one of OUTCOME_STATES
    actual: str | None = None  # A: the realized level for THIS issue's own window (SCOPE_BY_SLOT)
    # Whether the round log demonstrably already covered this whole day: `d` is strictly after the
    # earliest day any round was recorded on. Only meaningful for a RECONSTRUCTED round, where it
    # separates a LOST best-effort write from the pre-launch backlog. Strictly after, not at or
    # after: the earliest recorded day is a DAY, and on the deploy day itself the issues that fired
    # before the restart legitimately have no round — reading those as lost writes would put one
    # false positive per environment permanently on the page. The cost is that a write lost on that
    # first day reads as backlog, which is the safe direction.
    after_recording_started: bool = False
    # The round is not a record: rushhistorikk.reconstruct_rounds replayed the push policy on the
    # forecast because no togvarsel_rounds row existed. Classified and bucketed exactly like a
    # recorded round — the page judges the model, and the policy is a pure function of the forecast
    # — but counted apart, and never a sent push: Varsler sendt counts pushes that went out.
    reconstructed: bool = False


def _check_slot(slot: str) -> str:
    """The four slots are a closed vocabulary, as they are in togvarsel_notify.decide_issue. A
    fifth accepted silently would fall out of every slot row while still counting in the total."""
    if slot not in SCOPE_START_H:
        raise ValueError(f"unknown rushvarsel issue {slot!r}")
    return slot


def standing(issue: Issue) -> bool:
    """Whether a warning from this issue's baseline is still in force.

    Undefined for the two baseline slots, which have no baseline, and False there by construction:
    `_record_round` writes NULL `baseline_level` for 0630 and 1200.
    """
    return issue.baseline_level in _WARNING_LEVELS


def due(issue: Issue) -> bool:
    """Whether a push was OWED at this issue — Dekning's denominator condition.

    The class the traveller held going in — nothing for a baseline, the baseline's standing warning
    for a follow-up — differs from the class the rush became: a yellow or red rush with no warning
    standing owed a warning, a green rush with one standing owed a friskmelding. A level change
    inside the warning class (yellow held, red rush) owes nothing here, matching `justified`: the
    traveller was warned, and which of the two levels is the matrix's business. Reads `actual`
    without checking it is known — callers pass settled issues only.
    """
    return standing(issue) != (issue.actual in _WARNING_LEVELS)


def justified(issue: Issue) -> bool:
    """Whether a push that went out said the right CLASS — Presisjon's numerator condition, and
    with `due` Dekning's.

    A warning (yellow or red, either one) on a rush that turned yellow or red, or a friskmelding
    (the only green that ever pushes) on a rush that turned green. Equivalent to the verdict being
    `traff` or `traff_nesten` for a sent issue on a settled, eligible day; restated as a predicate
    so the rates do not depend on the verdict table's row order. Meaningless for an unsent issue.
    """
    return (issue.level != "green") == (issue.actual in _WARNING_LEVELS)


def grade(level: str | None, actual: str | None) -> str:
    """The one grading rule, used by table rows 5 and 8: equal is traff, a yellow/red swap is
    traff nesten, anything else — a graded green, or a green rush — is falsk alarm.

    Row 5 passes the issue's own level (a push went out saying it); row 8 passes `baseline_level`
    (nothing was sent, but that warning still stands). Same rule, two subjects.
    """
    if level == actual:
        return TRAFF
    if level in _WARNING_LEVELS and actual in _WARNING_LEVELS:
        return TRAFF_NESTEN
    return FALSK_ALARM


_A_KNOWN = "known"
_A_PENDING = "pending"
_A_UNRESOLVABLE = "unresolvable"


def _actual_state(issue: Issue) -> str:
    """The realized level as the table's `A` column reads it.

    A settled row carrying no level is a contradiction the page must not score — the settled pass
    writes a level or marks the day unresolvable — so it is read as unresolvable (`ukjent`) rather
    than silently graded against None.
    """
    if issue.outcome == OUTCOME_UNRESOLVABLE:
        return _A_UNRESOLVABLE
    if issue.outcome == OUTCOME_SETTLED:
        return _A_KNOWN if issue.actual in LEVELS else _A_UNRESOLVABLE
    return _A_PENDING


def classify(issue: Issue) -> str:
    """The verdict for one issue: the fourteen-row table, in order, first match wins.

    Returns one of VERDICTS and never raises: the taxonomy is total (R13), because a state it
    refused to name would be a state the page could not render.
    """
    if not issue.recorded:
        return IKKE_VURDERT  # row 1
    if issue.eligible is False:
        return UKJENT  # row 2 — an ineligible day's realized share means nothing
    if issue.level not in LEVELS:
        # Unreachable in practice: togvarsel_rounds records the level decide_issue validated. Read
        # as "the forecast disqualifies it" (R3) rather than raised, so one malformed row cannot
        # 500 a whole page.
        return UKJENT
    state = _actual_state(issue)
    if state == _A_UNRESOLVABLE:
        return UKJENT  # row 3
    if state == _A_PENDING:
        return VENTER  # row 4
    return _verdict_on_settled(issue)


def _verdict_on_settled(issue: Issue) -> str:
    """Rows 5-14: the day is eligible and the rush is known, so what remains is what was
    communicated. Split out from `classify` so the two halves read as the table does."""
    actual = issue.actual
    if issue.sent:
        if issue.level != "green":
            return grade(issue.level, actual)  # row 5 — a warning graded on its own level
        return TRAFF if actual == "green" else FALSK_ALARM  # rows 6, 7 — the all-clear
    if standing(issue):
        if issue.level != "green":
            return grade(issue.baseline_level, actual)  # row 8 — every confirming follow-up
        # Rows 9, 10: a friskmelding was owed and never went out. Reachable only on an untimely
        # round — before the deadline a green follow-up after a warned baseline always pushes — and
        # bom whatever the rush did: the traveller is left holding a warning nobody retracted.
        return BOM
    # Rows 11-14: nothing was communicated and nothing stands, so what the traveller holds is
    # "all clear" — a calm forecast (11, 12) and a suppressed warning (13, 14) are the same silence,
    # graded on the rush alone. Right when the rush was green (traff, since 2026-09-02 — it was
    # `riktig stille` before, and a verdict a calm month produced but Presisjon did not count put
    # that figure at 0 % on a good month); bom when it was not, whatever suppressed the warning.
    return TRAFF if actual == "green" else BOM


def population(issue: Issue) -> str:
    """Which of the seven buckets this issue belongs to, resolved in the table's order.

    The first four mirror `classify`'s first four rows, then KTD7's two exclusions — `late` before
    `untimely`, and both AFTER `venter`, so a late forecast on a day that has not settled counts as
    waiting rather than as excluded. Returns BUCKET_SCORED for everything left.
    """
    if not issue.recorded:
        return BUCKET_IKKE_VURDERT
    if issue.eligible is False:
        return BUCKET_INELIGIBLE
    state = _actual_state(issue)
    if issue.level not in LEVELS or state == _A_UNRESOLVABLE:
        return BUCKET_UKJENT
    if state == _A_PENDING:
        return BUCKET_VENTER
    if issue.late:
        return BUCKET_LATE  # excluded from Presisjon only; stays in Dekning
    if issue.reason == "untimely":
        return BUCKET_UNTIMELY  # likewise
    return BUCKET_SCORED



def _aware_utc(value: datetime) -> datetime:
    """A naive timestamp read as UTC, an aware one untouched.

    BigQuery hands back aware UTC, so a naive value here is a test fixture or a stray — and reading
    it as UTC rather than as local time keeps the figure from shifting by whatever offset the
    machine happens to be on. Shared by lead_minutes and _decided, which had this written out twice.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def lead_minutes(issue_key: str, decided_at: datetime | None) -> float | None:
    """Varslingstid for one round: signed minutes of WARNING TIME, positive when the push preceded
    the rush window it is about (KTD8).

    Measured from that issue's own `SCOPE_START_H` — 6.5 / 7.0 / 15.0 / 15.0 — on the Oslo day the
    push falls on. So a 07:01 push on 0700 reads -1 rather than -31, and 1200 leads by about 180.
    The morning issues landing at or just after their rush begins is a product finding worth
    seeing, not a number to massage; the page states the reference point beside the figure.

    None for a round that never pushed — which is what `decided_at is None` means to this function;
    a suppressed round has a `decided_at` of its own on the record, and callers pass it only for
    rounds where `sent` is true.
    """
    _check_slot(issue_key)
    if decided_at is None:
        return None
    pushed = _aware_utc(decided_at).astimezone(_OSLO)
    return (_scope_start(issue_key, pushed.date()) - pushed).total_seconds() / 60


def _scope_start(issue_key: str, day: date) -> datetime:
    """The Oslo wall-clock instant this issue's scored window starts on `day`. Built from the
    wall-clock components rather than by adding a timedelta to midnight, so a DST day still starts
    the rush at 06:30 local."""
    hours, minutes = divmod(round(SCOPE_START_H[issue_key] * 60), 60)
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=_OSLO)


# ---- The stats table --------------------------------------------------------------------


@dataclass(frozen=True)
class Counts:
    """The seven exclusion counts plus n, disjoint and reconciling: the six exclusions and
    `n_scored` sum to `n`. Shown beside the rates rather than used to filter rows — an untimely
    round's cause is infrastructural, and a reader who cannot see how many there were cannot tell
    a thin denominator from a bad product."""

    n: int = 0
    n_ikke_vurdert: int = 0
    n_ineligible: int = 0
    n_ukjent: int = 0
    n_venter: int = 0
    n_late: int = 0
    n_untimely: int = 0
    n_scored: int = 0
    # Orthogonal to the seven buckets, not an eighth: a reconstructed round lands in whichever
    # bucket its day puts it, and the seven above still sum to n. Counted so a reader can see how
    # much of a rate rests on replayed policy rather than on rounds that ran.
    n_reconstructed: int = 0
    # A SUBSET of n_reconstructed: the ones on a day the round log already covered, so a lost
    # best-effort write (or a closed gate) is visible during exactly the weeks the pre-launch
    # backlog is large enough to hide it.
    n_reconstructed_after_start: int = 0


@dataclass(frozen=True)
class StatsRow:
    """One row of "Slik har varslene truffet": the four figures with their own numerators and
    denominators, the 3x3 confusion matrix, and the seven counts.

    Each rate carries its numerator and denominator because each is over a DIFFERENT population
    (KTD5's metric table), and a rate quoted without its denominator on a page whose first weeks
    are almost entirely `ikke_vurdert` invites exactly the wrong reading. Rates are None on an
    empty denominator and render as an en dash, never 0 % (R17); counts default to 0.
    """

    slot: str  # one of SLOTS, or "total"
    presisjon: float | None
    presisjon_num: int  # justified pushes: the right class said, over the scored population
    presisjon_den: int  # pushes in the scored population (recorded or reconstructed)
    dekning: float | None
    dekning_num: int  # situations where a push was owed and a justified one went out
    dekning_den: int  # situations owing a push: ISSUES on a slot row, RUSHES on the total row
    varslingstid_min: float | None  # median warning time, minutes; the LAST push per rush in total
    varslingstid_n: int
    # Pushes issued in the window, friskmeldinger included — the notification-VOLUME figure, which
    # is what the fatigue question is about. Over the recorded population, not the scored one: a
    # push on a day that never settled still reached a phone. One per round, so it stands for
    # however many devices matched. RECORDED rounds only — see the next field.
    varsler_sendt: int
    # Pushes a RECONSTRUCTED round would have issued. Apart from varsler_sendt because that column
    # says "sent" and these never were; beside it so the volume the policy implies is still
    # readable over a window the round log does not cover.
    varsler_rekonstruert: int
    # Of varsler_sendt, the pushes on days whose outcome has not settled — in the count, not yet in
    # the matrix, which is over scored issues only. Printed beside the count because that gap is
    # the first thing a same-day reader sees: three pushes on the 06:30 row against a matrix that
    # sums to two, the morning's fasit still hours away. A SUBSET of varsler_sendt (recorded
    # rounds, not reconstructed ones), so "3 (1 venter)" reads as three, of which one.
    varsler_venter: int
    matrix: dict[str, dict[str, int]]  # matrix[forecast level][realized level] -> count, scored
    counts: Counts


@dataclass(frozen=True)
class Stats:
    """The stats table: a total row plus one row per issue slot, in issue order (KD6)."""

    total: StatsRow
    slots: tuple[StatsRow, ...]

    @property
    def rows(self) -> tuple[StatsRow, ...]:
        """Total first, then the four slots — the order the page renders."""
        return (self.total, *self.slots)


def _rate(num: int, den: int) -> float | None:
    """R17: null on an empty denominator, never 0.0. The distinction is the whole point — 0 % is a
    measurement and an en dash is the absence of one."""
    return None if den == 0 else num / den


def _empty_matrix() -> dict[str, dict[str, int]]:
    """All nine cells, always present and zero: the page prints every count as text inside the
    tint, and a missing cell would render as a hole rather than as a zero."""
    return {forecast: dict.fromkeys(LEVELS, 0) for forecast in LEVELS}


def summarize(issues: Iterable[Issue]) -> Stats:
    """The whole stats table from the window's issues: four figures, nine cells and seven counts,
    per slot and in total.

    Derived in process from the same rows the day list is built from (KTD5), so the summary cannot
    drift from the list it describes — the population is four rows per weekday, so a year is about
    a thousand rows and there is nothing to gain from a second query.
    """
    rows = list(issues)
    for issue in rows:
        _check_slot(issue.issue)
    return Stats(
        total=_stats_row("total", rows, per_rush=True),
        slots=tuple(
            _stats_row(slot, [i for i in rows if i.issue == slot], per_rush=False)
            for slot in SLOTS
        ),
    )


def _stats_row(name: str, issues: Sequence[Issue], *, per_rush: bool) -> StatsRow:
    """One row. `per_rush` is the total row's two aggregations: Dekning counts rushes rather than
    issues, and Varslingstid takes the last push per rush — neither is an aggregate of the slot
    rows, which is why they are computed here rather than combined afterwards. Presisjon adds up
    across slots and needs no flag."""
    buckets = [population(issue) for issue in issues]
    counts = Counts(
        n=len(issues),
        n_ikke_vurdert=buckets.count(BUCKET_IKKE_VURDERT),
        n_ineligible=buckets.count(BUCKET_INELIGIBLE),
        n_ukjent=buckets.count(BUCKET_UKJENT),
        n_venter=buckets.count(BUCKET_VENTER),
        n_late=buckets.count(BUCKET_LATE),
        n_untimely=buckets.count(BUCKET_UNTIMELY),
        n_scored=buckets.count(BUCKET_SCORED),
        n_reconstructed=sum(1 for issue in issues if issue.reconstructed),
        n_reconstructed_after_start=sum(
            1 for issue in issues if issue.reconstructed and issue.after_recording_started
        ),
    )
    scored = [i for i, b in zip(issues, buckets, strict=True) if b == BUCKET_SCORED]
    delivered = [i for i, b in zip(issues, buckets, strict=True) if b in DELIVERED_BUCKETS]

    # Presisjon: of the pushes that went out, the justified ones. Over the scored population only,
    # and over its PUSHES — a silent issue is not a notification and enters neither half. Not the
    # matrix's diagonal, which is over every scored issue by forecast level: a confirming follow-up
    # sits in the matrix and in no rate.
    pushes = [issue for issue in scored if issue.sent]
    presisjon_num = sum(1 for issue in pushes if justified(issue))

    matrix = _empty_matrix()
    for issue in scored:
        matrix[str(issue.level)][str(issue.actual)] += 1

    dekning_num, dekning_den = _coverage(delivered, per_rush=per_rush)
    leads = _leads(delivered, per_rush=per_rush)

    return StatsRow(
        slot=name,
        presisjon=_rate(presisjon_num, len(pushes)),
        presisjon_num=presisjon_num,
        presisjon_den=len(pushes),
        dekning=_rate(dekning_num, dekning_den),
        dekning_num=dekning_num,
        dekning_den=dekning_den,
        varslingstid_min=float(median(leads)) if leads else None,
        varslingstid_n=len(leads),
        varsler_sendt=sum(1 for issue in issues if issue.sent and not issue.reconstructed),
        varsler_rekonstruert=sum(1 for issue in issues if issue.sent and issue.reconstructed),
        varsler_venter=sum(
            1
            for issue, bucket in zip(issues, buckets, strict=True)
            if issue.sent and not issue.reconstructed and bucket == BUCKET_VENTER
        ),
        matrix=matrix,
        counts=counts,
    )


def _rush_key(issue: Issue) -> tuple[date, str]:
    return (issue.d, RUSH_BY_SLOT[issue.issue])


def _coverage(delivered: Sequence[Issue], *, per_rush: bool) -> tuple[int, int]:
    """Dekning's numerator and denominator over the delivered population: of the situations where
    a push was owed (`due`), those where a justified one went out.

    A slot row counts ISSUES — that slot's own opportunities to notify, so the 06:30 row keeps a
    miss the 07:00 round later made good. The total row counts RUSHES: a rush is a situation if
    either of its issues owed a push, and covered if either of those got a justified one, so a
    rush missed at 06:30 and caught at 07:00 reads 100 % there and the lateness is left to
    Varslingstid. A confirming follow-up owes nothing and is no situation on either row.

    The numerator asks for a JUSTIFIED push and not merely a push, for one edge: a yellow warning
    standing, the follow-up escalating to red, and the rush turning green — a friskmelding was
    owed and an escalation went out, which is not the notification the situation called for.
    """
    owed = [issue for issue in delivered if due(issue)]
    if not per_rush:
        return sum(1 for issue in owed if issue.sent and justified(issue)), len(owed)
    rushes: dict[tuple[date, str], list[Issue]] = {}
    for issue in owed:
        rushes.setdefault(_rush_key(issue), []).append(issue)
    covered = sum(
        1 for group in rushes.values() if any(i.sent and justified(i) for i in group)
    )
    return covered, len(rushes)


def _leads(delivered: Sequence[Issue], *, per_rush: bool) -> list[float]:
    """Varslingstid's sample: the leads of the pushes on settled eligible days — which is the
    delivered population narrowed to `sent`, so a late forecast's push counts (it did reach a
    traveller) while a suppressed round contributes nothing.

    A slot row takes that slot's own pushes. The total row takes the LAST push per rush, the
    requirement's own phrasing: the last wording is the one the traveller was left holding, so a
    rush warned twice contributes once.
    """
    pushes = [i for i in delivered if i.sent and i.decided_at is not None]
    if per_rush:
        last: dict[tuple[date, str], Issue] = {}
        for issue in pushes:
            key = _rush_key(issue)
            previous = last.get(key)
            if previous is None or _decided(issue) > _decided(previous):
                last[key] = issue
        pushes = list(last.values())
    leads = [lead_minutes(i.issue, i.decided_at) for i in pushes]
    return [lead for lead in leads if lead is not None]


def _decided(issue: Issue) -> datetime:
    """`decided_at` as a comparable instant. Naive values are read as UTC, matching
    `lead_minutes`, so a mixed list cannot raise mid-comparison."""
    decided_at = issue.decided_at
    assert decided_at is not None  # callers filter on it
    return _aware_utc(decided_at)
