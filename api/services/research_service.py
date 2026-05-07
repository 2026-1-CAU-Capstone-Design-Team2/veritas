from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..api_common import new_id, utc_now_iso
from ..repositories import state_repository as repo


def create_research_job(
    workspace_id: str | None,
    instruction: str,
    reference_urls: list[str],
) -> dict[str, Any]:
    selected_workspace_id = workspace_id or repo.get_current_workspace_id()
    workspace = repo.find_workspace(selected_workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"workspace '{selected_workspace_id}' not found")

    job_id = new_id("rs")
    cleaned_urls = [url.strip() for url in reference_urls if url.strip()]
    job = {
        "jobId": job_id,
        "workspaceId": selected_workspace_id,
        "workspaceName": workspace["name"],
        "instruction": instruction.strip(),
        "referenceUrls": cleaned_urls,
        "status": "completed",
        "submittedAt": utc_now_iso(),
        "summary": "조사 요청과 레퍼런스 URL을 접수했고, 검증 단계에서 사용할 수 있는 mock 조사 결과를 준비했습니다.",
        "collectedDocuments": [
            {"title": "APAC 지역 AI 규제 동향", "source": cleaned_urls[0] if cleaned_urls else "mock://policy-report"},
            {"title": "엔터프라이즈 LLM 벤치마크 2026", "source": cleaned_urls[1] if len(cleaned_urls) > 1 else "mock://benchmark"},
            {"title": "AI 워크플로우 보안 가이드", "source": "mock://security-guide"},
        ],
    }
    repo.save_research_job(job_id, job)
    return job


def get_research_job(job_id: str) -> dict[str, Any]:
    job = repo.get_research_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"research job '{job_id}' not found")
    return job


def list_research_jobs(limit: int) -> dict[str, list[dict[str, Any]]]:
    jobs = sorted(repo.list_research_jobs(), key=lambda item: item["submittedAt"], reverse=True)
    return {"items": jobs[:limit]}
