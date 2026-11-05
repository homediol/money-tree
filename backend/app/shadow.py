"""Paper-trading session isolated from the real-money executor."""
from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import datetime, timezone

from app.betting.execution_defaults import DEFAULT_FIXED_STAKE, FIXED_CASHOUT
from app.betting.schemas import DecisionIntent


def _now():
    return datetime.now(timezone.utc).isoformat()


class ShadowManager:
    def __init__(self, *, repository, broadcaster=None):
        self.repository, self.broadcaster = repository, broadcaster
        self.emergency_stop = None
        self.mode = "STOPPED"
        self.session = None
        self._pending = {}
        self._processed_rounds = set()
        self._lock = asyncio.Lock()
        # Recover unresolved paper rounds as PAUSED/UNKNOWN. Recovery never
        # resumes a session or authorizes a real execution automatically.
        try:
            recent = repository.list_shadow_sessions(limit=1)
            recovered = recent[0] if recent else None
            if recovered and not recovered.get("ended_at"):
                self.session = recovered
                self.mode = "PAUSED"
                recovered["recovery_state"] = "RECONCILIATION_REQUIRED"
                recovered["recovery_reason"] = "process_restart_pending_result_review"
                for trade in repository.list_shadow_trades(session_id=recovered.get("session_id"), limit=1000):
                    if trade.get("status") == "PAPER_AUTHORIZED":
                        trade["status"] = "UNKNOWN"
                        trade["recovery_reason"] = "restart_pending_result_reconciliation"
                        repository.save_shadow_trade(trade)
                        self._pending.setdefault(str(trade["target_round_id"]), []).append(trade)
                repository.save_shadow_session(recovered)
        except Exception:
            # Startup remains safe and the mode manager will keep SHADOW off.
            self.session = None
            self.mode = "STOPPED"

    async def emit(self, event, **payload):
        if self.broadcaster:
            await self.broadcaster({"type": f"shadow:{event}", **payload})

    def status(self):
        s = self.session or {}
        panels = s.get("panel_summary", {})
        return {"mode": self.mode, "session_id": s.get("session_id"),
                "starting_balance": s.get("starting_balance"), "current_balance": s.get("current_balance"),
                "goal_balance": s.get("goal_balance"), "profit": (None if not s else round(s["current_balance"] - s["starting_balance"], 2)),
                "bets": s.get("bets", 0), "wins": s.get("wins", 0), "losses": s.get("losses", 0),
                "unknown": s.get("unknown", 0), "roi": (None if not s or not s["starting_balance"] else (s["current_balance"] - s["starting_balance"]) / s["starting_balance"]),
                "rejected_decisions": s.get("rejected_decisions", 0), "production_isolated": True,
                "configuration": s.get("config"), "panel_summary": panels,
                "latest_round_id": s.get("latest_round_id"),
                "pending_rounds": list(self._pending),
                "stop_reason": s.get("end_reason"),
                "verified_real_balance": s.get("current_verified_real_balance"),
                "real_balance_observed_at": s.get("real_balance_observed_at"),
                "emergency_stop": bool(getattr(self.emergency_stop, "latched", False))}

    def betting_status(self):
        s = self.session or {}
        return {"state": "SHADOW", "mode": "SHADOW", "automatic_enabled": self.mode == "SHADOW",
                "current_balance": s.get("current_balance"),
                "real_balance_at_start": s.get("starting_balance"),
                "current_verified_real_balance": s.get("current_verified_real_balance"),
                "starting_balance": s.get("starting_balance"), "goal_balance": s.get("goal_balance"),
                "current_bet": next(iter(self._pending.values()), None),
                "emergency_stop": bool(getattr(self.emergency_stop, "latched", False)),
                "browser_status": "SHADOW", "last_ui_ready": True,
                "latest_history_round_id": s.get("latest_round_id"),
                "session_configuration": s.get("config")}

    def record_real_balance(self, observation: dict):
        if not self.session or not observation.get("verified"):
            return False
        self.session["current_verified_real_balance"] = float(observation["balance"])
        self.session["real_balance_observed_at"] = observation.get("observed_at")
        self.repository.save_shadow_session(self.session)
        return True

    def drift(self):
        sample = int((self.session or {}).get("bets", 0))
        if sample < 30:
            return {"status": "INSUFFICIENT_SAMPLE", "warnings": [], "sample_size": sample,
                    "promotion": "INSUFFICIENT_SAMPLE"}
        return {"status": "OBSERVING", "warnings": ["DATA_DRIFT_WARNING"], "sample_size": sample,
                "promotion": "UNCERTAIN"}

    async def start(self, *, starting_balance=5000.0, goal_balance=None,
                    health=None, configuration=None):
        async with self._lock:
            if self.mode == "SHADOW":
                return self.status()
            if self._pending:
                raise ValueError("SHADOW START BLOCKED: pending panel results require exact-round reconciliation")
            gate = health.can_bet_now(mode="SIMULATION") if health else {"allowed": False, "reasons": ["health unavailable"]}
            if not gate.get("allowed"):
                raise ValueError("SHADOW START BLOCKED: " + ", ".join(gate.get("reasons", [])))
            config = self._validate_configuration(configuration or {}, starting_balance, goal_balance)
            self.session = {"session_id": "SH-" + uuid.uuid4().hex[:12],
                            "starting_balance": float(starting_balance), "current_balance": float(starting_balance),
                            "goal_balance": config["goal_balance"], "bets": 0, "wins": 0, "losses": 0, "unknown": 0,
                            "staked": 0.0, "payout": 0.0,
                            "started_at": _now(), "config": config,
                            "panel_summary": {"1": self._empty_panel_summary(), "2": self._empty_panel_summary()}}
            self.mode = "SHADOW"; self._pending.clear(); self._processed_rounds.clear()
            self.repository.save_shadow_session(self.session)
            self.repository.append_shadow_ledger(self.session["session_id"], "SHADOW_START", 0, self.session["current_balance"])
            await self.emit("started", session=self.status())
            return self.status()

    @staticmethod
    def _empty_panel_summary():
        return {"bets": 0, "wins": 0, "losses": 0, "stake": 0.0,
                "payout": 0.0, "pnl": 0.0}

    @staticmethod
    def _validate_configuration(configuration, starting_balance, goal_balance):
        """Keep the selected fixed stake and one selected execution panel."""
        if (isinstance(starting_balance, bool) or not isinstance(starting_balance, (int, float))
                or not math.isfinite(float(starting_balance)) or starting_balance < 0):
            raise ValueError("starting balance must be a finite non-negative number")
        if not isinstance(configuration, dict):
            raise ValueError("session configuration must be an object")
        win_enabled = configuration.get("win_limit_enabled", goal_balance is not None)
        if not isinstance(win_enabled, bool):
            raise ValueError("win limit choice must be true or false")
        win_limit = configuration.get("win_limit")
        if win_enabled:
            if win_limit is None and goal_balance is not None:
                win_limit = float(goal_balance) - float(starting_balance)
            if (isinstance(win_limit, bool) or not isinstance(win_limit, (int, float))
                    or not math.isfinite(float(win_limit)) or float(win_limit) <= 0):
                raise ValueError("enabled win limit must be a finite positive profit amount")
            goal_balance = float(starting_balance) + float(win_limit)
        else:
            win_limit = None
            goal_balance = None
        mode = str(configuration.get("mode", "AUTOMATIC")).upper()
        if mode not in {"MANUAL", "AUTOMATIC"}:
            raise ValueError("mode must be MANUAL or AUTOMATIC")
        panel = configuration.get("execution_panel", 0)
        if isinstance(panel, bool) or panel not in (0, 1):
            raise ValueError("execution panel must be 0 or 1")
        stake = configuration.get("fixed_stake_rwf", DEFAULT_FIXED_STAKE)
        if (isinstance(stake, bool) or not isinstance(stake, (int, float))
                or not math.isfinite(float(stake)) or int(stake) != stake or stake <= 0):
            raise ValueError("fixed simulated stake must be a positive whole number")
        return {"mode": mode, "starting_balance": float(starting_balance),
                "goal_balance": float(goal_balance) if goal_balance is not None else None,
                "win_limit_enabled": win_enabled, "win_limit": win_limit,
                "execution_panel": panel, "fixed_stake_rwf": int(stake),
                "cashout_multiplier": FIXED_CASHOUT,
                "stake_currency": configuration.get("stake_currency")}

    async def stop(self, reason="manual"):
        if self._pending:
            self.mode = "PAUSED"
            if self.session:
                self.session["end_reason"] = "pending_result_reconciliation_required"
                self.session["requested_stop_reason"] = reason
                self.session["recovery_state"] = "RECONCILIATION_REQUIRED"
                self.repository.save_shadow_session(self.session)
            await self.emit("paused", reason="pending_result_reconciliation_required", session=self.status())
            return self.status()
        if self.session:
            self.session["ended_at"], self.session["end_reason"] = _now(), reason
            self.session["report"] = "THIS WAS PAPER/SHADOW PERFORMANCE. NO REAL BETS WERE PLACED."
            self.repository.save_shadow_session(self.session)
            self.repository.append_shadow_ledger(self.session["session_id"], "SHADOW_STOP", 0, self.session["current_balance"])
        self.mode = "STOPPED"; await self.emit("stopped", reason=reason, session=self.status(), report="THIS WAS PAPER/SHADOW PERFORMANCE. NO REAL BETS WERE PLACED."); return self.status()

    async def pause(self):
        if self.mode == "SHADOW": self.mode = "PAUSED"; await self.emit("paused", session=self.status())
        return self.status()

    async def resume(self, health=None):
        if self.mode == "PAUSED":
            gate = health.can_bet_now(mode="SIMULATION") if health else {"allowed": False}
            if not gate.get("allowed"): raise ValueError("SHADOW RESUME BLOCKED")
            self.mode = "SHADOW"; await self.emit("resumed", session=self.status())
        return self.status()

    async def reconcile_round(self, observed: dict):
        """Settle only the shadow trade targeting this exact observed round."""
        if self.mode not in {"SHADOW", "PAUSED"} or not self.session:
            return
        round_id = str(observed.get("round_id"))
        async with self._lock:
            if round_id in self._processed_rounds: return
            self._processed_rounds.add(round_id)
            self.session["latest_round_id"] = round_id
            pending = self._pending.pop(round_id, None)
            if pending:
                await self._reconcile(pending, observed)

    async def submit_decision(self, decision: dict, *, emergency_stop: bool = False):
        async with self._lock:
            return await self._submit_decision(decision, emergency_stop=emergency_stop)

    async def _submit_decision(self, decision: dict, *, emergency_stop: bool = False):
        """Record one eligible, target-bound decision in the paper ledger."""
        if self.mode != "SHADOW" or not self.session:
            return {"accepted": False, "reason": "shadow_mode_not_active"}
        if emergency_stop or bool(getattr(self.emergency_stop, "latched", False)):
            return {"accepted": False, "reason": "emergency_stop"}
        if decision.get("status") != "READY_FOR_EXECUTION":
            return {"accepted": False, "reason": "decision_not_eligible"}
        decision_id = str(decision.get("decision_id") or "")
        target_round_id = str(decision.get("target_round_id") or decision.get("round_id") or "")
        if not decision_id or not target_round_id:
            return {"accepted": False, "reason": "decision_identity_missing"}
        try:
            expiry = datetime.fromisoformat(str(decision.get("expires_at")).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return {"accepted": False, "reason": "decision_expiry_missing_or_invalid"}
        if expiry <= datetime.now(timezone.utc):
            return {"accepted": False, "reason": "decision_expired"}
        config = self.session.get("config", {})
        amount = config.get("fixed_stake_rwf", DEFAULT_FIXED_STAKE)
        if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or int(amount) != amount or amount <= 0):
            return {"accepted": False, "reason": "fixed_stake_missing_or_invalid"}
        decision_amount = decision.get("requested_stake", decision.get("bet_amount"))
        if decision_amount is not None and (
                isinstance(decision_amount, bool) or not isinstance(decision_amount, (int, float))
                or int(decision_amount) != int(amount)):
            return {"accepted": False, "reason": "fixed_stake_configuration_mismatch"}
        cashout = decision.get("cashout_target", decision.get("cashout", FIXED_CASHOUT))
        if (isinstance(cashout, bool) or not isinstance(cashout, (int, float))
                or round(float(cashout), 2) != FIXED_CASHOUT):
            return {"accepted": False, "reason": "cashout_must_equal_2_00x"}
        all_trades = self.repository.list_shadow_trades(limit=5000)
        if any(str(t.get("decision_id")) == decision_id
               and str(t.get("target_round_id")) == target_round_id
               for t in all_trades):
            return {"accepted": False, "reason": "duplicate_shadow_decision"}
        if target_round_id in self._pending:
            return {"accepted": False, "reason": "shadow_round_already_pending"}
        try:
            intent = DecisionIntent(decision_id=decision_id, round_id=target_round_id,
                                    execute=True, cashout=FIXED_CASHOUT,
                                    bet_amount=int(amount), expires_at=expiry,
                                    source="rules_engine")
        except Exception as exc:
            return {"accepted": False, "reason": f"invalid_decision:{exc}"}
        trade = {"execution_id": "P-" + uuid.uuid4().hex,
                 "session_id": self.session["session_id"], "decision_id": decision_id,
                 "target_round_id": target_round_id,
                 "panel": int(config.get("execution_panel", 0)) + 1,
                 "profile": "FIXED", "bet_amount": intent.effective_amount(),
                 "cashout_target": FIXED_CASHOUT,
                 "balance_before": self.session["current_balance"],
                 "status": "PAPER_AUTHORIZED", "bet_accepted": True, "cashout_confirmed": False,
                 "created_at": _now(), "mode": "SHADOW_REALISTIC", "simulated": True}
        trades = [trade]
        self._pending[target_round_id] = trades
        self.repository.save_shadow_trade(trade)
        await self.emit("paper_trade", trade=trade)
        self.repository.save_shadow_session(self.session)
        return {"accepted": True, "trades": trades}

    async def _reconcile(self, trade, observed):
        trades = trade if isinstance(trade, list) else [trade]
        multiplier = float(observed.get("multiplier"))
        combined_pnl = 0.0
        for item in trades:
            target = float(item["cashout_target"])
            win = multiplier >= target
            stake = float(item["bet_amount"])
            pnl = stake * (target - 1) if win else -stake
            combined_pnl += pnl
            self.session["current_balance"] = round(self.session["current_balance"] + pnl, 2)
            self.session["bets"] += 1; self.session["staked"] += stake
            self.session["payout"] += stake * target if win else 0
            summary = self.session["panel_summary"][str(item["panel"])]
            summary["bets"] += 1; summary["stake"] = round(summary["stake"] + stake, 2)
            summary["payout"] = round(summary["payout"] + (stake * target if win else 0), 2)
            summary["pnl"] = round(summary["pnl"] + pnl, 2)
            if win:
                self.session["wins"] += 1; summary["wins"] += 1
            else:
                self.session["losses"] += 1; summary["losses"] += 1
            item.update(status="PAPER_RECONCILED", result="WIN" if win else "LOSS",
                        result_multiplier=multiplier, cashout_confirmed=bool(win),
                        pnl=round(pnl, 2), payout=round(stake * target if win else 0, 2),
                        balance_after=self.session["current_balance"], resolved_at=_now())
            self.repository.save_shadow_trade(item)
            self.repository.append_shadow_ledger(self.session["session_id"], "PAPER_PANEL_RESULT", pnl,
                                                 self.session["current_balance"])
            await self.emit("result", trade=item)
        await self.emit("round_result", round_id=str(observed.get("round_id")), pnl=round(combined_pnl, 2),
                        balance=self.session["current_balance"], panels=trades)
        await self.emit("balance", balance=self.session["current_balance"])
        if self.session.get("goal_balance") is not None and self.session["current_balance"] >= self.session["goal_balance"]:
            self.repository.append_shadow_ledger(self.session["session_id"], "WIN_LIMIT_REACHED", 0, self.session["current_balance"])
            await self.stop("WIN_LIMIT_REACHED"); await self.emit("alert", alert="WIN_LIMIT_REACHED")
        self.repository.save_shadow_session(self.session); await self.emit("metrics", metrics=self.status())
