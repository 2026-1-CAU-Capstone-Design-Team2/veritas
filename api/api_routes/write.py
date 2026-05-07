from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ..api_models import PredictionAckRequest, TypingContextRequest
from ..services import write_service

router = APIRouter()


@router.post("/api/v1/write/typing-context", status_code=202)
async def typing_context_push(payload: TypingContextRequest) -> dict[str, Any]:
    return write_service.push_typing_context(
        payload.sessionId,
        payload.workspaceId,
        payload.cursor,
        payload.prefix,
        payload.suffix,
    )


@router.get("/api/v1/write/predictions/stream")
async def prediction_subscribe(sessionId: str = Query(...), workspaceId: str = Query(...)) -> StreamingResponse:
    return StreamingResponse(
        write_service.prediction_event_stream(sessionId, workspaceId),
        media_type="text/event-stream",
    )


@router.post("/api/v1/write/predictions/{predictionId}/ack")
async def prediction_ack(predictionId: str, payload: PredictionAckRequest) -> dict[str, str]:
    return write_service.ack_prediction(predictionId, payload.action)
