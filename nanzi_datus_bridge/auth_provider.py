"""NanZi service authentication and dynamic Datus project configuration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Never

import httpx
from fastapi import Request

from datus.api.auth.context import AppContext
from datus.api.auth.provider import EvictCallback
from datus.utils.exceptions import DatusException, ErrorCode
from nanzi_datus_bridge.config_builder import NanziConfigBuilder, NanziConfigError
from nanzi_datus_bridge.nanzi_client import (
    NANZI_DATUS_PROTOCOL,
    NanziCallbackConfigurationError,
    NanziCallbackError,
    NanziClient,
)
from nanzi_datus_bridge.runtime_settings import resolve_setting, service_token_status

_MAX_CACHE_TTL_SECONDS = 30.0
_MAX_CACHE_ENTRIES = 1024
_DEFAULT_CACHE_ENTRIES = 128
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PROJECT_ID = re.compile(r"^nzp_[0-9a-f]{32}$")


class NanziBridgeError(DatusException):
    """Base sanitized bridge error surfaced through Datus's native API seam."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.COMMON_VALIDATION_FAILED, message=message)


class NanziAuthenticationError(NanziBridgeError):
    """The inbound NanZi service identity could not be authenticated."""


class NanziConfigurationError(NanziBridgeError):
    """The bridge or returned project configuration is incompatible."""


@dataclass(frozen=True)
class _Identity:
    project_id: str
    user_id: str
    agent_id: str
    datasource_id: str
    trace_id: str


@dataclass(frozen=True)
class _CacheEntry:
    config: object
    fingerprint: str
    payload_digest: str
    agent_id: str
    datasource_id: str
    expires_at: float


