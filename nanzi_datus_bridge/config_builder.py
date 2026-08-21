"""Strict in-memory ``AgentConfig`` construction for NanZi projects."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from datus.configuration.agent_config import AgentConfig
from datus.utils.exceptions import DatusException, ErrorCode

_TOP_LEVEL_FIELDS = {
    "project_id",
    "agent_id",
    "datasource",
    "model",
    "config_mutable",
    "bash",
    "query_limits",
    "config_fingerprint",
}
_OPTIONAL_TOP_LEVEL_FIELDS = {"mcp"}
_DATASOURCE_FIELDS = {"id", "type", "host", "port", "username", "password", "database"}
_MODEL_REQUIRED_FIELDS = {"type", "model", "api_key", "base_url"}
_MODEL_FIELDS = _MODEL_REQUIRED_FIELDS | {
    "default_headers",
    "enable_thinking",
    "reasoning_effort",
}
_REASONING_EFFORTS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
_MCP_FIELDS = {"server_name", "url"}
_SKILL_FIELDS = {"id", "content_sha256"}
_QUERY_LIMITS = {
    "allowed_statement_types": ("select", "cte", "explain"),
    "timeout_seconds": 60,
    "max_rows": 1000,
    "max_result_bytes": 2 * 1024 * 1024,
}
_SUPPORTED_DATASOURCE_TYPES = frozenset({"mysql", "starrocks"})
_PROJECT_ID_PATTERN = re.compile(r"^nzp_[0-9a-f]{32}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_SKILLS = 128
_MAX_SKILL_MD_BYTES = 256 * 1024
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_BLOCKED_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "host",
        "proxy-authorization",
        "transfer-encoding",
    }
)


@dataclass(frozen=True, slots=True)
class NanziModelDTO:
    """Exact model object accepted in NanZi's v1 callback response."""

    type: Literal["openai"]
    model: str
    api_key: str = field(repr=False)
    base_url: str
    default_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    enable_thinking: bool = False
    reasoning_effort: str = "off"

    def __post_init__(self) -> None:
        if self.type != "openai" or not all((self.model.strip(), self.api_key.strip(), self.base_url.strip())):
            raise NanziConfigError()
        object.__setattr__(
            self,
            "default_headers",
            MappingProxyType(_normalize_default_headers(self.default_headers)),
        )
        if not isinstance(self.enable_thinking, bool):
            raise NanziConfigError()
        if self.reasoning_effort not in _REASONING_EFFORTS:
            raise NanziConfigError()
        if self.reasoning_effort == "off" and self.enable_thinking:
            raise NanziConfigError()
        if self.reasoning_effort != "off" and not self.enable_thinking:
            raise NanziConfigError()


