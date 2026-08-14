"""Key adapters used by signed, top-level ProductionRun contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MappingHmacKeyProvider:
    """In-memory key provider for services and tests; keys are never persisted."""

    keys: dict[str, bytes]

    def get_key(self, key_id: str) -> bytes | None:
        return self.keys.get(key_id)
