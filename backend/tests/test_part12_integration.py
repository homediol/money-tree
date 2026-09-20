from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.system_orchestrator import SystemOrchestrator


class FakeHistory:
    def __init__(self):
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1
        return {"status": "HEALTHY"}

    async def stop(self):
        self.stops += 1

    def status(self):
        return {"status": "HEALTHY", "running": True}


class FakeHealth:
    def snapshot(self):
        return {"state": "HEALTHY"}


async def idle(_app):
    await asyncio.Event().wait()


def test_orchestrator_is_idempotent_and_shuts_down_once():
    async def run():
        history = FakeHistory()
        app = SimpleNamespace(state=SimpleNamespace(
            history_collector=history, system_health=FakeHealth(),
            system_health_monitor=idle, history_monitor=idle,
            betting=SimpleNamespace(shutdown=lambda: asyncio.sleep(0)),
        ))
        orchestrator = SystemOrchestrator(app)
        await orchestrator.start()
        await orchestrator.start()
        assert history.starts == 1
        assert orchestrator.started is True
        await orchestrator.shutdown()
        await orchestrator.shutdown()
        assert history.stops == 1
        assert orchestrator.started is False

    asyncio.run(run())

