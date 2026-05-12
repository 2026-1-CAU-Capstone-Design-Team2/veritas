from __future__ import annotations

from fastapi import APIRouter

from ..services import documents_service

router = APIRouter()


@router.get("/api/v1/documents/{workspaceId}/summary")
async def document_summary(workspaceId: str) -> dict[str, str]:
    return documents_service.get_document_summary(workspaceId)


@router.get("/api/v1/documents/{workspaceId}/merged")
async def document_merged(workspaceId: str) -> dict[str, str]:
    return documents_service.get_document_merged(workspaceId)
