from __future__ import annotations

import json
import os
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
    autosurvey_llm=None,
    embedding_llm=None,
    batch_size: int = 5,
    max_context: int = 16384,
    enable_screen_context: bool = True,
    screen_interval_sec: float = 5.0,
    screen_debug_log: bool = False,
    custom_document_tools: list[dict[str, Any]] | None = None,
):
    from services.run_store_tool_funcs import RunStoreService
    from storage.vector_store import VectorStore
    from services.rag_service import RAGService
    from services.local_corpus import TableQueryService
    from services.screen_tool_funcs import ScreenContextService

    from .current_time_tool import CurrentTimeTool
    from .document_cleanup_tool import DocumentCleanupTool
    from .document_summarize_tool import DocumentSummarizeTool
    from .fetch_webpage_tool import FetchWebpageTool
    from .final_report_tool import FinalReportTool
    from .query_plan_tool import QueryPlanTool
    from .rag_tool import RAGSearchTool
    from .screen_context_tool import ScreenContextTool
    from .table_query_tool import TableQueryTool
    from .term_grounding_tool import TermGroundingTool
    from .verify_flow_planner_tool import VerifyFlowPlannerTool
    from .web_search_tool import WebSearchTool

    registry = ToolRegistry()
    run_store_service = RunStoreService(run_root)
    research_llm = autosurvey_llm or llm
    dense_llm = embedding_llm or llm

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
            llm=research_llm,
            tool_registry=registry,
        )
    )

    registry.register(
        QueryPlanTool(
            schema=load_schema(TOOLS_DIR / "query_plan_tool" / "tool_schema.json"),
            llm=research_llm,
            run_store_service=run_store_service,
            tool_registry=registry,
        )
    )

    registry.register(
        DocumentSummarizeTool(
            schema=load_schema(TOOLS_DIR / "document_summarize_tool" / "tool_schema.json"),
            llm=research_llm,
            run_store_service=run_store_service,
            batch_size=batch_size,
            max_context=max_context,
        )
    )

    # Per-doc cleanup — strips boilerplate paragraphs the LLM flags in raw_md
    # and persists the cleaned body to clean_md/<id>.md plus a meta-only
    # summary/doc_<id>.md. Runs after fetch in AutoSurvey, replacing the
    # previous per-doc LLM summarize pass on the workflow's critical path.
    #
    # cleanup_mode "auto" resolves by the research LLM's type: the local
    # llama-server client keeps the per-document boilerplate-removal call;
    # an external API client (OpenAI) takes the batch path — clean_md becomes
    # a raw_md pass-through and per-doc metadata comes from ONE call per
    # collect cycle, cutting AutoSurvey's per-doc LLM call count.
    # Override with VERITAS_AUTOSURVEY_CLEANUP_MODE=per_doc|batch.
    registry.register(
        DocumentCleanupTool(
            schema=load_schema(TOOLS_DIR / "document_cleanup_tool" / "tool_schema.json"),
            llm=research_llm,
            run_store_service=run_store_service,
            cleanup_mode=os.getenv("VERITAS_AUTOSURVEY_CLEANUP_MODE", "auto"),
        )
    )

    registry.register(
        FinalReportTool(
            schema=load_schema(TOOLS_DIR / "final_report_tool" / "tool_schema.json"),
            llm=research_llm,
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
        llm=dense_llm,
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

    # Structured queries over registered local .csv/.xlsx files — reads the
    # ORIGINAL files from the local-corpus manifest (run_root/local/), so data
    # questions get loss-free answers instead of the 200-row indexed profile.
    # `llm` (the chat LLM that consumes the output) is passed only for the
    # local-privacy guard.
    registry.register(
        TableQueryTool(
            schema=load_schema(TOOLS_DIR / "table_query_tool" / "tool_schema.json"),
            table_query_service=TableQueryService(run_root),
            llm=llm,
        )
    )

    if enable_screen_context:
        try:
            screen_context_service = ScreenContextService(
                run_root,
                interval_sec=screen_interval_sec,
                console_log=screen_debug_log,
                llm=llm,
                custom_document_tools=custom_document_tools,
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
