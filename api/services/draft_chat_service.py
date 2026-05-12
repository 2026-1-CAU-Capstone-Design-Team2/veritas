from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..api_common import new_id, utc_now_iso
from ..repositories import state_repository as repo


def generate_draft(workspace_id: str, prompt: str) -> dict[str, Any]:
    workspace = repo.find_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"workspace '{workspace_id}' not found")

    draft_id = new_id("dr")
    draft = {
        "draftId": draft_id,
        "workspaceId": workspace_id,
        "title": f"{workspace['name']} 초안",
        "content": f"{prompt}\n\n이 초안은 '{workspace['name']}' 워크스페이스 기준으로 생성되었습니다.",
        "prompt": prompt,
        "updatedAt": utc_now_iso(),
    }
    repo.save_draft(draft_id, draft)
    return {"draftId": draft_id, "title": draft["title"], "content": draft["content"]}


def regenerate_draft(draft_id: str, prompt: str) -> dict[str, Any]:
    draft = repo.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"draft '{draft_id}' not found")

    draft["prompt"] = prompt
    draft["content"] = f"{prompt}\n\n재생성된 초안입니다."
    draft["updatedAt"] = utc_now_iso()
    return {"draftId": draft_id, "content": draft["content"]}


def send_chat_message(workspace_id: str, message: str, mode: str = "research") -> dict[str, str]:
    workspace = repo.find_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"workspace '{workspace_id}' not found")

    message_id = new_id("msg")
    session_id = f"session_{workspace_id}"
    history = repo.get_or_create_chat_history(session_id)
    history.append({"role": "user", "text": message})
    if mode == "rag":
        assistant_text = f"{workspace['name']}의 저장 문서와 검증 결과를 기준으로 근거를 찾아 답변하겠습니다."
    else:
        assistant_text = f"{workspace['name']} 기준으로 새 조사 방향, 확인할 출처, 정리 방식을 제안하겠습니다."
    history.append({"role": "assistant", "text": assistant_text})
    return {"messageId": message_id, "assistant": assistant_text, "mode": mode}


def get_chat_history(session_id: str) -> dict[str, Any]:
    items = repo.get_chat_history(session_id)
    return {"items": items, "nextCursor": None}
