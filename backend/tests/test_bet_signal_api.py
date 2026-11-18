from __future__ import annotations

import asyncio
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
import httpx
import pytest

from app.api import signals as signal_api
from app.database.repository import Repository
from app.round_distribution import RoundDistributionHub
import main as production_app


ENGINE_TOKEN = "node-rules-engine-secret"
READER_TOKEN = "external-project-reader-token-0123456789"
SESSION_ID = "shadow-session-1"
SOURCE_ID = "round-source-100"
TARGET_ID = "round-target-101"
RULE_IDS = ["RULE_1", "RULE_2", "RULE_3", "RULE_4"]


def test_published_signal_without_target_index_keeps_action_and_null_index():
    projected = signal_api._project_published_signal({
        "action": "BET", "distribution_status": "ACCEPTED", "target_round_index": None,
    })
    assert projected.model_dump() == {"action": "OK", "index": None}


class InlineExecutor:
    def submit(self, function, /, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class FakeEngine:
    def __init__(self, *, status="RUNNING", selection_status="ACTIVE", disabled=()):
        disabled = set(disabled)
        self.rule_configuration = {
            rule_id: {"configured": True, "enabled": rule_id not in disabled}
            for rule_id in RULE_IDS
        }
        self._detail = {"session_id": SESSION_ID, "status": status,
                        "selection_status": selection_status, "rule_ids": list(RULE_IDS)}

    def status(self):
        return {"active_session": {**self._detail}}

    def session(self, session_id):
        return dict(self._detail) if session_id == SESSION_ID else None


class RateLimiter:
    def allow_request(self, _token_id):
        return True, 0


class PostgresContractRepo:
    """Use real Repository schema/SQL with an isolated local database."""

    database_url = "postgresql://test-contract"

    def __init__(self, path):
        self.local = Repository(path)
        self.local.database_url = None
        self.local.require_postgres = False
        self.local.init()
        self.committed = {SOURCE_ID}
        self.committed_indices = set()

    def external_project_token(self, _digest):
        return {"token_id": "reader-1", "project_id": "test-consumer"}

    def has_committed_round_distribution_event(self, round_id):
        return str(round_id) in self.committed

    def has_committed_round_distribution_index(self, stream_id, round_index):
        return (str(stream_id), int(round_index)) in self.committed_indices

    def bet_signal_for_target(self, session_id, target_round_id):
        return self.local.bet_signal_for_target(session_id, target_round_id)

    def insert_bet_signal(self, signal):
        return self.local.insert_bet_signal(signal)

    def list_bet_signals(self, limit=100, *, session_id=None):
        return self.local.list_bet_signals(limit, session_id=session_id)

    def bet_signal_by_id(self, signal_id):
        return self.local.bet_signal_by_id(signal_id)


class FakeRequest:
    def __init__(self, app, *, body=None, headers=None):
        self.app = app
        self._body = body
        self.headers = headers or {}
        self.client = SimpleNamespace(host="testclient")
        self.url = SimpleNamespace(scheme="http", hostname="testserver")

    async def json(self):
        return self._body


class FakeWebSocket:
    def __init__(self, app, *, authorization=READER_TOKEN, origin=None, first_message=None):
        self.app = app
        self.client = SimpleNamespace(host="testclient")
        self.url = SimpleNamespace(scheme="ws", hostname="testserver")
        self.headers = {}
        if authorization:
            self.headers["authorization"] = f"Bearer {authorization}"
        if origin:
            self.headers["origin"] = origin
        self.sent = asyncio.Queue()
        self.first_message = first_message
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        if self.first_message is None:
            raise WebSocketDisconnect(code=1000)
        message, self.first_message = self.first_message, None
        return message

    async def send_json(self, value):
        await self.sent.put(value)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def _now():
    return datetime.now(timezone.utc)


def make_app(tmp_path, *, engine=None, expired=False):
    app = FastAPI()
    repo = PostgresContractRepo(tmp_path / "signals.sqlite")
    app.state.wp = SimpleNamespace(repository=repo)
    app.state.settings = SimpleNamespace(api_key="owner-only-test-key",
        rules_publisher_api_key=ENGINE_TOKEN,
        cors_origins=["http://localhost:5173"], round_stream_allowed_origins=[])
    app.state.round_distribution = RateLimiter()
    app.state.rules_engine = engine or FakeEngine()
    app.state.round_stream_id = "stream-1"
    current = _now()
    deadline = current - timedelta(seconds=1) if expired else current + timedelta(seconds=30)
    app.state.latest_provider_phase_observation = {
        "phase": "BETTING_OPEN", "source_round_id": SOURCE_ID,
        "source_round_index": 100, "canonical_target_round_id": TARGET_ID,
        "target_round_index": 101, "target_identity_verified": True,
        "target_started": False, "observed_at": current.isoformat(),
        "betting_deadline": deadline.isoformat(),
    }
    return app, repo


def signal_body(*, matched=None, selected=None, timestamp=None, rule=None, signal_id=None):
    matched = list(RULE_IDS[:1] if matched is None else matched)
    selected = list(RULE_IDS if selected is None else selected)
    return {
        "action": "BET", "rule": rule if rule is not None else (matched[0] if matched else None),
        "description": "; ".join(f"{rule_id} returned TRUE" for rule_id in matched) or "No rule returned TRUE",
        "selected_rules": selected, "matched_rules": matched,
        "round_id": SOURCE_ID, "target_round_id": TARGET_ID,
        "session_id": SESSION_ID, "mode": "SHADOW",
        "timestamp": timestamp or _now().isoformat(),
        **({"signal_id": signal_id} if signal_id else {}),
    }


async def post_signal_async(app, payload):
    result = await signal_api.publish_signal(FakeRequest(app, body=payload,
        headers={"authorization": f"Bearer {ENGINE_TOKEN}"}))
    hub = getattr(app.state, "bet_signal_hub", None)
    if hub:
        hub.wait_for_persistence()
    return result


def post_signal(app, payload):
    return asyncio.run(post_signal_async(app, payload))


def consumer_request(app):
    return FakeRequest(app, headers={"authorization": f"Bearer {READER_TOKEN}"})


def test_zero_true_rules_publish_no_bet(tmp_path):
    app, repo = make_app(tmp_path)
    response = post_signal(app, signal_body(matched=[]))
    assert response["accepted"] is False
    assert response["reason"] == "NO_TRUE_RULES"
    assert repo.list_bet_signals() == []


def test_verified_signal_target_index_comes_from_collector_phase(tmp_path):
    app, _repo = make_app(tmp_path)
    body = signal_body()
    body.update(source_round_index=100, target_round_index=999)

    signal = asyncio.run(signal_api._validate_signal_body(body, app))

    assert signal["target_round_index"] == 101


def test_one_true_rule_publishes_one_bet(tmp_path):
    app, repo = make_app(tmp_path)
    response = post_signal(app, signal_body(matched=[RULE_IDS[0]]))
    assert response["accepted"] is True
    assert response["signal"]["action"] == "BET"
    assert response["signal"]["matched_rules"] == [RULE_IDS[0]]
    assert len(repo.list_bet_signals()) == 1


def test_two_true_rules_are_one_signal_with_two_matches(tmp_path):
    app, repo = make_app(tmp_path)
    response = post_signal(app, signal_body(matched=RULE_IDS[:2]))
    assert response["signal"]["matched_rules"] == RULE_IDS[:2]
    assert len(repo.list_bet_signals()) == 1


def test_four_true_rules_are_one_signal_with_four_matches(tmp_path):
    app, repo = make_app(tmp_path)
    response = post_signal(app, signal_body(matched=RULE_IDS))
    assert response["signal"]["matched_rules"] == RULE_IDS
    assert len(repo.list_bet_signals()) == 1


def test_disabled_true_rule_is_rejected_and_never_published(tmp_path):
    app, repo = make_app(tmp_path, engine=FakeEngine(disabled=[RULE_IDS[3]]))
    with pytest.raises(HTTPException) as error:
        post_signal(app, signal_body(matched=[RULE_IDS[3]], selected=RULE_IDS[:3]))
    assert error.value.status_code == 422
    assert error.value.detail["error"] == "matched_rule_was_not_selected_and_enabled"
    assert repo.list_bet_signals() == []


def test_unselected_true_rule_is_rejected_and_never_published(tmp_path):
    app, repo = make_app(tmp_path)
    with pytest.raises(HTTPException) as error:
        post_signal(app, signal_body(matched=["RULE_NOT_SELECTED"]))
    assert error.value.status_code == 422
    assert repo.list_bet_signals() == []


def test_duplicate_evaluation_for_same_session_target_is_not_republished(tmp_path):
    app, repo = make_app(tmp_path)

    async def run():
        first = await signal_api.publish_signal(FakeRequest(app, body=signal_body(matched=RULE_IDS[:2]),
            headers={"authorization": f"Bearer {ENGINE_TOKEN}"}))
        second = await signal_api.publish_signal(FakeRequest(app, body=signal_body(
            matched=RULE_IDS[2:], signal_id="different-signal-id"),
            headers={"authorization": f"Bearer {ENGINE_TOKEN}"}))
        app.state.bet_signal_hub.wait_for_persistence()
        return first, second

    first, second = asyncio.run(run())
    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["duplicate"] is True
    assert len(repo.list_bet_signals()) == 1


def test_signal_is_committed_before_realtime_fanout(tmp_path):
    app, repo = make_app(tmp_path)
    insert = repo.insert_bet_signal
    persisted_before_return = []

    def delayed_insert(signal):
        result = insert(signal)
        persisted_before_return.append(bool(repo.bet_signal_for_target(
            signal["session_id"], signal["target_round_id"])))
        return result

    repo.insert_bet_signal = delayed_insert

    async def run():
        hub = signal_api._hub(app)
        queue = hub.subscribe()
        response = await signal_api.publish_signal(FakeRequest(app, body=signal_body(),
            headers={"authorization": f"Bearer {ENGINE_TOKEN}"}))
        delivered = queue.get_nowait()
        assert response["persistence"] == "COMMITTED"
        assert delivered == response["signal"]
        hub.unsubscribe(queue)
        return hub

    asyncio.run(run())
    assert persisted_before_return == [True]
    assert len(repo.list_bet_signals()) == 1


def test_internal_signal_latency_stops_at_durable_api_acceptance(tmp_path):
    app, repo = make_app(tmp_path)
    repo.accept_bet_signal = repo.local.accept_bet_signal
    app.state.round_distribution = RoundDistributionHub()
    app.state.signal_priority_executor = InlineExecutor()
    decision_started_ns = time.perf_counter_ns() - 100_000_000
    backend_received_ns = decision_started_ns - 100_000_000
    collector_detected_at = (datetime.now(timezone.utc) - timedelta(milliseconds=250)).isoformat()

    result = asyncio.run(signal_api.publish_internal_signal(
        app, signal_body(), decision_started_ns=decision_started_ns,
        backend_received_ns=backend_received_ns,
        collector_detection_at=collector_detected_at))
    metrics = asyncio.run(app.state.round_distribution.metrics())

    assert result["accepted"] is True
    decision_acceptance = metrics["latest_samples"]["decision_to_signal_api_acceptance_ms"]
    backend_acceptance = metrics["latest_samples"]["backend_receive_to_signal_api_acceptance_ms"]
    end_to_end_acceptance = metrics["latest_samples"][
        "collector_detection_to_signal_api_acceptance_ms"]
    assert decision_acceptance["correlation_id"] == SOURCE_ID
    assert backend_acceptance["correlation_id"] == SOURCE_ID
    assert decision_acceptance["latency_ms"] > 0
    assert backend_acceptance["latency_ms"] > decision_acceptance["latency_ms"]
    assert end_to_end_acceptance["correlation_id"] == SOURCE_ID
    assert 200 <= end_to_end_acceptance["latency_ms"] < 1000
    assert result["signal"]["distribution_status"] == "ACCEPTED"


def test_transient_signal_persistence_failure_retries_before_acceptance(tmp_path):
    app, repo = make_app(tmp_path)
    insert = repo.insert_bet_signal
    attempts = 0

    def flaky_insert(signal):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("temporary database interruption")
        return insert(signal)

    repo.insert_bet_signal = flaky_insert
    response = post_signal(app, signal_body())
    assert response["accepted"] is True
    assert response["persistence"] == "COMMITTED"
    assert attempts == 3
    assert len(repo.list_bet_signals()) == 1


def test_signal_duplicate_survives_new_in_memory_hub_after_restart(tmp_path):
    app, repo = make_app(tmp_path)
    first = post_signal(app, signal_body())
    restarted_app = SimpleNamespace(state=SimpleNamespace(**dict(app.state._state)))
    restarted_app.state.bet_signal_hub = signal_api.BetSignalHub()
    second = post_signal(restarted_app, signal_body())
    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["signal"]["signal_id"] == first["signal"]["signal_id"]
    assert len(repo.list_bet_signals()) == 1


def test_stopped_shadow_session_cannot_publish(tmp_path):
    app, repo = make_app(tmp_path, engine=FakeEngine(status="MANUALLY_STOPPED",
                                                      selection_status="STOPPED"))
    with pytest.raises(HTTPException) as error:
        post_signal(app, signal_body())
    assert error.value.status_code == 409
    assert repo.list_bet_signals() == []


def test_expired_target_round_cannot_publish(tmp_path):
    app, repo = make_app(tmp_path, expired=True)
    with pytest.raises(HTTPException) as error:
        post_signal(app, signal_body())
    assert error.value.status_code == 409
    assert error.value.detail["error"] == "target_round_window_expired_or_stale"
    assert repo.list_bet_signals() == []


def test_missing_or_unverified_target_is_never_invented(tmp_path):
    app, repo = make_app(tmp_path)
    app.state.latest_provider_phase_observation = None
    with pytest.raises(HTTPException) as error:
        post_signal(app, signal_body())
    assert error.value.status_code == 409
    assert error.value.detail["error"] == "authoritative_target_identity_or_open_window_unavailable"
    assert repo.list_bet_signals() == []


def test_external_websocket_receives_the_exact_accepted_signal(tmp_path):
    app, _repo = make_app(tmp_path)

    async def run():
        websocket = FakeWebSocket(app)
        task = asyncio.create_task(signal_api.websocket_signals(websocket))
        ready = await websocket.sent.get()
        heartbeat = await websocket.sent.get()
        response = await signal_api.publish_signal(FakeRequest(app,
            body=signal_body(matched=RULE_IDS[:2]),
            headers={"authorization": f"Bearer {ENGINE_TOKEN}"}))
        delivered = await websocket.sent.get()
        app.state.bet_signal_hub.wait_for_persistence()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return ready, heartbeat, response, delivered

    ready, heartbeat, response, delivered = asyncio.run(run())
    authoritative = response["signal"]
    assert ready == {"type": "ready", "stream": "bet-signals"}
    assert heartbeat["type"] == "heartbeat"
    assert delivered == {"type": "signal", "signal": authoritative}


def test_browser_websocket_uses_one_time_origin_bound_ticket(tmp_path):
    app, _repo = make_app(tmp_path)
    origin = "http://localhost:5173"
    issued = asyncio.run(signal_api.create_websocket_ticket(FakeRequest(app, headers={
        "authorization": f"Bearer {READER_TOKEN}", "origin": origin,
    })))
    ticket_payload = json.loads(issued.body)
    assert issued.status_code == 200
    assert "no-store" in issued.headers["cache-control"]
    assert ticket_payload["expires_in_seconds"] == 30
    assert ticket_payload["ticket"]

    async def run():
        websocket = FakeWebSocket(app, authorization=None, origin=origin,
            first_message={"type": "authenticate", "ticket": ticket_payload["ticket"]})
        task = asyncio.create_task(signal_api.websocket_signals(websocket))
        ready = await asyncio.wait_for(websocket.sent.get(), timeout=1)
        heartbeat = await asyncio.wait_for(websocket.sent.get(), timeout=1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return websocket, ready, heartbeat

    websocket, ready, heartbeat = asyncio.run(run())
    assert websocket.accepted is True
    assert ready == {"type": "ready", "stream": "bet-signals"}
    assert heartbeat["type"] == "heartbeat"

    reused = FakeWebSocket(app, authorization=None, origin=origin,
        first_message={"type": "authenticate", "ticket": ticket_payload["ticket"]})
    asyncio.run(signal_api.websocket_signals(reused))
    assert reused.closed == (4401, "WEBSOCKET_TICKET_INVALID_OR_USED")

    other_ticket = asyncio.run(signal_api.create_websocket_ticket(FakeRequest(app, headers={
        "authorization": f"Bearer {READER_TOKEN}", "origin": origin,
    })))
    other_value = json.loads(other_ticket.body)["ticket"]
    # This origin is permitted by the test app, so the ticket's origin binding
    # is exercised independently of the general origin allowlist.
    app.state.settings.round_stream_allowed_origins = ["http://127.0.0.1:5173"]
    wrong_origin = FakeWebSocket(app, authorization=None, origin="http://127.0.0.1:5173",
        first_message={"type": "authenticate", "ticket": other_value})
    asyncio.run(signal_api.websocket_signals(wrong_origin))
    assert wrong_origin.closed == (4403, "WEBSOCKET_TICKET_ORIGIN_MISMATCH")


def test_invalid_websocket_credentials_and_scope_are_rejected(tmp_path):
    app, repo = make_app(tmp_path)
    repo.external_project_token = lambda _digest: None
    invalid = FakeWebSocket(app, authorization="invalid-project-token-that-is-long-enough")
    asyncio.run(signal_api.websocket_signals(invalid))
    assert invalid.closed == (4401, "INVALID_OR_REVOKED_PROJECT_TOKEN")

    repo.external_project_token = lambda _digest: {
        "token_id": "reader-no-scope", "key_type": "consumer", "scopes": [], "expires_at": None,
    }
    insufficient = FakeWebSocket(app, authorization=READER_TOKEN)
    asyncio.run(signal_api.websocket_signals(insufficient))
    assert insufficient.closed == (4403, "SIGNALS_READ_SCOPE_REQUIRED")


def test_http_routes_accept_and_return_the_same_signal(tmp_path):
    app, repo = make_app(tmp_path)
    app.include_router(signal_api.router)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            posted = await client.post("/api/v1/signals", json=signal_body(matched=RULE_IDS[:2]),
                headers={"authorization": f"Bearer {ENGINE_TOKEN}"})
            assert posted.status_code == 202
            authoritative = posted.json()["signal"]
            headers = {"authorization": f"Bearer {READER_TOKEN}"}
            latest = await client.get("/api/v1/signals/latest", headers=headers)
            listed = await client.get("/api/v1/signals", headers=headers)
            selected = await client.get("/api/v1/rules/selected", headers=headers)
            matched = await client.get("/api/v1/rules/matched", headers=headers)
            assert latest.json()["signal"] == authoritative
            assert listed.json()["signals"] == [authoritative]
            assert selected.json()["selected_rules"] == RULE_IDS
            assert matched.json()["matched_rules"] == RULE_IDS[:2]
            return authoritative

    authoritative = asyncio.run(run())
    app.state.bet_signal_hub.wait_for_persistence()
    assert repo.list_bet_signals() == [authoritative]


def test_selected_matched_rest_and_sse_endpoints_expose_same_signal(tmp_path):
    app, _repo = make_app(tmp_path)

    async def run():
        accepted = await post_signal_async(app, signal_body(matched=RULE_IDS[:2]))
        request = consumer_request(app)
        selected = await signal_api.selected_rules(request)
        matched = await signal_api.matched_rules(request)
        latest = await signal_api.latest_signal(request)
        response = await signal_api.stream_signals(request)
        chunk = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return accepted["signal"], selected, matched, latest, response, chunk

    accepted, selected, matched, latest, response, chunk = asyncio.run(run())
    assert selected["selected_rules"] == RULE_IDS
    assert matched["matched_rules"] == RULE_IDS[:2]
    assert latest["signal"] == accepted
    assert response.status_code == 200
    assert "event: bet_signal" in chunk
    assert accepted["signal_id"] in chunk
    assert accepted["target_round_id"] in chunk
    encoded = chunk.split("data: ", 1)[1].strip()
    assert json.loads(encoded) == accepted


def test_sse_storage_failure_is_returned_before_stream_response_starts(tmp_path):
    app, repo = make_app(tmp_path)
    repo.list_bet_signals = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("storage unavailable"))
    hub = signal_api._hub(app)

    with pytest.raises(HTTPException) as error:
        asyncio.run(signal_api.stream_signals(consumer_request(app)))

    assert error.value.status_code == 503
    assert error.value.detail["error"] == "signal_store_unavailable"
    assert hub.consumer_counts()["sse"] == 0


def test_latest_signal_returns_503_when_authoritative_storage_fails(tmp_path):
    app, repo = make_app(tmp_path)
    repo.list_bet_signals = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("storage unavailable"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(signal_api.latest_signal(consumer_request(app)))
    assert error.value.status_code == 503
    assert error.value.detail["error"] == "signal_store_unavailable"


def _stored_positive_signal(*, target_index=101, timestamp=None):
    signal = signal_body(timestamp=timestamp or _now().isoformat())
    signal.update(
        signal_id="stored-positive-signal",
        target_round_index=target_index,
        target_identity_status="PROVIDER_VERIFIED",
        target_identity_verified=True,
        distribution_status="ACCEPTED",
    )
    return signal


def _collector_phase_event(*, observed_at=None):
    now = _now()
    return {
        "source": "OWNED_AVIATOR_GAME_FRAME_READ_ONLY",
        "event_id": "collector-phase-test-event",
        "source_round_id": SOURCE_ID,
        "source_round_index": 100,
        "phase": "BETTING_OPEN",
        "observed_at": (observed_at or now).isoformat(),
        "provider_round_id": "provider-target-101",
        "canonical_target_round_id": TARGET_ID,
        "target_round_index": 101,
        "betting_deadline": (now + timedelta(seconds=30)).isoformat(),
        "target_identity_verified": True,
        "target_started": False,
        "target_identity_proof": {
            "provider_round_id": "provider-target-101",
            "target_round_id": TARGET_ID,
            "target_not_started": True,
            "proof_method": "platform_target_id_before_start",
        },
        "evidence": {
            "source": "AVIATOR_GAME_PHASE_READ_ONLY",
            "provider_phase_source": "EXPLICIT_PROVIDER_DATA_ATTRIBUTES",
            "provider_phase_selector": (
                "[data-aviator-phase][data-provider-round-id]"
                "[data-canonical-target-round-id][data-betting-deadline]"
            ),
            "selector": ".bet-block .btn-success.bet",
            "button_visible": True,
            "button_enabled": True,
            "stake_input_visible": True,
            "bet_tab_active": True,
        },
    }


def test_canonical_published_bet_projects_to_ok_with_actual_target_index(tmp_path):
    app, repo = make_app(tmp_path)
    source = signal_body(timestamp=_now().isoformat())
    source["source_round_index"] = 100

    published = asyncio.run(post_signal_async(app, source))
    action, diagnostic = asyncio.run(signal_api.evaluate_latest_signal_action(app))
    latest = asyncio.run(signal_api.latest_signal(consumer_request(app)))

    assert published["accepted"] is True
    assert action.model_dump() == {"action": "OK", "index": "#101"}
    assert latest["signal"]["action"] == "BET"
    assert latest["signal"]["target_round_index"] == 101
    assert diagnostic["reason"] == "CANONICAL_SIGNAL_PUBLISHED"
    assert diagnostic["validation_passed"] is True
    assert set(action.model_dump()) == {"action", "index"}


def test_action_endpoint_reads_latest_from_same_feed_and_order_as_latest(tmp_path):
    app, repo = make_app(tmp_path)
    older = _stored_positive_signal(target_index=20564, timestamp=_now().isoformat())
    older.update(signal_id="ordered-signal-older", source_round_index=20563)
    newer = _stored_positive_signal(
        target_index=20565, timestamp=(_now() - timedelta(seconds=1)).isoformat())
    newer.update(signal_id="ordered-signal-newer", source_round_index=20564, session_id="another-session")
    repo.insert_bet_signal(older)
    repo.insert_bet_signal(newer)

    latest = asyncio.run(signal_api.latest_signal(consumer_request(app)))
    listed = asyncio.run(signal_api.signals(consumer_request(app), limit=10))
    action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))

    assert latest["signal"]["signal_id"] == "ordered-signal-newer"
    assert listed["signals"][0] == latest["signal"]
    assert action.model_dump() == {"action": "OK", "index": "#20565"}


