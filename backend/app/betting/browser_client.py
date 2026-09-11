"""Raw Chrome DevTools Protocol client for the betting module.

Part 1 is strictly read-only: this client *observes* the game page, maps the
top-level and (where reachable) in-page frames, runs capability snapshots and
returns parsed balance/payout data. No CDP command in this module clicks,
types, navigates or mutates the page.

Frame discovery notes (winner.rw / Spribe Aviator):
  - /json lists a page target for winner.rw and a separate *iframe* target for
    aviaport.spribegaming.com (an OOPIF). The aviaport frame is cross-process
    from the winner.rw page, so page-level CDP evaluation cannot reach inside
    it; we instead connect to the aviaport *target's own* websocket.
  - From inside the aviaport document the game plane ``aviator-next`` is a
    same-site child iframe, reachable via ``contentDocument``. When that child
    is out of process the DOM is opaque and we report ``ui_ready: false``
    instead of guessing.

Evaluation flow:
  snapshot() → evaluate AVIATOR_NEXT_DOC in the aviaport target to obtain the
  child document, then evaluate SNAPSHOT inside that document. Every step is
  wrapped so one missing frame produces a structured error, never a crash.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin

from app.betting.config import BettingSettings
from app.betting.selectors import (
    AVIATOR_NEXT_DOC,
    GAME_FRAME_HINT,
    PAGE_HINT,
    READ_BALANCE,
    SNAPSHOT,
    GameSnapshot,
)

log = logging.getLogger("betting.browser")


class BrowserError(Exception):
    """Base class for browser-layer failures."""


class BrowserUnreachable(BrowserError):
    """CDP endpoint unreachable or listing no targets."""


class BrowserUiError(BrowserError):
    """Page reachable but the Aviator UI is not inspectable right now."""


@dataclass
class Target:
    id: str
    kind: str  # 'page' | 'iframe' | 'webview' ...
    url: str
    ws: str

    @property
    def is_aviator_page(self) -> bool:
        return "aviator" in self.url and PAGE_HINT in self.url

    @property
    def is_aviaport_frame(self) -> bool:
        return GAME_FRAME_HINT in self.url


def _fetch_json_list(cdp_http: str) -> list[dict]:
    """GET /json/list on the CDP endpoint (urllib; no external deps)."""
    url = urljoin(cdp_http.rstrip("/") + "/", "json/list")
    with urllib.request.urlopen(url, timeout=3.0) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


class CdpSession:
    """One websocket connection to a Chrome DevTools target."""

    def __init__(self, ws_url: str, settings: BettingSettings):
        self.ws_url = ws_url
        self.settings = settings
        self._ws: Any = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def connect(self) -> None:
        # websockets >= 12 legacy client; imported lazily so unit tests that
        # never open a socket do not need the dependency on the import path.
        import websockets

        self._ws = await websockets.connect(self.ws_url, open_timeout=5, close_timeout=2)
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._ws.recv()
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Socket died: fail every pending request.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BrowserError("CDP connection closed"))
            self._pending.clear()

    async def call(self, method: str, params: Optional[dict] = None) -> dict:
        if self._ws is None:
            raise BrowserError("CDP socket not open")
        self._msg_id += 1
        mid = self._msg_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        try:
            result = await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise BrowserError(f"CDP call {method} timed out") from None
        if "error" in result:
            raise BrowserError(f"CDP {method} error: {result['error']}")
        return result.get("result", {})

    async def evaluate(self, expression: str, return_by_value: bool = True) -> Any:
        """Runtime.evaluate a *pure function expression* in the target page.

        ``expression`` must be a self-contained IIFE/arrow function string
        (see selectors.py) so it works from any execution context.
        """
        expr = expression if expression.lstrip().startswith(("(", "function")) else f"({expression})"
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": return_by_value},
        )
        exc = result.get("exceptionDetails")
        if exc:
            raise BrowserUiError(f"page-side exception: {exc.get('text', 'unknown')}")
        if "result" not in result:
            raise BrowserUiError("CDP evaluation returned no value")
        value = result["result"].get("value")
        if value is None and result["result"].get("type") == "undefined":
            return None
        return value

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if getattr(self, "_reader", None):
            self._reader.cancel()
            try:
                await self._reader
            except Exception:
                pass


class AviatorBrowserClient:
    """Discovers the live Aviator browser session and takes read-only snapshots."""

    def __init__(self, settings: BettingSettings):
        self.settings = settings
        self._page_ws: str | None = None
        self._frame_ws: str | None = None
        self._session: CdpSession | None = None
        self._target_cache: list[Target] = []
        self._discover_ts: float = 0.0

    # ── discovery ────────────────────────────────────────────────────────────
    def _discover(self) -> list[Target]:
        try:
            raw = _fetch_json_list(self.settings.cdp_http)
        except Exception as exc:  # ConnectionRefused / timeout / HTTP error
            raise BrowserUnreachable(f"CDP endpoint {self.settings.cdp_http} unreachable: {exc}") from exc
        targets = [Target(id=t.get("id", ""), kind=t.get("type", ""),
                          url=t.get("url", ""), ws=t.get("webSocketDebuggerUrl", ""))
                   for t in raw if t.get("webSocketDebuggerUrl")]
        self._target_cache = targets
        self._discover_ts = time.monotonic()
        return targets

    def resolve_targets(self) -> tuple[Optional[Target], Optional[Target]]:
        """Return (aviator_page, aviaport_frame) targets; None when missing."""
        targets = self._discover()
        page = next((t for t in targets if t.is_aviator_page), None)
        frame = next((t for t in targets if t.is_aviaport_frame), None)
        return page, frame

    # ── connection ───────────────────────────────────────────────────────────
    async def _connect_session(self) -> CdpSession:
        if self._session is not None and getattr(self._session, "_ws", None) is not None:
            return self._session
        _, frame = self.resolve_targets()
        # Read-only observation only needs the game frame; the winner.rw page
        # target is required as confirmation that the browser is on Aviator.
        if frame is None or not frame.ws:
            raise BrowserUnreachable(
                "no aviaport.spribegaming.com frame target — browser not on "
                "Aviator, or game not loaded (OOPIF hidden)"
            )
        session = CdpSession(frame.ws, self.settings)
        await session.connect()
        self._session = session
        return session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ── read-only observations ───────────────────────────────────────────────
    async def readiness(self) -> dict:
        """Observed readiness: page presence + balance + game-DOM reachability.

        Pure observation — never mutates the page. Raised state values are
        expressed as strings plus booleans so they serialise cleanly.
        """
        try:
            page, frame = self.resolve_targets()
        except BrowserUnreachable as exc:
            return {"connected": False, "stage": "unreachable",
                    "error": str(exc), "page_found": False, "game_found": False}
        if page is None or frame is None:
            return {"connected": True, "stage": "targets_missing",
                    "error": "browser up but Aviator page/frame targets missing",
                    "page_found": page is not None, "game_found": frame is not None}
        try:
            session = await self._connect_session()
            bal = await session.evaluate(READ_BALANCE)
            bal_text = (bal or {}).get("text", "") if isinstance(bal, dict) else ""
            # Probe child-frame reachability without navigating/clicks.
            probe = await session.evaluate(AVIATOR_NEXT_DOC)
            next_reachable = bool(probe and probe.get("ok") is True)
        except (BrowserError, BrowserUiError) as exc:
            return {"connected": True, "stage": "read_error",
                    "error": str(exc), "page_found": True, "game_found": True}
        return {"connected": True, "stage": "ready",
                "page_found": True, "game_found": True,
                "balance_text": bal_text,
                "aviator_next_reachable": next_reachable,
                "error": None}

    async def snapshot(self) -> GameSnapshot:
        """Read a full capability snapshot from the game DOM."""
        try:
            session = await self._connect_session()
        except BrowserUnreachable as exc:
            snap = GameSnapshot(ok=False, error=str(exc))
            return snap
        try:
            probe = await session.evaluate(AVIATOR_NEXT_DOC)
        except (BrowserError, BrowserUiError) as exc:
            return GameSnapshot(ok=False, error=f"aviator-next probe failed: {exc}")
        if not probe or probe.get("ok") is not True:
            return GameSnapshot(ok=False,
                                error=probe.get("err", "aviator-next unreachable (OOPIF boundary?)"))
        value = await session.evaluate(SNAPSHOT)
        snap = GameSnapshot.from_value(value)
        return snap


