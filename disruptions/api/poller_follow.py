"""Following the last build: how a pod loads what the lease holder's cycle wrote (zero-downtime U3).

The mechanism only. WHAT is followed -- `main._FOLLOWED_CACHES`, `main._FOLLOWED_LOOKUPS` and the
stamps they read -- stays in `main`, because those entries are lambdas over `main`'s own caches and
refresh functions; `main` composes the three functions here with them, passing its BigQuery client,
the registries and the loaders on every call. This module imports no sibling, so it can never reach
back into `main` and no import cycle can appear through it.

No age bound (KTD6). The 10-minute one this replaced was written for a dead predecessor under
Recreate; a live leader on a quiet night legitimately leaves tables older than that, and a pod that
refused them would answer 503 beside a leader serving the same table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime

from google.api_core.exceptions import NotFound

logger = logging.getLogger(__name__)


def tables_built(client, tables: tuple[str, ...], *, timeout_s: float) -> datetime | None:
    """The newest modified time of `tables`, None when none exists. One metadata read each, which
    BigQuery does not bill: this is all a follower's tick costs while nothing moved (R13). A table
    that does not exist yet (the bus family before its first predict, a first deploy) is skipped;
    any other failure raises to the caller."""
    newest = None
    for table in tables:
        try:
            modified = client.get_table(table, timeout=timeout_s).modified
        except NotFound:
            continue
        if modified is not None and (newest is None or modified > newest):
            newest = modified
    return newest


def load_from_last_build(
    entry: tuple, built_of: Callable[[tuple[str, ...]], datetime | None], *, leader: bool
) -> str:
    """Load one serving cache from its tables when they moved past the stamp it holds.

    `entry` is one `(source tables, stamp, load(built, leader))` of `main._FOLLOWED_CACHES`, and
    `built_of` reads the tables' newest modified time (:func:`tables_built`).

    Returns "loaded", "unchanged", "missing" (no source table exists) or "error" (the load served
    what it could and said it was incomplete, stamped below `built` so the next tick loads again);
    raises what the load raises, leaving the cache and its stamp as they were. `leader` is whether
    the caller holds the lease: for the call-prediction cache it decides whether the situations'
    English is machine-translated (KTD12) and whether the load checks the lease and beats between
    its two queries (U2's fence) -- a follower has no lease to lose."""
    tables, stamp, load = entry
    built = built_of(tables)
    if built is None:
        return "missing"
    current = stamp()
    if current is not None and built <= current:
        return "unchanged"
    if load(built, leader) is False:
        return "error"
    return "loaded"


def follow_last_build(
    caches: Mapping[str, tuple],
    lookups: Mapping[str, tuple],
    lookups_built: dict[str, datetime],
    *,
    load_cache: Callable[..., str],
    built_of: Callable[[tuple[str, ...]], datetime | None],
    refresh_line_modes: Callable[[], object],
) -> dict[str, str]:
    """Bring the serving caches and per-pod lookups up to the tables the leader last built.

    `load_cache(name, leader=False)` loads one of `caches` (:func:`load_from_last_build`); each of
    `lookups` is `(source tables, load() -> bool)`, stamped in `lookups_built` only on success.
    Each load stands alone: a missing, unreadable or old-schema table leaves that one cache as it
    was ("error"/"missing") for the next tick or the leader's cycle, and nothing here raises. One
    `event="follow_last_build"` record per call carries every outcome; `loaded` lists what moved,
    which on a quiet tick is nothing.
    """
    result: dict[str, str] = {}
    for name in caches:
        try:
            result[name] = load_cache(name, leader=False)
        except Exception:
            logger.exception("Could not load %s from the last build; keeping the copy held", name)
            result[name] = "error"
    for name, (tables, load) in lookups.items():
        try:
            built = built_of(tables)
            if built is None:
                result[name] = "missing"
            elif name in lookups_built and built <= lookups_built[name]:
                result[name] = "unchanged"
            elif load():
                lookups_built[name] = built
                result[name] = "loaded"
            else:
                # Served what it could and said so: unstamped, so the next tick asks again rather
                # than waiting for the table to move (the planned tables move once a day).
                result[name] = "error"
        except Exception:
            logger.exception("Could not load the %s lookup; keeping the copy held", name)
            result[name] = "error"
    # Line modes come from Journey Planner, not a table, and keep their own 24 h TTL.
    try:
        refresh_line_modes()
    except Exception:
        logger.exception("Line modes load failed; serving the previous map")
    loaded = [name for name, outcome in result.items() if outcome == "loaded"]
    logger.info(
        "Followed the last build: %s",
        ", ".join(loaded) or "nothing moved",
        extra={"fields": {"event": "follow_last_build", "loaded": loaded, "outcomes": result}},
    )
    return result
