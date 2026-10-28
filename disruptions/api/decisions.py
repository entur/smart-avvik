"""The alert-decision log: persist what Avviksvarsel concluded, so it can be checked later.

Until this module existed, the only record of what a traveller was told was Cloud Logging, and
nothing anywhere recorded what actually *happened* to the train we warned about. That makes the
one question worth asking about an alerting system unanswerable: when we alerted, were we right?

What is recorded is **every decision**, not every push. A row is written for each situation the
matcher matched to a registered journey — including the ones that stayed below the alert
threshold. That asymmetry is the whole point: a journey we did not alert on leaves no trace
otherwise, so `miss` and `correct_reject` would be unobservable and any "precision" figure would
be measuring only the half of the confusion matrix that flatters us.

Rows are keyed on (journey definition x situation x departure), and the same decision reappears as
its prediction evolves. Those repeats are not duplicates to be suppressed — they are the lead-time
series, the axis alerting quality varies along most (eval/impact_eval.py: alertable ~30-60 min
pre-departure, and ask-time features decay).

**Not on every cycle, since 2026-09-12** (KTD15), and this qualifies the paragraph above rather than
replacing it. One row per subject per cycle was the rule from the day this module existed and the
reasoning for it stands; what does not stand is the arithmetic. Measured in prd on 2026-09-11:
74 000-93 000 rows a day at 226 registered definitions, ~400 per definition per day, 88-95 % of them
sub-threshold no-SX subjects re-scored every ~295 s with nothing about them having moved. At the
35 000-80 000 definitions this stack is being taken to that is 14-32 M rows and 14-33 GB a DAY, and
a 14-day /historikk window over it is 70-150 GB per request — a page that refuses rather than
renders. So `varsel._thinning_reason` keeps every row a figure `varsel_decision_groups` computes is
made of — the first sighting, every alert-worthy/sub-threshold transition, every new maximum
probability, every push, and a closing row carrying the subject's last observation — and writes a
standing subject nothing has changed about at most once per quarter hour. The lead-time series
survives for the subjects it is a series ABOUT (an alerting subject transitions and re-peaks, so it
keeps its cycles); what thins away is the flat middle of a subject nobody was ever told about.
`n_decisions` is the one column whose meaning changes, and its description says so.

Design notes
------------
* **No import of main** (the rule the rest of this package follows): main injects a BigQuery client
  via :func:`configure`. varsel imports this module lazily, the way it already imports
  :mod:`llm_message`, so a deploy missing it degrades to "no log" rather than "no pushes".
* **Fail-open, always.** Every entry point swallows its exceptions. A push that reached a
  traveller must never be lost to a logging problem, and the poller thread must never die in one.
* **Streaming inserts, not DML.** `insert_rows_json` has no daily table-modification quota (DML
  does, at 1500/table/day, which a 60 s poller can approach), and the streaming buffer's
  restriction — no DML UPDATE for ~90 min — costs nothing here because this table is append-only
  by design and the labelling job writes a *separate* outcomes table.
* **Privacy.** `journey_digest` (shared with :func:`llm_message.key_digest` so the two records
  join) plus a device count. No device_id, no journey_id, no time window, no weekday pattern.
  **Two channels since 2026-09-13** (U16, KTD4) and the claim above is unchanged by it: a webhook
  route contributes a `channel` value and a `n_subscriptions` count and nothing else, so the grain
  is still the journey definition and no subscription, client, destination or external reference
  reaches this table. KTD10 held client routes out of it entirely until `/historikk` and
  `/meldinger` were behind authentication; they have been since 2026-09-10, and at a hundred
  thousand commuters leaving them out would have meant leaving the product unscored.
  `message_input` is the one field that carries origin, destination and departure time together — it
  is what the writer was shown, so it cannot be redacted here without defeating the purpose — which
  is why `/historikk-api` serves neither it nor `from_stop`/`to_stop`.
* **Everything the writer saw, for every decision.** The row records the input, not just its
  output: the document verbatim, the observation and alternative quoted inside it, which backend
  answered, and `message_status` — why the stored wording has the source it does. A deterministic
  row can mean the LLM is off, the cycle hit its cap, the retry gave up or the model refused, and
  those are four different problems that used to look identical once written down.
  `message_input` is the same object the message was actually written from
  (`llm_message.build_message_input`, serialized by `llm_message.message_input_json`) — the model's
  prompt and, when no model wrote anything, the deterministic template's source too.
  `alt_status` is that same idea for the advice: a row without an alternative can mean the lookup
  found nothing, that it kept the traveller on the train they have, or that no lookup was issued at
  all — and since most rows are sub-threshold, the last of those is most of any offer rate's
  denominator.
* **Both wordings, not just the one that went out** (2026-08-07). `VARSEL_LLM_ENABLED` used to
  decide whether a written wording *existed*; it now decides only which of the two is *sent*
  (`llm_message.generation_enabled` vs `delivery_enabled`). So the row carries `message_llm` and
  `message_deterministic` side by side, and `message`/`message_source` describe **delivery** rather
  than availability: with delivery off — prd, today — an LLM wording exists that the push did not
  carry, and `message_source = 'deterministic' AND message_llm IS NOT NULL` is exactly that
  withheld-by-policy population. Recording only what was sent would leave the deployment whose
  pushes reach real travellers with nothing to compare its own wording against, which is the whole
  reason the flag was split.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import metrics  # sibling leaf module (no main import): the decision-write counter (KTD11)

try:
    # BigQuery's OWN retry policy, imported only to narrow its deadline (see `_WRITE_RETRY`).
    # Guarded because this module must stay importable without the BigQuery library: a deployment
    # that has no client records nothing anyway (`record` returns early), and `test_decisions.py`
    # runs on stdlib plus pydantic. Absent, `_WRITE_RETRY` is None, which the real client reads as
    # "one attempt" — the same bound `timeout=` already puts on that attempt.
    from google.cloud.bigquery.retry import DEFAULT_RETRY as _BQ_DEFAULT_RETRY
except ImportError:  # pragma: no cover - exercised only where google-cloud-bigquery is absent
    _BQ_DEFAULT_RETRY = None

if TYPE_CHECKING:  # type-checker only: no runtime import, so the varsel <-> decisions cycle stays broken
    from varsel import Alert

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

TABLE = "disruptions.varsel_decisions"

#: The `channel` column's vocabulary (U16, KTD4). A fixed three-value set rather than anything read
#: out of the data: it is also the allow-list `/historikk-api` and `/meldinger-api` validate their
#: channel filter against, and a value landing in a public endpoint's response-cache key has to come
#: from a bounded set. 'browser' is a /varsel registration, 'webhook' a client subscription and
#: 'both' a definition the two share -- deliberately NOT `notification_store.CHANNELS`' 'web_push',
#: because this table's word for that side has been 'browser' everywhere since it existed and this
#: module may not import that one.
CHANNEL_BROWSER = "browser"
CHANNEL_WEBHOOK = "webhook"
CHANNEL_BOTH = "both"
CHANNELS = (CHANNEL_BROWSER, CHANNEL_WEBHOOK, CHANNEL_BOTH)

_DDL_PATH = Path(__file__).parent / "create_varsel_decisions.sql"

# The DDL file is two halves, split on this marker: structure above, column descriptions below.
# Both are idempotent and both run at every startup, but only the first decides whether the log is
# usable -- see ensure_table. The marker lives in the SQL as a comment, so a reader of either file
# sees the same boundary; `partition` means a file without it degrades to "all structural", which is
# the safe direction (a description failure would then block again, rather than a structural failure
# being ignored).
_BEST_EFFORT_MARKER = "-- BEST-EFFORT SECTION"
_ALL_DDL_SQL = _DDL_PATH.read_text()
_STRUCTURE_SQL, _, _DESCRIPTIONS_SQL = _ALL_DDL_SQL.partition(_BEST_EFFORT_MARKER)

# Injected by main.lifespan; None disables recording entirely (tests, and any deploy where the
# client could not be built).
_bq_client: Any | None = None
_table_ready = False

# How long to wait before retrying the CREATE after a failure. Without it a persistent problem
# (a missing permission, a malformed statement) re-issues the DDL on every 60 s poller tick for the
# life of the process, burning a BigQuery job each time to fail the same way. Same shape as
# llm_message._LATCH_TTL: back off, but expire so a fixed grant takes effect without a redeploy.
_ENSURE_RETRY_S = 15 * 60
_ensure_failed_at: float | None = None
# Same backoff, separate state: the description refresh succeeds and fails independently of the
# structural DDL, and only the structural half decides whether a row can be written at all.
_descriptions_done = False
_descriptions_failed_at: float | None = None

# The DDL runs on the startup path of a single always-on pod, in front of the poller thread, so
# it gets an explicit ceiling rather than inheriting the client's "wait forever".
_ENSURE_TIMEOUT_S = 60.0
# `record` runs inside check_and_notify on the poller thread, so its streaming insert needs a
# ceiling for the same reason (a hung insertAll wedges the tick, /health goes stale, liveness
# restarts into the hang). Generous against a small batch: a write that needs longer than this has
# something wrong with it, and the write fails open anyway — the row is logged as lost and the
# traveller's push is unaffected.
#
# IT BOUNDS ONE HTTP ATTEMPT, NOT THE CALL, and until 2026-09-12 this comment claimed otherwise.
# `insert_rows_json` runs under the client's `DEFAULT_RETRY`, whose deadline is
# `_DEFAULT_RETRY_DEADLINE = 10.0 * 60.0` — so a single chunk could legitimately sit here for ten
# minutes, and several sequential chunks for as many. `HEALTH_STALE_AFTER_S` is 600 s and a restart
# comes up with both planned-call tables cold, so the library's default turns a BigQuery wobble into
# a cold-start crash loop. See `_WRITE_RETRY` for the bound, and `record`'s `beat` for the other
# half: the deadline below is only the exposure /health sees because a chunk boundary beats.
_WRITE_TIMEOUT_S = 60.0

# What ONE chunk may spend in total, retries included. BigQuery's own `DEFAULT_RETRY` with nothing
# but its deadline narrowed: the predicate stays the library's, so exactly the same errors are
# retried and only the time spent on them changes — the minimal edit, and the one that cannot
# silently stop retrying something that used to recover.
#
# The arithmetic, stated because it is not the 60 s the name suggests. api_core checks the deadline
# BEFORE each sleep, so an attempt that starts just under it still runs its own `timeout=`: the
# worst case per chunk is `_WRITE_TIMEOUT_S` (the retry deadline) plus one attempt, i.e. ~120 s.
# That is the whole exposure `/health` sees, because `record` beats the poller's liveness seam
# between chunks — where before this it was (600 + 60) s per chunk, with no beat at all, however
# many chunks a cycle had.
#
# None where google-cloud-bigquery is absent (see the guarded import above). The real client reads
# that as "no retry wrapper", i.e. one attempt bounded by `timeout=` — strictly tighter than this,
# never looser, which is the safe direction for a fallback nobody will notice taking effect.
_WRITE_RETRY = (
    _BQ_DEFAULT_RETRY.with_timeout(_WRITE_TIMEOUT_S) if _BQ_DEFAULT_RETRY is not None else None
)

# ---- One cycle is several insert REQUESTS (U4, 2026-09-12) ---------------------------------------
#
# `insert_rows_json` maps to one `insertAll` request, and BigQuery refuses one over **10 MB or
# 50 000 rows**. Nothing in this repo stated those limits, and until this change a cycle's rows went
# as a single call whatever their number: at today's 1 100-1 500 rows of ~1 KB on an evening cycle
# that is a tenth of the byte bound, and at 100 000 commuters it is 30 000+ rows, where ONE refused
# request loses the whole cycle's decisions rather than a share of them. A lost request is the
# silent failure this module is built around — the write fails open by design, so the only trace is
# a WARNING and a counter — which is why the bound is enforced here rather than discovered upstream.
#
# HALF of each documented limit, not the limit itself. The byte count below is an estimate of the
# serialized row (see `_row_bytes`) and the wire form carries per-row insertIds and envelope on top
# of it, so sending right up to the ceiling would fail for a reason no reading of this code
# explains. Half is also far enough under that a row three times the size of anything measured
# still fits the estimate's error.
_MAX_ROWS_PER_INSERT = 10_000
_MAX_BYTES_PER_INSERT = 5 * 1024 * 1024


def configure(*, bq_client: Any) -> None:
    """Wire in the BigQuery client. Called once from main.lifespan."""
    global _bq_client
    _bq_client = bq_client


def enabled() -> bool:
    return _bq_client is not None


def ensure_table() -> bool:
    """Create the decision log if it is not there yet. Safe to call repeatedly.

    Done from the app rather than a provisioning script because prd is an isolated project fed by
    free BigQuery copies and nobody runs build scripts against it — the table would simply not
    exist there. `CREATE TABLE IF NOT EXISTS` never touches an existing table's contents.

    Run as TWO jobs, and only the first one decides readiness. The second carries reworded column
    descriptions to tables that already exist, and it is the half that can fail for a reason having
    nothing to do with whether a row can be written: BigQuery caps table-metadata updates at 5 per
    10 seconds per table, and that block only ever grows — every reworded description adds a
    permanent `ALTER COLUMN ... SET OPTIONS`. Measured 2026-08-07 against dev, the file reliably
    returned "Exceeded rate limits: too many table update operations for this table" once it reached
    seven of them. Sending the whole file as one job made that a decision-log outage: any failure
    left `_table_ready` False, so `record()` wrote nothing at all and a *documentation* edit silently
    took down the log on the next deploy. Structure first, then descriptions, and a failure in the
    descriptions is logged and forgiven.
    """
    global _table_ready, _ensure_failed_at
    if _bq_client is None:
        return False
    if _table_ready:
        return True
    if _ensure_failed_at is not None and monotonic() - _ensure_failed_at < _ENSURE_RETRY_S:
        return False
    if not _table_ready:
        try:
            _bq_client.query(_STRUCTURE_SQL).result(timeout=_ENSURE_TIMEOUT_S)
            _table_ready = True
            _ensure_failed_at = None
            logger.info("Decision log ready (%s)", TABLE)
        except Exception:
            # Not fatal: recording is an observability feature, and the alerting path it observes
            # must keep working without it. Backed off rather than retried every tick.
            _ensure_failed_at = monotonic()
            logger.warning(
                "Could not ensure %s; decisions will not be recorded", TABLE, exc_info=True
            )
            return False
    _refresh_descriptions()
    return _table_ready


def _refresh_descriptions() -> None:
    """Carry reworded column descriptions to tables that already exist. Never raises.

    Its own retry state rather than riding on ``_table_ready``, because the two have different
    success conditions and the descriptions half is the one that fails. It is rate-limit-prone by
    construction — BigQuery caps table-metadata updates at 5 per 10 s per table and the block has
    more statements than that — and ``ensure_table`` returns early once the table is ready, so
    piggy-backing gave this exactly one attempt per process. On a pod that redeploys by manual
    dispatch (prd) a single transient burst then left `bq show --schema` stale for the pod's whole
    life, which is the opposite of what this block exists for. Backed off on the same ``_ENSURE_RETRY_S``
    as the structural half so a later poller tick — well past the 10 s quota window — lands it.
    """
    global _descriptions_done, _descriptions_failed_at
    if _descriptions_done or not _DESCRIPTIONS_SQL.strip():
        return
    if _descriptions_failed_at is not None and monotonic() - _descriptions_failed_at < _ENSURE_RETRY_S:
        return
    try:
        _bq_client.query(_BEST_EFFORT_MARKER + _DESCRIPTIONS_SQL).result(timeout=_ENSURE_TIMEOUT_S)
        _descriptions_done = True
        _descriptions_failed_at = None
    except Exception:
        # The table is already usable; what failed is documentation. Say so at warning level so a
        # persistently stale `bq show --schema` is discoverable, and carry on recording.
        _descriptions_failed_at = monotonic()
        logger.warning(
            "Could not refresh %s column descriptions; the schema comments may be stale. "
            "Decisions are still being recorded and this will be retried.",
            TABLE,
            exc_info=True,
            extra={"fields": {"event": "varsel_decision_write", "result": "stale_descriptions"}},
        )


def _oslo_date(when: datetime | None):
    """Operating day of a call, in the timezone the call tables key on."""
    return when.astimezone(_OSLO).date() if when is not None else None


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when is not None else None


def _alternative_status(alert: Alert) -> str:
    """What became of the alternative-journey lookup for this decision.

    The same shape as `message_status` beside it, and for the same reason: a single boolean cannot
    separate **"a lookup ran and found nothing"** from **"no lookup was ever issued"**, and roughly
    83 % of the rows in this table are sub-threshold decisions that never issue one. Without the
    distinction an offer rate has no defined denominator across the 2026-08-06 selection change,
    which measurably moves the numerator — the same trap `et_cancelled` was added to avoid, one
    column over.

    Written on **every** row, alert-worthy or not. The four values this function can establish:

    * ``offered`` — the row carries an alternative. Read off the **Alert**, never off
      ``alert.impact``: the two agree since 2026-08-12 (`varsel._collect_alerts` projects each
      situation's own lookup onto the response copy the alert carries), but the Alert is the field
      the wording is actually built from, and reading the response is what once recorded
      `has_alternative=true` beside a message input with no `alternativ_reise`.
    * ``suppressed`` — `impact.original_is_best`: the selection ran against a real pool and kept the
      traveller on the train they already have. A verdict, not an absence.
    * ``none_found`` — `impact.alternative_subject` is set, so the lookup ran to completion, and it
      produced neither an offer nor a "stay put".
    * ``not_worth_lookup`` — nothing above: no lookup was issued for **this train**. The impact gate
      (`impact._worth_an_alternative`) is the reason for the overwhelming majority; the 6 h horizon
      (`alternatives.within_lookahead`) and the per-response lookup cap
      (`impact._MAX_ALTERNATIVE_LOOKUPS`) also land here, as does a reading taken from a
      non-canonical stop ref about a train the canonical pass never looked up.

    The grain of all four changed on 2026-08-12 without the vocabulary changing: they used to
    describe one lookup per journey-window, back-filled onto every alert in it, and now describe a
    lookup per train. Expect `not_worth_lookup` to grow against `offered` at long lead times — that
    is the horizon guard finally reaching the trains that used to inherit a nearer train's answer,
    not a drop in what the Journey Planner can find.

    Two of the vocabulary's values are deliberately **never written**: ``beyond_horizon`` and
    ``error``. `impact.for_trip` records neither, and its `except` branch resets the response to
    exactly the three values an un-attempted lookup leaves behind, so both are indistinguishable
    from "no lookup" by the time it reaches this module. They could only be guessed — from a
    departure more than `alternatives._MAX_LOOKAHEAD_HOURS` out, say — and a reconstruction of
    another module's control flow in a column that reads as an observation is how a log stops being
    evidence. They stay reserved in the column description so a later unit that plumbs the reason
    through can fill them in without a vocabulary change.
    """
    if getattr(alert, "alternative_journey", None) is not None:
        return "offered"
    impact = getattr(alert, "impact", None)
    if getattr(impact, "original_is_best", False):
        return "suppressed"
    if getattr(impact, "alternative_subject", None) is not None:
        return "none_found"
    return "not_worth_lookup"


def _alternative_fields(alert: Alert) -> dict:
    """The suggested journey as flat columns: was one offered, how it compares, when, which lines.

    The full object is in `message_input`; these exist so "how often do we actually offer one, and is
    it any good?" is a GROUP BY rather than a JSON scrape. Measured 2026-08-04 under the old
    earliest-arrival-and-at-least-three-minutes-faster rule, the answer was "effectively never" —
    which is only visible because it was recorded. The 2026-08-06 selection rule moves that rate
    sharply, which is exactly why `alt_status` is written on every row: an offer rate whose
    denominator shifts under it measures the change in the log, not the change in the advice.

    **Measured 2026-08-06 in dev, and the paragraph above does not survive it.** `has_alternative`
    has only been WRITTEN since 2026-08-05 — this function is what writes it, and it was added in
    fa27085 that day. Every one of the 673 rows before that carries NULL. So "measured 2026-08-04"
    cannot have come from this column, and the 0 % it reports is the absence of instrumentation, not
    the absence of offers. Grouping the log into SUBJECTS (journey x situation x departure, since
    the poller re-decides each one ~10 times):

        08-01..08-04   83 subjects   0 measurable            no measurement exists
        08-05..08-06   50 subjects  50 measurable, 13 offered        26.0 %

    That 26 % is the old earliest-arrival-and-three-minutes-faster rule, since the 2026-08-06
    selection rule had not deployed when those rows were written. It is the ONLY measurement of it
    there has ever been. **"Effectively never" was never measured; treat it as a claim, not a
    finding.**

    The trap here is worth more than the figure, because it caught two readers in a row. NULL is a
    third bucket, never a default: `LOGICAL_OR(has_alternative)` over uninstrumented rows is NULL,
    `COUNTIF` does not count it, and 83 subjects with no data render as 83 subjects with no offer.
    That is the same rule `/historikk` states for `n_unsegmented` and the same one `alt_status`
    exists to enforce — and it was still got backwards while writing a correction about it.

    Caveats travel with the figures: dev only, 9 registered journeys over 9 stop pairs, and n=50
    (so roughly +/-12 points). The cancelled-train case — the one the new rule exists for — has
    **2** subjects, neither offered. That is not evidence for the rule; it is the absence of enough
    data to test it, and the honest reason to let `alt_status` accumulate after deploy.

    The query is `data/queries/alternative_offer_rate.sql`; re-run it rather than quoting this
    block, which is a snapshot and will go stale exactly the way its predecessor did.

    Three things about the offer itself that the old columns cannot express, because the journey they
    describe is no longer necessarily a faster one:

    * `alt_arrives_before_original` — the fallback tier legitimately arrives *later* than the
      timetable promised, which is worth offering to somebody whose train is cancelled and misleading
      to count as a saving. `alt_minutes_saved` is signed for the same reason and is recorded as it
      comes: a negative value is an answer, not a row to drop.
    * `alt_at_risk` — we offered a journey our own models flag, because nothing safer got there in
      time. A wording that presents it as a clean escape would be wrong, so the log has to be able to
      find those rows.
    * `alt_scored` — whether the prediction cache had the alternative's boarding call at all. It
      lives on `AlternativePrediction`, **not** on `AlternativeJourney`: a flat
      `getattr(alt, "scored", None)` would ship a column of permanent NULLs that reads as
      "unscored", which is precisely the fact-vs-absence confusion the flag exists to prevent.

    `alt_operator` / `alt_jkey` / `alt_stop_order` are what make the advice **scoreable**. Every
    other decision this log records gets a nightly hit / false-alarm / miss verdict from
    `data/queries/label_varsel_decisions.sql`, which joins realized ET outcomes on
    `(operator, jkey, call_date, stop_order)`; the row carried that key for the disrupted train and
    not for the one we told the traveller to take instead, so "take this instead" was the only
    traveller-facing claim in this repo that nothing could ever label. They come off the boarding
    `CallPred` `impact._score_candidate` resolved, through `AlternativePrediction` — the same nested
    read as `alt_scored`, and for the same reason: `AlternativeJourney` carries no journey ref, and a
    flat `getattr(alt, "jkey", None)` would ship a column of permanent NULLs. The fourth part of the
    key is not a column of its own: the operating day is `DATE(alt_departure_time, "Europe/Oslo")`,
    exact except where an access walk straddles midnight.

    They are NULL whenever `alt_scored` is False, which is the ordinary case — the prediction cache
    only holds journeys a live situation touches — and that is the honest answer rather than a gap.
    An operator read off a leg's line codespace would fill the column and join to nothing: that is
    the JP codespace, which `impact._score_candidate` explicitly refuses to gate on, and two thirds
    of a join key is not a join key.
    """
    alt = getattr(alert, "alternative_journey", None)
    fields: dict = {
        "has_alternative": alt is not None,
        "alt_status": _alternative_status(alert),
    }
    if alt is None:
        return fields
    legs = [leg for leg in (getattr(alt, "legs", None) or []) if getattr(leg, "mode", None) != "foot"]
    # Through the departure prediction, never off the journey — see the docstring.
    departure = getattr(alt, "departure_prediction", None)
    fields.update(
        {
            "alt_minutes_saved": getattr(alt, "minutes_saved", None),
            "alt_departure_time": _iso(getattr(alt, "departure_time", None)),
            "alt_arrival_time": _iso(getattr(alt, "arrival_time", None)),
            "alt_arrives_before_original": getattr(alt, "arrives_before_original", None),
            "alt_at_risk": getattr(alt, "at_risk", None),
            "alt_scored": getattr(departure, "scored", None),
            "alt_operator": getattr(departure, "operator", None),
            "alt_jkey": getattr(departure, "jkey", None),
            "alt_stop_order": getattr(departure, "stop_order", None),
            "alt_lines": ", ".join(
                str(
                    getattr(leg, "public_code", None)
                    or getattr(leg, "line", None)
                    or getattr(leg, "mode", "")
                )
                for leg in legs
            )
            or None,
        }
    )
    return fields


def _observation_fields(alert: Alert) -> dict:
    """What had actually been observed on the line — the one payload item the model is told to quote.

    Stored flat as well as inside `message_input` because this is the claim a traveller-facing message
    makes about the real world ("kl. 17:40 var avgangen innstilt ved …"), and checking it against
    what happened is the whole point of the log. `obs_observed_at` carries the observation's own age:
    the source is rebuilt on a 60/120-minute TTL, so "latest" can be an hour old.
    """
    obs = getattr(alert, "latest_observation", None)
    if obs is None:
        return {}
    return {
        "obs_cancelled": getattr(obs, "cancelled", None),
        "obs_stop_name": getattr(obs, "at_stop_name", None) or getattr(obs, "at_stop", None),
        "obs_delayed_min": getattr(obs, "delayed_min", None),
        "obs_observed_at": _iso(getattr(obs, "observed_at", None)),
    }


#: :func:`_trip_fields`' keys, which only a departure with a change's row carries.
_TRIP_COLUMNS = ("trip_legs", "trip_reason_kind", "trip_reason_index", "trip_p_miss")


def _trip_fields(alert: Alert) -> dict:
    """A departure with a change's four columns (multi-leg U11, KTD16); none on a direct train.

    Absent rather than NULL there, which the table reads the same: a direct row stays the bytes it
    always was, and the streaming insert never rejects one for naming a column the startup ALTER
    added minutes before (~10 min, `.claude/rules/historikk.md`) -- only a trip row can be.

    `trip_legs` is every continuation the verdict was taken over, planned first, as JSON: what the
    nightly labeller resolves «connection held» and the arrival delay from, since `trip.legs` is the
    planned route alone and carries no journey key. The reason kind and index are `trip.problem`'s,
    so NULL below the bar; `trip_p_miss` is the planned route's own KTD7 figure, whatever decided.
    The deciding pair itself is `impact_probability`/`alert_threshold` (:func:`_gated_figure`).
    Cheap enough for :func:`rescored`, so a closing row carries the last look's trip too.
    """
    legs = getattr(alert, "trip_legs_snapshot", None)
    if legs is None:
        return {}
    problem = getattr(getattr(alert, "trip", None), "problem", None)
    return {
        "trip_legs": json.dumps(legs, separators=(",", ":")),
        "trip_reason_kind": getattr(problem, "kind", None),
        "trip_reason_index": getattr(problem, "index", None),
        "trip_p_miss": getattr(alert, "trip_p_miss", None),
    }


def _gated_figure(alert: Alert) -> float | None:
    """The figure the alert gate decided on: a departure with a change's DECIDING reason when it is
    set (multi-leg KTD6), else the boarding call's own. Recording one number and applying another is
    the failure `alert_policy.gate`'s docstring forbids, and `/historikk`'s precision reads these
    two columns as the pair the gate applied. The trip's own columns are :func:`_trip_fields`.
    Restated as `varsel._gated_figure` for the thinning ledger; keep the two in step."""
    deciding = getattr(alert, "deciding_probability", None)
    return deciding if deciding is not None else getattr(alert, "impact_probability", None)


def _gated_bar(alert: Alert) -> float | None:
    """The bar beside :func:`_gated_figure`, from the same source."""
    if getattr(alert, "deciding_probability", None) is not None:
        return getattr(alert, "deciding_threshold", None)
    return getattr(alert, "alert_threshold", None)


def build_row(
    journey: dict,
    alert: Alert,
    *,
    journey_digest: str,
    n_devices: int,
    pushed: bool,
    channel: str = CHANNEL_BROWSER,
    n_subscriptions: int = 0,
    message: str | None,
    message_source: str,
    message_llm: str | None = None,
    message_deterministic: str | None = None,
    message_input: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    message_status: str | None = None,
    prompt_version: str | None = None,
) -> dict:
    """One decision as a BigQuery row.

    `alert` is read through duck typing so this module imports neither varsel nor main at runtime;
    the annotation is resolved only by a type checker (see the TYPE_CHECKING import above), which
    is what keeps a renamed `Alert` field from silently becoming a column of NULLs.

    `message` is the text that was actually sent (or that would have been, where nothing was
    delivered) and `message_source` says which of the two wordings that was. Since 2026-08-07 those
    describe **delivery**, not availability: `message_llm` and `message_deterministic` carry the two
    candidate wordings beside them, both written for every alert-worthy decision, because
    `VARSEL_LLM_ENABLED` now selects which one goes out instead of deciding whether one is written
    at all. Keying `message_source` off "did an LLM write anything" would therefore badge a template
    push as the model's — on the page, and permanently in `varsel_decision_outcomes` once the
    nightly job copies the pair there — so the caller must choose it on what it delivered.

    `message_deterministic` is written on **every** decision, including the ~83 % sub-threshold ones
    no wording is ever sent for; `message_llm` only where a model produced one. That asymmetry is
    what dates a row: a NULL `message_llm` means either "no model wording exists" or "written before
    the column did", while `message_deterministic IS NOT NULL` marks the row as post-change. The
    same NULL-is-a-third-bucket rule `/historikk` states for `n_unsegmented`.

    `message_input` is the exact JSON document the message was (or would have been) written from —
    verbatim, not a reconstruction, so it stays faithful as `llm_message.build_message_input`
    evolves. It is the only field here that records the writer's *input*; everything else describes
    the decision. Note it is this cycle's input: when `message_status` is `ok_cached` the text beside
    it was written from an earlier cycle's input, whose figures were fresher then than these are now.

    The keyword arguments default to None so a caller that has no LLM context — a test, a future
    surface — still produces a valid row rather than needing to fake provenance it does not have.

    `channel` and `n_subscriptions` are U16's (2026-09-13, KTD4): a row now says which channel or
    channels watched this journey definition, and how many webhook subscriptions were on it beside
    `n_devices`' browser registrations. They default to the browser and to zero because that is what
    every row written before them meant and what every caller outside `varsel.check_and_notify`
    still means — the pull read records nothing, and a test building a row has a browser journey in
    front of it.

    **What is NOT here, and must not be added.** No subscription id, client id, destination or
    external reference: the grain stays the journey DEFINITION, which is a route, a window and a
    weekday set and names nobody, and `test_notification_contract` greps this module for exactly
    those names. A count is the whole of what the client side contributes.
    """
    departure = getattr(alert, "departure_time", None)
    decided_at = getattr(alert, "decided_at", None) or datetime.now(tz=_OSLO)
    minutes_to_departure = None
    if departure is not None and decided_at is not None:
        minutes_to_departure = int((departure - decided_at).total_seconds() // 60)
    impact = getattr(alert, "impact", None)
    return {
        **_observation_fields(alert),
        **_alternative_fields(alert),
        **_trip_fields(alert),
        "message_input": message_input,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "message_status": message_status,
        "prompt_version": prompt_version,
        "decided_at": decided_at.isoformat(),
        "journey_digest": journey_digest,
        "from_stop": journey.get("from_stop"),
        "to_stop": journey.get("to_stop"),
        "situation_number": getattr(alert, "situation_number", None),
        "summary": getattr(alert, "summary", None),
        "description": getattr(alert, "description", None),
        "operator": getattr(alert, "operator", None),
        "line": getattr(alert, "line", None),
        "mode": getattr(alert, "mode", None),
        "jkey": getattr(alert, "jkey", None),
        "call_date": _oslo_date(departure).isoformat() if departure is not None else None,
        "stop_place": getattr(alert, "stop_place", None),
        "stop_order": getattr(alert, "stop_order", None),
        "departure_time": departure.isoformat() if departure is not None else None,
        "minutes_to_departure": minutes_to_departure,
        # Whether the two probabilities beside this are a prediction at all. When ET has already
        # declared the boarding call cancelled they are both 1.0 by construction
        # (impact._cancelled_prediction): the app relayed a known fact instead of forecasting one.
        # Recorded so the log can be SEGMENTED, not filtered -- such a row is a real hit for the
        # traveller (relaying what ET knows is most of the value; nobody reads the ET feed on their
        # phone) and a gimme for the models, and one precision figure cannot mean both. Unmarked,
        # the two populations are indistinguishable afterwards and the headline number drifts with
        # the mix between them rather than with anything about the app.
        "et_cancelled": bool(getattr(alert, "cancelled", False)),
        # The destination half of the same split (2026-10-19): the boarding call ran and ET had
        # cancelled the call at `to_stop`, so the 1.0s beside it are a relayed fact too.
        "destination_cancelled": bool(getattr(alert, "destination_cancelled", False)),
        "impact_probability": _gated_figure(alert),
        "cancel_probability": getattr(alert, "cancel_probability", None),
        "ge10_probability": getattr(alert, "ge10_probability", None),
        "delay_p50_min": getattr(alert, "delay_p50_min", None),
        "delay_p90_min": getattr(alert, "delay_p90_min", None),
        "alert_threshold": _gated_bar(alert),
        "alertworthy": bool(getattr(alert, "alertworthy", False)),
        "pushed": bool(pushed),
        "n_devices": n_devices,
        "channel": channel,
        "n_subscriptions": n_subscriptions,
        "message": message,
        "message_source": message_source,
        # The two candidate wordings, recorded whether or not they were the one delivered. Reading
        # either as "what the traveller was told" is the mistake to avoid: that is `message`, and
        # which of these it equals is `message_source`.
        "message_llm": message_llm,
        "message_deterministic": message_deterministic,
        "snapshot_refreshed_at": (
            impact.refreshed_at.isoformat()
            if impact is not None and getattr(impact, "refreshed_at", None) is not None
            else None
        ),
    }


def rescored(row: dict, alert: Alert) -> dict:
    """`row` re-stated with a later cycle's figures — the cheap half of :func:`build_row`.

    What the thinning rule (KTD15) carries between writes. A subject nothing has changed about does
    not get a row, but its LAST look still has to reach the log when it retires, or
    `final_impact_probability` — "the closest look we got before departure" — would describe a cycle
    that was deliberately skipped. So every observation updates a stored row in place, and the one
    left standing when the subject retires is written.

    Deliberately only the fields that come off the `alert` by attribute read: the prediction block,
    the observation, the alternative, and the two time columns derived from them. The expensive half
    of a row — the deterministic template body and the message-input JSON — is exactly the work the
    rule exists to stop repeating, and is carried over from the last row that was written. So a
    closing row's WORDING can be up to one thinning interval older than its figures; the wording the
    pages quote is taken off the first PUSHED row, which is never thinned.

    `pushed` is forced False: a row nobody was notified on must not inherit an earlier cycle's mark,
    which `pushed_any` would OR into a subject that never had a push. That is also what keeps a
    CLOSING row honest now that `pushed` covers a minted webhook event as well as a Web Push (U16):
    the mint is stamped onto the ledger's row, and a later cycle re-stating it must not claim a
    notification it did not issue.

    `channel` and `n_subscriptions` are carried by `**row` rather than recomputed, like the wording
    and the message input beside them: both describe the recipients, which this function has no
    access to. So a closing row's channel can be up to one thinning interval old — a client that
    subscribed to a corridor during the quiet middle reads as 'browser' on that row and as 'both' on
    the next written one, and the view's aggregate is what makes the SUBJECT read 'both' either way.
    """
    departure = getattr(alert, "departure_time", None)
    decided_at = getattr(alert, "decided_at", None) or datetime.now(UTC)
    minutes_to_departure = None
    if departure is not None:
        minutes_to_departure = int((departure - decided_at).total_seconds() // 60)
    impact = getattr(alert, "impact", None)
    return {
        # A trip row's columns go with it when the last look is a direct train's (the same subject).
        **{key: value for key, value in row.items() if key not in _TRIP_COLUMNS},
        **_observation_fields(alert),
        **_alternative_fields(alert),
        **_trip_fields(alert),
        "decided_at": decided_at.isoformat(),
        "minutes_to_departure": minutes_to_departure,
        "et_cancelled": bool(getattr(alert, "cancelled", False)),
        "destination_cancelled": bool(getattr(alert, "destination_cancelled", False)),
        "impact_probability": _gated_figure(alert),
        "cancel_probability": getattr(alert, "cancel_probability", None),
        "ge10_probability": getattr(alert, "ge10_probability", None),
        "delay_p50_min": getattr(alert, "delay_p50_min", None),
        "delay_p90_min": getattr(alert, "delay_p90_min", None),
        "alert_threshold": _gated_bar(alert),
        "alertworthy": bool(getattr(alert, "alertworthy", False)),
        "pushed": False,
        "snapshot_refreshed_at": (
            impact.refreshed_at.isoformat()
            if impact is not None and getattr(impact, "refreshed_at", None) is not None
            else None
        ),
    }


def _row_bytes(row: dict) -> int:
    """About how many bytes this row costs in an insertAll request.

    An ESTIMATE, deliberately: the exact figure is the client's own serialization plus per-row
    insertIds and envelope, and reproducing that here would couple this module to a library detail
    for a bound that is already set at half the documented limit. `ensure_ascii` (json.dumps'
    default) escapes the Norwegian text the rows are full of, so the count errs HIGH — the safe
    direction for a bound whose failure mode is a refused request.

    Never raises. A row this cannot serialize is one `insert_rows_json` will refuse anyway, and
    charging it the full request budget keeps it in a chunk of its own where its refusal costs
    nothing else.
    """
    try:
        return len(json.dumps(row, default=str))
    except Exception:  # noqa: BLE001 - a sizing estimate must never be the thing that loses a cycle
        return _MAX_BYTES_PER_INSERT


def _chunks(rows: list[dict]) -> list[list[dict]]:
    """Split one cycle's rows into requests under BOTH bounds, order preserved.

    A row bigger than the byte bound on its own still goes out, alone: dropping it here would make
    the loss invisible (the counter would read `ok`), and a loop that refuses to place it would
    never terminate — on the poller thread, which is the tick `/health` watches.
    """
    out: list[list[dict]] = []
    chunk: list[dict] = []
    size = 0
    for row in rows:
        row_size = _row_bytes(row)
        if chunk and (
            len(chunk) >= _MAX_ROWS_PER_INSERT or size + row_size > _MAX_BYTES_PER_INSERT
        ):
            out.append(chunk)
            chunk, size = [], 0
        chunk.append(row)
        size += row_size
    if chunk:
        out.append(chunk)
    return out


def _insert_chunk(chunk: list[dict], index: int, total: int) -> int:
    """One insertAll request. Returns how many of ITS rows were accepted; never raises.

    Scoped per chunk on purpose: a schema drift (`errors` returned) and an outage (an exception)
    must each cost their own request's rows and nothing else, which is the whole reason a cycle is
    split at all. The counter is per REQUEST for the same reason — a cycle that lost one chunk of
    four is not an `error` cycle and not an `ok` one.
    """
    try:
        errors = _bq_client.insert_rows_json(TABLE, chunk, timeout=_WRITE_TIMEOUT_S, retry=_WRITE_RETRY)
    except Exception:
        # `event` on this record, which it did not carry until 2026-09-12 — so the one branch that
        # loses a whole request was the one branch a query on event="varsel_decision_write" could
        # not find, and a dashboard built on that event read a losing cycle as a quiet one.
        logger.warning(
            "Could not record %d alert decisions (chunk %d of %d)",
            len(chunk),
            index + 1,
            total,
            exc_info=True,
            extra={
                "fields": {
                    "event": "varsel_decision_write",
                    "result": "error",
                    "chunk": index + 1,
                    "chunks": total,
                    "n_rows": len(chunk),
                }
            },
        )
        # The write fails open by design, which is exactly why the failure needs a counter: the
        # log going quiet is otherwise invisible (KTD11).
        metrics.DECISION_WRITE_TOTAL.labels(result="error").inc()
        return 0
    if errors:
        # insert_rows_json reports per-row failures instead of raising, so a schema drift shows up
        # here as a silently short write unless it is logged explicitly.
        logger.warning(
            "%d of %d alert decisions rejected by BigQuery (chunk %d of %d): %s",
            len(errors),
            len(chunk),
            index + 1,
            total,
            errors[:3],
            extra={
                "fields": {
                    "event": "varsel_decision_write",
                    "result": "partial",
                    "chunk": index + 1,
                    "chunks": total,
                }
            },
        )
        metrics.DECISION_WRITE_TOTAL.labels(result="partial").inc()
        return len(chunk) - len(errors)
    logger.info(
        "Recorded %d alert decisions (chunk %d of %d)",
        len(chunk),
        index + 1,
        total,
        extra={
            "fields": {
                "event": "varsel_decision_write",
                "result": "ok",
                "n_rows": len(chunk),
                "chunk": index + 1,
                "chunks": total,
            }
        },
    )
    metrics.DECISION_WRITE_TOTAL.labels(result="ok").inc()
    return len(chunk)


def record(rows: list[dict], *, beat: Callable[[], None] | None = None) -> int:
    """Append decision rows. Returns how many were accepted; never raises.

    A failure here is logged and dropped. The alternative — letting it propagate — would put a
    BigQuery outage on the path between a live disruption and a traveller's phone.

    Sent as one insertAll request per `_chunks` bound, which at today's volumes is still exactly one
    request per cycle; a request that fails costs its own rows and no others.

    `beat` is the poller's liveness seam, injected rather than imported for the reason every seam in
    this package is (this module imports neither `main` nor `varsel`): `varsel._heartbeat`, which is
    `main._beat` when one was wired and a no-op otherwise. **A chunk boundary is a progress point in
    exactly `/health`'s sense** — the threshold means "no STEP has finished in
    `HEALTH_STALE_AFTER_S`" — so without it several sequential chunks, each able to spend
    `_WRITE_RETRY`'s deadline plus an attempt, are one unbroken silence inside the `varsel_notify`
    phase, and the restart that follows comes up with both planned-call tables cold. Called AFTER
    each chunk, including a failed one: a chunk that failed slowly is still proof the thread is
    moving, which is the same rule `main._phase` follows by beating from its `finally`.

    Guarded here as well as inside the seam. `record` promises never to raise and the caller's
    rollback depends on it: a beat that threw would unwind `_record_decisions` before its ledger
    rollback ran, which is the very loss this function's return value exists to report.
    """
    if not rows:
        return 0
    if not ensure_table():
        # Two different states wear this one False, and only one of them is nothing to report.
        # No client configured is a deployment that never intended to record — counting an
        # `error` there would page on every such environment forever. A configured client whose
        # structural CREATE failed is the 2026-08-07 mode: a BigQuery table-update rate limit (5
        # per 10 s) turns a cosmetic description edit into a decision log that records NOTHING,
        # and `ensure_table` only logs one WARNING per retry window — below the runbook's ERROR
        # rule. Uncounted, the counter simply flatlines, which on a dashboard is indistinguishable
        # from "no alerts happened" — exactly the silent loss KTD11 added this counter to catch.
        if _bq_client is not None:
            metrics.DECISION_WRITE_TOTAL.labels(result="error").inc()
        return 0
    chunks = _chunks(rows)
    accepted = 0
    for index, chunk in enumerate(chunks):
        accepted += _insert_chunk(chunk, index, len(chunks))
        if beat is not None:
            try:
                beat()
            except Exception:  # a liveness signal must never lose a cycle's rows
                logger.warning(
                    "Heartbeat seam failed during the decision write; continuing",
                    exc_info=True,
                    extra={"fields": {"event": "varsel_decision_write", "result": "beat_failed"}},
                )
    return accepted


# --- Labelling-freshness detector (R8, KTD7) ------------------------------------------------------
#
# The nightly outcome labelling (`varsel-label-daily`, a BigQuery scheduled query) runs OUTSIDE
# this process, and nothing in the app consumes its output on any hot path — so when it dies (a
# schema drift breaking the registered copy of the SQL is the documented way) the only symptom is
# /historikk quietly filling with `venter`, and past the job's 10-day retry window every
# still-unlabelled decision is lost forever. This check turns that silence into a structured ERROR
# within hours. It rides the poller's wall-clock tick (see main.poll_for_changes) rather than the
# SX rebuild, because a quiet night with no SX movement is exactly when a dead labelling job must
# still be noticed — and deliberately not as a `_rebuild_all` phase, whose list is pinned by
# test_poller_refresh.py.

# At most one BigQuery check per 6 hours: hours of latency are irrelevant against a 10-day loss
# window, and the check must cost jobs on the scale of the scheduled query it watches, not on the
# scale of the 60 s tick that hosts it.
_FRESHNESS_CHECK_INTERVAL_S = 6 * 3600
# The labelling job runs daily at 04:00 UTC, so a healthy MAX(labelled_at) is under ~24 h old plus
# jitter; 48 h means at least one whole run has failed to land.
_LABELLING_STALE_AFTER_H = 48.0
# Bounded like _ENSURE_TIMEOUT_S and for the same reason: this query runs on the sole poller
# thread, which must never inherit "wait forever" from a new call site.
_FRESHNESS_TIMEOUT_S = 60.0

# Both aggregates are partition-bounded, and neither bound is cosmetic. `call_date` is
# varsel_decision_outcomes' partition key while `labelled_at` is NOT, so an unbounded
# MAX(labelled_at) is a full-table scan repeated every 6 h forever — and 10 days is the labelling
# job's own retry window, past which a fresher row cannot appear. varsel_decisions is
# PARTITION BY DATE(decided_at), so the 48 h work-to-label cut carries a DATE() predicate beside
# it (3 days: the timestamp cut can straddle at most two UTC date partitions, plus margin) or it
# prunes nothing. Nothing in this repo executes SQL, so test_decisions_freshness.py pins the
# bounds as source text.
_FRESHNESS_SQL = """\
SELECT
  (SELECT MAX(labelled_at)
     FROM disruptions.varsel_decision_outcomes
    WHERE call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY)) AS latest_labelled,
  (SELECT COUNT(*)
     FROM disruptions.varsel_decisions
    WHERE DATE(decided_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 11 DAY)
      AND call_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY)
                        AND DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)) AS labellable_decisions