def test_unaccepted_record_is_filtered_consistently_from_all_signal_reads(tmp_path):
    app, repo = make_app(tmp_path)
    older = _stored_positive_signal(target_index=20564, timestamp=_now().isoformat())
    older.update(signal_id="older-accepted-signal", source_round_index=20563)
    newer = _stored_positive_signal(
        target_index=20565, timestamp=(_now() - timedelta(seconds=1)).isoformat())
    newer.update(signal_id="newer-rejected-signal", target_round_id="newer-unaccepted-target",
                 session_id="newer-session", source_round_index=20564,
                 distribution_status="REJECTED")
    repo.insert_bet_signal(older)
    repo.insert_bet_signal(newer)

    latest = asyncio.run(signal_api.latest_signal(consumer_request(app)))
    listed = asyncio.run(signal_api.signals(consumer_request(app), limit=10))
    action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))

    assert latest["signal"]["signal_id"] == "older-accepted-signal"
    assert [signal["signal_id"] for signal in listed["signals"]] == ["older-accepted-signal"]
    assert action.model_dump() == {"action": "OK", "index": "#20564"}


def test_a_new_published_signal_replaces_the_previous_latest_action(tmp_path):
    app, repo = make_app(tmp_path)
    first = signal_body(timestamp=_now().isoformat())
    first["source_round_index"] = 100
    asyncio.run(post_signal_async(app, first))
    first_action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))

    source_id = "round-source-101"
    target_id = "round-target-102"
    repo.committed.add(source_id)
    app.state.latest_provider_phase_observation.update({
        "source_round_id": source_id,
        "source_round_index": 101,
        "canonical_target_round_id": target_id,
        "target_round_index": 102,
    })
    second = signal_body(timestamp=(_now() + timedelta(milliseconds=20)).isoformat())
    second.update(round_id=source_id, target_round_id=target_id, source_round_index=101,
                  signal_id="second-published-signal")
    asyncio.run(post_signal_async(app, second))
    second_action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))

    assert first_action.model_dump() == {"action": "OK", "index": "#101"}
    assert second_action.model_dump() == {"action": "OK", "index": "#102"}
    assert repo.local.count_bet_signals() == 2


