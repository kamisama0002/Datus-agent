from __future__ import annotations

from types import MappingProxyType

import pytest

from nanzi_datus_bridge.config_builder import NanziConfigBuilder, NanziConfigError
from tests.nanzi_bridge.conftest import project_config


def test_builds_native_agent_config_in_memory_with_hardened_policy(tmp_path) -> None:
    home = tmp_path / "runtime-home"
    config = NanziConfigBuilder(home=str(home)).build_agent_config(project_config())

    datasource = config.current_db_config()
    assert config.current_datasource == "nanzi_17"
    assert datasource.type == "mysql"
    assert datasource.host == "mysql.internal"
    assert datasource.port == "3306"
    assert datasource.username == "readonly_user"
    assert datasource.password == "database-password"
    assert datasource.database == "sales"
    assert datasource.extra["timeout_seconds"] == 60
    assert config.config_mutable is False
    assert config.bash_tool_enabled is False
    assert config.nanzi_query_limits == MappingProxyType(
        {
            "allowed_statement_types": ("select", "cte", "explain"),
            "timeout_seconds": 60,
            "max_rows": 1000,
            "max_result_bytes": 2 * 1024 * 1024,
        }
    )
    assert config.nanzi_config_fingerprint == "a" * 64
    assert not home.exists()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"config_mutable": True}, "incompatible"),
        ({"bash": {"enabled": True}}, "incompatible"),
        ({"query_limits": {"timeout_seconds": 61}}, "incompatible"),
        ({"config_fingerprint": "not-a-fingerprint"}, "incompatible"),
        ({"unexpected": "field"}, "incompatible"),
    ],
)
def test_rejects_protocol_or_policy_shape_drift(overrides, message) -> None:
    with pytest.raises(NanziConfigError, match=message):
        NanziConfigBuilder().build_agent_config(project_config(overrides=overrides))


def test_rejects_non_mysql_or_incomplete_credentials_without_leaking_them() -> None:
    body = project_config(password="credential-that-must-not-leak")
    body["datasource"]["type"] = "postgresql"

    with pytest.raises(NanziConfigError) as exc_info:
        NanziConfigBuilder().build_agent_config(body)

    assert "credential-that-must-not-leak" not in str(exc_info.value)
