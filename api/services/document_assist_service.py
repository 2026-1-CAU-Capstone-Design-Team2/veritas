from __future__ import annotations

from typing import Any, Iterator

from fastapi import HTTPException

from ..api_common import new_id, utc_now_iso
from ..repositories import state_repository as repo
from . import draft_chat_service
from .agent_runtime import get_runtime


def analyze_document(workspace_id: str, text: str, cursor: int | None) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail="text must not be empty")

    prompt = (
        "다음 문서를 검토하고 사용자가 바로 반영할 수 있는 분석과 수정 제안을 작성해 주세요. "
        "출처가 부족한 주장, 논리 흐름, 문장 명확성을 중심으로 답하세요.\n\n"
        f"cursor: {cursor}\n\n"
        f"{stripped}"
    )
    analysis = get_runtime().answer_chat(prompt, mode="research")
    session_id = new_id("da")
    payload = {
        "sessionId": session_id,
        "workspaceId": workspace_id,
        "workspaceName": _workspace_name(workspace_id),
        "cursor": cursor,
        "analysis": analysis,
        "warnings": [],
        "recommendations": [analysis],
        "suggestions": [
            {
                "category": "analysis",
                "text": analysis,
                "tone": "idle",
            }
        ],
        "updatedAt": utc_now_iso(),
    }
    repo.save_document_assist_session(session_id, payload)
    return payload


def send_chat_message(workspace_id: str, message: str, mode: str = "research") -> dict[str, Any]:
    """Route document-assist chat through the same persisted history as the
    main chat panel so both views stay in sync.
    """
    result = draft_chat_service.send_chat_message(workspace_id, message, mode)
    return {
        "messageId": result.get("messageId"),
        "workspaceId": workspace_id,
        "workspaceName": _workspace_name(workspace_id),
        "mode": result.get("mode") or mode,
        "reply": result.get("assistant") or "",
    }


def send_chat_message_stream(
    workspace_id: str,
    message: str,
    mode: str = "research",
) -> Iterator[bytes]:
    """SSE alias that reuses the chat streaming pipeline."""
    return draft_chat_service.send_chat_message_stream(workspace_id, message, mode)


def get_snapshot(session_id: str) -> dict[str, Any]:
    session = repo.get_document_assist_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"document assist session '{session_id}' not found")
    return session


def _workspace_name(workspace_id: str) -> str:
    workspace = repo.find_workspace(workspace_id)
    return str(workspace.get("name") or workspace_id) if workspace else workspace_id
