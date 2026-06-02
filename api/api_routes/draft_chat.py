from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from services import form_extract

from ..api_models import (
    ChatMessageRequest,
    DraftBuiltinGenerateRequest,
    DraftBuiltinRegenerateRequest,
    DraftGenerateRequest,
    DraftRegenerateRequest,
)
from ..services import draft_chat_service, draft_service

router = APIRouter()


@router.post("/api/v1/draft/forms/import")
async def draft_form_import(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    """Extract a Markdown form template + outline from an uploaded form file.

    Supports .docx / .doc / .hwp / .hwpx / .pdf (and plain text). The body is
    stripped heuristically, keeping only structure (headings / bullets / tables).
    Accepts a multipart ``files`` field (the shared upload client posts a list);
    only the first file is used.
    """
    if not files:
        raise HTTPException(status_code=400, detail="양식 파일이 필요합니다.")
    upload = files[0]
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail="빈 파일입니다.")
    return form_extract.extract_form(upload.filename or "", raw)


@router.get("/api/v1/draft/forms")
def draft_forms() -> dict[str, Any]:
    """Built-in form catalog + tone/length options the draft wizard renders."""
    return draft_service.list_forms()


@router.post("/api/v1/draft/builtin/generate")
def draft_builtin_generate(payload: DraftBuiltinGenerateRequest) -> dict[str, Any]:
    # Sync LLM call (tone-driven sampling) — plain `def` runs in the FastAPI
    # threadpool so the event loop stays responsive.
    return draft_service.generate_builtin_draft(payload.workspaceId, payload.model_dump())


@router.post("/api/v1/draft/builtin/regenerate")
def draft_builtin_regenerate(payload: DraftBuiltinRegenerateRequest) -> dict[str, Any]:
    return draft_service.regenerate_builtin_draft(payload.workspaceId, payload.draftNumber)


@router.get("/api/v1/draft/builtin/list")
def draft_builtin_list(workspaceId: str = Query(...)) -> dict[str, Any]:
    return draft_service.list_drafts(workspaceId)


@router.post("/api/v1/draft/generate")
def draft_generate(payload: DraftGenerateRequest) -> dict[str, Any]:
    # Sync LLM call — plain `def` keeps the event loop responsive.
    return draft_chat_service.generate_draft(payload.workspaceId, payload.prompt)


@router.post("/api/v1/draft/{draftId}/regenerate")
def draft_regenerate(draftId: str, payload: DraftRegenerateRequest) -> dict[str, Any]:
    return draft_chat_service.regenerate_draft(draftId, payload.prompt)


@router.post("/api/v1/chat/messages")
def chat_send(payload: ChatMessageRequest) -> dict[str, str]:
    return draft_chat_service.send_chat_message(
        payload.workspaceId,
        payload.message,
        payload.mode,
        source_scope_filter=payload.sourceScopeFilter,
        include_private_local=payload.includePrivateLocal,
    )


@router.post("/api/v1/chat/messages/stream")
async def chat_send_stream(payload: ChatMessageRequest) -> StreamingResponse:
    return StreamingResponse(
        draft_chat_service.send_chat_message_stream(
            payload.workspaceId,
            payload.message,
            payload.mode,
            doc_text=payload.docText,
            source=payload.source,
            source_scope_filter=payload.sourceScopeFilter,
            include_private_local=payload.includePrivateLocal,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/chat/sessions/{sessionId}/messages")
async def chat_history(sessionId: str, cursor: str | None = Query(default=None)) -> dict[str, Any]:
    _ = cursor
    return draft_chat_service.get_chat_history(sessionId)
