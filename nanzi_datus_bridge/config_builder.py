"""Strict in-memory ``AgentConfig`` construction for NanZi projects."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

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
_DATASOURCE_FIELDS = {"id", "type", "host", "port", "username", "password", "database"}
_MODEL_FIELDS = {"type", "model", "api_key", "base_url"}
_QUERY_LIMITS = {
    "allowed_statement_types": ("select", "cte", "explain"),
    "timeout_seconds": 60,
    "max_rows": 1000,
    "max_result_bytes": 2 * 1024 * 1024,
}
_PROJECT_ID_PATTERN = re.compile(r"^nzp_[0-9a-f]{32}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class NanziModelDTO:
    """Exact model object accepted in NanZi's v1 callback response."""

    type: Literal["openai"]
    model: str
    api_key: str = field(repr=False)
    base_url: str

    def __post_init__(self) -> None:
        if self.type != "openai" or not all((self.model.strip(), self.api_key.strip(), self.base_url.strip())):
            raise NanziConfigError()


class NanziConfigError(DatusException):
    """Sanitized, fail-closed NanZi project-configuration error."""

    def __init__(self, message: str = "NanZi project configuration is incompatible") -> None:
        super().__init__(ErrorCode.COMMON_CONFIG_ERROR, message=message)


class NanziConfigBuilder:
    """Validate NanZi's v1 payload and construct Datus configuration in memory."""

    def __init__(self, *, home: str = "~/.datus-nanzi") -> None:
        self._home = home

    def build_agent_config(self, raw: Mapping[str, Any]) -> AgentConfig:
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
                        "type": "mysql",
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
        config = AgentConfig(nodes={}, **agent_raw)
        config.current_datasource = datasource_name
        # Sidecar policy metadata is immutable and remains available to callers
        # that need to report the effective NanZi contract.
        config.nanzi_query_limits = MappingProxyType(dict(_QUERY_LIMITS))
        config.nanzi_config_fingerprint = normalized["config_fingerprint"]
        config.nanzi_project_id = normalized["project_id"]
        config.nanzi_agent_id = normalized["agent_id"]
        config.nanzi_datasource_id = str(datasource["id"])
        return config

    @staticmethod
    def _validate(raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or set(raw) != _TOP_LEVEL_FIELDS:
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
        if datasource["type"] != "mysql" or not valid_strings or not valid_id or not valid_port:
            raise NanziConfigError()
        normalized["datasource"] = datasource

        model = normalized["model"]
        if not isinstance(model, Mapping) or set(model) != _MODEL_FIELDS:
            raise NanziConfigError()
        model = dict(model)
        if model["type"] != "openai" or not all(
            isinstance(model[field], str) and bool(model[field].strip()) for field in _MODEL_FIELDS
        ):
            raise NanziConfigError()
        normalized["model"] = NanziModelDTO(
            type=model["type"],
            model=model["model"],
            api_key=model["api_key"],
            base_url=model["base_url"],
        )
        return normalized

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
