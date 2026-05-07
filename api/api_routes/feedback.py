from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile

from ..api_models import FeedbackAnalyzeRequest
from ..services import feedback_service

router = APIRouter()


@router.post("/api/v1/feedback/files", status_code=201)
async def feedback_upload(
    files: list[UploadFile] | None = File(default=None),
    files_bracket: list[UploadFile] | None = File(default=None, alias="files[]"),
) -> dict[str, list[dict[str, str]]]:
    selected_files = files or files_bracket or []
    if not selected_files:
        raise HTTPException(status_code=400, detail="at least one feedback file is required")
    return feedback_service.upload_feedback_files(selected_files)


@router.post("/api/v1/feedback/analyze")
async def feedback_analyze(payload: FeedbackAnalyzeRequest) -> dict[str, str]:
    return feedback_service.analyze_feedback(payload.fileIds)


@router.get("/api/v1/feedback/results/{fileId}")
async def feedback_result(fileId: str) -> dict[str, Any]:
    return feedback_service.get_feedback_result(fileId)


@router.delete("/api/v1/feedback/session", status_code=204)
async def feedback_clear(sessionId: str = Query(...)) -> Response:
    feedback_service.clear_feedback_session(sessionId)
    return Response(status_code=204)
