from __future__ import annotations

import httpx
import pytest

from nanzi_datus_bridge.nanzi_client import NanziCallbackError, NanziClient
from tests.nanzi_bridge.conftest import PROTOCOL, SERVICE_TOKEN, project_config, project_id


@pytest.mark.anyio
async def test_calls_exact_internal_project_config_contract() -> None:
    seen_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(200, json=project_config())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = NanziClient(
            base_url="http://nanzi.local/",
            service_token=SERVICE_TOKEN,
            protocol=PROTOCOL,
            http_client=http_client,
        )
        body = await client.fetch_project_config(
            project_id=project_id(),
            user_id="user-23",
            agent_id="agent-17",
            datasource_id="17",
            trace_id="trace-29",
        )

    assert body == project_config()
    assert seen_request is not None
    assert seen_request.method == "GET"
    assert seen_request.url == httpx.URL(
        f"http://nanzi.local/api/internal/datus/v1/projects/{project_id()}/config"
    )
    assert seen_request.headers["Authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert seen_request.headers["X-Nanzi-Datus-Protocol"] == PROTOCOL
    assert seen_request.headers["X-Trace-Id"] == "trace-29"
    assert seen_request.headers["X-Nanzi-Project-Id"] == project_id()
    assert seen_request.headers["X-Nanzi-User-Id"] == "user-23"
    assert seen_request.headers["X-Nanzi-Agent-Id"] == "agent-17"
    assert seen_request.headers["X-Nanzi-Datasource-Id"] == "17"


@pytest.mark.anyio
@pytest.mark.parametrize("response", [httpx.Response(503, text="database-password"), httpx.Response(200, text="{")])
async def test_sanitizes_callback_status_and_json_failures(response: httpx.Response) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = NanziClient(
            base_url="http://nanzi.local",
            service_token=SERVICE_TOKEN,
            http_client=http_client,
        )
        with pytest.raises(NanziCallbackError) as exc_info:
            await client.fetch_project_config(
                project_id=project_id(),
                user_id="user-23",
                agent_id="agent-17",
                datasource_id="17",
                trace_id="trace-29",
            )

    message = str(exc_info.value)
    assert "database-password" not in message
    assert SERVICE_TOKEN not in message
    assert "project configuration is unavailable" in message
