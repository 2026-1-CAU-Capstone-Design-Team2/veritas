from __future__ import annotations

from typing import Any

from ..repositories import state_repository as repo


def get_settings() -> dict[str, Any]:
    return repo.get_settings()


def update_model(model_name: str) -> dict[str, Any]:
    model = repo.set_model_settings(model_name)
    return {"model": model, "updated": True}


def update_local_access(folder_paths: list[str]) -> dict[str, Any]:
    local_access = repo.set_local_access_settings(folder_paths)
    return {"localAccess": local_access, "updated": True}
