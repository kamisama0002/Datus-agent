"""Contract tests for the no-Docker NanZi integration runtime skeleton."""

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _assert_native_commands_fail_fast(script: str) -> None:
    lines = script.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("& "):
            assert index + 1 < len(lines)
            assert "$LASTEXITCODE -ne 0" in lines[index + 1]


def test_runtime_skeleton_contract() -> None:
    """The example stays local-only, immutable, and wired to the bridge provider."""
    config = _read("conf/agent-nanzi.example.yml")
    environment = _read(".env.nanzi.example")
    setup_script = _read("scripts/setup-nanzi-integration.ps1")
    start_script = _read("scripts/start-nanzi-integration.ps1")

    assert re.search(r"^agent:\s*$", config, re.MULTILINE)
    assert re.search(r"^  config_mutable: false\s*$", config, re.MULTILINE)
    assert re.search(r"^  bash:\s*\n    enabled: false\s*$", config, re.MULTILINE)
    assert "class: nanzi_datus_bridge.auth_provider.NanziAuthProvider" in config
    assert "NANZI_" not in environment or "=" in environment

    assert "uv venv --python 3.12 .venv" in setup_script
    assert "datus-mysql" in setup_script
    assert "datus-metricflow" in setup_script
    assert "datus-semantic-osi[metricflow]" in setup_script
    assert "--host 127.0.0.1 --port 8001 --workers 1" in start_script
    assert "docker" not in setup_script.lower()
    assert "docker" not in start_script.lower()
    _assert_native_commands_fail_fast(setup_script)
    _assert_native_commands_fail_fast(start_script)


def test_bridge_provider_is_importable_and_fails_closed() -> None:
    """Task 2 must not accidentally accept traffic before Task 7 authentication."""
    provider_module = importlib.import_module("nanzi_datus_bridge.auth_provider")
    config_module = importlib.import_module("nanzi_datus_bridge.config_builder")
    provider = provider_module.NanziAuthProvider()

    assert hasattr(config_module, "NanziConfigBuilder")
    with pytest.raises(RuntimeError, match="Task 7"):
        asyncio.run(provider.authenticate(object()))
