"""Fail-closed AuthProvider skeleton for the NanZi-to-Datus boundary.

Task 7 will add NanZi callback validation and per-project configuration
construction. Until then, this provider must reject every request rather than
silently allowing unauthenticated access.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable


EvictCallback = Callable[[str], Awaitable[None]]


class NanziAuthProvider:
    """Datus AuthProvider-compatible placeholder that rejects all requests."""

    def __init__(self) -> None:
        self._evict_callbacks: list[EvictCallback] = []

    async def authenticate(self, request: object) -> None:
        """Reject requests until Task 7 implements NanZi authentication."""
        del request
        raise RuntimeError("NanZi authentication is not implemented; complete Task 7 before accepting requests.")

    def on_evict(self, callback: EvictCallback) -> None:
        """Store Datus cache-eviction callbacks for the future Task 7 provider."""
        self._evict_callbacks.append(callback)
