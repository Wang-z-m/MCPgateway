from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GatewayError(Exception):
    category: str
    message: str
    jsonrpc_code: int
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.category}: {self.message}"
