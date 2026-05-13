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
def switch_workspace(payload: WorkspaceSwitchRequest) -> dict[str, str]:
    # Plain `def` so FastAPI dispatches to its thread pool. Workspace switch
    # rebuilds the tool registry / ChromaDB handles, which is potentially slow
    # and must not occupy the event loop while a research run is in flight.
    return workspaces_service.switch_workspace(payload.workspaceId)
