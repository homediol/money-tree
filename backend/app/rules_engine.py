"""Prospective, shadow-only Rules Engine migrated from aviator-shadow-experiment-node.

The registry freezes source semantics by mode.  Engines own their state; the
coordinator is the only component allowed to create a combined selection.
No code in this module can authorize or submit a real-money action.
"""
from __future__ import annotations

from copy import deepcopy
from collections import deque
from datetime import datetime, timezone
import json
import hashlib
import math
import re
from threading import RLock
import time
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

ENGINE_VERSION = "WINNER_RULES_ENGINE_SHADOW_V1"
STATE_KEY = "rules_engine_state_v1"
WINDOW_SIZE = 50
H_THRESHOLD = 2.0
NET_OUTCOME_LIMIT_REASONS = {"WIN_LIMIT_REACHED", "LOSS_LIMIT_REACHED"}
MODES = {"START_CHECK", "LEGACY_SEPARATE_TRACKER", "FIVE_RULE_TRACKER", "ALL_RULES_TOGETHER"}
COMBINED_POLICY_ID = "USER_PLAYING_POLICY"
START_CHECK_POLICY_ID = "USER_PLAYING_POLICY_START_CHECK"
LEGACY_POLICY_ID = "USER_PLAYING_POLICY_LEGACY_TRACKER"
SPECIAL_SKIP_POLICY_IDS = {START_CHECK_POLICY_ID, COMBINED_POLICY_ID}


