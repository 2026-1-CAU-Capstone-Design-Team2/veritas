from __future__ import annotations

from typing import Any

from tools.tool import BaseTool, ToolResult


class TableQueryTool(BaseTool):
    """LLM-facing structured query over registered local tabular files.

    Unlike rag_search (embedding retrieval over indexed summary chunks), this
    tool reads the original .csv/.xlsx files listed in the local-corpus
    manifest and runs filter/aggregate operations over the FULL data — no row
    caps, no embedding loss. The actual query engine lives in
    services.local_corpus.TableQueryService; this tool only exposes it to
    tool-calling agents.
    """

    def __init__(self, schema: dict[str, Any], table_query_service, llm=None) -> None:
        super().__init__(schema=schema)
        self.table_query_service = table_query_service
        # Generation LLM that will consume this tool's output (the chat LLM).
        # Held only for the local-privacy guard below.
        self.llm = llm

    @property
    def name(self) -> str:
        return "table_query"

    def run(
        self,
        operation: str | None = None,
        file_name: str | None = None,
        sheet_name: str | None = None,
        columns: list[str] | None = None,
        where: list[dict[str, Any]] | None = None,
        group_by: list[str] | None = None,
        aggregate: list[dict[str, Any]] | None = None,
        sort_by: str | None = None,
        descending: bool = False,
        limit: int = 50,
        **_: Any,
    ) -> ToolResult:
        try:
            self._ensure_local_generation_allowed()
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        action = str(operation or "").strip().lower()
        if action not in {"list_tables", "describe", "query"}:
            return ToolResult(
                success=False,
                error="`operation` must be one of: list_tables, describe, query.",
            )

        try:
            if action == "list_tables":
                data = self.table_query_service.list_tables()
            elif action == "describe":
                data = self.table_query_service.describe(file_name or "", sheet_name)
            else:
                data = self.table_query_service.query(
                    file_name or "",
                    sheet_name=sheet_name,
                    columns=columns,
                    where=where,
                    group_by=group_by,
                    aggregate=aggregate,
                    sort_by=sort_by,
                    descending=descending,
                    limit=limit,
                )
            return ToolResult(success=True, data=data)
        except Exception as e:
            return ToolResult(success=False, error=f"Table query failed: {e}")

    def _ensure_local_generation_allowed(self) -> None:
        """Local table contents are local_private data — same guard as
        RAGService._ensure_local_generation_allowed: refuse to surface them
        into a generation pipeline backed by an external provider."""
        if self.llm is None:
            return
        module_name = type(self.llm).__module__.lower()
        class_name = type(self.llm).__name__.lower()
        if "openai" in module_name or "openai" in class_name:
            raise RuntimeError(
                "Local table data requires a local LLM. "
                "Refusing to expose local table contents to an external provider."
            )


__all__ = ["TableQueryTool"]
