from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..api_models import WorkspaceSwitchRequest
from ..services import workspaces_service

router = APIRouter()


@router.get("/api/v1/workspaces")
async def workspace_list(status: str | None = Query(default=None)) -> dict[str, list[dict[str, Any]]]:
    return workspaces_service.list_workspaces(status)


@router.post("/api/v1/workspaces/switch")
async def switch_workspace(payload: WorkspaceSwitchRequest) -> dict[str, str]:
    return workspaces_service.switch_workspace(payload.workspaceId)
