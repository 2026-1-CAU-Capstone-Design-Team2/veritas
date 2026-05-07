from __future__ import annotations

from typing import Any

from fastapi import HTTPException, UploadFile

from ..api_common import new_id
from ..repositories import state_repository as repo


async def upload_feedback_files(files: list[UploadFile]) -> dict[str, list[dict[str, str]]]:
    items: list[dict[str, str]] = []
    for file in files:
        file_id = new_id("file")
        file_name = file.filename or "unknown"
        content_type = file.content_type or "application/octet-stream"
        raw = await file.read()
        text = raw.decode("utf-8", errors="ignore") if raw else ""
        repo.save_feedback_file(file_id, file_name, content_type, text)
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
        "name": file_info["name"],
        "charCount": len(file_info.get("text", "")),
        "lineCount": len(file_info.get("text", "").splitlines()),
        "weakPoints": _build_weak_points(file_info.get("text", "")),
        "suggestions": [
            "핵심 주장마다 근거 문장 또는 출처를 한 줄 이상 추가하세요.",
            "모호한 표현을 수치나 기준으로 구체화하세요.",
            "결론 단락에 실행 항목 2~3개를 명시하세요.",
        ],
    }


def clear_feedback_session(session_id: str) -> None:
    repo.clear_feedback_session(session_id)


def _build_weak_points(text: str) -> list[str]:
    weak_points: list[str] = []
    if len(text) < 280:
        weak_points.append("문서 길이가 짧아 핵심 근거가 충분하지 않을 수 있습니다.")
    if "출처" not in text and "source" not in text.lower():
        weak_points.append("출처 표기가 보이지 않아 신뢰도 검증이 어렵습니다.")
    if "TODO" in text or "추후" in text:
        weak_points.append("미완료 표기가 포함되어 최종본 품질이 낮아질 수 있습니다.")
    return weak_points or ["치명적 문제는 감지되지 않았습니다. 문장 간 연결성과 근거 명확성만 점검하세요."]
