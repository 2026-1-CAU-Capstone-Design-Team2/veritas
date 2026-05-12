from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException

from ..api_common import new_id, utc_now_iso
from ..repositories import state_repository as repo
from .agent_runtime import get_runtime


def create_research_job(
    workspace_id: str | None,
    instruction: str,
    reference_urls: list[str],
) -> dict[str, Any]:
    instruction_text = instruction.strip()
    if not instruction_text:
        raise HTTPException(status_code=422, detail="instruction must not be empty")

    selected_workspace_id = workspace_id or repo.get_current_workspace_id()
    job_id = new_id("rs")
    job = {
        "jobId": job_id,
        "workspaceId": selected_workspace_id,
        "workspaceName": _workspace_name(selected_workspace_id),
        "instruction": instruction_text,
        "referenceUrls": [url.strip() for url in reference_urls if url.strip()],
        "status": "running",
        "submittedAt": utc_now_iso(),
    }
    repo.save_research_job(job_id, job)

    started_at = time.perf_counter()
    try:
        result = get_runtime().run_autosurvey(
            instruction=instruction_text,
            reference_urls=job["referenceUrls"],
        )
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["completedAt"] = utc_now_iso()
        repo.save_research_job(job_id, job)
        raise HTTPException(status_code=502, detail=f"AutoSurvey workflow failed: {e}") from e

    workflow_result = result.get("workflow_result") if isinstance(result, dict) else {}
    result_workspace_id = str(result.get("workspace_id") or selected_workspace_id)
    result_workspace_name = str(result.get("workspace_name") or result_workspace_id)
    final_excerpt = str(result.get("final_report_excerpt") or "").strip()
    elapsed_seconds = float(result.get("elapsed_seconds") or (time.perf_counter() - started_at))
    documents = result.get("documents", [])
    if not isinstance(documents, list):
        documents = []
    job.update(
        {
            "status": "completed",
            "workspaceId": result_workspace_id,
            "workspaceName": result_workspace_name,
            "completedAt": utc_now_iso(),
            "summary": final_excerpt,
            "finalPath": result.get("final_path"),
            "finalMarkdown": result.get("final_report", ""),
            "indexedChunks": result.get("indexed_chunks"),
            "documents": documents,
            "documentCount": result.get("document_count", len(documents)),
            "nonDuplicateDocumentCount": result.get("non_duplicate_document_count"),
            "elapsedSeconds": elapsed_seconds,
            "collectedDocuments": documents or _collected_documents(workflow_result),
            "workflowResult": workflow_result,
        }
    )
    repo.save_research_job(job_id, job)
    repo.upsert_workspace(
        {
            "workspaceId": result_workspace_id,
            "name": result_workspace_name,
            "detail": f"문서 {job['documentCount']}개 · {job['elapsedSeconds']:.1f}초",
            "status": "completed",
            "lastWorkedAt": job["completedAt"],
        }
    )
    repo.set_current_workspace(result_workspace_id)
    repo.save_document(
        result_workspace_id,
        {
            "workspaceId": result_workspace_id,
            "summary": str(result.get("final_report") or final_excerpt),
            "mergedText": _format_document_list(documents),
            "finalPath": result.get("final_path"),
            "documentCount": job["documentCount"],
            "elapsedSeconds": elapsed_seconds,
            "updatedAt": job["completedAt"],
        },
    )
    return job


def get_research_job(job_id: str) -> dict[str, Any]:
    job = repo.get_research_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"research job '{job_id}' not found")
    return job


def list_research_jobs(limit: int) -> dict[str, list[dict[str, Any]]]:
    jobs = sorted(repo.list_research_jobs(), key=lambda item: item["submittedAt"], reverse=True)
    return {"items": jobs[:limit]}


def _workspace_name(workspace_id: str) -> str:
    workspace = repo.find_workspace(workspace_id)
    return str(workspace.get("name") or workspace_id) if workspace else workspace_id


def _collected_documents(workflow_result: Any) -> list[dict[str, str]]:
    if not isinstance(workflow_result, dict):
        return []

    documents: list[dict[str, str]] = []
    for plan_key in ("initial_plan", "active_plan"):
        plan = workflow_result.get(plan_key)
        if not isinstance(plan, dict):
            continue
        for query in plan.get("search_queries", []) or []:
            query_text = str(query or "").strip()
            if query_text:
                documents.append({"title": query_text, "source": "autosurvey_query"})
    return documents


def _format_document_list(documents: list[Any]) -> str:
    lines = ["찾아낸 문서"]
    for index, item in enumerate(documents, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled")
        url = str(item.get("url") or "")
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)
