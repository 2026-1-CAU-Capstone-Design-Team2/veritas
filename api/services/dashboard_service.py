from __future__ import annotations

from typing import Any

from ..repositories import state_repository as repo


def get_dashboard_summary(workspace_id: str | None) -> dict[str, Any]:
    summary = repo.get_dashboard_summary()
    recent_workspaces = repo.get_recent_workspaces(5)
    recent_documents = repo.get_recent_documents(5)
    summary = {
        **summary,
        "processed_docs": summary.get("processedDocs", 0),
        "validated_workspaces": summary.get("verifiedWorkspaces", 0),
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
    items = repo.get_recent_workspaces(limit)
    return {"items": [{"workspaceId": item["workspaceId"], "name": item["name"], "lastWorkedAt": item["lastWorkedAt"]} for item in items]}


def get_recent_documents(limit: int) -> dict[str, list[dict[str, Any]]]:
    return {"items": repo.get_recent_documents(limit)}
