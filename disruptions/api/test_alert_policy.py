"""Checks for the shared alert gate (alert_policy) and the re-export that keeps varsel's names.

Runs standalone (`uv run --with fastapi --with pydantic --with pywebpush python
test_alert_policy.py`) or under pytest. No BigQuery, no network: nothing here imports main.

The gate moved out of varsel on 2026-08-06 so `impact` can read it without importing `varsel` back.
That makes two things worth pinning: the numbers themselves (a silent edit to the tuned table
changes what every traveller is told), and the *shape* of the move — one table, re-exported rather
than copied, and no import cycle in either direction.
"""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import alert_policy
import varsel
from alert_policy import min_threshold, threshold_for

_API_DIR = os.path.dirname(os.path.abspath(__file__))


def test_operator_override_and_default():
    # GOA runs conservative, so its tuned cut is below the default (eval/tune_alert_thresholds.py).
    assert threshold_for("GOA") == 0.50
    # Rail is deliberately absent from the table: 0.65 is validated for the NSB family, and VYG (the
    # codespace varsel actually records for rail) has no entry either.
    assert threshold_for("VYG") == 0.65
    # No operator yet — the matcher asks before it knows which train it found.
    assert threshold_for(None) == 0.65
    # An operator we have never tuned falls back to the default rather than to anything looser.
    assert threshold_for("ZZZ") == 0.65


def test_min_threshold_is_the_floor_of_the_table():
    assert min_threshold() == 0.50
    assert min_threshold() == min(alert_policy._OPERATOR_ALERT_THRESHOLD.values())
    # The floor must never sit above the default, or a journey on a default-cut operator could be
    # alert-worthy and still be screened out of the work the floor gates.
    assert min_threshold() <= threshold_for(None)


def test_no_sx_population_is_cut_at_its_own_threshold():
    """A call no SX message named is gated on `_NOSX_ALERT_THRESHOLD`, whatever its operator.

    The population is what selects the cut, not the operator, because the operator table is tuned
    against predictions calibrated on flagged calls. Feeding a no-SX probability into GOA's 0.50
    would apply a cut fitted to a different distribution.
    """
    assert threshold_for("VYG", has_situation=False) == alert_policy._NOSX_ALERT_THRESHOLD
    # An operator override does NOT win over the population: GOA's 0.50 is a flagged-call number.
    assert threshold_for("GOA", has_situation=False) == alert_policy._NOSX_ALERT_THRESHOLD
    assert threshold_for(None, has_situation=False) == alert_policy._NOSX_ALERT_THRESHOLD


def test_has_situation_is_keyword_only_and_defaults_to_the_flagged_cut():
    """Every caller that predates the second population must keep its exact meaning.

    Positional would be worse than merely ugly here: `threshold_for(op, False)` reads as "not
    something", and the something it turns off is the operator table.
    """
    assert threshold_for("GOA") == threshold_for("GOA", has_situation=True) == 0.50
    try:
        threshold_for("GOA", False)  # type: ignore[misc]
    except TypeError:
        pass
    else:  # pragma: no cover - only reached if the signature regresses
        raise AssertionError("has_situation must be keyword-only")


def test_no_sx_is_cut_HIGHER_than_a_published_disruption_is():
    """The direction is the part that is easy to get backwards, so it is pinned rather than left to
    a comment.

    The intuition says a population with a 12x lower base rate should be cut LOWER, and reasoning
    from "recover the recall the old broken path had" says so too. Both are wrong. Without a message
    the only signal is the last hour of observation, so a no-SX probability decays with the ask
    horizon much faster than a flagged one — measured out of sample, precision at 0.65 falls to
    0.712 one bucket out and the far buckets fire nothing at all. And the alert reaches the
    traveller with no explanation attached, because there is none to attach. Both point the same
    way: a higher bar, not a lower one.
    """
    assert alert_policy._NOSX_ALERT_THRESHOLD > alert_policy._ALERT_THRESHOLD
    assert threshold_for("VYG", has_situation=False) > threshold_for("VYG")


def test_no_sx_cut_sits_at_or_above_the_pre_screen_floor():
    """`min_threshold` gates work that only alert-worthy trips should pay for, and it does not know
    about this population. That is safe only while the no-SX cut is the higher of the two — below
    the floor, a no-SX trip could be alert-worthy and still be screened out before anything looked
    at it. If the cut is ever lowered past 0.50, `min_threshold` has to take the min over both."""
    assert alert_policy._NOSX_ALERT_THRESHOLD >= min_threshold()


