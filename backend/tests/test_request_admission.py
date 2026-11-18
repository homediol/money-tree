from concurrent.futures import ThreadPoolExecutor
import threading
import asyncio
from types import SimpleNamespace

from starlette.requests import Request

from main import api_request_admission
from app.round_distribution import RoundDistributionHub
from app.services.request_admission import ApiRequestAdmission


def test_rate_limiter_enforces_window_and_retry_after():
    admission = ApiRequestAdmission(max_concurrent=4, requests_per_minute=2, max_clients=10)
    assert admission.allow_request("client-a", now=0) == (True, 0)
    assert admission.allow_request("client-a", now=10) == (True, 0)
    assert admission.allow_request("client-a", now=30) == (False, 30)
    assert admission.allow_request("client-a", now=61) == (True, 0)


def test_rate_limiter_client_cache_is_bounded():
    admission = ApiRequestAdmission(max_concurrent=4, requests_per_minute=10, max_clients=3)
    for index in range(10):
        assert admission.allow_request(f"client-{index}", now=float(index)) == (True, 0)
    snapshot = admission.snapshot()
    assert snapshot["rate_limit_clients"] == 3
    assert snapshot["max_rate_limit_clients"] == 3


def test_round_stream_rate_limit_client_cache_is_bounded():
    hub = RoundDistributionHub()
    for index in range(hub.MAX_RATE_LIMIT_KEYS + 100):
        assert hub.allow_request(f"client-{index}", now=1.0) == (True, 0)
    assert len(hub._requests) == hub.MAX_RATE_LIMIT_KEYS


def test_concurrency_admission_is_thread_safe_and_never_exceeds_limit():
    limit = 5
    admission = ApiRequestAdmission(max_concurrent=limit, requests_per_minute=100, max_clients=100)
    barrier = threading.Barrier(20)

    def acquire(_index):
        barrier.wait(timeout=3)
        return admission.try_acquire()

    with ThreadPoolExecutor(max_workers=20) as executor:
        accepted = list(executor.map(acquire, range(20)))

    assert sum(accepted) == limit
    assert admission.snapshot()["active_requests"] == limit
    for _ in range(limit):
        admission.release()
    assert admission.snapshot()["active_requests"] == 0
    admission.record_response(200, 12.5)
    admission.record_response(503, 40)
    admission.record_rejection(429)
    metrics = admission.snapshot()
    assert metrics["requests_total"] == 3
    assert metrics["failures_total"] == 1
    assert metrics["rate_limited_total"] == 1
    assert metrics["latency_p95_ms"] == 40
    assert admission.record_rejection(503) == 1
    assert admission.record_rejection(503) == 2


def _request(admission, *, authorization=""):
    headers = [(b"authorization", authorization.encode())] if authorization else []
    app = SimpleNamespace(state=SimpleNamespace(api_request_admission=admission))
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/api/test", "raw_path": b"/api/test",
        "query_string": b"", "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000), "app": app,
    })


def test_api_middleware_returns_429_for_rate_limit_and_503_for_capacity():
    async def ok(_request):
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    rate_limited = ApiRequestAdmission(max_concurrent=2, requests_per_minute=1, max_clients=4)
    first = asyncio.run(api_request_admission(_request(rate_limited), ok))
    second = asyncio.run(api_request_admission(_request(rate_limited), ok))
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"

    overloaded = ApiRequestAdmission(max_concurrent=1, requests_per_minute=10, max_clients=4)
    assert overloaded.try_acquire() is True
    response = asyncio.run(api_request_admission(_request(overloaded), ok))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    overloaded.release()


def test_simultaneous_api_requests_are_admitted_with_bounded_active_work():
    async def run_requests():
        from starlette.responses import JSONResponse

        admission = ApiRequestAdmission(max_concurrent=3, requests_per_minute=100,
                                        max_clients=100)

        async def slow_ok(_request):
            await asyncio.sleep(0.01)
            return JSONResponse({"ok": True})

        responses = await asyncio.gather(*(
            api_request_admission(_request(admission), slow_ok)
            for _ in range(10)
        ))
        return [response.status_code for response in responses], admission.snapshot()

    statuses, metrics = asyncio.run(run_requests())
    assert statuses.count(200) == 3
    assert statuses.count(503) == 7
    assert metrics["active_requests"] == 0
    assert metrics["overloaded_total"] == 7
