"""Paper-trading session that reuses production decision and risk gates."""
from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import datetime, timezone

from app.betting.schemas import DecisionIntent


def _now():
    return datetime.now(timezone.utc).isoformat()


class ShadowManager:
    def __init__(self, *, repository, broadcaster=None):
        self.repository, self.broadcaster = repository, broadcaster
        self.risk_manager = None
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
        current_round_exposure = sum(float(trade.get("bet_amount") or 0)
                                     for trades in self._pending.values()
                                     for trade in (trades if isinstance(trades, list) else [trades]))
        return {"mode": self.mode, "session_id": s.get("session_id"),
                "starting_balance": s.get("starting_balance"), "current_balance": s.get("current_balance"),
                "goal_balance": s.get("goal_balance"), "profit": (None if not s else round(s["current_balance"] - s["starting_balance"], 2)),
                "bets": s.get("bets", 0), "wins": s.get("wins", 0), "losses": s.get("losses", 0),
                "unknown": s.get("unknown", 0), "roi": (None if not s or not s["starting_balance"] else (s["current_balance"] - s["starting_balance"]) / s["starting_balance"]),
                "drawdown": s.get("drawdown", 0), "longest_losing_streak": s.get("longest_losing_streak", 0),
                "risk_blocks": s.get("risk_blocks", 0), "production_isolated": True,
                "configuration": s.get("config"), "panel_summary": panels,
                "total_exposure": s.get("total_exposure", 0),
                "current_round_exposure": current_round_exposure,
                "latest_round_id": s.get("latest_round_id"),
                "pending_rounds": list(self._pending),
                "stop_reason": s.get("end_reason"),
                "verified_real_balance": s.get("current_verified_real_balance"),
                "real_balance_observed_at": s.get("real_balance_observed_at"),
                "emergency_stop": bool(getattr(self.risk_manager, "emergency_latched", False))}

    def betting_status(self):
        s = self.session or {}
        return {"state": "SHADOW", "mode": "SHADOW", "automatic_enabled": self.mode == "SHADOW",
                "profile": s.get("profile", "PROFILE_A"), "current_balance": s.get("current_balance"),
                "real_balance_at_start": s.get("starting_balance"),
                "current_verified_real_balance": s.get("current_verified_real_balance"),
                "starting_balance": s.get("starting_balance"), "goal_balance": s.get("goal_balance"),
                "current_bet": next(iter(self._pending.values()), None),
                "emergency_stop": bool(getattr(self.risk_manager, "emergency_latched", False)),
                "consecutive_losses": s.get("losing_streak", 0),
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

    async def start(self, *, profile="PROFILE_A", starting_balance=5000.0, goal_balance=None,
                    health=None, configuration=None):
        async with self._lock:
            if self.mode == "SHADOW":
                return self.status()
            if self._pending:
                raise ValueError("SHADOW START BLOCKED: pending panel results require exact-round reconciliation")
            gate = health.can_bet_now(mode="SIMULATION") if health else {"allowed": False, "reasons": ["health unavailable"]}
            if not gate.get("allowed"):
                raise ValueError("SHADOW START BLOCKED: " + ", ".join(gate.get("reasons", [])))
            config = self._validate_configuration(configuration or {}, profile, starting_balance, goal_balance)
            self.session = {"session_id": "SH-" + uuid.uuid4().hex[:12], "profile": profile.upper(),
                            "starting_balance": float(starting_balance), "current_balance": float(starting_balance),
                            "goal_balance": goal_balance, "bets": 0, "wins": 0, "losses": 0, "unknown": 0,
                            "staked": 0.0, "payout": 0.0, "peak": float(starting_balance), "drawdown": 0.0,
                            "losing_streak": 0, "longest_losing_streak": 0, "risk_blocks": 0,
                            "started_at": _now(), "config": config,
                            "total_exposure": 0.0,
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
    def _validate_configuration(configuration, profile, starting_balance, goal_balance):
        """Normalize the one authoritative two-panel session configuration."""
        from app.betting.profiles import get_profile
        from app.risk.config import get_risk_profile
        profile_data = get_profile(profile)
        risk_profile = get_risk_profile(profile)
        if not isinstance(starting_balance, (int, float)) or not math.isfinite(float(starting_balance)) or starting_balance <= 0:
            raise ValueError("starting balance must be a positive verified number")
        if goal_balance is None or not isinstance(goal_balance, (int, float)) or not math.isfinite(float(goal_balance)) or goal_balance <= starting_balance:
            raise ValueError("goal must be a finite amount above the verified starting balance")
        mode = str(configuration.get("mode", "AUTOMATIC")).upper()
        if mode not in {"MANUAL", "AUTOMATIC"}:
            raise ValueError("mode must be MANUAL or AUTOMATIC")
        panels = configuration.get("panels") or [
            {"enabled": True, "stake": None, "cashout": 2.0},
            {"enabled": True, "stake": None, "cashout": 1.5},
        ]
        if not isinstance(panels, list) or len(panels) != 2:
            raise ValueError("exactly two panel configurations are required")
        normalized = []
        for index, panel in enumerate(panels, 1):
            enabled = bool(panel.get("enabled", True))
            try:
                target = round(float(panel.get("cashout", 2.0 if index == 1 else 1.5)), 2)
            except (TypeError, ValueError):
                raise ValueError(f"panel {index} cashout must be numeric")
            if not 1.01 <= target <= 100:
                raise ValueError(f"panel {index} cashout must be between 1.01x and 100x")
            stake = panel.get("stake")
            if mode == "MANUAL" and enabled:
                if (isinstance(stake, bool) or not isinstance(stake, (int, float))
                        or not math.isfinite(float(stake)) or int(stake) != stake):
                    raise ValueError(f"panel {index} stake must be a positive whole number")
                if stake < risk_profile.minimum_bet or stake > risk_profile.maximum_bet:
                    raise ValueError(f"panel {index} stake is outside the selected Risk profile limits")
                if target > risk_profile.cashout:
                    raise ValueError(f"panel {index} cashout exceeds the selected Risk profile target")
                stake = int(stake)
            else:
                stake = None
            normalized.append({"panel": index, "enabled": enabled, "stake": stake,
                               "cashout": target if mode == "MANUAL" else (2.0 if index == 1 else 1.5)})
        if not any(panel["enabled"] for panel in normalized):
            raise ValueError("enable at least one panel")
        combined_limit = configuration.get("maximum_combined_exposure")
        if combined_limit is None:
            combined_limit = min(float(starting_balance), float(profile_data.max_amount_bif))
        try:
            combined_limit = float(combined_limit)
        except (TypeError, ValueError):
            raise ValueError("maximum combined exposure must be numeric")
        if not math.isfinite(combined_limit) or combined_limit <= 0 or combined_limit > float(starting_balance):
            raise ValueError("maximum combined exposure must be positive and no greater than the verified balance")
        if mode == "MANUAL":
            exposure = sum(p["stake"] or 0 for p in normalized if p["enabled"])
            if exposure > combined_limit:
                raise ValueError("combined panel stakes exceed the maximum combined exposure")
            if exposure > float(starting_balance) * risk_profile.maximum_balance_percentage:
                raise ValueError("combined manual stakes exceed the Risk profile balance percentage")
        return {"mode": mode, "starting_balance": float(starting_balance),
                "goal_balance": float(goal_balance) if goal_balance is not None else None,
                "profile": profile.upper(), "panels": normalized,
                "maximum_combined_exposure": combined_limit,
            "limits": {"session_loss": profile_data.max_loss_bif,
                           "minimum_stake": profile_data.min_amount_bif,
                           "maximum_stake": profile_data.max_amount_bif}}

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

    async def submit_decision(self, decision: dict, prediction: dict | None = None,
                              *, emergency_stop: bool = False):
        async with self._lock:
            return await self._submit_decision(decision, prediction, emergency_stop=emergency_stop)

    async def _submit_decision(self, decision: dict, prediction: dict | None = None,
                               *, emergency_stop: bool = False):
        """Send a shared production decision to the isolated shadow ledger."""
        if self.mode != "SHADOW" or not self.session:
            return {"accepted": False, "reason": "shadow_mode_not_active"}
        if emergency_stop:
            self.session["risk_blocks"] += 1
            self.repository.save_shadow_session(self.session)
            return {"accepted": False, "reason": "emergency_stop"}
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
        all_trades = self.repository.list_shadow_trades(limit=5000)
        if any(str(t.get("decision_id")) == decision_id
               and str(t.get("target_round_id")) == target_round_id
               for t in all_trades):
            return {"accepted": False, "reason": "duplicate_shadow_decision"}
        if target_round_id in self._pending:
            return {"accepted": False, "reason": "shadow_round_already_pending"}
        config = self.session.get("config", {})
        approved = risk.get("approved_bet", risk.get("approved_bet_amount"))
        if isinstance(approved, bool) or not isinstance(approved, (int, float)) or approved <= 0:
            return {"accepted": False, "reason": "risk_approved_stake_missing"}
        active = [p for p in config.get("panels", []) if p["enabled"]]
        if not active:
            return {"accepted": False, "reason": "no_enabled_panels"}
        occupied = {(str(t.get("target_round_id")), int(t.get("panel", 1)))
                    for t in all_trades if t.get("session_id") == self.session["session_id"]}
        if any((target_round_id, p["panel"]) in occupied for p in active):
            return {"accepted": False, "reason": "duplicate_panel_round"}
        stakes = ({p["panel"]: p["stake"] for p in active} if config.get("mode") == "MANUAL"
                  else self._allocate_automatic_stakes(active, int(approved), config))
        if len(stakes) != len(active):
            self.session["risk_blocks"] += 1
            return {"accepted": False, "reason": "approved_stake_too_small_for_enabled_panels"}
        exposure = sum(stakes.values())
        try:
            from app.risk.config import get_risk_profile
            risk_profile = get_risk_profile(self.session["profile"])
            combined_balance_limit = self.session["current_balance"] * risk_profile.maximum_balance_percentage
        except Exception:
            combined_balance_limit = 0
        if exposure > combined_balance_limit:
            self.session["risk_blocks"] += 1
            return {"accepted": False, "reason": "combined_exposure_exceeds_risk_balance_percentage"}
        approved_cashout = risk.get("cashout", risk.get("cashout_target"))
        if config.get("mode") == "MANUAL" and any(p["cashout"] > float(approved_cashout) for p in active):
            self.session["risk_blocks"] += 1
            return {"accepted": False, "reason": "manual_cashout_exceeds_risk_profile_target"}
        if exposure > float(approved):
            self.session["risk_blocks"] += 1
            return {"accepted": False, "reason": "combined_panel_exposure_exceeds_risk_approval"}
        if exposure > config.get("maximum_combined_exposure", 0):
            self.session["risk_blocks"] += 1
            return {"accepted": False, "reason": "maximum_combined_exposure_exceeded"}
        if exposure > self.session["current_balance"]:
            self.session["risk_blocks"] += 1
            return {"accepted": False, "reason": "insufficient_verified_balance"}
        trades = []
        for panel in active:
            try:
                intent = DecisionIntent(
                    decision_id=f"{decision_id}:P{panel['panel']}", round_id=target_round_id,
                    execute=True, profile=self.session["profile"], cashout=panel["cashout"],
                    bet_amount=stakes[panel["panel"]], source=f"shadow:panel-{panel['panel']}",
                )
            except Exception as exc:
                return {"accepted": False, "reason": f"invalid_panel_{panel['panel']}:{exc}"}
            trade = {
                "execution_id": "P-" + uuid.uuid4().hex,
                "session_id": self.session["session_id"], "decision_id": decision_id,
                "prediction_id": (prediction or {}).get("prediction_id"),
                "target_round_id": target_round_id, "panel": panel["panel"],
                "profile": self.session["profile"], "bet_amount": stakes[panel["panel"]],
                "cashout_target": intent.cashout, "balance_before": self.session["current_balance"],
                "status": "PAPER_AUTHORIZED", "bet_accepted": True, "cashout_confirmed": False,
                "created_at": _now(), "mode": "SHADOW_REALISTIC", "simulated": True,
            }
            trades.append(trade)
        self._pending[target_round_id] = trades
        self.session["total_exposure"] = round(self.session.get("total_exposure", 0) + exposure, 2)
        for trade in trades:
            self.repository.save_shadow_trade(trade)
            await self.emit("paper_trade", trade=trade)
        self.repository.save_shadow_session(self.session)
        return {"accepted": True, "trades": trades}

    @staticmethod
    def _allocate_automatic_stakes(panels, approved, config):
        # Risk approval is one combined round budget; split it evenly, with
        # any odd remainder assigned to the first enabled panel.
        cap = min(approved, int(config.get("maximum_combined_exposure", approved)))
        count = len(panels)
        if cap < count or cap // count < int(config.get("limits", {}).get("minimum_stake", 1)):
            return {}
        base, remainder = divmod(cap, count)
        result = {}
        for index, panel in enumerate(panels):
            result[panel["panel"]] = base + (1 if index < remainder else 0)
        return result

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
        await self.submit_decision(decision, prediction,
                                   emergency_stop=bool(getattr(app.state.risk, "emergency_latched", False)))

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
                self.session["losing_streak"] = 0
            else:
                self.session["losses"] += 1; summary["losses"] += 1
                self.session["losing_streak"] += 1
                self.session["longest_losing_streak"] = max(self.session["longest_losing_streak"], self.session["losing_streak"])
            item.update(status="PAPER_RECONCILED", result="WIN" if win else "LOSS",
                        result_multiplier=multiplier, cashout_confirmed=bool(win),
                        pnl=round(pnl, 2), payout=round(stake * target if win else 0, 2),
                        balance_after=self.session["current_balance"], resolved_at=_now())
            self.repository.save_shadow_trade(item)
            self.repository.append_shadow_ledger(self.session["session_id"], "PAPER_PANEL_RESULT", pnl,
                                                 self.session["current_balance"])
            await self.emit("result", trade=item)
        self.session["peak"] = max(self.session["peak"], self.session["current_balance"])
        self.session["drawdown"] = self.session["peak"] - self.session["current_balance"]
        await self.emit("round_result", round_id=str(observed.get("round_id")), pnl=round(combined_pnl, 2),
                        balance=self.session["current_balance"], panels=trades)
        await self.emit("balance", balance=self.session["current_balance"])
        if self.session["current_balance"] <= self.session["starting_balance"] - self.session["config"]["limits"]["session_loss"]:
            await self.stop("session_loss_limit")
        if self.session.get("goal_balance") is not None and self.session["current_balance"] >= self.session["goal_balance"]:
            self.repository.append_shadow_ledger(self.session["session_id"], "GOAL_REACHED", 0, self.session["current_balance"])
            await self.stop("GOAL_REACHED"); await self.emit("alert", alert="GOAL_REACHED")
        self.repository.save_shadow_session(self.session); await self.emit("metrics", metrics=self.status())
