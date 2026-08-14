from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from datus.api.service import DatusAPIService
from nanzi_datus_bridge.health import build_nanzi_health


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _write_nanzi_config(tmp_path):
    config_path = tmp_path / "agent-nanzi.yml"
    config_path.write_text(
        """agent:
  api:
    auth_provider:
      class: nanzi_datus_bridge.auth_provider.NanziAuthProvider
      kwargs:
        callback_url: ${NANZI_CALLBACK_URL}
        service_token: ${NANZI_DATUS_INTERNAL_TOKEN}
        protocol: nanzi-datus/v1
""",
        encoding="utf-8",
    )
    return config_path


def test_nanzi_health_reports_liveness_and_local_configuration_readiness(tmp_path) -> None:
    config_path = _write_nanzi_config(tmp_path)

    with patch("httpx.AsyncClient", side_effect=AssertionError("network access attempted")):
        health = build_nanzi_health(
            environment={
                "NANZI_CALLBACK_URL": "http://127.0.0.1:8000",
                "NANZI_DATUS_INTERNAL_TOKEN": "configured-at-runtime",
            },
            config_path=config_path,
        )

    assert health == {
        "liveness": "alive",
        "nanzi": {
            "protocol": "nanzi-datus/v1",
            "ready": True,
            "checks": {
                "callback_url": "configured",
                "service_token": "configured",
                "config": "compatible",
            },
        },
    }


def test_nanzi_health_uses_stable_sanitized_diagnostics(tmp_path) -> None:
    secret = "do-not-expose-this-token"
    health = build_nanzi_health(
        environment={
            "NANZI_CALLBACK_URL": "https://user:password@remote.example/callback?token=secret",
            "NANZI_DATUS_INTERNAL_TOKEN": secret,
        },
        config_path=tmp_path / "missing-agent.yml",
    )

    serialized = json.dumps(health, sort_keys=True)
    assert health["liveness"] == "alive"
    assert health["nanzi"]["ready"] is False
    assert health["nanzi"]["checks"] == {
        "callback_url": "invalid",
        "service_token": "configured",
        "config": "missing",
    }
    assert secret not in serialized
    assert "password" not in serialized
    assert "remote.example" not in serialized


@pytest.mark.parametrize(
    "config_text",
    [
        "agent: [not-valid-yaml",
        """agent:
  notes: |
    class: nanzi_datus_bridge.auth_provider.NanziAuthProvider
    callback_url: ${NANZI_CALLBACK_URL}
    service_token: ${NANZI_DATUS_INTERNAL_TOKEN}
    protocol: nanzi-datus/v1
""",
        """agent:
  api:
    wrong_provider_path:
      class: nanzi_datus_bridge.auth_provider.NanziAuthProvider
      kwargs:
        callback_url: ${NANZI_CALLBACK_URL}
        service_token: ${NANZI_DATUS_INTERNAL_TOKEN}
        protocol: nanzi-datus/v1
""",
        """agent:
  api:
    auth_provider:
      class: nanzi_datus_bridge.auth_provider.NanziAuthProvider
      kwargs:
        callback_url: ${NANZI_CALLBACK_URL}
        service_token: ${NANZI_DATUS_INTERNAL_TOKEN}
        protocol: nanzi-datus/v2
""",
    ],
)
def test_nanzi_health_structurally_rejects_incompatible_yaml(tmp_path, config_text) -> None:
    config_path = tmp_path / "agent-nanzi.yml"
    config_path.write_text(config_text, encoding="utf-8")
    health = build_nanzi_health(
        environment={
            "NANZI_CALLBACK_URL": "http://127.0.0.1:8000",
            "NANZI_DATUS_INTERNAL_TOKEN": "strong-test-secret-3f6b9a",
        },
        config_path=config_path,
    )
    assert health["nanzi"]["ready"] is False
    assert health["nanzi"]["checks"]["config"] == "incompatible"


def test_nanzi_health_requires_yaml_environment_resolution(tmp_path) -> None:
    config_path = _write_nanzi_config(tmp_path)
    health = build_nanzi_health(environment={}, config_path=config_path)
    assert health["nanzi"]["ready"] is False
    assert health["nanzi"]["checks"] == {
        "callback_url": "missing",
        "service_token": "missing",
        "config": "incompatible",
    }


