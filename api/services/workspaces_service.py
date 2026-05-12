from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..repositories import state_repository as repo


def list_workspaces(status: str | None) -> dict[str, list[dict[str, Any]]]:
    items = repo.list_workspaces()
    if status:
        items = [item for item in items if item["status"] == status]
    return {
        "items": [
            {
                "workspaceId": item["workspaceId"],
                "name": item["name"],
                "detail": item["detail"],
                "status": item.get("status"),
                "lastWorkedAt": item.get("lastWorkedAt"),
            }
            for item in items
        ]
    }


def switch_workspace(workspace_id: str) -> dict[str, str]:
    workspace = repo.find_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"workspace '{workspace_id}' not found")

    repo.set_current_workspace(workspace["workspaceId"])
    return {"workspaceId": workspace["workspaceId"], "name": workspace["name"]}
