from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..api_common import new_id, utc_now_iso
from ..repositories import state_repository as repo
from .agent_runtime import get_runtime


def generate_draft(workspace_id: str, prompt: str) -> dict[str, Any]:
    prompt_text = prompt.strip()
    if not prompt_text:
        raise HTTPException(status_code=422, detail="prompt must not be empty")

    content = get_runtime().answer_chat(
        f"다음 요청에 맞춰 바로 사용할 수 있는 초안을 작성해 주세요.\n\n{prompt_text}",
        mode="research",
    )
    draft_id = new_id("dr")
    draft = {
        "draftId": draft_id,
        "workspaceId": workspace_id,
        "title": prompt_text[:80] or "Draft",
        "content": content,
        "prompt": prompt,
        "updatedAt": utc_now_iso(),
    }
    repo.save_draft(draft_id, draft)
    return {"draftId": draft_id, "title": draft["title"], "content": draft["content"]}


def regenerate_draft(draft_id: str, prompt: str) -> dict[str, Any]:
    draft = repo.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"draft '{draft_id}' not found")

    prompt_text = prompt.strip()
    if not prompt_text:
        raise HTTPException(status_code=422, detail="prompt must not be empty")

    draft["prompt"] = prompt
    draft["content"] = get_runtime().answer_chat(
        f"다음 요청에 맞춰 기존 초안을 다시 작성해 주세요.\n\n{prompt_text}",
        mode="research",
    )
    draft["updatedAt"] = utc_now_iso()
    return {"draftId": draft_id, "content": draft["content"]}


def send_chat_message(workspace_id: str, message: str, mode: str = "research") -> dict[str, str]:
    message_text = message.strip()
    if not message_text:
        raise HTTPException(status_code=422, detail="message must not be empty")

    message_id = new_id("msg")
    session_id = f"session_{workspace_id}"
    history = repo.get_or_create_chat_history(session_id)
    history.append({"role": "user", "text": message})
    assistant_text = get_runtime().answer_chat(message_text, mode)
    history.append({"role": "assistant", "text": assistant_text})
    return {"messageId": message_id, "assistant": assistant_text, "mode": mode}


def get_chat_history(session_id: str) -> dict[str, Any]:
    items = repo.get_chat_history(session_id)
    return {"items": items, "nextCursor": None}
