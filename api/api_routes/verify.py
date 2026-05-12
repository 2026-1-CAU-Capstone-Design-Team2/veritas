from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..services import verify_service

router = APIRouter()


@router.get("/api/v1/verify/results")
async def verify_list(
    workspaceId: str | None = Query(default=None),
    level: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    return verify_service.list_verify_results(workspaceId, level, page, pageSize)


@router.get("/api/v1/verify/results/{docId}")
async def verify_detail(docId: str) -> dict[str, Any]:
    return verify_service.get_verify_detail(docId)
