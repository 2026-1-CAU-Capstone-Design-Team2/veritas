from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from langchain_community.tools import DuckDuckGoSearchResults

from tools.tool import BaseTool, ToolResult


@dataclass
class SearchResult:
    title: str
    link: str
    snippet: str


class WebSearchTool(BaseTool):
    """
    Web search tool using DuckDuckGo.

    Expected schema name:
        "web_search"

    Expected arguments (example):
        query: str
        num_results: int = 5
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        super().__init__(schema=schema)
        self._search_backend = DuckDuckGoSearchResults(
            output_format="list",
            max_results=10,
        )

    @property
    def name(self) -> str:
        return "web_search"

    def run(self, query: str, num_results: int = 5) -> ToolResult:
        query = (query or "").strip()

        if not query:
            return ToolResult(
                success=False,
                error="`query` must be a non-empty string.",
            )

        if num_results <= 0:
            return ToolResult(
                success=False,
                error="`num_results` must be greater than 0.",
            )

        try:
            raw_results = self._search_backend.invoke(query)
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Web search failed: {e}",
            )

        try:
            normalized_results = self._normalize_results(raw_results, limit=num_results)
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Failed to normalize search results: {e}",
            )

        summary_lines = []
        for idx, item in enumerate(normalized_results, start=1):
            summary_lines.append(
                f"{idx}. {item.title}\n"
                f"   URL: {item.link}\n"
                f"   Snippet: {item.snippet}"
            )

        content = "\n\n".join(summary_lines) if summary_lines else "No search results found."

        return ToolResult(
            success=True,
            content=content,
            data={
                "query": query,
                "count": len(normalized_results),
                "results": [asdict(item) for item in normalized_results],
            },
        )

    def _normalize_results(self, raw_results: Any, limit: int) -> list[SearchResult]:
        """
        Normalize DuckDuckGoSearchResults output into a stable list[SearchResult].

        Typical output_format="list" shape:
            [
                {"title": "...", "link": "...", "snippet": "..."},
                ...
            ]
        """
        if raw_results is None:
            return []

        if not isinstance(raw_results, list):
            raise TypeError(f"Expected list from search backend, got: {type(raw_results).__name__}")

        normalized: list[SearchResult] = []

        for item in raw_results:
            if not isinstance(item, dict):
                continue

            title = str(item.get("title", "")).strip()
            link = str(item.get("link", "")).strip()
            snippet = str(item.get("snippet", "")).strip()

            if not title and not link and not snippet:
                continue

            normalized.append(
                SearchResult(
                    title=title,
                    link=link,
                    snippet=snippet,
                )
            )

            if len(normalized) >= limit:
                break

        return normalized