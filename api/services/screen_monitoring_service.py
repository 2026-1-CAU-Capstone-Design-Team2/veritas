from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..api_models import ProactiveFeedbackRequest
from . import proactive_service
from .agent_runtime import get_runtime


# Legacy reward shaping for the (non-proactive) intervention selection policy.
# The proactive pipeline owns its own canonical reward table — see
# ``services.proactive.reward.CANONICAL_REWARD``. This map stays here only for
# screen events that originated *before* the proactive bandit was wired in
# (events without a ``decisionId`` come through the old scheduler path).
SCREEN_FEEDBACK_REWARDS: dict[str, float] = {
    "like": 1.0,
    "copy": 0.3,
    "dislike": -1.0,
}


def start_monitoring(workspace_id: str | None) -> dict[str, Any]:
    runtime = get_runtime()
    if workspace_id:
        runtime.set_workspace(workspace_id)
    return runtime.start_screen_monitoring()


def stop_monitoring() -> dict[str, Any]:
    return get_runtime().stop_screen_monitoring()


def get_status() -> dict[str, Any]:
    return get_runtime().screen_monitoring_status()


def get_events(since: int, limit: int, workspace_id: str | None = None) -> dict[str, Any]:
    runtime = get_runtime()
    # Continuous workspace sync (the screen path's equivalent of chat calling
    # set_workspace per message): keep the screen runtime — and thus its
    # rag_service — bound to whatever workspace the user is currently in, so
    # interventions never ground in a stale/other workspace's knowledge base.
    # set_workspace is a no-op when already on this workspace and only rebuilds
    # (+ restarts monitoring) on an actual change.
    ws = str(workspace_id or "").strip()
    if ws and ws != "default":
        runtime.set_workspace(ws)
    return runtime.get_screen_events_since(since=since, limit=limit)


def record_feedback(event_id: str, intervention_type: str, action: str) -> dict[str, Any]:
    """Record one feedback action for an external-screen intervention.

    Dual-path behavior:

    1. **Proactive decisions** — when ``event_id`` starts with the proactive
       prefix (``pd_``) we route the feedback into the proactive bandit's
       canonical pipeline. Any of ``copy / red_reject / retry / timeout / like
       / dislike`` is accepted; the canonical mapper in
       ``services.proactive.reward`` collapses legacy aliases.
    2. **Legacy interventions** — pre-proactive events still come in with the
       old screen-monitor IDs; those go through the original
       ``record_screen_feedback`` path with the legacy reward table.

    This keeps the HTTP contract backwards-compatible (older Qt frontends keep
    sending ``like / copy / dislike``) while routing new flows through the
    bandit's unified rewards.
    """
    event_id = str(event_id or "").strip()
    if not event_id:
        raise HTTPException(status_code=422, detail="eventId is required")
    raw_action = str(action or "").strip().lower()
    if not raw_action:
        raise HTTPException(status_code=422, detail="action is required")

    if event_id.startswith("pd_"):
        return proactive_service.record_feedback(
            ProactiveFeedbackRequest(
                decisionId=event_id,
                action=raw_action,
                metadata={
                    "interventionType": str(intervention_type or "").strip() or "none",
                    "source": "screen_monitoring_legacy_route",
                },
            )
        )

    if raw_action not in SCREEN_FEEDBACK_REWARDS:
        raise HTTPException(
            status_code=422,
            detail=f"action must be one of {sorted(SCREEN_FEEDBACK_REWARDS)} for legacy events",
        )
    return get_runtime().record_screen_feedback(
        event_id=event_id,
        intervention_type=str(intervention_type or "").strip() or "none",
        action=raw_action,
        reward=SCREEN_FEEDBACK_REWARDS[raw_action],
    )
