from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidationError(ValueError):
    """Raised when a schema object fails lightweight validation."""

    message: str
    line: Optional[int] = None
    seq: Optional[int] = None
    field: Optional[str] = None

    def __str__(self) -> str:
        parts: list[str] = [self.message]
        context: list[str] = []
        if self.line is not None:
            context.append(f"line={self.line}")
        if self.seq is not None:
            context.append(f"seq={self.seq}")
        if self.field:
            context.append(f"field={self.field}")
        if context:
            parts.append(f"({' '.join(context)})")
        return " ".join(parts)
