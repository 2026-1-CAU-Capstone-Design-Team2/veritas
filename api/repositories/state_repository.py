from __future__ import annotations

from copy import deepcopy
from typing import Any

from db.app_state import read_json, write_json
from llm.model_catalog import (
    DEFAULT_EMBEDDING_MODEL_ID,
    DEFAULT_LLM_MODEL_ID,
    default_model_settings,
    get_model,
)

from ..api_common import STATE


SETTINGS_STATE_KEY = "settings"
_settings_loaded = False


def _deep_merge(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in override.items():
        if (
            isinstance(value, dict)
            and isinstance(merged.get(key), dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _settings_defaults() -> dict[str, Any]:
    defaults = deepcopy(STATE.get("settings") or {})
    return _deep_merge(defaults, default_model_settings())


def _ensure_settings_loaded() -> None:
    global _settings_loaded
    if _settings_loaded:
        return
    stored = read_json(SETTINGS_STATE_KEY, {})
    if not isinstance(stored, dict):
        stored = {}
    STATE["settings"] = _deep_merge(_settings_defaults(), stored)
    _settings_loaded = True


def _persist_settings() -> None:
    _ensure_settings_loaded()
    write_json(SETTINGS_STATE_KEY, STATE["settings"])


def reload_settings() -> dict[str, Any]:
    """Force-reload the settings cache from SQLite.

    The settings store is shared with ``llm.model_settings`` (same app_state
    ``"settings"`` key). When a live model switch persists through that path,
    this re-syncs the in-memory ``STATE["settings"]`` cache so ``get_settings``
    (the ``GET /api/v1/settings`` source) reflects the change without a restart.
    """
    global _settings_loaded
    _settings_loaded = False
    _ensure_settings_loaded()
    return STATE["settings"]


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
    _ensure_settings_loaded()
    return _public_settings(STATE["settings"])


def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    public = deepcopy(settings)
    autosurvey_openai = public.setdefault("autosurveyOpenAI", {})
    if not isinstance(autosurvey_openai, dict):
        autosurvey_openai = {}
        public["autosurveyOpenAI"] = autosurvey_openai
    api_key = str(autosurvey_openai.pop("apiKey", "") or "").strip()
    autosurvey_openai["provider"] = str(
        autosurvey_openai.get("provider") or ("openai" if api_key else "local")
    )
    autosurvey_openai["apiKeySet"] = bool(api_key)
    autosurvey_openai["apiKeyPreview"] = _api_key_preview(api_key)
    return public


def _api_key_preview(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:3]}...{api_key[-4:]}"


def set_model_settings(model_id: str) -> dict[str, Any]:
    _ensure_settings_loaded()
    spec = get_model(model_id or DEFAULT_LLM_MODEL_ID, kind="llm")
    STATE["settings"]["model"] = {
        "modelId": spec.id,
        "modelName": spec.name,
    }
    _persist_settings()
    return STATE["settings"]["model"]


def set_embedding_model_settings(model_id: str) -> dict[str, Any]:
    _ensure_settings_loaded()
    spec = get_model(model_id or DEFAULT_EMBEDDING_MODEL_ID, kind="embedding")
    STATE["settings"]["embeddingModel"] = {
        "modelId": spec.id,
        "modelName": spec.name,
    }
    _persist_settings()
    return STATE["settings"]["embeddingModel"]


def set_launcher_initial_model_selected(value: bool) -> dict[str, Any]:
    _ensure_settings_loaded()
    launcher = STATE["settings"].setdefault("launcher", {})
    if not isinstance(launcher, dict):
        launcher = {}
        STATE["settings"]["launcher"] = launcher
    launcher["initialModelSelected"] = bool(value)
    _persist_settings()
    return launcher


def set_local_access_settings(folder_paths: list[str]) -> dict[str, Any]:
    _ensure_settings_loaded()
    cleaned_paths: list[str] = []
    for folder_path in folder_paths:
        cleaned = folder_path.strip()
        if cleaned and cleaned not in cleaned_paths:
            cleaned_paths.append(cleaned)
    STATE["settings"]["localAccess"] = {"folderPaths": cleaned_paths}
    _persist_settings()
    return STATE["settings"]["localAccess"]


def set_document_tools_settings(custom_tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist the user-defined document editing tools.

    Each tool is normalized to ``{"name", "identifier"}``; entries without a
    name are dropped and exact duplicates are collapsed.
    """
    _ensure_settings_loaded()
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
    _persist_settings()
    return STATE["settings"]["documentTools"]


def set_research_method_settings(sample_count: int, plan_count: int) -> dict[str, Any]:
    """Persist AutoSurvey pacing — initial scout sample size and per-plan
    collect/batch-summary cycle size — so research runs honor the user's
    설정 > 고급 설정 values."""
    _ensure_settings_loaded()
    STATE["settings"]["research"] = {
        "sampleCount": max(1, int(sample_count)),
        "planCount": max(1, int(plan_count)),
    }
    _persist_settings()
    return STATE["settings"]["research"]


def set_autosurvey_openai_settings(
    *,
    api_key: str = "",
    clear: bool = False,
) -> dict[str, Any]:
    _ensure_settings_loaded()
    config = STATE["settings"].setdefault("autosurveyOpenAI", {})
    if not isinstance(config, dict):
        config = {}
        STATE["settings"]["autosurveyOpenAI"] = config

    if clear:
        config.pop("apiKey", None)
        config["provider"] = "local"
    else:
        cleaned_key = str(api_key or "").strip()
        if cleaned_key:
            config["apiKey"] = cleaned_key
            config["provider"] = "openai"
        elif not str(config.get("apiKey") or "").strip():
            config["provider"] = "local"

    config.pop("apiKeySet", None)
    config.pop("apiKeyPreview", None)
    _persist_settings()
    return _public_settings(STATE["settings"])["autosurveyOpenAI"]


def set_llm_parallel_settings(value: int) -> int:
    """Persist the parallel-decoding concurrency (설정 > 고급 설정 > 병렬 디코딩).

    Hard-clamped to 1..5: 1 keeps the historical serial path, and 5 caps how
    many concurrent requests a low-spec local llama-server is asked to juggle.
    The clamped value is what callers should apply to ``LLMClient.max_parallel``.
    """
    _ensure_settings_loaded()
    clamped = max(1, min(5, int(value)))
    STATE["settings"]["llmParallel"] = clamped
    _persist_settings()
    return clamped


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
