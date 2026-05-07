from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..api_models import ResearchJobCreateRequest
from ..services import research_service

router = APIRouter()


@router.post("/api/v1/research/jobs", status_code=201)
async def research_job_create(payload: ResearchJobCreateRequest) -> dict[str, Any]:
    return research_service.create_research_job(
        payload.workspaceId,
        payload.instruction,
        payload.referenceUrls,
    )


@router.get("/api/v1/research/jobs")
async def research_job_list(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, list[dict[str, Any]]]:
    return research_service.list_research_jobs(limit)


@router.get("/api/v1/research/jobs/{jobId}")
async def research_job_detail(jobId: str) -> dict[str, Any]:
    return research_service.get_research_job(jobId)
