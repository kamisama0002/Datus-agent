from __future__ import annotations

import httpx
import pytest

import nanzi_datus_bridge.nanzi_client as nanzi_client_module
from nanzi_datus_bridge.nanzi_client import NanziCallbackError, NanziClient
from tests.nanzi_bridge.conftest import PROTOCOL, SERVICE_TOKEN, project_config, project_id


@pytest.mark.anyio
async def test_calls_exact_internal_project_config_contract() -> None:
    seen_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(200, json=project_config())

    client = NanziClient(
        base_url="http://nanzi.local/",
        service_token=SERVICE_TOKEN,
        protocol=PROTOCOL,
        http_transport=httpx.MockTransport(handler),
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

    client = NanziClient(
        base_url="http://nanzi.local",
        service_token=SERVICE_TOKEN,
        http_transport=httpx.MockTransport(handler),
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


@pytest.mark.anyio
async def test_does_not_follow_callback_redirects() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "http://attacker.invalid/steal"})

    client = NanziClient(
        base_url="http://nanzi.local",
        service_token=SERVICE_TOKEN,
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(NanziCallbackError, match="project configuration is unavailable"):
        await client.fetch_project_config(
            project_id=project_id(),
            user_id="user-23",
            agent_id="agent-17",
            datasource_id="17",
            trace_id="trace-29",
        )

    assert len(requests) == 1
    assert requests[0].url.host == "nanzi.local"


@pytest.mark.anyio
async def test_http_client_always_disables_environment_proxies(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid:8080")
    constructor_kwargs: list[dict] = []
    real_async_client = httpx.AsyncClient

    def recording_async_client(*args, **kwargs):
        constructor_kwargs.append(dict(kwargs))
        return real_async_client(*args, **kwargs)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=project_config())

    monkeypatch.setattr(nanzi_client_module.httpx, "AsyncClient", recording_async_client)
    client = NanziClient(
        base_url="http://nanzi.local",
        service_token=SERVICE_TOKEN,
        http_transport=httpx.MockTransport(handler),
    )
    await client.fetch_project_config(
        project_id=project_id(),
        user_id="user-23",
        agent_id="agent-17",
        datasource_id="17",
        trace_id="trace-29",
    )

    assert len(constructor_kwargs) == 1
    assert constructor_kwargs[0]["timeout"] == httpx.Timeout(3.0)
    assert constructor_kwargs[0]["follow_redirects"] is False
    assert constructor_kwargs[0]["trust_env"] is False
    assert constructor_kwargs[0]["transport"] is client._http_transport
