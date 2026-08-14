from __future__ import annotations

import json
from pathlib import Path

from fastapi import Request
import httpx
import pytest

from datus.api.models.cli_models import SSEEvent
from nanzi_datus_bridge.auth_provider import NanziAuthProvider
from nanzi_datus_bridge.nanzi_client import NANZI_DATUS_PROTOCOL
from tests.nanzi_bridge.conftest import SERVICE_TOKEN, project_config, project_id


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "nanzi_datus" / "v1"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.anyio
async def test_request_fixture_drives_auth_provider_contract() -> None:
    fixture = _load_fixture("request.json")
    callback_requests: list[httpx.Request] = []

    async def callback(request: httpx.Request) -> httpx.Response:
        callback_requests.append(request)
        return httpx.Response(200, json=project_config())

    headers = dict(fixture["request"]["headers"])
    headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"
    request = Request(
        {
            "type": "http",
            "method": fixture["request"]["method"],
            "path": fixture["request"]["path"],
            "headers": [(name.lower().encode("ascii"), value.encode("utf-8")) for name, value in headers.items()],
        }
    )
    provider = NanziAuthProvider(
        callback_url="http://nanzi.local",
        service_token=SERVICE_TOKEN,
        http_transport=httpx.MockTransport(callback),
    )

    context = await provider.authenticate(request)

    assert fixture["protocol"] == NANZI_DATUS_PROTOCOL
    assert context.project_id == project_id()
    assert context.user_id == "user-23"
    assert callback_requests[0].headers["X-Nanzi-Datus-Protocol"] == NANZI_DATUS_PROTOCOL


def test_sse_fixture_matches_public_event_models_and_is_secret_free() -> None:
    fixture = _load_fixture("sse.json")
    events = [SSEEvent.model_validate(event) for event in fixture["events"]]
    serialized = json.dumps(fixture, sort_keys=True)

    assert fixture["protocol"] == NANZI_DATUS_PROTOCOL
    assert fixture["content_type"] == "text/event-stream"
    assert [event.event for event in events] == ["session", "message", "end"]
    assert [event.id for event in events] == [1, 2, 3]
    for forbidden in (
        "authorization",
        "service_token",
        "password",
        "api_key",
        "callback_response",
        "sql_result",
    ):
        assert forbidden not in serialized.lower()
