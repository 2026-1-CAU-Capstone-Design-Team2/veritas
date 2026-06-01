"""Unified proactive surface — /api/v1/proactive/{observe,generate,feedback}.

Native editor and external screen both flow through these routes. The
``editor_service.suggest_stream`` and ``screen_monitoring_service.record_feedback``
wrappers stay live for backward compatibility but internally call the same
service module behind these routes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ..api_models import (
    ProactiveFeedbackRequest,
    ProactiveGenerateRequest,
    ProactiveObserveRequest,
)
from ..services import proactive_service

router = APIRouter()


@router.post("/api/v1/proactive/observe")
def proactive_observe(payload: ProactiveObserveRequest) -> dict[str, Any]:
    return proactive_service.observe(payload)


@router.post("/api/v1/proactive/generate/stream")
def proactive_generate_stream(payload: ProactiveGenerateRequest) -> StreamingResponse:
    return StreamingResponse(
        proactive_service.generate_stream(payload),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/v1/proactive/feedback")
def proactive_feedback(payload: ProactiveFeedbackRequest) -> dict[str, Any]:
    return proactive_service.record_feedback(payload)


@router.get("/api/v1/proactive/explain/{decision_id}")
def proactive_explain(decision_id: str) -> dict[str, Any]:
    """Human-readable trace of one decision: mask, engage stats, top UCB
    scores, chosen context scope + snippet length. Use this when the
    JSONL feature vector is too dense to read by eye.
    """
    return proactive_service.explain_decision(decision_id)


@router.get("/api/v1/proactive/snapshot")
def proactive_snapshot(
    workspaceId: str | None = Query(default=None),
) -> dict[str, Any]:
    """Read-only "what is the bandit thinking right now?" probe.

    Returns π_min/π_max/discount, θ_hat, decay-weighted counts per arm, and the
    EMA-smoothed user stats. Use this when the bandit feels stuck so you can
    see exactly how low ``recent_negative_rate`` has gotten.
    """
    return proactive_service.get_snapshot(workspaceId)


@router.post("/api/v1/proactive/reset")
def proactive_reset(
    workspaceId: str | None = Query(default=None),
) -> dict[str, Any]:
    """Wipe the learned bandit state for the given workspace.

    History (decisions.jsonl / feedback.jsonl) is kept for audit; only
    ``policy_state.json`` and the in-memory caches are cleared. The next
    observe() starts from the same prior as a brand-new workspace.
    """
    return proactive_service.reset_policy(workspaceId)
