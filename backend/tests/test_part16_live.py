from types import SimpleNamespace
import asyncio

import pytest

from app.live import LiveActivationManager


def test_live_activation_is_observing_and_requires_exact_confirmation():
    controller = LiveActivationManager()
    assert controller.mode == "OBSERVING"
    assert controller.live_active is False


def test_live_start_rejects_missing_confirmation_without_side_effect():
    controller = LiveActivationManager(SimpleNamespace(state=SimpleNamespace()))
    result = asyncio.run(controller.start({"confirmation": "enable live betting"}))
    assert result["ok"] is False
    assert result["error"] == "explicit_confirmation_required"
    assert controller.mode == "OBSERVING"
