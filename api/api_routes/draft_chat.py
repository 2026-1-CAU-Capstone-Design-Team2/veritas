from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..api_models import ChatMessageRequest, DraftGenerateRequest, DraftRegenerateRequest
from ..services import draft_chat_service

router = APIRouter()


@router.post("/api/v1/draft/generate")
async def draft_generate(payload: DraftGenerateRequest) -> dict[str, Any]:
    return draft_chat_service.generate_draft(payload.workspaceId, payload.prompt)


@router.post("/api/v1/draft/{draftId}/regenerate")
async def draft_regenerate(draftId: str, payload: DraftRegenerateRequest) -> dict[str, Any]:
    return draft_chat_service.regenerate_draft(draftId, payload.prompt)


@router.post("/api/v1/chat/messages")
async def chat_send(payload: ChatMessageRequest) -> dict[str, str]:
    return draft_chat_service.send_chat_message(payload.workspaceId, payload.message)


@router.get("/api/v1/chat/sessions/{sessionId}/messages")
async def chat_history(sessionId: str, cursor: str | None = Query(default=None)) -> dict[str, Any]:
    _ = cursor
    return draft_chat_service.get_chat_history(sessionId)
