from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
import httpx
import pytest

from datus.api import deps
from datus.api.auth.context import AppContext
from datus.api.routes import config_routes
from datus.api.services.datus_service import DatusService
from datus.api.services.datus_service_cache import DatusServiceCache
from nanzi_datus_bridge.auth_provider import NanziAuthProvider
from tests.nanzi_bridge.conftest import PROTOCOL, SERVICE_TOKEN, project_config, project_id, request_for


class _CapturingCache:
    def __init__(self) -> None:
        self.service = object()
        self.project_id: str | None = None
        self.expected_fingerprint: str | None = None

    async def get_or_create(self, project_id, _factory, expected_fingerprint=None):
        self.project_id = project_id
        self.expected_fingerprint = expected_fingerprint
        return self.service

    async def evict(self, _project_id: str) -> None:
        return None


@pytest.mark.anyio
async def test_native_service_dependency_uses_provider_and_exact_request_contract(monkeypatch) -> None:
    seen_callback: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_callback
        seen_callback = request
        return httpx.Response(200, json=project_config())

    provider = NanziAuthProvider(
        callback_url="http://nanzi.local",
        service_token=SERVICE_TOKEN,
        protocol=PROTOCOL,
        http_transport=httpx.MockTransport(handler),
    )
    cache = _CapturingCache()
    preserved = {
        name: getattr(deps, name)
        for name in (
            "_auth_provider",
            "_service_cache",
            "_datasource",
            "_default_source",
            "_default_interactive",
            "_stream_thinking",
        )
    }
    for name, value in preserved.items():
        monkeypatch.setattr(deps, name, value)
    deps.init_deps(provider, cache)

    request = request_for()
    service = await deps.get_datus_service(request)
    context = deps.get_app_context(request)

    assert service is cache.service
    assert context.project_id == project_id()
    assert cache.project_id == project_id()
    assert cache.expected_fingerprint == DatusService.compute_fingerprint(context.config)
    assert context.config.nanzi_config_fingerprint == "a" * 64
    assert seen_callback is not None
    assert seen_callback.headers["Authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert seen_callback.headers["X-Nanzi-Datus-Protocol"] == PROTOCOL
    assert seen_callback.headers["X-Nanzi-Project-Id"] == project_id()
    assert seen_callback.headers["X-Nanzi-User-Id"] == "user-23"
    assert seen_callback.headers["X-Nanzi-Agent-Id"] == "agent-17"
    assert seen_callback.headers["X-Nanzi-Datasource-Id"] == "17"
    assert seen_callback.headers["X-Trace-Id"] == "trace-29"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/config/datasources", {"datasources": {"other": {"type": "mysql"}}}),
        ("/api/v1/config/models", {"models": {"other": {"type": "openai"}}, "target": "other"}),
    ],
)
async def test_native_put_routes_reject_immutable_context_before_save(path, payload) -> None:
    app = FastAPI()
    app.include_router(config_routes.router)
    service = MagicMock()
    context = AppContext(
        user_id="user-23",
        project_id=project_id(),
        config=SimpleNamespace(config_mutable=False),
    )
    app.dependency_overrides[deps.get_datus_service] = lambda: service
    app.dependency_overrides[deps.get_app_context] = lambda: context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://datus.local") as client:
        response = await client.put(path, json=payload)

    assert response.status_code == 403
    assert response.json()["detail"] == "Project configuration is immutable"
    assert service.mock_calls == []


def _service(*, fingerprint: str, active: bool):
    service = MagicMock()
    service.config_fingerprint = fingerprint
    service.has_active_tasks.return_value = active
    service.shutdown = AsyncMock()
    service.task_manager.wait_all_tasks = AsyncMock()
    return service


@pytest.mark.anyio
async def test_fingerprint_eviction_keeps_active_task_routes_until_drain() -> None:
    cache = DatusServiceCache()
    active = _service(fingerprint="fp-old", active=True)
    replacement = _service(fingerprint="fp-new", active=False)
    await cache.get_or_create("project", AsyncMock(return_value=active))

    await cache.evict("project")
    factory = AsyncMock(return_value=replacement)
    routed = [
        await cache.get_or_create("project", factory, expected_fingerprint="fp-new")
        for _route in ("stop", "resume", "user-interaction")
    ]

    assert routed == [active, active, active]
    factory.assert_not_awaited()
    active.shutdown.assert_not_awaited()
    active.task_manager.wait_all_tasks.assert_not_awaited()

    active.has_active_tasks.return_value = False
    rebuilt = await cache.get_or_create("project", factory, expected_fingerprint="fp-new")
    assert rebuilt is replacement
    active.shutdown.assert_awaited_once()
