from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from fastapi import Request
import httpx
import pytest

from datus.api.models.cli_models import SSEEndData, SSEEvent, SSESessionData, StreamChatInput
from datus.api.services.action_sse_converter import action_to_sse_event
from datus.schemas.action_history import ActionHistory, ActionRole, ActionStatus
from nanzi_datus_bridge.auth_provider import NanziAuthProvider, NanziConfigurationError
from nanzi_datus_bridge.nanzi_client import NANZI_DATUS_PROTOCOL
from nanzi_datus_bridge.runtime_settings import service_token_status
from tests.nanzi_bridge.conftest import project_config, runtime_project_id


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "nanzi_datus" / "v1"
SESSION_ID = "nzs_2d0a3dae-581d-5e30-abc7-4ea3bc5e6553"
MESSAGE_ID = "nanzi-v1-message-0001"
FIXED_TIME = datetime(2026, 8, 14, tzinfo=timezone.utc)


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _timestamp(seconds: int) -> str:
    return (FIXED_TIME + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _converted_event(event_id: int, action: ActionHistory, *, message_id: str = MESSAGE_ID, **kwargs) -> dict:
    event = action_to_sse_event(action, event_id=event_id, message_id=message_id, **kwargs)
    assert event is not None
    return event.model_dump(mode="json")


def _canonical_events() -> list[dict]:
    session = SSEEvent(
        id=1,
        event="session",
        data=SSESessionData(session_id=SESSION_ID, llm_session_id=None),
        timestamp=_timestamp(0),
    ).model_dump(mode="json")
    create = _converted_event(
        2,
        ActionHistory(
            action_id="thinking-1",
            role=ActionRole.ASSISTANT,
            status=ActionStatus.PROCESSING,
            action_type="thinking_delta",
            output={"delta": "Checking"},
            start_time=FIXED_TIME + timedelta(seconds=1),
        ),
        stream_thinking=True,
        is_first_delta=True,
    )
    append = _converted_event(
        3,
        ActionHistory(
            action_id="thinking-2",
            role=ActionRole.ASSISTANT,
            status=ActionStatus.PROCESSING,
            action_type="thinking_delta",
            output={"delta": " completed orders"},
            start_time=FIXED_TIME + timedelta(seconds=2),
        ),
        stream_thinking=True,
        is_first_delta=False,
    )
    answer = _converted_event(
        4,
        ActionHistory(
            action_id="response-1",
            role=ActionRole.ASSISTANT,
            status=ActionStatus.SUCCESS,
            action_type="response",
            output={
                "sql": "SELECT COUNT(*) AS count FROM orders WHERE status = 'completed'",
                "response": "The completed-order count is 42.",
                "is_thinking": False,
            },
            start_time=FIXED_TIME + timedelta(seconds=3),
        ),
        message_id="nanzi-v1-answer-0001",
    )
    tool_call = _converted_event(
        5,
        ActionHistory(
            action_id="nanzi-v1-tool-0001",
            role=ActionRole.TOOL,
            status=ActionStatus.PROCESSING,
            action_type="tool_call",
            input={
                "function_name": "execute_sql",
                "arguments": {"sql": "SELECT COUNT(*) AS count FROM orders WHERE status = 'completed'"},
            },
            start_time=FIXED_TIME + timedelta(seconds=4),
        ),
        message_id="nanzi-v1-tool-message-0001",
        proxied_tool_names={"execute_sql"},
    )
    tool_result = _converted_event(
        6,
        ActionHistory(
            action_id="complete_nanzi-v1-tool-0001",
            role=ActionRole.TOOL,
            status=ActionStatus.SUCCESS,
            action_type="tool_call",
            input={"function_name": "execute_sql", "arguments": {}},
            output={"summary": "Count query completed", "success": 1, "result": {"row_count": 1}},
            start_time=FIXED_TIME + timedelta(seconds=4),
            end_time=FIXED_TIME + timedelta(seconds=5),
        ),
        message_id="nanzi-v1-tool-message-0001",
        proxied_tool_names={"execute_sql"},
    )
    usage = _converted_event(
        7,
        ActionHistory(
            action_id=SESSION_ID,
            role=ActionRole.ASSISTANT,
            status=ActionStatus.SUCCESS,
            action_type="token_usage",
            output={
                "cumulative": {
                    "requests": 1,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                },
                "delta": {
                    "requests": 1,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                },
                "context_length": 128000,
                "last_call_input_tokens": 10,
            },
            start_time=FIXED_TIME + timedelta(seconds=6),
        ),
    )
    end = SSEEvent(
        id=8,
        event="end",
        data=SSEEndData(
            session_id=SESSION_ID,
            llm_session_id=None,
            total_events=8,
            action_count=4,
            duration=0.25,
            requests=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cached_tokens=0,
            session_total_tokens=10,
            context_length=128000,
        ),
        timestamp=_timestamp(7),
    ).model_dump(mode="json")
    return [session, create, append, answer, tool_call, tool_result, usage, end]


def test_contract_manifest_hashes_exact_canonical_fixture_bytes() -> None:
    manifest = _load_fixture("contract-manifest.json")
    assert manifest["protocol"] == NANZI_DATUS_PROTOCOL
    assert manifest["hash_algorithm"] == "sha256"
    assert set(manifest["fixtures"]) == {"request.json", "sse.json"}
    for filename, expected_hash in manifest["fixtures"].items():
        actual_hash = hashlib.sha256((FIXTURE_ROOT / filename).read_bytes()).hexdigest()
        assert actual_hash == expected_hash


def test_raw_request_fixture_bearer_is_rejected_by_runtime_validation() -> None:
    fixture = _load_fixture("request.json")
    raw_token = fixture["request"]["headers"]["Authorization"].removeprefix("Bearer ")

    assert service_token_status(raw_token) == "placeholder"
    with pytest.raises(NanziConfigurationError):
        NanziAuthProvider(callback_url="http://127.0.0.1:8000", service_token=raw_token)


@pytest.mark.anyio
async def test_request_fixture_is_exact_provider_and_chat_dto_contract() -> None:
    fixture = deepcopy(_load_fixture("request.json"))
    callback_requests: list[httpx.Request] = []
    service_token = "fixture-test-only-secret-9c8e7a6b5d4f"
    fixture["request"]["headers"]["Authorization"] = f"Bearer {service_token}"

    async def callback(request: httpx.Request) -> httpx.Response:
        callback_requests.append(request)
        payload = project_config()
        payload["model"]["enable_thinking"] = True
        payload["model"]["reasoning_effort"] = "xhigh"
        return httpx.Response(200, json=payload)

    assert fixture["protocol"] == NANZI_DATUS_PROTOCOL
    assert set(fixture["request"]["headers"]) == {
        "Accept",
        "Authorization",
        "Content-Type",
        "X-Nanzi-Agent-Id",
        "X-Nanzi-Datasource-Id",
        "X-Nanzi-Datus-Protocol",
        "X-Nanzi-Model-Id",
        "X-Nanzi-Project-Id",
        "X-Nanzi-Reasoning-Effort",
        "X-Nanzi-Thinking-Enable",
        "X-Nanzi-Trace-Id",
        "X-Nanzi-User-Id",
    }
    request = Request(
        {
            "type": "http",
            "method": fixture["request"]["method"],
            "path": fixture["request"]["path"],
            "headers": [
                (name.lower().encode("ascii"), value.encode("utf-8"))
                for name, value in fixture["request"]["headers"].items()
            ],
        }
    )
    provider = NanziAuthProvider(
        callback_url="http://127.0.0.1:8000",
        service_token=service_token,
        http_transport=httpx.MockTransport(callback),
    )
    context = await provider.authenticate(request)
    body = StreamChatInput.model_validate(fixture["request"]["json"])

    assert fixture["request"]["method"] == "POST"
    assert fixture["request"]["path"] == "/api/v1/chat/stream"
    assert body.model_dump(mode="json", exclude_none=True) == fixture["request"]["json"]
    assert body.session_id == SESSION_ID
    assert body.orchestrator_context is not None
    assert body.orchestrator_context.recall_policy.mode == "none"
    assert body.orchestrator_context.recall_policy.requires_fresh_query is True
    assert body.orchestrator_context.response_policy.mode == "concise"
    assert body.orchestrator_context.compression.source_tokens == 120
    assert context.project_id == runtime_project_id(
        "deepseek/deepseek-chat",
        thinking_enable=True,
        reasoning_effort="xhigh",
    )
    assert context.user_id == "user-23"
    assert context.config.active_model().enable_thinking is True
    assert context.config.active_model().reasoning_effort == "xhigh"
    assert callback_requests[0].headers["X-Trace-Id"] == "trace-29"
    assert callback_requests[0].headers["X-Nanzi-Datus-Protocol"] == NANZI_DATUS_PROTOCOL
    assert callback_requests[0].headers["X-Nanzi-Model-Id"] == "deepseek/deepseek-chat"
    assert callback_requests[0].headers["X-Nanzi-Thinking-Enable"] == "true"
    assert callback_requests[0].headers["X-Nanzi-Reasoning-Effort"] == "xhigh"


def test_sse_fixture_is_exact_converter_and_dto_wire_contract() -> None:
    fixture = _load_fixture("sse.json")
    events = [SSEEvent.model_validate(event) for event in fixture["events"]]
    serialized = json.dumps(fixture, sort_keys=True)
    assert fixture == {
        "protocol": NANZI_DATUS_PROTOCOL,
        "content_type": "text/event-stream",
        "events": _canonical_events(),
    }
    assert [event.event for event in events] == [
        "session",
        "message",
        "message",
        "message",
        "message",
        "message",
        "usage",
        "end",
    ]
    assert [event.id for event in events] == list(range(1, 9))
    for forbidden in (
        "database-password",
        "model-api-key",
        "mysql.internal",
        "models.internal",
        "callback_response",
        "sql_result",
    ):
        assert forbidden not in serialized.lower()