def test_repeated_action_polling_does_not_publish_or_mutate_signals(tmp_path):
    app, repo = make_app(tmp_path)
    payload = signal_body(timestamp=_now().isoformat())
    payload["source_round_index"] = 100
    asyncio.run(post_signal_async(app, payload))
    before = repo.local.list_bet_signals(10)
    before_count = repo.local.count_bet_signals()

    responses = [
        asyncio.run(signal_api.latest_signal_action(FakeRequest(app))).model_dump()
        for _ in range(4)
    ]

    assert responses == [{"action": "OK", "index": "#101"}] * 4
    assert repo.local.count_bet_signals() == before_count
    assert repo.local.list_bet_signals(10) == before


def test_no_published_signal_uses_latest_no_data_behavior_without_fabricated_wait(tmp_path):
    app, repo = make_app(tmp_path)

    action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))
    latest = asyncio.run(signal_api.latest_signal(consumer_request(app)))

    assert action.model_dump() == {"ok": True, "signal": None}
    assert action.model_dump() == latest
    assert repo.local.count_bet_signals() == 0


def test_action_projection_uses_target_index_and_never_source_index(tmp_path):
    app, repo = make_app(tmp_path)
    signal = _stored_positive_signal(target_index=20565)
    signal.update(signal_id="source-target-index-test", source_round_index=20564)
    repo.insert_bet_signal(signal)

    action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))

    assert action.model_dump() == {"action": "OK", "index": "#20565"}


