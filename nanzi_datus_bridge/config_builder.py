"""Configuration-builder interface reserved for NanZi callback integration."""

from __future__ import annotations

from typing import Protocol


class NanziConfigBuilder(Protocol):
    """Build the Datus configuration authorized for one NanZi project.

    Task 7 supplies the implementation after it can validate the NanZi
    callback and resolve an authorized project identity.
    """

    def build_agent_config(self, project_id: str) -> object:
        """Return the Datus configuration for an authenticated project."""
        ...
