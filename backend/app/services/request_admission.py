"""Bounded in-process API rate and concurrency admission control."""
from __future__ import annotations

import math
import os
import threading
import time
from collections import OrderedDict, deque
from typing import Any


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class ApiRequestAdmission:
    """Per-process API guard with bounded client state and explicit overload."""

    def __init__(self, *, max_concurrent: int = 64, requests_per_minute: int = 600,
                 max_clients: int = 4096):
        self.max_concurrent = max(1, int(max_concurrent))
        self.requests_per_minute = max(1, int(requests_per_minute))
        self.max_clients = max(1, int(max_clients))
        self._active = 0
        self._clients: OrderedDict[str, deque[float]] = OrderedDict()
        self._request_latencies_ms: deque[float] = deque(maxlen=1000)
        self._requests_total = 0
        self._failures_total = 0
        self._rate_limited_total = 0
        self._overloaded_total = 0
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "ApiRequestAdmission":
        return cls(
            max_concurrent=_bounded_int("WINNER_API_MAX_IN_FLIGHT", 64, 8, 512),
            requests_per_minute=_bounded_int("WINNER_API_REQUESTS_PER_MINUTE", 600, 60, 6000),
            max_clients=_bounded_int("WINNER_API_RATE_LIMIT_CLIENTS", 4096, 128, 65536),
        )

    def allow_request(self, client_key: str, now: float | None = None) -> tuple[bool, int]:
        current = time.monotonic() if now is None else float(now)
        key = str(client_key)[:256] or "unknown"
        with self._lock:
            bucket = self._clients.pop(key, deque())
            cutoff = current - 60.0
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.requests_per_minute:
                retry_after = max(1, math.ceil(60.0 - (current - bucket[0])))
                self._clients[key] = bucket
                return False, retry_after
            bucket.append(current)
            self._clients[key] = bucket
            while len(self._clients) > self.max_clients:
                self._clients.popitem(last=False)
            return True, 0

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self.max_concurrent:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("api_admission_release_without_acquire")
            self._active -= 1

    def record_response(self, status_code: int, duration_ms: float) -> None:
        with self._lock:
            self._requests_total += 1
            if int(status_code) >= 500:
                self._failures_total += 1
            self._request_latencies_ms.append(max(0.0, float(duration_ms)))

    def record_rejection(self, status_code: int) -> int:
        with self._lock:
            if int(status_code) == 429:
                self._rate_limited_total += 1
                rejection_count = self._rate_limited_total
            elif int(status_code) == 503:
                self._overloaded_total += 1
                rejection_count = self._overloaded_total
            else:
                rejection_count = 0
            self._requests_total += 1
            self._failures_total += int(status_code) >= 500
            return rejection_count

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latencies = sorted(self._request_latencies_ms)

            def percentile(fraction: float):
                if not latencies:
                    return None
                index = min(len(latencies) - 1, max(0, math.ceil(fraction * len(latencies)) - 1))
                return round(latencies[index], 3)

            return {
                "active_requests": self._active,
                "max_concurrent": self.max_concurrent,
                "rate_limit_clients": len(self._clients),
                "max_rate_limit_clients": self.max_clients,
                "requests_total": self._requests_total,
                "failures_total": self._failures_total,
                "rate_limited_total": self._rate_limited_total,
                "overloaded_total": self._overloaded_total,
                "latency_samples": len(latencies),
                "latency_p50_ms": percentile(0.50),
                "latency_p95_ms": percentile(0.95),
            }
