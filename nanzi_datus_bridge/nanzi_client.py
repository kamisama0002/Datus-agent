"""Sanitized HTTP client for NanZi's internal Datus project-config API."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

NANZI_DATUS_PROTOCOL = "nanzi-datus/v1"
_CALLBACK_ERROR = "NanZi project configuration is unavailable"


class NanziCallbackError(RuntimeError):
    """A callback failed without exposing response bodies or credentials."""


class NanziCallbackConfigurationError(NanziCallbackError):
    """The callback target is not a safe loopback HTTP endpoint."""


def normalize_callback_url(base_url: str) -> str:
    """Validate and canonicalize the loopback-only callback origin."""
    if not isinstance(base_url, str):
        raise NanziCallbackConfigurationError(_CALLBACK_ERROR)
    value = base_url.strip()
    if (
        not value
        or "\\" in value
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise NanziCallbackConfigurationError(_CALLBACK_ERROR)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise NanziCallbackConfigurationError(_CALLBACK_ERROR) from None

    host = parsed.hostname
    if (
        parsed.scheme != "http"
        or host not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is None
        or not 1 <= port <= 65535
    ):
        raise NanziCallbackConfigurationError(_CALLBACK_ERROR)

    normalized_host = f"[{host}]" if host == "::1" else host
    return f"http://{normalized_host}:{port}"


class NanziClient:
    """Fetch project configuration over the versioned loopback HTTP contract."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        protocol: str = NANZI_DATUS_PROTOCOL,
        timeout_seconds: float = 3.0,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = normalize_callback_url(base_url)
        self._service_token = service_token
        self._protocol = protocol
        self._timeout = httpx.Timeout(timeout_seconds)
        self._http_transport = http_transport

    @property
    def base_url(self) -> str:
        """Validated NanZi origin, useful to hosts colocating the MCP gateway."""
        return self._base_url

    async def fetch_project_config(
        self,
        *,
        project_id: str,
        user_id: str,
        agent_id: str,
        datasource_id: str,
        trace_id: str,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        path = f"/api/internal/datus/v1/projects/{project_id}/config"
        headers = {
            "Authorization": f"Bearer {self._service_token}",
            "X-Nanzi-Datus-Protocol": self._protocol,
            "X-Trace-Id": trace_id,
            "X-Nanzi-Project-Id": project_id,
            "X-Nanzi-User-Id": user_id,
            "X-Nanzi-Agent-Id": agent_id,
            "X-Nanzi-Datasource-Id": datasource_id,
        }
        if model_id is not None:
            headers["X-Nanzi-Model-Id"] = model_id
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._http_transport,
            ) as client:
                response = await client.get(f"{self._base_url}{path}", headers=headers)
        except httpx.HTTPError:
            raise NanziCallbackError(_CALLBACK_ERROR) from None

        if response.status_code != 200:
            raise NanziCallbackError(_CALLBACK_ERROR)
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise NanziCallbackError(_CALLBACK_ERROR) from None
        if not isinstance(payload, dict):
            raise NanziCallbackError(_CALLBACK_ERROR)
        return payload
