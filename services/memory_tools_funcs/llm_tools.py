"""LLM이 호출 중 직접 부르는 memory self-edit tools.

LLM에게 노출되는 3개:
- working_context_append: stable fact를 working_context에 추가
- working_context_replace: 기존 fact를 치환
- recall_search: 옛 turn을 키워드로 검색
"""

from __future__ import annotations

from typing import Any, Callable


WORKING_CONTEXT_APPEND_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "working_context_append",
        "description": (
            "Save a stable fact about the user, project, or session into working context. "
            "Use sparingly for facts that remain true across many turns (preferences, ongoing tasks, key constraints)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "A single concise fact to remember.",
                },
            },
            "required": ["fact"],
        },
    },
}


WORKING_CONTEXT_REPLACE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "working_context_replace",
        "description": (
            "Replace an existing fact in working context with an updated version. "
            "Use when a previously stored fact becomes stale or contradicted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "old": {"type": "string", "description": "Exact text of the fact to replace."},
                "new": {"type": "string", "description": "Updated fact text."},
            },
            "required": ["old", "new"],
        },
    },
}


RECALL_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "recall_search",
        "description": (
            "Search the recall storage (full history of past turns) for specific details that "
            "are not in the current context window. Use when you need an exact past detail."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for."},
                "limit": {
                    "type": "integer",
                    "description": "Max number of matches to return (default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}


MEMORY_TOOL_SCHEMAS: list[dict[str, Any]] = [
    WORKING_CONTEXT_APPEND_SCHEMA,
    WORKING_CONTEXT_REPLACE_SCHEMA,
    RECALL_SEARCH_SCHEMA,
]


def build_memory_tool_runner(runtime) -> Callable[[str, dict[str, Any]], Any]:
    """tool_name + arguments → memory_runtime 메서드 호출로 dispatch.

    runtime은 MemoryRuntime. 직접 import 안 함(circular 회피).
    """

    def _runner(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        name = (tool_name or "").strip()
        args = arguments or {}

        if name == "working_context_append":
            fact = str(args.get("fact") or "").strip()
            if not fact:
                return {"ok": False, "error": "fact is required"}
            runtime.working.append_fact(fact)
            return {"ok": True, "appended": fact}

        if name == "working_context_replace":
            old = str(args.get("old") or "").strip()
            new = str(args.get("new") or "").strip()
            if not old:
                return {"ok": False, "error": "old is required"}
            success = runtime.working.replace_fact(old, new)
            return {"ok": success, "replaced": success}

        if name == "recall_search":
            query = str(args.get("query") or "").strip()
            try:
                limit = int(args.get("limit") or 5)
            except (TypeError, ValueError):
                limit = 5
            if not query:
                return {"matches": [], "count": 0}
            results = runtime.recall.search(query, limit=max(1, min(20, limit)))
            return {
                "matches": [
                    {
                        "role": r.get("role"),
                        "content": r.get("content"),
                        "created_at": r.get("created_at"),
                    }
                    for r in results
                ],
                "count": len(results),
            }

        return {"ok": False, "error": f"unknown memory tool: {name}"}

    return _runner
