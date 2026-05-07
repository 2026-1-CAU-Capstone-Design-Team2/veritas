from __future__ import annotations

from typing import Any

from ..repositories import state_repository as repo


def get_dashboard_summary(workspace_id: str | None) -> dict[str, Any]:
    summary = repo.get_dashboard_summary()
    if workspace_id:
        summary["selectedWorkspace"] = workspace_id
    return summary


def get_recent_workspaces(limit: int) -> dict[str, list[dict[str, Any]]]:
    items = repo.get_recent_workspaces(limit)
    return {"items": [{"workspaceId": item["workspaceId"], "name": item["name"], "lastWorkedAt": item["lastWorkedAt"]} for item in items]}


def get_recent_documents(limit: int) -> dict[str, list[dict[str, Any]]]:
    return {"items": repo.get_recent_documents(limit)}
