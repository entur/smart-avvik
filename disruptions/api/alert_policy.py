"""The alert gate: how bad a prediction has to be before the traveller is told.

One operating point, several readers. :func:`varsel.check_and_notify` applies it to decide whether a
matched train earns a push; ``/demo`` scores its retrospective verdicts against it so the demo
replays exactly what the push pipeline would have decided; and the ``/impact`` matcher asks the same
question about a *candidate alternative* — "would the trip we are about to recommend be alert-worthy
itself?" — because recommending a train that is about to be alerted on is worse than saying nothing.

That last reader is why this is a module of its own rather than part of ``varsel``, where the table
lived until 2026-08-06. ``varsel`` imports ``impact``, so ``impact`` importing back would be a
circular import. Injection (``impact.configure``) is the wrong tool here: that pattern exists for
state the poller owns and hands down, and this is a constant table with no lifecycle at all — it is
imported, like :mod:`deployment`.

Nothing here reads the environment or the clock: the numbers are tuned offline
(``eval/tune_alert_thresholds.py``) and change only by editing this file.
"""

from __future__ import annotations

# Push a match only when the worst matching train's impact probability crosses this — the same
# operating threshold as /demo and eval/impact_eval.py (>=91 % precision at every ask horizon).
_ALERT_THRESHOLD = 0.65
# A journey counts as "trouble" when cancelled or >= this many minutes late — the event the impact
# probability predicts. Public because /demo scores its realized outcomes against the same bar
# (main._DEMO_TROUBLE_DELAY_MIN), as it takes its threshold from threshold_for(): one definition of
# "trouble" behind both surfaces.
TROUBLE_DELAY_MIN = 10

# Per-operator alert threshold overrides (default _ALERT_THRESHOLD). The pooled bus family is
# calibrated per operator, but a single 0.65 cut under-/over-alerts several operators; these
# values align every operator at ~90 % pooled precision on the serving-matched holdout
# (eval/tune_alert_thresholds.py, tuned 2026-07-07 — re-run after retrains/platt refits).
# GOA/TRO/ATB run conservative → lower cut buys recall (GOA .81→.87, ATB .65→.79) at the bar;
# RUT/KOL/FIN/NOR/MOR sit below the bar at 0.65 → higher cut restores precision (MOR .80→.96).
# SOF/SKY stay on the default (already at/near the bar); OST and GCO have too little holdout
# evidence to override (OST: ~6 alerts at any >=90 % cut). NSB is the rail family (0.65
# validated at ~91 % in eval/impact_eval.py) and is deliberately absent.
_OPERATOR_ALERT_THRESHOLD: dict[str, float] = {
    "ATB": 0.55,
    "FIN": 0.75,
    "GOA": 0.50,
    "KOL": 0.75,
    "MOR": 0.90,
    "NOR": 0.80,
    "RUT": 0.75,
    "TRO": 0.55,
}


def threshold_for(operator: str | None) -> float:
    """The alerting threshold for an operator (None -> the default). Shared by the varsel
    matcher and the /demo verdicts so both surfaces apply the same operating point."""
    return _OPERATOR_ALERT_THRESHOLD.get(operator or "", _ALERT_THRESHOLD)


def min_threshold() -> float:
    """The lowest bar any operator alerts at — the floor of the whole gate (GOA, 0.50).

    A named policy fact rather than a ``min()`` over the table at each call site, because the
    callers that need it ask *before* the operator is known: the operator is a property of the
    matched train, which only exists after the matcher has run. Spreading a `min()` over another
    module's private dict would put the table's shape in two places and let the two drift.

    Its callers use it to pre-screen work that is only worth doing for an alert-worthy trip (the
    alternative-journey lookup in :func:`varsel._impact` is a blocking external call). The floor is
    the safe cut for that: no alert-worthy journey is ever denied the work, it only means a few
    trips that are sub-threshold *for their own operator* pay for it anyway.
    """
    return min(_OPERATOR_ALERT_THRESHOLD.values(), default=_ALERT_THRESHOLD)
