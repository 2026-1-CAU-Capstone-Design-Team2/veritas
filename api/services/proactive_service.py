"""FastAPI ↔ ProactiveOrchestrator adapter (rule-based version).

Translates the camelCase Pydantic schemas to the orchestrator's plain dicts
and back, and frames SSE event dicts into the wire format. Every other
caller in the codebase should depend on this module rather than poking the
orchestrator directly.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

from fastapi import HTTPException

from services.proactive.models import ProactiveObservation

from ..api_models import (
    ProactiveFeedbackRequest,
    ProactiveGenerateRequest,
    ProactiveObserveRequest,
)
from .agent_runtime import get_runtime


def _ensure_workspace(workspace_id: str):
    runtime = get_runtime()
    ws = str(workspace_id or "").strip()
    if ws and ws != "default":
        runtime.set_workspace(ws)
    return runtime


def _request_to_observation(payload: ProactiveObserveRequest) -> ProactiveObservation:
    document_key = (payload.documentKey or payload.documentId or "").strip()
    metadata = dict(payload.metadata or {})
    return ProactiveObservation(
        surface=payload.surface,  # type: ignore[arg-type]
        workspace_id=payload.workspaceId,
        document_key=document_key,
        document_id=payload.documentId or "",
        source_app=payload.sourceApp or "",
        window_title=payload.windowTitle or "",
        text=payload.text or "",
        cursor_index=payload.cursor,
        prefix=payload.prefix or "",
        suffix=payload.suffix or "",
        current_sentence=payload.currentSentence or "",
        current_paragraph=payload.currentParagraph or "",
        previous_paragraph=payload.previousParagraph or "",
        changed_text=payload.changedText or "",
        confidence=float(payload.confidence or 0.0),
        captured_at="",
        metadata=metadata,
    )


# ---------------------------------------------------------------- observe


def observe(payload: ProactiveObserveRequest) -> dict[str, Any]:
    runtime = _ensure_workspace(payload.workspaceId)
    orch = runtime.get_proactive_orchestrator()
    observation = _request_to_observation(payload)
    result = orch.observe(observation)

    decision_id = result["decision_id"]
    threshold = float(result.get("threshold") or 0.0)
    candidate_count = int(result.get("candidate_count") or 0)
    anchor = result.get("anchor")

    base = {
        "decisionId": decision_id,
        "workspaceId": payload.workspaceId,
        "surface": payload.surface,
        "candidateCount": candidate_count,
        "threshold": threshold,
        "anchor": {
            "anchorId": getattr(anchor, "anchor_id", "") if anchor else "",
            "confidence": float(getattr(anchor, "confidence", 0.0) or 0.0) if anchor else 0.0,
            "source": getattr(anchor, "source", "unknown") if anchor else "unknown",
        },
    }

    if result["prediction"] == "task":
        task = result["task"]
        base.update(
            {
                "prediction": "task",
                "shouldIntervene": True,
                "task": {
                    "taskType": task.task_type,
                    "targetAnchorId": task.target_anchor_id,
                    "contextScope": task.context_scope,
                    "renderMode": task.render_mode,
                    "reason": task.reason,
                    "evaluatorScore": task.evaluator_score,
                },
            }
        )
    else:
        null_pred = result["null"]
        base.update(
            {
                "prediction": "null",
                "shouldIntervene": False,
                "reason": null_pred.reason,
                "gateReasons": list(null_pred.gate_reasons),
            }
        )
    return base


# ---------------------------------------------------------------- generate


def generate_stream(payload: ProactiveGenerateRequest) -> Iterator[bytes]:
    runtime = get_runtime()
    orch = runtime.get_proactive_orchestrator()
    decision_id = (payload.decisionId or "").strip()
    if not decision_id:
        raise HTTPException(status_code=422, detail="decisionId is required")
    bundle = orch.get_decision(decision_id)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail=f"decision '{decision_id}' is not in cache",
        )
    prediction = bundle["prediction"]
    # NullPrediction → empty start/done so SSE clients can detect "no render".
    from services.proactive.proposal_models import is_task

    if not is_task(prediction):
        yield _sse({"type": "start", "decisionId": decision_id, "renderMode": "none"})
        yield _sse({"type": "done", "decisionId": decision_id})
        return
    for event in orch.stream_generation(decision_id):
        yield _sse(event)


# ---------------------------------------------------------------- feedback


def record_feedback(payload: ProactiveFeedbackRequest) -> dict[str, Any]:
    runtime = get_runtime()
    orch = runtime.get_proactive_orchestrator()
    decision_id = (payload.decisionId or "").strip()
    if not decision_id:
        raise HTTPException(status_code=422, detail="decisionId is required")
    action = (payload.action or "").strip()
    if not action:
        raise HTTPException(status_code=422, detail="action is required")
    record = orch.record_feedback(
        decision_id=decision_id,
        raw_action=action,
        metadata=dict(payload.metadata or {}),
    )
    return {
        "decisionId": record.get("decision_id"),
        "surface": record.get("surface"),
        "canonicalFeedback": record.get("canonical_feedback"),
        "taskType": record.get("task_type"),
        "adaptationChanges": dict(record.get("adaptation_changes") or {}),
    }


# ---------------------------------------------------------------- explain / snapshot / reset


def explain_decision(decision_id: str) -> dict[str, Any]:
    runtime = get_runtime()
    orch = runtime.get_proactive_orchestrator()
    explanation = orch.explain(decision_id)
    if explanation is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"decision '{decision_id}' is not in the in-memory cache. "
                "The decisions.jsonl log still has the raw record."
            ),
        )
    return explanation


def get_snapshot(workspace_id: str | None = None) -> dict[str, Any]:
    if workspace_id:
        _ensure_workspace(workspace_id)
    runtime = get_runtime()
    orch = runtime.get_proactive_orchestrator()
    return orch.snapshot()


def reset_policy(workspace_id: str | None = None) -> dict[str, Any]:
    if workspace_id:
        _ensure_workspace(workspace_id)
    runtime = get_runtime()
    orch = runtime.get_proactive_orchestrator()
    return orch.reset()


# ---------------------------------------------------------------- internal


def _sse(event: dict[str, Any]) -> bytes:
    name = str(event.get("type") or "message")
    body = json.dumps(event, ensure_ascii=False)
    return f"event: {name}\ndata: {body}\n\n".encode("utf-8")
