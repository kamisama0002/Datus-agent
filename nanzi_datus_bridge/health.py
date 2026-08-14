"""Local-only health and readiness diagnostics for the NanZi bridge."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys
from urllib.parse import urlsplit

from nanzi_datus_bridge.nanzi_client import NANZI_DATUS_PROTOCOL
from nanzi_datus_bridge.runtime_settings import resolve_setting, service_token_status


_AUTH_PROVIDER = "nanzi_datus_bridge.auth_provider.NanziAuthProvider"
_CALLBACK_TEMPLATE = "${NANZI_CALLBACK_URL}"
_TOKEN_TEMPLATE = "${NANZI_DATUS_INTERNAL_TOKEN}"


def is_nanzi_mode(api_config: object) -> bool:
    """Return whether Datus is configured with the NanZi auth provider."""
    if not isinstance(api_config, Mapping):
        return False
    provider = api_config.get("auth_provider")
    return isinstance(provider, Mapping) and provider.get("class") == _AUTH_PROVIDER


def build_nanzi_health(
    *,
    environment: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, object]:
    """Build sanitized readiness details without contacting external services."""
    env = os.environ if environment is None else environment
    resolved_config = config_path or env.get("DATUS_CONFIG") or "conf/agent-nanzi.example.yml"
    checks = {
        "callback_url": _callback_status(env.get("NANZI_CALLBACK_URL", "")),
        "service_token": service_token_status(env.get("NANZI_DATUS_INTERNAL_TOKEN", "")),
        "config": _config_status(Path(resolved_config), env),
    }
    return {
        "liveness": "alive",
        "nanzi": {
            "protocol": NANZI_DATUS_PROTOCOL,
            "ready": all(value == expected for value, expected in zip(checks.values(), _ready_values())),
            "checks": checks,
        },
    }


def _ready_values() -> tuple[str, str, str]:
    return ("configured", "configured", "compatible")


def _callback_status(raw_value: object) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip() or "${" in raw_value:
        return "missing"
    try:
        parsed = urlsplit(raw_value.strip())
        port = parsed.port
    except (TypeError, ValueError):
        return "invalid"
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        return "invalid"
    return "configured"


def _config_status(config_path: Path, environment: Mapping[str, str]) -> str:
    if not config_path.exists():
        return "missing"
    if not config_path.is_file():
        return "unreadable"
    try:
        from datus.configuration.agent_config_loader import ConfigurationManager

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            agent = ConfigurationManager(str(config_path)).data
    except FileNotFoundError:
        return "missing"
    except (OSError, UnicodeError):
        return "unreadable"
    except Exception:
        return "incompatible"
    if not isinstance(agent, Mapping):
        return "incompatible"
    api = agent.get("api")
    provider = api.get("auth_provider") if isinstance(api, Mapping) else None
    if not isinstance(provider, Mapping) or provider.get("class") != _AUTH_PROVIDER:
        return "incompatible"
    kwargs = provider.get("kwargs")
    if not isinstance(kwargs, Mapping):
        return "incompatible"
    if (
        kwargs.get("callback_url") != _CALLBACK_TEMPLATE
        or kwargs.get("service_token") != _TOKEN_TEMPLATE
        or kwargs.get("protocol") != NANZI_DATUS_PROTOCOL
    ):
        return "incompatible"
    callback_url = resolve_setting(kwargs.get("callback_url"), "NANZI_CALLBACK_URL", environment)
    service_token = resolve_setting(kwargs.get("service_token"), "NANZI_DATUS_INTERNAL_TOKEN", environment)
    if _callback_status(callback_url) != "configured" or service_token_status(service_token) != "configured":
        return "incompatible"
    return "compatible"


def main(argv: Sequence[str] | None = None) -> int:
    """Run a non-starting preflight check and emit only sanitized JSON."""
    parser = argparse.ArgumentParser(description="Check local NanZi bridge configuration without starting Datus")
    parser.add_argument("--config", default="conf/agent-nanzi.example.yml")
    args = parser.parse_args(argv)
    health = build_nanzi_health(config_path=args.config)
    sys.stdout.write(json.dumps(health, ensure_ascii=True, sort_keys=True) + "\n")
    return 0 if health["nanzi"]["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
