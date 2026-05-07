from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..api_common import new_id, utc_now_iso
from ..repositories import state_repository as repo


def analyze_document(workspace_id: str, text: str, cursor: int | None) -> dict[str, Any]:
    workspace = repo.find_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"workspace '{workspace_id}' not found")

    stripped = text.strip()
    warnings: list[str] = []
    recommendations: list[str] = []

    if len(stripped) < 120:
        warnings.append("입력 문서가 짧아 근거 충분성을 판단하기 어렵습니다.")
    if "출처" not in stripped and "source" not in stripped.lower():
        warnings.append("출처 표기가 보이지 않아 검증 화면과의 연결성이 약합니다.")
    if "TODO" in stripped or "추후" in stripped:
        warnings.append("미완료 표기가 포함되어 최종 문서 전에 정리가 필요합니다.")

    recommendations.extend(
        [
            "핵심 주장을 첫 문단 앞쪽에 배치하세요.",
            "수치나 정책명 뒤에는 검증 가능한 근거 문장을 붙이세요.",
            "결론에는 다음 행동을 2~3개 항목으로 정리하세요.",
        ]
    )

    session_id = new_id("da")
    payload = {
        "sessionId": session_id,
        "workspaceId": workspace_id,
        "workspaceName": workspace["name"],
        "cursor": cursor,
        "analysis": "문서 분석 텍스트, 추천 문장, 경고, 수정 제안을 mock 결과로 생성했습니다.",
        "warnings": warnings or ["치명적 경고는 감지되지 않았습니다."],
        "recommendations": recommendations,
        "updatedAt": utc_now_iso(),
    }
    repo.save_document_assist_session(session_id, payload)
    return payload


def send_chat_message(workspace_id: str, message: str) -> dict[str, Any]:
    workspace = repo.find_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"workspace '{workspace_id}' not found")

    lowered = message.lower()
    if "보고" in lowered or "브리프" in lowered:
        reply = "경영진 보고용으로 개요, 핵심 리스크, 실행 권고 순서의 초안을 정리하겠습니다."
    elif "메일" in lowered or "안내" in lowered or "공지" in lowered:
        reply = "수신자 중심의 짧고 명확한 문장으로 바로 보낼 수 있는 초안을 작성하겠습니다."
    else:
        reply = "선택한 검증 완료 워크스페이스의 맥락을 반영해 자연스럽게 이어서 작성하겠습니다."

    return {
        "messageId": new_id("msg"),
        "workspaceId": workspace_id,
        "workspaceName": workspace["name"],
        "reply": reply,
    }


def get_snapshot(session_id: str) -> dict[str, Any]:
    session = repo.get_document_assist_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"document assist session '{session_id}' not found")
    return session
