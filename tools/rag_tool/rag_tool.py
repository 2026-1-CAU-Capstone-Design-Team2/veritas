from __future__ import annotations

from typing import Any

from tools.tool import BaseTool, ToolResult


class RAGSearchTool(BaseTool):
    """Thin LLM-facing wrapper around RAGService.retrieve().

    The actual RAG implementation lives in services.rag_service.RAGService.
    This tool intentionally exposes only retrieval to tool-calling agents.
    Force-RAG Q&A should call RAGService.answer() directly.
    """

    def __init__(self, schema: dict[str, Any], rag_service) -> None:
        super().__init__(schema=schema)
        self.rag_service = rag_service

    @property
    def name(self) -> str:
        return "rag_search"

    def run(self, query: str | None = None, use_history: bool = True, **_: Any) -> ToolResult:
        if not query or not str(query).strip():
            return ToolResult(success=False, error="`query` is required for rag_search.")

        try:
            documents = self.rag_service.retrieve(str(query).strip(), use_history=use_history)
            return ToolResult(success=True, data={"documents": documents})
        except Exception as e:
            return ToolResult(success=False, error=f"RAG search failed: {e}")


# Backward-compatible class name for imports that still refer to RAGTool.
RAGTool = RAGSearchTool

__all__ = ["RAGSearchTool", "RAGTool"]
