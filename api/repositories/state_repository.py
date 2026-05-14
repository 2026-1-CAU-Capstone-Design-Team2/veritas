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


def upsert_workspace(workspace: dict[str, Any]) -> None:
    workspace_id = str(workspace.get("workspaceId") or "").strip()
    if not workspace_id:
        return
    for index, item in enumerate(STATE["workspaces"]):
        if item.get("workspaceId") == workspace_id:
            STATE["workspaces"][index] = {**item, **workspace}
            return
    STATE["workspaces"].append(workspace)


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


def save_document(workspace_id: str, document: dict[str, Any]) -> None:
    STATE["documents"][workspace_id] = document


def save_feedback_file(file_id: str, name: str, content_type: str, text: str = "") -> None:
    STATE["feedback_files"][file_id] = {
        "fileId": file_id,
        "name": name,
        "contentType": content_type,
        "text": text,
    }


def get_feedback_file(file_id: str) -> dict[str, Any] | None:
    return STATE["feedback_files"].get(file_id)


def save_feedback_session(analysis_id: str, file_ids: list[str], status: str) -> None:
    STATE["feedback_sessions"][analysis_id] = {"fileIds": file_ids, "status": status}


def save_feedback_result(file_id: str, payload: dict[str, Any]) -> None:
    STATE["feedback_results"][file_id] = payload


def get_feedback_result(file_id: str) -> dict[str, Any] | None:
    return STATE["feedback_results"].get(file_id)


def clear_feedback_session(session_id: str) -> None:
    STATE["feedback_sessions"].pop(session_id, None)


def save_prediction_state(key: str, payload: dict[str, Any]) -> None:
    STATE["prediction_state"][key] = payload


def get_prediction_state(key: str) -> dict[str, Any] | None:
    return STATE["prediction_state"].get(key)


def save_research_job(job_id: str, job: dict[str, Any]) -> None:
    STATE["research_jobs"][job_id] = job


def get_research_job(job_id: str) -> dict[str, Any] | None:
    return STATE["research_jobs"].get(job_id)


def list_research_jobs() -> list[dict[str, Any]]:
    return list(STATE["research_jobs"].values())


def save_document_assist_session(session_id: str, payload: dict[str, Any]) -> None:
    STATE["document_assist_sessions"][session_id] = payload


def get_document_assist_session(session_id: str) -> dict[str, Any] | None:
    return STATE["document_assist_sessions"].get(session_id)


def get_ui_state() -> dict[str, Any]:
    return STATE["ui_state"]


def get_settings() -> dict[str, Any]:
    return STATE["settings"]


def set_model_settings(model_name: str) -> dict[str, Any]:
    STATE["settings"]["model"] = {"modelName": model_name}
    return STATE["settings"]["model"]


def set_local_access_settings(folder_paths: list[str]) -> dict[str, Any]:
    cleaned_paths: list[str] = []
    for folder_path in folder_paths:
        cleaned = folder_path.strip()
        if cleaned and cleaned not in cleaned_paths:
            cleaned_paths.append(cleaned)
    STATE["settings"]["localAccess"] = {"folderPaths": cleaned_paths}
    return STATE["settings"]["localAccess"]


def set_document_tools_settings(custom_tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist the user-defined document editing tools.

    Each tool is normalized to ``{"name", "identifier"}``; entries without a
    name are dropped and exact duplicates are collapsed.
    """
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for tool in custom_tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        identifier = str(tool.get("identifier") or "").strip()
        key = f"{name}|{identifier}".lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({"name": name, "identifier": identifier})
    STATE["settings"]["documentTools"] = {"custom": cleaned}
    return STATE["settings"]["documentTools"]


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
