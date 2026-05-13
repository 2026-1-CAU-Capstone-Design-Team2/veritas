from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..api_models import ScreenMonitoringStartRequest
from ..services import screen_monitoring_service

router = APIRouter()


@router.post("/api/v1/screen-monitoring/start")
async def start_screen_monitoring(payload: ScreenMonitoringStartRequest | None = None) -> dict[str, Any]:
    workspace_id = payload.workspaceId if payload is not None else None
    return screen_monitoring_service.start_monitoring(workspace_id)


@router.post("/api/v1/screen-monitoring/stop")
async def stop_screen_monitoring() -> dict[str, Any]:
    return screen_monitoring_service.stop_monitoring()


@router.get("/api/v1/screen-monitoring/status")
async def screen_monitoring_status() -> dict[str, Any]:
    return screen_monitoring_service.get_status()


@router.get("/api/v1/screen-monitoring/events")
async def screen_monitoring_events(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return screen_monitoring_service.get_events(since=since, limit=limit)
