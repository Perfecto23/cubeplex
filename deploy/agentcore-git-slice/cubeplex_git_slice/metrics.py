"""Small, JSON-safe worker metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CloneMetrics:
    fresh_clone_ms: float | None = None
    reuse_ms: float | None = None
    restore_ms: float | None = None
    fresh_received_bytes: int | None = None
    reuse_received_bytes: int | None = None
    fresh_object_bytes: int | None = None
    reuse_object_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fresh_clone_ms": self.fresh_clone_ms,
            "reuse_ms": self.reuse_ms,
            "restore_ms": self.restore_ms,
            "fresh_received_bytes": self.fresh_received_bytes,
            "reuse_received_bytes": self.reuse_received_bytes,
            "fresh_object_bytes": self.fresh_object_bytes,
            "reuse_object_bytes": self.reuse_object_bytes,
        }


@dataclass(slots=True)
class Metrics:
    boot_id: str
    clone: CloneMetrics = field(default_factory=CloneMetrics)
    model_calls: int = 0
    tool_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "boot_id": self.boot_id,
            "clone": self.clone.to_dict(),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
        }
