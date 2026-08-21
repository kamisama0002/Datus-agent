from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi import Request

SERVICE_TOKEN = "unit-test-service-token"
PROTOCOL = "nanzi-datus/v1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def project_id(agent_id: str = "agent-17", datasource_id: str = "17") -> str:
    digest = hashlib.sha256(f"default:{agent_id}:{datasource_id}".encode()).hexdigest()
    return f"nzp_{digest[:32]}"


def runtime_project_id(
    model_id: str,
    *,
    thinking_enable: bool | None = None,
    reasoning_effort: str | None = None,
) -> str:
    if thinking_enable is None and reasoning_effort is None:
        material = b"nanzi-datus-model-runtime-v1\0" + model_id.encode("ascii")
    else:
        material = (
            b"nanzi-datus-reasoning-runtime-v1\0"
            + model_id.encode("ascii")
            + b"\0"
            + str(bool(thinking_enable)).lower().encode("ascii")
            + b"\0"
            + str(reasoning_effort or "").encode("ascii")
        )
    digest = hashlib.sha256(material).hexdigest()
    return f"{project_id()}_m_{digest[:32]}"


def request_for(
    *,
    token: str = SERVICE_TOKEN,
    protocol: str = PROTOCOL,
    agent_id: str = "agent-17",
    datasource_id: str = "17",
    user_id: str = "user-23",
    trace_id: str = "trace-29",
    model_id: str | None = None,
    thinking_enable: bool | None = None,
    reasoning_effort: str | None = None,
    supplied_project_id: str | None = None,
    omit: set[str] | None = None,
) -> Request:
    values = {
        "authorization": f"Bearer {token}",
        "x-nanzi-datus-protocol": protocol,
        "x-nanzi-project-id": supplied_project_id or project_id(agent_id, datasource_id),
        "x-nanzi-user-id": user_id,
        "x-nanzi-agent-id": agent_id,
        "x-nanzi-datasource-id": datasource_id,
        "x-nanzi-trace-id": trace_id,
    }
    if model_id is not None:
        values["x-nanzi-model-id"] = model_id
    if thinking_enable is not None:
        values["x-nanzi-thinking-enable"] = "true" if thinking_enable else "false"
    if reasoning_effort is not None:
        values["x-nanzi-reasoning-effort"] = reasoning_effort
    omitted = omit or set()
    headers = [(name.encode("ascii"), value.encode("utf-8")) for name, value in values.items() if name not in omitted]
    return Request({"type": "http", "method": "POST", "path": "/api/v1/chat/stream", "headers": headers})


def project_config(
    *,
    agent_id: str = "agent-17",
    datasource_id: int = 17,
    fingerprint: str = "a" * 64,
    password: str = "database-password",
    supplied_project_id: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "project_id": supplied_project_id or project_id(agent_id, str(datasource_id)),
        "agent_id": agent_id,
        "datasource": {
            "id": datasource_id,
            "type": "mysql",
            "host": "mysql.internal",
            "port": 3306,
            "username": "readonly_user",
            "password": password,
            "database": "sales",
        },
        "model": {
            "type": "openai",
            "model": "gpt-4.1-mini",
            "api_key": "test-model-api-key",
            "base_url": "https://models.internal/v1",
            "default_headers": {
                "x-openai-actor-authorization": "local-image-extension",
            },
            "enable_thinking": False,
            "reasoning_effort": "off",
        },
        "config_mutable": False,
        "bash": {"enabled": False},
        "query_limits": {
            "allowed_statement_types": ["select", "cte", "explain"],
            "timeout_seconds": 60,
            "max_rows": 1000,
            "max_result_bytes": 2 * 1024 * 1024,
        },
        "skills": [],
        "config_fingerprint": fingerprint,
    }
    if overrides:
        body.update(overrides)
    return body
