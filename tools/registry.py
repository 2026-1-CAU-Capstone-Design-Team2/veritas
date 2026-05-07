from __future__ import annotations

from typing import Any

from .tool import BaseTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        name = tool.name
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = tool

    def unregister(self, name: str) -> None:
        if name not in self._tools:
            raise KeyError(f"Tool not registered: {name}")
        del self._tools[name]

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            available = ", ".join(sorted(self._tools.keys()))
            raise KeyError(f"Tool not registered: {name}. Available tools: {available}")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def list_schemas(self) -> list[dict[str, Any]]:
        return [tool.schema for tool in self._tools.values()]

    def call(self, name: str, **kwargs: Any):
        tool = self.get(name)
        return tool.run(**kwargs)