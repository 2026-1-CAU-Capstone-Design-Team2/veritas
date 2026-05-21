from __future__ import annotations

from copy import deepcopy
from typing import Any

from db.app_state import read_json, write_json

from .model_catalog import (
    DEFAULT_EMBEDDING_MODEL_ID,
    DEFAULT_LLM_MODEL_ID,
    default_model_settings,
    get_model,
    selected_embedding_from_settings,
    selected_model_from_settings,
)


SETTINGS_STATE_KEY = "settings"


def _deep_merge(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings() -> dict[str, Any]:
    stored = read_json(SETTINGS_STATE_KEY, {})
    if not isinstance(stored, dict):
        stored = {}
    return _deep_merge(default_model_settings(), stored)


def save_settings(settings: dict[str, Any]) -> None:
    write_json(SETTINGS_STATE_KEY, settings)


def launcher_initial_model_selected(settings: dict[str, Any] | None = None) -> bool:
    payload = settings or load_settings()
    launcher = payload.get("launcher")
    return bool(isinstance(launcher, dict) and launcher.get("initialModelSelected"))


def save_selected_models(
    *,
    llm_model_id: str | None = None,
    embedding_model_id: str | None = None,
    mark_initial_selected: bool = False,
) -> dict[str, Any]:
    settings = load_settings()
    llm = get_model(llm_model_id or selected_model_from_settings(settings).id or DEFAULT_LLM_MODEL_ID, kind="llm")
    embedding = get_model(
        embedding_model_id
        or selected_embedding_from_settings(settings).id
        or DEFAULT_EMBEDDING_MODEL_ID,
        kind="embedding",
    )
    settings["model"] = {
        "modelId": llm.id,
        "modelName": llm.name,
    }
    settings["embeddingModel"] = {
        "modelId": embedding.id,
        "modelName": embedding.name,
    }
    launcher = settings.setdefault("launcher", {})
    if isinstance(launcher, dict) and mark_initial_selected:
        launcher["initialModelSelected"] = True
    save_settings(settings)
    return settings
