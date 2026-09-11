"""Session state machine and manager for the betting module.

Design rules enforced here:
- No prediction logic. Decisions arrive ready-made through
  ``DecisionIntent``; this module only validates, gates and executes.
- Simulation is never presented as reality: sim ledger entries carry
  ``simulated: true`` and the session state is SIMULATION.
- When the browser cannot be reached the session reports
  NOT_CONNECTED / WAITING_FOR_BROWSER — it never fabricates a placement.
- Part 1 never places real bets: validated REAL decisions end as DEFERRED
  with reason ``real_placement_disabled`` (or are REJECTED by pre-flight),
  never as PLACED.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from app.betting.browser_adapters import (
    PlacementBackend,
    PlacementUnavailable,
    RealBrowserBackend,
    SimulationBackend,
)
from app.betting.browser_client import AviatorBrowserClient, BrowserUnreachable
from app.betting.config import BettingSettings
from app.betting.profiles import PROFILES, BettingProfile, get_profile
from app.betting.schemas import DecisionIntent, ModeName, SessionStartRequest
from app.betting.states import (
    DecisionRejectReason,
    Mode,
    OutcomeStatus,
    SessionState,
)

log = logging.getLogger("betting.session")

Broadcaster = Callable[[dict], Awaitable[None]]


class BettingError(Exception):
    """Domain error surfaced to the API layer."""


class SessionNotRunning(BettingError):
    def __init__(self, message: str = "no active betting session"):
        super().__init__(message)
        self.code = "session_not_running"


class SessionAlreadyActive(BettingError):
    def __init__(self, session_id: str, state: str):
        super().__init__(f"session {session_id} already active ({state})")
        self.code = "session_already_active"
        self.session_id = session_id


class DuplicateDecision(BettingError):
    def __init__(self, decision_id: str):
        super().__init__(f"decision {decision_id!r} already processed")
        self.code = "duplicate_decision"
        self.decision_id = decision_id


def _now() -> float:
    return time.time()


def _new_entry_id() -> str:
    return uuid.uuid4().hex[:12]


def _mk_entry(intent: DecisionIntent, **kw: Any) -> dict:
    entry: dict = intent.as_dict()
    entry.update({
        "entry_id": _new_entry_id(),
        "status": OutcomeStatus.RECEIVED.value,
        "reason": None,
        "mode": None,
        "simulated": False,
        "placed_at": None,
        "resolved_at": None,
        "crash_point": None,
        "pnl_bif": None,
        "round_label": None,
        "note": None,
    })
    entry.update(kw)
    return entry


class BettingSession:
    """One betting session (REAL or SIMULATION) with its own run loop."""

    def __init__(
        self,
        *,
        settings: BettingSettings,
        request: SessionStartRequest,
        session_id: str,
        broadcaster: Optional[Broadcaster] = None,
    ):
        self.settings = settings
        self.request = request
        self.session_id = session_id
        self.mode = Mode(request.mode)
        self.profile = get_profile(request.profile)
        self.label = request.label or ""
        self.broadcaster = broadcaster

        self.state = SessionState.IDLE
        self.stop_reason: Optional[str] = None
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None
        self.last_error: Optional[str] = None

        # Live browser / environment.
        self.backend: Optional[PlacementBackend] = None
        self._browser_client: Optional[AviatorBrowserClient] = None

        # Observed state (REAL mode).
        self.last_balance: Optional[float] = None
        self.last_balance_text: str = ""
        self.last_ui_ready: bool = False
        self.last_snapshot: dict = {}
        self.consecutive_read_errors = 0
        self._latest_crash: Optional[float] = None

        # Bookkeeping.
        self.ledger: list[dict] = []
        self._seen_decisions: set[str] = set()
        self.cumulative_pnl: float = 0.0
        self.resolved_count = 0
        self.placed_count = 0
        self.deferred_count = 0
        self.rejected_count = 0
        self._lock = asyncio.Lock()
        self._stop_requested = False
        self._task: Optional[asyncio.Task] = None

    # ── helpers ──────────────────────────────────────────────────────────────
    async def _emit(self, payload: dict) -> None:
        if self.broadcaster is None:
            return
        try:
            await self.broadcaster(payload)
        except Exception:
            log.debug("broadcaster failed", exc_info=True)

    def _emit_state(self, **extra: Any) -> None:
        asyncio.ensure_future(self._emit(
            {"type": "betting:state", "session_id": self.session_id,
             "state": self.state.value, "mode": self.mode.value, **extra}))

    def _emit_ledger(self, entry: dict, action: str) -> None:
        asyncio.ensure_future(self._emit(
            {"type": "betting:ledger", "session_id": self.session_id,
             "action": action, "entry": entry}))

    def _set_state(self, state: SessionState, **extra: Any) -> None:
        if self.state != state:
            log.info("session %s → %s", self.session_id, state.value)
        self.state = state
        self._emit_state(**extra)

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep that wakes early when a stop was requested."""
        deadline = time.monotonic() + seconds
        while not self._stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.1))

    def _append_ledger(self, entry: dict) -> None:
        self.ledger.append(entry)
        if len(self.ledger) > self.settings.ledger_cap:
            self.ledger = self.ledger[-self.settings.ledger_cap:]
        self._emit_ledger(entry, "append")

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Called by the manager before spawning the run task.

        The backend is built *synchronously* here (cheap object construction;
        no sockets are opened until the run loop calls readiness/snapshot) so
        that decisions submitted immediately after ``start`` can never observe
        a half-initialised session.
        """
        self.started_at = _now()
        if self.mode == Mode.SIMULATION:
            sim = SimulationBackend(round_every_s=self.settings.sim_round_every_s)
            self.backend = sim
            self.last_balance = sim.current_balance()
            self.last_balance_text = f"{sim.current_balance():.0f}BIF"
        else:
            client = AviatorBrowserClient(self.settings)
            self._browser_client = client
            self.backend = RealBrowserBackend(
                client, allow_real_placement=self.settings.allow_real_placement,
            )
        self._set_state(SessionState.STARTING, started_at=self.started_at)

    def request_stop(self, reason: str = "manual") -> None:
        """Ask the run loop to stop at the next safe point."""
        self._stop_requested = True
        if self.stop_reason is None:
            self.stop_reason = reason
        if self.state.is_active:
            self._set_state(SessionState.STOPPING, stop_reason=self.stop_reason)

    async def _finish(self, reason: str) -> None:
        self.stop_reason = reason
        self.stopped_at = _now()
        self._set_state(SessionState.STOPPING, stop_reason=reason)
        if self._browser_client is not None:
            try:
                await self._browser_client.close()
            except Exception:
                pass
        self._set_state(SessionState.STOPPED, stop_reason=reason,
                        stopped_at=self.stopped_at, summary=self.summary())

    async def run(self) -> None:
        """Main loop. ``start()`` must be called first."""
        try:
            if self.mode == Mode.SIMULATION:
                await self._run_simulation()
            else:
                await self._run_real()
        except asyncio.CancelledError:
            self.stopped_at = _now()
            self.stop_reason = self.stop_reason or "cancelled"
            self._set_state(SessionState.STOPPED, stop_reason=self.stop_reason)
            raise
        except Exception as exc:  # defensive: never kill the manager silently
            self.last_error = str(exc)
            log.exception("betting session %s crashed", self.session_id)
            self._set_state(SessionState.ERROR, error=str(exc))

    # ── REAL mode (read-only in Part 1) ──────────────────────────────────────
    async def _run_real(self) -> None:
        self._emit_state()
        while not self._stop_requested:
            await self._real_observe_cycle()
            if self._stop_requested:
                break
            await self._interruptible_sleep(self.settings.poll_interval_s)
        await self._finish(self.stop_reason or "manual")

    async def _real_observe_cycle(self) -> None:
        """One observe iteration: connect → snapshot → classify state.

        Pure read-only observation of the live Aviator UI.
        """
        try:
            status = await self.backend.readiness()  # type: ignore[union-attr]
        except BrowserUnreachable as exc:
            self._mark_observe_failure(str(exc))
            await asyncio.sleep(self.settings.browser_recheck_s)
            return
        connected = bool(status.get("connected"))
        stage = status.get("stage")
        if not connected or stage in ("unreachable",):
            if self.state == SessionState.CONNECTED:
                self._set_state(SessionState.WAITING_FOR_BROWSER)
            elif self.state not in (SessionState.STARTING, SessionState.WAITING_FOR_BROWSER):
                self._set_state(SessionState.NOT_CONNECTED,
                                error=status.get("error"))
            await asyncio.sleep(self.settings.browser_recheck_s)
            return
        if stage in ("targets_missing", "read_error"):
            # Browser is there but the Aviator game plane is not observable.
            if self.state == SessionState.CONNECTED:
                self._set_state(SessionState.WAITING_FOR_BROWSER,
                                error=status.get("error"))
            elif self.state != SessionState.WAITING_FOR_BROWSER:
                self._set_state(SessionState.WAITING_FOR_BROWSER,
                                error=status.get("error"))
            await asyncio.sleep(self.settings.browser_recheck_s)
            return
        # Ready: game frame reachable.
        if self.state in (SessionState.STARTING, SessionState.NOT_CONNECTED,
                          SessionState.WAITING_FOR_BROWSER):
            self._set_state(SessionState.CONNECTED)
        self.consecutive_read_errors = 0
        snap = await self.backend.snapshot()  # type: ignore[union-attr]
        if not snap.get("ok"):
            self._mark_observe_failure(snap.get("error") or "snapshot failed")
            return
        self.last_snapshot = snap
        self.last_balance = snap.get("balance")
        self.last_balance_text = snap.get("balance_text") or ""
        self.last_ui_ready = bool(snap.get("ui_ready"))
        if snap.get("payouts_head"):
            self._latest_crash = snap["payouts_head"][0]

    def _mark_observe_failure(self, error: str) -> None:
        self.last_error = error
        self.consecutive_read_errors += 1
        if self.consecutive_read_errors >= self.settings.max_consecutive_read_errors:
            if self.state in (SessionState.CONNECTED, SessionState.STARTING):
                self._set_state(SessionState.WAITING_FOR_BROWSER, error=error)
            self.consecutive_read_errors = 0

    # ── SIMULATION mode ──────────────────────────────────────────────────────
    async def _run_simulation(self) -> None:
        sim = self.backend  # created in start()
        assert isinstance(sim, SimulationBackend)
        self._set_state(SessionState.SIMULATION)
        while not self._stop_requested:
            resolved = await sim.collect_resolved()
            for item in resolved:
                await self._apply_sim_resolution(item)
            await self._refresh_sim_snapshot(sim)
            if self._auto_stop_check():
                break
            await self._interruptible_sleep(min(self.settings.poll_interval_s, 0.5))
        await self._finish(self.stop_reason or "manual")

    async def _apply_sim_resolution(self, item: dict) -> None:
        decision_id = item["decision_id"]
        target = next((e for e in self.ledger
                       if e["decision_id"] == decision_id
                       and e["status"] == OutcomeStatus.PLACED.value), None)
        if target is None:
            log.warning("sim resolution for unknown decision %s", decision_id)
            return
        target["status"] = OutcomeStatus.RESOLVED.value
        target["crash_point"] = item["crash_point"]
        target["pnl_bif"] = item["pnl_bif"]
        target["round_label"] = item["round_label"]
        target["resolved_at"] = item["resolved_at"]
        target["note"] = "won" if item["won"] else "lost"
        self.resolved_count += 1
        self.cumulative_pnl = round(self.cumulative_pnl + item["pnl_bif"], 2)
        self._emit_ledger(target, "resolved")

    async def _refresh_sim_snapshot(self, sim: SimulationBackend) -> None:
        self.last_balance = sim.current_balance()
        self.last_balance_text = f"{sim.current_balance():.0f}BIF"
        snap = await sim.snapshot()
        self.last_snapshot = snap
        self.last_ui_ready = True
        if snap.get("payouts_head"):
            self._latest_crash = snap["payouts_head"][0]

    def _auto_stop_check(self) -> bool:
        req = self.request
        if req.max_rounds is not None and self.resolved_count >= req.max_rounds:
            self.request_stop("max_rounds")
            return True
        if (req.target_profit_bif is not None
                and self.cumulative_pnl >= req.target_profit_bif):
            self.request_stop("target_profit")
            return True
        if (req.max_loss_bif is not None
                and self.cumulative_pnl <= -req.max_loss_bif):
            self.request_stop("max_loss")
            return True
        return False

    # ── decisions ────────────────────────────────────────────────────────────
    async def submit_decision(self, intent: DecisionIntent) -> dict:
        """Validate, gate, execute and record one external decision.

        Returns the ledger entry (also broadcast as ``betting:ledger``).
        """
        if not self.state.is_active:
            raise SessionNotRunning(f"session {self.session_id} not active ({self.state.value})")
        async with self._lock:
            if intent.decision_id in self._seen_decisions:
                raise DuplicateDecision(intent.decision_id)
            entry = _mk_entry(intent, mode=self.mode.value,
                              simulated=self.mode == Mode.SIMULATION)
            try:
                if self.mode == Mode.SIMULATION:
                    await self._exec_sim_decision(intent, entry)
                else:
                    await self._exec_real_decision(intent, entry)
            finally:
                self._seen_decisions.add(intent.decision_id)
                self._append_ledger(entry)
            return entry

    async def _exec_sim_decision(self, intent: DecisionIntent, entry: dict) -> None:
        backend: SimulationBackend = self.backend  # type: ignore[assignment]
        amount = intent.effective_amount()
        balance = backend.current_balance()
        if amount > balance:
            entry.update(status=OutcomeStatus.REJECTED.value,
                         reason=DecisionRejectReason.INSUFFICIENT_BALANCE.value,
                         note=f"sim balance {balance:.0f}BIF < stake {amount}BIF")
            self.rejected_count += 1
            return
        try:
            outcome = await backend.place(
                amount_bif=amount,
                target_multiplier=intent.target_multiplier,
                bet_slot=intent.bet_slot,
                decision_id=intent.decision_id,
            )
        except PlacementUnavailable as exc:
            entry.update(status=OutcomeStatus.REJECTED.value,
                         reason=exc.reason, note=exc.message)
            self.rejected_count += 1
            return
        self.placed_count += 1
        entry.update(status=OutcomeStatus.PLACED.value, placed_at=_now(),
                     round_label=outcome.round_label, note=outcome.detail)

    async def _exec_real_decision(self, intent: DecisionIntent, entry: dict) -> None:
        amount = intent.effective_amount()
        profile = self.profile
        if amount > profile.max_loss_bif:
            entry.update(status=OutcomeStatus.REJECTED.value,
                         reason=DecisionRejectReason.INTERNAL_ERROR.value,
                         note="stake exceeds profile max_loss_bif")
            self.rejected_count += 1
            return
        if self.last_balance is None:
            entry.update(status=OutcomeStatus.REJECTED.value,
                         reason=DecisionRejectReason.UI_NOT_READY.value,
                         note="game balance unknown — cannot verify funds")
            self.rejected_count += 1
            return
        if self.last_balance < amount:
            entry.update(status=OutcomeStatus.REJECTED.value,
                         reason=DecisionRejectReason.INSUFFICIENT_BALANCE.value,
                         note=f"game balance {self.last_balance:.0f}BIF < stake {amount}BIF")
            self.rejected_count += 1
            return
        if not self.last_ui_ready:
            entry.update(status=OutcomeStatus.REJECTED.value,
                         reason=DecisionRejectReason.UI_NOT_READY.value,
                         note="betting panel not visible in this game phase")
            self.rejected_count += 1
            return
        backend: RealBrowserBackend = self.backend  # type: ignore[assignment]
        if not backend.allow_real_placement:
            entry.update(status=OutcomeStatus.DEFERRED.value,
                         reason=DecisionRejectReason.PLACEMENT_DISABLED.value,
                         note="BETTING_ALLOW_REAL_PLACEMENT is off — placement deferred to Part 2")
            self.deferred_count += 1
            return
        # Master switch on: still no real driver in Part 1 — defer honestly.
        try:
            await backend.ensure_ready()
        except PlacementUnavailable as exc:
            entry.update(status=OutcomeStatus.DEFERRED.value,
                         reason=DecisionRejectReason.PLACEMENT_DRIVER_UNAVAILABLE.value,
                         note=exc.message)
            self.deferred_count += 1
            return
        entry.update(status=OutcomeStatus.DEFERRED.value,
                     reason=DecisionRejectReason.PLACEMENT_DRIVER_UNAVAILABLE.value,
                     note="real placement driver not yet implemented (Part 2)")
        self.deferred_count += 1

    # ── status ───────────────────────────────────────────────────────────────
    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "mode": self.mode.value,
            "simulated": self.mode == Mode.SIMULATION,
            "stop_reason": self.stop_reason,
            "cumulative_pnl_bif": self.cumulative_pnl,
            "resolved_decisions": self.resolved_count,
            "placed_count": self.placed_count,
            "deferred_count": self.deferred_count,
            "rejected_count": self.rejected_count,
            "ledger_size": len(self.ledger),
        }

    def status(self) -> dict:
        backend_desc: dict = {}
        if self.backend is not None:
            backend_desc = self.backend.describe()
        elapsed = None
        if self.started_at is not None:
            elapsed = round(_now() - self.started_at, 1)
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "mode": self.mode.value,
            "simulated": self.mode == Mode.SIMULATION,
            "profile": self.profile.key,
            "profile_label": self.profile.label,
            "label": self.label,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "elapsed_s": elapsed,
            "stop_reason": self.stop_reason,
            "last_error": self.last_error,
            "last_balance_bif": self.last_balance,
            "last_balance_text": self.last_balance_text,
            "last_ui_ready": self.last_ui_ready,
            "latest_crash": self._latest_crash,
            "running_pnl_bif": self.cumulative_pnl,
            "resolved_decisions": self.resolved_count,
            "placed_count": self.placed_count,
            "deferred_count": self.deferred_count,
            "rejected_count": self.rejected_count,
            "goals": {
                "target_profit_bif": self.request.target_profit_bif,
                "max_loss_bif": self.request.max_loss_bif,
                "max_rounds": self.request.max_rounds,
            },
            "backend": backend_desc,
            "ledger_size": len(self.ledger),
            "consecutive_read_errors": self.consecutive_read_errors,
        }

    def ledger_slice(self, limit: int = 50) -> list[dict]:
        limit = min(max(limit, 1), self.settings.ledger_cap)
        return list(reversed(self.ledger[-limit:]))


class BettingManager:
    """Owns the current session; entry point for the API layer."""

    def __init__(
        self,
        settings: BettingSettings,
        *,
        broadcaster: Optional[Broadcaster] = None,
    ):
        self.settings = settings
        self.broadcaster = broadcaster
        self.session: Optional[BettingSession] = None
        self.session_seq = 0
        self._lock = asyncio.Lock()
        self.created_at = _now()

    # ── events ───────────────────────────────────────────────────────────────
    async def _emit(self, payload: dict) -> None:
        if self.broadcaster is None:
            return
        try:
            await self.broadcaster(payload)
        except Exception:
            log.debug("broadcaster failed", exc_info=True)

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start_session(self, request: SessionStartRequest) -> dict:
        async with self._lock:
            if self.session is not None and self.session.state.is_active:
                raise SessionAlreadyActive(self.session.session_id,
                                           self.session.state.value)
            # profile validity check (schemas already restrict to literals)
            get_profile(request.profile)
            self.session_seq += 1
            session = BettingSession(
                settings=self.settings,
                request=request,
                session_id=f"S-{self.session_seq}",
                broadcaster=self.broadcaster,
            )
            self.session = session
        session.start()
        loop = asyncio.get_event_loop()
        session._task = loop.create_task(session.run())
        await self._emit({"type": "betting:session_started",
                          "session_id": session.session_id,
                          "mode": session.mode.value})
        return session.status()

    async def stop_session(self, *, emergency: bool = False) -> dict:
        async with self._lock:
            session = self.session
            if session is None or not session.state.is_active:
                raise SessionNotRunning("no active session to stop")
            session.request_stop("emergency" if emergency else "manual")
        await self._emit({"type": "betting:session_stopping",
                          "session_id": session.session_id,
                          "reason": "emergency" if emergency else "manual"})
        return session.status()

    async def submit_decision(self, intent: DecisionIntent) -> dict:
        session = self.session
        if session is None:
            raise SessionNotRunning("no betting session started")
        return await session.submit_decision(intent)

    # ── queries ──────────────────────────────────────────────────────────────
    def status(self) -> dict:
        if self.session is None:
            return {"session_id": None, "state": SessionState.IDLE.value,
                    "mode": None, "simulated": False, "session": None,
                    "backend": {"managed": False},
                    "ledger_size": 0}
        out = self.session.status()
        out["session"] = {"id": out["session_id"]}
        return out

    def ledger(self, limit: int = 50) -> list[dict]:
        if self.session is None:
            return []
        return self.session.ledger_slice(limit)

    async def shutdown(self) -> None:
        """Cancel any running session (called on app lifespan exit)."""
        async with self._lock:
            session = self.session
            if session is not None and session._task is not None:
                session.request_stop("shutdown")
                session._task.cancel()
                try:
                    await session._task
                except (asyncio.CancelledError, Exception):
                    pass


# Process-wide convenience holder (the API uses app.state.betting).
_default_manager: Optional[BettingManager] = None


def get_betting_manager() -> Optional[BettingManager]:
    return _default_manager


def configure_default_manager(manager: BettingManager) -> None:
    global _default_manager
    _default_manager = manager


