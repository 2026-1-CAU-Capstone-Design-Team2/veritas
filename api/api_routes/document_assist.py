from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..api_models import DocumentAssistAnalyzeRequest, DocumentAssistChatRequest
from ..services import document_assist_service

router = APIRouter()


@router.post("/api/v1/document-assist/analyze")
def document_assist_analyze(payload: DocumentAssistAnalyzeRequest) -> dict[str, Any]:
    # Sync LLM call — plain `def` so the event loop stays free.
    return document_assist_service.analyze_document(payload.workspaceId, payload.text, payload.cursor)


@router.post("/api/v1/document-assist/chat/messages")
def document_assist_chat(payload: DocumentAssistChatRequest) -> dict[str, Any]:
    return document_assist_service.send_chat_message(payload.workspaceId, payload.message, payload.mode)


@router.post("/api/v1/document-assist/chat/messages/stream")
async def document_assist_chat_stream(payload: DocumentAssistChatRequest) -> StreamingResponse:
    return StreamingResponse(
        document_assist_service.send_chat_message_stream(
            payload.workspaceId, payload.message, payload.mode
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/document-assist/sessions/{sessionId}")
async def document_assist_snapshot(sessionId: str) -> dict[str, Any]:
    return document_assist_service.get_snapshot(sessionId)