def test_historical_bet_without_distribution_marker_remains_a_canonical_projection(tmp_path):
    app, repo = make_app(tmp_path)
    signal = _stored_positive_signal(target_index=20565)
    signal.update(signal_id="legacy-bet-signal", source_round_index=20564)
    signal.pop("distribution_status")
    repo.insert_bet_signal(signal)
    stored_before = repo.list_bet_signals(1)[0]

    action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))
    stored_after = repo.list_bet_signals(1)[0]

    assert action.model_dump() == {"action": "OK", "index": "#20565"}
    assert stored_after == stored_before


def test_missing_or_invalid_target_index_is_not_replaced_by_source_index(tmp_path):
    app, repo = make_app(tmp_path)
    signal = _stored_positive_signal(target_index=None)
    signal.update(signal_id="missing-target-index", source_round_index=20564)
    repo.insert_bet_signal(signal)

    action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))

    assert action.model_dump() == {"ok": True, "signal": None}


def test_unsupported_wait_decision_is_not_published_by_canonical_pipeline(tmp_path):
    app, _repo = make_app(tmp_path)
    payload = signal_body()
    payload["action"] = "WAIT"

    with pytest.raises(HTTPException) as error:
        asyncio.run(signal_api._validate_signal_body(payload, app))

    assert error.value.status_code == 422
    assert error.value.detail["error"] == "published_signal_action_must_be_BET"


