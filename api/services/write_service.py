from __future__ import annotations

import asyncio
import json
from typing import Any

from ..api_common import new_id
from ..repositories import state_repository as repo


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
    payloads = [
        {"predictionId": new_id("pr"), "text": "다음 문단 제안: 핵심 근거를 먼저 제시하세요.", "confidence": 0.84},
        {"predictionId": new_id("pr"), "text": "보완 제안: 수치를 한 문장 더 추가하면 설득력이 높아집니다.", "confidence": 0.79},
    ]
    for payload in payloads:
        event = {
            "sessionId": session_id,
            "workspaceId": workspace_id,
            **payload,
        }
        yield f"event: prediction\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0)


def ack_prediction(prediction_id: str, action: str) -> dict[str, str]:
    repo.save_prediction_state(prediction_id, {"action": action})
    return {"predictionId": prediction_id, "action": action}
