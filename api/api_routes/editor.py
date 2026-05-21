from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ..api_models import (
    EditorAssistRequest,
    EditorChatRequest,
    EditorExportRequest,
    EditorSaveRequest,
    EditorSuggestRequest,
)
from ..services import editor_service

router = APIRouter()


@router.get("/api/v1/editor/document")
def editor_load(
    workspaceId: str = Query(...),
    source: str = Query("new"),
    docId: str | None = Query(default=None),
) -> dict[str, Any]:
    return editor_service.load_document(workspaceId, source, docId)


@router.get("/api/v1/editor/documents")
def editor_list(workspaceId: str = Query(...)) -> dict[str, Any]:
    return editor_service.list_documents(workspaceId)


@router.post("/api/v1/editor/document")
def editor_save(payload: EditorSaveRequest) -> dict[str, Any]:
    return editor_service.save_document(
        payload.workspaceId, payload.docId, payload.content, payload.title
    )


@router.post("/api/v1/editor/suggest")
async def editor_suggest(payload: EditorSuggestRequest) -> StreamingResponse:
    return StreamingResponse(
        editor_service.suggest_stream(
            payload.workspaceId, payload.prefix, payload.suffix, payload.maxTokens, payload.useWorkspace
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/v1/editor/assist")
async def editor_assist(payload: EditorAssistRequest) -> StreamingResponse:
    return StreamingResponse(
        editor_service.assist_stream(
            payload.workspaceId, payload.action, payload.text, payload.maxTokens, payload.useWorkspace
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/v1/editor/chat")
async def editor_chat(payload: EditorChatRequest) -> StreamingResponse:
    return StreamingResponse(
        editor_service.chat_stream(payload.workspaceId, payload.message, payload.docText, payload.useWorkspace),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/editor/sources")
def editor_sources(workspaceId: str = Query(...)) -> dict[str, Any]:
    return editor_service.get_sources(workspaceId)


@router.post("/api/v1/editor/export")
def editor_export(payload: EditorExportRequest) -> dict[str, Any]:
    # Sync pandoc subprocess — plain `def` keeps the event loop free.
    return editor_service.export_document(
        payload.workspaceId,
        payload.content,
        payload.format,
        payload.outputPath,
        payload.docId,
    )
