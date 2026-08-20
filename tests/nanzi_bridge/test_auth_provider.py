from __future__ import annotations

import hashlib
from collections import Counter

import httpx
import pytest

from nanzi_datus_bridge.auth_provider import (
    NanziAuthenticationError,
    NanziAuthProvider,
    NanziConfigurationError,
)
from tests.nanzi_bridge.conftest import PROTOCOL, SERVICE_TOKEN, project_config, project_id, request_for


def _provider(http_transport: httpx.AsyncBaseTransport, **kwargs) -> NanziAuthProvider:
    return NanziAuthProvider(
        callback_url="http://127.0.0.1:8000",
        service_token=SERVICE_TOKEN,
        protocol=PROTOCOL,
        http_transport=http_transport,
        **kwargs,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "incoming_request",
    [
        request_for(token="wrong-token"),
        request_for(protocol="nanzi-datus/v2"),
        request_for(omit={"authorization"}),
        request_for(omit={"x-nanzi-user-id"}),
        request_for(supplied_project_id="nzp_" + "0" * 32),
        request_for(model_id="model id"),
        request_for(model_id="模型"),
    ],
)
async def test_rejects_untrusted_or_inconsistent_requests_before_callback(incoming_request) -> None:
    callback_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal callback_calls
        callback_calls += 1
        return httpx.Response(200, json=project_config())

    with pytest.raises(NanziAuthenticationError):
        await _provider(httpx.MockTransport(handler)).authenticate(incoming_request)

    assert callback_calls == 0


@pytest.mark.anyio
async def test_returns_stable_native_app_context() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=project_config())

    context = await _provider(httpx.MockTransport(handler)).authenticate(request_for())

    assert context.user_id == "user-23"
    assert context.project_id == project_id()
    assert context.config is not None
    assert context.config.current_datasource == "nanzi_17"
    assert context.principal == {
        "tenant_id": "default",
        "agent_id": "agent-17",
        "datasource_id": "17",
    }


@pytest.mark.anyio
async def test_reuses_unexpired_fingerprint_and_evicts_once_after_change() -> None:
    now = [100.0]
    callback_calls = 0
    responses = [
        project_config(fingerprint="a" * 64),
        project_config(fingerprint="b" * 64, password="rotated-password"),
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal callback_calls
        body = responses[min(callback_calls, len(responses) - 1)]
        callback_calls += 1
        return httpx.Response(200, json=body)

    evicted: list[str] = []

    async def on_evict(value: str) -> None:
        evicted.append(value)

    provider = _provider(httpx.MockTransport(handler), clock=lambda: now[0])
    provider.on_evict(on_evict)
    first = await provider.authenticate(request_for())
    second = await provider.authenticate(request_for())
    now[0] += 31.0
    third = await provider.authenticate(request_for())

    assert first.config is second.config
    assert third.config is not first.config
    assert callback_calls == 2
    assert evicted == [project_id()]


@pytest.mark.anyio
async def test_selected_models_use_separate_runtime_projects_and_config_caches() -> None:
    callback_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model_id = request.headers["X-Nanzi-Model-Id"]
        callback_models.append(model_id)
        body = project_config(
            fingerprint=hashlib.sha256(model_id.encode()).hexdigest()
        )
        body["model"]["model"] = model_id
        return httpx.Response(200, json=body)

    evicted: list[str] = []

    async def on_evict(value: str) -> None:
        evicted.append(value)

    provider = _provider(httpx.MockTransport(handler))
    provider.on_evict(on_evict)
    deepseek = await provider.authenticate(
        request_for(model_id="deepseek/deepseek-chat")
    )
    qwen = await provider.authenticate(request_for(model_id="qwen/qwen3-32b"))
    deepseek_again = await provider.authenticate(
        request_for(model_id="deepseek/deepseek-chat")
    )

    assert callback_models == ["deepseek/deepseek-chat", "qwen/qwen3-32b"]
    assert deepseek.project_id != qwen.project_id
    assert deepseek.project_id == deepseek_again.project_id
    assert deepseek.config is deepseek_again.config
    assert deepseek.config.session_dir == qwen.config.session_dir
    assert deepseek.principal["model_id"] == "deepseek/deepseek-chat"
    assert qwen.principal["model_id"] == "qwen/qwen3-32b"
    assert evicted == []


@pytest.mark.anyio
async def test_same_fingerprint_with_different_payload_fails_closed() -> None:
    now = [100.0]
    responses = [project_config(), project_config(password="changed-without-new-fingerprint")]
    callback_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal callback_calls
        body = responses[callback_calls]
        callback_calls += 1
        return httpx.Response(200, json=body)

    provider = _provider(httpx.MockTransport(handler), clock=lambda: now[0])
    await provider.authenticate(request_for())
    now[0] += 31.0
    with pytest.raises(NanziConfigurationError, match="incompatible") as exc_info:
        await provider.authenticate(request_for())

    assert "changed-without-new-fingerprint" not in str(exc_info.value)


@pytest.mark.anyio
async def test_unsupported_callback_model_type_fails_closed_without_secret_leak() -> None:
    body = project_config()
    body["model"]["type"] = "anthropic"
    body["model"]["api_key"] = "callback-model-secret-that-must-not-leak"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(NanziConfigurationError) as exc_info:
        await _provider(httpx.MockTransport(handler)).authenticate(request_for())

    assert "callback-model-secret-that-must-not-leak" not in str(exc_info.value)
    assert "anthropic" not in str(exc_info.value)


@pytest.mark.anyio
async def test_project_config_cache_is_bounded() -> None:
    calls: Counter[str] = Counter()

    async def handler(request: httpx.Request) -> httpx.Response:
        pid = request.url.path.split("/")[-2]
        calls[pid] += 1
        datasource_id = int(request.headers["X-Nanzi-Datasource-Id"])
        return httpx.Response(
            200,
            json=project_config(
                agent_id=request.headers["X-Nanzi-Agent-Id"],
                datasource_id=datasource_id,
                fingerprint=f"{datasource_id:x}" * 64,
            ),
        )

    provider = _provider(httpx.MockTransport(handler), max_cache_entries=2)
    for number in (1, 2, 3, 1):
        await provider.authenticate(request_for(agent_id=f"agent-{number}", datasource_id=str(number)))

    assert calls[project_id("agent-1", "1")] == 2
    assert sum(calls.values()) == 4


@pytest.mark.parametrize("token", [None, "", "${MISSING_TOKEN}", "change-me", "placeholder"])
def test_missing_or_placeholder_service_token_fails_closed(monkeypatch, token) -> None:
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    with pytest.raises(NanziConfigurationError):
        NanziAuthProvider(callback_url="http://127.0.0.1:8000", service_token=token)


@pytest.mark.parametrize(
    "callback_url",
    [
        "https://127.0.0.1:8000",
        "http://127.0.0.1",
        "http://example.com:8000",
        "http://localhost:8000?next=http://127.0.0.1:8000",
        "http://localhost:8000#fragment",
    ],
)
def test_rejects_unsafe_callback_url_during_provider_construction(callback_url) -> None:
    with pytest.raises(NanziConfigurationError, match="configuration is unavailable"):
        NanziAuthProvider(callback_url=callback_url, service_token=SERVICE_TOKEN)