def _research_definitions() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": "RECENT_5_H_GE_4", "version": "1.0.0",
            "name": "Recent 5: at least 4 H", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 5,
            "condition": {"window": 5, "outcome": "H", "operator": ">=", "count": 4,
                          "includes_source": True, "latest_source_must_be_h": False},
            "initial_state": {"status": "WARMING_UP", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["Use the latest five consecutive verified completed outcomes including source.",
                                 "TRUE at four or more H; FALSE only when the verified window is ready.",
                                 "Each TRUE requests source round + 1 once; only its own request resolves its state."],
            "reason_rw": "H 4 muri rounds 5: condition yujujwe.",
        },
        {
            "rule_id": "PATTERN_LH", "version": "1.0.0",
            "name": "Pattern LH", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 2,
            "condition": {"pattern": ["L", "H"], "includes_source": True},
            "initial_state": {"status": "WARMING_UP", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["TRUE exactly for the latest verified pair L,H.",
                                 "Each TRUE requests the immediately following target once."],
            "reason_rw": "Rounds ebyiri ziheruka ni L,H.",
        },
        {
            "rule_id": "PATTERN_HH", "version": "1.0.0",
            "name": "Pattern HH", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 2,
            "condition": {"pattern": ["H", "H"], "includes_source": True,
                          "reevaluate_each_source": True, "overlap_allowed": True},
            "initial_state": {"status": "WARMING_UP", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["TRUE exactly for the latest verified pair H,H.",
                                 "Repeated HH pairs may request successive distinct targets."],
            "reason_rw": "Rounds ebyiri ziheruka ni H,H.",
        },
        {
            "rule_id": "SOURCE_H_MULT_2_3", "version": "1.0.0",
            "name": "Source multiplier [2, 3)", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 1,
            "condition": {"minimum_inclusive": 2.0, "maximum_exclusive": 3.0},
            "initial_state": {"status": "WAIT", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["TRUE when the original source multiplier is >=2.00 and <3.00.",
                                 "Each TRUE requests the immediately following target once."],
            "reason_rw": "Igikubo cya source kiri hagati ya 2.00 na 3.00.",
        },
        {
            "rule_id": "AFTER_HH_CONTINUE_MAX_3", "version": "1.0.0",
            "name": "After HH: continue for at most 3 targets", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 2,
            "condition": {"trigger_pattern": ["H", "H"], "maximum_selections": 3,
                          "ignore_triggers_while_active": True, "stop_on_ww_or_ll": False,
                          "rearm_after_third_resolved_target": True},
            "initial_state": {"cycle_mode": "IDLE", "cycle": None, "pending_target": None,
                              "failure_count": 0, "recovery_count": 0},
            "transition_rules": ["Idle verified HH requests target one and starts a private cycle.",
                                 "After each owned target resolves, request the next consecutive target up to three.",
                                 "Ignore HH while active; do not reset or extend the cycle.",
                                 "Complete and rearm from the third resolved target source regardless of result.",
                                 "Do not apply STANDARD first-WIN or WW/LL stopping."],
            "reason_rw": "HH yabonetse; itegeko risaba target ikurikira.",
        },
    ]


RESEARCH_RULES = _research_definitions()
_research_by_id = {row["rule_id"]: row for row in RESEARCH_RULES}

SPECIAL_SKIP_POLICY_RULE = (
    "After H trigger, selected LOSS then WIN enters special skip: skip one source, "
    "bet after L <= 2.00, repeat after WIN, and end the cycle after two special wins "
    "or a skipped H > 2.00. One selected LOSS during the active special skip ends "
    "that policy cycle immediately."
)

START_CHECK_POLICY = {
    "rule_id": START_CHECK_POLICY_ID, "version": "AVIATOR_2X_CONSECUTIVE_STOP_V3",
    "name": "Start a Check: source legacy policy", "mode": "START_CHECK",
    "parameters": {"stake_rwf": 100, "cashout_multiplier": 2.0, "window_size": WINDOW_SIZE,
                    "profit_target_rwf": 400},
    "target_offset": 1,
    "condition": {"standard_trigger": ["L", "H"], "trigger_h_counts_as_previous_result": True,
                  "standard_stop": ["HH", "LL"], "two_bet_wins_activates_after_two_failed_ll_cycles": True,
                  "two_bet_wins_stop": "two consecutive selected H outcomes",
                  "h_loss_win_special_skip": True},
    "initial_state": {"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                      "failed_cycle_count": 0, "consecutive_bet_wins": 0, "pending_target": None},
    "transition_rules": ["WAIT_LOW waits for L; WAIT_HIGH waits for H.",
                          "H triggers source+1; legacy reducer seeds previous outcome H.",
                          "STANDARD stops on HH or LL; LL terminal L satisfies the low condition.",
                          SPECIAL_SKIP_POLICY_RULE,
                          "Two consecutive LL-ending STANDARD cycles activate TWO_BET_WINS.",
                          "TWO_BET_WINS stops after two consecutive selected H outcomes; trigger H is not a bet.",
                          "A +400 profit target can stop new selections while the 50-round window continues observing."],
    "source_semantics_note": "Distinct from All Rules Together; preserves rule-reducer.js behavior.",
}

LEGACY_SINGLE_TRACKER_POLICY = {
    "rule_id": LEGACY_POLICY_ID, "version": "AVIATOR_2X_CONSECUTIVE_STOP_V3",
    "name": "Legacy separate tracker: single rule", "mode": "LEGACY_SEPARATE_TRACKER",
    "parameters": {"stake_rwf": 100, "cashout_multiplier": 2.0, "window_size": WINDOW_SIZE,
                    "profit_target_rwf": 400},
    "target_offset": 1,
    "condition": {**deepcopy(START_CHECK_POLICY["condition"]), "h_loss_win_special_skip": False},
    "initial_state": deepcopy(START_CHECK_POLICY["initial_state"]),
    "transition_rules": [rule for rule in START_CHECK_POLICY["transition_rules"]
                          if rule != SPECIAL_SKIP_POLICY_RULE],
    "source_semantics_note": "Separate state and shadow ledger; source is live-shadow-test.js.",
}

COMBINED_POLICY = {
    "rule_id": COMBINED_POLICY_ID, "version": "AVIATOR_2X_CONSECUTIVE_STOP_V3",
    "name": "Accepted user playing policy", "mode": "ALL_RULES_TOGETHER",
    "parameters": {}, "target_offset": 1,
    "condition": {"standard_trigger": ["L", "H"], "trigger_h_counts_as_selected_result": False,
                  "standard_first_selected_h_stops": True,
                  "after_first_selected_l_stop_on": ["HH", "LL"],
                  "h_loss_win_special_skip": True,
                  "two_bet_wins_activates_after_two_consecutive_standard_ll_cycles": True,
                  "two_bet_wins_trigger_h_counts_as_selected_result": False,
                  "two_bet_wins_continue_until": ["HH", "LL"],
                  "two_bet_wins_ll_keeps_mode": True, "two_bet_wins_hh_clears_mode": True},
    "initial_state": {"rule_state": "WAIT_LOW", "mode": "STANDARD", "failed_cycle_count": 0,
                      "pending_target": None, "selected_result_streak": []},
    "transition_rules": ["WAIT_LOW waits for L; WAIT_HIGH waits for H.",
                          "H proposes source+1 and is not a selected WIN.",
                          "In STANDARD, the first selected H ends the cycle; after first selected L, continue to HH or LL.",
                          SPECIAL_SKIP_POLICY_RULE,
                          "Two consecutive STANDARD LL cycles activate TWO_BET_WINS; a successful STANDARD cycle resets that streak.",
                          "TWO_BET_WINS keeps mode after LL and clears to STANDARD after selected HH.",
                          "Only this policy's own matching combined selection advances selected-result counters."],
    "source_semantics_note": "Accepted All Rules Together contract; nested mode, not a duplicate rule instance.",
}


def _now(value: str | None = None) -> str:
    stamp = value or datetime.now(timezone.utc).isoformat()
    if not isinstance(stamp, str) or not stamp or _parse_time(stamp) is None:
        raise ValueError("evaluated_at must be an ISO timestamp")
    return stamp


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def _multiplier(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 1 and result < float("inf") else None


def classify_outcome(value: Any) -> str | None:
    number = _multiplier(value)
    return None if number is None else ("H" if number >= H_THRESHOLD else "L")


def _identity(event: Mapping[str, Any], *, require_sequence: bool = True) -> dict[str, Any] | None:
    if not isinstance(event, Mapping):
        return None
    multiplier = _multiplier(event.get("multiplier"))
    try:
        index = int(event.get("round_index"))
        sequence = int(event.get("stream_sequence") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    stream_id = event.get("stream_id")
    round_id = event.get("round_id")
    observation_id = event.get("observation_id") or round_id
    if (not isinstance(stream_id, str) or not stream_id.strip()
            or not isinstance(round_id, str) or not round_id.strip()
            or not isinstance(observation_id, str) or not observation_id.strip()
            or index < 1 or (require_sequence and sequence < 1) or multiplier is None):
        return None
    return {
        "stream_id": stream_id, "stream_sequence": sequence, "round_id": round_id,
        "observation_id": observation_id, "round_index": index,
        "platform_round_id": event.get("platform_round_id"),
        "identity_source": event.get("identity_source"), "multiplier": multiplier,
        "outcome": classify_outcome(multiplier), "continuity_status": event.get("continuity_status"),
        "durability": event.get("durability"), "event_type": event.get("event_type"),
        "observed_at": event.get("observed_at"),
        "stored_at": event.get("stored_at") or event.get("commit_acknowledged_at"),
    }


def _reference(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"streamId": row["stream_id"], "roundId": row["round_id"],
            "observationId": row["observation_id"], "roundIndex": row["round_index"],
            "multiplier": row["multiplier"], "outcome": row["outcome"]}


def _expected(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"streamId": row["stream_id"], "roundId": None, "observationId": None,
            "roundIndex": row["round_index"] + 1,
            "expectedImmediateSuccessorOf": _reference(row)}


def _fresh_engine(rule_id: str) -> dict[str, Any]:
    if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
        state = {"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                "failed_cycle_count": 0, "consecutive_bet_wins": 0, "pending_selection_id": None,
                "pending_target": None, "selected_outcomes": [], "failure_count": 0, "recovery_count": 0}
        if rule_id in SPECIAL_SKIP_POLICY_IDS:
            state.update({"special_skip_active": False, "special_skip_after_round_index": None,
                          "special_skip_win_count": 0,
                          "special_skip_terminal_round_index": None})
        return state
    if rule_id == COMBINED_POLICY_ID:
        return {"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                "failed_cycle_count": 0, "consecutive_bet_wins": 0, "pending_selection_id": None,
                "pending_target": None, "selected_outcomes": [], "cycle_number": 0,
                "failure_count": 0, "recovery_count": 0,
                "special_skip_active": False, "special_skip_after_round_index": None,
                "special_skip_win_count": 0, "special_skip_terminal_round_index": None}
    state = {"pending_selection_id": None, "pending_target": None, "failure_count": 0,
             "recovery_count": 0, "selected_wins": 0, "selected_losses": 0}
    if rule_id == "AFTER_HH_CONTINUE_MAX_3":
        state.update({"cycle_mode": "IDLE", "cycle": None})
    return state


def _result(rule_id: str, version: str, session: Mapping[str, Any], source: Mapping[str, Any],
            target: Mapping[str, Any] | None, eligible: bool | None, status: str, reason: str,
            inputs: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any],
            evaluated_at: str) -> dict[str, Any]:
    return {
        "rule_id": rule_id, "rule_version": version,
        "source_round": _reference(source), "target_round": deepcopy(target),
        "evaluated_at": evaluated_at, "result": eligible, "eligible": eligible,
        "status": status, "reason": reason,
        "state_snapshot": {"version": 1, "before": deepcopy(dict(before)), "after": deepcopy(dict(after))},
        "proposal": {"ruleId": rule_id, "ruleVersion": version, "experimentId": session["session_id"],
                     "sourceRoundRef": _reference(source), "expectedTargetRef": deepcopy(target),
                     "eligible": eligible, "status": status, "reason": reason,
                     "evaluatedAt": evaluated_at, "relevantInputs": deepcopy(dict(inputs)),
                     "privateState": {"before": deepcopy(dict(before)), "after": deepcopy(dict(after))}},
    }


def _stable_selection_id(session: Mapping[str, Any], source: Mapping[str, Any],
                         rule_ids: list[str]) -> str:
    """Keep a decision's identity stable when a durable inbox event is replayed."""
    expected = _expected(source)
    identity = "\0".join(("winner-predict-selection-v1",
        str(session.get("session_id") or ""), str(source.get("stream_id") or ""),
        str(source.get("round_id") or ""), str(expected.get("roundIndex") or ""),
        ",".join(str(rule_id) for rule_id in rule_ids)))
    return str(uuid5(NAMESPACE_URL, identity))


def _history_is_contiguous(history: list[dict[str, Any]], count: int, stream_id: str) -> list[dict[str, Any]] | None:
    tail = history[-count:]
    if len(tail) != count:
        return None
    for i, row in enumerate(tail):
        if row.get("continuity_status") != "VERIFIED" or row.get("outcome") not in {"H", "L"}:
            return None
        if row.get("stream_id") != stream_id:
            return None
        if i and row.get("round_index") != tail[i - 1].get("round_index", 0) + 1:
            return None
    return tail


class RulesEngine:
    """Rule catalogue, independent shadow-session states, and shared coordinator."""

    RESTARTABLE_INTEGRITY_STOPS = {
        "ROUND_INDEX_GAP",
        "ROUND_IDENTITY_CONFLICT",
        "OBSERVED_COMMIT_IDENTITY_CONFLICT",
        "STREAM_IDENTITY_CHANGED",
    }

    def __init__(self, persistence: Any | None = None) -> None:
        self._rules: tuple[Any, ...] = ()  # compatibility: no executable real-money rules
        self._lock = RLock()
        self._persistence = persistence
        self.observation_inbox: Any | None = None
        self.last_evaluation: dict[str, Any] | None = None
        self.last_evaluation_at: str | None = None
        self.configuration_version = 0
        self.rule_configuration: dict[str, dict[str, Any]] = {}
        self.presets: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.evaluation_count = 0
        self._persistence_generation = 0
        self._delivery_metrics: dict[str, deque[float]] = {
            "rules_engine_processing_ms": deque(maxlen=2000),
            "target_settlement_processing_ms": deque(maxlen=2000),
            "target_settlement_durable_ms": deque(maxlen=2000),
        }
        self._delivery_metric_ids: dict[str, set[str]] = {key: set() for key in self._delivery_metrics}
        self._delivery_metric_order: dict[str, deque[str]] = {
            key: deque() for key in self._delivery_metrics
        }
        if persistence is not None:
            saved = persistence.load_application_state(STATE_KEY)
            if isinstance(saved, dict) and saved.get("engine_version") == ENGINE_VERSION:
                self._restore(saved)
                prime_ledger_cache = getattr(persistence, "prime_rules_engine_ledger_cache", None)
                if callable(prime_ledger_cache):
                    # The restored application snapshot and its queryable
                    # ledger were committed together for current-format data.
                    # Seed the incremental writer so the first new round does
                    # not replay durable history. Migrate any sessions found
                    # only in a legacy snapshot during startup.
                    if prime_ledger_cache(self._snapshot()):
                        self._persist()
            self.recover_execution_reconciliation()

    @staticmethod
    def not_configured(rule_id: str) -> dict[str, Any]:
        return {"rule_id": str(rule_id), "enabled": True, "evaluated_round": None,
                "result": False, "reason": "NOT_CONFIGURED"}

    @staticmethod
    def aggregate(evaluations: Any) -> dict[str, Any]:
        rows = list(evaluations or ())
        # Compatibility for the retired, target-less preview API. This returns
        # only a preview label; it cannot create a prospective selection. The
        # migrated session coordinator below accepts only explicit target refs.
        if rows and all(isinstance(item, Mapping) and "eligible" not in item for item in rows):
            if not all("evaluated_round" in item for item in rows):
                return RulesEngine._no_action(rows, "INVALID_RULE_EVALUATION")
            seen_legacy: set[str] = set()
            true_legacy: list[str] = []
            for item in rows:
                rule_id = item.get("rule_id")
                result = item.get("result")
                if isinstance(rule_id, str) and rule_id in seen_legacy:
                    return RulesEngine._no_action(rows, "INVALID_RULE_ID")
                if (not isinstance(rule_id, str) or not rule_id.strip()
                        or not isinstance(item.get("enabled", True), bool)
                        or not isinstance(result, bool) or not isinstance(item.get("reason"), str)):
                    return RulesEngine._no_action(rows, "INVALID_RULE_EVALUATION")
                seen_legacy.add(rule_id)
                if item.get("enabled", True) and result:
                    true_legacy.append(rule_id)
            enabled_legacy = sum(item.get("enabled", True) is True for item in rows)
            return {"result": bool(true_legacy), "true_rule_ids": true_legacy,
                    "rule_evaluations": deepcopy(rows),
                    "decision": "RULE_MATCH" if true_legacy else "NO_ACTION",
                    "reason": "RULE_MATCH" if true_legacy else "NO_RULE_MATCHED",
                    "rules_configured": len(rows), "rules_enabled": enabled_legacy,
                    "automatic_execution": "OFF", "can_bet": False,
                    "preview_only": True, "selection_created": False}
        seen: set[str] = set()
        enabled: list[dict[str, Any]] = []
        refs: set[str] = set()
        for item in rows:
            if not isinstance(item, Mapping):
                return RulesEngine._no_action(rows, "INVALID_RULE_EVALUATION")
            rule_id = item.get("rule_id")
            if not isinstance(rule_id, str) or not rule_id.strip() or rule_id in seen:
                return RulesEngine._no_action(rows, "INVALID_RULE_ID")
            seen.add(rule_id)
            eligible = item.get("eligible", item.get("result"))
            if (not isinstance(item.get("enabled", True), bool)
                    or (eligible is not None and not isinstance(eligible, bool))
                    or not isinstance(item.get("reason"), str)):
                return RulesEngine._no_action(rows, "INVALID_RULE_EVALUATION")
            if item.get("enabled", True) and eligible is True:
                target = item.get("expectedTargetRef", item.get("target_round"))
                if not isinstance(target, Mapping):
                    return RulesEngine._no_action(rows, "TRUE_WITHOUT_TARGET")
                stream_ref = target.get("streamId", target.get("stream_id"))
                round_ref = target.get("roundIndex", target.get("round_index"))
                if (not isinstance(stream_ref, str) or not stream_ref.strip()
                        or isinstance(round_ref, bool) or not isinstance(round_ref, int) or round_ref < 1):
                    return RulesEngine._no_action(rows, "INVALID_TARGET_IDENTITY")
                refs.add(json.dumps(target, sort_keys=True, separators=(",", ":"), default=str))
                enabled.append(dict(item))
        if len(refs) > 1:
            return RulesEngine._no_action(rows, "TARGET_MISMATCH")
        true_ids = [item["rule_id"] for item in enabled]
        true_ids.extend(item.get("ruleId") for item in rows
                        if isinstance(item, Mapping) and item.get("enabled", True) is True
                        and item.get("eligible", item.get("result")) is True
                        and item.get("ruleId") and item.get("ruleId") not in true_ids)
        n_enabled = sum(1 for item in rows if isinstance(item, Mapping) and item.get("enabled", True) is True)
        decision = "SELECT" if true_ids else ("WAIT" if n_enabled else "NO_ACTION")
        return {"result": bool(true_ids), "true_rule_ids": true_ids, "rule_evaluations": deepcopy(rows),
                "decision": decision,
                "reason": "RULE_MATCHED" if true_ids else ("NO_RULE_MATCHED" if n_enabled else "NOT_CONFIGURED"),
                "rules_configured": len(rows), "rules_enabled": n_enabled,
                "automatic_execution": "OFF", "can_bet": False}

    @staticmethod
    def _no_action(rows: Any, reason: str) -> dict[str, Any]:
        values = list(rows)
        return {"result": False, "true_rule_ids": [], "rule_evaluations": deepcopy(values),
                "decision": "BLOCKED" if reason in {"TARGET_MISMATCH", "TRUE_WITHOUT_TARGET"} else "NO_ACTION",
                "reason": reason, "rules_configured": len(values), "rules_enabled": 0,
                "automatic_execution": "OFF", "can_bet": False}

    def _snapshot(self, *, session_ids: set[str] | None = None) -> dict[str, Any]:
        sessions = (self.sessions if session_ids is None else
                    {session_id: self.sessions[session_id] for session_id in session_ids
                     if session_id in self.sessions})
        return {"engine_version": ENGINE_VERSION, "_rules_engine_generation": self._persistence_generation,
                "configuration_version": self.configuration_version,
                "rule_configuration": deepcopy(self.rule_configuration), "presets": deepcopy(self.presets),
                # Persistence only reads this snapshot synchronously. _persist
                # holds the engine lock through serialization, so copying every
                # nested round/evaluation record here only adds a second full
                # traversal of the session payload without improving safety.
                "sessions": dict(sessions), "last_evaluation": deepcopy(self.last_evaluation),
                "last_evaluation_at": self.last_evaluation_at, "evaluation_count": self.evaluation_count}

    def persist_current_snapshot(self, *, session_ids: set[str] | None = None) -> dict[str, float]:
        """Capture state under the rules lock, then persist without holding it.

        The collector observation inbox is the durable event log. A caller may
        therefore let the ordered Rules Engine continue after capturing this
        immutable snapshot; it must acknowledge the covered inbox events only
        after this method reports a successful database commit.
        """
        if self._persistence is None:
            return {"snapshot_serialization_ms": 0.0, "database_persistence_ms": 0.0,
                    "persistence_total_ms": 0.0}
        started_ns = time.perf_counter_ns()
        with self._lock:
            snapshot_started_ns = time.perf_counter_ns()
            snapshot = deepcopy(self._snapshot(session_ids=session_ids))
            snapshot_finished_ns = time.perf_counter_ns()
        persist = getattr(self._persistence, "save_rules_engine_snapshot", None)
        if callable(persist):
            persist(snapshot, _now())
        else:
            self._persistence.save_application_state(STATE_KEY, snapshot, _now())
        finished_ns = time.perf_counter_ns()
        return {
            "snapshot_serialization_ms": round(
                (snapshot_finished_ns - snapshot_started_ns) / 1_000_000.0, 3),
            "database_persistence_ms": round(
                (finished_ns - snapshot_finished_ns) / 1_000_000.0, 3),
            "persistence_total_ms": round((finished_ns - started_ns) / 1_000_000.0, 3),
        }

    def _restore(self, state: dict[str, Any]) -> None:
        self._persistence_generation = int(state.get("_rules_engine_generation") or 0)
        self.configuration_version = int(state.get("configuration_version") or 0)
        original_rule_configuration = deepcopy(state.get("rule_configuration") or {})
        original_presets = deepcopy(state.get("presets") or {})
        original_sessions = deepcopy(state.get("sessions") or {})
        self.rule_configuration = deepcopy(state.get("rule_configuration") or {})
        self.presets = deepcopy(state.get("presets") or {})
        self.sessions = deepcopy(state.get("sessions") or {})
        # Old snapshots may contain controls from the removed loss-limit
        # subsystem. Keep result history but discard those controls on load.
        removed_controls = {
            "loss_limit_rwf",
            "reconciled_loss_count_limit_enabled", "reconciled_loss_count_limit",
            "reconciled_loss_limit_enabled", "reconciled_loss_limit_rwf",
            "reconciled_profit_target_enabled", "reconciled_profit_target_rwf",
            "reconciled_win_limit_enabled", "reconciled_win_limit_count",
        }
        for row in self.rule_configuration.values():
            params = row.get("parameters") if isinstance(row, dict) else None
            if isinstance(params, dict):
                for key in removed_controls:
                    params.pop(key, None)
        for preset in self.presets.values():
            options = preset.get("options") if isinstance(preset, dict) else None
            if isinstance(options, dict):
                if options.get("net_outcome_limit_version") != 1:
                    options.pop("loss_limit_enabled", None)
                    options.pop("loss_limit_count", None)
                for key in removed_controls:
                    options.pop(key, None)
        for session in self.sessions.values():
            config = session.get("configuration_snapshot")
            if isinstance(config, dict):
                if config.get("net_outcome_limit_version") != 1:
                    config.pop("loss_limit_enabled", None)
                    config.pop("loss_limit_count", None)
                for key in removed_controls:
                    config.pop(key, None)
            session.pop("financial_stop", None)
            self._normalize_legacy_limit_stop(session)
            self._ensure_legacy_stop_provenance(session)
            self._recover_auto_next_boundary(session)
            if self._effective_session_status(session) == "COMPLETED":
                # Older persisted snapshots can retain SETTLING after their
                # full verified observation window was already consumed.
                session["status"] = "COMPLETED"
            if session.get("status") == "RUNNING":
                if session.get("stop_reason"):
                    # A persisted session-level stop is fail-closed if an old
                    # snapshot also claimed it was still RUNNING.
                    session["status"] = "PAUSED"
                elif (session.get("selection_status") == "STOPPED"
                      and session.get("selection_stop_reason")):
                    session["status"] = "SETTLING"
        self.last_evaluation = deepcopy(state.get("last_evaluation"))
        self.last_evaluation_at = state.get("last_evaluation_at")
        self.evaluation_count = int(state.get("evaluation_count") or 0)
        if (self._persistence is not None
                and (self.rule_configuration != original_rule_configuration
                     or self.presets != original_presets
                     or self.sessions != original_sessions)):
            # Persist stop-state normalization without deleting any historical
            # stop events, so stale reasons do not return after the next boot.
            self._persist()

    def _persist(self, *, session_ids: set[str] | None = None) -> dict[str, float]:
        if self._persistence is not None:
            started_ns = time.perf_counter_ns()
            with self._lock:
                snapshot_started_ns = time.perf_counter_ns()
                self._persistence_generation += 1
                snapshot = self._snapshot(session_ids=session_ids)
                snapshot_finished_ns = time.perf_counter_ns()
                persist = getattr(self._persistence, "save_rules_engine_snapshot", None)
                if callable(persist):
                    persist(snapshot, _now())
                else:
                    # Legacy persistence adapters have no session-level upsert;
                    # they must keep receiving the complete snapshot.
                    fallback_snapshot = self._snapshot() if session_ids is not None else snapshot
                    self._persistence.save_application_state(STATE_KEY, fallback_snapshot, _now())
                finished_ns = time.perf_counter_ns()
                return {
                    "snapshot_serialization_ms": round(
                        (snapshot_finished_ns - snapshot_started_ns) / 1_000_000.0, 3),
                    "database_persistence_ms": round(
                        (finished_ns - snapshot_finished_ns) / 1_000_000.0, 3),
                    "persistence_total_ms": round(
                        (finished_ns - started_ns) / 1_000_000.0, 3),
                }
        return {"snapshot_serialization_ms": 0.0,
                "database_persistence_ms": 0.0, "persistence_total_ms": 0.0}

    def _record_delivery_metric(self, stage: str, event_id: str, value_ms: float) -> None:
        values = self._delivery_metrics.get(stage)
        if values is None or not event_id:
            return
        seen = self._delivery_metric_ids[stage]
        order = self._delivery_metric_order[stage]
        if event_id in seen:
            return
        seen.add(event_id)
        order.append(event_id)
        if len(order) > 10000:
            seen.discard(order.popleft())
        if math.isfinite(value_ms) and value_ms >= 0:
            values.append(float(value_ms))

    @staticmethod
    def _latency_summary(values: deque[float]) -> dict[str, Any]:
        ordered = sorted(values)
        if not ordered:
            return {"samples": 0, "average_ms": None, "p50_ms": None,
                    "p95_ms": None, "p99_ms": None, "max_ms": None}

        def percentile(probability: float) -> float:
            index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
            return round(ordered[index], 3)

        return {"samples": len(ordered), "average_ms": round(sum(ordered) / len(ordered), 3),
                "p50_ms": percentile(0.50),
                "p95_ms": percentile(0.95), "p99_ms": percentile(0.99),
                "max_ms": round(ordered[-1], 3)}

    def _apply_reconciled_financials(self, session: dict[str, Any], execution: Mapping[str, Any],
                                    financials: Mapping[str, Any], at: str) -> None:
        execution_id = str(execution.get("execution_id") or "")
        reconciled = session.setdefault("reconciled_financials", {
            "reconciled_execution_ids": [], "realized_betting_pl": None,
            "currency": None, "currency_status": None, "win_count": 0,
            "loss_count": 0, "reconciled_bets": 0,
        })
        ids = list(reconciled.get("reconciled_execution_ids") or [])
        if execution_id and execution_id not in ids:
            ids.append(execution_id)
        reconciled.update({"reconciled_execution_ids": ids,
                           "realized_betting_pl": financials.get("realized_betting_pl"),
                           "currency": financials.get("currency"),
                           "currency_status": financials.get("currency_status"),
                           "win_count": int(financials.get("win_count") or 0),
                           "loss_count": int(financials.get("loss_count") or 0),
                           "reconciled_bets": int(financials.get("reconciled_bets") or 0),
                           "updated_at": at})
        selection_id = str(execution.get("decision_id") or "")
        selection = next((item for item in session.get("selections", [])
                          if str(item.get("selection_id")) == selection_id), None)
        if selection is not None:
            selection["financial_result"] = {
                "status": "RECONCILED", "result": execution.get("result"),
                "execution_id": execution_id, "profit_loss": execution.get("profit_loss"),
                "currency": reconciled.get("currency"), "reconciled_at": at,
            }

    def record_financial_reconciliation(self, execution: Mapping[str, Any],
                                        reconciliation: Mapping[str, Any], *,
                                        persist: bool = True) -> dict[str, Any]:
        """Apply one receipt-backed real transaction to its shared rule session."""
        if self._persistence is None or execution.get("simulated") is True:
            return {"ok": False, "reason": "real_rule_session_reconciliation_unavailable"}
        record = getattr(self._persistence, "record_rule_execution_reconciliation", None)
        if not callable(record):
            return {"ok": False, "reason": "rule_reconciliation_store_unavailable"}
        saved = record(dict(execution), dict(reconciliation))
        if not saved.get("ok"):
            return saved
        session_id = str(saved.get("session_id") or "")
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                return {"ok": False, "reason": "rule_session_not_restored", "session_id": session_id}
            self._apply_reconciled_financials(
                session, execution, saved.get("financials") or {},
                str(reconciliation.get("created_at") or execution.get("resolved_at") or _now()),
            )
            if persist:
                self._persist(session_ids={session_id})
            financial_snapshot = deepcopy(session.get("reconciled_financials"))
            result = {"ok": True, "duplicate": bool(saved.get("duplicate")),
                      "session_id": session_id, "financials": financial_snapshot}
        return result

    def block_unknown_execution_reconciliation(self, execution: Mapping[str, Any],
                                               reason: str) -> dict[str, Any]:
        """Pause one rule session for an unresolved transaction without counting it."""
        if self._persistence is None:
            return {"ok": False, "reason": "rule_reconciliation_store_unavailable"}
        mark_pending = getattr(self._persistence, "mark_rule_execution_pending", None)
        if not callable(mark_pending):
            return {"ok": False, "reason": "rule_reconciliation_store_unavailable"}
        intent = mark_pending(dict(execution), reason)
        if not intent:
            return {"ok": False, "reason": "rule_execution_intent_not_found"}
        session_id = str(intent.get("session_id") or "")
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                return {"ok": False, "reason": "rule_session_not_restored", "session_id": session_id}
            session.update({"status": "PAUSED", "selection_status": "STOPPED",
                            "unresolved_execution_id": execution.get("execution_id"),
                            "unresolved_execution_reason": reason})
            self._record_session_stop(session, "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION", _now(),
                "EXECUTION_RECONCILIATION", stop_selection=True,
                details={"execution_id": execution.get("execution_id"), "reason": reason})
            self._persist(session_ids={session_id})
            return {"ok": True, "session_id": session_id, "reason": reason}

    def recover_execution_reconciliation(self) -> None:
        """Restore receipt totals and unresolved-execution pauses after restart."""
        if self._persistence is None:
            return
        list_reconciled = getattr(self._persistence, "list_reconciled_rule_executions", None)
        open_executions = getattr(self._persistence, "open_executions", None)
        get_intent = getattr(self._persistence, "rule_execution_intent", None)
        if not callable(list_reconciled):
            return
        changed = False
        for session_id in list(self.sessions):
            for execution in list_reconciled(session_id):
                result = execution.get("result")
                event = {"status": "RECONCILED", "outcome": result,
                         "round_id": execution.get("target_round_id"),
                         "created_at": execution.get("resolved_at") or _now()}
                applied = self.record_financial_reconciliation(execution, event, persist=False)
                changed = changed or applied.get("ok") is True
        if callable(open_executions) and callable(get_intent):
            for execution in open_executions():
                intent = get_intent(str(execution.get("decision_id") or ""))
                if intent and str(intent.get("session_id") or "") in self.sessions:
                    blocked = self.block_unknown_execution_reconciliation(
                        execution, str(execution.get("error") or "restart_reconciliation_required"))
                    changed = changed or blocked.get("ok") is True
        if changed:
            self._persist()

    def registry(self) -> dict[str, Any]:
        rows = [deepcopy(START_CHECK_POLICY), deepcopy(LEGACY_SINGLE_TRACKER_POLICY),
                *deepcopy(RESEARCH_RULES), deepcopy(COMBINED_POLICY)]
        for row in rows:
            config = self.rule_configuration.get(row["rule_id"], {})
            row["configured"] = config.get("configured", False)
            row["enabled"] = config.get("enabled", False)
            row["configuration"] = deepcopy(config.get("parameters", row.get("parameters", {})))
        return {"engine_version": ENGINE_VERSION, "configuration_version": self.configuration_version,
                "rules": rows, "modes": [
                    {"id": "START_CHECK", "source_section": "Run Controls — Start a Check",
                     "rule_ids": [START_CHECK_POLICY_ID], "window_size": WINDOW_SIZE,
                     "stake_rwf": 100, "stop_semantics": "NET ±400 RWF"},
                    {"id": "LEGACY_SEPARATE_TRACKER", "source_section": "Legacy — Separate Tracker",
                     "rule_ids": [LEGACY_POLICY_ID], "window_size": WINDOW_SIZE,
                     "stop_semantics": "50 verified rounds; one isolated legacy policy state"},
                    {"id": "FIVE_RULE_TRACKER", "source_section": "Five independent rules",
                     "rule_ids": [row["rule_id"] for row in RESEARCH_RULES], "window_size": WINDOW_SIZE,
                     "stop_semantics": "50 verified rounds; independent evaluations feed one shared selection per target"},
                     {"id": "ALL_RULES_TOGETHER", "source_section": "Test — All Rules Together",
                     "rule_ids": [row["rule_id"] for row in RESEARCH_RULES] + [COMBINED_POLICY_ID],
                     "window_size": WINDOW_SIZE, "stop_semantics": "one shared target-bound selection"},
                ]}

    def configure(self, rule_id: str, *, enabled: bool, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        known = {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID, *_research_by_id}
        if rule_id not in known or not isinstance(enabled, bool):
            raise ValueError("INVALID_RULE_CONFIGURATION")
        allowed = {} if rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else {
            "profit_target_enabled", "profit_target_rwf", "window_size",
            "auto_next_check_enabled", "auto_next_on_limit_enabled",
        }
        values = dict(parameters or {})
        if set(values) - set(allowed):
            raise ValueError("UNSUPPORTED_RULE_PARAMETER")
        if rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} and values:
            raise ValueError("RULE_HAS_NO_CONFIGURABLE_PARAMETERS")
        defaults = {"profit_target_enabled": True, "profit_target_rwf": 400,
                    "window_size": WINDOW_SIZE, "auto_next_check_enabled": False,
                    "auto_next_on_limit_enabled": False}
        if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            defaults.update(values)
            for key in ("profit_target_enabled", "auto_next_check_enabled",
                        "auto_next_on_limit_enabled"):
                if not isinstance(defaults[key], bool):
                    raise ValueError("INVALID_BOOLEAN_PARAMETER")
            if defaults["auto_next_check_enabled"] and defaults["auto_next_on_limit_enabled"]:
                raise ValueError("AUTO_NEXT_TRIGGERS_MUTUALLY_EXCLUSIVE")
            for enabled_key, amount_key in (("profit_target_enabled", "profit_target_rwf"),):
                if defaults[enabled_key] and (isinstance(defaults[amount_key], bool)
                    or not isinstance(defaults[amount_key], int) or defaults[amount_key] <= 0):
                    raise ValueError("INVALID_NET_LIMIT")
            if (isinstance(defaults["window_size"], bool) or not isinstance(defaults["window_size"], int)
                    or not 1 <= defaults["window_size"] <= 100000):
                raise ValueError("INVALID_WINDOW_SIZE")
        with self._lock:
            self.configuration_version += 1
            self.rule_configuration[rule_id] = {"configured": True, "enabled": enabled,
                "version": self.configuration_version,
                "parameters": defaults if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else {}}
            self._persist(session_ids=set())
            return deepcopy(self.rule_configuration[rule_id])

    def save_preset(self, name: str, mode: str, rule_ids: list[str], options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or mode not in MODES:
            raise ValueError("INVALID_PRESET")
        valid = self._allowed_rules(mode)
        if (not isinstance(rule_ids, list) or len(set(rule_ids)) != len(rule_ids)
                or any(rule_id not in valid for rule_id in rule_ids)):
            raise ValueError("INVALID_PRESET_RULES")
        preset_options = deepcopy(dict(options or {}))
        if mode in {"START_CHECK", "LEGACY_SEPARATE_TRACKER", "ALL_RULES_TOGETHER"}:
            preset_options.setdefault("auto_next_check_enabled", False)
            preset_options.setdefault("auto_next_on_limit_enabled", False)
            if (not isinstance(preset_options["auto_next_check_enabled"], bool)
                    or not isinstance(preset_options["auto_next_on_limit_enabled"], bool)):
                raise ValueError("INVALID_BOOLEAN_PARAMETER")
            if (preset_options["auto_next_check_enabled"]
                    and preset_options["auto_next_on_limit_enabled"]):
                raise ValueError("AUTO_NEXT_TRIGGERS_MUTUALLY_EXCLUSIVE")
        else:
            preset_options.pop("auto_next_on_limit_enabled", None)
        for key in (
            "loss_limit_rwf",
            "reconciled_loss_count_limit_enabled", "reconciled_loss_count_limit",
            "reconciled_loss_limit_enabled", "reconciled_loss_limit_rwf",
            "reconciled_profit_target_enabled", "reconciled_profit_target_rwf",
            "reconciled_win_limit_enabled", "reconciled_win_limit_count",
        ):
            preset_options.pop(key, None)
        if mode == "ALL_RULES_TOGETHER":
            self._validate_net_outcome_limits(preset_options)
            preset_options["net_outcome_limit_version"] = 1
        else:
            preset_options.pop("loss_limit_enabled", None)
            preset_options.pop("loss_limit_count", None)
        value = {"name": name.strip(), "mode": mode, "rule_ids": list(rule_ids),
                 "options": preset_options, "configuration_version": self.configuration_version,
                 "saved_at": _now()}
        with self._lock:
            self.presets[value["name"]] = value
            self._persist(session_ids=set())
        return deepcopy(value)

    @staticmethod
    def _allowed_rules(mode: str) -> set[str]:
        if mode == "START_CHECK":
            return {START_CHECK_POLICY_ID}
        if mode == "LEGACY_SEPARATE_TRACKER":
            return {LEGACY_POLICY_ID}
        if mode == "FIVE_RULE_TRACKER":
            return set(_research_by_id)
        if mode == "ALL_RULES_TOGETHER":
            return {*_research_by_id, COMBINED_POLICY_ID}
        return set()

    @staticmethod
    def _validate_net_outcome_limits(options: dict[str, Any]) -> None:
        for enabled_key, count_key in (
            ("win_limit_enabled", "win_limit_count"),
            ("loss_limit_enabled", "loss_limit_count"),
        ):
            options.setdefault(enabled_key, False)
            if not isinstance(options[enabled_key], bool):
                raise ValueError("INVALID_BOOLEAN_PARAMETER")
            if options[enabled_key]:
                count = options.get(count_key)
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise ValueError("INVALID_RESULT_COUNT_LIMIT")
            else:
                options[count_key] = None

    def start_session(self, *, mode: str, rule_ids: list[str], boundary: Mapping[str, Any],
                      prestart_context: list[Mapping[str, Any]] | None = None,
                      options: Mapping[str, Any] | None = None, now: str | None = None,
                      replace_session_id: str | None = None,
                      reconfigure_session_id: str | None = None) -> dict[str, Any]:
        now = _now(now)
        if replace_session_id is not None and reconfigure_session_id is not None:
            raise ValueError("INVALID_SESSION_REPLACEMENT")
        if mode not in MODES or not isinstance(rule_ids, list) or len(set(rule_ids)) != len(rule_ids):
            raise ValueError("INVALID_SESSION_CONFIGURATION")
        allowed = self._allowed_rules(mode)
        if any(rule_id not in allowed for rule_id in rule_ids):
            raise ValueError("RULE_NOT_ALLOWED_IN_MODE")
        for rule_id in rule_ids:
            configured = self.rule_configuration.get(rule_id)
            if not configured or configured.get("configured") is not True or configured.get("enabled") is not True:
                raise ValueError("RULE_NOT_ENABLED")
        if not rule_ids:
            # A zero-rule session is legal and reports NO_ACTION; it never selects.
            pass
        start = _identity(boundary)
        if not start or boundary.get("event_type") != "round.persisted" or boundary.get("durability") != "COMMITTED" \
                or boundary.get("continuity_status") != "VERIFIED":
            raise ValueError("VERIFIED_COMMITTED_BOUNDARY_REQUIRED")
        opts = dict(options or {})
        window_size = opts.get("window_size", WINDOW_SIZE)
        if mode in {"START_CHECK", "LEGACY_SEPARATE_TRACKER"}:
            expected_policy = START_CHECK_POLICY_ID if mode == "START_CHECK" else LEGACY_POLICY_ID
            if rule_ids not in ([], [expected_policy]):
                raise ValueError("SINGLE_POLICY_MODE_ACCEPTS_ONLY_ITS_POLICY")
            configured = self.rule_configuration.get(expected_policy, {}).get("parameters", {})
            defaults = {"window_size": WINDOW_SIZE, "stake_rwf": 100, "cashout_multiplier": 2.0,
                        "profit_target_enabled": True, "profit_target_rwf": 400,
                        "auto_next_check_enabled": False, "auto_next_on_limit_enabled": False,
                        "execution_panel": None,
                        "stake_currency": "RWF"}
            defaults.update(configured)
            defaults.update(opts)
            opts = defaults
            opts["fixed_stake_rwf"] = opts.get("fixed_stake_rwf", opts["stake_rwf"])
            if opts["cashout_multiplier"] != 2.0:
                raise ValueError("START_CHECK_CASHOUT_MUST_BE_2_00")
            for key in ("profit_target_enabled", "auto_next_check_enabled",
                        "auto_next_on_limit_enabled"):
                if not isinstance(opts.get(key), bool):
                    raise ValueError("INVALID_BOOLEAN_PARAMETER")
            if opts["profit_target_enabled"] and (isinstance(opts["profit_target_rwf"], bool)
                    or not isinstance(opts["profit_target_rwf"], int) or opts["profit_target_rwf"] <= 0):
                raise ValueError("INVALID_NET_LIMIT")
        elif mode == "ALL_RULES_TOGETHER":
            if "fixed_stake_rwf" not in opts:
                opts["fixed_stake_rwf"] = 100
            opts.setdefault("stake_currency", "RWF")
            if isinstance(opts["fixed_stake_rwf"], bool) or not isinstance(opts["fixed_stake_rwf"], int) or opts["fixed_stake_rwf"] < 1:
                raise ValueError("INVALID_FIXED_STAKE")
            if opts["stake_currency"] != "RWF":
                raise ValueError("FIXED_STAKE_RWF_CURRENCY_MUST_BE_RWF")
            for key in ("win_limit_enabled", "loss_limit_enabled", "auto_next_check_enabled",
                        "auto_next_on_limit_enabled"):
                opts.setdefault(key, False)
                if not isinstance(opts[key], bool):
                    raise ValueError("INVALID_BOOLEAN_PARAMETER")
            max_selections = opts.get("max_selections")
            if max_selections is not None and (isinstance(max_selections, bool)
                    or not isinstance(max_selections, int) or max_selections < 1):
                raise ValueError("INVALID_MAX_SELECTIONS")
            self._validate_net_outcome_limits(opts)
            opts["net_outcome_limit_version"] = 1
        else:
            retained = dict(opts)
            opts = {"window_size": retained.get("max_observed_rounds", window_size),
                    "execution_panel": retained.get("execution_panel", 0),
                    "stake_currency": retained.get("stake_currency", "RWF")}
            opts["fixed_stake_rwf"] = retained.get("fixed_stake_rwf", 100)
        if (opts.get("auto_next_check_enabled") is True
                and opts.get("auto_next_on_limit_enabled") is True):
            raise ValueError("AUTO_NEXT_TRIGGERS_MUTUALLY_EXCLUSIVE")
        execution_panel = opts.get("execution_panel")
        if execution_panel is not None and (isinstance(execution_panel, bool) or execution_panel not in (0, 1)):
            raise ValueError("INVALID_EXECUTION_PANEL")
        opts["execution_panel"] = execution_panel
        if "stake_currency" in opts and opts["stake_currency"] is not None:
            currency = str(opts["stake_currency"]).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("INVALID_STAKE_CURRENCY")
            opts["stake_currency"] = currency
        if (("fixed_stake_rwf" in opts or "stake_rwf" in opts)
                and opts.get("stake_currency") != "RWF"):
            raise ValueError("RWF_STAKE_FIELD_CURRENCY_MUST_BE_RWF")
        # Retain the fixed frontend stake in each rule mode. Removed loss-limit
        # settings from older preset payloads are deliberately ignored.
        stake = opts.get("fixed_stake_rwf", opts.get("stake_rwf", 100))
        if isinstance(stake, bool) or not isinstance(stake, int) or stake < 1:
            raise ValueError("INVALID_FIXED_STAKE")
        opts["fixed_stake_rwf"] = stake
        opts.pop("stake_rwf", None)
        for key in ("loss_limit_rwf",
                    "reconciled_loss_count_limit_enabled", "reconciled_loss_count_limit",
                    "reconciled_loss_limit_enabled", "reconciled_loss_limit_rwf",
            "reconciled_profit_target_enabled", "reconciled_profit_target_rwf",
            "reconciled_win_limit_enabled", "reconciled_win_limit_count"):
            opts.pop(key, None)
        if mode != "ALL_RULES_TOGETHER":
            opts.pop("loss_limit_enabled", None)
            opts.pop("loss_limit_count", None)
        window_size = opts.get("max_observed_rounds", opts.get("window_size", WINDOW_SIZE))
        if isinstance(window_size, bool) or not isinstance(window_size, int) or not 1 <= window_size <= 100000:
            raise ValueError("INVALID_WINDOW_SIZE")
        context: list[dict[str, Any]] = []
        for item in prestart_context or []:
            row = _identity(item, require_sequence=False)
            if (row and item.get("continuity_status") == "VERIFIED" and row["stream_id"] == start["stream_id"]):
                if context and row["round_index"] != context[-1]["round_index"] + 1:
                    context = []
                context.append(row)
        context = context[-5:]
        synchronization_point = context[-1] if context and context[-1]["round_index"] > start["round_index"] else start
        session_id = str(uuid4())
        engines = {rule_id: _fresh_engine(rule_id) for rule_id in rule_ids}
        snapshot_version = max(1, self.configuration_version)
        session = {
            "session_id": session_id, "mode": mode, "status": "RUNNING", "selection_status": "ACTIVE",
            "started_at": now, "ended_at": None, "window_size": window_size,
            "configuration_version": snapshot_version, "configuration_snapshot": deepcopy(opts),
            "rule_ids": list(rule_ids), "rule_versions": {rid: self._version(rid) for rid in rule_ids},
            "manifest": self._manifest(rule_ids),
            "start_boundary": _reference(start) | {"streamSequence": start["stream_sequence"]},
            "synchronization_point": _reference(synchronization_point),
            "last_stream_id": start["stream_id"], "last_stream_sequence": start["stream_sequence"],
            "last_round_index": synchronization_point["round_index"],
            "last_round_id": synchronization_point["round_id"],
            "context": context, "engines": engines, "observed_rounds": 0,
            "prestart_skipped_rounds": 0,
            "selections": [], "rule_selections": {rid: [] for rid in rule_ids},
            "evaluations": [], "decisions": [], "total_wins": 0, "total_losses": 0,
            "net_profit_rwf": 0, "selection_stop_reason": None, "stop_reason": None,
            "reconciled_financials": {"realized_betting_pl": None, "currency": None,
                                       "win_count": 0, "loss_count": 0,
                                       "reconciled_bets": 0, "reconciled_execution_ids": []},
            "auto_next_armed": False, "auto_next_trigger": None,
            "auto_next_armed_at": None, "auto_next_block_reason": None,
            "auto_next_boundary": None, "auto_next_context": [],
            "auto_next_start_round_id": None,
            "automatic_execution": "OFF",
        }
        with self._lock:
            dirty_session_ids = {session_id}
            if reconfigure_session_id is not None:
                reconfiguring = self.sessions.get(str(reconfigure_session_id))
                if (reconfiguring is None
                        or reconfiguring.get("status") not in {"RUNNING", "SETTLING", "PAUSED"}):
                    raise ValueError("RULES_SESSION_NOT_ACTIVE")
            conflicts = [existing for existing in self.sessions.values()
                         if existing.get("mode") == mode
                         and (reconfigure_session_id is None
                              or existing.get("session_id") != str(reconfigure_session_id))
                         and existing.get("last_stream_id") == start["stream_id"]
                         and existing.get("status") in {"RUNNING", "SETTLING", "PAUSED"}
                         and (existing.get("selection_status") == "ACTIVE"
                              or existing.get("status") == "SETTLING")]
            replaceable = None
            if replace_session_id is not None:
                replaceable = self.sessions.get(str(replace_session_id))
                if (replaceable is None
                        or replaceable.get("mode") != mode
                        or replaceable.get("status") != "PAUSED"
                        or replaceable.get("selection_status") != "ACTIVE"
                        or replaceable.get("stop_reason") not in self.RESTARTABLE_INTEGRITY_STOPS):
                    raise ValueError("SESSION_NOT_RESTARTABLE")
                # Integrity pauses can span collector stream changes, and old
                # integrity-paused sessions can coexist after repeated gaps.
                # They are inert while PAUSED, so only a live run or a manual
                # pause on the current stream should prevent this explicit
                # replacement. Keep all prior outcomes attached to their
                # original session when archiving the selected one.
                unresolved_conflicts = [existing for existing in self.sessions.values()
                    if existing is not replaceable
                    and existing.get("mode") == mode
                    and (existing.get("status") in {"RUNNING", "SETTLING"}
                         or (existing.get("last_stream_id") == start["stream_id"]
                             and existing.get("status") == "PAUSED"
                             and existing.get("selection_status") == "ACTIVE"
                             and existing.get("stop_reason") not in self.RESTARTABLE_INTEGRITY_STOPS))]
                if unresolved_conflicts:
                    raise ValueError("SESSION_ALREADY_ACTIVE_FOR_MODE")
                selections = list(replaceable.get("selections") or [])
                selections.extend(item for rows in (replaceable.get("rule_selections") or {}).values()
                                  for item in rows)
                pending_shadow_outcomes = sum(item.get("status") == "PENDING" for item in selections)
                if (self._has_pending_event_records(replaceable)
                        or replaceable.get("unresolved_execution_id")):
                    raise ValueError("SESSION_REQUIRES_RECONCILIATION")
                has_open = getattr(self._persistence, "has_open_rule_session_executions", None)
                if callable(has_open):
                    try:
                        if has_open(str(replace_session_id)):
                            raise ValueError("SESSION_REQUIRES_RECONCILIATION")
                    except ValueError:
                        raise
                    except Exception as exc:
                        raise ValueError("SESSION_RECONCILIATION_STATUS_UNAVAILABLE") from exc
            elif conflicts:
                raise ValueError("SESSION_ALREADY_ACTIVE_FOR_MODE")
            # A manually started run owns the next boundary.  Do not also
            # launch a previously armed automatic continuation for that mode.
            for existing in self.sessions.values():
                if (existing.get("mode") == mode
                        and (reconfigure_session_id is None
                             or existing.get("session_id") != str(reconfigure_session_id))
                        and existing.get("last_stream_id") == start["stream_id"]
                        and existing.get("auto_next_armed")):
                    existing["auto_next_armed"] = False
                    existing["auto_next_block_reason"] = "MANUAL_SESSION_STARTED"
                    dirty_session_ids.add(str(existing["session_id"]))
            if replaceable is not None:
                prior_reason = str(replaceable.get("stop_reason"))
                replaceable.update({"status": "MANUALLY_STOPPED", "ended_at": now,
                                    "auto_next_armed": False, "auto_next_trigger": None,
                                    "auto_next_armed_at": None,
                                    "integrity_stop_reason": prior_reason})
                self._record_session_stop(
                    replaceable, "REPLACED_AFTER_INTEGRITY_STOP", now,
                    "OPERATOR_CONFIRMED_SHADOW_RESTART", stop_selection=True,
                    details={"new_session_id": session_id,
                             "prior_integrity_reason": prior_reason,
                             "preserved_pending_shadow_outcomes": pending_shadow_outcomes},
                )
                session["replaced_session_id"] = str(replace_session_id)
                session["replacement_reason"] = prior_reason
                dirty_session_ids.add(str(replace_session_id))
            self.sessions[session_id] = session
            self._persist(session_ids=dirty_session_ids)
        return deepcopy(session)

    def _version(self, rule_id: str) -> str:
        if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            return (START_CHECK_POLICY["version"] if rule_id == START_CHECK_POLICY_ID
                    else LEGACY_SINGLE_TRACKER_POLICY["version"])
        if rule_id == COMBINED_POLICY_ID:
            return COMBINED_POLICY["version"]
        return _research_by_id[rule_id]["version"]

    @staticmethod
    def _definition(rule_id: str) -> dict[str, Any]:
        if rule_id == START_CHECK_POLICY_ID:
            return deepcopy(START_CHECK_POLICY)
        if rule_id == LEGACY_POLICY_ID:
            return deepcopy(LEGACY_SINGLE_TRACKER_POLICY)
        if rule_id == COMBINED_POLICY_ID:
            return deepcopy(COMBINED_POLICY)
        return deepcopy(_research_by_id[rule_id])

    def _manifest(self, rule_ids: list[str]) -> dict[str, Any]:
        entries = [self._definition(rule_id) for rule_id in rule_ids]
        encoded = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return {"manifest_version": "WINNER_RULES_MANIFEST_V1", "rules": entries,
                "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}

    @staticmethod
    def _selection_targets(selection: Mapping[str, Any], source: Mapping[str, Any]) -> bool:
        target = selection.get("expected_target") or {}
        if not isinstance(target, Mapping):
            return False
        expected_stream = target.get("streamId", target.get("stream_id"))
        expected_index = target.get("roundIndex", target.get("round_index"))
        expected_id = target.get("roundId", target.get("round_id"))
        return (expected_stream == source.get("stream_id")
                and expected_index == source.get("round_index")
                and (expected_id is None or expected_id == source.get("round_id")))

    @staticmethod
    def _settle_selection(selection: dict[str, Any], source: dict[str, Any], at: str,
                          *, stake: float, payout_multiplier: float) -> dict[str, Any]:
        """Apply one verified target result and preserve its evidence and accounting."""
        win = source["outcome"] == "H"
        result = "WIN" if win else "LOSS"
        payout = round(stake * payout_multiplier, 2) if win else 0.0
        profit_loss = round(payout - stake, 2) if win else round(-stake, 2)
        target = _reference(source)
        selection.update({
            "decision_id": selection.get("decision_id") or selection.get("selection_id"),
            "source_round_id": (selection.get("source_round") or {}).get("roundId"),
            "target_round_id": source.get("round_id"),
            "stake_rwf": stake,
            "payout_multiplier": payout_multiplier,
            "status": result,
            "result": result,
            "settlement_status": result,
            "settlement_phase": "SETTLED",
            "actual_multiplier": source["multiplier"],
            "actual_target": target,
            "resolved_at": at,
            "settled_at": at,
            "target_round_completed_at": source.get("observed_at"),
            "target_persistence_status": source.get("durability", "UNKNOWN"),
            "payout_rwf": payout,
            "profit_loss_rwf": profit_loss,
            "settlement_evidence": {
                "event_type": source.get("event_type"),
                "durability": source.get("durability", "UNKNOWN"),
                "continuity_status": source.get("continuity_status", "UNKNOWN"),
                "stream_id": source.get("stream_id"),
                "stream_sequence": source.get("stream_sequence"),
                "target_round_id": source.get("round_id"),
                "target_round_index": source.get("round_index"),
                "target_multiplier": source.get("multiplier"),
                "target_observed_at": source.get("observed_at"),
                "settled_at": at,
            },
        })
        expected_target = selection.get("expected_target")
        if isinstance(expected_target, dict):
            expected_target["roundId"] = source.get("round_id")
        intent = selection.get("execution_intent")
        if isinstance(intent, dict):
            intent["target_round_id"] = source.get("round_id")
        return {"decision_id": selection.get("decision_id"),
                "selection_id": selection.get("selection_id"),
                "source_round_id": selection.get("source_round_id"),
                "target_round_id": source.get("round_id"),
                "result": result, "stake_rwf": stake, "payout_rwf": payout,
                "profit_loss_rwf": profit_loss, "settled_at": at}

    @staticmethod
    def _mark_target_persistence_committed(session: dict[str, Any], source: Mapping[str, Any]) -> bool:
        changed = False
        evidence = {
            "event_type": source.get("event_type"),
            "durability": source.get("durability", "UNKNOWN"),
            "continuity_status": source.get("continuity_status", "UNKNOWN"),
            "stream_id": source.get("stream_id"),
            "stream_sequence": source.get("stream_sequence"),
            "target_round_id": source.get("round_id"),
            "stored_at": source.get("stored_at"),
        }
        rows = list(session.get("selections") or [])
        rows.extend(selection for group in (session.get("rule_selections") or {}).values()
                    for selection in group)
        for selection in rows:
            actual_target = selection.get("actual_target") or {}
            if (selection.get("status") not in {"WIN", "LOSS"}
                    or actual_target.get("streamId") != source.get("stream_id")
                    or actual_target.get("roundId") != source.get("round_id")):
                continue
            if (selection.get("target_persistence_status") == "COMMITTED"
                    and (selection.get("settlement_evidence") or {}).get("persistence_confirmation") == evidence):
                continue
            selection["target_persistence_status"] = "COMMITTED"
            changed = True
            settlement_evidence = selection.setdefault("settlement_evidence", {})
            if isinstance(settlement_evidence, dict):
                settlement_evidence["persistence_confirmation"] = deepcopy(evidence)
        last_settled = session.get("last_settled_target")
        if (isinstance(last_settled, dict)
                and last_settled.get("stream_id") == source.get("stream_id")
                and last_settled.get("round_id") == source.get("round_id")):
            if (last_settled.get("durability") != "COMMITTED"
                    or last_settled.get("persistence_confirmation") != evidence):
                last_settled["durability"] = "COMMITTED"
                last_settled["persistence_confirmation"] = deepcopy(evidence)
                changed = True
        return changed

    def _resolve(self, session: dict[str, Any], source: dict[str, Any], at: str) -> list[dict[str, Any]]:
        target_index = source["round_index"]
        stream_id = source["stream_id"]
        settled: list[dict[str, Any]] = []
        seen_decisions: set[str] = set()
        for combined in session.get("selections", []):
            if combined.get("status") != "PENDING" or not self._selection_targets(combined, source):
                continue
            decision_id = str(combined.get("decision_id") or combined.get("selection_id") or "")
            if decision_id and decision_id in seen_decisions:
                continue
            if decision_id:
                seen_decisions.add(decision_id)
            configuration = session.get("configuration_snapshot") or {}
            raw_stake = combined.get("stake_rwf")
            if raw_stake is None:
                raw_stake = configuration.get("fixed_stake_rwf", 100)
            raw_payout = combined.get("payout_multiplier")
            if raw_payout is None:
                raw_payout = configuration.get("cashout_multiplier")
            if raw_payout is None:
                raw_payout = 2.0
            try:
                stake = float(raw_stake)
                payout_multiplier = float(raw_payout)
            except (TypeError, ValueError, OverflowError):
                combined["settlement_block_reason"] = "STAKE_OR_PAYOUT_CONFIGURATION_INVALID"
                continue
            if (not math.isfinite(stake) or stake <= 0 or not math.isfinite(payout_multiplier)
                    or payout_multiplier != 2.0):
                combined["settlement_block_reason"] = "STAKE_OR_PAYOUT_CONFIGURATION_INVALID"
                continue
            settled_item = self._settle_selection(combined, source, at,
                stake=stake, payout_multiplier=payout_multiplier)
            settled.append(settled_item)
            session["net_profit_rwf"] = round(float(session.get("net_profit_rwf") or 0)
                                                + settled_item["profit_loss_rwf"], 2)
            for rule_id in combined.get("contributing_rule_ids", []):
                state = session.get("engines", {}).get(rule_id)
                if not state:
                    continue
                if state.get("pending_selection_id") == combined.get("selection_id"):
                    self._advance_selected_state(session, rule_id, state, source,
                        settled_item["result"], combined, at)
                    state["pending_selection_id"] = None
                    state["pending_target"] = None
        if settled:
            wins, losses = self._settled_shared_result_counts(session)
            session["total_wins"] = wins
            session["total_losses"] = losses
            config = session["configuration_snapshot"]
            if session["mode"] == "ALL_RULES_TOGETHER":
                self._reconcile_net_outcome_limit_stop(session, wins, losses, at, source)
                if config.get("max_selections") and len(session["selections"]) >= config["max_selections"]:
                    self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source,
                                         stop_source="MAX_SELECTIONS")
                self._arm_auto_next_after_limit(
                    session, session.get("selection_stop_reason"), at, source)
            if session["mode"] in {"START_CHECK", "LEGACY_SEPARATE_TRACKER"}:
                limit = session["configuration_snapshot"]
                if limit.get("profit_target_enabled") and session["net_profit_rwf"] >= limit.get("profit_target_rwf", 400):
                    self._stop_selecting(session, "PROFIT_TARGET_REACHED", at, source,
                                         stop_source="PROFIT_TARGET")
                    self._arm_auto_next_after_limit(
                        session, session.get("selection_stop_reason"), at, source)
        for rule_id, selections in session["rule_selections"].items():
            for selection in selections:
                if selection.get("status") != "PENDING" or not self._selection_targets(selection, source):
                    continue
                result = "WIN" if source["outcome"] == "H" else "LOSS"
                selection.update({"status": result, "result": result,
                    "actual_multiplier": source["multiplier"], "actual_target": _reference(source),
                    "resolved_at": at,
                    "target_persistence_status": source.get("durability", "UNKNOWN")})
                state = session["engines"][rule_id]
                if state.get("pending_selection_id") == selection.get("selection_id"):
                    self._advance_selected_state(session, rule_id, state, source, result, selection, at)
                    state["pending_selection_id"] = None
                    state["pending_target"] = None
        return settled

    def _advance_selected_state(self, session: dict[str, Any], rule_id: str, state: dict[str, Any],
                                source: dict[str, Any], result: str, selection: dict[str, Any], at: str) -> None:
        state["selected_wins"] = int(state.get("selected_wins", 0)) + (result == "WIN")
        state["selected_losses"] = int(state.get("selected_losses", 0)) + (result == "LOSS")
        outcome = "H" if result == "WIN" else "L"
        if rule_id == "AFTER_HH_CONTINUE_MAX_3":
            cycle = state.get("cycle")
            if cycle and cycle.get("active") and cycle.get("cycle_id") == selection.get("cycle_id"):
                cycle["resolved_count"] += 1
                cycle["last_result"] = result
                if cycle["resolved_count"] >= 3:
                    cycle["active"] = False
                    cycle["status"] = "COMPLETED"
                    state["cycle_mode"] = "IDLE"
            return
        if rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
            return
        state["selected_outcomes"].append(outcome)
        special = state["mode"] == "TWO_BET_WINS"
        previous = state.get("previous_outcome")
        if rule_id in SPECIAL_SKIP_POLICY_IDS and state.get("special_skip_active") and not special:
            if outcome == "H":
                special_wins = int(state.get("special_skip_win_count", 0)) + 1
                if special_wins >= 2:
                    state.update({"rule_state": "WAIT_LOW", "mode": "STANDARD",
                                  "previous_outcome": None, "failed_cycle_count": 0,
                                  "consecutive_bet_wins": 0, "selected_outcomes": [],
                                  "special_skip_active": False,
                                  "special_skip_after_round_index": None,
                                  "special_skip_win_count": 0})
                    return
                state.update({"previous_outcome": "H", "special_skip_win_count": special_wins,
                              "special_skip_after_round_index": int(source["round_index"])})
                return
            # A real selected LOSS inside the special sequence ends this policy
            # cycle immediately. The same verified round is evaluated below,
            # so retain its index to prevent WAIT_LOW reprocessing it.
            state.update({"rule_state": "WAIT_LOW", "mode": "STANDARD",
                          "previous_outcome": None, "consecutive_bet_wins": 0,
                          "selected_outcomes": [], "special_skip_active": False,
                          "special_skip_after_round_index": None,
                          "special_skip_win_count": 0,
                          "special_skip_terminal_round_index": int(source["round_index"])})
            return
        if (rule_id in SPECIAL_SKIP_POLICY_IDS and not special
                and state.get("mode") == "STANDARD" and not state.get("special_skip_active")
                and previous == "L" and outcome == "H"
                and state["selected_outcomes"] == ["L", "H"]):
            state.update({"special_skip_active": True,
                          "special_skip_after_round_index": int(source["round_index"]),
                          "special_skip_win_count": 0, "previous_outcome": "H"})
            return
        if not special and rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            previous = previous or "H"  # exact legacy reducer seed from trigger H
            if previous == outcome:
                if outcome == "H":
                    state["rule_state"] = "WAIT_LOW"
                    state["failed_cycle_count"] = 0
                    state["mode"] = "STANDARD"
                else:
                    state["rule_state"] = "WAIT_HIGH"
                    state["failed_cycle_count"] = int(state.get("failed_cycle_count", 0)) + 1
                    if state["failed_cycle_count"] >= 2:
                        state["mode"] = "TWO_BET_WINS"
                state["previous_outcome"] = None
                state["selected_outcomes"] = []
                return
            state["previous_outcome"] = outcome
            return
        if not special:
            if len(state["selected_outcomes"]) == 1 and outcome == "H":
                state.update({"rule_state": "WAIT_LOW", "previous_outcome": None,
                              "failed_cycle_count": 0, "selected_outcomes": []})
                return
            if previous == outcome:
                if outcome == "H":
                    state.update({"rule_state": "WAIT_LOW", "previous_outcome": None,
                                  "failed_cycle_count": 0, "selected_outcomes": []})
                else:
                    state.update({"rule_state": "WAIT_HIGH", "previous_outcome": None,
                                  "failed_cycle_count": int(state.get("failed_cycle_count", 0)) + 1,
                                  "selected_outcomes": []})
                    if state["failed_cycle_count"] >= 2:
                        state["mode"] = "TWO_BET_WINS"
                return
            state["previous_outcome"] = outcome
            return
        if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            streak = int(state.get("consecutive_bet_wins", 0)) + 1 if outcome == "H" else 0
            state["previous_outcome"] = outcome
            state["consecutive_bet_wins"] = streak
            if streak >= 2:
                state.update({"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                              "failed_cycle_count": 0, "consecutive_bet_wins": 0, "selected_outcomes": []})
            return
        if previous == outcome:
            if outcome == "H":
                state.update({"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                              "failed_cycle_count": 0, "consecutive_bet_wins": 0, "selected_outcomes": []})
            else:
                state.update({"rule_state": "WAIT_HIGH", "previous_outcome": None,
                              "selected_outcomes": []})
            return
        state["previous_outcome"] = outcome
        state["consecutive_bet_wins"] = int(state.get("consecutive_bet_wins", 0)) + 1 if outcome == "H" else 0

    @staticmethod
    def _source_round(source: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(source, Mapping):
            return None
        round_id = source.get("round_id", source.get("roundId"))
        stream_id = source.get("stream_id", source.get("streamId"))
        round_index = source.get("round_index", source.get("roundIndex"))
        if round_id is None and stream_id is None and round_index is None:
            return None
        return {"stream_id": stream_id, "round_id": round_id, "round_index": round_index}

    @staticmethod
    def _append_stop_event(session: dict[str, Any], reason: str, at: str, stop_source: str,
                           source_round: Mapping[str, Any] | None, scope: str,
                           details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        event = {"reason": reason, "source": stop_source, "stopped_at": at,
                 "source_round": deepcopy(dict(source_round)) if source_round else None,
                 "scope": scope, "details": deepcopy(dict(details or {}))}
        events = session.get("stop_history")
        if not isinstance(events, list):
            events = []
            session["stop_history"] = events
        if not any(item.get("reason") == reason and item.get("stopped_at") == at
                   and item.get("scope") == scope for item in events if isinstance(item, Mapping)):
            events.append(event)
        return event

    @staticmethod
    def _resume_after_round_gap(session: dict[str, Any], row: Mapping[str, Any], at: str,
                                expected_index: int) -> None:
        """Start a clean verified history segment after a committed index gap."""
        received_index = int(row.get("round_index") or 0)
        remaining_late_transitions = []
        for item in session.get("late_target_transitions") or []:
            if not isinstance(item, Mapping):
                continue
            try:
                target_index = int(item.get("target_round_index") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if (item.get("target_stream_id") != row.get("stream_id")
                    or target_index >= received_index):
                remaining_late_transitions.append(item)
                continue
            raw_proposal = item.get("proposal")
            proposal = deepcopy(dict(raw_proposal)) if isinstance(raw_proposal, Mapping) else {}
            resolution = {"status": "TARGET_SKIPPED_BY_ROUND_GAP",
                          "target_round_index": target_index,
                          "actual_bet_placed": False}
            proposal["late_target_resolution"] = resolution
            nested = proposal.get("proposal")
            if not isinstance(nested, dict):
                nested = {}
                proposal["proposal"] = nested
            relevant = nested.get("relevantInputs")
            if not isinstance(relevant, dict):
                relevant = {}
                nested["relevantInputs"] = relevant
            relevant["lateTargetResolution"] = deepcopy(resolution)
            for record in (session.get("event_records") or {}).values():
                if not isinstance(record, Mapping):
                    continue
                if record.get("source_round") != item.get("source_round"):
                    continue
                for saved in record.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == item.get("rule_id"):
                        saved.update(deepcopy(proposal))
            for record in session.get("evaluations") or []:
                if not isinstance(record, Mapping):
                    continue
                if record.get("source_round") != item.get("source_round"):
                    continue
                for saved in record.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == item.get("rule_id"):
                        saved.update(deepcopy(proposal))
        session["late_target_transitions"] = remaining_late_transitions
        ledgers = [session.get("selections") or []]
        ledgers.extend((session.get("rule_selections") or {}).values())
        for selections in ledgers:
            for selection in selections:
                if selection.get("status") != "PENDING":
                    continue
                target = selection.get("expected_target") or selection.get("target_round") or {}
                try:
                    target_index = int(target.get("roundIndex", target.get("round_index")))
                except (TypeError, ValueError, OverflowError):
                    continue
                target_stream = target.get("streamId", target.get("stream_id"))
                if target_stream == row.get("stream_id") and target_index < received_index:
                    selection.update({"status": "SKIPPED_GAP", "result": None,
                                      "settlement_status": "SKIPPED_GAP",
                                      "settlement_phase": "ROUND_GAP",
                                      "settlement_block_reason": "TARGET_INDEX_SKIPPED_BY_ROUND_GAP",
                                      "gap_skipped_at": at})
        pending_selection_ids = {
            str(selection.get("selection_id"))
            for selections in ledgers for selection in selections
            if selection.get("status") == "PENDING" and selection.get("selection_id")
        }
        previous_states = session.get("engines") or {}
        reset_states: dict[str, dict[str, Any]] = {}
        for rule_id in session.get("rule_ids") or []:
            fresh = _fresh_engine(rule_id)
            previous = previous_states.get(rule_id) or {}
            # Keep cumulative result and health counters, while dropping stale
            # pattern history and pending state from the broken segment.
            for key in ("selected_wins", "selected_losses", "failure_count", "recovery_count"):
                if key in previous:
                    fresh[key] = previous[key]
            pending_id = previous.get("pending_selection_id")
            if pending_id is not None and str(pending_id) in pending_selection_ids:
                fresh["pending_selection_id"] = pending_id
                fresh["pending_target"] = deepcopy(previous.get("pending_target"))
            reset_states[rule_id] = fresh
        session["engines"] = reset_states
        session["context"] = []
        session["last_continuity_gap"] = {
            "expected_index": int(expected_index),
            "received_index": received_index,
            "round_id": row.get("round_id"),
            "resumed_at": at,
        }
        manual_pause = (session.get("status") == "PAUSED"
                        and session.get("stop_reason") == "MANUAL_PAUSE")
        resumed_status = ("PAUSED" if manual_pause else
                          "RUNNING" if session.get("selection_status") == "ACTIVE" else "SETTLING")
        resumed_reason = "MANUAL_PAUSE" if manual_pause else None
        updates = {"status": resumed_status, "stop_reason": resumed_reason,
                   "gap_expected_index": None, "gap_received_index": None,
                   # The committed round itself becomes the first source in
                   # the new segment, so the normal path accepts it below.
                   "last_round_index": int(row.get("round_index") or 1) - 1,
                   "last_round_id": None}
        if not manual_pause:
            updates.update({"stop_source": None, "stop_at": None, "stop_round_id": None,
                            "stop_details": {}})
        session.update(updates)

    @staticmethod
    def _persisted_late_target(source: Mapping[str, Any], target_index: int,
                               target: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(target, Mapping):
            return None
        verified = target.get("continuity_verified")
        if not (verified is True or verified == 1):
            return None
        multiplier = _multiplier(target.get("multiplier"))
        round_id = target.get("round_id")
        try:
            actual_index = int(target.get("round_index") or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if (multiplier is None or not isinstance(round_id, str) or not round_id.strip()
                or actual_index != int(target_index)):
            return None
        return {"stream_id": source["stream_id"], "round_id": round_id,
                "observation_id": round_id, "round_index": int(target_index),
                "multiplier": multiplier, "outcome": classify_outcome(multiplier),
                "event_type": "round.persisted", "durability": "COMMITTED",
                "continuity_status": "VERIFIED"}

    @staticmethod
    def _observed_late_target(source: Mapping[str, Any], target_index: int,
                              event: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if (not isinstance(event, Mapping) or event.get("event_type") != "round.observed"
                or event.get("durability") != "PENDING"
                or event.get("continuity_status") != "VERIFIED"):
            return None
        target = _identity(event, require_sequence=False)
        if (target is None or target.get("stream_id") != source.get("stream_id")
                or target.get("round_index") != int(target_index)):
            return None
        return target

    def _apply_late_target_to_rule_state(self, session: dict[str, Any], proposal: dict[str, Any],
                                         target: Mapping[str, Any], at: str) -> dict[str, Any] | None:
        rule_id = str(proposal.get("rule_id") or "")
        state = (session.get("engines") or {}).get(rule_id)
        if (not isinstance(state, dict) or state.get("pending_selection_id")
                or target.get("outcome") not in {"H", "L"}):
            return None
        selection_id = "late:" + hashlib.sha256("\0".join((
            str(session.get("session_id") or ""), rule_id,
            str((proposal.get("source_round") or {}).get("roundId") or ""),
            str((proposal.get("target_round") or {}).get("roundIndex") or ""),
        )).encode("utf-8")).hexdigest()
        virtual_selection = {"selection_id": selection_id, "selected_at": at,
                             "source_round": deepcopy(proposal.get("source_round") or {}),
                             "expected_target": deepcopy(proposal.get("target_round") or {}),
                             "contributing_rule_ids": [rule_id]}
        self._commit_contributor(session, proposal, virtual_selection)
        result = "WIN" if target.get("outcome") == "H" else "LOSS"
        self._advance_selected_state(session, rule_id, state, dict(target), result,
                                     virtual_selection, at)
        if state.get("pending_selection_id") == selection_id:
            state["pending_selection_id"] = None
            state["pending_target"] = None
        resolution = {"status": "APPLIED_TO_RULE_STATE", "result": result,
                      "target_round": _reference(target),
                      "target_persistence_status": target.get("durability"),
                      "actual_bet_placed": False}
        proposal["late_target_resolution"] = deepcopy(resolution)
        proposal.setdefault("proposal", {}).setdefault("relevantInputs", {})[
            "lateTargetResolution"] = deepcopy(resolution)
        after = deepcopy(state)
        proposal.setdefault("state_snapshot", {})["after"] = after
        proposal.setdefault("proposal", {}).setdefault("privateState", {})["after"] = deepcopy(after)
        return resolution

    def _resolve_deferred_late_targets(self, session: dict[str, Any], row: Mapping[str, Any],
                                       at: str) -> bool:
        pending = session.get("late_target_transitions") or []
        if not pending:
            return False
        remaining = []
        changed = False
        for item in pending:
            if not isinstance(item, Mapping):
                continue
            try:
                target_index = int(item.get("target_round_index") or 0)
                current_index = int(row.get("round_index") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if item.get("target_stream_id") != row.get("stream_id"):
                remaining.append(item)
                continue
            target: dict[str, Any] | None = None
            if target_index == current_index:
                if (row.get("continuity_status") == "VERIFIED"
                        and row.get("event_type") in {"round.observed", "round.persisted"}
                        and row.get("durability") in {"PENDING", "COMMITTED"}
                        and row.get("round_id") and _multiplier(row.get("multiplier")) is not None):
                    target = dict(row)
            if target is None and target_index <= current_index:
                # Retry canonical stores when the target's first observation
                # lacked continuity proof or its event was missed. Do this
                # before allowing a later source to evaluate stale policy state.
                checker = getattr(self._persistence, "round_continuity_for_indices", None)
                if callable(checker):
                    try:
                        persisted = checker([target_index])
                    except Exception:
                        persisted = None
                    if isinstance(persisted, Mapping):
                        target = self._persisted_late_target(
                            {"stream_id": row.get("stream_id")}, target_index,
                            persisted.get(target_index))
                if target is None:
                    reader = getattr(self.observation_inbox, "pending_round_for_index", None)
                    if callable(reader):
                        try:
                            observed = reader(target_index)
                        except Exception:
                            observed = None
                        target = self._observed_late_target(
                            {"stream_id": row.get("stream_id")}, target_index, observed)
            if target is None:
                remaining.append(item)
                continue
            raw_proposal = item.get("proposal")
            proposal = deepcopy(dict(raw_proposal)) if isinstance(raw_proposal, Mapping) else {}
            resolution = self._apply_late_target_to_rule_state(session, proposal, target, at)
            if resolution is None:
                remaining.append(item)
                continue
            source_round = item.get("source_round")
            rule_id = proposal.get("rule_id")
            for record in (session.get("event_records") or {}).values():
                if not isinstance(record, Mapping):
                    continue
                if record.get("source_round") != source_round:
                    continue
                for saved in record.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == rule_id:
                        saved.update(deepcopy(proposal))
            for record in session.get("evaluations") or []:
                if not isinstance(record, Mapping):
                    continue
                if record.get("source_round") != source_round:
                    continue
                for saved in record.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == rule_id:
                        saved.update(deepcopy(proposal))
            latest = session.get("last_evaluation")
            if isinstance(latest, dict) and latest.get("source_round") == source_round:
                for saved in latest.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == rule_id:
                        saved.update(deepcopy(proposal))
            changed = True
        session["late_target_transitions"] = remaining
        return changed

    def _record_session_stop(self, session: dict[str, Any], reason: str, at: str,
                             stop_source: str, source_round: Mapping[str, Any] | None = None,
                             *, stop_selection: bool = False,
                             details: Mapping[str, Any] | None = None) -> None:
        ref = self._source_round(source_round) or self._source_round({
            "stream_id": session.get("last_stream_id"), "round_id": session.get("last_round_id"),
            "round_index": session.get("last_round_index"),
        })
        scope = "SESSION_AND_SELECTION" if stop_selection else "SESSION"
        event = self._append_stop_event(session, reason, at, stop_source, ref, scope, details)
        session.update({"stop_reason": reason, "stop_source": stop_source, "stop_at": at,
                        "stop_round_id": (ref or {}).get("round_id"), "stop_details": deepcopy(dict(details or {})),
                        "last_stop_event": event})
        if stop_selection:
            session.update({"selection_status": "STOPPED", "selection_stop_reason": reason,
                            "selection_stop_source": stop_source, "selection_stopped_at": at,
                            "selection_stopped_at_round_id": (ref or {}).get("round_id"),
                            "selection_stop_details": deepcopy(dict(details or {}))})

    def _stop_selecting(self, session: dict[str, Any], reason: str, at: str,
                        source: Mapping[str, Any], *, stop_source: str | None = None,
                        stop_details: Mapping[str, Any] | None = None) -> None:
        if session.get("selection_status") == "ACTIVE":
            ref = self._source_round(source)
            stop_source = stop_source or "RULE_ENGINE"
            event = self._append_stop_event(session, reason, at, stop_source, ref,
                                            "SELECTION", stop_details)
            session.update({"selection_status": "STOPPED", "selection_stop_reason": reason,
                            "selection_stop_source": stop_source,
                            "selection_stopped_at": at,
                            "selection_stopped_at_round_id": (ref or {}).get("round_id"),
                            "selection_stop_details": deepcopy(dict(stop_details or {})),
                            "last_selection_stop_event": event})
            if session.get("status") == "RUNNING":
                # The collector continues resolving pending targets and
                # completing the observation window, but new selections stop.
                session["status"] = "SETTLING"

    def _reconcile_net_outcome_limit_stop(self, session: dict[str, Any], wins: int, losses: int,
                                          at: str, source: Mapping[str, Any]) -> None:
        """Keep the active selection stop aligned with settled net outcomes.

        Stop events are append-only history. A net limit can cease to be true
        as already-pending selections settle, so its active fields and status
        must be cleared without deleting that historical event.
        """
        config = session.get("configuration_snapshot") or {}
        net_stop = self._net_outcome_stop_reason(wins, losses, config)
        active_reason = session.get("selection_stop_reason")
        active_source = session.get("selection_stop_source")
        is_net_stop = (active_reason in NET_OUTCOME_LIMIT_REASONS
                       and active_source in {None, "NET_OUTCOME_LIMIT", "LEGACY_UNVERIFIED"})

        if net_stop is None:
            if is_net_stop:
                stopped_at = session.get("selection_stopped_at")
                if stopped_at and not any(
                    isinstance(event, Mapping)
                    and event.get("reason") == active_reason
                    and event.get("stopped_at") == stopped_at
                    and event.get("scope") in {"SELECTION", "SESSION_AND_SELECTION"}
                    for event in (session.get("stop_history") or [])
                ):
                    self._append_stop_event(session, active_reason, stopped_at,
                        active_source or "NET_OUTCOME_LIMIT", None, "SELECTION",
                        session.get("selection_stop_details"))
                session.update({"selection_status": "ACTIVE", "selection_stop_reason": None,
                    "selection_stop_source": None, "selection_stopped_at": None,
                    "selection_stopped_at_round_id": None, "selection_stop_details": {}})
                if session.get("status") == "SETTLING":
                    session["status"] = "RUNNING"
            if (session.get("stop_reason") in NET_OUTCOME_LIMIT_REASONS
                    and session.get("stop_source") in {None, "NET_OUTCOME_LIMIT", "LEGACY_UNVERIFIED"}):
                session.update({"stop_reason": None, "stop_source": None, "stop_at": None,
                                "stop_round_id": None, "stop_details": {}})
            if (session.get("auto_next_trigger") == "NET_OUTCOME_LIMIT"
                    and session.get("auto_next_armed")
                    and not session.get("auto_next_session_id")):
                session.update({"auto_next_armed": False, "auto_next_trigger": None,
                                "auto_next_armed_at": None,
                                "auto_next_block_reason": "LIMIT_CONDITION_NO_LONGER_ACTIVE"})
            return

        limit_name = "WIN_LIMIT" if net_stop == "WIN_LIMIT_REACHED" else "LOSS_LIMIT"
        details = {"limit": limit_name,
                   "limit_count": config.get("win_limit_count" if limit_name == "WIN_LIMIT"
                                              else "loss_limit_count"),
                   "settled_wins": wins, "settled_losses": losses,
                   "net_outcome": wins - losses}
        if session.get("selection_status") == "ACTIVE":
            self._stop_selecting(session, net_stop, at, source,
                                 stop_source="NET_OUTCOME_LIMIT", stop_details=details)
            return
        if is_net_stop:
            if active_reason != net_stop:
                ref = self._source_round(source)
                event = self._append_stop_event(session, net_stop, at, "NET_OUTCOME_LIMIT",
                                                ref, "SELECTION", details)
                session.update({"selection_stop_reason": net_stop,
                    "selection_stop_source": "NET_OUTCOME_LIMIT",
                    "selection_stopped_at": at,
                    "selection_stopped_at_round_id": (ref or {}).get("round_id"),
                    "last_selection_stop_event": event})
            session["selection_stop_details"] = details
            if session.get("status") == "RUNNING":
                session["status"] = "SETTLING"

    @staticmethod
    def _arm_auto_next_after_limit(session: dict[str, Any], reason: str | None,
                                   at: str, source: Mapping[str, Any]) -> None:
        if session.get("configuration_snapshot", {}).get("auto_next_on_limit_enabled") is not True:
            return
        # Keep the first configured trigger. In particular, a maximum-selection
        # limit is armed when its final selection is created; settlement of
        # that selection must not move the next-check boundary one round later.
        if session.get("auto_next_armed") is True:
            return
        if reason not in {"PROFIT_TARGET_REACHED", "MAX_SELECTIONS_REACHED",
                          *NET_OUTCOME_LIMIT_REASONS}:
            return
        expected_source = ("PROFIT_TARGET" if reason == "PROFIT_TARGET_REACHED"
                           else "MAX_SELECTIONS" if reason == "MAX_SELECTIONS_REACHED"
                           else "NET_OUTCOME_LIMIT")
        if (session.get("selection_status") != "STOPPED"
                or session.get("selection_stop_reason") != reason
                or session.get("selection_stop_source") != expected_source
                or session.get("status") not in {"SETTLING", "COMPLETED"}
                or session.get("auto_next_session_id")):
            return
        context = deepcopy(session.get("context") or [])
        if not context or context[-1].get("round_id") != source.get("round_id"):
            context.append(deepcopy(dict(source)))
        session.update({"auto_next_armed": True, "auto_next_trigger": "LIMIT_REACHED",
                        "auto_next_armed_at": at,
                        "auto_next_after_round_id": source.get("round_id"),
                        "auto_next_boundary": None,
                        "auto_next_context": context[-5:],
                        "auto_next_block_reason": None})
        if (source.get("event_type") == "round.persisted"
                and source.get("durability") == "COMMITTED"
                and source.get("continuity_status") == "VERIFIED"
                and int(source.get("stream_sequence") or 0) > 0):
            RulesEngine._record_auto_next_boundary(session, source)

    @staticmethod
    def _recover_auto_next_boundary(session: dict[str, Any]) -> None:
        """Recover a legacy armed boundary only from matching commit evidence."""
        configuration = session.get("configuration_snapshot") or {}
        window_auto_next = (session.get("status") == "COMPLETED"
            and session.get("auto_next_trigger") in {None, "WINDOW_COMPLETED"}
            and configuration.get("auto_next_check_enabled") is True)
        limit_auto_next = (session.get("status") in {"SETTLING", "COMPLETED"}
            and session.get("auto_next_trigger") == "LIMIT_REACHED"
            and configuration.get("auto_next_on_limit_enabled") is True
            and session.get("selection_stop_source") in {
                "PROFIT_TARGET", "NET_OUTCOME_LIMIT", "MAX_SELECTIONS"}
            and session.get("selection_stop_reason") in {
                "PROFIT_TARGET_REACHED", "MAX_SELECTIONS_REACHED",
                *NET_OUTCOME_LIMIT_REASONS})
        if (session.get("auto_next_armed") is not True
                and session.get("auto_next_block_reason") == "AUTO_NEXT_COMMITTED_BOUNDARY_UNAVAILABLE"
                and not session.get("auto_next_session_id")
                and (window_auto_next or limit_auto_next)):
            # Older processing could disarm auto-next before the matching
            # persisted event attached its commit proof. Retry recovery from
            # the durable event ledger instead of leaving the session stopped.
            session["auto_next_armed"] = True
            session["auto_next_armed_at"] = (session.get("auto_next_armed_at")
                                               or session.get("ended_at"))
            session["auto_next_block_reason"] = None
        if (session.get("auto_next_armed") is not True
                or isinstance(session.get("auto_next_boundary"), Mapping)):
            return
        after_round_id = session.get("auto_next_after_round_id")
        context = deepcopy(session.get("context") or [])
        candidate = next((row for row in reversed(context)
                          if row.get("round_id") == after_round_id), None)
        if not isinstance(candidate, dict):
            return
        committed = next((record for record in (session.get("event_records") or {}).values()
            if record.get("persistence_status") == "COMMITTED"
            and (record.get("source_round") or {}).get("roundId") == after_round_id), None)
        if not isinstance(committed, Mapping):
            return
        evidence = committed.get("commit_evidence") or {}
        sequence = committed.get("committed_stream_sequence") or evidence.get("stream_sequence")
        if (evidence.get("event_type") != "round.persisted"
                or evidence.get("durability") != "COMMITTED"
                or evidence.get("continuity_status") != "VERIFIED"
                or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1):
            return
        candidate.update({"stream_sequence": sequence, "event_type": "round.persisted",
                          "durability": "COMMITTED", "continuity_status": "VERIFIED"})
        session["auto_next_boundary"] = deepcopy(candidate)
        for index, row in enumerate(context):
            if (row.get("stream_id") == candidate.get("stream_id")
                    and row.get("round_id") == after_round_id):
                context[index] = deepcopy(candidate)
        session["auto_next_context"] = context[-5:]

    @staticmethod
    def _record_auto_next_boundary(session: dict[str, Any], source: Mapping[str, Any]) -> None:
        """Freeze the committed round and context that triggered auto-next."""
        if (source.get("event_type") != "round.persisted"
                or source.get("durability") != "COMMITTED"
                or source.get("continuity_status") != "VERIFIED"
                or int(source.get("stream_sequence") or 0) < 1):
            return
        boundary = deepcopy(dict(source))
        context = deepcopy(session.get("auto_next_context") or session.get("context") or [])
        replaced = False
        for index, row in enumerate(context):
            if row.get("stream_id") == boundary.get("stream_id") \
                    and row.get("round_id") == boundary.get("round_id"):
                context[index] = boundary
                replaced = True
        if not replaced:
            context.append(boundary)
        session["auto_next_boundary"] = boundary
        session["auto_next_context"] = context[-5:]

    def _proposal(self, session: dict[str, Any], rid: str, source: dict[str, Any],
                  history: list[dict[str, Any]], at: str) -> dict[str, Any]:
        state = session["engines"][rid]
        before = deepcopy(state)
        expected = _expected(source)
        if state.get("pending_selection_id"):
            reason = "Target yasabwe n'iri tegeko ntirakemuwe." if rid not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID} else "Rule irategereje target yayo."
            return _result(rid, session["rule_versions"][rid], session, source, expected, None,
                           "PENDING_TARGET", reason, {"pendingTarget": state.get("pending_target")}, before, state, at)
        if rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
            return self._policy_proposal(session, rid, source, expected, at, before)
        definition = _research_by_id[rid]
        tail = _history_is_contiguous(history, definition["history_required"], source["stream_id"])
        if tail is None:
            after = deepcopy(state)
            reason = "Amakuru akurikirana ntarahagije."
            return _result(rid, definition["version"], session, source, expected, None,
                           "UNAVAILABLE_HISTORY", reason, {"historyRequired": definition["history_required"]},
                           before, after, at)
        values = [item["outcome"] for item in tail]
        matched = False
        cycle_id = None
        if rid == "RECENT_5_H_GE_4":
            matched = values.count("H") >= 4
            reason = f"H {values.count('H')} muri rounds 5: condition yujujwe." if matched else f"H {values.count('H')} muri rounds 5; condition ntiyujujwe."
        elif rid == "PATTERN_LH":
            matched = values == ["L", "H"]
            reason = "Rounds ebyiri ziheruka ni L,H." if matched else "Rounds ebyiri ziheruka si L,H."
        elif rid == "PATTERN_HH":
            matched = values == ["H", "H"]
            reason = "Rounds ebyiri ziheruka ni H,H." if matched else "Rounds ebyiri ziheruka si H,H."
        elif rid == "SOURCE_H_MULT_2_3":
            number = source["multiplier"]
            matched = 2.0 <= number < 3.0
            reason = "Igikubo cya source kiri hagati ya 2.00 na 3.00." if matched else "Igikubo cya source kiri hanze ya [2.00, 3.00)."
        else:
            cycle = state.get("cycle")
            if cycle and cycle.get("active"):
                matched = int(cycle.get("requested_count", 0)) < 3
                cycle_id = cycle.get("cycle_id")
                reason = "Cycle ya HH irasaba target ikurikiraho." if matched else "Cycle ya HH yarasabiye targets eshatu."
            else:
                matched = values == ["H", "H"]
                reason = "HH yabonetse; itegeko risaba target ikurikira." if matched else "HH ntiraboneka ku source."
        inputs = {"history": [{"roundId": item["round_id"], "roundIndex": item["round_index"],
                                "multiplier": item["multiplier"], "outcome": item["outcome"]} for item in tail],
                  "condition": deepcopy(definition["condition"])}
        if rid == "AFTER_HH_CONTINUE_MAX_3":
            inputs["continuationCycle"] = deepcopy(state.get("cycle"))
        return _result(rid, definition["version"], session, source, expected, matched,
                       "TRUE" if matched else "FALSE", reason, inputs, before, state, at)

    def _policy_proposal(self, session: dict[str, Any], rid: str, source: dict[str, Any],
                         expected: dict[str, Any], at: str, before: dict[str, Any]) -> dict[str, Any]:
        state = session["engines"][rid]
        outcome = source["outcome"]
        mode = state["mode"]
        matched = False
        reason = "Rule iracyategereje L."
        transition: dict[str, Any] | None = None
        after = deepcopy(state)
        terminal_round_index = state.get("special_skip_terminal_round_index")
        try:
            is_special_loss_terminal_round = (
                rid in SPECIAL_SKIP_POLICY_IDS
                and terminal_round_index is not None
                and int(source["round_index"]) == int(terminal_round_index))
        except (TypeError, ValueError, OverflowError):
            is_special_loss_terminal_round = False
        if is_special_loss_terminal_round:
            # Settlement already closed the cycle; ignore this same source as
            # a new WAIT_LOW observation, then consume the one-round marker.
            after["special_skip_terminal_round_index"] = None
            reason = "Special bet LOSS yahagaritse cycle; round imwe ntisubirwamo kuri WAIT_LOW."
            transition = {"type": "SPECIAL_SKIP_LOSS_TERMINAL_ROUND"}
        elif (rid in SPECIAL_SKIP_POLICY_IDS and mode == "STANDARD"
                and state.get("special_skip_active")):
            armed_after = state.get("special_skip_after_round_index")
            try:
                skip_round_is_upcoming = int(source["round_index"]) > int(armed_after)
            except (TypeError, ValueError, OverflowError):
                skip_round_is_upcoming = False
            if not skip_round_is_upcoming:
                reason = "Special skip irategereje round imwe ikurikira."
                transition = {"type": "SPECIAL_SKIP_ARMED"}
            elif float(source["multiplier"]) <= H_THRESHOLD:
                matched = True
                reason = "Round ya special skip ni L (<= 2.00); BET isabwa source+1."
                transition = {"type": "SPECIAL_SKIP_BET"}
            else:
                reason = "Round ya special skip ni H (> 2.00); cycle irangiye isubiye kuri WAIT_LOW."
                after.update({"rule_state": "WAIT_LOW", "mode": "STANDARD",
                              "previous_outcome": None, "failed_cycle_count": 0,
                              "consecutive_bet_wins": 0, "selected_outcomes": [],
                              "special_skip_active": False,
                              "special_skip_after_round_index": None,
                              "special_skip_win_count": 0})
                transition = {"type": "SPECIAL_SKIP_TERMINATE"}
        elif state["rule_state"] == "WAIT_LOW":
            if outcome == "L":
                matched = False
                reason = "Rule irategereje H nyuma ya L." if rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else "L yabonetse; itegereje H."
                after["rule_state"] = "WAIT_HIGH"
        elif state["rule_state"] == "WAIT_HIGH":
            if outcome == "H":
                matched = True
                reason = "L,H yujujwe; H isaba target ikurikira." if rid == COMBINED_POLICY_ID else "L,H yujujwe; H isaba target ikurikira."
                transition = {"type": "TRIGGER"}
        elif state["rule_state"] == "PLAYING":
            matched = True
            reason = "Policy ikomeje target zikurikirana; H/L yatoranyijwe ni yo ibarwa." \
                if rid == COMBINED_POLICY_ID else "Cycle iracyakora; source yasabye target ikurikiraho."
            transition = {"type": "CONTINUE"}
        return _result(rid, session["rule_versions"][rid], session, source, expected, matched,
                       "TRUE" if matched else "FALSE", reason,
                       {"sourceOutcome": outcome, "privateMode": mode, "transition": transition},
                       before, after, at)

    def _commit_contributor(self, session: dict[str, Any], proposal: dict[str, Any], selection: dict[str, Any]) -> None:
        rid = proposal["rule_id"]
        state = session["engines"][rid]
        target = deepcopy(selection["expected_target"])
        target["contributorRuleId"] = rid
        state["pending_selection_id"] = selection["selection_id"]
        state["pending_target"] = target
        if rid == "AFTER_HH_CONTINUE_MAX_3":
            cycle = state.get("cycle")
            if not cycle or not cycle.get("active"):
                cycle = {"cycle_id": str(uuid4()), "active": True, "status": "ACTIVE",
                         "requested_count": 0, "resolved_count": 0, "started_at": selection["selected_at"]}
                state["cycle"] = cycle
                state["cycle_mode"] = "CONTINUE_MAX_3"
            cycle["requested_count"] += 1
            cycle["last_source_round_id"] = selection["source_round"]["roundId"]
            selection["cycle_id"] = cycle["cycle_id"]
        elif rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
            relevant = proposal["proposal"].get("relevantInputs", {})
            if relevant.get("transition", {}).get("type") == "TRIGGER":
                state["rule_state"] = "PLAYING"
                state["previous_outcome"] = "H" if rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else None
                state["selected_outcomes"] = []
                state["cycle_number"] = int(state.get("cycle_number", 0)) + 1
            elif relevant.get("transition", {}).get("type") == "CONTINUE":
                state["rule_state"] = "PLAYING"

    def observe_round(self, event: Mapping[str, Any], *, now: str | None = None,
                      target_already_completed: bool = False,
                      defer_persistence: bool = False) -> dict[str, Any]:
        """Consume a verified observed or committed event, preserving its two stages."""
        processing_started_ns = time.perf_counter_ns()
        lock_wait_ms = 0.0
        policy_evaluation_ms = 0.0
        standard_rule_evaluation_ms = 0.0
        selected_result_evaluation_ms = 0.0
        two_bet_wins_transition_ms = 0.0
        target_identity_check_ms = 0.0
        decision_aggregation_ms = 0.0
        persistence_timings: dict[str, float] = {}
        at = _now(now)
        if not isinstance(event, Mapping):
            event = {}
        observed_stage = (event.get("event_type") == "round.observed" and event.get("durability") == "PENDING")
        committed_stage = (event.get("event_type") == "round.persisted" and event.get("durability") == "COMMITTED")
        row = _identity(event, require_sequence=not observed_stage)
        trusted = bool(row and (observed_stage or committed_stage)
                       and event.get("continuity_status") == "VERIFIED")
        if row is not None and trusted:
            # _identity intentionally keeps only round identity fields. Preserve
            # the verified delivery evidence too, so auto-next can freeze the
            # committed boundary for both window and configured-limit triggers.
            row.update({"event_type": event["event_type"],
                        "durability": event["durability"],
                        "continuity_status": event["continuity_status"]})
        lock_requested_ns = time.perf_counter_ns()
        with self._lock:
            lock_wait_ms = (time.perf_counter_ns() - lock_requested_ns) / 1_000_000.0
            dirty_session_ids: set[str] = set()
            if not trusted:
                status = ("UNAVAILABLE_CONTINUITY" if event.get("continuity_status") != "VERIFIED" else
                          "UNAVAILABLE_INVALID_INPUT" if _multiplier(event.get("multiplier")) is None
                          else "UNAVAILABLE_NOT_COMMITTED" if event.get("event_type") != "round.persisted"
                          or event.get("durability") != "COMMITTED" else "UNAVAILABLE_IDENTITY")
                reason = ("Amakuru akurikirana ntarahagije." if status == "UNAVAILABLE_CONTINUITY"
                          else "Multiplier cyangwa identity ntiyemejwe." if status != "UNAVAILABLE_NOT_COMMITTED"
                          else "Round itarangiye kandi ngo ibikwe ntisuzumwa.")
                invalid_ref = {"streamId": event.get("stream_id"), "roundId": event.get("round_id"),
                               "observationId": event.get("observation_id"),
                               "roundIndex": event.get("round_index"), "multiplier": event.get("multiplier"),
                               "outcome": None}
                sessions = []
                for session in self.sessions.values():
                    if session.get("status") not in {"RUNNING", "SETTLING", "PAUSED"}:
                        continue
                    evaluations = []
                    for rid in session["rule_ids"]:
                        before = deepcopy(session["engines"][rid])
                        result = _result(rid, session["rule_versions"][rid], session, {
                            "stream_id": str(event.get("stream_id") or ""),
                            "round_id": str(event.get("round_id") or ""),
                            "observation_id": str(event.get("observation_id") or event.get("round_id") or ""),
                            "round_index": int(event.get("round_index") or 0),
                            "multiplier": _multiplier(event.get("multiplier")) or 0.0,
                            "outcome": None,
                        }, None, None, status, reason, {"eventType": event.get("event_type"),
                            "durability": event.get("durability"),
                            "continuityStatus": event.get("continuity_status")},
                            before, before, at)
                        result["source_round"] = invalid_ref
                        result["proposal"]["sourceRoundRef"] = invalid_ref
                        evaluations.append(result)
                    sessions.append({"session_id": session["session_id"], "rule_evaluations": evaluations,
                                     "decision": {"action": "BLOCKED", "reason": status,
                                                  "true_rule_ids": [], "selection_id": None}})
                evaluation = {"decision": "BLOCKED", "result": False, "reason": status,
                              "source_round": invalid_ref, "rule_evaluations": [], "true_rule_ids": [],
                              "sessions": sessions, "automatic_execution": "OFF", "can_bet": False,
                              "evaluated_at": at}
                self.last_evaluation, self.last_evaluation_at = evaluation, at
                self._persist(session_ids=set())
                return deepcopy(evaluation)
            # Auto-next must be based on a PostgreSQL-committed contiguous
            # round. An observed/pending event cannot start another session.
            if committed_stage:
                dirty_session_ids.update(self._spawn_auto_next_sessions(row, at))
            settlement_started_ns = time.perf_counter_ns()
            settlements_by_session: dict[str, list[dict[str, Any]]] = {}
            for session in list(self.sessions.values()):
                if session.get("last_stream_id") != row["stream_id"]:
                    continue
                if committed_stage:
                    # Commit confirmation is immutable evidence for an already
                    # settled target, including sessions that have since ended.
                    if self._mark_target_persistence_committed(session, row):
                        dirty_session_ids.add(str(session["session_id"]))
                if not self._session_accepts_settlement_events(session):
                    continue
                if observed_stage or not session.get("last_received_round"):
                    session["last_received_round"] = {
                        "stream_id": row["stream_id"], "round_id": row["round_id"],
                        "round_index": row["round_index"], "multiplier": row["multiplier"],
                        "event_type": event.get("event_type"),
                        "durability": event.get("durability"),
                        "observed_at": row.get("observed_at"),
                        "received_at": event.get("api_received_at"),
                    }
                    dirty_session_ids.add(str(session["session_id"]))
                known_round = next((record.get("source_round")
                    for record in (session.get("event_records") or {}).values()
                    if isinstance(record, Mapping)
                    and isinstance(record.get("source_round"), Mapping)
                    and record["source_round"].get("streamId") == row["stream_id"]
                    and record["source_round"].get("roundId") == row["round_id"]), None)
                has_pending_target = any(
                    selection.get("status") == "PENDING" and self._selection_targets(selection, row)
                    for selection in session.get("selections", []))
                # A replay can settle outstanding work only if that target
                # event has not already been consumed. This prevents a late
                # or corrupt selection row from being backdated onto history.
                resolve_started_ns = time.perf_counter_ns()
                settled_items = ([] if known_round is not None
                                 else self._resolve(session, row, at))
                if self._resolve_deferred_late_targets(session, row, at):
                    dirty_session_ids.add(str(session["session_id"]))
                resolve_elapsed_ms = (time.perf_counter_ns() - resolve_started_ns) / 1_000_000.0
                selected_result_evaluation_ms += resolve_elapsed_ms
                if any(state.get("mode") == "TWO_BET_WINS"
                       for state in (session.get("engines") or {}).values()
                       if isinstance(state, Mapping)):
                    two_bet_wins_transition_ms += resolve_elapsed_ms
                if settled_items or has_pending_target:
                    dirty_session_ids.add(str(session["session_id"]))
                if settled_items:
                    session["last_settled_target"] = {
                        "stream_id": row["stream_id"], "round_id": row["round_id"],
                        "round_index": row["round_index"], "multiplier": row["multiplier"],
                        "event_type": event.get("event_type"),
                        "durability": event.get("durability"),
                        "observed_at": row.get("observed_at"),
                        "settled_at": at, "settlement_count": len(settled_items),
                        "results": deepcopy(settled_items),
                    }
                    settlements_by_session[session["session_id"]] = settled_items
            settlement_processing_ms = (time.perf_counter_ns() - settlement_started_ns) / 1_000_000.0
            record_key = (f"pending:{row['stream_id']}:{row['round_id']}" if observed_stage
                          else f"{row['stream_id']}:{row['stream_sequence']}")
            results: list[dict[str, Any]] = []
            for session in list(self.sessions.values()):
                committed_key = f"{row['stream_id']}:{row['stream_sequence']}"
                pending_key = f"pending:{row['stream_id']}:{row['round_id']}"
                if committed_stage:
                    committed_prior = session.get("event_records", {}).get(committed_key)
                    pending_prior = session.get("event_records", {}).get(pending_key)
                    if committed_prior:
                        if committed_prior.get("source_round") != _reference(row):
                            session["status"] = "PAUSED"
                            self._record_session_stop(session, "ROUND_IDENTITY_CONFLICT", at,
                                "ROUND_IDENTITY_VALIDATION", row,
                                details={"event_key": committed_key})
                            dirty_session_ids.add(str(session["session_id"]))
                        results.append(deepcopy(committed_prior))
                        continue
                    if pending_prior:
                        if pending_prior.get("source_round") != _reference(row):
                            session["status"] = "PAUSED"
                            self._record_session_stop(session, "OBSERVED_COMMIT_IDENTITY_CONFLICT", at,
                                "OBSERVED_COMMIT_IDENTITY_VALIDATION", row,
                                details={"event_key": pending_key})
                            dirty_session_ids.add(str(session["session_id"]))
                            continue
                        if pending_prior.get("gap_recovery_pending"):
                            # The observed event was deliberately held until its
                            # PostgreSQL commit proved the new boundary. Resume
                            # and evaluate that committed round as the first
                            # source in a clean history segment.
                            previous_sequence = int(session.get("last_stream_sequence") or 0)
                            expected_now = int(session.get("last_round_index") or 0) + 1
                            legacy_gap = (session.get("stop_reason") == "ROUND_INDEX_GAP"
                                or (int(session.get("gap_expected_index") or 0) > 0
                                    and int(session.get("gap_received_index") or 0) > 0))
                            manual_pause = (session.get("status") == "PAUSED"
                                and session.get("stop_reason") == "MANUAL_PAUSE")
                            can_recover = (session.get("status") in {"RUNNING", "SETTLING"}
                                           or (legacy_gap and session.get("status") == "PAUSED")
                                           or manual_pause)
                            if (can_recover and int(row["stream_sequence"]) > previous_sequence
                                    and (legacy_gap or int(row["round_index"]) >= expected_now)):
                                if legacy_gap or int(row["round_index"]) > expected_now:
                                    gap_expected = int(session.get("gap_expected_index") or
                                                       pending_prior.get("gap_expected_index") or expected_now)
                                    self._resume_after_round_gap(session, row, at, gap_expected)
                                if (session.get("auto_next_armed") is True
                                        and session.get("auto_next_after_round_id") == row["round_id"]):
                                    self._record_auto_next_boundary(session, row)
                                for selection in session.get("selections", []):
                                    actual_target = selection.get("actual_target") or {}
                                    if (actual_target.get("streamId") == row["stream_id"]
                                            and actual_target.get("roundId") == row["round_id"]):
                                        selection["target_persistence_status"] = "COMMITTED"
                                for selections in (session.get("rule_selections") or {}).values():
                                    for selection in selections:
                                        actual_target = selection.get("actual_target") or {}
                                        if (actual_target.get("streamId") == row["stream_id"]
                                                and actual_target.get("roundId") == row["round_id"]):
                                            selection["target_persistence_status"] = "COMMITTED"
                                session["event_records"].pop(pending_key, None)
                                dirty_session_ids.add(str(session["session_id"]))
                                # Continue below through normal committed
                                # source evaluation instead of merely promoting
                                # the provisional WAIT record.
                            else:
                                pending_prior["persistence_status"] = "COMMITTED"
                                pending_prior["committed_stream_sequence"] = row["stream_sequence"]
                                pending_prior["commit_evidence"] = {
                                    "event_type": event.get("event_type"),
                                    "durability": event.get("durability"),
                                    "continuity_status": event.get("continuity_status"),
                                    "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                                    "stream_id": row["stream_id"],
                                    "stream_sequence": row["stream_sequence"],
                                }
                                if (session.get("auto_next_armed") is True
                                        and session.get("auto_next_after_round_id") == row["round_id"]):
                                    self._record_auto_next_boundary(session, row)
                                session["event_records"][committed_key] = pending_prior
                                session["event_records"].pop(pending_key, None)
                                session["last_stream_sequence"] = max(previous_sequence,
                                                                       int(row["stream_sequence"]))
                                for selection in session.get("selections", []):
                                    actual_target = selection.get("actual_target") or {}
                                    if (actual_target.get("streamId") == row["stream_id"]
                                            and actual_target.get("roundId") == row["round_id"]):
                                        selection["target_persistence_status"] = "COMMITTED"
                                for selections in (session.get("rule_selections") or {}).values():
                                    for selection in selections:
                                        actual_target = selection.get("actual_target") or {}
                                        if (actual_target.get("streamId") == row["stream_id"]
                                                and actual_target.get("roundId") == row["round_id"]):
                                            selection["target_persistence_status"] = "COMMITTED"
                                dirty_session_ids.add(str(session["session_id"]))
                                results.append(deepcopy(pending_prior))
                                continue
                        else:
                            pending_prior["persistence_status"] = "COMMITTED"
                            pending_prior["committed_stream_sequence"] = row["stream_sequence"]
                            pending_prior["commit_evidence"] = {
                                "event_type": event.get("event_type"),
                                "durability": event.get("durability"),
                                "continuity_status": event.get("continuity_status"),
                                "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                                "stream_id": row["stream_id"],
                                "stream_sequence": row["stream_sequence"],
                            }
                            if (session.get("auto_next_armed") is True
                                    and session.get("auto_next_after_round_id") == row["round_id"]):
                                self._record_auto_next_boundary(session, row)
                            session["event_records"][committed_key] = pending_prior
                            session["event_records"].pop(pending_key, None)
                            for saved_evaluation in session.get("evaluations", []):
                                if (saved_evaluation.get("source_round") == pending_prior.get("source_round")
                                        and saved_evaluation.get("evaluated_at") == pending_prior.get("evaluated_at")):
                                    saved_evaluation["persistence_status"] = "COMMITTED"
                                    saved_evaluation["committed_stream_sequence"] = row["stream_sequence"]
                            session["last_stream_sequence"] = max(int(session.get("last_stream_sequence", 0)), row["stream_sequence"])
                            for selection in session.get("selections", []):
                                actual_target = selection.get("actual_target") or {}
                                if (actual_target.get("streamId") == row["stream_id"]
                                        and actual_target.get("roundId") == row["round_id"]):
                                    selection["target_persistence_status"] = "COMMITTED"
                            for selections in session.get("rule_selections", {}).values():
                                for selection in selections:
                                    actual_target = selection.get("actual_target") or {}
                                    if (actual_target.get("streamId") == row["stream_id"]
                                            and actual_target.get("roundId") == row["round_id"]):
                                        selection["target_persistence_status"] = "COMMITTED"
                            dirty_session_ids.add(str(session["session_id"]))
                            results.append(deepcopy(pending_prior))
                            continue
                prior = session.get("event_records", {}).get(record_key)
                if prior:
                    if prior.get("source_round") != _reference(row):
                        session["status"] = "PAUSED"
                        self._record_session_stop(session, "ROUND_IDENTITY_CONFLICT", at,
                            "ROUND_IDENTITY_VALIDATION", row, details={"event_key": record_key})
                        dirty_session_ids.add(str(session["session_id"]))
                    results.append(deepcopy(prior))
                    continue
                if observed_stage:
                    committed_prior = next((item for item in session.get("event_records", {}).values()
                        if item.get("persistence_status") == "COMMITTED"
                        and isinstance(item.get("source_round"), Mapping)
                        and item["source_round"].get("streamId") == row["stream_id"]
                        and item["source_round"].get("roundId") == row["round_id"]), None)
                    if committed_prior:
                        if committed_prior.get("source_round") != _reference(row):
                            session["status"] = "PAUSED"
                            self._record_session_stop(session, "ROUND_IDENTITY_CONFLICT", at,
                                "ROUND_IDENTITY_VALIDATION", row,
                                details={"event_key": "committed_round_replay"})
                            dirty_session_ids.add(str(session["session_id"]))
                        results.append(deepcopy(committed_prior))
                        continue
                if self._effective_session_status(session) == "COMPLETED":
                    # Do not keep treating a legacy full-window SETTLING row
                    # as an active observer after its verified quota is met.
                    continue
                if session.get("status") not in {"RUNNING", "SETTLING", "PAUSED", "MANUALLY_STOPPED"}:
                    continue
                legacy_gap_pause = (session["status"] == "PAUSED"
                    and session.get("stop_reason") == "ROUND_INDEX_GAP")
                gap_marker_active = (session.get("stop_reason") == "ROUND_INDEX_GAP"
                    or (int(session.get("gap_expected_index") or 0) > 0
                        and int(session.get("gap_received_index") or 0) > 0))
                manual_pause = (session.get("status") == "PAUSED"
                                and session.get("stop_reason") == "MANUAL_PAUSE")
                can_recover_gap_marker = (session.get("status") in {"RUNNING", "SETTLING"}
                    or legacy_gap_pause or manual_pause)
                if (session["status"] == "PAUSED"
                        and session.get("stop_reason") not in {None, "MANUAL_PAUSE"}
                        and not legacy_gap_pause):
                    continue
                if row["stream_id"] != session["last_stream_id"]:
                    session["status"] = "PAUSED"
                    self._record_session_stop(session, "STREAM_IDENTITY_CHANGED", at,
                        "STREAM_IDENTITY_VALIDATION", row,
                        details={"expected_stream_id": session.get("last_stream_id"),
                                 "received_stream_id": row["stream_id"]})
                    dirty_session_ids.add(str(session["session_id"]))
                    continue
                if not observed_stage and row["stream_sequence"] <= session["last_stream_sequence"]:
                    # A replay older than the persisted cursor is a no-op; no retrospective selections.
                    continue
                if gap_marker_active and committed_stage and can_recover_gap_marker:
                    # Older persisted sessions may already have a cursor beyond
                    # the collector's real round index. A newer committed
                    # delivery is the recovery boundary, even when its index
                    # is lower than that poisoned cursor.
                    gap_expected = int(session.get("gap_expected_index") or
                                       int(session.get("last_round_index") or 0) + 1)
                    self._resume_after_round_gap(session, row, at, gap_expected)
                    legacy_gap_pause = False
                    dirty_session_ids.add(str(session["session_id"]))
                expected_index = int(session["last_round_index"]) + 1
                if gap_marker_active and observed_stage:
                    # Hold the event until its canonical commit arrives; then
                    # it can safely become the first source in the new segment.
                    record = {"session_id": session["session_id"],
                              "source_round": _reference(row), "target_round": None,
                              "rule_evaluations": [],
                              "decision": {"action": "WAIT",
                                           "reason": "ROUND_INDEX_GAP_WAITING_FOR_COMMIT"},
                              "evaluated_at": at, "persistence_status": "PENDING",
                              "gap_recovery_pending": can_recover_gap_marker,
                              "gap_expected_index": int(session.get("gap_expected_index") or expected_index)}
                    session.setdefault("event_records", {})[record_key] = record
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                legacy_gap_received_index = int(session.get("gap_received_index") or 0)
                stale_round = (row["round_index"] < expected_index
                    or (legacy_gap_pause and legacy_gap_received_index
                        and row["round_index"] < legacy_gap_received_index))
                if stale_round:
                    # A late target can still settle above, but it must never
                    # rewind source progress or unpause a gap-paused session.
                    record = {"session_id": session["session_id"],
                              "source_round": _reference(row), "target_round": None,
                              "rule_evaluations": [],
                              "decision": {"action": "WAIT", "reason": "OUT_OF_ORDER_ROUND_IGNORED"},
                              "evaluated_at": at,
                              "persistence_status": "PENDING" if observed_stage else "COMMITTED"}
                    if committed_stage:
                        record["committed_stream_sequence"] = row["stream_sequence"]
                        record["commit_evidence"] = {
                            "event_type": event.get("event_type"),
                            "durability": event.get("durability"),
                            "continuity_status": event.get("continuity_status"),
                            "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                            "stream_id": row["stream_id"],
                            "stream_sequence": row["stream_sequence"],
                        }
                    session.setdefault("event_records", {})[record_key] = record
                    if committed_stage:
                        session["last_stream_sequence"] = max(
                            int(session.get("last_stream_sequence") or 0), row["stream_sequence"])
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                if row["round_index"] > expected_index:
                    details = {"expected_index": expected_index,
                               "received_index": int(row["round_index"])}
                    manual_pause = (session.get("status") == "PAUSED"
                                    and session.get("stop_reason") == "MANUAL_PAUSE")
                    can_recover_gap = (session.get("status") in {"RUNNING", "SETTLING"}
                                       or legacy_gap_pause or manual_pause)
                    if observed_stage or not can_recover_gap:
                        # A pending observation cannot move the durable cursor.
                        # Preserve the session and let the matching commit
                        # establish the fresh boundary before evaluation.
                        record = {"session_id": session["session_id"],
                                  "source_round": _reference(row), "target_round": None,
                                  "rule_evaluations": [],
                                  "decision": {"action": "WAIT", "reason": "ROUND_INDEX_GAP",
                                               **details},
                                  "evaluated_at": at,
                                  "persistence_status": "PENDING" if observed_stage else "COMMITTED",
                                  "gap_recovery_pending": bool(observed_stage and can_recover_gap),
                                  "gap_expected_index": expected_index}
                        if committed_stage:
                            record["committed_stream_sequence"] = row["stream_sequence"]
                            record["commit_evidence"] = {
                                "event_type": event.get("event_type"),
                                "durability": event.get("durability"),
                                "continuity_status": event.get("continuity_status"),
                                "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                                "stream_id": row["stream_id"],
                                "stream_sequence": row["stream_sequence"],
                            }
                        session.setdefault("event_records", {})[record_key] = record
                        dirty_session_ids.add(str(session["session_id"]))
                        results.append(record)
                        continue
                    self._resume_after_round_gap(session, row, at, expected_index)
                    dirty_session_ids.add(str(session["session_id"]))
                unresolved_late_target = False
                for pending_transition in session.get("late_target_transitions") or []:
                    if (not isinstance(pending_transition, Mapping)
                            or pending_transition.get("target_stream_id") != row.get("stream_id")):
                        continue
                    try:
                        unresolved_late_target = (
                            int(pending_transition.get("target_round_index") or 0)
                            <= int(row.get("round_index") or 0))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if unresolved_late_target:
                        break
                if unresolved_late_target:
                    # Do not allow any rule to bet from policy state that is
                    # waiting for a verified result from an earlier late target.
                    session["context"].append(deepcopy(row))
                    session["context"] = session["context"][-5:]
                    session["observed_rounds"] += 1
                    session.update({"last_round_index": row["round_index"],
                                    "last_round_id": row["round_id"]})
                    record = {"session_id": session["session_id"],
                              "source_round": _reference(row), "target_round": None,
                              "rule_evaluations": [],
                              "decision": {"action": "WAIT",
                                           "reason": "LATE_TARGET_RESULT_UNVERIFIED"},
                              "evaluated_at": at,
                              "persistence_status": "PENDING" if observed_stage else "COMMITTED"}
                    if committed_stage:
                        session["last_stream_sequence"] = row["stream_sequence"]
                        record["committed_stream_sequence"] = row["stream_sequence"]
                    session.setdefault("event_records", {})[record_key] = record
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                observed_at = _parse_time(row.get("observed_at"))
                started_at = _parse_time(session.get("started_at"))
                auto_next_start_round = session.get("auto_next_start_round_id")
                if (observed_stage and observed_at is not None and started_at is not None
                        and observed_at.tzinfo is not None and started_at.tzinfo is not None
                        and observed_at.astimezone(timezone.utc) <= started_at.astimezone(timezone.utc)
                        and row.get("round_id") != auto_next_start_round):
                    # The pending-priority path can reach Rules Engine before
                    # PostgreSQL. Preserve the old session boundary semantics:
                    # a round already visible when the session started may
                    # advance context, but can never produce a selection.
                    session["context"].append(deepcopy(row))
                    session["context"] = session["context"][-5:]
                    session["prestart_skipped_rounds"] = int(
                        session.get("prestart_skipped_rounds", 0)) + 1
                    session.update({"last_round_index": row["round_index"],
                                    "last_round_id": row["round_id"]})
                    record = {"session_id": session["session_id"],
                              "source_round": _reference(row), "target_round": _expected(row),
                              "rule_evaluations": [],
                              "decision": {"action": "WAIT", "reason": "PRESTART_ROUND"},
                              "evaluated_at": at, "persistence_status": "PENDING",
                              "prestart_only": True}
                    session.setdefault("event_records", {})[record_key] = record
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                if (committed_stage and observed_at is not None and started_at is not None
                        and observed_at.tzinfo is not None and started_at.tzinfo is not None
                        and observed_at.astimezone(timezone.utc) <= started_at.astimezone(timezone.utc)
                        and row.get("round_id") != auto_next_start_round):
                    # A PostgreSQL commit can trail the API observation inbox.
                    # Advance continuity and keep the outcome as history, but
                    # never evaluate a source that existed before this session.
                    session["context"].append(deepcopy(row))
                    session["context"] = session["context"][-5:]
                    session["prestart_skipped_rounds"] = int(
                        session.get("prestart_skipped_rounds", 0)) + 1
                    session.update({"last_stream_sequence": row["stream_sequence"],
                                    "last_round_index": row["round_index"],
                                    "last_round_id": row["round_id"]})
                    dirty_session_ids.add(str(session["session_id"]))
                    continue
                if session["status"] == "PAUSED" and session.get("stop_reason") == "MANUAL_PAUSE":
                    session["context"].append(deepcopy(row))
                    session["context"] = session["context"][-5:]
                    session["observed_rounds"] += 1
                    session.update({"status": "PAUSED", "last_stream_sequence": row["stream_sequence"],
                                    "last_round_index": row["round_index"], "last_round_id": row["round_id"]})
                    record = {"source_round": _reference(row), "rule_evaluations": [],
                              "decision": {"action": "WAIT", "reason": "MANUALLY_PAUSED"},
                              "evaluated_at": at}
                    session.setdefault("event_records", {})[record_key] = record
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                history = deepcopy(session["context"]) + [deepcopy(row)]
                session["observed_rounds"] += 1
                session["context"] = history[-5:]
                final_window_source = session["observed_rounds"] >= session["window_size"]
                active = (session["selection_status"] == "ACTIVE" and session["status"] == "RUNNING"
                          and not final_window_source and not target_already_completed)
                evaluation_rows: list[dict[str, Any]] = []
                proposals: list[dict[str, Any]] = []
                late_true_rule_ids: set[str] = set()
                for rid in session["rule_ids"]:
                    state = session["engines"][rid]
                    before = deepcopy(state)
                    proposal_started_ns = time.perf_counter_ns()
                    try:
                        proposal = self._proposal(session, rid, row, history, at)
                        if state.get("failure_count", 0):
                            state["failure_count"] = 0
                            state["recovery_count"] = int(state.get("recovery_count", 0)) + 1
                    except Exception as exc:
                        state["failure_count"] = int(state.get("failure_count", 0)) + 1
                        proposal = _result(rid, session["rule_versions"][rid], session, row, _expected(row), None,
                            "EVALUATION_ERROR", "Iri tegeko ntiryabashije gusuzuma amakuru.",
                            {"errorCategory": type(exc).__name__}, before, state, at)
                    proposal_elapsed_ms = (time.perf_counter_ns() - proposal_started_ns) / 1_000_000.0
                    if rid in {COMBINED_POLICY_ID, START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
                        policy_evaluation_ms += proposal_elapsed_ms
                    else:
                        standard_rule_evaluation_ms += proposal_elapsed_ms
                    proposal["enabled"] = True  # Session snapshot owns participation.
                    proposal["evaluation_id"] = hashlib.sha256("\0".join((
                        str(session["session_id"]), rid, str(row["stream_id"]),
                        str(row["round_index"]))).encode("utf-8")).hexdigest()
                    if target_already_completed and proposal.get("eligible") is True:
                        late_true_rule_ids.add(rid)
                        proposal["eligible"] = None
                        proposal["result"] = None
                        proposal["status"] = "TARGET_ALREADY_COMPLETED"
                        proposal["reason"] = "Target yamaze kuboneka; nta cyemezo cyasubijwe inyuma."
                        proposal["proposal"].update({"eligible": None, "status": "TARGET_ALREADY_COMPLETED",
                                                     "reason": proposal["reason"]})
                    if final_window_source and proposal.get("eligible") is not None:
                        proposal["eligible"] = None
                        proposal["result"] = None
                        proposal["status"] = "WINDOW_COMPLETED_NO_TARGET"
                        proposal["reason"] = "Idirishya rirangiye; nta target nshya yemewe."
                        proposal["proposal"].update({"eligible": None,
                                                     "status": "WINDOW_COMPLETED_NO_TARGET",
                                                     "reason": proposal["reason"]})
                    if rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
                        transition = proposal.get("proposal", {}).get("relevantInputs", {}).get("transition") or {}
                        if (transition.get("type") == "SPECIAL_SKIP_TERMINATE"
                                and proposal.get("status") != "TARGET_ALREADY_COMPLETED"):
                            # A skipped H closes only its owning policy cycle;
                            # it is verified source evidence, never a bet result.
                            session["engines"][rid].update(
                                deepcopy(proposal["state_snapshot"]["after"]))
                        elif (transition.get("type") == "SPECIAL_SKIP_LOSS_TERMINAL_ROUND"
                              and proposal.get("status") != "TARGET_ALREADY_COMPLETED"):
                            session["engines"][rid]["special_skip_terminal_round_index"] = None
                        elif proposal.get("eligible") is False:
                            # Waiting-state transitions are owned by this policy
                            # and depend only on its verified source, not aggregation.
                            session["engines"][rid]["rule_state"] = proposal["state_snapshot"]["after"].get("rule_state")
                    evaluation_rows.append(proposal)
                    if active:
                        proposals.append(proposal)
                target_identity_guard = ("TARGET_ALREADY_COMPLETED" if target_already_completed
                                          else "NOT_APPLICABLE")
                target_guard_unavailable = False
                late_target_row: dict[str, Any] | None = None
                target_index = int(row["round_index"]) + 1
                checker = getattr(self._persistence, "round_continuity_for_indices", None)
                observation_checker = getattr(self.observation_inbox,
                                              "has_pending_round_index", None)
                pending_target_reader = getattr(self.observation_inbox,
                                                "pending_round_for_index", None)
                target_guard_started_ns = time.perf_counter_ns()
                if active and any(proposal.get("eligible") is True for proposal in proposals):
                    if target_already_completed:
                        target_identity_guard = "TARGET_ALREADY_COMPLETED"
                    else:
                        try:
                            existing_target = (checker([target_index])
                                               if callable(checker) else {})
                            already_observed = (bool(observation_checker(target_index))
                                                if callable(observation_checker) else False)
                        except Exception:
                            existing_target = None
                            already_observed = False
                            target_guard_unavailable = True
                            target_identity_guard = "TARGET_IDENTITY_CHECK_UNAVAILABLE"
                        else:
                            if existing_target and target_index in existing_target:
                                target_already_completed = True
                                target_identity_guard = "TARGET_ALREADY_COMPLETED"
                                late_target_row = self._persisted_late_target(
                                    row, target_index, existing_target[target_index])
                            elif already_observed:
                                target_already_completed = True
                                target_identity_guard = "TARGET_ALREADY_OBSERVED"
                                pending_target = (pending_target_reader(target_index)
                                                  if callable(pending_target_reader) else None)
                                late_target_row = self._observed_late_target(
                                    row, target_index, pending_target)
                            elif callable(checker) or callable(observation_checker):
                                target_identity_guard = "NO_COMPLETED_TARGET_FOUND"
                elif target_already_completed:
                    target_identity_guard = "TARGET_ALREADY_COMPLETED"
                if target_already_completed and late_target_row is None:
                    # A caller may already know the target is late. Recover its
                    # verified result from canonical history or the observation
                    # inbox when available, so private rule state can catch up.
                    try:
                        existing_target = (checker([target_index])
                                           if callable(checker) else {})
                    except Exception:
                        existing_target = {}
                    if existing_target and target_index in existing_target:
                        late_target_row = self._persisted_late_target(
                            row, target_index, existing_target[target_index])
                    if late_target_row is None and callable(pending_target_reader):
                        try:
                            pending_target = pending_target_reader(target_index)
                        except Exception:
                            pending_target = None
                        late_target_row = self._observed_late_target(
                            row, target_index, pending_target)
                target_identity_check_ms += (time.perf_counter_ns() - target_guard_started_ns) / 1_000_000.0
                if target_already_completed or target_guard_unavailable:
                        reason = ("Target yamaze kuboneka; nta cyemezo cyasubijwe inyuma."
                                  if target_already_completed else
                                  "Target ntiyashoboye kugenzurwa; icyemezo kirahagaritswe.")
                        status = ("TARGET_ALREADY_COMPLETED" if target_already_completed
                                  else "UNAVAILABLE_TARGET_IDENTITY")
                        for proposal in evaluation_rows:
                            if proposal.get("eligible") is not True:
                                continue
                            if target_already_completed:
                                late_true_rule_ids.add(str(proposal.get("rule_id") or ""))
                            proposal.update({"eligible": None, "result": None,
                                             "status": status, "reason": reason})
                            proposal.setdefault("proposal", {}).update({
                                "eligible": None, "status": status, "reason": reason,
                            })
                if target_already_completed and late_true_rule_ids:
                    for proposal in evaluation_rows:
                        if (proposal.get("status") != "TARGET_ALREADY_COMPLETED"
                                or str(proposal.get("rule_id") or "") not in late_true_rule_ids):
                            continue
                        if late_target_row is not None:
                            resolution = self._apply_late_target_to_rule_state(
                                session, proposal, late_target_row, at)
                        else:
                            resolution = None
                        if resolution is None:
                            resolution = {"status": "WAITING_FOR_VERIFIED_TARGET_RESULT",
                                          "target_round_index": target_index,
                                          "actual_bet_placed": False}
                            session.setdefault("late_target_transitions", []).append({
                                "source_round": _reference(row),
                                "target_stream_id": row["stream_id"],
                                "target_round_index": target_index,
                                "rule_id": proposal["rule_id"],
                                "queued_at": at,
                                "proposal": deepcopy(proposal),
                            })
                            proposal["late_target_resolution"] = deepcopy(resolution)
                            proposal.setdefault("proposal", {}).setdefault("relevantInputs", {})[
                                "lateTargetResolution"] = deepcopy(resolution)
                        dirty_session_ids.add(str(session["session_id"]))
                aggregation_started_ns = time.perf_counter_ns()
                if active and session["mode"] in {"FIVE_RULE_TRACKER", "ALL_RULES_TOGETHER"}:
                    combined = self._combine(session, row, proposals, at)
                elif active and session["mode"] in {"START_CHECK", "LEGACY_SEPARATE_TRACKER"}:
                    combined = self._combine(session, row, proposals, at)
                else:
                    wait_reason = ("TARGET_ALREADY_COMPLETED" if target_already_completed else
                                   "TARGET_IDENTITY_CHECK_UNAVAILABLE" if target_guard_unavailable else
                                   "WINDOW_COMPLETED_NO_TARGET" if final_window_source else
                                   session.get("selection_stop_reason") or "NO_ACTIVE_RULES")
                    combined = {"action": "NO_ACTION" if not session.get("rule_ids") else "WAIT", "reason":
                                "NO_ENABLED_RULES" if not session.get("rule_ids") else wait_reason,
                                "true_rule_ids": [], "selection_id": None}
                decision_aggregation_ms += (time.perf_counter_ns() - aggregation_started_ns) / 1_000_000.0
                if target_guard_unavailable:
                    combined = {"action": "WAIT", "reason": "TARGET_IDENTITY_CHECK_UNAVAILABLE",
                                "true_rule_ids": [], "selection_id": None}
                if session["observed_rounds"] >= session["window_size"]:
                    session["status"] = "COMPLETED"
                    session["ended_at"] = at
                    self._record_session_stop(session, "WINDOW_COMPLETED", at,
                        "WINDOW_COMPLETION", row)
                    if session["selection_status"] == "ACTIVE":
                        self._stop_selecting(session, "WINDOW_COMPLETED", at, row,
                                             stop_source="WINDOW_COMPLETION")
                    if not session.get("auto_next_session_id") and not session.get("auto_next_armed"):
                        session["auto_next_armed"] = bool(
                            session["configuration_snapshot"].get("auto_next_check_enabled"))
                        if session["auto_next_armed"]:
                            session.update({"auto_next_trigger": "WINDOW_COMPLETED",
                                           "auto_next_armed_at": at,
                                           "auto_next_after_round_id": row.get("round_id"),
                                           "auto_next_boundary": None,
                                           "auto_next_context": deepcopy(session.get("context") or []),
                                           "auto_next_block_reason": None})
                            if committed_stage:
                                self._record_auto_next_boundary(session, row)
                if row.get("round_id") == auto_next_start_round:
                    session["auto_next_start_round_id"] = None
                session.update({"last_stream_sequence": row["stream_sequence"] if not observed_stage else session["last_stream_sequence"],
                                "last_round_index": row["round_index"], "last_round_id": row["round_id"]})
                dirty_session_ids.add(str(session["session_id"]))
                record = {"session_id": session["session_id"],
                          "source_round": _reference(row), "target_round": _expected(row),
                          "rule_evaluations": evaluation_rows, "decision": combined, "evaluated_at": at,
                          "persistence_status": "PENDING" if observed_stage else "COMMITTED",
                          "target_identity_guard": target_identity_guard,
                          "commit_evidence": {
                              "event_type": event.get("event_type"),
                              "durability": event.get("durability"),
                              "continuity_status": event.get("continuity_status"),
                              "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                              "stream_id": row["stream_id"],
                              "stream_sequence": row["stream_sequence"],
                          }}
                if target_already_completed:
                    late_reason = (target_identity_guard if target_identity_guard in {
                        "TARGET_ALREADY_COMPLETED", "TARGET_ALREADY_OBSERVED"} else "TARGET_ALREADY_COMPLETED")
                    record["decision"] = {"action": "WAIT", "reason": late_reason,
                                           "true_rule_ids": [], "selection_id": None}
                shared_action = ("SELECT" if record["decision"].get("action") == "SELECT"
                                 else "NO_SELECT" if evaluation_rows else "NOT_EVALUATED")
                shared_decision = {
                    "decision_id": f"{session['session_id']}:{row['stream_id']}:{row['stream_sequence']}:shared",
                    "session_id": session["session_id"],
                    "source_round": _reference(row),
                    "target_round": _expected(row),
                    "action": shared_action,
                    "reason": record["decision"].get("reason"),
                    "true_rule_ids": list(record["decision"].get("true_rule_ids") or []),
                    "selection_id": record["decision"].get("selection_id"),
                    "evaluated_at": at,
                    "persistence_status": record["persistence_status"],
                }
                record["shared_decision"] = shared_decision
                session.setdefault("event_records", {})[record_key] = record
                if committed_stage:
                    session["event_records"][committed_key] = record
                session["evaluations"].append(deepcopy(record))
                session["evaluations"] = session["evaluations"][-500:]
                session.setdefault("decisions", []).append(deepcopy(shared_decision))
                session["decisions"] = session["decisions"][-500:]
                self.evaluation_count += len(evaluation_rows)
                results.append(record)
            actions = [item.get("decision", {}).get("action") for item in results]
            if len(results) == 1:
                decision = actions[0]
                selected_rule_ids = list(results[0].get("decision", {}).get("true_rule_ids", []))
            elif results:
                decision = "SESSION_SCOPED"
                selected_rule_ids = []
            else:
                decision = "NO_ACTION"
                selected_rule_ids = []
            output = {"source_round": _reference(row), "target_round": _expected(row),
                      "decision": decision, "result": decision == "SELECT",
                      "true_rule_ids": selected_rule_ids,
                      "sessions": results, "automatic_execution": "OFF", "can_bet": False,
                      "evaluated_at": at,
                      "target_settlements": [item for rows in settlements_by_session.values()
                                             for item in rows],
                      "event_timings_ms": {
                          "target_settlement_processing_ms": (
                              round(settlement_processing_ms, 3) if settlements_by_session else None),
                          "target_settlement_durable_ms": None,
                      }}
            self.last_evaluation = output
            self.last_evaluation_at = at
            pre_persist_processing_ms = (time.perf_counter_ns() - processing_started_ns) / 1_000_000.0
            for session in self.sessions.values():
                if str(session.get("session_id")) not in dirty_session_ids:
                    continue
                received = session.get("last_received_round") or {}
                if (received.get("stream_id") == row["stream_id"]
                        and received.get("round_id") == row["round_id"]):
                    session["last_event_processing_latency_ms"] = round(pre_persist_processing_ms, 3)
                    if session["session_id"] in settlements_by_session:
                        session["last_target_settlement_processing_latency_ms"] = round(
                            settlement_processing_ms, 3)
            for result in results:
                result["timings_ms"] = {
                    "target_settlement_processing_ms": (
                        round(settlement_processing_ms, 3)
                        if result.get("session_id") in settlements_by_session else None),
                    "rules_engine_processing_ms": round(pre_persist_processing_ms, 3),
                }
            for session_id in dirty_session_ids:
                session = self.sessions.get(str(session_id))
                if session is not None:
                    self._trim_committed_event_records(session, keep=100)
            if defer_persistence:
                # The caller's durable observation inbox is the recovery
                # record. A separate ordered writer checkpoints state after
                # urgent signal dispatch, without holding this worker or the
                # rules lock on PostgreSQL latency.
                persistence_timings = {
                    "snapshot_serialization_ms": None,
                    "database_persistence_ms": None,
                    "persistence_total_ms": None,
                    "persistence_deferred": True,
                }
                self._persistence_generation += 1
                output["_deferred_persistence_session_ids"] = sorted(dirty_session_ids)
            else:
                persistence_timings = self._persist(session_ids=dirty_session_ids) or persistence_timings
            total_processing_ms = (time.perf_counter_ns() - processing_started_ns) / 1_000_000.0
            durable_settlement_ms = ((time.perf_counter_ns() - settlement_started_ns) / 1_000_000.0
                                     if settlements_by_session else None)
            output["event_timings_ms"]["rules_engine_processing_ms"] = round(total_processing_ms, 3)
            output["event_timings_ms"].update({
                "rules_engine_lock_wait_ms": round(lock_wait_ms, 3),
                "user_playing_policy_evaluation_ms": round(policy_evaluation_ms, 3),
                "standard_rule_evaluation_ms": round(standard_rule_evaluation_ms, 3),
                "selected_result_evaluation_ms": round(selected_result_evaluation_ms, 3),
                "two_bet_wins_transition_ms": round(two_bet_wins_transition_ms, 3),
                "target_identity_check_ms": round(target_identity_check_ms, 3),
                "decision_aggregation_ms": round(decision_aggregation_ms, 3),
                **persistence_timings,
            })
            if durable_settlement_ms is not None:
                output["event_timings_ms"]["target_settlement_durable_ms"] = round(durable_settlement_ms, 3)
            event_metric_id = (str(event.get("observation_id") or event.get("round_id") or "")
                               + ":" + str(event.get("event_type") or "unknown"))
            if event_metric_id:
                self._record_delivery_metric("rules_engine_processing_ms", event_metric_id,
                                             total_processing_ms)
                for stage in ("rules_engine_lock_wait_ms", "user_playing_policy_evaluation_ms",
                              "standard_rule_evaluation_ms", "selected_result_evaluation_ms",
                              "two_bet_wins_transition_ms", "target_identity_check_ms",
                              "decision_aggregation_ms", "snapshot_serialization_ms",
                              "database_persistence_ms"):
                    if defer_persistence and stage in {
                            "snapshot_serialization_ms", "database_persistence_ms"}:
                        continue
                    self._record_delivery_metric(
                        stage, event_metric_id,
                        float(output["event_timings_ms"].get(stage) or 0.0))
                if settlements_by_session:
                    self._record_delivery_metric("target_settlement_processing_ms", event_metric_id,
                                                 settlement_processing_ms)
                    self._record_delivery_metric("target_settlement_durable_ms", event_metric_id,
                                                 durable_settlement_ms or 0.0)
            return deepcopy(output)

    def has_round_event_consumers(self) -> bool:
        """Return whether any persisted session can consume or settle a round.

        The collector also forwards rounds while no SHADOW session is active.
        Persisting a no-op engine snapshot for every such round made its
        latency grow with the round-event backlog and delayed ordinary API
        actions. This check avoids that write while retaining paused,
        settling, and terminal sessions that still have pending targets.
        """
        with self._lock:
            return any(
                self._session_accepts_source_events(session)
                or self._session_accepts_settlement_events(session)
                for session in self.sessions.values()
            )

    def _spawn_auto_next_sessions(self, current: dict[str, Any], at: str) -> set[str]:
        changed_session_ids: set[str] = set()
        for previous in list(self.sessions.values()):
            if not previous.get("auto_next_armed"):
                continue
            trigger = previous.get("auto_next_trigger")
            completed_window = previous.get("status") == "COMPLETED" and (
                trigger in {None, "WINDOW_COMPLETED"}
                and previous.get("configuration_snapshot", {}).get("auto_next_check_enabled") is True)
            stopped_at_limit = (previous.get("status") in {"SETTLING", "COMPLETED"}
                and trigger == "LIMIT_REACHED"
                and previous.get("configuration_snapshot", {}).get("auto_next_on_limit_enabled") is True
                and previous.get("selection_stop_source") in {
                    "PROFIT_TARGET", "NET_OUTCOME_LIMIT", "MAX_SELECTIONS"}
                and previous.get("selection_stop_reason") in {
                    "PROFIT_TARGET_REACHED", "MAX_SELECTIONS_REACHED",
                    *NET_OUTCOME_LIMIT_REASONS})
            if not (completed_window or stopped_at_limit):
                continue
            active_peer = any(item is not previous
                              and item.get("mode") == previous.get("mode")
                              and item.get("last_stream_id") == current["stream_id"]
                              and item.get("status") in {"RUNNING", "SETTLING", "PAUSED"}
                              and item.get("selection_status") == "ACTIVE"
                              for item in self.sessions.values())
            if active_peer:
                previous["auto_next_armed"] = False
                previous["auto_next_armed_at"] = None
                previous["auto_next_block_reason"] = "SESSION_ALREADY_ACTIVE_FOR_MODE"
                changed_session_ids.add(str(previous["session_id"]))
                continue
            # Capture the exact committed event that completed the old window.
            if current.get("round_id") == previous.get("auto_next_after_round_id"):
                self._record_auto_next_boundary(previous, current)
                if isinstance(previous.get("auto_next_boundary"), Mapping):
                    changed_session_ids.add(str(previous["session_id"]))
                continue
            boundary = previous.get("auto_next_boundary")
            context = previous.get("auto_next_context")
            valid_boundary = (isinstance(boundary, Mapping)
                and boundary.get("event_type") == "round.persisted"
                and boundary.get("durability") == "COMMITTED"
                and boundary.get("continuity_status") == "VERIFIED"
                and int(boundary.get("stream_sequence") or 0) > 0
                and boundary.get("round_id") == previous.get("auto_next_after_round_id"))
            if not valid_boundary:
                # If the saved completion boundary was lost, rebase the
                # successor on this later verified commit. Its history starts
                # with this round, so missing older context cannot block it.
                try:
                    current_index = int(current.get("round_index"))
                    previous_index = int(previous.get("last_round_index"))
                    current_sequence = int(current.get("stream_sequence"))
                except (TypeError, ValueError, OverflowError):
                    current_index = previous_index = current_sequence = 0
                can_rebase = (current.get("event_type") == "round.persisted"
                    and current.get("durability") == "COMMITTED"
                    and current.get("continuity_status") == "VERIFIED"
                    and current.get("stream_id") == previous.get("last_stream_id")
                    and current_index > previous_index and current_sequence > 0)
                if can_rebase:
                    previous["auto_next_boundary"] = None
                    previous["auto_next_context"] = [deepcopy(current)]
                    self._record_auto_next_boundary(previous, current)
                    previous["auto_next_block_reason"] = None
                    changed_session_ids.add(str(previous["session_id"]))
                    continue
                previous["auto_next_block_reason"] = "AUTO_NEXT_COMMITTED_BOUNDARY_UNAVAILABLE"
                changed_session_ids.add(str(previous["session_id"]))
                continue
            if (current["stream_id"] != boundary.get("stream_id")
                    or current["round_index"] <= boundary.get("round_index", 0)
                    or current["stream_sequence"] <= boundary.get("stream_sequence", 0)):
                continue
            if (not isinstance(context, list) or not context
                    or context[-1].get("stream_id") != boundary.get("stream_id")
                    or context[-1].get("round_id") != boundary.get("round_id")):
                previous["auto_next_armed"] = False
                previous["auto_next_armed_at"] = None
                previous["auto_next_block_reason"] = "AUTO_NEXT_CONTEXT_UNAVAILABLE"
                changed_session_ids.add(str(previous["session_id"]))
                continue
            session_id = str(uuid4())
            rule_ids = list(previous["rule_ids"])
            clone = {
                "session_id": session_id, "mode": previous["mode"], "status": "RUNNING",
                "selection_status": "ACTIVE", "started_at": at, "ended_at": None,
                "window_size": previous["window_size"],
                "configuration_version": previous["configuration_version"],
                "configuration_snapshot": deepcopy(previous["configuration_snapshot"]),
                "rule_ids": rule_ids, "rule_versions": deepcopy(previous["rule_versions"]),
                "manifest": deepcopy(previous["manifest"]),
                "start_boundary": _reference(boundary) | {"streamSequence": boundary["stream_sequence"]},
                "last_stream_id": boundary["stream_id"], "last_stream_sequence": boundary["stream_sequence"],
                "last_round_index": boundary["round_index"], "last_round_id": boundary["round_id"],
                "context": deepcopy(context[-5:]),
                "engines": {rid: _fresh_engine(rid) for rid in rule_ids}, "observed_rounds": 0,
                "prestart_skipped_rounds": 0,
                "selections": [], "rule_selections": {rid: [] for rid in rule_ids},
                "evaluations": [], "decisions": [], "total_wins": 0, "total_losses": 0,
                "net_profit_rwf": 0, "selection_stop_reason": None, "stop_reason": None,
                "reconciled_financials": {"realized_betting_pl": None, "currency": None,
                                           "win_count": 0, "loss_count": 0,
                                           "reconciled_bets": 0, "reconciled_execution_ids": []},
                "auto_next_armed": False, "auto_next_trigger": None,
                "auto_next_armed_at": None, "auto_next_block_reason": None,
                "auto_next_boundary": None, "auto_next_context": [],
                "auto_next_start_round_id": current["round_id"],
                "automatic_execution": "OFF",
            }
            previous["auto_next_armed"] = False
            previous["auto_next_started_at"] = at
            previous["auto_next_session_id"] = session_id
            self.sessions[session_id] = clone
            changed_session_ids.update((str(previous["session_id"]), session_id))
        return changed_session_ids

    def _combine(self, session: dict[str, Any], source: dict[str, Any],
                 proposals: list[dict[str, Any]], at: str) -> dict[str, Any]:
        true_rows = [row for row in proposals if row.get("eligible") is True]
        expected_source, expected_target = _reference(source), _expected(source)
        if any(row.get("proposal", {}).get("sourceRoundRef") != expected_source
               or row.get("proposal", {}).get("expectedTargetRef") != expected_target for row in true_rows):
            return {"action": "BLOCKED", "reason": "TARGET_MISMATCH", "true_rule_ids": [], "selection_id": None}
        config = session["configuration_snapshot"]
        if session["selection_status"] != "ACTIVE":
            return {"action": "WAIT", "reason": session.get("selection_stop_reason") or "SELECTIONS_STOPPED",
                    "true_rule_ids": [], "selection_id": None}
        if not true_rows:
            if not session.get("rule_ids"):
                return {"action": "NO_ACTION", "reason": "NO_ENABLED_RULES", "true_rule_ids": [], "selection_id": None}
            return {"action": "WAIT", "reason": "NO_TRUE_PROPOSALS", "true_rule_ids": [], "selection_id": None}
        if config.get("max_selections") and len(session["selections"]) >= config["max_selections"]:
            self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source,
                                 stop_source="MAX_SELECTIONS")
            return {"action": "WAIT", "reason": "MAX_SELECTIONS_REACHED", "true_rule_ids": [], "selection_id": None}
        selected_rule_ids = [proposal["rule_id"] for proposal in true_rows]
        selection_id = _stable_selection_id(session, source, selected_rule_ids)
        contributors = [{"rule_id": proposal["rule_id"], "rule_version": proposal["rule_version"],
                         "reason": proposal["reason"], "proposal": deepcopy(proposal["proposal"])}
                        for proposal in true_rows]
        selection = {"selection_id": selection_id, "session_id": session["session_id"],
                     "decision_id": selection_id,
                     "source_round": _reference(source), "expected_target": _expected(source),
                     "source_round_id": source["round_id"], "target_round_id": None,
                     "contributing_rule_ids": [row["rule_id"] for row in true_rows],
                     "matched_rule_ids": [row["rule_id"] for row in true_rows],
                     "contributors": contributors, "selected_at": at, "status": "PENDING", "result": "PENDING",
                     "settlement_status": "PENDING",
                     "actual_multiplier": None, "actual_target": None, "resolved_at": None,
                     "settled_at": None, "settlement_evidence": None,
                     "stake_rwf": config.get("fixed_stake_rwf", 100),
                     "payout_multiplier": 2.0, "payout_rwf": None, "profit_loss_rwf": None}
        target = selection["expected_target"]
        panel = config.get("execution_panel", 0)
        intent_id = hashlib.sha256(
            f"{target['streamId']}\0{target['roundIndex']}\0{panel}".encode("utf-8")
        ).hexdigest()
        requested_stake = config.get("requested_stake")
        if requested_stake is None:
            requested_stake = config.get("fixed_stake_rwf", selection["stake_rwf"])
        selection["execution_intent"] = {
            "intent_id": intent_id,
            "decision_id": selection_id,
            "source_round_id": selection["source_round"]["roundId"],
            "target_round_id": target.get("roundId"),
            "target_stream_id": target["streamId"],
            "target_round_index": target["roundIndex"],
            "selected_rule_ids": list(selection["contributing_rule_ids"]),
            "requested_stake": requested_stake,
            "requested_stake_currency": config.get("stake_currency"),
            "cashout_target": 2.0,
            "created_at": at,
            "expires_at": target.get("expiresAt"),
            "session_id": session["session_id"],
            "panel": panel,
            "execution_status": "BLOCKED_READ_ONLY",
            "blockers": [
                "REAL_MONEY_EXECUTION_DISABLED",
                "EXPLICIT_SESSION_AUTHORIZATION_REQUIRED",
                "TARGET_ROUND_ID_UNVERIFIED",
                "BETTING_DEADLINE_UNVERIFIED",
                "BET_AMOUNT_CURRENCY_UNVERIFIED" if not config.get("stake_currency") else
                    "STAKE_CURRENCY_NOT_RECONCILED",
                "CASHOUT_PLATFORM_ACKNOWLEDGEMENT_UNVERIFIED",
                "PLATFORM_ACCEPTANCE_AND_SETTLEMENT_UNVERIFIED",
            ],
        }
        session["selections"].append(selection)
        for proposal in true_rows:
            self._commit_contributor(session, proposal, selection)
        max_selections = config.get("max_selections")
        if (session.get("mode") == "ALL_RULES_TOGETHER"
                and isinstance(max_selections, int) and not isinstance(max_selections, bool)
                and len(session["selections"]) >= max_selections):
            self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source,
                                 stop_source="MAX_SELECTIONS")
            self._arm_auto_next_after_limit(
                session, session.get("selection_stop_reason"), at, source)
        return {"action": "SELECT", "reason": "ONE_OR_MORE_RULES_TRUE", "selection_id": selection_id,
                "true_rule_ids": list(selection["contributing_rule_ids"]),
                "target_round": deepcopy(selection["expected_target"]),
                "contributor_reasons": [{"rule_id": row["rule_id"], "reason": row["reason"]} for row in true_rows]}

    def _separate(self, session: dict[str, Any], source: dict[str, Any],
                  proposals: list[dict[str, Any]], at: str) -> dict[str, Any]:
        true_rows = [row for row in proposals if row.get("eligible") is True]
        selected: list[str] = []
        selection_ids: list[str] = []
        expected_source, expected_target = _reference(source), _expected(source)
        if any(row.get("proposal", {}).get("sourceRoundRef") != expected_source
               or row.get("proposal", {}).get("expectedTargetRef") != expected_target for row in true_rows):
            return {"action": "BLOCKED", "reason": "TARGET_MISMATCH", "true_rule_ids": [], "selection_id": None}
        for proposal in true_rows:
            rid = proposal["rule_id"]
            selection_id = _stable_selection_id(session, source, [rid])
            selection = {"selection_id": selection_id, "session_id": session["session_id"],
                         "rule_id": rid, "rule_version": proposal["rule_version"],
                         "source_round": _reference(source), "expected_target": _expected(source),
                         "reason": proposal["reason"], "selected_at": at,
                         "status": "PENDING", "result": "PENDING", "actual_multiplier": None,
                         "actual_target": None, "resolved_at": None}
            session["rule_selections"][rid].append(selection)
            self._commit_contributor(session, proposal, selection)
            selected.append(rid)
            selection_ids.append(selection_id)
        return {"action": "SELECT" if selected else ("WAIT" if session.get("rule_ids") else "NO_ACTION"),
                "reason": "INDEPENDENT_TRACKER_SELECTIONS" if selected else "NO_TRUE_PROPOSALS",
                "true_rule_ids": selected, "selection_ids": selection_ids}

    def _start_check_summary(self, session: dict[str, Any]) -> dict[str, Any]:
        rid = START_CHECK_POLICY_ID
        state = session.get("engines", {}).get(rid, {})
        return {"state": state.get("rule_state", "WAIT_LOW"), "mode": state.get("mode", "STANDARD"),
                "failed_cycles": state.get("failed_cycle_count", 0),
                "selected_wins": state.get("selected_wins", 0), "selected_losses": state.get("selected_losses", 0)}

    def session_action(self, session_id: str, action: str, *, now: str | None = None) -> dict[str, Any]:
        at = _now(now)
        if action not in {"pause", "resume", "stop", "reset", "emergency_stop"}:
            raise ValueError("INVALID_SESSION_ACTION")
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                raise KeyError("SESSION_NOT_FOUND")
            if action == "pause" and session["status"] == "RUNNING":
                session.update({"status": "PAUSED", "paused_at": at})
                self._record_session_stop(session, "MANUAL_PAUSE", at, "MANUAL_OPERATOR")
            elif action == "resume" and session["status"] == "PAUSED" and session.get("stop_reason") == "MANUAL_PAUSE":
                session.update({"status": "RUNNING", "stop_reason": None, "resumed_at": at})
            elif (action == "resume" and session["status"] == "PAUSED"
                  and session.get("stop_reason") == "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION"):
                has_open = getattr(self._persistence, "has_open_rule_session_executions", None)
                if callable(has_open) and has_open(session_id):
                    raise ValueError("UNRECONCILED_EXECUTION_REMAINS")
                session.update({"status": "RUNNING", "stop_reason": None,
                                "selection_status": "ACTIVE", "selection_stop_reason": None,
                                "unresolved_execution_id": None,
                                "unresolved_execution_reason": None,
                                "resumed_at": at})
            elif action in {"stop", "emergency_stop"}:
                reason = "EMERGENCY_STOP" if action == "emergency_stop" else "MANUAL_STOP"
                prior_reason = session.get("selection_stop_reason") or session.get("stop_reason")
                if prior_reason and prior_reason not in {"MANUAL_PAUSE", "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION", reason}:
                    session["integrity_stop_reason"] = prior_reason
                session.update({"status": "MANUALLY_STOPPED", "ended_at": at,
                                "auto_next_armed": False, "auto_next_trigger": None,
                                "auto_next_armed_at": None})
                self._record_session_stop(session, reason, at,
                    "EMERGENCY_STOP_CONTROL" if action == "emergency_stop" else "MANUAL_STOP_CONTROL",
                    stop_selection=True)
            elif action == "reset":
                session.update({"status": "RESET", "ended_at": at,
                                "auto_next_armed": False, "auto_next_trigger": None,
                                "auto_next_armed_at": None})
                self._record_session_stop(session, "RESET", at, "MANUAL_RESET_CONTROL",
                                          stop_selection=True)
            else:
                raise ValueError("INVALID_SESSION_TRANSITION")
            self._persist(session_ids={str(session_id)})
            return deepcopy(session)

    def close_for_reconfiguration(self, session_id: str, new_session_id: str,
                                  confirmation: str, *, now: str | None = None) -> dict[str, Any]:
        """Archive the old configuration after an explicit operator-confirmed replacement."""
        if confirmation != f"RECONFIGURE SESSION {session_id}":
            raise ValueError("OPERATOR_CONFIRMATION_REQUIRED")
        at = _now(now)
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError("SESSION_NOT_FOUND")
            if session.get("status") not in {"RUNNING", "SETTLING", "PAUSED"}:
                raise ValueError("RULES_SESSION_NOT_ACTIVE")
            session.update({"status": "RECONFIGURED", "ended_at": at,
                            "auto_next_armed": False, "auto_next_trigger": None,
                            "auto_next_armed_at": None,
                            "reconfigured_to_session_id": str(new_session_id)})
            self._record_session_stop(session, "RECONFIGURED", at,
                "OPERATOR_CONFIRMED_RECONFIGURATION", stop_selection=True,
                details={"new_session_id": str(new_session_id),
                         "operator_confirmation": True,
                         "confirmed_at": at})
            self._persist(session_ids={str(session_id)})
            return deepcopy(session)

    def session(self, session_id: str) -> dict[str, Any] | None:
        # Session snapshots are read by HTTP and collector callback threads
        # while observe_round mutates the same ledgers. Copy under the engine
        # lock so a concurrent update cannot produce a torn view or make
        # deepcopy fail with "dictionary changed size during iteration".
        with self._lock:
            value = self.sessions.get(session_id)
            if not value:
                return None
            result = deepcopy(value)
            summary = self._session_summary(result)
            result.update({"total_wins": summary["results"]["WIN"],
                           "total_losses": summary["results"]["LOSS"],
                           "status": summary["status"],
                           "net_outcome": summary["net_outcome"],
                           "results": deepcopy(summary["results"]),
                           "selection_count": summary["selection_count"],
                           "stop_reason": summary["stop_reason"],
                           "stop_reason_source": summary["stop_reason_source"],
                           "stop_reason_at": summary["stop_reason_at"],
                           "stop_reason_round_id": summary["stop_reason_round_id"],
                           "stop_reason_details": deepcopy(summary["stop_reason_details"]),
                           "stop_history": deepcopy(summary["stop_history"])})
            return result

    def signal_validation_session(self, session_id: str | None = None) -> dict[str, Any] | None:
        """Return only the active-session fields needed to validate a signal.

        The public status/detail methods build settlement summaries and deep
        copy the bounded history. Signal validation only needs session identity,
        status, and selected rule IDs, so it should not copy that history on the
        urgent decision path.
        """
        with self._lock:
            active = self._selected_session()
            if (active is None or self._effective_session_status(active) != "RUNNING"
                    or active.get("selection_status") != "ACTIVE"):
                return None
            active_id = str(active.get("session_id") or "")
            if session_id is not None and active_id != str(session_id):
                return None
            return {"session_id": active_id, "mode": active.get("mode"),
                    "status": "RUNNING", "selection_status": "ACTIVE",
                    "rule_ids": list(active.get("rule_ids") or [])}

    def signal_selection_context(self, session_id: str, selection_id: str) -> dict[str, Any] | None:
        """Copy only the selected decision and identity fields for publication."""
        with self._lock:
            session = self.sessions.get(str(session_id))
            if session is None:
                return None
            selection = next((row for row in session.get("selections", [])
                              if isinstance(row, Mapping)
                              and str(row.get("selection_id") or row.get("decision_id") or "")
                                  == str(selection_id)), None)
            if selection is None:
                for rows in (session.get("rule_selections") or {}).values():
                    if not isinstance(rows, list):
                        continue
                    selection = next((row for row in rows if isinstance(row, Mapping)
                                      and str(row.get("selection_id") or row.get("decision_id") or "")
                                          == str(selection_id)), None)
                    if selection is not None:
                        break
            if selection is None:
                return None
            return {"session_id": str(session.get("session_id") or ""),
                    "mode": session.get("mode"),
                    "status": self._effective_session_status(session),
                    "selection_status": session.get("selection_status"),
                    "last_round_index": session.get("last_round_index"),
                    "rule_ids": list(session.get("rule_ids") or []),
                    "selection": deepcopy(selection)}

    def delete_session(self, session_id: str, *, now: str | None = None) -> dict[str, Any]:
        """Delete a finished shadow session while retaining execution/audit records."""
        at = _now(now)
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                raise KeyError("SESSION_NOT_FOUND")
            if self._effective_session_status(session) not in {
                    "COMPLETED", "SETTLING", "PAUSED", "MANUALLY_STOPPED", "RESET", "RECONFIGURED"}:
                raise ValueError("SESSION_NOT_FINISHED")
            if self._has_pending_event_records(session):
                raise ValueError("SESSION_HAS_PENDING_EVENTS")
            if session.get("auto_next_armed"):
                raise ValueError("SESSION_AUTO_NEXT_ARMED")
            selections = list(session.get("selections") or [])
            selections.extend(item for rows in (session.get("rule_selections") or {}).values()
                              for item in rows)
            has_open = getattr(self._persistence, "has_open_rule_session_executions", None)
            if callable(has_open) and has_open(session_id):
                raise ValueError("SESSION_HAS_OPEN_EXECUTIONS")

            delete_persisted = getattr(self._persistence, "delete_rules_engine_session", None)
            if not callable(delete_persisted):
                raise ValueError("SESSION_DELETE_PERSISTENCE_UNAVAILABLE")
            deleted = deepcopy(session)
            summary = self._session_summary(session)
            snapshot = self._snapshot()
            del self.sessions[session_id]
            try:
                delete_persisted(session_id, snapshot, at)
            except Exception:
                self.sessions[session_id] = deleted
                raise
            return {"ok": True, "session_id": session_id, "deleted_at": at,
                    "summary": summary}

    def delete_sessions(self, session_ids: list[str], *, now: str | None = None) -> dict[str, Any]:
        """Remove a selected group of inactive sessions using one persistence transaction."""
        if not isinstance(session_ids, list):
            raise ValueError("INVALID_SESSION_IDS")
        unique_ids = list(dict.fromkeys(str(session_id) for session_id in session_ids))
        if not unique_ids or any(not session_id.strip() for session_id in unique_ids):
            raise ValueError("INVALID_SESSION_IDS")

        at = _now(now)
        removable_statuses = {
            "COMPLETED", "SETTLING", "PAUSED", "MANUALLY_STOPPED", "RESET", "RECONFIGURED",
        }
        with self._lock:
            eligible: dict[str, dict[str, Any]] = {}
            failures: list[dict[str, str]] = []
            for session_id in unique_ids:
                session = self.sessions.get(session_id)
                if session is None:
                    failures.append({"session_id": session_id, "reason": "SESSION_NOT_FOUND"})
                    continue
                if self._effective_session_status(session) not in removable_statuses:
                    failures.append({"session_id": session_id, "reason": "SESSION_NOT_FINISHED"})
                    continue
                if session.get("auto_next_armed"):
                    failures.append({"session_id": session_id, "reason": "SESSION_AUTO_NEXT_ARMED"})
                    continue
                eligible[session_id] = deepcopy(session)

            if not eligible:
                return {"ok": not failures, "deleted_session_ids": [], "failures": failures}
            delete_batch = getattr(self._persistence, "delete_rules_engine_sessions", None)
            if not callable(delete_batch):
                raise ValueError("SESSION_DELETE_PERSISTENCE_UNAVAILABLE")

            snapshot = self._snapshot()
            for session_id in eligible:
                self.sessions.pop(session_id, None)
            try:
                persisted = delete_batch(list(eligible), snapshot, at)
            except Exception:
                self.sessions.update(eligible)
                raise

            persisted_deleted = set(persisted.get("deleted_session_ids") or [])
            deleted_ids = [session_id for session_id in eligible if session_id in persisted_deleted]
            failures.extend(persisted.get("failures") or [])
            for session_id, session in eligible.items():
                if session_id not in persisted_deleted:
                    self.sessions[session_id] = session
            return {
                "ok": not failures,
                "deleted_session_ids": deleted_ids,
                "failures": failures,
            }

    def sessions_status(self) -> list[dict[str, Any]]:
        return [self._session_summary(item) for item in sorted(self.sessions.values(),
                key=lambda row: row.get("started_at", ""), reverse=True)]

    @staticmethod
    def _settled_shared_result_counts(session: Mapping[str, Any]) -> tuple[int, int]:
        """Count each distinct decision once; separate decisions may share a target."""
        wins = losses = 0
        seen_selection_ids: set[str] = set()
        seen_legacy_targets: set[tuple[str, int]] = set()
        for row in session.get("selections", []) or []:
            if not isinstance(row, Mapping):
                continue
            result = row.get("status")
            if result not in {"WIN", "LOSS"}:
                continue
            selection_id = row.get("selection_id")
            if selection_id:
                selection_key = str(selection_id)
                if selection_key in seen_selection_ids:
                    continue
                seen_selection_ids.add(selection_key)
            else:
                target = row.get("expected_target") or {}
                stream_id = target.get("streamId", target.get("stream_id")) if isinstance(target, Mapping) else None
                round_index = target.get("roundIndex", target.get("round_index")) if isinstance(target, Mapping) else None
                target_key = ((stream_id, round_index) if isinstance(stream_id, str)
                              and isinstance(round_index, int) and not isinstance(round_index, bool) else None)
                if target_key is not None and target_key in seen_legacy_targets:
                    continue
                if target_key is not None:
                    seen_legacy_targets.add(target_key)
            if result == "WIN":
                wins += 1
            else:
                losses += 1
        return wins, losses

    @staticmethod
    def _pending_shared_result_count(session: Mapping[str, Any]) -> int:
        pending = [row for row in session.get("selections", []) or []
                   if isinstance(row, Mapping) and row.get("status") == "PENDING"]
        identifiers = [str(row.get("selection_id")) for row in pending if row.get("selection_id")]
        return len(set(identifiers)) + sum(1 for row in pending if not row.get("selection_id"))

    @classmethod
    def _normalize_legacy_limit_stop(cls, session: dict[str, Any]) -> None:
        """Separate current limit state from historical reasons in old snapshots."""
        config = session.get("configuration_snapshot") or {}
        wins, losses = cls._settled_shared_result_counts(session)
        valid_reason = cls._net_outcome_stop_reason(wins, losses, config)
        for reason_field, source_field, at_field, round_field, details_field, scope in (
            ("selection_stop_reason", "selection_stop_source", "selection_stopped_at",
             "selection_stopped_at_round_id", "selection_stop_details", "SELECTION"),
            ("stop_reason", "stop_source", "stop_at", "stop_round_id", "stop_details", "SESSION"),
        ):
            reason = session.get(reason_field)
            if reason not in NET_OUTCOME_LIMIT_REASONS:
                continue
            stopped_at = session.get(at_field)
            stop_source = session.get(source_field) or "LEGACY_UNVERIFIED"
            round_id = session.get(round_field)
            if valid_reason == reason:
                session[source_field] = stop_source
                session.setdefault("legacy_stop_source_status", "REASON_VALIDATED_PROVENANCE_MISSING")
                # A valid limit stops selections while the session settles
                # pending results and finishes its observation window.
                if session.get("status") in {"RUNNING", "SETTLING"}:
                    session.update({"status": "SETTLING", "selection_status": "STOPPED"})
                    if reason_field == "stop_reason" and not session.get("selection_stop_reason"):
                        session.update({"selection_stop_reason": reason,
                            "selection_stop_source": stop_source,
                            "selection_stopped_at": stopped_at,
                            "selection_stopped_at_round_id": round_id,
                            "selection_stop_details": deepcopy(session.get(details_field) or {})})
                continue

            details = {"original_reason": reason, "settled_wins": wins,
                "settled_losses": losses, "net_outcome": wins - losses,
                "win_limit_enabled": config.get("win_limit_enabled") is True,
                "loss_limit_enabled": config.get("loss_limit_enabled") is True}
            session["legacy_stop_reason"] = reason
            session["legacy_stop_reason_status"] = "UNVERIFIED_DISABLED_OR_UNMET_LIMIT"
            session["legacy_stop_details"] = deepcopy(details)
            if stopped_at is None:
                session["legacy_stop_timestamp_status"] = "UNAVAILABLE"
            else:
                history = session.get("stop_history") or []
                if not any(isinstance(event, Mapping) and event.get("reason") == reason
                           and event.get("stopped_at") == stopped_at for event in history):
                    event = cls._append_stop_event(session, reason, stopped_at, stop_source,
                        {"round_id": round_id} if round_id else None, scope, details)
                    session["last_selection_stop_event" if reason_field == "selection_stop_reason"
                            else "last_stop_event"] = event

            # Keep the original event and legacy metadata, but make sure a
            # disabled/unmet limit is no longer the current stop reason.
            for field in (reason_field, source_field, at_field, round_field, details_field):
                session[field] = None if field != details_field else {}
            if (reason_field == "selection_stop_reason"
                    and session.get("status") in {"RUNNING", "SETTLING"}):
                session["selection_status"] = "ACTIVE"
                if session.get("status") == "SETTLING":
                    session["status"] = "RUNNING"

    @classmethod
    def _ensure_legacy_stop_provenance(cls, session: dict[str, Any]) -> None:
        """Mark historical stops whose original source or time was not persisted."""
        for reason_field, source_field, at_field, round_field, scope in (
            ("selection_stop_reason", "selection_stop_source", "selection_stopped_at",
             "selection_stopped_at_round_id", "LEGACY_SELECTION_STOP"),
            ("stop_reason", "stop_source", "stop_at", "stop_round_id", "LEGACY_SESSION_STOP"),
        ):
            reason = session.get(reason_field)
            if not isinstance(reason, str) or not reason:
                continue
            if not session.get(source_field):
                session[source_field] = "LEGACY_UNVERIFIED"
            stopped_at = session.get(at_field)
            if not stopped_at:
                session["legacy_stop_timestamp_status"] = "UNAVAILABLE"
                continue
            if not any(isinstance(event, Mapping) and event.get("reason") == reason
                       and event.get("stopped_at") == stopped_at
                       for event in (session.get("stop_history") or [])):
                event = cls._append_stop_event(session, reason, stopped_at, session[source_field],
                    {"round_id": session.get(round_field)} if session.get(round_field) else None,
                    scope, {"provenance_status": "LEGACY_UNVERIFIED"})
                session["last_selection_stop_event" if reason_field == "selection_stop_reason"
                        else "last_stop_event"] = event

    @staticmethod
    def _net_outcome_stop_reason(wins: int, losses: int,
                                 configuration: Mapping[str, Any]) -> str | None:
        net_outcome = wins - losses
        win_count = configuration.get("win_limit_count")
        if (configuration.get("win_limit_enabled") is True
                and isinstance(win_count, int) and not isinstance(win_count, bool)
                and win_count > 0 and net_outcome >= win_count):
            return "WIN_LIMIT_REACHED"
        loss_count = configuration.get("loss_limit_count")
        if (configuration.get("loss_limit_enabled") is True
                and isinstance(loss_count, int) and not isinstance(loss_count, bool)
                and loss_count > 0 and -net_outcome >= loss_count):
            return "LOSS_LIMIT_REACHED"
        return None

    @staticmethod
    def _trim_committed_event_records(session: dict[str, Any], *, keep: int = 100) -> None:
        """Bound the live deduplication snapshot; full history stays in SQL ledgers.

        Pending observed records must remain until their matching committed
        event upgrades them. Older committed records are safely covered by the
        monotonic per-session stream cursor and the durable evaluation ledgers.
        """
        records = session.get("event_records")
        if not isinstance(records, dict) or len(records) <= keep:
            return
        pending = {key: value for key, value in records.items()
                   if isinstance(value, Mapping)
                   and value.get("persistence_status") == "PENDING"}
        committed = [(key, value) for key, value in records.items()
                     if isinstance(value, Mapping)
                     and value.get("persistence_status") != "PENDING"]
        committed.sort(key=lambda item: (
            int((item[1].get("source_round") or {}).get("roundIndex") or 0), item[0]))
        retained = dict(committed[-max(1, int(keep)):])
        retained.update(pending)
        session["event_records"] = retained

    @staticmethod
    def _has_pending_event_records(session: Mapping[str, Any]) -> bool:
        return any(record.get("persistence_status") == "PENDING"
                   for record in (session.get("event_records") or {}).values())

    @staticmethod
    def _effective_session_status(session: Mapping[str, Any]) -> str:
        status = str(session.get("status") or "UNKNOWN")
        observed = session.get("observed_rounds")
        window = session.get("window_size")
        if (status in {"RUNNING", "SETTLING"}
                and isinstance(observed, int) and not isinstance(observed, bool)
                and isinstance(window, int) and not isinstance(window, bool)
                and window > 0 and observed >= window):
            return "COMPLETED"
        return status

    @staticmethod
    def _session_accepts_source_events(session: Mapping[str, Any]) -> bool:
        status = RulesEngine._effective_session_status(session)
        if status in {"RUNNING", "SETTLING"}:
            return True
        if status == "PAUSED" and session.get("stop_reason") in {None, "MANUAL_PAUSE"}:
            return True
        if status == "PAUSED" and session.get("stop_reason") == "ROUND_INDEX_GAP":
            # Replay can carry a legacy gap-paused session to the next fresh
            # boundary, where observe_round rebases and resumes it.
            return True
        if status == "MANUALLY_STOPPED":
            selections = list(session.get("selections") or [])
            selections.extend(item for rows in (session.get("rule_selections") or {}).values()
                              for item in rows)
            return any(item.get("status") == "PENDING" for item in selections)
        return False

    @staticmethod
    def _session_accepts_settlement_events(session: Mapping[str, Any]) -> bool:
        """Allow terminal sessions to settle already-created targets, but honor reset/archive."""
        status = RulesEngine._effective_session_status(session)
        if status in {"RUNNING", "SETTLING", "PAUSED"}:
            return True
        if status not in {"COMPLETED", "MANUALLY_STOPPED", "RECONFIGURED"}:
            return False
        selections = list(session.get("selections") or [])
        selections.extend(item for rows in (session.get("rule_selections") or {}).values()
                          for item in rows)
        return any(item.get("status") == "PENDING" for item in selections)

    def _selected_session(self) -> dict[str, Any] | None:
        rows = sorted(self.sessions.values(),
                      key=lambda row: row.get("started_at", ""), reverse=True)
        running = next((item for item in rows
                        if self._effective_session_status(item) in {"RUNNING", "SETTLING"}), None)
        if running:
            return running
        return next((item for item in rows if item.get("status") == "PAUSED"
                     and item.get("stop_reason") in {
                         None, "MANUAL_PAUSE", "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION"}), None)

    def stream_cursor(self, stream_id: str, latest_sequence: int) -> int:
        cursors = [int(row.get("last_stream_sequence", latest_sequence))
                   for row in self.sessions.values()
                   if row.get("last_stream_id") == stream_id
                   and (self._session_accepts_source_events(row)
                        or self._has_pending_event_records(row))]
        return min(cursors) if cursors else int(latest_sequence)

    @staticmethod
    def _session_summary(session: Mapping[str, Any]) -> dict[str, Any]:
        # New sessions count each shared target once, regardless of how many
        # independent rules contributed. Keep the old per-rule ledger summary
        # readable for sessions persisted by the previous tracker version.
        rows = session.get("selections", [])
        if session.get("mode") == "FIVE_RULE_TRACKER" and not rows:
            legacy_rows = [item for values in session.get("rule_selections", {}).values() for item in values]
            if legacy_rows:
                rows = legacy_rows
                results = {status: sum(item.get("status") == status for item in rows)
                           for status in ("WIN", "LOSS", "PENDING")}
            else:
                results = {"WIN": 0, "LOSS": 0, "PENDING": 0}
        else:
            pending = RulesEngine._pending_shared_result_count(session)
            wins, losses = RulesEngine._settled_shared_result_counts(session)
            results = {"WIN": wins, "LOSS": losses, "PENDING": pending}
        net_outcome = results["WIN"] - results["LOSS"]
        selection_ids = [str(row.get("selection_id")) for row in rows
                         if isinstance(row, Mapping) and row.get("selection_id")]
        selection_count = len(set(selection_ids)) + sum(
            1 for row in rows if isinstance(row, Mapping) and not row.get("selection_id"))
        configuration = session.get("configuration_snapshot") or {}
        requested_stake = configuration.get("requested_stake")
        if requested_stake is None:
            requested_stake = configuration.get("fixed_stake_rwf", configuration.get("stake_rwf"))
        stop_reason = session.get("selection_stop_reason") or session.get("stop_reason")
        selection_reason = bool(session.get("selection_stop_reason"))
        matching_stop_event = next((event for event in reversed(session.get("stop_history") or [])
            if isinstance(event, Mapping) and event.get("reason") == stop_reason
            and event.get("scope") in ({"SELECTION", "SESSION_AND_SELECTION"} if selection_reason
                                        else {"SESSION", "SESSION_AND_SELECTION"})), None)
        stop_source = (((matching_stop_event or {}).get("source")
                       or session.get("selection_stop_source" if selection_reason else "stop_source"))
                       if stop_reason is not None else None)
        stop_at = (((matching_stop_event or {}).get("stopped_at")
                   or session.get("selection_stopped_at" if selection_reason else "stop_at"))
                   if stop_reason is not None else None)
        stop_round = ((matching_stop_event or {}).get("source_round") or {})
        stop_round_id = ((stop_round.get("round_id")
                         or session.get("selection_stopped_at_round_id" if selection_reason else "stop_round_id"))
                         if stop_reason is not None else None)
        stop_details = (((matching_stop_event or {}).get("details")
                        or session.get("selection_stop_details" if selection_reason else "stop_details") or {})
                        if stop_reason is not None else {})
        evaluations = session.get("evaluations") or []
        last_evaluation_at = (evaluations[-1].get("evaluated_at")
                              if evaluations and isinstance(evaluations[-1], Mapping) else None)
        return {"session_id": session["session_id"], "mode": session["mode"],
                "status": RulesEngine._effective_session_status(session),
                "selection_status": session.get("selection_status"), "started_at": session.get("started_at"),
                "ended_at": session.get("ended_at"),
                "last_activity_at": session.get("ended_at") or last_evaluation_at or session.get("started_at"),
                "rule_ids": list(session.get("rule_ids", [])),
                "configuration_version": session.get("configuration_version"),
                "observed_rounds": session.get("observed_rounds", 0), "window_size": session.get("window_size"),
                "latest_observed_round": deepcopy(session.get("last_received_round")),
                "last_received_round": deepcopy(session.get("last_received_round")),
                "last_settled_target": deepcopy(session.get("last_settled_target")),
                "last_evaluation_at": last_evaluation_at,
                "event_processing_latency_ms": session.get("last_event_processing_latency_ms"),
                "settlement_processing_latency_ms": session.get("last_target_settlement_processing_latency_ms"),
                "is_active": RulesEngine._effective_session_status(session) in {"RUNNING", "SETTLING"},
                "prestart_skipped_rounds": session.get("prestart_skipped_rounds", 0),
                "selection_count": selection_count, "results": results,
                "net_outcome": net_outcome,
                "net_outcome_limit_version": configuration.get("net_outcome_limit_version"),
                "win_limit_enabled": configuration.get("win_limit_enabled", False),
                "win_limit_count": configuration.get("win_limit_count"),
                "loss_limit_enabled": configuration.get("loss_limit_enabled", False),
                "loss_limit_count": configuration.get("loss_limit_count"),
                "auto_next_check_enabled": configuration.get("auto_next_check_enabled"),
                "auto_next_on_limit_enabled": configuration.get("auto_next_on_limit_enabled"),
                "net_profit_rwf": session.get("net_profit_rwf", 0),
                "auto_next_armed": session.get("auto_next_armed", False),
                "auto_next_trigger": session.get("auto_next_trigger"),
                "auto_next_armed_at": session.get("auto_next_armed_at"),
                "auto_next_session_id": session.get("auto_next_session_id"),
                "auto_next_block_reason": session.get("auto_next_block_reason"),
                "stop_reason": stop_reason, "stop_reason_source": stop_source,
                "stop_reason_at": stop_at, "stop_reason_round_id": stop_round_id,
                "stop_reason_details": deepcopy(dict(stop_details)),
                "legacy_stop_reason": session.get("legacy_stop_reason"),
                "legacy_stop_reason_status": session.get("legacy_stop_reason_status"),
                "legacy_stop_timestamp_status": session.get("legacy_stop_timestamp_status"),
                "stop_history": deepcopy(session.get("stop_history") or []),
                "integrity_stop_reason": session.get("integrity_stop_reason"),
                "selection_results_kind": "SHADOW_ROUND_OUTCOMES",
                "execution_panel": configuration.get("execution_panel"),
                "requested_stake": requested_stake,
                "requested_stake_currency": configuration.get("stake_currency"),
                "reconciled_financials": deepcopy(session.get("reconciled_financials") or {
                    "realized_betting_pl": None, "currency": None,
                    "win_count": 0, "loss_count": 0, "reconciled_bets": 0,
                }),
                "automatic_execution": "OFF"}

    def ui_setup_configuration(self) -> dict[str, Any]:
        """Return the active session's UI stake and single selected panel.

        This is configuration only. It is independent of whether any rule has
        produced TRUE and it never authorizes a financial action.
        """
        with self._lock:
            active = self._selected_session()
            if not active:
                return {"status": "BLOCKED", "reason": "NO_ACTIVE_RULES_SESSION",
                        "session_id": None, "requested_stake": None,
                        "requested_stake_currency": None, "execution_panel": None,
                        "automatic_execution": "OFF"}
            config = deepcopy(active.get("configuration_snapshot") or {})
            requested_stake = config.get("requested_stake")
            if requested_stake is None:
                requested_stake = config.get("fixed_stake_rwf", config.get("stake_rwf"))
            currency = config.get("stake_currency")
            if currency is None and ("fixed_stake_rwf" in config or "stake_rwf" in config):
                currency = "RWF"
            panel = config.get("execution_panel")
            if (isinstance(requested_stake, bool)
                    or not isinstance(requested_stake, (int, float))
                    or requested_stake <= 0):
                return {"status": "BLOCKED", "reason": "SESSION_STAKE_NOT_CONFIGURED",
                        "session_id": active.get("session_id"), "requested_stake": None,
                        "requested_stake_currency": currency, "execution_panel": panel,
                        "automatic_execution": "OFF"}
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                return {"status": "BLOCKED", "reason": "SESSION_STAKE_CURRENCY_UNVERIFIED",
                        "session_id": active.get("session_id"), "requested_stake": requested_stake,
                        "requested_stake_currency": None, "execution_panel": panel,
                        "automatic_execution": "OFF"}
            if panel is None:
                return {"status": "BLOCKED", "reason": "SESSION_EXECUTION_PANEL_NOT_SELECTED",
                        "session_id": active.get("session_id"), "requested_stake": requested_stake,
                        "requested_stake_currency": currency, "execution_panel": None,
                        "automatic_execution": "OFF"}
            if isinstance(panel, bool) or panel not in (0, 1):
                return {"status": "BLOCKED", "reason": "SESSION_EXECUTION_PANEL_INVALID",
                        "session_id": active.get("session_id"), "requested_stake": requested_stake,
                        "requested_stake_currency": currency, "execution_panel": None,
                        "automatic_execution": "OFF"}
            return {"status": "CONFIGURED", "reason": None,
                    "session_id": active.get("session_id"),
                    "session_status": active.get("status"),
                    "requested_stake": requested_stake,
                    "requested_stake_currency": currency,
                    "execution_panel": panel,
                    "automatic_execution": "OFF"}

    def evaluate(self, evaluated_round: dict[str, Any] | None = None) -> dict[str, Any]:
        # Compatibility endpoint deliberately does not infer from a loose/latest row.
        # Only committed, ordered round.persisted events can reach observe_round().
        at = _now()
        active_sessions = any(item.get("status") == "RUNNING" and item.get("selection_status") == "ACTIVE"
                              for item in self.sessions.values())
        if evaluated_round is not None and active_sessions:
            result = {"result": False, "true_rule_ids": [], "rule_evaluations": [],
                      "decision": "BLOCKED", "reason": "COMMITTED_EVENT_REQUIRED",
                      "source_round": deepcopy(evaluated_round), "automatic_execution": "OFF", "can_bet": False}
        elif evaluated_round is not None:
            result = self.aggregate(())
            result["evaluated_round"] = deepcopy(evaluated_round)
            result["reason"] = "NOT_CONFIGURED"
        else:
            result = self.aggregate(())
        result["evaluated_at"] = at
        self.last_evaluation, self.last_evaluation_at = result, at
        self._persist()
        return deepcopy(result)

    def execution_authorized(self) -> bool:
        session = self._selected_session()
        if not session or session.get("status") != "RUNNING" or session.get("selection_status") != "ACTIVE":
            return False
        return any(
            row.get("status") == "PENDING"
            and row.get("selection_id")
            and row.get("execution_intent", {}).get("decision_id") == row.get("selection_id")
            and row.get("execution_intent", {}).get("target_stream_id")
            and isinstance(row.get("execution_intent", {}).get("target_round_index"), int)
            and row.get("execution_intent", {}).get("target_round_id")
            and row.get("execution_intent", {}).get("provider_round_id")
            and row.get("execution_intent", {}).get("target_identity_verified") is True
            and row.get("execution_intent", {}).get("expires_at")
            for row in session.get("selections", [])
        )

    def execution_selection(self, decision_id: str) -> dict[str, Any] | None:
        """Return the active shared selection matching a submitted decision ID."""
        session = self._selected_session()
        if not session or session.get("status") != "RUNNING" or session.get("selection_status") != "ACTIVE":
            return None
        for selection in session.get("selections", []):
            if (str(selection.get("selection_id")) == str(decision_id)
                    and selection.get("status") == "PENDING"):
                return deepcopy(selection)
        return None

    def bind_provider_target(self, decision_id: str,
                             evidence: Mapping[str, Any]) -> dict[str, Any] | None:
        """Bind one pending selection to fresh, provider-sourced target evidence.

        The provider identity must map to the canonical target ID and immediate
        successor index explicitly. No round ID is inferred from button state.
        """
        from app.betting.platform_state import classify_betting_window

        if not isinstance(evidence, Mapping) or classify_betting_window(evidence) != "BETTING_OPEN":
            return None
        with self._lock:
            session = self._selected_session()
            if (not session or session.get("status") != "RUNNING"
                    or session.get("selection_status") != "ACTIVE"):
                return None
            selection = next((item for item in session.get("selections", [])
                              if str(item.get("selection_id")) == str(decision_id)
                              and item.get("status") == "PENDING"), None)
            if selection is None:
                return None
            source = selection.get("source_round") or {}
            target = selection.get("expected_target") or {}
            intent = selection.get("execution_intent") or {}
            canonical_target = str(evidence.get("canonical_target_round_id") or "")
            provider_target = str(evidence.get("provider_round_id") or "")
            if (evidence.get("source_round_id") != source.get("roundId")
                    or evidence.get("source_round_index") != source.get("roundIndex")
                    or evidence.get("target_round_index") != target.get("roundIndex")
                    or not canonical_target or not provider_target
                    or target.get("roundId") not in (None, canonical_target)
                    or intent.get("target_round_id") not in (None, canonical_target)):
                return None
            target["roundId"] = canonical_target
            selection["expected_target"] = target
            intent.update({
                "target_round_id": canonical_target,
                "provider_round_id": provider_target,
                "target_identity_verified": True,
                "target_identity_proof": deepcopy(evidence.get("target_identity_proof")),
                "betting_deadline": evidence.get("betting_deadline"),
                "expires_at": evidence.get("betting_deadline"),
                "provider_phase_observed_at": evidence.get("observed_at"),
                "execution_status": "TARGET_BOUND_BLOCKED",
            })
            intent["blockers"] = [reason for reason in intent.get("blockers", [])
                                  if reason not in {"TARGET_ROUND_ID_UNVERIFIED",
                                                    "BETTING_DEADLINE_UNVERIFIED"}]
            selection["execution_intent"] = intent
            self._persist(session_ids={str(session["session_id"])})
            return deepcopy(selection)

    def needs_round_observation(self) -> bool:
        """Only session-owned state needs each collector event.

        An empty engine must not make the collector wait for a state write on
        every round. Completed sessions armed for auto-next still need the
        boundary event, and manually stopped windows keep observing.
        """
        with self._lock:
            return any(
                self._session_accepts_source_events(item)
                or item.get("status") == "MANUALLY_STOPPED"
                or self._has_pending_event_records(item)
                or (self._effective_session_status(item) == "COMPLETED"
                    and item.get("auto_next_armed") is True)
                for item in self.sessions.values()
            )

    def status(self) -> dict[str, Any]:
        active = self._selected_session()
        active_summary = self._session_summary(active) if active else None
        sessions = self.sessions_status()
        latest_summary = sessions[0] if sessions else None
        # Metrics remain tied to a concrete settlement scope after a session
        # stops or completes. Keep active_session separate for control/readiness.
        summary = active_summary or latest_summary
        configured = sum(1 for row in self.rule_configuration.values() if row.get("configured"))
        enabled = sum(1 for row in self.rule_configuration.values() if row.get("configured") and row.get("enabled"))
        if active:
            configured = len(active.get("rule_ids", []))
            enabled = len(active.get("rule_ids", []))
        latest = (self.last_evaluation or {}).get("source_round",
                  (self.last_evaluation or {}).get("evaluated_round"))
        with self._lock:
            event_latency_metrics = {stage: self._latency_summary(values)
                                     for stage, values in self._delivery_metrics.items()}
        return {"status": active.get("status") if active else ("READY" if configured else "NOT_CONFIGURED"),
                "rules_configured": configured, "rules_enabled": enabled,
                "configuration_version": self.configuration_version,
                "presets": deepcopy(self.presets),
                "last_rule_evaluation": self.last_evaluation_at or "NONE",
                "last_evaluated_round": latest, "decision": (self.last_evaluation or {}).get("decision", "NO_ACTION"),
                "automatic_execution": "OFF", "rule_evaluations": (self.last_evaluation or {}).get("rule_evaluations", []),
                "active_session": active_summary, "latest_session": latest_summary,
                "reporting_session_id": summary.get("session_id") if summary else None,
                "reporting_session_status": summary.get("status") if summary else None,
                "sessions": sessions, "selection_count": summary.get("selection_count", 0) if summary else 0,
                "total_wins": summary.get("results", {}).get("WIN", 0) if summary else 0,
                "total_losses": summary.get("results", {}).get("LOSS", 0) if summary else 0,
                "net_outcome": summary.get("net_outcome", 0) if summary else 0,
                "pending": summary.get("results", {}).get("PENDING", 0) if summary else 0,
                "event_latency_metrics": {"scope": "CURRENT_PROCESS", "stages_ms": event_latency_metrics},
                "can_bet": False}
