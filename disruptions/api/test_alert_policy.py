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


def test_varsel_reexports_the_same_gate_not_a_copy():
    # Identity, not equality: a forked table would still compare equal today and drift tomorrow.
    assert varsel.threshold_for is alert_policy.threshold_for
    assert varsel.min_threshold is alert_policy.min_threshold


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
