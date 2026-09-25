"""Application-level lifecycle coordinator.

The orchestrator owns no domain decisions or browser objects. It only starts
and stops already-constructed services, coordinates recovery, and keeps the
health/event loops alive.
"""
from __future__ import annotations

import asyncio
import logging


log = logging.getLogger("APP.lifecycle")


class SystemOrchestrator:
    def __init__(self, app):
        self.app = app
        self.tasks: list[asyncio.Task] = []
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        history = self.app.state.history_collector
        try:
            await history.start()
        except Exception:
            # Collector/browser availability is reported by system health; it
            # must not prevent the HTTP API from starting.
            log.exception("history collector failed during startup; API remains available")
        self.tasks = [
            self._task(self.app.state.system_health_monitor(self.app), "system-health"),
            self._task(self.app.state.history_monitor(self.app), "history-monitor"),
        ]
        operations_monitor = getattr(self.app.state, "operations_monitor", None)
        if operations_monitor is not None:
            self.tasks.append(self._task(operations_monitor(self.app), "operations-monitor"))
        self.started = True

    @staticmethod
    def _task(coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)

        def report_failure(done: asyncio.Task) -> None:
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                log.error("background task %s stopped unexpectedly: %s", name, error,
                          exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(report_failure)
        return task

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
        try:
            await self.app.state.betting.shutdown()
        except Exception:
            log.exception("betting shutdown failed")
        try:
            await self.app.state.history_collector.stop()
        except Exception:
            log.exception("history collector shutdown failed")
        self.started = False

    async def observe(self) -> dict:
        """Ensure history observation is running without enabling betting."""
        return await self.app.state.history_collector.start()

    def status(self) -> dict:
        return {"started": self.started,
                "history": self.app.state.history_collector.status(),
                "health": self.app.state.system_health.snapshot()}
