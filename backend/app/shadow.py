"""Paper-trading session that reuses production decision and risk gates."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from app.betting.schemas import DecisionIntent


def _now():
    return datetime.now(timezone.utc).isoformat()


class ShadowManager:
    def __init__(self, *, repository, broadcaster=None):
        self.repository, self.broadcaster = repository, broadcaster
        self.mode = "STOPPED"
        self.session = None
        self._pending = {}
        self._processed_rounds = set()
        self._lock = asyncio.Lock()

    async def emit(self, event, **payload):
        if self.broadcaster:
            await self.broadcaster({"type": f"shadow:{event}", **payload})

    def status(self):
        s = self.session or {}
        return {"mode": self.mode, "session_id": s.get("session_id"),
                "starting_balance": s.get("starting_balance"), "current_balance": s.get("current_balance"),
                "goal_balance": s.get("goal_balance"), "profit": (None if not s else round(s["current_balance"] - s["starting_balance"], 2)),
                "bets": s.get("bets", 0), "wins": s.get("wins", 0), "losses": s.get("losses", 0),
                "unknown": s.get("unknown", 0), "roi": (None if not s or not s["starting_balance"] else (s["current_balance"] - s["starting_balance"]) / s["starting_balance"]),
                "drawdown": s.get("drawdown", 0), "longest_losing_streak": s.get("longest_losing_streak", 0),
                "risk_blocks": s.get("risk_blocks", 0), "production_isolated": True}

    def betting_status(self):
        s = self.session or {}
        return {"state": "SHADOW", "mode": "SHADOW", "automatic_enabled": self.mode == "SHADOW",
                "profile": s.get("profile", "PROFILE_A"), "current_balance": s.get("current_balance"),
                "starting_balance": s.get("starting_balance"), "goal_balance": s.get("goal_balance"),
                "current_bet": next(iter(self._pending.values()), None), "emergency_stop": False,
                "browser_status": "SHADOW", "last_ui_ready": True, "latest_history_round_id": s.get("latest_round_id")}

    def drift(self):
        sample = int((self.session or {}).get("bets", 0))
        if sample < 30:
            return {"status": "INSUFFICIENT_SAMPLE", "warnings": [], "sample_size": sample,
                    "promotion": "INSUFFICIENT_SAMPLE"}
        return {"status": "OBSERVING", "warnings": ["DATA_DRIFT_WARNING"], "sample_size": sample,
                "promotion": "UNCERTAIN"}

    async def start(self, *, profile="PROFILE_A", starting_balance=5000.0, goal_balance=None, health=None):
        async with self._lock:
            if self.mode == "SHADOW":
                return self.status()
            gate = health.can_bet_now(mode="SIMULATION") if health else {"allowed": False, "reasons": ["health unavailable"]}
            if not gate.get("allowed"):
                raise ValueError("SHADOW START BLOCKED: " + ", ".join(gate.get("reasons", [])))
            self.session = {"session_id": "SH-" + uuid.uuid4().hex[:12], "profile": profile.upper(),
                            "starting_balance": float(starting_balance), "current_balance": float(starting_balance),
                            "goal_balance": goal_balance, "bets": 0, "wins": 0, "losses": 0, "unknown": 0,
                            "staked": 0.0, "payout": 0.0, "peak": float(starting_balance), "drawdown": 0.0,
                            "losing_streak": 0, "longest_losing_streak": 0, "risk_blocks": 0,
                            "started_at": _now(), "config": {"profile": profile.upper()}}
            self.mode = "SHADOW"; self._pending.clear(); self._processed_rounds.clear()
            self.repository.save_shadow_session(self.session)
            self.repository.append_shadow_ledger(self.session["session_id"], "SHADOW_START", 0, self.session["current_balance"])
            await self.emit("started", session=self.status())
            return self.status()

    async def stop(self, reason="manual"):
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

    async def submit_decision(self, decision: dict, prediction: dict | None = None):
        """Send a shared production decision to the isolated shadow ledger."""
        if self.mode != "SHADOW" or not self.session:
            return {"accepted": False, "reason": "shadow_mode_not_active"}
        if decision.get("status") != "READY_FOR_EXECUTION":
            self.session["risk_blocks"] += 1
            self.repository.save_shadow_session(self.session)
            return {"accepted": False, "reason": "decision_not_risk_approved"}
        risk = decision.get("risk_evaluation") or decision.get("risk") or {}
        if (not risk.get("approved")
                or risk.get("status", "APPROVED") != "APPROVED"
                or str(decision.get("profile") or self.session["profile"]).upper() != self.session["profile"]
                or str(risk.get("profile") or decision.get("profile") or self.session["profile"]).upper() != self.session["profile"]):
            return {"accepted": False, "reason": "risk_profile_or_approval_mismatch"}
        decision_id = str(decision.get("decision_id") or "")
        target_round_id = str(decision.get("target_round_id") or "")
        if not decision_id or not target_round_id:
            return {"accepted": False, "reason": "decision_identity_missing"}
        if any(str(t.get("decision_id")) == decision_id
               and str(t.get("target_round_id")) == target_round_id
               for t in self.repository.list_shadow_trades(limit=1000)):
            return {"accepted": False, "reason": "duplicate_shadow_decision"}
        if target_round_id in self._pending:
            return {"accepted": False, "reason": "shadow_round_already_pending"}
        try:
            intent = DecisionIntent(
                decision_id=decision_id, round_id=target_round_id, execute=True,
                profile=self.session["profile"],
                cashout=risk.get("cashout", risk.get("cashout_target")),
                bet_amount=risk.get("approved_bet", risk.get("approved_bet_amount")),
                source="shadow",
            )
        except Exception as exc:
            return {"accepted": False, "reason": f"invalid_approved_decision:{exc}"}
        trade = {
            "execution_id": "P-" + uuid.uuid4().hex,
            "session_id": self.session["session_id"], "decision_id": decision_id,
            "prediction_id": (prediction or {}).get("prediction_id"),
            "target_round_id": target_round_id, "profile": self.session["profile"],
            "bet_amount": risk.get("approved_bet", risk.get("approved_bet_amount")),
            "cashout_target": intent.cashout,
            "balance_before": self.session["current_balance"],
            "status": "PAPER_AUTHORIZED", "created_at": _now(),
            "mode": "SHADOW_REALISTIC", "simulated": True,
        }
        self._pending[target_round_id] = trade
        self.repository.save_shadow_trade(trade)
        await self.emit("paper_trade", trade=trade)
        return {"accepted": True, "trade": trade}

    async def process_round(self, app, observed: dict):
        """Compatibility wrapper; production routing uses the shared decision pipeline."""
        await self.reconcile_round(observed)
        if self.mode != "SHADOW" or not self.session:
            return
        prediction = app.state.wp.model_registry.predict_latest(app.state.wp.dataset_service)
        evidence = app.state.wp.build_evidence(prediction) if prediction else None
        if not prediction or not evidence:
            return
        decision = await app.state.decision_engine.evaluate(
            prediction, evidence,
            model_status=app.state.wp.model_registry.status(app.state.wp.dataset_service),
            betting_status=self.betting_status(), risk_manager=app.state.risk,
        )
        await self.emit("prediction", prediction=prediction, session_id=self.session["session_id"])
        await self.emit("decision", decision=decision, session_id=self.session["session_id"])
        await self.submit_decision(decision, prediction)

    async def _reconcile(self, trade, observed):
        multiplier = float(observed.get("multiplier")); target = float(trade["cashout_target"])
        win = multiplier >= target; stake = float(trade["bet_amount"])
        pnl = stake * (target - 1) if win else -stake
        self.session["current_balance"] = round(self.session["current_balance"] + pnl, 2)
        self.session["bets"] += 1; self.session["staked"] += stake
        self.session["payout"] += stake * target if win else 0
        if win: self.session["wins"] += 1; self.session["losing_streak"] = 0
        else: self.session["losses"] += 1; self.session["losing_streak"] += 1; self.session["longest_losing_streak"] = max(self.session["longest_losing_streak"], self.session["losing_streak"])
        self.session["peak"] = max(self.session["peak"], self.session["current_balance"]); self.session["drawdown"] = self.session["peak"] - self.session["current_balance"]
        trade.update(status="PAPER_RECONCILED", result="WIN" if win else "LOSS", result_multiplier=multiplier,
                     pnl=pnl, balance_after=self.session["current_balance"], resolved_at=_now())
        self.repository.save_shadow_trade(trade)
        self.repository.append_shadow_ledger(self.session["session_id"], "PAPER_STAKE", -stake, trade["balance_before"])
        self.repository.append_shadow_ledger(self.session["session_id"], "PAPER_PAYOUT" if win else "PAPER_LOSS", pnl, self.session["current_balance"])
        await self.emit("result", trade=trade); await self.emit("balance", balance=self.session["current_balance"])
        if self.session.get("goal_balance") is not None and self.session["current_balance"] >= self.session["goal_balance"]:
            self.repository.append_shadow_ledger(self.session["session_id"], "GOAL_REACHED", 0, self.session["current_balance"])
            await self.stop("goal_reached"); await self.emit("alert", alert="GOAL_REACHED")
        self.repository.save_shadow_session(self.session); await self.emit("metrics", metrics=self.status())
