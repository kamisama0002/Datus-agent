from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from fastapi import Request
import pytest


SERVICE_TOKEN = "unit-test-service-token"
PROTOCOL = "nanzi-datus/v1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def project_id(agent_id: str = "agent-17", datasource_id: str = "17") -> str:
    digest = hashlib.sha256(f"default:{agent_id}:{datasource_id}".encode()).hexdigest()
    return f"nzp_{digest[:32]}"


def request_for(
    *,
    token: str = SERVICE_TOKEN,
    protocol: str = PROTOCOL,
    agent_id: str = "agent-17",
    datasource_id: str = "17",
    user_id: str = "user-23",
    trace_id: str = "trace-29",
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
        "config_mutable": False,
        "bash": {"enabled": False},
        "query_limits": {
            "allowed_statement_types": ["select", "cte", "explain"],
            "timeout_seconds": 60,
            "max_rows": 1000,
            "max_result_bytes": 2 * 1024 * 1024,
        },
        "config_fingerprint": fingerprint,
    }
    if overrides:
        body.update(overrides)
    return body
