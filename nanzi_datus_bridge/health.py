"""Local-only health and readiness diagnostics for the NanZi bridge."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys
from urllib.parse import urlsplit

from nanzi_datus_bridge.nanzi_client import NANZI_DATUS_PROTOCOL


_AUTH_PROVIDER = "nanzi_datus_bridge.auth_provider.NanziAuthProvider"
_PLACEHOLDER_TOKENS = {"change-me", "changeme", "placeholder", "replace-me", "your-token"}
_CONFIG_MARKERS = (
    f"class: {_AUTH_PROVIDER}",
    "callback_url: ${NANZI_CALLBACK_URL}",
    "service_token: ${NANZI_DATUS_INTERNAL_TOKEN}",
    f"protocol: {NANZI_DATUS_PROTOCOL}",
)


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
        "service_token": _token_status(env.get("NANZI_DATUS_INTERNAL_TOKEN", "")),
        "config": _config_status(Path(resolved_config)),
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


def _token_status(raw_value: object) -> str:
    if not isinstance(raw_value, str):
        return "missing"
    value = raw_value.strip()
    if not value or "${" in value:
        return "missing"
    if value.lower() in _PLACEHOLDER_TOKENS:
        return "placeholder"
    return "configured"


def _config_status(config_path: Path) -> str:
    try:
        content = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing"
    except (OSError, UnicodeError):
        return "unreadable"
    return "compatible" if all(marker in content for marker in _CONFIG_MARKERS) else "incompatible"


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
