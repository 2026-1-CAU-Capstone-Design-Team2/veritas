from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    success: bool
    content: str | None = None
    data: Any | None = None
    error: str | None = None

class BaseTool(ABC):
    def __init__(self, schema: dict[str, Any]):
        self._schema = schema

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    def schema(self) -> dict[str, Any]:
        return self._schema

    @abstractmethod
    def run(self, **kwargs) -> ToolResult:
        raise NotImplementedError