"""Narrow API-key exemptions for the intentionally public history workspace."""

from __future__ import annotations

from uuid import UUID


_PUBLIC_HISTORY_GET_PATHS = frozenset({
    "/api/history",
    "/api/history/status",
    "/api/history/latest",
    "/api/history/recent",
    "/api/history/stats",
    "/api/history/admin-status",
})


def is_public_history_request(method: str, path: str) -> bool:
    """Return whether a history-only endpoint is deliberately keyless.

    The history dashboard and its export/reset workflow are public by operator
    request. Keep this allowlist exact so it cannot weaken other API routes.
    """
    method = method.upper()
    if method == "GET" and path in _PUBLIC_HISTORY_GET_PATHS:
        return True
    if method == "POST" and path in {"/api/history/exports", "/api/history/clear"}:
        return True

    segments = path.strip("/").split("/")
    if len(segments) not in {4, 5} or segments[:3] != ["api", "history", "exports"]:
        return False
    try:
        UUID(segments[3])
    except (ValueError, AttributeError):
        return False

    if len(segments) == 4:
        return method == "GET"
    return (segments[4] == "verify" and method == "POST") or (
        segments[4] == "download" and method == "GET"
    )
