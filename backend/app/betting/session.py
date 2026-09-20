"""Session state machine and manager for the betting module.

Design rules enforced here:
- No prediction logic. Decisions arrive ready-made through
  ``DecisionIntent``; this module only validates, gates and executes.
- Simulation is never presented as reality: sim ledger entries carry
  ``simulated: true`` and the session state is SIMULATION.
- When the browser cannot be reached the session reports
  NOT_CONNECTED / WAITING_FOR_BROWSER — it never fabricates a placement.
- Real placement is opt-in and only consumes final risk-approved instructions.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
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


class SafetyGateBlocked(BettingError):
    code = "safety_gate_blocked"

    def __init__(self, reasons: list[str] | None = None):
        self.reasons = reasons or ["system health is not safe for betting"]
        super().__init__("; ".join(self.reasons))


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
        repository=None,
        reconciler=None,
        safety_gate=None,
    ):
        self.settings = settings
        self.request = request
        self.session_id = session_id
        self.mode = Mode(request.mode)
        self.profile = get_profile(request.profile)
        self.label = request.label or ""
        self.broadcaster = broadcaster
        self.repository = repository
        self.reconciler = reconciler
        self.safety_gate = safety_gate

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
        self.starting_balance: Optional[float] = request.starting_balance
        self.goal_balance: Optional[float] = request.goal_balance
        self.current_bet: Optional[dict] = None
        self.last_result: Optional[dict] = None
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.unknowns = 0
        self.total_stake = 0.0
        self.total_payout = 0.0
        self.peak_balance = self.starting_balance
        self.drawdown = 0.0

        # Bookkeeping.
        self.ledger: list[dict] = []
        self._seen_decisions: set[tuple[str, str]] = set()
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

    def _event(self, kind: str, **extra: Any) -> None:
        asyncio.ensure_future(self._emit({
            "type": f"betting:{kind}", "session_id": self.session_id, **extra,
        }))

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
            sim = SimulationBackend(
                round_every_s=self.settings.sim_round_every_s,
                initial_balance=self.starting_balance or 2000.0,
            )
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
        self._event("started", status=self.status())
        self._event("waiting")
        self._append_financial("SESSION_START", amount=0,
                               observed=self.last_balance,
                               status="VERIFIED" if self.last_balance is not None else "UNKNOWN")

    def request_stop(self, reason: str = "manual") -> None:
        """Ask the run loop to stop at the next safe point."""
        self._stop_requested = True
        if self.stop_reason is None:
            self.stop_reason = reason
        if self.state.is_active:
            self._set_state(SessionState.STOPPING, stop_reason=self.stop_reason)
        if reason == "emergency":
            self._event("emergency_stop")

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
        self._event("stopped", reason=reason, status=self.status())
        self._append_financial("SESSION_END", amount=0, observed=self.last_balance,
                               status="VERIFIED" if self.last_balance is not None else "UNKNOWN")
        self._save_metrics()

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

    # ── REAL mode ────────────────────────────────────────────────────────────
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

        Observe the live UI and reconcile any active execution.
        """
        try:
            status = await self.backend.readiness()  # type: ignore[union-attr]
        except BrowserUnreachable as exc:
            if self.state == SessionState.CONNECTED:
                self._event("error", error=str(exc))
                asyncio.ensure_future(self._emit({"type": "browser:disconnected",
                                                   "session_id": self.session_id,
                                                   "error": str(exc)}))
                self.request_stop("browser_disconnected")
                return
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
            asyncio.ensure_future(self._emit({"type": "browser:connected",
                                               "session_id": self.session_id}))
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
        if self.last_balance is not None:
            self._event("balance_updated", balance=self.last_balance)
        self._goal_stop_check()
        resolved = await self.backend.collect_resolved()  # type: ignore[union-attr]
        for item in resolved:
            await self._apply_resolution(item)

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
        await self._apply_resolution(item)

    async def _apply_resolution(self, item: dict) -> None:
        decision_id = item["decision_id"]
        target = next((e for e in self.ledger
                       if e["decision_id"] == decision_id
                       and e["status"] == OutcomeStatus.PLACED.value), None)
        if target is None:
            log.warning("sim resolution for unknown decision %s", decision_id)
            return
        reconciliation = None
        if self.reconciler and not target.get("simulated"):
            # A payout-list change is not proof of the execution's exact round.
            # Preserve the observation and wait for History Collector matching.
            # A matching balance delta is the platform evidence that the bet
            # was accepted (and, for a win, that cashout was credited).
            before = item.get("balance_before")
            after = item.get("balance_after")
            expected_pnl = (float(item["amount_bif"]) *
                            (float(item["target_multiplier"]) - 1)
                            if item.get("won") else -float(item["amount_bif"]))
            observed_pnl = (float(after) - float(before)
                            if before is not None and after is not None else None)
            tolerance = max(1.0, float(item["amount_bif"]) * .01)
            balance_matches = (observed_pnl is not None
                               and abs(observed_pnl - expected_pnl) <= tolerance)
            platform_evidence = {
                "placement_confirmed": balance_matches,
                "cashout_requested": True,
                "cashout_executed": bool(item.get("won") and balance_matches),
                "cashout_confirmed": bool(item.get("won") and balance_matches),
                "platform_observed_balance": after,
                "observed_profit_loss": observed_pnl,
                "observation_complete": True,
            }
            self._persist_entry(target, "UNKNOWN", lifecycle_state="UNKNOWN",
                                observed_multiplier=item.get("crash_point"),
                                balance_after=item.get("balance_after"),
                                platform_evidence=platform_evidence,
                                error="awaiting_exact_history_round_match")
            execution = self.repository.execution_for(target["decision_id"], target["round_id"])
            exact = execution.get("exact_round_result") if execution else None
            if exact:
                reconciliation = await self.reconciler.reconcile(
                    execution, round_id=str(exact["round_id"]),
                    multiplier=float(exact["multiplier"]), evidence=platform_evidence)
                outcome = reconciliation["outcome"]
                if outcome == "UNKNOWN":
                    self.unknowns += 1
                    self.request_stop("reconciliation_unknown")
                    target.update(status="unknown", note="unknown", pnl_bif=None)
                    self.current_bet = None
                    self._save_metrics()
                    return
                item = {**item, "crash_point": float(exact["multiplier"]),
                        "won": outcome == "WIN",
                        "pnl_bif": float(execution.get("profit_loss") or 0)}
            else:
                self.current_bet = None
                self.request_stop("reconciliation_pending")
                self._save_metrics()
                return
        if reconciliation is None and self.reconciler and self.repository:
            execution = self.repository.execution_for(target["decision_id"], target["round_id"])
            evidence = {"placement_confirmed": bool(target.get("simulated")),
                        "cashout_requested": True,
                        "cashout_executed": bool(item.get("won")),
                        "cashout_confirmed": bool(item.get("won")),
                        "balance_after": item.get("balance_after", self.last_balance),
                        "platform_observed_balance": item.get("balance_after", self.last_balance)}
            reconciliation = await self.reconciler.reconcile(
                execution, round_id=str(target["round_id"]),
                multiplier=float(item["crash_point"]), evidence=evidence)
            outcome = reconciliation["outcome"]
            if outcome == "UNKNOWN":
                self.unknowns += 1
                self.request_stop("reconciliation_unknown")
                target.update(status="unknown", note="unknown", pnl_bif=None)
                self.current_bet = None
                self._save_metrics()
                return
        target["status"] = OutcomeStatus.RESOLVED.value
        target["crash_point"] = item["crash_point"]
        target["pnl_bif"] = item["pnl_bif"]
        target["round_label"] = item["round_label"]
        target["resolved_at"] = item["resolved_at"]
        target["note"] = "won" if item["won"] else "lost"
        self.last_result = dict(target)
        self.current_bet = None
        if item["won"]:
            self.wins += 1
            self.consecutive_losses = 0
            self.consecutive_wins += 1
            self.total_payout += item["amount_bif"] * item["target_multiplier"]
        else:
            self.losses += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        self.resolved_count += 1
        self.cumulative_pnl = round(self.cumulative_pnl + item["pnl_bif"], 2)
        self.total_stake += item["amount_bif"]
        observed = item.get("balance_after", self.last_balance)
        if observed is not None:
            self.last_balance = observed
            self.peak_balance = max(self.peak_balance or observed, observed)
            self.drawdown = max(0.0, (self.peak_balance or observed) - observed)
        self._emit_ledger(target, "resolved")
        self._event("bet_result", result=target)
        # Reconciliation commits the terminal execution status atomically with
        # its immutable ledger. Do not overwrite RECONCILED with an internal
        # lifecycle label after that transaction.
        if reconciliation is None:
            self._persist_entry(target, "CASHED_OUT" if item["won"] else "LOST",
                                result_multiplier=item["crash_point"],
                                balance_after=item.get("balance_after", self.last_balance),
                                profit_loss=item["pnl_bif"], resolved_at=item["resolved_at"])
        self._event("result", result=target)
        self._event("balance", balance=self.last_balance)
        self._save_metrics()

    async def _refresh_sim_snapshot(self, sim: SimulationBackend) -> None:
        self.last_balance = sim.current_balance()
        self.last_balance_text = f"{sim.current_balance():.0f}BIF"
        snap = await sim.snapshot()
        self.last_snapshot = snap
        self.last_ui_ready = True
        if snap.get("payouts_head"):
            self._latest_crash = snap["payouts_head"][0]
        self._event("balance_updated", balance=self.last_balance)

    def _goal_stop_check(self) -> bool:
        if (self.goal_balance is not None and self.last_balance is not None
                and self.last_balance >= self.goal_balance):
            self.request_stop("goal_reached")
            self._event("goal_reached", balance=self.last_balance,
                        goal_balance=self.goal_balance)
            return True
        return False

    def _auto_stop_check(self) -> bool:
        req = self.request
        if self._goal_stop_check():
            return True
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
        if self.safety_gate and self.mode != Mode.SIMULATION:
            gate = self.safety_gate(mode=self.mode.value)
            if not gate.get("allowed"):
                self.request_stop("health_safety_pause")
                raise SafetyGateBlocked(gate.get("reasons"))
        async with self._lock:
            decision_key = (intent.decision_id, intent.round_id or "")
            if decision_key in self._seen_decisions:
                raise DuplicateDecision(intent.decision_id)
            entry = _mk_entry(intent, mode=self.mode.value,
                              simulated=self.mode == Mode.SIMULATION)
            now = datetime.now(timezone.utc).isoformat()
            execution = {
                "execution_id": entry["entry_id"], "decision_id": intent.decision_id,
                "target_round_id": intent.round_id, "profile": intent.profile,
                "bet_amount": intent.effective_amount(), "cashout_target": intent.cashout,
                "status": "VALIDATING", "simulated": self.mode == Mode.SIMULATION,
                "created_at": now, "updated_at": now, "placed_at": None,
                "resolved_at": None, "result_multiplier": None,
                "balance_before": self.last_balance, "balance_after": None,
                "profit_loss": None, "error": None,
                "session_id": self.session_id, "lifecycle_state": "QUEUED",
                "requested_bet_amount": intent.effective_amount(),
                "requested_cashout": intent.cashout,
                "placement_confirmed": False,
            }
            if self.repository and not self.repository.create_execution(execution):
                raise DuplicateDecision(intent.decision_id)
            if self.repository:
                self.repository.append_execution_event(
                    execution["execution_id"], "QUEUED", now,
                    {"decision_id": intent.decision_id, "round_id": intent.round_id},
                )
            entry["execution_id"] = execution["execution_id"]
            self._event("decision_received", decision=entry)
            try:
                rejection = self._validate_contract(intent)
                if rejection:
                    reason, note = rejection
                    entry.update(status=OutcomeStatus.REJECTED.value,
                                 reason=reason, note=note)
                    self.rejected_count += 1
                elif self.mode == Mode.SIMULATION:
                    await self._exec_sim_decision(intent, entry)
                else:
                    await self._exec_real_decision(intent, entry)
            finally:
                self._seen_decisions.add(decision_key)
                self._append_ledger(entry)
                mapped = {
                    "placed": "BET_PLACED", "resolved": "CASHED_OUT",
                    "rejected": "REJECTED", "deferred": "FAILED",
                }.get(entry["status"], "FAILED")
                self._persist_entry(entry, mapped, error=entry.get("note") if mapped in {"FAILED", "REJECTED"} else None)
                if mapped == "BET_PLACED":
                    self._persist_entry(entry, mapped, lifecycle_state="PLACED",
                                        placement_confirmed=bool(entry.get("simulated")))
                    self._append_financial("BET_DEBIT", execution_id=entry["execution_id"],
                                           amount=-float(entry["amount_bif"]),
                                           observed=None, status="PENDING")
            return entry

    def _append_financial(self, event_type: str, *, execution_id=None, amount=None,
                          observed=None, status="UNKNOWN") -> None:
        if not self.repository:
            return
        stamp = datetime.now(timezone.utc).isoformat()
        self.repository.append_ledger({
            "ledger_id": uuid.uuid4().hex, "execution_id": execution_id,
            "session_id": self.session_id, "event_type": event_type, "amount": amount,
            "internal_expected_balance": None,
            "platform_observed_balance": observed,
            "reconciled_balance": observed if status == "VERIFIED" else None,
            "balance_status": status, "recorded_at": stamp,
            "simulated": self.mode == Mode.SIMULATION,
        })

    def _save_metrics(self) -> None:
        if not self.repository:
            return
        payload = self.status()
        payload.update({"unknown": self.unknowns, "consecutive_wins": self.consecutive_wins,
                        "total_stake": self.total_stake, "total_payout": self.total_payout,
                        "peak_balance": self.peak_balance, "drawdown": self.drawdown,
                        "win_rate": self.wins / (self.wins + self.losses)
                        if self.wins + self.losses else None})
        self.repository.save_session_metrics(self.session_id,
                                             datetime.now(timezone.utc).isoformat(), payload)

    def _persist_entry(self, entry: dict, status: str, **updates) -> None:
        if not self.repository or not entry.get("execution_id"):
            return
        record = self.repository.execution_for(entry["decision_id"], entry["round_id"])
        if not record:
            return
        record.update(updates)
        record["status"] = status
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        if status == "BET_PLACED" and not record.get("placed_at"):
            record["placed_at"] = record["updated_at"]
        self.repository.update_execution(record)
        self.repository.append_execution_event(
            record["execution_id"], record.get("lifecycle_state", status),
            record["updated_at"], {"status": status, **updates},
        )

    def _validate_contract(self, intent: DecisionIntent) -> Optional[tuple[str, str]]:
        if not intent.execute:
            return (DecisionRejectReason.EXECUTION_NOT_AUTHORIZED.value,
                    "decision execute flag is false")
        if not intent.round_id or not intent.round_id.strip():
            return (DecisionRejectReason.INVALID_ROUND.value,
                    "round_id is required")
        if intent.is_expired():
            return (DecisionRejectReason.EXPIRED_DECISION.value,
                    "decision has expired")
        if intent.profile != self.profile.key:
            return (DecisionRejectReason.PROFILE_MISMATCH.value,
                    "decision profile does not match active session")
        if round(float(intent.cashout), 2) != round(self.profile.base_target, 2):
            return (DecisionRejectReason.CASHOUT_MISMATCH.value,
                    f"cashout must equal {self.profile.base_target:.2f}x for {self.profile.key}")
        if self.current_bet is not None:
            return (DecisionRejectReason.BET_ALREADY_ACTIVE.value,
                    "another bet is still active")
        return None

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
        self.current_bet = entry
        self._event("bet_placed", bet=entry)

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
                         note="BETTING_ALLOW_REAL_PLACEMENT is off — real placement is disabled")
            self.deferred_count += 1
            return
        try:
            await backend.ensure_ready()
        except PlacementUnavailable as exc:
            entry.update(status=OutcomeStatus.DEFERRED.value,
                         reason=DecisionRejectReason.PLACEMENT_DRIVER_UNAVAILABLE.value,
                         note=exc.message)
            self.deferred_count += 1
            return
        try:
            outcome = await backend.place(
                amount_bif=amount, target_multiplier=float(intent.cashout),
                bet_slot=intent.bet_slot, decision_id=intent.decision_id,
            )
        except PlacementUnavailable as exc:
            entry.update(status=OutcomeStatus.DEFERRED.value,
                         reason=exc.reason, note=exc.message)
            self.deferred_count += 1
            return
        self.placed_count += 1
        entry.update(status=OutcomeStatus.PLACED.value, placed_at=_now(),
                     note=outcome.detail)
        self.current_bet = entry
        self._event("bet_placed", bet=entry)

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
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "unknown": self.unknowns,
            "total_stake": self.total_stake,
            "total_payout": self.total_payout,
            "peak_balance": self.peak_balance,
            "drawdown": self.drawdown,
            "win_rate": self.wins / (self.wins + self.losses) if self.wins + self.losses else None,
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
            "cashout": self.profile.base_target,
            "enabled": self.state.is_active,
            "automatic_enabled": self.state.is_active,
            "label": self.label,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "elapsed_s": elapsed,
            "stop_reason": self.stop_reason,
            "last_error": self.last_error,
            "last_balance_bif": self.last_balance,
            "current_balance": self.last_balance,
            "starting_balance": self.starting_balance,
            "goal_balance": self.goal_balance,
            "last_balance_text": self.last_balance_text,
            "last_ui_ready": self.last_ui_ready,
            "latest_crash": self._latest_crash,
            "running_pnl_bif": self.cumulative_pnl,
            "profit": ((self.last_balance - self.starting_balance)
                       if self.last_balance is not None and self.starting_balance is not None
                       else self.cumulative_pnl),
            "current_round": self.last_snapshot.get("round_label"),
            "current_bet": self.current_bet,
            "last_result": self.last_result,
            "total_bets": self.placed_count,
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
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
        repository=None,
        reconciler=None,
        safety_gate=None,
    ):
        self.settings = settings
        self.broadcaster = broadcaster
        self.repository = repository
        self.reconciler = reconciler
        self.safety_gate = safety_gate
        self.session: Optional[BettingSession] = None
        self.last_request: Optional[SessionStartRequest] = None
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
                repository=self.repository,
                reconciler=self.reconciler,
                safety_gate=self.safety_gate,
            )
            self.session = session
            self.last_request = request
        session.start()
        loop = asyncio.get_event_loop()
        session._task = loop.create_task(session.run())
        await self._emit({"type": "betting:session_started",
                          "session_id": session.session_id,
                          "mode": session.mode.value})
        return session.status()

    async def resume_session(self) -> dict:
        """Resume only the last explicit session configuration."""
        if self.session is not None and self.session.state.is_active:
            return self.session.status()
        if self.last_request is None:
            raise SessionNotRunning("no paused session configuration to resume")
        return await self.start_session(self.last_request)

    async def stop_session(self, *, emergency: bool = False,
                           reason: Optional[str] = None) -> dict:
        async with self._lock:
            session = self.session
            if session is None or not session.state.is_active:
                raise SessionNotRunning("no active session to stop")
            stop_reason = reason or ("emergency" if emergency else "manual")
            session.request_stop(stop_reason)
        await self._emit({"type": "betting:session_stopping",
                          "session_id": session.session_id,
                          "reason": stop_reason})
        return session.status()

    async def submit_decision(self, intent: DecisionIntent) -> dict:
        session = self.session
        if session is None:
            raise SessionNotRunning("no betting session started")
        return await session.submit_decision(intent)

    # ── queries ──────────────────────────────────────────────────────────────
    def status(self) -> dict:
        if self.session is None:
            return {
                "session_id": None, "state": SessionState.IDLE.value,
                "lifecycle_status": "idle", "enabled": False,
                "automatic_enabled": False, "mode": None,
                "simulated": False, "session": None,
                "backend": {"managed": False}, "browser_status": "NOT_CONNECTED",
                "starting_balance": None, "current_balance": None,
                "goal_balance": None, "profit": 0.0, "profile": None,
                "cashout": None, "current_round": None, "current_bet": None,
                "last_result": None, "total_bets": 0, "wins": 0,
                "losses": 0, "consecutive_losses": 0, "ledger_size": 0,
            }
        out = self.session.status()
        out["session"] = {"id": out["session_id"]}
        out["lifecycle_status"] = out["state"].lower()
        out["browser_status"] = (
            "CONNECTED" if out["state"] == SessionState.CONNECTED.value
            else "SIMULATION" if out["simulated"]
            else "WAITING_FOR_BROWSER" if out["state"] in {
                SessionState.STARTING.value, SessionState.WAITING_FOR_BROWSER.value,
            }
            else "NOT_CONNECTED"
        )
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
