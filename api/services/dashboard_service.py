from __future__ import annotations

from typing import Any

from db import dashboard_repository as home_repo

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


def get_home_summary() -> dict[str, Any]:
    """Home dashboard payload for the desktop ``DashboardPage``.

    Moved here from the legacy ``db.dashboard_service`` so the frontend reaches
    it over HTTP (``GET /api/v1/dashboard/home``) instead of importing the db
    layer directly. The underlying SQLite/draft reads stay in the db-layer
    repository ``db.dashboard_repository`` (parallel to ``db.activity_repository``).
    The response shape is unchanged so the existing page renders identically.
    """
    summary = home_repo.get_dashboard_summary()
    total_docs = summary["total_docs"]
    feedback_completed_docs = summary["feedback_completed_docs"]
    feedback_rate = (
        0 if total_docs == 0 else round((feedback_completed_docs / total_docs) * 100)
    )
    return {
        "processed_docs": summary["processed_docs"],
        "validated_workspaces": summary["validated_workspaces"],
        "feedback_rate": feedback_rate,
        "recent_workspaces": home_repo.get_recent_workspaces(limit=5),
        "recent_activities": home_repo.get_recent_activities(limit=5),
        "recent_drafts": home_repo.get_recent_drafts(limit=5),
    }


def rename_workspace(workspace_id: str, name: str) -> dict[str, Any]:
    """Rename a workspace from the dashboard. Replaces the page's former direct
    SQLite UPDATE; the SQL stays in ``db.dashboard_repository``."""
    name = str(name or "").strip()
    if not name:
        return {"workspaceId": workspace_id, "updated": False, "reason": "empty_name"}
    updated = home_repo.rename_workspace(workspace_id, name)
    return {"workspaceId": workspace_id, "name": name, "updated": bool(updated)}
