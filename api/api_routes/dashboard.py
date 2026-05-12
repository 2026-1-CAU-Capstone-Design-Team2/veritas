from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from ..services import dashboard_service

router = APIRouter()


@router.get("/api/v1/dashboard/summary")
async def dashboard_summary(workspaceId: str | None = Query(default=None)) -> dict[str, Any]:
    return dashboard_service.get_dashboard_summary(workspaceId)


@router.get("/api/v1/dashboard/recent-workspaces")
async def recent_workspaces(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, list[dict[str, Any]]]:
    return dashboard_service.get_recent_workspaces(limit)


@router.get("/api/v1/dashboard/recent-documents")
async def recent_documents(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, list[dict[str, Any]]]:
    return dashboard_service.get_recent_documents(limit)
