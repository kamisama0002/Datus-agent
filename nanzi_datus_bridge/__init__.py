"""NanZi-owned integration boundary for the Datus API runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "NanziAuthProvider": ("nanzi_datus_bridge.auth_provider", "NanziAuthProvider"),
    "NanziCallbackError": ("nanzi_datus_bridge.nanzi_client", "NanziCallbackError"),
    "NanziClient": ("nanzi_datus_bridge.nanzi_client", "NanziClient"),
    "NanziConfigBuilder": ("nanzi_datus_bridge.config_builder", "NanziConfigBuilder"),
    "NanziConfigError": ("nanzi_datus_bridge.config_builder", "NanziConfigError"),
}

__all__ = [
    "NanziAuthProvider",
    "NanziCallbackError",
    "NanziClient",
    "NanziConfigBuilder",
    "NanziConfigError",
]


def __getattr__(name: str) -> Any:
    """Load bridge exports on demand so local diagnostics stay side-effect free."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
