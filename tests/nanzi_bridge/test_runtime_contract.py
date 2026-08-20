"""Contract tests for the no-Docker NanZi integration runtime skeleton."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from datus.api.auth.loader import load_auth_provider
from datus.configuration.agent_config_loader import load_agent_config

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _native_commands(script: str) -> list[str]:
    lines = script.splitlines()
    command_indexes = [index for index, line in enumerate(lines) if re.match(r"^\s*&\s+\S", line)]
    assert command_indexes
    for index in command_indexes:
        following_statements = [
            line.strip() for line in lines[index + 1 :] if line.strip() and not line.lstrip().startswith("#")
        ]
        assert following_statements
        assert re.fullmatch(
            r"if \(\$LASTEXITCODE -ne 0\) \{ exit \$LASTEXITCODE \}",
            following_statements[0],
        )
    return [lines[index].strip() for index in command_indexes]


def test_runtime_skeleton_contract() -> None:
    """The example stays local-only, immutable, and wired to the bridge provider."""
    config = _read("conf/agent-nanzi.example.yml")
    environment = _read(".env.nanzi.example")
    setup_script = _read("scripts/setup-nanzi-integration.ps1")
    start_script = _read("scripts/start-nanzi-integration.ps1")
    check_script = _read("scripts/check-nanzi-integration.ps1")
    runbook = _read("docs/integration/nanzi.md")

    assert re.search(r"^agent:\s*$", config, re.MULTILINE)
    assert re.search(r"^  config_mutable: false\s*$", config, re.MULTILINE)
    assert re.search(r"^  bash:\s*\n    enabled: false\s*$", config, re.MULTILINE)
    assert "class: nanzi_datus_bridge.auth_provider.NanziAuthProvider" in config
    assignments = [line for line in environment.splitlines() if line and not line.startswith("#")]
    assert assignments == [
        "NANZI_CALLBACK_URL=http://127.0.0.1:8000",
        "NANZI_DATUS_INTERNAL_TOKEN=",
        "NANZI_SKILLS_ROOT=~/.agents/skills",
    ]
    assert "${NANZI_CALLBACK_URL}" in config
    assert "${NANZI_DATUS_INTERNAL_TOKEN}" in config
    assert "${NANZI_SKILLS_ROOT}" in config

    assert "uv venv --python 3.12 .venv" in setup_script
    assert "datus-mysql" in setup_script
    assert "datus-starrocks" in setup_script
    assert "datus-metricflow" in setup_script
    assert "datus-semantic-osi[metricflow]" in setup_script
    assert "--host 127.0.0.1 --port 8001 --workers 1" in start_script
    assert "docker" not in setup_script.lower()
    assert "docker" not in start_script.lower()
    assert "Start-Process" not in setup_script + start_script
    setup_commands = _native_commands(setup_script)
    start_commands = _native_commands(start_script)
    assert len(setup_commands) == 2
    assert setup_commands[0].startswith("& $uv venv --python 3.12 .venv")
    assert setup_commands[1].startswith("& $uv pip install --python $python")
    assert start_commands == [
        "& $python -m datus.api.main --host 127.0.0.1 --port 8001 --workers 1 --config conf/agent-nanzi.example.yml"
    ]
    assert "-m nanzi_datus_bridge.health --config conf/agent-nanzi.example.yml" in check_script
    assert "datus.api.main" not in check_script
    assert "uvicorn" not in check_script.lower()
    assert "Python 3.12" in runbook
    assert "127.0.0.1:8001" in runbook
    assert "--workers 1" in runbook
    assert "scripts/check-nanzi-integration.ps1" in runbook
    assert "docker" not in runbook.lower()


def test_yaml_dynamic_loader_resolves_environment_placeholders(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NANZI_CALLBACK_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("NANZI_DATUS_INTERNAL_TOKEN", "loader-test-token")
    config_path = tmp_path / "agent.yml"
    config_path.write_text(
        """agent:
  home: ~/.datus-nanzi
  skip_init_dirs: true
  config_mutable: false
  bash:
    enabled: false
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

    agent_config = load_agent_config(config=str(config_path), reload=True)
    provider = load_auth_provider(agent_config.api_config, datasource="default")

    provider_module = importlib.import_module("nanzi_datus_bridge.auth_provider")
    assert isinstance(provider, provider_module.NanziAuthProvider)
