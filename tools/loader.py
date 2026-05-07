from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .registry import ToolRegistry

from .web_search_tool import WebSearchTool
from .fetch_webpage_tool import FetchWebpageTool
from .current_time_tool import CurrentTimeTool
from .term_grounding_tool import TermGroundingTool
from .query_plan_tool import QueryPlanTool
from .document_summarize_tool import DocumentSummarizeTool
from .final_report_tool import FinalReportTool

from services.run_store_tool_funcs import RunStoreService


TOOLS_DIR = Path(__file__).resolve().parent


def load_schema(schema_path: str | Path) -> dict[str, Any]:
    path = Path(schema_path)
    if not path.exists():
        raise FileNotFoundError(f"Tool schema file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_registry(llm, run_root: str | Path, *, batch_size: int = 5, max_context: int = 16384):
    registry = ToolRegistry()
    run_store_service = RunStoreService(run_root)

    registry.register(
        WebSearchTool(
            schema=load_schema(TOOLS_DIR / "web_search_tool" / "tool_schema.json")
        )
    )

    registry.register(
        FetchWebpageTool(
            schema=load_schema(TOOLS_DIR / "fetch_webpage_tool" / "tool_schema.json")
        )
    )

    registry.register(
        CurrentTimeTool(
            schema=load_schema(TOOLS_DIR / "current_time_tool" / "tool_schema.json")
        )
    )

    registry.register(
        TermGroundingTool(
            schema=load_schema(TOOLS_DIR / "term_grounding_tool" / "tool_schema.json"),
            llm=llm,
            tool_registry=registry,
        )
    )

    registry.register(
        QueryPlanTool(
            schema=load_schema(TOOLS_DIR / "query_plan_tool" / "tool_schema.json"),
            llm=llm,
            run_store_service=run_store_service,
            tool_registry=registry,
        )
    )

    registry.register(
        DocumentSummarizeTool(
            schema=load_schema(TOOLS_DIR / "document_summarize_tool" / "tool_schema.json"),
            llm=llm,
            run_store_service=run_store_service,
            batch_size=batch_size,
            max_context=max_context,
        )
    )

    registry.register(
        FinalReportTool(
            schema=load_schema(TOOLS_DIR / "final_report_tool" / "tool_schema.json"),
            llm=llm,
            run_store_service=run_store_service,
        )
    )

    return registry, run_store_service