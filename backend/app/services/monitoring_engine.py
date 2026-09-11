from __future__ import annotations

from pathlib import Path


class MonitoringEngine:
    def __init__(self, data_path: Path):
        self.data_path = Path(data_path)
        self._last_mtime: float | None = None
        self._last_size: int | None = None

    def has_changed(self) -> bool:
        if not self.data_path.exists():
            return False
        stat = self.data_path.stat()
        changed = self._last_mtime is None or stat.st_mtime != self._last_mtime or stat.st_size != self._last_size
        self._last_mtime = stat.st_mtime
        self._last_size = stat.st_size
        return changed

