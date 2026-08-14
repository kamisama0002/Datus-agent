"""Sanitized HTTP client for NanZi's internal Datus project-config API."""

from __future__ import annotations

from typing import Any

import httpx


NANZI_DATUS_PROTOCOL = "nanzi-datus/v1"
_CALLBACK_ERROR = "NanZi project configuration is unavailable"


class NanziCallbackError(RuntimeError):
    """A callback failed without exposing response bodies or credentials."""


class NanziClient:
    """Fetch project configuration over the versioned loopback HTTP contract."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        protocol: str = NANZI_DATUS_PROTOCOL,
        timeout_seconds: float = 3.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._protocol = protocol
        self._timeout = httpx.Timeout(timeout_seconds)
        self._http_client = http_client

    async def fetch_project_config(
        self,
        *,
        project_id: str,
        user_id: str,
        agent_id: str,
        datasource_id: str,
        trace_id: str,
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
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    f"{self._base_url}{path}", headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
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
