from .tool import BaseTool, ToolResult
from .registry import ToolRegistry

__all__ = [
    "BaseTool",
    "ToolResult",
    "ToolRegistry",
    "build_registry",
    "load_schema",
    "RAGTool",
]


def __getattr__(name: str):
    if name in {"build_registry", "load_schema"}:
        from .loader import build_registry, load_schema

        return {
            "build_registry": build_registry,
            "load_schema": load_schema,
        }[name]

    if name == "RAGTool":
        from .rag_tool import RAGTool

        return RAGTool

    raise AttributeError(f"module 'tools' has no attribute {name!r}")
