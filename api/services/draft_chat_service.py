from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from fastapi import HTTPException

from db import activity_repository as activity

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
    activity.log_activity(workspace_id, "draft_created", f"초안 생성 · {draft['title']}")
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
    history = _get_workspace_chat_history(workspace_id, session_id)
    history.append({"role": "user", "text": message})
    runtime = get_runtime()
    runtime.set_workspace(workspace_id)
    assistant_text = runtime.answer_chat_selection(message_text, mode)
    history.append({"role": "assistant", "text": assistant_text})
    _save_workspace_chat_history(workspace_id, history)
    return {"messageId": message_id, "assistant": assistant_text, "mode": mode}


def send_chat_message_stream(
    workspace_id: str,
    message: str,
    mode: str = "research",
    doc_text: str = "",
    source: str = "chat",
) -> Iterator[bytes]:
    """Stream the assistant response as SSE events.

    Events:
        event: start  data: {"messageId": "...", "workspaceId": "..."}
        event: delta  data: {"text": "<chunk>"}
        event: done   data: {"messageId": "...", "assistant": "<full>", "mode": "..."}
        event: error  data: {"error": "..."}

    ``doc_text`` (the editor's open document) rides along as additive agent
    context; ``source`` tags both turns so the main chat page and the editor's
    문서 대화 render one shared log while marking which surface spoke.
    """
    message_text = message.strip()
    if not message_text:
        yield _sse("error", {"error": "message must not be empty"})
        return

    doc_context = (doc_text or "")[:4000].strip()
    message_id = new_id("msg")
    session_id = f"session_{workspace_id}"
    history = _get_workspace_chat_history(workspace_id, session_id)
    history.append({"role": "user", "text": message, "source": source})

    runtime = get_runtime()
    runtime.set_workspace(workspace_id)

    yield _sse(
        "start",
        {"messageId": message_id, "workspaceId": workspace_id, "mode": mode},
    )

    collected: list[str] = []
    try:
        for chunk in runtime.answer_chat_selection_iter(
            message_text, mode, doc_context=doc_context
        ):
            if not chunk:
                continue
            collected.append(chunk)
            yield _sse("delta", {"text": chunk})
    except Exception as e:
        error_text = f"[chat][error] {e}"
        collected.append(error_text)
        yield _sse("error", {"error": str(e)})

    assistant_text = "".join(collected)
    history.append({"role": "assistant", "text": assistant_text, "source": source})
    _save_workspace_chat_history(workspace_id, history)
    yield _sse(
        "done",
        {
            "messageId": message_id,
            "assistant": assistant_text,
            "mode": mode,
        },
    )


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def get_chat_history(session_id: str) -> dict[str, Any]:
    workspace_id = session_id.removeprefix("session_")
    items = _get_workspace_chat_history(workspace_id, session_id)
    return {"items": items, "nextCursor": None}


def _get_workspace_chat_history(workspace_id: str, session_id: str) -> list[dict[str, Any]]:
    history = repo.get_or_create_chat_history(session_id)
    if history:
        return history

    path = _chat_history_path(workspace_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("items", payload) if isinstance(payload, dict) else payload
    except Exception:
        items = []
    if isinstance(items, list):
        history.extend([item for item in items if isinstance(item, dict)])
    return history


def _save_workspace_chat_history(workspace_id: str, history: list[dict[str, Any]]) -> None:
    path = _chat_history_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"items": history, "updatedAt": utc_now_iso()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _chat_history_path(workspace_id: str) -> Path:
    root = Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()
    workspace_dir = root / workspace_id if workspace_id != "default" else root / "api"
    return workspace_dir / "chat_history.json"
