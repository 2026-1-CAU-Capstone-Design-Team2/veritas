"""FastAPI ↔ ProactiveOrchestrator adapter.

This is the *only* module that knows about both the HTTP/Pydantic shape and
the orchestrator's dataclass shape — every other piece of the proactive stack
stays UI-agnostic.

Responsibilities:

1. Translate ``ProactiveObserveRequest`` (camelCase fields, Pydantic) into
   the orchestrator's ``ProactiveObservation`` dataclass and back.
2. Frame the orchestrator's plain ``{type, ...}`` dicts as SSE bytes for the
   stream route.
3. Reach the runtime's per-workspace orchestrator and ensure the active
   workspace matches the request's ``workspaceId`` — same pattern as
   ``editor_service`` and ``screen_monitoring_service``.
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


# ---------------------------------------------------------------- helpers


def _ensure_workspace(workspace_id: str):
    """Make the requested workspace the active runtime workspace.

    Mirrors the existing pattern in ``screen_monitoring_service.get_events``:
    proactive observations must ground in the same RAG index the user thinks
    they're in, so we drive ``runtime.set_workspace`` per request. The runtime
    short-circuits when already on this workspace.
    """
    runtime = get_runtime()
    ws = str(workspace_id or "").strip()
    if ws and ws != "default":
        runtime.set_workspace(ws)
    return runtime


def _request_to_observation(payload: ProactiveObserveRequest) -> ProactiveObservation:
    # ``documentKey`` is the stable per-document handle the orchestrator's
    # rolling telemetry keys on. Fall back to documentId so callers that only
    # have one of them still group consistently.
    document_key = (payload.documentKey or payload.documentId or "").strip()
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
        metadata=dict(payload.metadata or {}),
    )


def _decision_to_response(decision: Any) -> dict[str, Any]:
    """Camel-case projection for HTTP. Mirrors the JSONL log shape but with
    the snake/camel mapping the frontend expects."""
    snap = getattr(decision, "feature_snapshot", None)
    selected = dict(decision.selected_context or {})
    # Drop full text from the HTTP response too — the frontend doesn't need
    # the body back (it sent it). Keep offsets / scope.
    selected.pop("text", None)
    selected.pop("prefix", None)
    selected.pop("suffix", None)
    selected.pop("focused_sentence", None)
    return {
        "decisionId": decision.decision_id,
        "surface": decision.surface,
        "workspaceId": decision.workspace_id,
        "documentKey": decision.document_key,
        "candidateSuggestionType": decision.candidate_suggestion_type,
        "availableSuggestionActions": list(decision.available_suggestion_actions),
        "engageAction": decision.engage_action,
        "shouldIntervene": bool(decision.should_intervene),
        "interventionProbability": float(decision.intervention_probability),
        "suggestionType": decision.suggestion_type,
        "contextScope": decision.context_scope,
        "renderMode": decision.render_mode,
        "selectedContext": selected,
        "policyInfo": dict(decision.policy_info or {}),
        "createdAt": decision.created_at,
        "expiresAt": decision.expires_at,
        "features": (
            {
                "engage": {
                    "names": list(snap.engage_feature_names),
                    "values": list(snap.engage_features),
                },
                "suggest": {
                    "names": list(snap.suggest_feature_names),
                    "values": list(snap.suggest_features),
                },
                "primitive": dict(snap.primitive),
            }
            if snap is not None
            else None
        ),
    }


def _feedback_to_response(record: Any) -> dict[str, Any]:
    return {
        "decisionId": record.decision_id,
        "surface": record.surface,
        "canonicalFeedback": record.feedback_action,
        "engageReward": record.engage_reward,
        "suggestionReward": record.suggestion_reward,
        "recordedAt": record.recorded_at,
        "metadata": dict(record.metadata or {}),
    }


# ---------------------------------------------------------------- entry points


def observe(payload: ProactiveObserveRequest) -> dict[str, Any]:
    runtime = _ensure_workspace(payload.workspaceId)
    orch = runtime.get_proactive_orchestrator()
    observation = _request_to_observation(payload)
    decision = orch.observe(observation)
    return _decision_to_response(decision)


def generate_stream(payload: ProactiveGenerateRequest) -> Iterator[bytes]:
    runtime = get_runtime()
    orch = runtime.get_proactive_orchestrator()
    decision_id = (payload.decisionId or "").strip()
    if not decision_id:
        raise HTTPException(status_code=422, detail="decisionId is required")
    bundle = orch.get_decision(decision_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"decision '{decision_id}' is not in cache (already expired or unknown)")
    decision = bundle["decision"]
    if not decision.should_intervene:
        # The orchestrator decided no_op — surface a minimal start/done pair
        # so SSE clients can detect "nothing to render" without polling.
        yield _sse({"type": "start", "decisionId": decision_id, "renderMode": "none"})
        yield _sse({"type": "done", "decisionId": decision_id})
        return
    for event in orch.stream_generation(decision_id):
        yield _sse(event)


def get_snapshot(workspace_id: str | None = None) -> dict[str, Any]:
    """Operator-facing "what does the bandit currently think?" probe.

    When ``workspace_id`` is set, we drive the active runtime to that
    workspace so the snapshot reflects the workspace the user is asking
    about — matching the per-workspace orchestrator binding model.
    """
    if workspace_id:
        _ensure_workspace(workspace_id)
    runtime = get_runtime()
    orch = runtime.get_proactive_orchestrator()
    return orch.snapshot()


def explain_decision(decision_id: str) -> dict[str, Any]:
    """Operator-facing readable trace of one decision.

    Returns a 404-style payload (HTTPException) when the decision is not in
    the in-memory cache — the JSONL log still has the raw data but this
    endpoint trades raw fidelity for readability.
    """
    runtime = get_runtime()
    orch = runtime.get_proactive_orchestrator()
    explanation = orch.explain(decision_id)
    if explanation is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"decision '{decision_id}' is not in the in-memory cache. "
                "Either it has rolled out (cache is bounded at 512 entries) "
                "or it was never produced by this orchestrator instance. "
                "The JSONL log in proactive_policy/decisions.jsonl still has it."
            ),
        )
    return explanation


def reset_policy(workspace_id: str | None = None) -> dict[str, Any]:
    """Drop the learned bandit state for one workspace.

    Used when the engage policy has locked to ``pi_min`` after a streak of
    rejects and the operator wants a fresh prior. History (decisions.jsonl /
    feedback.jsonl) is kept for audit; only ``policy_state.json`` and the
    in-memory caches are cleared.
    """
    if workspace_id:
        _ensure_workspace(workspace_id)
    runtime = get_runtime()
    orch = runtime.get_proactive_orchestrator()
    return orch.reset()


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
    return _feedback_to_response(record)


# ---------------------------------------------------------------- internal


def _sse(event: dict[str, Any]) -> bytes:
    """Serialize one orchestrator event to the SSE wire format we use across
    the editor / screen routes. Event name = ``type``; data = the dict itself.
    """
    name = str(event.get("type") or "message")
    body = json.dumps(event, ensure_ascii=False)
    return f"event: {name}\ndata: {body}\n\n".encode("utf-8")
