from __future__ import annotations

from typing import Any

from fastapi import HTTPException, UploadFile

from ..api_common import new_id
from ..repositories import state_repository as repo


def upload_feedback_files(files: list[UploadFile]) -> dict[str, list[dict[str, str]]]:
    items: list[dict[str, str]] = []
    for file in files:
        file_id = new_id("file")
        file_name = file.filename or "unknown"
        content_type = file.content_type or "application/octet-stream"
        repo.save_feedback_file(file_id, file_name, content_type)
        items.append({"fileId": file_id, "name": file_name})
    return {"items": items}


def analyze_feedback(file_ids: list[str]) -> dict[str, str]:
    missing_file_ids = [file_id for file_id in file_ids if repo.get_feedback_file(file_id) is None]
    if missing_file_ids:
        raise HTTPException(status_code=404, detail=f"file(s) not found: {', '.join(missing_file_ids)}")

    analysis_id = new_id("an")
    repo.save_feedback_session(analysis_id, file_ids, "completed")
    return {"analysisId": analysis_id, "status": "completed"}


def get_feedback_result(file_id: str) -> dict[str, Any]:
    file_info = repo.get_feedback_file(file_id)
    if file_info is None:
        raise HTTPException(status_code=404, detail=f"file '{file_id}' not found")

    return {
        "fileId": file_id,
        "charCount": 1200,
        "lineCount": 95,
        "weakPoints": ["출처 표기 부족"],
        "suggestions": ["근거 문장 추가"],
    }


def clear_feedback_session(session_id: str) -> None:
    repo.clear_feedback_session(session_id)
