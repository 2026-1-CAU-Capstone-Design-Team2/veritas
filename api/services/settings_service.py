from __future__ import annotations

from typing import Any

from llm.model_catalog import DEFAULT_LLM_MODEL_ID

from ..repositories import state_repository as repo


def get_settings() -> dict[str, Any]:
    return repo.get_settings()


def update_model(model_id: str | None, legacy_name: str | None = None) -> dict[str, Any]:
    legacy_map = {
        "0.8B": "qwen35-0.8b-q8_0",
        "2B": "qwen35-2b-q8_0",
        "4B": "qwen35-4b-q4",
        "9B": "qwen35-9b-q4",
    }
    resolved_model_id = model_id or legacy_map.get(str(legacy_name or ""), DEFAULT_LLM_MODEL_ID)
    model = repo.set_model_settings(resolved_model_id)
    return {"model": model, "updated": True}


def update_embedding_model(model_id: str) -> dict[str, Any]:
    embedding_model = repo.set_embedding_model_settings(model_id)
    return {"embeddingModel": embedding_model, "updated": True}


def update_local_access(folder_paths: list[str]) -> dict[str, Any]:
    local_access = repo.set_local_access_settings(folder_paths)
    return {"localAccess": local_access, "updated": True}


def update_document_tools(custom_tools: list[dict[str, Any]]) -> dict[str, Any]:
    document_tools = repo.set_document_tools_settings(custom_tools)
    return {"documentTools": document_tools, "updated": True}


def update_research_method(sample_count: int, plan_count: int) -> dict[str, Any]:
    research = repo.set_research_method_settings(sample_count, plan_count)
    return {"research": research, "updated": True}


def update_llm_parallel(value: int) -> dict[str, Any]:
    """Persist the parallel-decoding concurrency and apply it to the live
    shared LLM client.

    ``LLMClient.map_parallel`` reads ``max_parallel`` at call time, so updating
    the attribute on the already-constructed runtime client takes effect on the
    next batch (cleanup / summarize / embeddings) without a restart. The live
    apply is best-effort: if the runtime has not been built yet the persisted
    STATE value is what matters, and a runtime built later still starts from the
    env default (the UI re-applies on the next save).
    """
    parallel = repo.set_llm_parallel_settings(value)
    try:
        # Lazy import to avoid a circular import at module load
        # (agent_runtime imports a large dependency graph).
        from .agent_runtime import get_runtime

        # Delegate to the runtime's own setter instead of reaching through
        # ``runtime.llm.max_parallel`` (keeps the LLM-client mutation
        # encapsulated behind AgentRuntime).
        get_runtime().set_llm_parallel(parallel)
    except Exception:
        pass
    return {"llmParallel": parallel, "updated": True}
