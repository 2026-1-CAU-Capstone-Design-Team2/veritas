from __future__ import annotations

from typing import Any, Callable, Iterable

from tools.registry import ToolRegistry


LLMToolRunner = Callable[[str, dict[str, Any]], Any]


def build_llm_tooling(
    tool_registry: ToolRegistry | None,
    *,
    stage_label: str,
    allowed_tool_names: Iterable[str],
    expose_predicate: Callable[[str], bool] | None = None,
) -> tuple[list[dict[str, Any]] | None, LLMToolRunner | None]:
    """Build tool schemas and a shared tool runner for LLM tool-calling.

    Args:
        tool_registry: Registry that stores executable tools.
        stage_label: Human-readable stage name for error messages.
        allowed_tool_names: Candidate tool names that can be exposed.
        expose_predicate: Optional predicate for per-tool conditional exposure.
    """
    if tool_registry is None:
        return None, None

    selected_names: list[str] = []
    selected_schemas: list[dict[str, Any]] = []

    for raw_name in allowed_tool_names:
        tool_name = str(raw_name).strip()
        if not tool_name:
            continue
        if expose_predicate is not None and not expose_predicate(tool_name):
            continue
        if not tool_registry.has(tool_name):
            continue

        selected_names.append(tool_name)
        selected_schemas.append(tool_registry.get(tool_name).schema)

    if not selected_schemas:
        return None, None

    allowed = set(selected_names)

    def _tool_runner(name: str, arguments: dict[str, Any]) -> Any:
        tool_name = str(name or "").strip()
        if tool_name not in allowed:
            return {"error": f"Unsupported tool for {stage_label}: {tool_name}"}

        result = tool_registry.call(tool_name, **(arguments or {}))
        if not result.success:
            return {"error": result.error or f"{tool_name} tool failed"}

        if result.data is not None:
            return result.data
        return {"content": result.content or ""}

    return selected_schemas, _tool_runner
