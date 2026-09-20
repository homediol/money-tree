"""Application-level lifecycle coordinator.

The orchestrator owns no domain decisions or browser objects. It only starts
and stops already-constructed services, coordinates recovery, and keeps the
health/event loops alive.
"""
from __future__ import annotations

import asyncio


class SystemOrchestrator:
    def __init__(self, app):
        self.app = app
        self.tasks: list[asyncio.Task] = []
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        history = self.app.state.history_collector
        await history.start()
        self.tasks = [
            asyncio.create_task(self.app.state.system_health_monitor(self.app)),
            asyncio.create_task(self.app.state.history_monitor(self.app)),
        ]
        operations_monitor = getattr(self.app.state, "operations_monitor", None)
        if operations_monitor is not None:
            self.tasks.append(asyncio.create_task(operations_monitor(self.app)))
        self.started = True

    async def shutdown(self) -> None:
        if not self.started:
            return
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except BaseException:
                pass
        self.tasks.clear()
        await self.app.state.betting.shutdown()
        await self.app.state.history_collector.stop()
        self.started = False

    async def observe(self) -> dict:
        """Ensure history observation is running without enabling betting."""
        return await self.app.state.history_collector.start()

    def status(self) -> dict:
        return {"started": self.started,
                "history": self.app.state.history_collector.status(),
                "health": self.app.state.system_health.snapshot()}
