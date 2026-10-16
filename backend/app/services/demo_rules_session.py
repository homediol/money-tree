"""Isolated SPRIBE Demo sessions over a volatile round buffer."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import re
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4

from app.rules_engine import (
    COMBINED_POLICY_ID, LEGACY_POLICY_ID, START_CHECK_POLICY_ID,
    RulesEngine, _fresh_engine, _identity, _reference, _expected,
    _history_is_contiguous, _now,
)
from app.round_buffer import InMemoryRoundBuffer

DEMO_ORIGIN = "https://aviator-demo.spribegaming.com"
DEMO_SESSION_TTL_HOURS = 8
PHASE_EVIDENCE_MAX_AGE_SECONDS = 3
PANEL_EVIDENCE_MAX_AGE_SECONDS = 5
PANEL_CONFIGURATION_MAX_AGE_SECONDS = 15


def _parsed_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc) if stamp.tzinfo else None


def _age_seconds(value: Any, now: datetime) -> float | None:
    stamp = _parsed_time(value)
    return (now - stamp).total_seconds() if stamp else None


def demo_environment_evidence(value: Mapping[str, Any]) -> bool:
    """Accept only explicit SPRIBE Demo origin evidence from the collector."""
    return (value.get("environment") == "DEMO"
            and value.get("demo_origin") == DEMO_ORIGIN
            and value.get("source") in {
                "SPRIBE_DEMO_DIRECT_PAGE_HISTORY",
                "SPRIBE_DEMO_GAME_FRAME_READ_ONLY",
            })


class DemoRulesSessionManager:
    def __init__(self, repository: Any, rules_engine: RulesEngine,
                 *, round_buffer: InMemoryRoundBuffer | None = None,
                 now_provider=lambda: datetime.now(timezone.utc)):
        self.repository = repository
        self.rules_engine = rules_engine
        self.round_buffer = round_buffer or InMemoryRoundBuffer()
        self.now_provider = now_provider
        self._lock = RLock()
        recovered_at = self.now_provider().astimezone(timezone.utc).isoformat()
        self.repository.recover_demo_rule_sessions(recovered_at)
        self.sessions: dict[str, dict[str, Any]] = {
            str(row["session_id"]): row for row in self.repository.list_demo_rule_sessions(1000)
        }
        self.latest_game_phase: dict[str, Any] = {
            "phase": "UNKNOWN", "round_phase": "UNKNOWN", "observed_at": None,
            "evidence": {"reason": "NO_FRESH_DEMO_PHASE_OBSERVATION"},
            "panel_observations": [],
        }

    def _active(self) -> dict[str, Any] | None:
        return next((row for row in self.sessions.values()
                     if row.get("status") == "ACTIVE"), None)

    def start(self, rule_ids: list[str] | None = None, *, execution_panel: str | None = None) -> dict[str, Any]:
        if rule_ids is None:
            rule_ids = [rule_id for rule_id, config in self.rules_engine.rule_configuration.items()
                        if config.get("configured") is True and config.get("enabled") is True]
        if not isinstance(rule_ids, list) or not rule_ids or len(set(rule_ids)) != len(rule_ids):
            raise ValueError("NO_ENABLED_DEMO_RULES_OR_INVALID_RULE_IDS")
        if execution_panel not in {None, "PANEL_1", "PANEL_2"}:
            raise ValueError("DEMO_EXECUTION_PANEL_INVALID")
        registry = {row["rule_id"] for row in self.rules_engine.registry().get("rules", [])}
        for rule_id in rule_ids:
            config = self.rules_engine.rule_configuration.get(rule_id)
            if rule_id not in registry or not config or config.get("enabled") is not True:
                raise ValueError(f"DEMO_RULE_NOT_ENABLED:{rule_id}")
        now_dt = self.now_provider().astimezone(timezone.utc)
        now = now_dt.isoformat()
        stream = self.round_buffer.snapshot()
        latest = stream.get("latest") or {}
        context: list[dict[str, Any]] = []
        for row in stream.get("rounds", [])[-5:]:
            normalized = _identity(row, require_sequence=True)
            if normalized and normalized.get("continuity_status") == "VERIFIED" and normalized["stream_id"] == stream["epoch_id"]:
                if context and (normalized["round_index"] != context[-1]["round_index"] + 1
                                or normalized["stream_sequence"] != context[-1]["stream_sequence"] + 1):
                    context = []
                context.append(normalized)
        context = context[-5:]
        session_id = str(uuid4())
        versions = {rule_id: self.rules_engine._version(rule_id) for rule_id in rule_ids}
        manifest = self.rules_engine._manifest(rule_ids)
        session = {
            "session_id": session_id,
            "environment": "DEMO",
            "status": "START",
            "enabled_rule_ids": list(rule_ids),
            "rule_versions": versions,
            "manifest": manifest,
            "execution_panel": execution_panel,
            "started_at": now,
            "updated_at": now,
            "stopped_at": None,
            "expires_at": (now_dt + timedelta(hours=DEMO_SESSION_TTL_HOURS)).isoformat(),
            "stream_id": stream["epoch_id"],
            "buffer_epoch_id": stream["epoch_id"],
            "cursor_sequence": int(stream.get("sequence") or 0),
            "last_round_index": int(latest.get("round_index") or 0),
            "last_round_id": latest.get("round_id"),
            "context": context,
            "engine_states": {rule_id: _fresh_engine(rule_id) for rule_id in rule_ids},
            "pending_selections": [],
            "decisions": [],
            "latest_decision": None,
            "latest_game_phase": deepcopy(self.latest_game_phase),
            "latest_panel_observations": [],
            "selection_count": 0,
            "no_select_count": 0,
            "blocked_count": 0,
            "execution_attempt_count": 0,
            "errors": [],
            "rules_engine_environment": "DEMO",
            "real_money_action_submitted": False,
        }
        with self._lock:
            if self._active() or any(row.get("status") in {"START", "RECOVERY"}
                                      for row in self.sessions.values()):
                raise ValueError("DEMO_SESSION_ALREADY_ACTIVE_OR_REQUIRES_RECOVERY")
            self.repository.start_demo_rule_session(session)
            self.sessions[session_id] = deepcopy(session)
        return self.get(session_id) or session

    def stop(self, session_id: str, reason: str = "OPERATOR_STOP") -> dict[str, Any]:
        with self._lock:
            session = self.sessions.get(str(session_id))
            if not session:
                raise KeyError("demo_session_not_found")
            if session.get("status") not in {"ACTIVE", "RECOVERY", "START"}:
                return deepcopy(session)
            now = self.now_provider().astimezone(timezone.utc).isoformat()
            session.update(status="STOP", stopped_at=now, updated_at=now, stop_reason=str(reason))
            self.repository.update_demo_rule_session(session, "STOP", {"reason": reason})
            return deepcopy(session)

    def recover(self, session_id: str) -> dict[str, Any]:
        """Explicitly recover a persisted Demo session after a backend restart."""
        with self._lock:
            session = self.sessions.get(str(session_id))
            if not session:
                raise KeyError("demo_session_not_found")
            if session.get("status") != "RECOVERY":
                raise ValueError("DEMO_SESSION_NOT_IN_RECOVERY")
            now_dt = self.now_provider().astimezone(timezone.utc)
            if _parsed_time(session.get("expires_at")) is None or now_dt >= _parsed_time(session["expires_at"]):
                session.update(status="EXPIRED", updated_at=now_dt.isoformat(),
                               stopped_at=now_dt.isoformat(), stop_reason="SESSION_EXPIRED")
                self.repository.update_demo_rule_session(session, "EXPIRED", {"reason": "SESSION_EXPIRED"})
                return deepcopy(session)
            stream = self.round_buffer.snapshot()
            context = []
            for row in stream.get("rounds", [])[-5:]:
                normalized = _identity(row, require_sequence=True)
                if normalized and normalized.get("continuity_status") == "VERIFIED":
                    if context and (normalized["round_index"] != context[-1]["round_index"] + 1
                                    or normalized["stream_sequence"] != context[-1]["stream_sequence"] + 1):
                        context = []
                    context.append(normalized)
            session.update(status="ACTIVE", updated_at=now_dt.isoformat(), recovery_reason=None,
                           stream_id=stream["epoch_id"], buffer_epoch_id=stream["epoch_id"],
                           cursor_sequence=int(stream.get("sequence") or 0),
                           last_round_index=int((stream.get("latest") or {}).get("round_index") or 0),
                           last_round_id=(stream.get("latest") or {}).get("round_id"),
                           context=context[-5:], pending_selections=[],
                           engine_states={rule_id: _fresh_engine(rule_id)
                                          for rule_id in session["enabled_rule_ids"]},
                           decisions=[], latest_decision=None, latest_game_phase=deepcopy(self.latest_game_phase),
                           latest_panel_observations=[])
            for attempt in self.repository.demo_execution_attempts(session_id, 500):
                if attempt.get("state") in {"SELECT_RECEIVED", "WAITING_FOR_WINDOW", "EXECUTION_ELIGIBLE", "UNKNOWN"}:
                    attempt.update(state="UNKNOWN", reason="MEMORY_BUFFER_RESET_REQUIRES_FRESH_ROUNDS",
                                   blockers=["MEMORY_BUFFER_RESET_REQUIRES_FRESH_ROUNDS"],
                                   updated_at=now_dt.isoformat())
                    self.repository.update_demo_execution_attempt(attempt)
            self.repository.update_demo_rule_session(session, "ACTIVE", {"reason": "EXPLICIT_OPERATOR_RECOVERY"})
            self.sessions[str(session_id)] = deepcopy(session)
            return deepcopy(session)

    def _expire_if_due(self, session: dict[str, Any], now: datetime) -> bool:
        expires = _parsed_time(session.get("expires_at"))
        if session.get("status") not in {"ACTIVE", "RECOVERY"} or expires is None or now < expires:
            return False
        session.update(status="EXPIRED", updated_at=now.isoformat(), stopped_at=now.isoformat(),
                       stop_reason="SESSION_EXPIRED")
        self.repository.update_demo_rule_session(session, "EXPIRED", {"reason": "SESSION_EXPIRED"})
        return True

    def _consume_unprocessed(self, session: dict[str, Any]) -> None:
        events = [row for row in self.round_buffer.snapshot().get("rounds", [])
                  if row.get("stream_id") == session.get("stream_id")
                  and int(row.get("stream_sequence") or 0) > int(session.get("cursor_sequence") or 0)]
        for event in events:
            if session.get("status") != "ACTIVE":
                break
            self._evaluate_round(session, event)

    def ingest_observation(self, event: dict[str, Any], *, demo_source: str) -> dict[str, Any]:
        payload = {**event, "environment": "DEMO", "source": demo_source,
                   "demo_origin": DEMO_ORIGIN}
        result = self.round_buffer.append(payload)
        with self._lock:
            session = self._active()
            if result.get("reset") and session:
                at = self.now_provider().astimezone(timezone.utc).isoformat()
                self._recovery(session, at, "IN_MEMORY_BUFFER_RESET_COLLECTOR_SESSION_CHANGED", payload)
            elif result.get("inserted") and session:
                if result["round"].get("stream_id") != session.get("stream_id"):
                    at = self.now_provider().astimezone(timezone.utc).isoformat()
                    self._recovery(session, at, "IN_MEMORY_BUFFER_EPOCH_CHANGED", payload)
                else:
                    self._evaluate_round(session, result["round"])
        active = self._active()
        return {"inserted": result.get("inserted", False), "reset": result.get("reset", False),
                "snapshot": result.get("snapshot"),
                "session_dispatch": "PROCESSED" if active and result.get("inserted") else
                    "NO_ACTIVE_DEMO_SESSION" if not active else "DUPLICATE"}

    def ingest_committed_round(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = {**event, "event_type": "round.observed", "durability": "IN_MEMORY",
                   "environment": "DEMO", "demo_origin": DEMO_ORIGIN}
        result = self.ingest_observation(payload,
            demo_source=str(event.get("source") or "SPRIBE_DEMO_DIRECT_PAGE_HISTORY"))
        latest = result.get("snapshot", {}).get("latest")
        return {"ok": True, "inserted": result["inserted"], "event": latest,
                "session_dispatch": result["session_dispatch"], "durability": "IN_MEMORY"}

    def _resolve_previous_targets(self, session: dict[str, Any], source: dict[str, Any], at: str) -> None:
        keep = []
        for selection in session.get("pending_selections", []):
            target = selection.get("target") or {}
            if (target.get("streamId") != source.get("stream_id")
                    or int(target.get("roundIndex") or -1) != int(source.get("round_index") or -2)):
                keep.append(selection)
                continue
            outcome = "WIN" if float(source["multiplier"]) >= 2.0 else "LOSS"
            audit = {"selection_id": selection["selection_id"], "target_round_id": source["round_id"],
                     "target_round_index": source["round_index"], "observed_multiplier": source["multiplier"],
                     "rule_outcome": outcome, "result_kind": "OBSERVATIONAL_RULE_OUTCOME",
                     "financial_bet_result": None, "observed_at": at}
            session.setdefault("rule_target_observations", []).append(audit)
            session["rule_target_observations"] = session["rule_target_observations"][-200:]
            for rule_id in selection["contributing_rule_ids"]:
                state = session["engine_states"][rule_id]
                if state.get("pending_selection_id") == selection["selection_id"]:
                    self.rules_engine._advance_selected_state(
                        {"mode": "ALL_RULES_TOGETHER"}, rule_id, state, source, outcome, selection, at)
                    state["pending_selection_id"] = None
                    state["pending_target"] = None
        session["pending_selections"] = keep

    def _evaluate_round(self, session: dict[str, Any], event: dict[str, Any]) -> None:
        now_dt = self.now_provider().astimezone(timezone.utc)
        at = now_dt.isoformat()
        if self._expire_if_due(session, now_dt):
            return
        if (event.get("environment") != "DEMO" or event.get("durability") != "IN_MEMORY"
                or event.get("event_type") != "round.observed"):
            self._record_blocked_round(session, event, at, "DEMO_IN_MEMORY_ROUND_REQUIRED")
            return
        source = _identity(event, require_sequence=True)
        if source is None:
            self._record_blocked_round(session, event, at, "DEMO_ROUND_IDENTITY_UNAVAILABLE")
            return
        if event.get("continuity_status") != "VERIFIED":
            self._record_blocked_round(session, event, at, "DEMO_ROUND_CONTINUITY_UNKNOWN", source)
            session["context"] = []
            return
        if source["stream_id"] != session.get("stream_id"):
            self._recovery(session, at, "IN_MEMORY_BUFFER_EPOCH_CHANGED", event)
            return
        if source["stream_sequence"] <= int(session.get("cursor_sequence") or 0):
            return
        self._resolve_previous_targets(session, source, at)
        history = deepcopy(session.get("context") or []) + [deepcopy(source)]
        proposals: list[dict[str, Any]] = []
        for rule_id in session["enabled_rule_ids"]:
            before = deepcopy(session["engine_states"][rule_id])
            shell = {
                "session_id": session["session_id"], "mode": "ALL_RULES_TOGETHER",
                "status": "RUNNING", "selection_status": "ACTIVE",
                "rule_ids": list(session["enabled_rule_ids"]),
                "rule_versions": session["rule_versions"],
                "engines": session["engine_states"],
                "selections": [], "rule_selections": {},
            }
            try:
                proposal = self.rules_engine._proposal(shell, rule_id, source, history, at)
                if proposal.get("eligible") is False and rule_id in {
                        START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
                    session["engine_states"][rule_id] = deepcopy(
                        proposal.get("state_snapshot", {}).get("after") or before)
            except Exception as exc:
                proposal = {"rule_id": rule_id, "rule_version": session["rule_versions"][rule_id],
                            "enabled": True, "eligible": None, "result": None,
                            "status": "EVALUATION_ERROR", "reason": f"RULE_EVALUATION_ERROR:{type(exc).__name__}",
                            "evaluated_at": at, "source_round": _reference(source),
                            "expected_target": _expected(source), "state_snapshot": {"before": before, "after": before}}
            proposal["enabled"] = True
            proposals.append(proposal)

        true_rows = [row for row in proposals if row.get("eligible") is True]
        unavailable = [row for row in proposals if row.get("eligible") is None]
        action = "SELECT" if true_rows else "BLOCKED" if unavailable else "NO_SELECT"
        decision_id = hashlib.sha256(
            f"demo\0{session['session_id']}\0{source['stream_id']}\0{source['stream_sequence']}".encode()).hexdigest()
        expected = _expected(source)
        decision = {
            "decision_id": decision_id,
            "session_id": session["session_id"],
            "environment": "DEMO",
            "action": action,
            "reason": ("RULE_MATCHED" if true_rows else "RULE_UNAVAILABLE" if unavailable else "NO_ENABLED_RULE_MATCHED"),
            "true_rule_ids": [row["rule_id"] for row in true_rows],
            "source_round": {**_reference(source), "stream_sequence": source["stream_sequence"]},
            "target_round_id": None,
            "target_round_index": expected["roundIndex"] if true_rows else None,
            "target_identity_status": "UNKNOWN",
            "created_at": at,
            "evaluations": deepcopy(proposals),
            "real_money_action_submitted": False,
        }
        attempt = None
        if true_rows:
            transitions = [{"state": "SELECT_RECEIVED", "at": at},
                           {"state": "WAITING_FOR_WINDOW", "at": at}]
            attempt = {
                "attempt_id": str(uuid4()), "session_id": session["session_id"],
                "decision_id": decision_id, "target_round_id": None,
                "target_round_index": expected["roundIndex"],
                "state": "WAITING_FOR_WINDOW", "reason": "BETTING_WINDOW_UNKNOWN",
                "blockers": ["DEMO_TARGET_PROVIDER_IDENTITY_UNAVAILABLE",
                             "DEMO_BETTING_WINDOW_COUNTDOWN_UNVERIFIED"],
                "created_at": at, "updated_at": at, "transitions": transitions,
                "environment": "DEMO", "real_money_action_submitted": False,
                "ui_acknowledgement": None, "acceptance_receipt": None,
                "settlement_receipt": None, "result": None, "balance_change": None,
            }
            session["pending_selections"].append({
                "selection_id": decision_id, "source_round": _reference(source),
                "target": expected, "expected_target": expected,
                "contributing_rule_ids": [row["rule_id"] for row in true_rows],
                "contributors": deepcopy(true_rows), "selected_at": at, "status": "PENDING",
            })
            for proposal in true_rows:
                shell = {"session_id": session["session_id"], "mode": "ALL_RULES_TOGETHER",
                         "rule_ids": session["enabled_rule_ids"], "rule_versions": session["rule_versions"],
                         "engines": session["engine_states"]}
                selection = session["pending_selections"][-1]
                try:
                    self.rules_engine._commit_contributor(shell, proposal, selection)
                except Exception as exc:
                    session.setdefault("errors", []).append({"at": at, "reason": f"SELECT_STATE_COMMIT_ERROR:{type(exc).__name__}"})
            session["selection_count"] += 1
            session["execution_attempt_count"] += 1
        elif unavailable:
            session["blocked_count"] += 1
        else:
            session["no_select_count"] += 1
        session["updated_at"] = at
        session["cursor_sequence"] = source["stream_sequence"]
        session["last_round_index"] = source["round_index"]
        session["last_round_id"] = source["round_id"]
        session["context"] = history[-5:]
        session["latest_decision"] = deepcopy(decision)
        session.setdefault("decisions", []).append(deepcopy(decision))
        session["decisions"] = session["decisions"][-500:]
        persisted = self.repository.save_demo_evaluation(session, proposals, decision, attempt)
        if persisted and attempt:
            self._refresh_attempt(attempt)

    def _record_blocked_round(self, session: dict[str, Any], event: dict[str, Any], at: str,
                              reason: str, source: dict[str, Any] | None = None) -> None:
        session["blocked_count"] = int(session.get("blocked_count") or 0) + 1
        session["updated_at"] = at
        if source:
            session["cursor_sequence"] = source["stream_sequence"]
            session["last_round_index"] = source["round_index"]
            session["last_round_id"] = source["round_id"]
        decision_id = hashlib.sha256(
            f"demo-blocked\0{session['session_id']}\0{event.get('stream_id')}\0{event.get('stream_sequence')}".encode()).hexdigest()
        evaluations = [{"rule_id": rule_id, "rule_version": session["rule_versions"][rule_id],
                        "enabled": True, "eligible": None, "status": reason, "reason": reason,
                        "evaluated_at": at} for rule_id in session["enabled_rule_ids"]]
        source_row = source or {"stream_id": str(session["stream_id"]),
                                "stream_sequence": int(event.get("stream_sequence") or 0),
                                "round_id": str(event.get("round_id") or "UNKNOWN"),
                                "round_index": int(event.get("round_index") or 0)}
        decision = {"decision_id": decision_id, "session_id": session["session_id"],
                    "environment": "DEMO", "action": "BLOCKED", "reason": reason,
                    "true_rule_ids": [], "source_round": source_row, "target_round_id": None,
                    "target_round_index": None, "target_identity_status": "UNKNOWN",
                    "created_at": at, "evaluations": evaluations,
                    "real_money_action_submitted": False}
        session["latest_decision"] = deepcopy(decision)
        session.setdefault("decisions", []).append(deepcopy(decision))
        self.repository.save_demo_evaluation(session, evaluations, decision)

    def _recovery(self, session: dict[str, Any], at: str, reason: str,
                  event: Mapping[str, Any]) -> None:
        session.update(status="RECOVERY", updated_at=at, recovery_reason=reason,
                       received_round_id=event.get("round_id"))
        self.repository.update_demo_rule_session(session, "RECOVERY", {
            "reason": reason, "round_id": event.get("round_id")})

    def _refresh_attempt(self, attempt: dict[str, Any]) -> None:
        """Update the read-only eligibility record; this path cannot click."""
        now = self.now_provider().astimezone(timezone.utc)
        phase = self.latest_game_phase
        evidence = phase.get("evidence") if isinstance(phase.get("evidence"), dict) else {}
        target_id = evidence.get("target_round_id")
        identity_verified = (evidence.get("target_identity_status") == "VERIFIED"
                             and isinstance(target_id, str) and bool(target_id.strip()))
        phase_age = _age_seconds(phase.get("observed_at"), now)
        deadline = _parsed_time(evidence.get("deadline_at"))
        countdown = evidence.get("countdown_seconds")
        window_verified = (
            phase.get("phase") == "BETTING_OPEN"
            and evidence.get("source") == "AVIATOR_EXPLICIT_BETTING_PHASE_DOM"
            and evidence.get("phase_indicator_visible") is True
            and evidence.get("explicit_window_state") == "BETTING_OPEN"
            and isinstance(countdown, (int, float)) and not isinstance(countdown, bool)
            and countdown > 0 and deadline is not None
            and phase_age is not None and -0.25 <= phase_age <= PHASE_EVIDENCE_MAX_AGE_SECONDS
            and (deadline - now).total_seconds() > 0
        )
        panel_id = next((row for row in self.sessions.get(attempt["session_id"], {}).get("latest_panel_observations", [])
                         if row.get("panel_id") == self.sessions.get(attempt["session_id"], {}).get("execution_panel")), None)
        panel_age = _age_seconds(panel_id.get("observed_at"), now) if panel_id else None
        panel_verified = bool(panel_id and panel_id.get("state") == "BET_AVAILABLE"
                              and panel_age is not None and -0.25 <= panel_age <= PANEL_EVIDENCE_MAX_AGE_SECONDS
                              and panel_id.get("frame_identity") == evidence.get("frame_identity"))
        session = self.sessions.get(attempt["session_id"], {})
        config_id = next((row for row in phase.get("panel_configurations", [])
                          if row.get("panel_id") == session.get("execution_panel")), None)
        config_age = _age_seconds(config_id.get("observed_at"), now) if config_id else None
        config_verified = bool(
            config_id and config_id.get("status") == "VERIFIED"
            and config_id.get("requirements_satisfied") is True
            and config_id.get("auto_bet") == "OFF"
            and config_id.get("auto_cashout") == "ON"
            and isinstance(config_id.get("cashout_target"), (int, float))
            and not isinstance(config_id.get("cashout_target"), bool)
            and round(float(config_id["cashout_target"]), 2) == 2.0
            and isinstance(config_id.get("stake"), (int, float))
            and not isinstance(config_id.get("stake"), bool)
            and float(config_id["stake"]) > 0
            and isinstance(config_id.get("currency"), str)
            and bool(re.fullmatch(r"[A-Z]{3}", config_id["currency"]))
            and config_age is not None
            and -0.25 <= config_age <= PANEL_CONFIGURATION_MAX_AGE_SECONDS
            and config_id.get("frame_identity") == evidence.get("frame_identity"))
        attempt["updated_at"] = now.isoformat()
        attempt["target_round_id"] = target_id if identity_verified else None
        attempt["target_identity_status"] = "VERIFIED" if identity_verified else "UNKNOWN"
        attempt["betting_window"] = {"status": "VERIFIED" if window_verified else "UNKNOWN",
                                     "phase": phase.get("phase", "UNKNOWN"),
                                     "deadline_at": evidence.get("deadline_at") if window_verified else None,
                                     "observed_at": phase.get("observed_at"),
                                     "remaining_seconds": (deadline - now).total_seconds() if window_verified else None,
                                     "source": evidence.get("source"),
                                     "phase_indicator_visible": evidence.get("phase_indicator_visible") is True,
                                     "explicit_window_state": evidence.get("explicit_window_state"),
                                     "countdown_seconds": countdown,
                                     "phase_text": evidence.get("text"),
                                     "frame_identity": evidence.get("frame_identity")}
        attempt["panel_evidence"] = deepcopy(panel_id) if panel_id else None
        attempt["panel_configuration_evidence"] = deepcopy(config_id) if config_id else None
        blockers = []
        if phase.get("environment") != "DEMO":
            blockers.append("DEMO_ENVIRONMENT_NOT_VERIFIED")
        if not identity_verified:
            blockers.append("DEMO_TARGET_PROVIDER_IDENTITY_UNAVAILABLE")
        if not window_verified:
            blockers.append("DEMO_BETTING_WINDOW_COUNTDOWN_UNVERIFIED")
        if not panel_verified:
            blockers.append("DEMO_SELECTED_PANEL_NOT_FRESHLY_BET_AVAILABLE")
        if not config_verified:
            blockers.append("DEMO_PANEL_CONFIGURATION_UNVERIFIED")
        if blockers:
            attempt["state"] = "WAITING_FOR_WINDOW" if phase.get("phase") != "BETTING_OPEN" or not window_verified else "UNKNOWN"
            attempt["reason"] = blockers[0]
            attempt["blockers"] = blockers
        else:
            attempt["state"] = "EXECUTION_ELIGIBLE"
            attempt["reason"] = "DEMO_PREREQUISITES_VERIFIED_WAITING_FOR_BROWSER_OWNER"
            attempt["blockers"] = []
        last = attempt.get("transitions", [])[-1] if attempt.get("transitions") else {}
        if last.get("state") != attempt["state"] or last.get("reason") != attempt["reason"]:
            attempt.setdefault("transitions", []).append({"state": attempt["state"],
                "at": attempt["updated_at"], "reason": attempt["reason"]})
        self.repository.update_demo_execution_attempt(attempt)

    def claim_execution(self) -> dict[str, Any] | None:
        active = self._active()
        if not active:
            return None
        now = self.now_provider().astimezone(timezone.utc)
        self.repository.expire_demo_execution_claims(
            (now - timedelta(seconds=15)).isoformat())
        attempts = self.repository.demo_execution_attempts(active["session_id"], 500)
        for attempt in attempts:
            if attempt.get("state") == "EXECUTION_ELIGIBLE":
                self._refresh_attempt(attempt)
        return self.repository.claim_demo_execution_attempt(now.isoformat())

    def complete_execution(self, attempt_id: str, claim_token: str,
                           result: Mapping[str, Any]) -> bool:
        allowed = {"BET_SUBMITTED", "BET_QUEUED", "BET_ACTIVE", "UNKNOWN", "FAILED"}
        state = str(result.get("state") or "UNKNOWN").upper()
        if state not in allowed:
            raise ValueError("DEMO_EXECUTION_RESULT_STATE_INVALID")
        attempts = self.repository.demo_execution_attempts(None, 1000)
        attempt = next((row for row in attempts if str(row.get("attempt_id")) == str(attempt_id)), None)
        if not attempt or attempt.get("environment") != "DEMO":
            return False
        at = self.now_provider().astimezone(timezone.utc).isoformat()
        attempt.update({"state": state, "reason": str(result.get("reason") or state),
                        "updated_at": at, "dispatch_state": "COMPLETED",
                        "real_money_action_submitted": False,
                        "acceptance_receipt": None, "settlement_receipt": None,
                        "result": None, "balance_change": None})
        if isinstance(result.get("ui_acknowledgement"), dict):
            attempt["ui_acknowledgement"] = {
                "kind": "DOM_PANEL_STATE", "state": str(result["ui_acknowledgement"].get("state") or "UNKNOWN"),
                "observed_at": str(result["ui_acknowledgement"].get("observed_at") or at),
                "evidence": result["ui_acknowledgement"].get("evidence") or {}}
        attempt.setdefault("transitions", []).append({"state": state, "at": at,
            "reason": attempt["reason"]})
        return self.repository.complete_demo_execution_attempt(attempt, claim_token)

    def observe_game_phase(self, phase: dict[str, Any]) -> dict[str, Any]:
        now = self.now_provider().astimezone(timezone.utc)
        self.latest_game_phase = {**deepcopy(phase), "environment": "DEMO"}
        panels = phase.get("panel_observations") if isinstance(phase.get("panel_observations"), list) else []
        self.repository.record_demo_panel_observations(
            self._active().get("session_id") if self._active() else None, panels)
        with self._lock:
            session = self._active()
            if session:
                session["latest_game_phase"] = deepcopy(self.latest_game_phase)
                session["latest_panel_observations"] = deepcopy(panels)
                session["updated_at"] = now.isoformat()
                self.repository.update_demo_rule_session(session)
                attempts = self.repository.demo_execution_attempts(session["session_id"], 500)
                for attempt in attempts:
                    if attempt.get("state") in {"SELECT_RECEIVED", "WAITING_FOR_WINDOW", "UNKNOWN", "EXECUTION_ELIGIBLE"}:
                        self._refresh_attempt(attempt)
        return self.status()

    def get(self, session_id: str) -> dict[str, Any] | None:
        return deepcopy(self.sessions.get(str(session_id)))

    def status(self) -> dict[str, Any]:
        now = self.now_provider().astimezone(timezone.utc)
        with self._lock:
            for session in list(self.sessions.values()):
                self._expire_if_due(session, now)
            active = self._active()
            latest_phase = deepcopy(active.get("latest_game_phase") if active else self.latest_game_phase)
            session_rows = self.repository.list_demo_rule_sessions(100)
            latest_session = session_rows[0] if session_rows else None
            visible_session = active or latest_session
            visible_session_id = visible_session.get("session_id") if visible_session else None
            attempts = (self.repository.demo_execution_attempts(visible_session_id, 500)
                        if visible_session_id else [])
            decisions = (self.repository.demo_session_decisions(visible_session_id, 500)
                         if visible_session_id else [])
            counts = {
                "select": sum(row.get("action") == "SELECT" for row in decisions),
                "no_select": sum(row.get("action") == "NO_SELECT" for row in decisions),
                "blocked": sum(row.get("action") == "BLOCKED" for row in decisions),
                "bets_attempted": sum(row.get("state") in {"BET_SUBMITTED", "BET_QUEUED", "BET_ACTIVE", "BET_SETTLED"} for row in attempts),
                "bets_accepted": sum(row.get("acceptance_receipt") is not None for row in attempts),
                "wins": sum(row.get("result") == "WIN" and row.get("settlement_receipt") for row in attempts),
                "losses": sum(row.get("result") == "LOSS" and row.get("settlement_receipt") for row in attempts),
            }
            current_decision = (decisions[-1] if decisions else
                                deepcopy((active or {}).get("latest_decision")))
            buffer = self.round_buffer.snapshot()
            return {
                "environment": "DEMO",
                "status": active.get("status") if active else (latest_session or {}).get("status", "INACTIVE"),
                "active": bool(active),
                "active_session": deepcopy(active),
                "latest_session": latest_session,
                "sessions": session_rows,
                "rules_engine_state": "ACTIVE" if active else "INACTIVE",
                "current_select": deepcopy(current_decision),
                "executor_state": attempts[0].get("state") if attempts else "IDLE",
                "execution_attempts": attempts,
                "game_phase": latest_phase.get("round_phase", latest_phase.get("phase", "UNKNOWN")),
                "betting_window": {"state": latest_phase.get("phase", "UNKNOWN"),
                                   "observed_at": latest_phase.get("observed_at"),
                                   "evidence": deepcopy(latest_phase.get("evidence") or {})},
                "target_identity": {"status": (attempts[0].get("target_identity_status", "UNKNOWN")
                                                if attempts else "UNKNOWN"),
                                    "target_round_id": attempts[0].get("target_round_id") if attempts else None},
                "counts": counts,
                "latest_error": (active.get("errors") or [None])[-1] if active else None,
                "live_real_action_submitted": False,
                "history_source": "PROCESS_LOCAL_ROLLING_BUFFER",
                "round_buffer": {key: buffer[key] for key in (
                    "epoch_id", "collector_session_id", "sequence", "count", "capacity",
                    "reset_count", "continuity_after_restart")},
            }