@dataclass(frozen=True, slots=True)
class NanziMcpDTO:
    """NanZi MCP gateway metadata; upstream credentials never cross this boundary."""

    server_name: Literal["nanzi_mcp"]
    url: str

    def __post_init__(self) -> None:
        normalized = self.url.strip().rstrip("/")
        parsed = urlsplit(normalized)
        try:
            _ = parsed.port
        except ValueError:
            raise NanziConfigError() from None
        if (
            self.server_name != "nanzi_mcp"
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise NanziConfigError()
        object.__setattr__(self, "url", normalized)


@dataclass(frozen=True, slots=True)
class NanziSkillDTO:
    """One platform Skill authorized by NanZi for the current project."""

    id: str
    content_sha256: str

    def __post_init__(self) -> None:
        if (
            not _SKILL_ID_PATTERN.fullmatch(self.id)
            or not _FINGERPRINT_PATTERN.fullmatch(self.content_sha256)
        ):
            raise NanziConfigError()


class NanziConfigError(DatusException):
    """Sanitized, fail-closed NanZi project-configuration error."""

    def __init__(self, message: str = "NanZi project configuration is incompatible") -> None:
        super().__init__(ErrorCode.COMMON_CONFIG_ERROR, message=message)


def _normalize_default_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 16:
        raise NanziConfigError()
    normalized: dict[str, str] = {}
    total_length = 0
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise NanziConfigError()
        name = raw_name.strip().lower()
        header_value = raw_value.strip()
        if (
            not name
            or not _HEADER_NAME_PATTERN.fullmatch(name)
            or name in _BLOCKED_HEADERS
            or name in normalized
            or not header_value
            or len(header_value) > 1024
            or "\r" in header_value
            or "\n" in header_value
            or "\x00" in header_value
        ):
            raise NanziConfigError()
        total_length += len(name) + len(header_value)
        if total_length > 8192:
            raise NanziConfigError()
        normalized[name] = header_value
    return normalized


class NanziConfigBuilder:
    """Validate NanZi's v1 payload and construct Datus configuration in memory."""

    def __init__(
        self,
        *,
        home: str = "~/.datus-nanzi",
        skills_root: str = "~/.agents/skills",
    ) -> None:
        self._home = home
        self._skills_root = Path(skills_root).expanduser()

    def build_agent_config(
        self,
        raw: Mapping[str, Any],
        *,
        user_id: str | None = None,
        trace_id: str | None = None,
        service_token: str | None = None,
    ) -> AgentConfig:
        normalized = self._validate(raw)
        datasource = normalized["datasource"]
        model: NanziModelDTO = normalized["model"]
        datasource_name = f"nanzi_{datasource['id']}"
        agent_raw = {
            "home": self._home,
            "project_name": normalized["project_id"],
            "skip_init_dirs": True,
            "config_mutable": False,
            "bash": {"enabled": False},
            "target": "nanzi",
            "models": {
                "nanzi": {
                    "type": model.type,
                    "model": model.model,
                    "api_key": model.api_key,
                    "base_url": model.base_url,
                    "default_headers": dict(model.default_headers),
                    "enable_thinking": model.enable_thinking,
                    "reasoning_effort": model.reasoning_effort,
                }
            },
            "sql_policy": {
                "enabled": True,
                "provider": "nanzi_datus_bridge.query_policy:NanziReadOnlySqlPolicy",
                "allowed_statement_types": list(_QUERY_LIMITS["allowed_statement_types"]),
                "timeout_seconds": _QUERY_LIMITS["timeout_seconds"],
                "max_rows": _QUERY_LIMITS["max_rows"],
                "max_result_bytes": _QUERY_LIMITS["max_result_bytes"],
            },
            "services": {
                "datasources": {
                    datasource_name: {
                        "type": datasource["type"],
                        "host": datasource["host"],
                        "port": datasource["port"],
                        "username": datasource["username"],
                        "password": datasource["password"],
                        "database": datasource["database"],
                        "timeout_seconds": _QUERY_LIMITS["timeout_seconds"],
                        "default": True,
                    }
                }
            },
        }
        skill_directories = self._resolve_skill_directories(normalized["skills"])
        if skill_directories:
            agent_raw["skills"] = {
                "directories": skill_directories,
                "warn_duplicates": True,
                "whitelist_from_compaction": True,
            }
            agent_raw["agentic_nodes"] = {"chat": {"skills": "*"}}
        mcp: NanziMcpDTO | None = normalized.get("mcp")
        if mcp is not None:
            if not all(isinstance(value, str) and value.strip() for value in (user_id, trace_id, service_token)):
                raise NanziConfigError()
            agent_raw["services"]["mcp_servers"] = {
                mcp.server_name: {
                    "type": "http",
                    "url": mcp.url,
                    "timeout": 30,
                    "headers": {
                        "Authorization": f"Bearer {service_token}",
                        "X-Nanzi-Datus-Protocol": "nanzi-datus/v1",
                        "X-Nanzi-Project-Id": normalized["project_id"],
                        "X-Nanzi-User-Id": user_id,
                        "X-Nanzi-Agent-Id": normalized["agent_id"],
                        "X-Nanzi-Datasource-Id": str(datasource["id"]),
                        "X-Trace-Id": trace_id,
                    },
                }
            }
            agent_raw.setdefault("agentic_nodes", {}).setdefault("chat", {})[
                "mcp"
            ] = mcp.server_name
            # NanZi authenticates every gateway call and filters the tool list
            # to the caller's published Agent version. Datus may therefore run
            # this one managed namespace without an interactive second prompt.
            agent_raw["permissions"] = {
                "profile": "normal",
                "rules": [
                    {
                        "tool": f"mcp.{mcp.server_name}",
                        "pattern": "*",
                        "permission": "allow",
                    }
                ],
            }
        config = AgentConfig(nodes={}, **agent_raw)
        config.current_datasource = datasource_name
        # Sidecar policy metadata is immutable and remains available to callers
        # that need to report the effective NanZi contract.
        config.nanzi_query_limits = MappingProxyType(dict(_QUERY_LIMITS))
        config.nanzi_config_fingerprint = normalized["config_fingerprint"]
        config.nanzi_project_id = normalized["project_id"]
        config.nanzi_agent_id = normalized["agent_id"]
        config.nanzi_datasource_id = str(datasource["id"])
        config.nanzi_skill_manifest = MappingProxyType(
            {skill.id: skill.content_sha256 for skill in normalized["skills"]}
        )
        return config

    @staticmethod
    def _validate(raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise NanziConfigError()
        fields = set(raw)
        allowed_fields = _TOP_LEVEL_FIELDS | _OPTIONAL_TOP_LEVEL_FIELDS | {"skills"}
        if not _TOP_LEVEL_FIELDS.issubset(fields) or not fields.issubset(allowed_fields):
            raise NanziConfigError()
        normalized = dict(raw)
        if not isinstance(normalized["project_id"], str) or not _PROJECT_ID_PATTERN.fullmatch(
            normalized["project_id"]
        ):
            raise NanziConfigError()
        if not isinstance(normalized["agent_id"], str) or not normalized["agent_id"].strip():
            raise NanziConfigError()
        if normalized["config_mutable"] is not False or normalized["bash"] != {"enabled": False}:
            raise NanziConfigError()
        if not NanziConfigBuilder._query_limits_match(normalized["query_limits"]):
            raise NanziConfigError()
        fingerprint = normalized["config_fingerprint"]
        if not isinstance(fingerprint, str) or not _FINGERPRINT_PATTERN.fullmatch(fingerprint):
            raise NanziConfigError()

        datasource = normalized["datasource"]
        if not isinstance(datasource, Mapping) or set(datasource) != _DATASOURCE_FIELDS:
            raise NanziConfigError()
        datasource = dict(datasource)
        valid_strings = all(
            isinstance(datasource[field], str) and bool(datasource[field].strip())
            for field in ("host", "username", "password", "database")
        )
        valid_id = type(datasource["id"]) is int and datasource["id"] > 0
        valid_port = type(datasource["port"]) is int and 1 <= datasource["port"] <= 65535
        if (
            datasource["type"] not in _SUPPORTED_DATASOURCE_TYPES
            or not valid_strings
            or not valid_id
            or not valid_port
        ):
            raise NanziConfigError()
        normalized["datasource"] = datasource

        model = normalized["model"]
        if (
            not isinstance(model, Mapping)
            or not _MODEL_REQUIRED_FIELDS.issubset(model)
            or not set(model).issubset(_MODEL_FIELDS)
        ):
            raise NanziConfigError()
        model = dict(model)
        if model["type"] != "openai" or not all(
            isinstance(model[field], str) and bool(model[field].strip())
            for field in _MODEL_REQUIRED_FIELDS
        ):
            raise NanziConfigError()
        normalized["model"] = NanziModelDTO(
            type=model["type"],
            model=model["model"],
            api_key=model["api_key"],
            base_url=model["base_url"],
            default_headers=model.get("default_headers", {}),
            enable_thinking=model.get("enable_thinking", False),
            reasoning_effort=model.get("reasoning_effort", "off"),
        )
        mcp = normalized.get("mcp")
        if mcp is None:
            normalized["mcp"] = None
        else:
            if not isinstance(mcp, Mapping) or set(mcp) != _MCP_FIELDS:
                raise NanziConfigError()
            server_name = mcp.get("server_name")
            url = mcp.get("url")
            if not isinstance(server_name, str) or not isinstance(url, str) or not server_name.strip() or not url.strip():
                raise NanziConfigError()
            try:
                normalized["mcp"] = NanziMcpDTO(server_name=server_name, url=url)
            except (TypeError, ValueError, NanziConfigError):
                raise NanziConfigError() from None
        skills = normalized.get("skills", [])
        if not isinstance(skills, list) or len(skills) > _MAX_SKILLS:
            raise NanziConfigError()
        parsed_skills: list[NanziSkillDTO] = []
        seen_skill_ids: set[str] = set()
        for item in skills:
            if not isinstance(item, Mapping) or set(item) != _SKILL_FIELDS:
                raise NanziConfigError()
            skill_id = item.get("id")
            content_sha256 = item.get("content_sha256")
            if (
                not isinstance(skill_id, str)
                or not isinstance(content_sha256, str)
                or skill_id in seen_skill_ids
            ):
                raise NanziConfigError()
            try:
                parsed_skills.append(
                    NanziSkillDTO(
                        id=skill_id,
                        content_sha256=content_sha256,
                    )
                )
            except (TypeError, ValueError, NanziConfigError):
                raise NanziConfigError() from None
            seen_skill_ids.add(skill_id)
        normalized["skills"] = parsed_skills
        return normalized

    def _resolve_skill_directories(
        self,
        skills: list[NanziSkillDTO],
    ) -> list[str]:
        if not skills:
            return []
        try:
            root = self._skills_root.resolve(strict=True)
            if not root.is_dir():
                raise NanziConfigError()
        except (OSError, RuntimeError):
            raise NanziConfigError() from None

        directories: list[str] = []
        for skill in sorted(skills, key=lambda item: item.id):
            try:
                skill_file = (root / skill.id / "SKILL.md").resolve(strict=True)
                skill_file.relative_to(root)
                if not skill_file.is_file() or skill_file.stat().st_size > _MAX_SKILL_MD_BYTES:
                    raise NanziConfigError()
                content = skill_file.read_bytes()
            except (OSError, RuntimeError, ValueError):
                raise NanziConfigError() from None
            actual_digest = hashlib.sha256(content).hexdigest()
            if not hmac.compare_digest(actual_digest, skill.content_sha256):
                raise NanziConfigError()
            directories.append(str(skill_file.parent))
        return directories

    @staticmethod
    def _query_limits_match(value: Any) -> bool:
        if not isinstance(value, Mapping) or set(value) != set(_QUERY_LIMITS):
            return False
        return (
            value["allowed_statement_types"] == list(_QUERY_LIMITS["allowed_statement_types"])
            and type(value["timeout_seconds"]) is int
            and value["timeout_seconds"] == _QUERY_LIMITS["timeout_seconds"]
            and type(value["max_rows"]) is int
            and value["max_rows"] == _QUERY_LIMITS["max_rows"]
            and type(value["max_result_bytes"]) is int
            and value["max_result_bytes"] == _QUERY_LIMITS["max_result_bytes"]
        )
