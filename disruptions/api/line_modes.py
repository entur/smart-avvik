"""Each line's transport mode as the Journey Planner states it (KIX-23).

The `mode` the webhook payload, the journey-alerts read and journey-status carry is this, in JP's own
`TransportMode` vocabulary (`rail`, `bus`, `metro`, `tram`, `water`, `coach`, ...) -- the one the
Entur app already speaks. Neither the codespace nor the feed can say it: Ruter's T-bane and trikk
are scored in the bus family under `RUT`, and SIRI-ET's `vehicleModes` is empty for every RUT, SKY
and TRO journey (census of 2026-09-29). One JP query answers every line in the country (~4 700 lines,
~240 KB, under half a second), which `main.refresh_line_modes` asks once a day.

It is the LINE's mode: a replacement bus on a Vy rail line is `rail`, which is what a client
filtering on its favourite route's mode wants. A stdlib leaf, so `journey_status` may import it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from notification_wording import RAIL_CODESPACES

TTL = timedelta(hours=24)

_modes: dict[str, str] = {}
_fetched_at: datetime | None = None


def transport_mode(operator: str | None, line: str | None) -> str | None:
    """JP's mode for `line`; `rail` for a rail codespace JP has not answered for; otherwise null.

    Never `bus` by elimination: that is exactly what would call a T-bane a bus.
    """
    mode = _modes.get(line or "")
    if mode is None and operator in RAIL_CODESPACES:
        return "rail"
    return mode


def due(now: datetime) -> bool:
    return _fetched_at is None or now - _fetched_at >= TTL


def set_modes(lines: list[dict], now: datetime) -> int:
    """Replace the map from JP's `lines { id transportMode }`. Only called on a good reply, so a
    failed fetch keeps yesterday's map rather than turning every non-rail line null."""
    global _modes, _fetched_at
    _modes = {l["id"]: l["transportMode"] for l in lines if l.get("id") and l.get("transportMode")}
    _fetched_at = now
    return len(_modes)


def reset() -> None:
    global _modes, _fetched_at
    _modes, _fetched_at = {}, None
