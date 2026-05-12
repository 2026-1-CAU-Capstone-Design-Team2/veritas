from __future__ import annotations

from typing import Any

from ..repositories import state_repository as repo
from . import workspaces_service


def get_bootstrap() -> dict[str, Any]:
    ui_state = repo.get_ui_state()
    workspaces = workspaces_service.list_workspaces(None)["items"]
    return {
        "defaultRoute": ui_state.get("route", "dashboard"),
        "menus": [
            "dashboard",
            "research",
            "verify",
            "draft",
            "document_assist",
            "write",
            "document",
            "feedback",
            "settings",
        ],
        "workspaces": workspaces,
        "currentWorkspaceId": repo.get_current_workspace_id(),
        "settings": repo.get_settings(),
    }


def navigate(route: str) -> dict[str, Any]:
    repo.set_ui_route(route)
    return {"route": route, "updated": True}


def sync_workspace(workspace_id: str, workspace_name: str) -> dict[str, Any]:
    repo.set_ui_workspace(workspace_id, workspace_name)
    return {"workspaceId": workspace_id, "synced": True}


def queue_toast(level: str, message: str) -> dict[str, bool]:
    repo.set_ui_toast(level, message)
    return {"queued": True}


def show_prediction_popup(prediction_id: str, text: str, confidence: float, anchor: str) -> dict[str, Any]:
    repo.set_ui_prediction_popup(
        {
            "visible": True,
            "predictionId": prediction_id,
            "text": text,
            "confidence": confidence,
            "anchor": anchor,
        }
    )
    return {"predictionId": prediction_id, "visible": True}


def hide_prediction_popup(prediction_id: str, reason: str) -> dict[str, Any]:
    repo.set_ui_prediction_popup({"visible": False, "predictionId": prediction_id, "reason": reason})
    return {"predictionId": prediction_id, "visible": False}


def apply_prediction_popup(prediction_id: str) -> dict[str, Any]:
    repo.set_ui_prediction_popup({"visible": False, "predictionId": prediction_id, "applied": True})
    return {"predictionId": prediction_id, "applied": True}


def get_ui_snapshot(route: str | None) -> dict[str, Any]:
    ui_state = repo.get_ui_state()
    return {
        "route": route or ui_state.get("route", "dashboard"),
        "workspaceId": ui_state.get("workspaceId", repo.get_current_workspace_id()),
        "workspaceName": ui_state.get("workspaceName"),
        "predictionPopup": ui_state.get("predictionPopup", {"visible": False}),
        "toast": ui_state.get("toast"),
    }
