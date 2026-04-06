"""
stream_utils.py – Shared helpers for go2rtc stream introspection.

These utilities are used by both main.py (/api/stream-status) and
routers/creator.py (/api/creator/stream-info) to avoid duplicating
the live-status detection logic.
"""

from typing import Any


def is_producer_live(producers: list[dict[str, Any]]) -> bool:
    """Return True when at least one producer in a go2rtc stream is active.

    go2rtc marks a producer's ``state`` as ``None``, ``"offline"``, or
    ``"error"`` when it is not actively producing.  RTMP publishers that
    are connected also expose a ``"url"`` key.  A stream is considered live
    when at least one producer is neither offline nor errored.
    """
    return any(
        p.get("state") not in (None, "offline", "error") or "url" in p
        for p in producers
    )