@pytest.mark.parametrize(
    "token",
    [
        "<shared runtime token>",
        "<anything-placeholder-like>",
        "replace-with-a-shared-secret",
        "change-me",
        "CHANGE_ME",
        "changeme123",
        "example-token",
        "example_secret",
    ],
)
def test_health_and_auth_provider_share_placeholder_token_rejection(tmp_path, token) -> None:
    from nanzi_datus_bridge.auth_provider import NanziConfigurationError, NanziAuthProvider

    config_path = _write_nanzi_config(tmp_path)
    health = build_nanzi_health(
        environment={
            "NANZI_CALLBACK_URL": "http://127.0.0.1:8000",
            "NANZI_DATUS_INTERNAL_TOKEN": token,
        },
        config_path=config_path,
    )
    assert health["nanzi"]["checks"]["service_token"] == "placeholder"
    with pytest.raises(NanziConfigurationError):
        NanziAuthProvider(callback_url="http://127.0.0.1:8000", service_token=token)


def test_health_and_auth_provider_permit_strong_test_secret(tmp_path) -> None:
    from nanzi_datus_bridge.auth_provider import NanziAuthProvider

    token = "unit-test-service-token-3f6b9a7c"
    config_path = _write_nanzi_config(tmp_path)
    health = build_nanzi_health(
        environment={
            "NANZI_CALLBACK_URL": "http://127.0.0.1:8000",
            "NANZI_DATUS_INTERNAL_TOKEN": token,
        },
        config_path=config_path,
    )
    assert health["nanzi"]["checks"]["service_token"] == "configured"
    assert isinstance(NanziAuthProvider(callback_url="http://127.0.0.1:8000", service_token=token), NanziAuthProvider)


def test_preflight_cli_has_clean_sanitized_output() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "NANZI_CALLBACK_URL": "http://127.0.0.1:8000",
            "NANZI_DATUS_INTERNAL_TOKEN": "subprocess-test-token",
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nanzi_datus_bridge.health",
            "--config",
            "conf/agent-nanzi.example.yml",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["nanzi"]["ready"] is True


@pytest.mark.asyncio
async def test_datus_health_skips_database_and_model_probes_in_nanzi_mode(tmp_path, monkeypatch) -> None:
    config_path = _write_nanzi_config(tmp_path)
    monkeypatch.setenv("NANZI_CALLBACK_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("NANZI_DATUS_INTERNAL_TOKEN", "configured-at-runtime")
    service = DatusAPIService(argparse.Namespace(config=str(config_path)))
    service.agent_config = SimpleNamespace(
        api_config={
            "auth_provider": {
                "class": "nanzi_datus_bridge.auth_provider.NanziAuthProvider",
            }
        }
    )

    with patch("datus.api.service.Agent", side_effect=AssertionError("external probe attempted")):
        response = await service.health_check()

    assert response.status == "healthy"
    assert response.liveness == "alive"
    assert response.database_status == {"nanzi": "not_checked"}
    assert response.llm_status == "not_checked"
    assert response.capabilities["nanzi"]["protocol"] == "nanzi-datus/v1"
    assert response.capabilities["nanzi"]["ready"] is True


@pytest.mark.asyncio
async def test_datus_nanzi_health_sanitizes_unexpected_exceptions(tmp_path, caplog) -> None:
    config_path = _write_nanzi_config(tmp_path)
    service = DatusAPIService(argparse.Namespace(config=str(config_path)))
    service.agent_config = SimpleNamespace(
        api_config={"auth_provider": {"class": "nanzi_datus_bridge.auth_provider.NanziAuthProvider"}}
    )
    secret = "secret-token https://private.example/C:/private/config.yml"
    with patch("nanzi_datus_bridge.health.build_nanzi_health", side_effect=RuntimeError(secret)):
        response = await service.health_check()
    serialized = json.dumps(response.model_dump(), sort_keys=True)
    assert response.status == "healthy"
    assert response.liveness == "alive"
    assert response.database_status == {"nanzi": "not_checked"}
    assert response.llm_status == "not_checked"
    assert response.capabilities == {
        "nanzi": {
            "protocol": "nanzi-datus/v1",
            "ready": False,
            "checks": {"health": "error"},
        }
    }
    assert secret not in serialized
    assert secret not in caplog.text
    assert "private.example" not in caplog.text
