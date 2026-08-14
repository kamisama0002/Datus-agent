"""Shared, side-effect-free NanZi bridge runtime setting validation."""

from __future__ import annotations

from collections.abc import Mapping
import os
import re


_ENV_PLACEHOLDER = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
_PLACEHOLDER_NORMALIZED = {
    "placeholder",
    "replaceme",
    "replacewithasharedsecret",
    "sharedruntimetoken",
    "yourtoken",
}


def resolve_setting(
    value: object,
    environment_name: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve one literal or ``${ENV_NAME}`` setting without exposing it."""
    env = os.environ if environment is None else environment
    candidate = value
    if candidate is None:
        candidate = env.get(environment_name, "")
    if not isinstance(candidate, str):
        return ""
    candidate = candidate.strip()
    match = _ENV_PLACEHOLDER.fullmatch(candidate)
    if match:
        candidate = env.get(match.group(1), "").strip()
    if "${" in candidate:
        return ""
    return candidate


def service_token_status(value: object) -> str:
    """Classify a service token without returning or logging its value."""
    if not isinstance(value, str):
        return "missing"
    candidate = value.strip()
    if not candidate or "${" in candidate:
        return "missing"
    lowered = candidate.lower()
    normalized = re.sub(r"[^a-z0-9]", "", lowered)
    if (
        (candidate.startswith("<") and candidate.endswith(">"))
        or normalized in _PLACEHOLDER_NORMALIZED
        or normalized.startswith("changeme")
        or normalized.startswith("example")
    ):
        return "placeholder"
    return "configured"
