from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..api_models import (
    SettingsAutosurveyOpenAIRequest,
    SettingsDocumentToolsRequest,
    SettingsEmbeddingModelRequest,
    SettingsLlamaContextRequest,
    SettingsLlmParallelRequest,
    SettingsLocalAccessRequest,
    SettingsModelRequest,
    SettingsResearchMethodRequest,
)
from ..services import settings_service

router = APIRouter()


@router.get("/api/v1/settings")
async def settings_get() -> dict[str, Any]:
    return settings_service.get_settings()


@router.put("/api/v1/settings/model")
def settings_model_update(payload: SettingsModelRequest) -> dict[str, Any]:
    # Plain `def` (not `async`): a live model switch may download a multi-GB
    # GGUF and restart llama-server, so FastAPI must run it on its thread pool
    # instead of blocking the event loop (the frontend issues it on a worker
    # thread and polls /settings/model/progress).
    return settings_service.update_model(payload.modelId, payload.modelName)


@router.get("/api/v1/settings/model/progress")
async def settings_model_progress(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    return settings_service.get_model_switch_progress(since=since, limit=limit)


@router.put("/api/v1/settings/embedding-model")
async def settings_embedding_model_update(payload: SettingsEmbeddingModelRequest) -> dict[str, Any]:
    return settings_service.update_embedding_model(payload.modelId)


@router.put("/api/v1/settings/local-access")
async def settings_local_access_update(payload: SettingsLocalAccessRequest) -> dict[str, Any]:
    return settings_service.update_local_access(payload.folderPaths)


@router.put("/api/v1/settings/document-tools")
async def settings_document_tools_update(payload: SettingsDocumentToolsRequest) -> dict[str, Any]:
    return settings_service.update_document_tools(
        [tool.model_dump() for tool in payload.customTools]
    )


@router.put("/api/v1/settings/research-method")
async def settings_research_method_update(payload: SettingsResearchMethodRequest) -> dict[str, Any]:
    return settings_service.update_research_method(payload.sampleCount, payload.planCount)


@router.put("/api/v1/settings/autosurvey-openai")
async def settings_autosurvey_openai_update(payload: SettingsAutosurveyOpenAIRequest) -> dict[str, Any]:
    return settings_service.update_autosurvey_openai(
        payload.apiKey,
        clear=payload.clear,
    )


@router.put("/api/v1/settings/llm-parallel")
async def settings_llm_parallel_update(payload: SettingsLlmParallelRequest) -> dict[str, Any]:
    return settings_service.update_llm_parallel(payload.value)


@router.put("/api/v1/settings/llama-context")
def settings_llama_context_update(payload: SettingsLlamaContextRequest) -> dict[str, Any]:
    return settings_service.update_llama_context(payload.mode, payload.tokens)
