from __future__ import annotations

import json
from typing import Any

from ..api_common import new_id
from ..repositories import state_repository as repo
from .agent_runtime import get_runtime


def push_typing_context(
    session_id: str,
    workspace_id: str,
    cursor: int,
    prefix: str,
    suffix: str,
) -> dict[str, Any]:
    trace_id = new_id("tr")
    repo.save_prediction_state(
        session_id,
        {
            "workspaceId": workspace_id,
            "cursor": cursor,
            "prefix": prefix,
            "suffix": suffix,
            "traceId": trace_id,
        },
    )
    return {"accepted": True, "traceId": trace_id}


async def prediction_event_stream(session_id: str, workspace_id: str) -> Any:
    context = repo.get_prediction_state(session_id) or {}
    prefix = str(context.get("prefix") or "").strip()
    suffix = str(context.get("suffix") or "").strip()
    if not prefix and not suffix:
        return

    prompt = (
        "다음 작성 문맥에 자연스럽게 이어질 짧은 문장 또는 문단 하나만 제안해 주세요. "
        "설명 없이 삽입할 텍스트만 답하세요.\n\n"
        f"prefix:\n{prefix}\n\nsuffix:\n{suffix}"
    )
    text = get_runtime().answer_chat(prompt, mode="research").strip()
    if not text:
        return

    event = {
        "sessionId": session_id,
        "workspaceId": workspace_id,
        "predictionId": new_id("pr"),
        "text": text,
        "confidence": 0.0,
    }
    yield f"event: prediction\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def ack_prediction(prediction_id: str, action: str) -> dict[str, str]:
    repo.save_prediction_state(prediction_id, {"action": action})
    return {"predictionId": prediction_id, "action": action}
