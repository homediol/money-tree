"""Read-only verification for the fixed 2.00x auto-cashout setting."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

CASHOUT_TARGET = Decimal("2.00")


def inspect_cashout_configuration(control: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate one already-read panel without changing its controls.

    A displayed value and enabled switch are not treated as platform
    acknowledgement. Acknowledgement remains UNKNOWN unless the platform
    exposes independent evidence for it.
    """
    if not isinstance(control, Mapping):
        return {
            "target": 2.0, "displayed_value": None, "enabled": None,
            "platform_acknowledged": None, "configuration_valid": False,
            "verified": False, "failures": ["CASHOUT_CONTROL_NOT_FOUND"],
        }
    raw = control.get("displayedValue", control.get("displayed_value"))
    try:
        displayed = Decimal(str(raw).strip()) if raw is not None else None
        value_valid = displayed == CASHOUT_TARGET
    except (InvalidOperation, ValueError):
        displayed, value_valid = None, False
    enabled = control.get("autoCashoutEnabled", control.get("enabled"))
    auto_tab_selected = control.get("autoTabSelected", control.get("auto_tab_selected"))
    acknowledged = control.get("platformAcknowledged", control.get("platform_acknowledged"))
    failures: list[str] = []
    if raw is None:
        failures.append("CASHOUT_VALUE_UNAVAILABLE")
    elif not value_valid:
        failures.append("CASHOUT_VALUE_MUST_EQUAL_2_00")
    if enabled is not True:
        failures.append("AUTO_CASHOUT_NOT_CONFIRMED_ENABLED")
    if auto_tab_selected is not True:
        failures.append("AUTO_MODE_NOT_CONFIRMED_SELECTED")
    if acknowledged is False:
        failures.append("PLATFORM_REJECTED_CASHOUT_SETTING")
    elif acknowledged is not True:
        failures.append("PLATFORM_CASHOUT_ACKNOWLEDGEMENT_UNVERIFIED")
    return {
        "target": 2.0,
        "displayed_value": str(raw) if raw is not None else None,
        "enabled": enabled if isinstance(enabled, bool) else None,
        "auto_mode_selected": auto_tab_selected if isinstance(auto_tab_selected, bool) else None,
        "platform_acknowledged": acknowledged if isinstance(acknowledged, bool) else None,
        "configuration_valid": value_valid and enabled is True and auto_tab_selected is True,
        "verified": value_valid and enabled is True and auto_tab_selected is True and acknowledged is True,
        "failures": failures,
    }


class MockCashoutReadAdapter:
    """Deterministic read-only fixture used for cashout contract tests."""
    def __init__(self, control: Mapping[str, Any] | None):
        self.control = dict(control) if isinstance(control, Mapping) else None

    async def read_cashout_configuration(self, panel: int = 0) -> dict[str, Any]:
        if panel != 0:
            return inspect_cashout_configuration(None)
        return inspect_cashout_configuration(self.control)