def test_the_gate_is_the_table_plus_the_comparison_and_nothing_else():
    """`gate` is what every notification surface applies, so it must not be able to disagree with
    the table it reads (U1 scenario 2).

    Three populations, three bars, and each checked AT the bar, just below and just above — the
    boundary is the whole content of a gate, and `>` where `>=` was meant retires every alert that
    lands exactly on a tuned cut. Rail (VYG, the default 0.65 the family is validated at), bus
    (RUT, an override at 0.75, so a gate that ignored the operator would read 0.65 here) and the
    no-SX population (0.85, whose cut is selected by the POPULATION and not by the operator).

    `None` is its own case and the one worth stating out loud: there is no calibrated score, and
    reading its absence as "not bad" would be the manufactured all-clear this codebase refuses
    everywhere else. It still reports the bar, because `alert_threshold` is recorded on the
    decision row whether or not anything cleared it.
    """
    for operator, has_situation, bar in (
        ("VYG", True, 0.65),
        ("RUT", True, 0.75),
        ("VYG", False, 0.85),
        ("RUT", False, 0.85),
    ):
        expected = threshold_for(operator, has_situation=has_situation)
        assert expected == bar, "the table itself moved; re-read the tuning note before editing"
        for probability, alertworthy in (
            (bar, True),  # AT the bar clears it
            (bar - 0.01, False),
            (bar + 0.01, True),
            (None, False),
            (0.0, False),
            (1.0, True),
        ):
            result = alert_policy.gate(probability, operator, has_situation=has_situation)
            assert result == (expected, alertworthy), (operator, has_situation, probability)
            # Named fields, not just a 2-tuple: both halves are written to varsel_decisions and a
            # positional swap would put the verdict in the threshold column.
            assert (result.threshold, result.alertworthy) == result

    # The keyword-only default matches `threshold_for`'s, so a caller that predates the second
    # population keeps asking about the first.
    assert alert_policy.gate(0.7, "VYG") == alert_policy.gate(0.7, "VYG", has_situation=True)


def test_varsel_reexports_the_same_gate_not_a_copy():
    # Identity, not equality: a forked table would still compare equal today and drift tomorrow.
    assert varsel.threshold_for is alert_policy.threshold_for
    assert varsel.min_threshold is alert_policy.min_threshold
    # `gate` joined them on 2026-09-08 and is the one `_collect_alerts` actually applies.
    assert varsel.gate is alert_policy.gate


def test_varsel_trouble_delay_min_still_resolves():
    # main's /demo scores realized outcomes against this bar; it read it off varsel before the move.
    assert varsel.TROUBLE_DELAY_MIN == alert_policy.TROUBLE_DELAY_MIN
    assert alert_policy.TROUBLE_DELAY_MIN == 10


def test_varsel_gates_the_alternative_lookup_at_the_policy_floor():
    """`varsel._impact` must reach the floor through :func:`min_threshold`, and reach it at all.

    The gate lives one module away now, and this call site is inside `check_and_notify`'s
    per-journey try/except: a name that failed to resolve here would take push alerting down while
    leaving the suite green — the 2026-07-29 failure mode, in the same place.
    """
    seen = []

    def provider(**kwargs):
        # Falls off the end, i.e. answers None: `_impact` hands the response straight back and this
        # check is about the arguments it went out with, not about what came home.
        seen.append(kwargs)

    varsel.configure(impact_provider=provider, stops_provider=list)
    try:
        journey = {"from_stop": "NSR:StopPlace:337", "to_stop": "NSR:StopPlace:418"}
        start = datetime(2026, 8, 6, 7, 0, tzinfo=UTC)
        end = start + timedelta(minutes=30)
        varsel._impact(journey, start, end, with_alternative=True)
        varsel._impact(journey, start, end, with_alternative=False)
    finally:
        varsel._impact_provider = None

    assert [kw["min_impact"] for kw in seen] == [min_threshold(), 0.0]


def _import_in_subprocess(statement: str) -> subprocess.CompletedProcess:
    """Import modules in a fresh interpreter — the only way to test import *order*.

    Once this process has imported both, sys.modules makes every later order a no-op, so the cycle
    this module exists to prevent could reappear and the in-process assertion would still pass.
    """
    return subprocess.run(
        [sys.executable, "-c", statement],
        cwd=_API_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def test_impact_and_varsel_import_in_either_order():
    for statement in ("import impact, varsel", "import varsel, impact"):
        result = _import_in_subprocess(statement)
        assert result.returncode == 0, f"`{statement}` failed:\n{result.stderr}"


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} alert-policy checks passed.")


if __name__ == "__main__":
    _run()
