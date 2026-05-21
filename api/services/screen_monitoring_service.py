from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .agent_runtime import get_runtime


# Reward shaping for the intervention selection policy, kept in one place so it
# stays tunable without touching the UI or the store. Explicit reactions
# dominate; copy is a weak positive (the user acted on it without endorsing it).
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


def get_events(since: int, limit: int) -> dict[str, Any]:
    return get_runtime().get_screen_events_since(since=since, limit=limit)


def record_feedback(event_id: str, intervention_type: str, action: str) -> dict[str, Any]:
    event_id = str(event_id or "").strip()
    if not event_id:
        raise HTTPException(status_code=422, detail="eventId is required")
    action = str(action or "").strip().lower()
    if action not in SCREEN_FEEDBACK_REWARDS:
        raise HTTPException(
            status_code=422,
            detail=f"action must be one of {sorted(SCREEN_FEEDBACK_REWARDS)}",
        )
    return get_runtime().record_screen_feedback(
        event_id=event_id,
        intervention_type=str(intervention_type or "").strip() or "none",
        action=action,
        reward=SCREEN_FEEDBACK_REWARDS[action],
    )