def test_action_projection_does_not_read_rules_engine_or_collector_state(tmp_path):
    app, repo = make_app(tmp_path)
    signal = _stored_positive_signal(target_index=20565)
    signal.update(signal_id="no-runtime-dependencies", source_round_index=20564)
    repo.insert_bet_signal(signal)

    class UnavailableRuntime:
        def status(self):
            raise AssertionError("action projection must not read Rules Engine")

    app.state.rules_engine = UnavailableRuntime()
    app.state.latest_provider_phase_observation = None

    action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))

    assert action.model_dump() == {"action": "OK", "index": "#20565"}


def test_canonical_signal_database_read_does_not_block_api_event_loop(tmp_path):
    app, repo = make_app(tmp_path)
    signal = _stored_positive_signal(target_index=20565)
    signal.update(signal_id="nonblocking-feed-read", source_round_index=20564)
    repo.insert_bet_signal(signal)
    caller_thread = threading.get_ident()
    original_list = repo.list_bet_signals
    observed_threads = []

    def capture_reader_thread(*args, **kwargs):
        observed_threads.append(threading.get_ident())
        return original_list(*args, **kwargs)

    repo.list_bet_signals = capture_reader_thread
    action = asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))

    assert action.model_dump() == {"action": "OK", "index": "#20565"}
    assert observed_threads
    assert observed_threads[0] != caller_thread