class NanziAuthProvider:
    """Datus ``AuthProvider`` backed by NanZi's internal config callback."""

    def __init__(
        self,
        *,
        callback_url: str | None = None,
        service_token: str | None = None,
        protocol: str = NANZI_DATUS_PROTOCOL,
        cache_ttl_seconds: float = _MAX_CACHE_TTL_SECONDS,
        max_cache_entries: int = _DEFAULT_CACHE_ENTRIES,
        home: str = "~/.datus-nanzi",
        skills_root: str | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved_url = self._resolve_setting(callback_url, "NANZI_CALLBACK_URL")
        resolved_token = self._resolve_setting(service_token, "NANZI_DATUS_INTERNAL_TOKEN")
        if not resolved_url:
            self._configuration_error("NanZi callback configuration is unavailable")
        if service_token_status(resolved_token) != "configured":
            self._configuration_error("NanZi service authentication is unavailable")
        if protocol != NANZI_DATUS_PROTOCOL:
            self._configuration_error("NanZi-Datus protocol is incompatible")
        if not isinstance(cache_ttl_seconds, (int, float)) or not 0 < cache_ttl_seconds <= _MAX_CACHE_TTL_SECONDS:
            self._configuration_error("NanZi project cache configuration is incompatible")
        if type(max_cache_entries) is not int or not 1 <= max_cache_entries <= _MAX_CACHE_ENTRIES:
            self._configuration_error("NanZi project cache configuration is incompatible")

        self._service_token = resolved_token
        self._protocol = protocol
        self._cache_ttl_seconds = float(cache_ttl_seconds)
        self._max_cache_entries = max_cache_entries
        self._clock = clock
        try:
            self._client = NanziClient(
                base_url=resolved_url,
                service_token=resolved_token,
                protocol=protocol,
                http_transport=http_transport,
            )
        except NanziCallbackConfigurationError:
            self._configuration_error("NanZi callback configuration is unavailable")
        if skills_root is None:
            resolved_skills_root = self._resolve_setting(None, "NANZI_SKILLS_ROOT")
            if not resolved_skills_root:
                resolved_skills_root = "~/.agents/skills"
        else:
            resolved_skills_root = self._resolve_setting(
                skills_root,
                "NANZI_SKILLS_ROOT",
            )
            if not resolved_skills_root:
                self._configuration_error(
                    "NanZi Skill directory configuration is unavailable"
                )
        self._builder = NanziConfigBuilder(
            home=home,
            skills_root=resolved_skills_root,
        )
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._cache_lock = asyncio.Lock()
        self._evict_callbacks: list[EvictCallback] = []

    async def authenticate(self, request: Request) -> AppContext:
        identity = self._authenticate_headers(request)
        cache_key = f"{identity.project_id}:{identity.user_id}"
        async with self._cache_lock:
            previous = self._cache.get(cache_key)
            if previous is not None:
                if previous.agent_id != identity.agent_id or previous.datasource_id != identity.datasource_id:
                    self._configuration_error("NanZi project identity is incompatible")
                if previous.expires_at > self._clock():
                    self._cache.move_to_end(cache_key)
                    return self._app_context(identity, previous.config)

            try:
                payload = await self._client.fetch_project_config(
                    project_id=identity.project_id,
                    user_id=identity.user_id,
                    agent_id=identity.agent_id,
                    datasource_id=identity.datasource_id,
                    trace_id=identity.trace_id,
                )
                config = self._builder.build_agent_config(
                    payload,
                    user_id=identity.user_id,
                    trace_id=identity.trace_id,
                    service_token=self._service_token,
                )
            except (NanziCallbackError, NanziConfigError):
                self._configuration_error("NanZi project configuration is incompatible")

            if (
                config.nanzi_project_id != identity.project_id
                or config.nanzi_agent_id != identity.agent_id
                or config.nanzi_datasource_id != identity.datasource_id
            ):
                self._configuration_error("NanZi project configuration is incompatible")

            fingerprint = config.nanzi_config_fingerprint
            payload_digest = self._payload_digest(payload)
            if previous is not None and previous.fingerprint == fingerprint:
                if previous.payload_digest != payload_digest:
                    self._configuration_error("NanZi project configuration is incompatible")
                entry = _CacheEntry(
                    config=previous.config,
                    fingerprint=previous.fingerprint,
                    payload_digest=previous.payload_digest,
                    agent_id=previous.agent_id,
                    datasource_id=previous.datasource_id,
                    expires_at=self._clock() + self._cache_ttl_seconds,
                )
            else:
                if previous is not None:
                    await self._evict_changed_project(identity.project_id)
                entry = _CacheEntry(
                    config=config,
                    fingerprint=fingerprint,
                    payload_digest=payload_digest,
                    agent_id=identity.agent_id,
                    datasource_id=identity.datasource_id,
                    expires_at=self._clock() + self._cache_ttl_seconds,
                )

            self._cache[cache_key] = entry
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self._max_cache_entries:
                self._cache.popitem(last=False)
            return self._app_context(identity, entry.config)

    def on_evict(self, callback: EvictCallback) -> None:
        if not callable(callback):
            self._configuration_error("NanZi cache eviction configuration is incompatible")
        self._evict_callbacks.append(callback)

    def _authenticate_headers(self, request: Request) -> _Identity:
        scheme, separator, supplied_token = (request.headers.get("Authorization") or "").partition(" ")
        token_valid = (
            scheme == "Bearer"
            and separator == " "
            and bool(supplied_token)
            and hmac.compare_digest(supplied_token.encode("utf-8"), self._service_token.encode("utf-8"))
        )
        if not token_valid:
            self._authentication_error()
        if request.headers.get("X-Nanzi-Datus-Protocol") != self._protocol:
            self._authentication_error("NanZi-Datus protocol is incompatible")

        project_id = self._required_header(request, "X-Nanzi-Project-Id", _PROJECT_ID)
        user_id = self._required_header(request, "X-Nanzi-User-Id", _SAFE_ID)
        agent_id = self._required_header(request, "X-Nanzi-Agent-Id", _SAFE_ID)
        datasource_id = self._required_header(request, "X-Nanzi-Datasource-Id", re.compile(r"^[1-9][0-9]{0,18}$"))
        trace_id = request.headers.get("X-Trace-Id") or request.headers.get("X-Nanzi-Trace-Id")
        if not trace_id or not _SAFE_ID.fullmatch(trace_id):
            self._authentication_error()
        expected_project_id = self._build_project_id(agent_id, datasource_id)
        if not hmac.compare_digest(project_id.encode("ascii"), expected_project_id.encode("ascii")):
            self._authentication_error("NanZi project identity is invalid")
        return _Identity(project_id, user_id, agent_id, datasource_id, trace_id)

    @staticmethod
    def _required_header(request: Request, name: str, pattern: re.Pattern[str]) -> str:
        value = request.headers.get(name)
        if value is None or not pattern.fullmatch(value):
            NanziAuthProvider._authentication_error()
        return value

    @staticmethod
    def _build_project_id(agent_id: str, datasource_id: str) -> str:
        value = f"default:{agent_id}:{datasource_id}".encode("utf-8")
        return f"nzp_{hashlib.sha256(value).hexdigest()[:32]}"

    @staticmethod
    def _payload_digest(payload: dict[str, object]) -> str:
        canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _evict_changed_project(self, project_id: str) -> None:
        try:
            for callback in tuple(self._evict_callbacks):
                await callback(project_id)
        except Exception:
            self._configuration_error("NanZi project cache eviction failed")

    @staticmethod
    def _app_context(identity: _Identity, config: object) -> AppContext:
        return AppContext(
            user_id=identity.user_id,
            project_id=identity.project_id,
            config=config,
            principal={
                "tenant_id": "default",
                "agent_id": identity.agent_id,
                "datasource_id": identity.datasource_id,
            },
        )

    @staticmethod
    def _resolve_setting(value: str | None, environment_name: str) -> str:
        return resolve_setting(value, environment_name)

    @staticmethod
    def _authentication_error(message: str = "NanZi service authentication failed") -> Never:
        raise NanziAuthenticationError(message)

    @staticmethod
    def _configuration_error(message: str) -> Never:
        raise NanziConfigurationError(message)
