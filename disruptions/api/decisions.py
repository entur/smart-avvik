"""The alert-decision log: persist what Avviksvarsel concluded, so it can be checked later.

Until this module existed, the only record of what a traveller was told was Cloud Logging, and
nothing anywhere recorded what actually *happened* to the train we warned about. That makes the
one question worth asking about an alerting system unanswerable: when we alerted, were we right?

What is recorded is **every decision**, not every push. A row is written for each situation the
matcher matched to a registered journey — including the ones that stayed below the alert
threshold. That asymmetry is the whole point: a journey we did not alert on leaves no trace
otherwise, so `miss` and `correct_reject` would be unobservable and any "precision" figure would
be measuring only the half of the confusion matrix that flatters us.

Rows are written once per refresh cycle per (journey definition x situation x departure), so the
same decision reappears as its prediction evolves. Those repeats are not duplicates to be
suppressed — they are the lead-time series, the axis alerting quality varies along most
(eval/impact_eval.py: alertable ~30-60 min pre-departure, and ask-time features decay).

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

import logging
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # type-checker only: no runtime import, so the varsel <-> decisions cycle stays broken
    from varsel import Alert

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

TABLE = "disruptions.varsel_decisions"

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

# The DDL runs on the startup path of a single always-on pod. Every other bq call in this app is
# unbounded, but this one is new and sits in front of the poller thread, so it gets an explicit
# ceiling rather than inheriting "wait forever".
_ENSURE_TIMEOUT_S = 60.0


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
    try:
        _bq_client.query(_STRUCTURE_SQL).result(timeout=_ENSURE_TIMEOUT_S)
        _table_ready = True
        _ensure_failed_at = None
        logger.info("Decision log ready (%s)", TABLE)
    except Exception:
        # Not fatal: recording is an observability feature, and the alerting path it observes must
        # keep working without it. Backed off rather than retried every tick.
        _ensure_failed_at = monotonic()
        logger.warning("Could not ensure %s; decisions will not be recorded", TABLE, exc_info=True)
        return False
    if _DESCRIPTIONS_SQL.strip():
        try:
            _bq_client.query(_BEST_EFFORT_MARKER + _DESCRIPTIONS_SQL).result(
                timeout=_ENSURE_TIMEOUT_S
            )
        except Exception:
            # The table is already usable; what failed is documentation. Say so at warning level so
            # a persistently stale `bq show --schema` is discoverable, and carry on recording.
            logger.warning(
                "Could not refresh %s column descriptions; the schema comments may be stale. "
                "Decisions are still being recorded.",
                TABLE,
                exc_info=True,
                extra={"fields": {"event": "varsel_decision_write", "result": "stale_descriptions"}},
            )
    return _table_ready


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
      ``alert.impact``: `varsel.match_journey` back-fills one trip's lookup onto every alert it
      produced, so an alert whose worst reading came from a collapsed stop ref carries advice its
      own `/impact` response does not (the 2026-08-05 `llm_message` fix, which recorded
      `has_alternative=true` beside a message input with no `alternativ_reise`).
    * ``suppressed`` — `impact.original_is_best`: the selection ran against a real pool and kept the
      traveller on the train they already have. A verdict, not an absence.
    * ``none_found`` — `impact.alternative_subject` is set, so the lookup ran to completion, and it
      produced neither an offer nor a "stay put".
    * ``not_worth_lookup`` — nothing above: no lookup was issued for this response. The impact gate
      (`impact._worth_an_alternative`) is the reason for the overwhelming majority; a reading taken
      from a non-canonical stop ref, where `match_journey` deliberately skips the blocking call, also
      lands here.

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


def build_row(
    journey: dict,
    alert: Alert,
    *,
    journey_digest: str,
    n_devices: int,
    pushed: bool,
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
        "impact_probability": getattr(alert, "impact_probability", None),
        "cancel_probability": getattr(alert, "cancel_probability", None),
        "ge10_probability": getattr(alert, "ge10_probability", None),
        "delay_p50_min": getattr(alert, "delay_p50_min", None),
        "delay_p90_min": getattr(alert, "delay_p90_min", None),
        "alert_threshold": getattr(alert, "alert_threshold", None),
        "alertworthy": bool(getattr(alert, "alertworthy", False)),
        "pushed": bool(pushed),
        "n_devices": n_devices,
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


def record(rows: list[dict]) -> int:
    """Append decision rows. Returns how many were accepted; never raises.

    A failure here is logged and dropped. The alternative — letting it propagate — would put a
    BigQuery outage on the path between a live disruption and a traveller's phone.
    """
    if not rows or not ensure_table():
        return 0
    try:
        errors = _bq_client.insert_rows_json(TABLE, rows)
    except Exception:
        logger.warning("Could not record %d alert decisions", len(rows), exc_info=True)
        return 0
    if errors:
        # insert_rows_json reports per-row failures instead of raising, so a schema drift shows up
        # here as a silently short write unless it is logged explicitly.
        logger.warning(
            "%d of %d alert decisions rejected by BigQuery: %s",
            len(errors),
            len(rows),
            errors[:3],
            extra={"fields": {"event": "varsel_decision_write", "result": "partial"}},
        )
        return len(rows) - len(errors)
    logger.info(
        "Recorded %d alert decisions",
        len(rows),
        extra={"fields": {"event": "varsel_decision_write", "result": "ok", "n_rows": len(rows)}},
    )
    return len(rows)