def test_latest_signal_action_reports_database_failure(tmp_path):
    app, repo = make_app(tmp_path)
    repo.list_bet_signals = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("storage unavailable"))

    with pytest.raises(HTTPException) as error:
        asyncio.run(signal_api.latest_signal_action(FakeRequest(app)))

    assert error.value.status_code == 503
    assert error.value.detail["error"] == "signal_store_unavailable"


def test_signal_router_registers_all_required_endpoints():
    routes = {route.path for route in signal_api.router.routes}
    assert routes == {
        "/api/v1/signals", "/api/v1/signals/latest", "/api/v1/signals/latest/action",
        "/api/v1/internal/behaviour/strategy-eligibility",
        "/api/v1/signals/{signal_id}",
        "/api/v1/publisher/heartbeat", "/api/v1/rules/selected",
        "/api/v1/rules/matched", "/api/v1/ws/signals", "/api/v1/ws/ticket",
        "/api/v1/stream/signals",
    }


def test_latest_signal_action_is_mounted_on_production_app_as_public_read():
    path = "/api/v1/signals/latest/action"
    # FastAPI 0.137 represents included routers lazily in app.routes; OpenAPI
    # resolves those routes and verifies the production application exposes it.
    assert path in production_app.app.openapi()["paths"]
    assert "get" in production_app.app.openapi()["paths"][path]
    assert ("GET", path) in production_app.PUBLIC_API_READS


