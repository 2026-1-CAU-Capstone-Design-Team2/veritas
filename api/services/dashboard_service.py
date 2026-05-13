from __future__ import annotations

from typing import Any

from ..repositories import state_repository as repo
from . import workspaces_service


def get_dashboard_summary(workspace_id: str | None) -> dict[str, Any]:
    workspace_items = workspaces_service.list_workspaces(None)["items"]
    summary = repo.get_dashboard_summary()
    recent_workspaces = workspace_items[:5]
    recent_documents = repo.get_recent_documents(5)
    summary = {
        **summary,
        "processed_docs": summary.get("processedDocs", 0),
        "validated_workspaces": len([item for item in workspace_items if item.get("status") == "completed"]),
        "feedback_rate": summary.get("feedbackCompletionRate", 0),
        "recent_workspaces": [
            {
                "workspaceId": item["workspaceId"],
                "name": item["name"],
                "last_worked_at": item.get("lastWorkedAt"),
                "lastWorkedAt": item.get("lastWorkedAt"),
            }
            for item in recent_workspaces
        ],
        "recent_activities": [
            {
                "action": item.get("type", "document"),
                "description": item.get("name", ""),
                "created_at": item.get("createdAt"),
                "createdAt": item.get("createdAt"),
            }
            for item in recent_documents
        ],
    }
    if workspace_id:
        summary["selectedWorkspace"] = workspace_id
    return summary


def get_recent_workspaces(limit: int) -> dict[str, list[dict[str, Any]]]:
    items = workspaces_service.list_workspaces(None)["items"][:limit]
    return {"items": [{"workspaceId": item["workspaceId"], "name": item["name"], "lastWorkedAt": item.get("lastWorkedAt")} for item in items]}


def get_recent_documents(limit: int) -> dict[str, list[dict[str, Any]]]:
    return {"items": repo.get_recent_documents(limit)}
