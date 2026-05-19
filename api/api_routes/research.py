from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..api_models import ResearchJobCreateRequest
from ..services import research_service

router = APIRouter()


@router.post("/api/v1/research/jobs", status_code=201)
def research_job_create(payload: ResearchJobCreateRequest) -> dict[str, Any]:
    # Plain `def` (not `async def`) so FastAPI runs this in its thread pool.
    # AutoSurvey is a long-running blocking workflow; if this were `async def`,
    # the event loop would freeze for the entire run and every other request
    # — including the progress poller and workspace switch — would queue
    # behind it, making the UI appear frozen.
    return research_service.create_research_job(
        payload.workspaceId,
        payload.instruction,
        payload.referenceUrls,
        payload.maxDocs,
        payload.scoutDocs,
        payload.collectBatchSize,
    )


@router.get("/api/v1/research/jobs")
async def research_job_list(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, list[dict[str, Any]]]:
    return research_service.list_research_jobs(limit)


@router.get("/api/v1/research/jobs/{jobId}")
async def research_job_detail(jobId: str) -> dict[str, Any]:
    return research_service.get_research_job(jobId)


@router.get("/api/v1/research/progress")
async def research_progress(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    return research_service.get_progress(since=since, limit=limit)