def test_post_signal_openapi_documents_the_node_engine_contract():
    app = FastAPI()
    app.include_router(signal_api.router)
    operation = app.openapi()["paths"]["/api/v1/signals"]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["required"]) == signal_api.REQUIRED_SIGNAL_FIELDS
    assert schema["properties"]["action"]["const"] == "BET"
    assert schema["properties"]["mode"]["const"] == "SHADOW"
    assert "user selected and enabled" in schema["properties"]["selected_rules"]["description"]
    assert "evaluated TRUE" in schema["properties"]["matched_rules"]["description"]


def test_latest_signal_action_openapi_documents_projection_and_no_data_responses():
    app = FastAPI()
    app.include_router(signal_api.router)
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/signals/latest/action"]["get"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    response_examples = operation["responses"]["200"]["content"]["application/json"]["examples"]
    model_schema = schema["components"]["schemas"]["LatestSignalActionResponse"]

    assert operation["summary"] == "Get the latest Aviator signal action (public)"
    assert operation["x-api-key-required"] is False
    assert {item["$ref"].rsplit("/", 1)[-1] for item in response_schema["anyOf"]} == {
        "LatestSignalActionResponse", "LatestSignalNoDataResponse",
    }
    assert model_schema["required"] == ["action", "index"]
    assert model_schema["additionalProperties"] is False
    assert model_schema["properties"]["action"]["enum"] == ["OK", "WAIT"]
    assert model_schema["properties"]["index"]["type"] == "string"
    assert model_schema["properties"]["index"]["pattern"] == r"^#[1-9][0-9]*$"
    assert model_schema["examples"] == [
        {"action": "OK", "index": "#20063"},
        {"action": "WAIT", "index": "#20064"},
    ]
    assert response_examples["ok"]["value"] == {"action": "OK", "index": "#20063"}
    assert response_examples["wait"]["value"] == {"action": "WAIT", "index": "#20064"}
    no_data_schema = schema["components"]["schemas"]["LatestSignalNoDataResponse"]
    assert no_data_schema["required"] == ["ok", "signal"]
    assert no_data_schema["additionalProperties"] is False
    assert no_data_schema["properties"]["signal"]["type"] == "null"
    assert '{"ok":true,"signal":null}' in operation["description"]
