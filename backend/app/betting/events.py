"""Event payload builders.

All betting events travel over the existing /ws/live socket as *flat*
dicts with ``type`` prefixed ``betting:`` (e.g. ``betting:state``,
``betting:ledger``). The betting control page subscribes to its own socket
and filters ``betting:*`` messages so it never collides with the collector's
``updated_analysis`` / ``system_status`` traffic.
"""
from __future__ import annotations

from typing import Any


def betting_event(kind: str, **payload: Any) -> dict:
    """Build a flat betting event dict.

    ``kind`` is used *without* the prefix here — callers pass e.g.
    ``"state"`` and receive ``{"type": "betting:state", ...}``.
    """
    event: dict = {"type": f"betting:{kind}"}
    event.update(payload)
    return event


def state_event(state: str, mode: str, **extra: Any) -> dict:
    return betting_event("state", state=state, mode=mode, **extra)


def ledger_event(entry: dict, action: str = "append") -> dict:
    return betting_event("ledger", action=action, entry=entry)


def notice_event(kind: str, message: str, level: str = "info") -> dict:
    return betting_event("notice", notice_kind=kind, message=message, level=level)

