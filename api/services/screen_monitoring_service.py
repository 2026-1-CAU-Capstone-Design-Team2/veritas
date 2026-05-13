from __future__ import annotations

from typing import Any

from .agent_runtime import get_runtime


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
