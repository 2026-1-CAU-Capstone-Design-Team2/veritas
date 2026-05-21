from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..api_models import (
    SettingsDocumentToolsRequest,
    SettingsEmbeddingModelRequest,
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
async def settings_model_update(payload: SettingsModelRequest) -> dict[str, Any]:
    return settings_service.update_model(payload.modelId, payload.modelName)


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


@router.put("/api/v1/settings/llm-parallel")
async def settings_llm_parallel_update(payload: SettingsLlmParallelRequest) -> dict[str, Any]:
    return settings_service.update_llm_parallel(payload.value)
