from __future__ import annotations

import hashlib
from types import MappingProxyType

import pytest

from datus.api.services.chat_task_manager import _clone_agent_config
from datus.models.base import LLMBaseModel
from datus.models.openai_model import OpenAIModel
from datus.tools.permission.permission_manager import PermissionManager
from datus.tools.permission.profile_override import apply_profile_override
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
    active_model = config.active_model()
    assert config.target == "nanzi"
    assert active_model.type == "openai"
    assert active_model.model == "gpt-4.1-mini"
    assert active_model.api_key == "test-model-api-key"
    assert active_model.base_url == "https://models.internal/v1"
    assert active_model.default_headers == {
        "x-openai-actor-authorization": "local-image-extension",
    }
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


def test_preserves_starrocks_datasource_type(tmp_path) -> None:
    body = project_config()
    body["datasource"]["type"] = "starrocks"

    config = NanziConfigBuilder(home=str(tmp_path / "runtime-home")).build_agent_config(body)

    assert config.current_db_config().type == "starrocks"


def test_native_model_factory_constructs_openai_model(tmp_path) -> None:
    config = NanziConfigBuilder(home=str(tmp_path / "runtime-home")).build_agent_config(project_config())

    model = LLMBaseModel.create_model(config)

    assert isinstance(model, OpenAIModel)


def test_runtime_clone_preserves_immutable_sidecars(tmp_path) -> None:
    config = NanziConfigBuilder(home=str(tmp_path / "runtime-home")).build_agent_config(project_config())

    cloned = _clone_agent_config(config)
    cloned.config_mutable = True

    assert cloned is not config
    assert cloned.nanzi_query_limits is config.nanzi_query_limits
    assert config.config_mutable is False


def test_accepts_legacy_model_payload_without_default_headers(tmp_path) -> None:
    body = project_config()
    body["model"].pop("default_headers")

    config = NanziConfigBuilder(home=str(tmp_path / "runtime-home")).build_agent_config(body)

    assert config.active_model().default_headers == {}


def test_injects_only_the_nanZi_owned_mcp_gateway(tmp_path) -> None:
    body = project_config()
    body["mcp"] = {
        "server_name": "nanzi_mcp",
        "url": f"http://127.0.0.1:8000/api/internal/datus/v1/projects/{body['project_id']}/mcp",
    }

    config = NanziConfigBuilder(home=str(tmp_path / "runtime-home")).build_agent_config(
        body,
        user_id="17",
        trace_id="trace-17",
        service_token="bridge-service-token",
    )

    server = config.services.mcp_servers["nanzi_mcp"]
    assert server["type"] == "http"
    assert server["url"].endswith("/mcp")
    assert server["headers"]["Authorization"] == "Bearer bridge-service-token"
    assert server["headers"]["X-Trace-Id"] == "trace-17"
    assert config.agentic_nodes["chat"]["mcp"] == "nanzi_mcp"
    manager = PermissionManager(
        global_config=config.permissions_config,
        active_profile=config.active_profile_name,
    )
    assert apply_profile_override(manager, config, "auto") is True
    assert manager.check_permission(
        "mcp.nanzi_mcp",
        "flint_chart__render_chart",
        "chat",
    ) == "allow"
    assert manager.check_permission("mcp.other", "render_chart", "chat") == "ask"


def test_loads_only_hash_verified_nanzi_skills(tmp_path) -> None:
    skills_root = tmp_path / "platform-skills"
    skill_dir = skills_root / "chart-report"
    skill_dir.mkdir(parents=True)
    content = b"---\nname: chart-report\n---\nUse Flint to render report charts.\n"
    (skill_dir / "SKILL.md").write_bytes(content)
    body = project_config()
    body["skills"] = [
        {
            "id": "chart-report",
            "content_sha256": hashlib.sha256(content).hexdigest(),
        }
    ]

    config = NanziConfigBuilder(
        home=str(tmp_path / "runtime-home"),
        skills_root=str(skills_root),
    ).build_agent_config(body)

    assert config.agentic_nodes["chat"]["skills"] == "*"
    assert str(skill_dir.resolve()) in config.skills_config.directories
    assert config.nanzi_skill_manifest == MappingProxyType(
        {"chart-report": hashlib.sha256(content).hexdigest()}
    )


def test_rejects_nanzi_skill_when_shared_file_hash_does_not_match(tmp_path) -> None:
    skills_root = tmp_path / "platform-skills"
    skill_dir = skills_root / "chart-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("changed", encoding="utf-8")
    body = project_config()
    body["skills"] = [
        {"id": "chart-report", "content_sha256": "a" * 64}
    ]

    with pytest.raises(NanziConfigError, match="incompatible"):
        NanziConfigBuilder(skills_root=str(skills_root)).build_agent_config(body)


def test_rejects_mcp_gateway_without_bridge_identity(tmp_path) -> None:
    body = project_config()
    body["mcp"] = {
        "server_name": "nanzi_mcp",
        "url": f"http://127.0.0.1:8000/api/internal/datus/v1/projects/{body['project_id']}/mcp",
    }

    with pytest.raises(NanziConfigError, match="incompatible"):
        NanziConfigBuilder(home=str(tmp_path / "runtime-home")).build_agent_config(body)


def test_rejects_unsupported_model_type_without_leaking_credentials() -> None:
    body = project_config()
    body["model"]["type"] = "anthropic"
    body["model"]["api_key"] = "model-secret-that-must-not-leak"

    with pytest.raises(NanziConfigError) as exc_info:
        NanziConfigBuilder().build_agent_config(body)

    assert "model-secret-that-must-not-leak" not in str(exc_info.value)
    assert "anthropic" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"config_mutable": True}, "incompatible"),
        ({"bash": {"enabled": True}}, "incompatible"),
        ({"query_limits": {"timeout_seconds": 61}}, "incompatible"),
        ({"config_fingerprint": "not-a-fingerprint"}, "incompatible"),
        ({"model": {"type": "openai", "model": "gpt-4.1-mini"}}, "incompatible"),
        ({"unexpected": "field"}, "incompatible"),
    ],
)
def test_rejects_protocol_or_policy_shape_drift(overrides, message) -> None:
    with pytest.raises(NanziConfigError, match=message):
        NanziConfigBuilder().build_agent_config(project_config(overrides=overrides))


def test_rejects_unsupported_datasource_or_incomplete_credentials_without_leaking_them() -> None:
    body = project_config(password="credential-that-must-not-leak")
    body["datasource"]["type"] = "postgresql"

    with pytest.raises(NanziConfigError) as exc_info:
        NanziConfigBuilder().build_agent_config(body)

    assert "credential-that-must-not-leak" not in str(exc_info.value)


def test_rejects_header_injection_without_leaking_value() -> None:
    body = project_config()
    body["model"]["default_headers"] = {
        "x-safe": "safe\r\nx-injected: must-not-leak",
    }

    with pytest.raises(NanziConfigError) as exc_info:
        NanziConfigBuilder().build_agent_config(body)

    assert "must-not-leak" not in str(exc_info.value)
