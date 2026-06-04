from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..services import document_citation_service, documents_service

router = APIRouter()


@router.get("/api/v1/documents/{workspaceId}/summary")
async def document_summary(workspaceId: str) -> dict[str, str]:
    return documents_service.get_document_summary(workspaceId)


@router.get("/api/v1/documents/{workspaceId}/merged")
async def document_merged(workspaceId: str) -> dict[str, str]:
    return documents_service.get_document_merged(workspaceId)


@router.get("/api/v1/documents/{workspaceId}/citations/{docId}")
def document_citation(
    workspaceId: str, docId: str, claim: str = ""
) -> dict[str, Any]:
    """Resolve a clicked ``[doc_NNN]`` citation to its source snippet.

    Thin wrapper — all file reading, lexical matching, and metadata assembly
    live in :mod:`api.services.document_citation_service`. Declared plain
    ``def`` (not ``async``) so the synchronous ``clean_md`` read + sentence scan
    runs on the threadpool and never blocks the event loop.
    """
    return document_citation_service.get_citation(workspaceId, docId, claim)
