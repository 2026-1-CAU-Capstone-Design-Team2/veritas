from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..services import verify_service

router = APIRouter()


class VerifyJobCreateRequest(BaseModel):
    """POST body for ``/verify/jobs``.

    ``workspaceId`` may be omitted to fall back to the runtime's active
    workspace (mirrors ``set_workspace``'s default-resolution). ``tasks``
    defaults to all three pipelines.
    """

    workspaceId: str | None = None
    tasks: list[str] | None = None


@router.post("/api/v1/verify/jobs", status_code=201)
def verify_job_create(payload: VerifyJobCreateRequest) -> dict[str, Any]:
    # Plain `def` (not `async def`) so FastAPI runs this in its thread pool.
    # Verification is a multi-second blocking workflow; on the event loop it
    # would freeze the progress poller and every other request, leaving the
    # UI looking frozen.
    return verify_service.create_verify_job(payload.workspaceId, payload.tasks)


@router.get("/api/v1/verify/progress")
async def verify_progress(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    return verify_service.get_progress(since=since, limit=limit)


@router.get("/api/v1/verify/summary")
async def verify_summary(
    workspaceId: str | None = Query(default=None),
) -> dict[str, Any]:
    return verify_service.get_summary(workspaceId)


@router.get("/api/v1/verify/results")
async def verify_list(
    workspaceId: str | None = Query(default=None),
    level: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    # ``le`` was 100 originally, but the verify page deliberately requests
    # every doc in one round-trip (``page_size=200``) and handles paging
    # client-side so the level chips can show accurate per-level counts
    # without a second fetch. A page_size > 100 was tripping FastAPI's
    # validator and returning 422; the frontend silently caught the
    # ``ApiError`` and rendered an empty card list while the summary
    # endpoint (no such cap) kept working — that is exactly the bug
    # report "헤더는 있는데 아래 카드만 0개". Raising the cap to 500
    # gives every plausible corpus headroom without forcing the
    # frontend to paginate over the network.
    pageSize: int = Query(default=10, ge=1, le=500),
) -> dict[str, Any]:
    return verify_service.list_verify_results(workspaceId, level, page, pageSize)


@router.get("/api/v1/verify/results/{docId}")
async def verify_detail(
    docId: str,
    workspaceId: str | None = Query(default=None),
) -> dict[str, Any]:
    return verify_service.get_verify_detail(docId, workspaceId)
