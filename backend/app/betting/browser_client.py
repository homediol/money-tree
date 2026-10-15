"""Raw Chrome DevTools Protocol client for the betting module.

The default path observes the collector-owned game page and reports
capabilities. Explicit UI-practice methods may edit the fixed cashout setting
or stake input after requiring the authenticated Betting page and verified
two-page topology. No method clicks the bet button; real placement remains
disabled.

Frame discovery notes (winner.rw / Spribe Aviator):
  - /json lists a page target for winner.rw and a separate *iframe* target for
    aviaport.spribegaming.com (an OOPIF). The aviaport frame is cross-process
    from the winner.rw page, so page-level CDP evaluation cannot reach inside
    it; we instead connect to the aviaport *target's own* websocket.
  - ``aviator-next`` can itself be an out-of-process child. We resolve its
    frame ID through ``Page.getFrameTree`` and create an isolated execution
    world for that frame; no cross-origin ``contentDocument`` access is used.

Evaluation flow:
  snapshot() → resolve aviator-next frame ID → create isolated world → evaluate
  SNAPSHOT in that world. Every step is wrapped so one missing frame produces
  a structured error, never a crash.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlsplit

from app.betting.config import BettingSettings
from app.betting.platform_state import classify_betting_window, detect_insufficient_balance
from app.betting.selectors import (
    GAME_DOCUMENT_PROBE,
    GAME_FRAME_HINT,
    NEXT_GAME_HINT,
    PAGE_HINT,
    READ_BALANCE,
    SNAPSHOT,
    GameSnapshot,
    parse_balance_display,
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
    parent_id: str = ""

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
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, list):
        raise ValueError("CDP target list must be an array of target objects")
    for index, target in enumerate(payload):
        if not isinstance(target, dict):
            raise ValueError(f"CDP target at index {index} must be an object")
        websocket_url = target.get("webSocketDebuggerUrl")
        if websocket_url is not None and not isinstance(websocket_url, str):
            raise ValueError(f"CDP target at index {index} has an invalid websocket URL")
    return payload


class CdpSession:
    """One websocket connection to a Chrome DevTools target."""

    def __init__(self, ws_url: str, settings: BettingSettings):
        self.ws_url = ws_url
        self.settings = settings
        self._ws: Any = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    @property
    def connected(self) -> bool:
        """Best available websocket liveness check across websockets versions."""
        if self._ws is None:
            return False
        state = getattr(self._ws, "state", None)
        if state is None:
            return True
        return getattr(state, "name", str(state)).upper() == "OPEN"

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

    async def evaluate(self, expression: str, return_by_value: bool = True,
                       context_id: int | None = None) -> Any:
        """Evaluate a self-contained function expression in the target page.

        Read paths pass pure DOM probes. The only mutating expressions are the
        explicitly guarded cashout and stake UI-practice functions; neither
        expression locates or clicks a bet button.
        """
        source = expression.strip()
        # Every browser expression in this module is a function expression.
        # Runtime.evaluate only creates that function unless it is explicitly
        # invoked, which previously made every read return ``undefined``.
        expr = f"({source})()"
        params = {"expression": expr, "returnByValue": return_by_value,
                  "awaitPromise": True}
        if context_id is not None:
            params["contextId"] = context_id
        result = await self.call("Runtime.evaluate", params)
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
    """Attaches to the collector-owned browser; it never launches a browser/context."""

    def __init__(self, settings: BettingSettings):
        self.settings = settings
        self._page_ws: str | None = None
        self._frame_ws: str | None = None
        self._session: CdpSession | None = None
        self._frame_target_id: str | None = None
        self._target_cache: list[Target] = []
        self._discover_ts: float = 0.0
        self._selected_page_role = "UNKNOWN"

    @staticmethod
    def _browser_page_roles() -> dict[str, Any]:
        """Read only the page-role metadata written by the collector owner."""
        status_path = Path(__file__).resolve().parents[3] / "data" / "bot" / "status.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        roles = status.get("browser_pages")
        return roles if isinstance(roles, dict) else {}

    @staticmethod
    def _provider_authentication(frame: Target | None, balance: Any, probe: Any) -> tuple[str, str | None]:
        """Classify provider auth without returning or logging session tokens."""
        balance_state = (balance or {}).get("authenticationState") if isinstance(balance, dict) else None
        if balance_state == "EXPIRED" or (isinstance(probe, dict) and probe.get("loginPromptVisible") is True):
            return "AUTH_REQUIRED", None
        parsed = urlsplit(frame.url) if frame else None
        params = parse_qs(parsed.query, keep_blank_values=True) if parsed else {}
        token = next((value for key, values in params.items() if key.lower() == "token"
                      for value in values if value), None)
        user = next((value for key, values in params.items() if key.lower() == "user"
                     for value in values if value), None)
        if token and user:
            identity = hashlib.sha256(
                f"{parsed.hostname or ''}\\0{user}\\0{token}".encode("utf-8")
            ).hexdigest()[:24]
            return "AUTHENTICATED", identity
        if balance_state == "AUTHENTICATED":
            return "AUTHENTICATED", None
        return "UNKNOWN", None

    # ── discovery ────────────────────────────────────────────────────────────
    def _discover(self) -> list[Target]:
        try:
            raw = _fetch_json_list(self.settings.cdp_http)
        except ValueError as exc:
            raise BrowserUnreachable(f"invalid CDP target response: {exc}") from exc
        except Exception as exc:  # ConnectionRefused / timeout / HTTP error
            raise BrowserUnreachable(f"CDP endpoint {self.settings.cdp_http} unreachable: {exc}") from exc
        targets = [Target(id=str(t.get("id") or ""), kind=str(t.get("type") or ""),
                          url=str(t.get("url") or ""), ws=t.get("webSocketDebuggerUrl", ""),
                          parent_id=str(t.get("parentId") or ""))
                   for t in raw if t.get("webSocketDebuggerUrl")]
        self._target_cache = targets
        self._discover_ts = time.monotonic()
        return targets

    def resolve_targets(self) -> tuple[Optional[Target], Optional[Target]]:
        """Return only the collector-owned shared Aviator page and its OOPIF."""
        targets = self._discover()
        roles = self._browser_page_roles()
        aviator_target_id = str(roles.get("aviator_target_id") or "")
        page_state = str(roles.get("aviator_page_state") or "UNKNOWN")
        page = (next((target for target in targets
                      if target.id == aviator_target_id and target.kind == "page"), None)
                if aviator_target_id else None)
        if page is None or page_state != "OPEN" or not page.is_aviator_page:
            self._selected_page_role = "SHARED_AVIATOR_PAGE_MISSING"
            return None, None
        self._selected_page_role = "SHARED_AVIATOR"
        frame = next((t for t in targets if t.is_aviaport_frame and
                      page is not None and t.parent_id == page.id), None)
        return page, frame

    # ── connection ───────────────────────────────────────────────────────────
    async def _connect_session(self) -> CdpSession:
        _, frame = self.resolve_targets()
        # The game frame target is required for observation and guarded UI
        # practice; the winner.rw page confirms the target is Aviator.
        if frame is None or not frame.ws:
            raise BrowserUnreachable(
                "no aviaport.spribegaming.com frame target — browser not on "
                "Aviator, or game not loaded (OOPIF hidden)"
            )
        if (self._session is not None and self._frame_target_id == frame.id
                and self._session.connected):
            return self._session
        if self._session is not None:
            await self._session.close()
            self._session = None
        session = CdpSession(frame.ws, self.settings)
        await session.connect()
        self._session = session
        self._frame_target_id = frame.id
        return session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._frame_target_id = None

    @staticmethod
    def _find_frame(node: dict, url_hint: str) -> dict | None:
        """Return the first frame-tree entry whose URL contains ``url_hint``."""
        if not isinstance(node, dict):
            return None
        frame = node.get("frame") or {}
        if isinstance(frame, dict) and url_hint in str(frame.get("url", "")):
            return frame
        children = node.get("childFrames") or []
        if not isinstance(children, list):
            return None
        for child in children:
            found = AviatorBrowserClient._find_frame(child, url_hint)
            if found:
                return found
        return None

    async def _game_context(self, session: CdpSession) -> int:
        """Create an execution context directly inside the aviator-next frame."""
        await session.call("Page.enable")
        frame = None
        for attempt in range(4):
            tree = await session.call("Page.getFrameTree")
            if not isinstance(tree, dict):
                raise BrowserUiError("CDP frame tree response must be an object")
            frame = self._find_frame(tree.get("frameTree") or {}, NEXT_GAME_HINT)
            if frame and frame.get("id"):
                break
            if attempt < 3:
                await asyncio.sleep(0.15)
        if not frame or not frame.get("id"):
            raise BrowserUiError("aviator-next frame not found in CDP frame tree")
        world = await session.call(
            "Page.createIsolatedWorld",
            {"frameId": frame["id"], "worldName": "winner-predict",
             "grantUniveralAccess": False},
        )
        context_id = world.get("executionContextId")
        if context_id is None:
            raise BrowserUiError("could not create aviator-next execution context")
        return int(context_id)

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
                    "page_found": page is not None, "game_found": frame is not None,
                    "page_role": self._selected_page_role,
                    "browser_pages": self._browser_page_roles(),
                    "browser_topology_verified": False}
        try:
            session = await self._connect_session()
            bal = await session.evaluate(READ_BALANCE)
            bal_text = (bal or {}).get("text", "") if isinstance(bal, dict) else ""
            context_id = await self._game_context(session)
            probe = await session.evaluate(GAME_DOCUMENT_PROBE, context_id=context_id)
            if not isinstance(probe, dict):
                raise BrowserUiError("aviator-next probe result must be an object")
            next_reachable = probe.get("ok") is True
        except (BrowserError, BrowserUiError) as exc:
            return {"connected": True, "stage": "read_error",
                    "error": str(exc), "page_found": True, "game_found": True}
        return {"connected": True,
                "stage": "ready" if next_reachable else "session_ended" if isinstance(probe, dict) and probe.get("sessionEnded") else "game_dom_unreachable",
                "page_found": True, "game_found": True,
                "balance_text": bal_text,
                "authentication_state": self._provider_authentication(frame, bal, probe)[0],
                "page_role": self._selected_page_role,
                "browser_pages": self._browser_page_roles(),
                "browser_topology_verified": self._topology_verified(),
                "aviator_next_reachable": next_reachable,
                "error": None if next_reachable else
                         (probe or {}).get("err", "aviator-next document is not reachable"),
                "game_session_state": "SESSION_ENDED" if isinstance(probe, dict) and probe.get("sessionEnded") else "UNKNOWN" if not next_reachable else "ACTIVE"}

    def _topology_verified(self) -> bool:
        roles = self._browser_page_roles()
        aviator_id = str(roles.get("aviator_target_id") or "")
        page_ids = {target.id for target in self._target_cache if target.kind == "page"}
        try:
            page_count = int(roles.get("page_count") or 0)
        except (TypeError, ValueError):
            page_count = 0
        try:
            aviator_page_count = int(roles.get("aviator_page_count") or 0)
        except (TypeError, ValueError):
            aviator_page_count = 0
        frame_count = sum(1 for target in self._target_cache
                          if target.is_aviaport_frame and target.parent_id == aviator_id)
        target_aviator_page_count = sum(1 for target in self._target_cache
                                        if target.kind == "page" and target.is_aviator_page)
        return bool(self._selected_page_role == "SHARED_AVIATOR"
                    and roles.get("browser_count") == 1
                    and roles.get("context_count") == 1
                    and page_count == 1 and aviator_page_count == 1
                    and target_aviator_page_count == 1 and frame_count == 1
                    and aviator_id and aviator_id in page_ids
                    and roles.get("aviator_page_state") == "OPEN"
                    and roles.get("game_frame_state") == "READY")

    async def snapshot(self) -> GameSnapshot:
        """Read a full capability snapshot from the game DOM."""
        try:
            session = await self._connect_session()
        except BrowserUnreachable as exc:
            snap = GameSnapshot(ok=False, error=str(exc))
            return snap
        try:
            context_id = await self._game_context(session)
            probe = await session.evaluate(GAME_DOCUMENT_PROBE, context_id=context_id)
        except (BrowserError, BrowserUiError) as exc:
            return GameSnapshot(ok=False, error=f"aviator-next probe failed: {exc}")
        if not isinstance(probe, dict):
            return GameSnapshot(ok=False, error="aviator-next probe result must be an object")
        if not probe or probe.get("ok") is not True:
            return GameSnapshot(ok=False,
                                error=probe.get("err", "aviator-next unreachable (OOPIF boundary?)"))
        value = await session.evaluate(SNAPSHOT, context_id=context_id)
        snap = GameSnapshot.from_value(value)
        # Balance belongs to the aviaport shell, outside aviator-next.
        try:
            balance = await session.evaluate(READ_BALANCE)
            page, provider_frame = self.resolve_targets()
            hostname = urlsplit(page.url).hostname if page else None
            snap.provider_identity = hostname
            snap.authentication_state, snap.session_identity = self._provider_authentication(
                provider_frame, balance, probe,
            )
            snap.page_role = self._selected_page_role
            snap.browser_pages = self._browser_page_roles()
            snap.browser_topology_verified = self._topology_verified()
            if isinstance(balance, dict) and balance.get("ok"):
                snap.balance_text = balance.get("text") or ""
                parsed = parse_balance_display(snap.balance_text, balance.get("currency"))
                snap.balance = parsed["amount"]
                snap.balance_currency = parsed["currency"]
                snap.balance_observed_at = datetime.now(timezone.utc).isoformat()
            else:
                snap.balance = None
                snap.balance_currency = None
        except (BrowserError, BrowserUiError):
            snap.balance = None
            snap.balance_currency = None
            log.debug("balance read failed after game snapshot", exc_info=True)
        return snap

    async def _require_authenticated_betting_page(self) -> tuple[CdpSession, int]:
        """Fail closed unless the shared collector Aviator page is verified."""
        readiness = await self.readiness()
        if (readiness.get("connected") is not True
                or readiness.get("stage") != "ready"
                or readiness.get("authentication_state") != "AUTHENTICATED"
                or readiness.get("page_role") != "SHARED_AVIATOR"
                or readiness.get("browser_topology_verified") is not True):
            raise BrowserUiError(
                "UI_PRACTICE_REQUIRES_AUTHENTICATED_SHARED_AVIATOR_PAGE"
            )
        session = await self._connect_session()
        self.resolve_targets()
        if self._selected_page_role != "SHARED_AVIATOR" or not self._topology_verified():
            raise BrowserUiError("SHARED_AVIATOR_PAGE_CHANGED_BEFORE_UI_PRACTICE")
        context_id = await self._game_context(session)
        return session, context_id

    @staticmethod
    def _valid_panel(panel: int) -> bool:
        return type(panel) is int and panel in (0, 1)

    async def cashout_configuration(self, panel: int = 0) -> dict:
        """Read the selected panel's auto-cashout state; do not infer activation."""
        return await self.read_auto_cashout(panel)

    async def read_auto_cashout(self, panel: int = 0) -> dict:
        """Read configured cashout value, toggle, mode and acknowledgement."""
        from app.betting.cashout import inspect_cashout_configuration

        if not self._valid_panel(panel):
            return inspect_cashout_configuration(None)
        snapshot = await self.snapshot()
        controls = snapshot.cashout_controls if snapshot.ok else []
        control = next((item for item in controls if item.get("slot") == panel), None)
        return {
            **inspect_cashout_configuration(control),
            "observed_control": control,
            "page_role": snapshot.page_role,
            "authentication_state": snapshot.authentication_state,
        }

    async def set_auto_cashout(self, target: float = 2.00, panel: int = 0) -> dict:
        """Compatibility guard: cashout settings are configured manually."""
        del target, panel
        raise BrowserUiError("MANUAL_OPERATOR_CONFIGURATION_REQUIRED: verify cashout settings read-only")

    async def read_stake_input(self, panel: int = 0) -> dict:
        """Read the panel stake input and report minimum-stake evidence if present."""
        if not self._valid_panel(panel):
            return {"found": False, "reason": "BETTING_PANEL_SLOT_INVALID"}
        snapshot = await self.snapshot()
        field = next((item for item in snapshot.stake_inputs
                      if item.get("slot") == panel), None) if snapshot.ok else None
        minimum_attribute = field.get("minAttribute") if field else None
        return {
            "found": bool(field),
            "input": field,
            "page_role": snapshot.page_role,
            "authentication_state": snapshot.authentication_state,
            "minimum_stake_evidence": {
                "value": minimum_attribute,
                "source": "HTML_MIN_ATTRIBUTE" if minimum_attribute is not None else None,
                "platform_enforcement_verified": False,
            },
        }

    async def set_stake_input(self, amount: int | float | str = 100, panel: int = 0) -> dict:
        """Compatibility guard: stake entry is operator-owned."""
        del amount, panel
        raise BrowserUiError("MANUAL_OPERATOR_CONFIGURATION_REQUIRED: enter stake in Aviator UI manually")

    @staticmethod
    def _require_clear_two_panel_state(snapshot: GameSnapshot) -> None:
        if not snapshot.ok or len(snapshot.bet_panel_states) != 2:
            raise BrowserUiError("TWO_PANEL_BETTING_UI_UNVERIFIED")
        pending = [state.get("pending_bet_state", "UNKNOWN")
                   for state in snapshot.bet_panel_states]
        if any(state != "CLEAR" for state in pending):
            raise BrowserUiError("ACTIVE_OR_UNKNOWN_BET_STATE")

    @staticmethod
    def _require_auto_bet_off_two_panel_state(snapshot: GameSnapshot) -> None:
        """Cashout setup may not continue unless both Auto Bet switches are OFF."""
        controls = {item.get("slot"): item for item in snapshot.cashout_controls}
        if set(controls) != {0, 1}:
            raise BrowserUiError("TWO_PANEL_CASHOUT_CONTROLS_UNVERIFIED")
        if any(controls[slot].get("autoBetEnabled") is not False for slot in (0, 1)):
            raise BrowserUiError("AUTO_BET_NOT_VERIFIED_OFF_BOTH_PANELS")

    @staticmethod
    def _require_cashout_control(snapshot: GameSnapshot, panel: int) -> dict:
        control = next((item for item in snapshot.cashout_controls
                        if item.get("slot") == panel), None)
        if not control or control.get("autoTabVisible") is not True:
            raise BrowserUiError("AUTO_CASHOUT_CONTROLS_NOT_FOUND")
        if control.get("autoBetEnabled") is not False:
            raise BrowserUiError("AUTO_BET_NOT_VERIFIED_OFF")
        return control

    @staticmethod
    def _require_visible_stake_field(snapshot: GameSnapshot, panel: int) -> dict:
        field = next((item for item in snapshot.stake_inputs
                      if item.get("slot") == panel), None)
        if not field or field.get("visible") is not True:
            raise BrowserUiError("VISIBLE_STAKE_INPUT_NOT_FOUND")
        return field

    async def detect_bet_button(self, panel: int = 0) -> dict:
        """Return presence and observed state; does not interact with the button."""
        if not self._valid_panel(panel):
            return {"detected": False, "reason": "BETTING_PANEL_SLOT_INVALID"}
        snapshot = await self.snapshot()
        button = next((item for item in snapshot.place_bet_buttons
                       if item.get("associatedPanel", item.get("slot")) == panel), None) \
            if snapshot.ok else None
        return {
            "detected": bool(button and button.get("visible")),
            "button": button,
            "betting_window": snapshot.betting_window,
            "interaction_performed": False,
        }

    async def detect_betting_window(self, panel: int = 0) -> dict:
        if not self._valid_panel(panel):
            return {"state": "UNKNOWN", "reason": "BETTING_PANEL_SLOT_INVALID"}
        snapshot = await self.snapshot()
        if not snapshot.ok:
            return {"state": "UNKNOWN", "reason": snapshot.error or "SNAPSHOT_UNAVAILABLE"}
        evidence = next((item for item in snapshot.bet_panel_states
                         if item.get("slot") == panel), None)
        if evidence is None:
            return {"state": "UNKNOWN", "reason": "BETTING_PANEL_EVIDENCE_NOT_FOUND"}
        return {"state": classify_betting_window(evidence, panel=panel),
                "evidence": evidence}

    async def detect_insufficient_balance(self) -> dict:
        """Parse only explicit visible accessibility alert text; no balance inference."""
        snapshot = await self.snapshot()
        if not snapshot.ok:
            return {"detected": False, "observed": False,
                    "matched_messages": [], "source": None,
                    "error": snapshot.error}
        return detect_insufficient_balance(snapshot.status_messages)

    async def read_verified_balance(self) -> dict:
        """Return the observed account balance and whether identity evidence is complete."""
        snapshot = await self.snapshot()
        has_amount = (isinstance(snapshot.balance, (int, float))
                      and not isinstance(snapshot.balance, bool))
        verified = bool(
            snapshot.ok and snapshot.authentication_state == "AUTHENTICATED"
            and has_amount and snapshot.balance_currency
            and snapshot.provider_identity and snapshot.session_identity
            and snapshot.balance_observed_at
        )
        return {
            "verified": verified,
            "amount": snapshot.balance,
            "currency": snapshot.balance_currency,
            "observed_at": snapshot.balance_observed_at,
            "provider_identity": snapshot.provider_identity,
            "session_identity": snapshot.session_identity,
            "authentication_state": snapshot.authentication_state,
            "page_role": snapshot.page_role,
            "reason": None if verified else "ACCOUNT_OR_SESSION_IDENTITY_UNVERIFIED",
        }

    async def place_bet(self, *, amount_bif: int, cashout: float, slot: int) -> dict:
        """Keep the low-level financial action disabled pending separate auth."""
        del amount_bif, cashout, slot
        raise BrowserUiError(
            "REAL_MONEY_EXECUTION_DISABLED_UNTIL_SEPARATE_AUTHORIZATION_AND_VERIFICATION"
        )
