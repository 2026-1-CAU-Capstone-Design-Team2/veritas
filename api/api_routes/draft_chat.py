from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ..api_models import (
    ChatMessageRequest,
    DraftBuiltinGenerateRequest,
    DraftBuiltinRegenerateRequest,
    DraftGenerateRequest,
    DraftRegenerateRequest,
)
from ..services import draft_chat_service, draft_service

router = APIRouter()


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
    return draft_chat_service.send_chat_message(payload.workspaceId, payload.message, payload.mode)


@router.post("/api/v1/chat/messages/stream")
async def chat_send_stream(payload: ChatMessageRequest) -> StreamingResponse:
    return StreamingResponse(
        draft_chat_service.send_chat_message_stream(
            payload.workspaceId, payload.message, payload.mode
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/chat/sessions/{sessionId}/messages")
async def chat_history(sessionId: str, cursor: str | None = Query(default=None)) -> dict[str, Any]:
    _ = cursor
    return draft_chat_service.get_chat_history(sessionId)
