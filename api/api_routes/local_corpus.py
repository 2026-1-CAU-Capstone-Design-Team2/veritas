from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..api_models import LocalCorpusDeleteRequest, LocalCorpusIndexRequest
from ..services import local_corpus_app_service

router = APIRouter()


@router.post("/api/v1/local-corpus/index")
def local_corpus_index(payload: LocalCorpusIndexRequest) -> dict[str, Any]:
    return local_corpus_app_service.index_workspace_sources(
        payload.workspaceId,
        payload.roots,
        clear_local_first=payload.clearLocalFirst,
    )


@router.get("/api/v1/local-corpus/sources/{workspaceId}")
def local_corpus_sources(workspaceId: str) -> dict[str, Any]:
    return local_corpus_app_service.list_sources(workspaceId)


@router.delete("/api/v1/local-corpus/sources/{workspaceId}")
def local_corpus_delete(
    workspaceId: str,
    payload: LocalCorpusDeleteRequest,
) -> dict[str, Any]:
    return local_corpus_app_service.remove_sources(workspaceId, payload.sourceIds)
