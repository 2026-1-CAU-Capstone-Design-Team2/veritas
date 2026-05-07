from __future__ import annotations

from typing import Any

from ..api_common import STATE


def get_dashboard_summary() -> dict[str, int]:
    return dict(STATE["dashboard_summary"])


def get_recent_workspaces(limit: int) -> list[dict[str, Any]]:
    return STATE["workspaces"][:limit]


def get_recent_documents(limit: int) -> list[dict[str, Any]]:
    return STATE["recent_documents"][:limit]


def list_workspaces() -> list[dict[str, Any]]:
    return STATE["workspaces"]


def find_workspace(workspace_id: str) -> dict[str, Any] | None:
    return next((item for item in STATE["workspaces"] if item["workspaceId"] == workspace_id), None)


def set_current_workspace(workspace_id: str) -> None:
    workspace = find_workspace(workspace_id)
    STATE["current_workspace_id"] = workspace_id
    STATE["ui_state"]["workspaceId"] = workspace_id
    if workspace is not None:
        STATE["ui_state"]["workspaceName"] = workspace["name"]


def list_verify_results() -> list[dict[str, Any]]:
    return STATE["verify_results"]


def find_verify_result(doc_id: str) -> dict[str, Any] | None:
    return next((result for result in STATE["verify_results"] if result["docId"] == doc_id), None)


def save_draft(draft_id: str, draft: dict[str, Any]) -> None:
    STATE["drafts"][draft_id] = draft


def get_draft(draft_id: str) -> dict[str, Any] | None:
    return STATE["drafts"].get(draft_id)


def get_or_create_chat_history(session_id: str) -> list[dict[str, Any]]:
    return STATE["chat_sessions"].setdefault(session_id, [])


def get_chat_history(session_id: str) -> list[dict[str, Any]]:
    return STATE["chat_sessions"].get(session_id, [])


def get_document(workspace_id: str) -> dict[str, Any] | None:
    return STATE["documents"].get(workspace_id)


def save_feedback_file(file_id: str, name: str, content_type: str) -> None:
    STATE["feedback_files"][file_id] = {
        "fileId": file_id,
        "name": name,
        "contentType": content_type,
    }


def get_feedback_file(file_id: str) -> dict[str, Any] | None:
    return STATE["feedback_files"].get(file_id)


def save_feedback_session(analysis_id: str, file_ids: list[str], status: str) -> None:
    STATE["feedback_sessions"][analysis_id] = {"fileIds": file_ids, "status": status}


def clear_feedback_session(session_id: str) -> None:
    STATE["feedback_sessions"].pop(session_id, None)


def save_prediction_state(key: str, payload: dict[str, Any]) -> None:
    STATE["prediction_state"][key] = payload


def get_ui_state() -> dict[str, Any]:
    return STATE["ui_state"]


def get_settings() -> dict[str, Any]:
    return STATE["settings"]


def get_current_workspace_id() -> str:
    return STATE["current_workspace_id"]


def set_ui_route(route: str) -> None:
    STATE["ui_state"]["route"] = route


def set_ui_workspace(workspace_id: str, workspace_name: str) -> None:
    STATE["ui_state"]["workspaceId"] = workspace_id
    STATE["ui_state"]["workspaceName"] = workspace_name


def set_ui_toast(level: str, message: str) -> None:
    STATE["ui_state"]["toast"] = {"level": level, "message": message}


def set_ui_prediction_popup(payload: dict[str, Any]) -> None:
    STATE["ui_state"]["predictionPopup"] = payload
