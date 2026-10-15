from __future__ import annotations

import asyncio

import pytest

from app.services.rules_shadow_executor import execute_rules_selection_in_shadow


class _ForbiddenAccess:
    def __getattr__(self, name):
        raise AssertionError(f"retired shadow adapter accessed {name}")


@pytest.mark.parametrize("intent", [
    {"decision_id": "selection-1", "target_round_index": 11},
    {"decision_id": "legacy-1", "selected_panel": 0,
     "requested_stake": 100, "requested_stake_currency": "BIF",
     "cashout_target": 2.0, "risk_profile": "PROFILE_A"},
])
def test_rules_shadow_adapter_never_submits_or_sizes_paper_execution(intent):
    result = asyncio.run(execute_rules_selection_in_shadow(
        _ForbiddenAccess(), intent.get("decision_id"), intent,
    ))

    assert result == {
        "status": "BLOCKED",
        "reason": "RULES_ENGINE_OBSERVE_ONLY",
        "real_money_action_submitted": False,
    }
