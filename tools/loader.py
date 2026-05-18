from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .registry import ToolRegistry


TOOLS_DIR = Path(__file__).resolve().parent


def load_schema(schema_path: str | Path) -> dict[str, Any]:
    path = Path(schema_path)
    if not path.exists():
        raise FileNotFoundError(f"Tool schema file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_registry(
    llm,
    run_root: str | Path,
    *,
    batch_size: int = 5,
    max_context: int = 16384,
    enable_screen_context: bool = True,
    screen_interval_sec: float = 5.0,
    screen_debug_log: bool = False,
):
    from services.run_store_tool_funcs import RunStoreService
    from storage.vector_store import VectorStore
    from services.rag_service import RAGService
    from services.screen_tool_funcs import ScreenContextService

    from .current_time_tool import CurrentTimeTool
    from .document_summarize_tool import DocumentSummarizeTool
    from .fetch_webpage_tool import FetchWebpageTool
    from .final_report_tool import FinalReportTool
    from .query_plan_tool import QueryPlanTool
    from .rag_tool import RAGSearchTool
    from .screen_context_tool import ScreenContextTool
    from .term_grounding_tool import TermGroundingTool
    from .verify_flow_planner_tool import VerifyFlowPlannerTool
    from .web_search_tool import WebSearchTool

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

    # Verify flow planner — used by services/verification/sections, also
    # exposed in the chat registry so a future "/verify-flow" command (or
    # tool-using agent) can reach the same outline capability.
    registry.register(
        VerifyFlowPlannerTool(
            schema=load_schema(TOOLS_DIR / "verify_flow_planner_tool" / "tool_schema.json"),
            llm=llm,
        )
    )

    rag_service = RAGService(
        llm=llm,
        vector_store=VectorStore(
            persist_dir=Path(run_root) / "chromadb",
            collection_name="research_docs",
        ),
    )

    registry.register(
        RAGSearchTool(
            schema=load_schema(TOOLS_DIR / "rag_tool" / "tool_schema.json"),
            rag_service=rag_service,
        )
    )

    if enable_screen_context:
        try:
            screen_context_service = ScreenContextService(
                run_root,
                interval_sec=screen_interval_sec,
                console_log=screen_debug_log,
            )
            registry.register(
                ScreenContextTool(
                    schema=load_schema(TOOLS_DIR / "screen_context_tool" / "tool_schema.json"),
                    screen_context_service=screen_context_service,
                )
            )
        except Exception as e:
            print(f"[screen_context][warn] screen context tool disabled: {e}")

    return registry, run_store_service, rag_service
