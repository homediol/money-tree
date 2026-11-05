"""Independent, persistent operator stop control."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EmergencyStop:
    """Persist the operator latch without coupling it to execution policy."""

    STATE_KEY = "operational_safety"
    LEGACY_STATE_KEY = "risk_runtime"

    def __init__(self, repository, broadcaster=None):
        self.repository = repository
        self.broadcaster = broadcaster
        state = repository.load_application_state(self.STATE_KEY) or {}
        if "emergency_stop" not in state:
            # Carry forward only the stop latch from the retired state record.
            legacy = repository.load_application_state(self.LEGACY_STATE_KEY) or {}
            self.latched = bool(legacy.get("emergency_stop", legacy.get("emergency_latched", False)))
            self._persist("legacy_stop_state_migrated")
        else:
            self.latched = bool(state.get("emergency_stop"))

    def _persist(self, reason: str) -> None:
        self.repository.save_application_state(
            self.STATE_KEY,
            {"emergency_stop": self.latched, "reason": reason},
            _now(),
        )

    async def _emit(self, event: str, reason: str) -> None:
        if self.broadcaster:
            await self.broadcaster({"type": f"safety:{event}", "latched": self.latched,
                                    "reason": reason, "recorded_at": _now()})

    async def trigger(self, reason: str = "operator_emergency_stop") -> None:
        self.latched = True
        self._persist(reason)
        await self._emit("emergency_stop", reason)

    async def reset(self) -> None:
        self.latched = False
        self._persist("operator_reset")
        await self._emit("emergency_stop_reset", "operator_reset")

    def status(self) -> dict[str, Any]:
        return {"latched": self.latched}