"""

_freshness_last_checked: float | None = None  # monotonic stamp of the last attempted check
_freshness_quiet_logged = False  # the no-work-to-label notice fires at most once per process


def check_labelling_freshness() -> None:
    """Alert when the nightly labelling job has stopped landing verdicts. Never raises.

    The decision is R8's work-to-label gate, one query with two aggregates:

    * labelling stale (MAX(labelled_at) older than 48 h) **or absent** (NULL) while decisions the
      job has HAD THE CHANCE to label are waiting → ``logger.error`` with
      ``event="labelling_stale"``, carrying ``hours_stale`` when a timestamp exists and
      ``reason="no_labelled_rows"`` when none does. The absent case is the one a naive age check
      gets backwards: a prd outage that outlasts the 10-day window empties the bounded MAX to NULL
      precisely when loss becomes permanent, and NULL must alert there, not read as "nothing to
      report". The ERROR re-fires once per 6 h window for as long as the condition holds — it is
      an alert condition, not an event.
    * nothing labellable yet: at most ONE INFO per process, never an ERROR.

      **The work gate counts `call_date` in [today-10, today-2], not decisions from the last
      48 h.** The nightly job can only label a call that has run, and it joins `last_recorded`,
      which stops at yesterday — so a decision about today's or tomorrow's departure is not late,
      it is not yet labellable. Counting the last 48 h of decisions made every fresh environment
      (a rebuilt dev table, tst the moment a journey registers, prd's first day) fire
      ``reason="no_labelled_rows"`` — "the job appears to have stopped" — from the very first
      decision, every 6 h for over a day, when nothing had yet been labellable at all. An ERROR
      that cries wolf on day one of every deployment is worse than no detector: it teaches the
      reader to filter the event name out. The lower bound is the same 10 days as the MAX above,
      so the two aggregates describe one window; `DATE(decided_at) >= today-11` is the partition
      prune (a decision precedes its call by at most ~1 day, measured), not part of the gate.
    * fresh: log nothing.

    :data:`metrics.LABELLING_LAST_SUCCESS` is set whenever a labelled row exists at all, stale or
    fresh (the PromQL age rule is ``time() - value``, so the gauge must be the observation, never
    "now"). Deliberately not only on the healthy path: an age alert cannot age an absent series,
    so a gauge that disappeared exactly when labelling stopped would be useless to the rule
    watching it.

    Fail-open like everything else in this module: a query failure is a WARNING with
    ``result="check_failed"`` — the detector must never take the poller down. The 6 h window is
    stamped before the query, so a failing BigQuery backs off for the whole window instead of
    being re-asked on every 60 s tick.
    """
    global _freshness_last_checked, _freshness_quiet_logged
    if _bq_client is None:
        # No client, no check — and no window spent: configure() may land a moment later, and the
        # first configured check should then run immediately.
        return
    now = monotonic()
    if (
        _freshness_last_checked is not None
        and now - _freshness_last_checked < _FRESHNESS_CHECK_INTERVAL_S
    ):
        return
    _freshness_last_checked = now
    try:
        row = next(iter(_bq_client.query(_FRESHNESS_SQL).result(timeout=_FRESHNESS_TIMEOUT_S)))
        latest = row.latest_labelled
        labellable = int(row.labellable_decisions or 0)
    except Exception:
        logger.warning(
            "Labelling freshness check failed; retrying in %d h",
            _FRESHNESS_CHECK_INTERVAL_S // 3600,
            exc_info=True,
            extra={"fields": {"event": "labelling_stale", "result": "check_failed"}},
        )
        return
    hours_stale: float | None = None
    if latest is not None:
        if latest.tzinfo is None:
            # BigQuery TIMESTAMPs come back tz-aware UTC; normalize a naive one rather than let
            # the subtraction below throw on the poller thread.
            latest = latest.replace(tzinfo=UTC)
        hours_stale = (datetime.now(tz=UTC) - latest).total_seconds() / 3600.0
        # Set whenever a labelled row EXISTS, fresh or stale — not only on the healthy path, which
        # is where it was and which made the series mean "last labelling, but reported only while
        # it is going well". A gauge named last_success that vanishes exactly when labelling stops
        # is the opposite of what an age alert needs: the runbook computes `time() - value`, and a
        # value that is simply absent cannot be aged at all.
        metrics.LABELLING_LAST_SUCCESS.set(latest.timestamp())
        if hours_stale <= _LABELLING_STALE_AFTER_H:
            return
    if labellable > 0:
        fields: dict[str, Any] = {"event": "labelling_stale", "labellable_decisions": labellable}
        if hours_stale is not None:
            fields["hours_stale"] = round(hours_stale, 1)
            detail = f"latest outcome was labelled {hours_stale:.0f} h ago"
        else:
            fields["reason"] = "no_labelled_rows"
            detail = "no outcome labelled in the last 10 days"
        logger.error(
            "varsel-label-daily appears to have stopped: %s while %d decisions old enough to "
            "have been labelled are still waiting. Decisions still unlabelled after the job's "
            "10-day retry window are lost for good.",
            detail,
            labellable,
            extra={"fields": fields},
        )
        return
    if not _freshness_quiet_logged:
        _freshness_quiet_logged = True
        logger.info(
            "No decisions old enough to have been labelled yet, so stale labelling is expected "
            "here; the freshness check stays quiet until there is work to label",
            extra={"fields": {"event": "labelling_stale", "result": "no_labellable_decisions"}},
        )
