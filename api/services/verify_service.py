"""Thin API adapter for ``services/verification``.

The actual algorithms live in ``services/verification/`` (per
``ARCHITECTURE.md`` and ``VERIFY_DESIGN.md §0.3``). This module is the
*request-flow* layer:

* **create_verify_job** triggers a verification run via the runtime (which
  owns the progress ring buffer, like research does).
* **get_progress** passes through to the runtime's ring buffer.
* **list_verify_results / get_verify_detail / get_summary** read back what
  ``VerificationPersistence`` saved, delegating the domain → UI shape mapping
  to :mod:`verify_view`.

Phrasing / level thresholds / scoring blends — anything that decides what the
user sees — lives in :mod:`verify_view`, not here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from services.verification import VerificationPersistence

from ..api_common import new_id
from . import verify_view
from .agent_runtime import get_runtime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------


def create_verify_job(
    workspace_id: str | None,
    tasks: list[str] | None = None,
) -> dict[str, Any]:
    """Synchronously run verification on a workspace and return a result summary.

    Plain ``def`` from FastAPI's perspective (same as research) so the
    long-running pipeline executes on FastAPI's thread pool and the event loop
    stays free for the progress poller.
    """
    runtime = get_runtime()
    job_id = new_id("vf")
    summary = runtime.run_verification(
        workspace_id=workspace_id,
        tasks=tasks,
        job_id=job_id,
    )
    return {"jobId": job_id, **summary}


def get_progress(*, since: int, limit: int) -> dict[str, Any]:
    return get_runtime().get_verify_progress(since=since, limit=limit)


# ---------------------------------------------------------------------------
# Result read-back
# ---------------------------------------------------------------------------


def list_verify_results(
    workspace_id: str | None,
    level: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Page through a workspace's per-doc verification results.

    Returns an ``available: false`` payload (instead of raising 404) when the
    workspace has no verification artifacts yet — the frontend reads that to
    show the "검증 시작" empty state without treating the missing dataset as
    an error.
    """
    resolved = _resolve_workspace_id(workspace_id)
    if not resolved:
        return _empty_list_response(None)

    loaded = _load_artifacts(resolved)
    if loaded is None:
        return _empty_list_response(resolved)
    artifacts, meta = loaded

    items = verify_view.build_doc_items(artifacts, _load_doc_titles(resolved))
    if level and level != "전체":
        items = [item for item in items if item["level"] == level]

    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]

    return {
        "items": page_items,
        "page": page,
        "totalPages": total_pages,
        "totalItems": total,
        "workspaceId": resolved,
        "available": True,
        "updatedAt": meta.get("updatedAt"),
        "completedTasks": list(meta.get("completedTasks") or []),
    }


def get_verify_detail(doc_id: str, workspace_id: str | None = None) -> dict[str, Any]:
    """Detail payload for one document.

    Includes the per-section / per-facet score breakdowns and which concept
    clusters touch the doc — the data the detail dialog renders.
    """
    resolved = _resolve_workspace_id(workspace_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="검증 결과를 찾을 수 없습니다.")
    loaded = _load_artifacts(resolved)
    if loaded is None:
        raise HTTPException(status_code=404, detail="이 워크스페이스에는 검증 결과가 아직 없습니다.")
    artifacts, meta = loaded

    items = verify_view.build_doc_items(artifacts, _load_doc_titles(resolved))
    match = next((item for item in items if item["docId"] == doc_id), None)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=f"document '{doc_id}' has no verification record",
        )

    return {
        **match,
        "workspaceId": resolved,
        "updatedAt": meta.get("updatedAt"),
        "sectionBreakdown": verify_view.section_breakdown_for(doc_id, artifacts.sections),
        "facetBreakdown": verify_view.facet_breakdown_for(doc_id, artifacts.intent),
        "conceptParticipation": verify_view.concept_participation_for(artifacts.consensus),
    }


def get_summary(workspace_id: str | None) -> dict[str, Any]:
    """Workspace-level headline tiles (counts + averages).

    Empty payload when nothing has been verified yet so the UI can flip into
    "검증 시작" mode without a try/except dance.
    """
    resolved = _resolve_workspace_id(workspace_id)
    if not resolved:
        return _empty_summary(None)

    loaded = _load_artifacts(resolved)
    if loaded is None:
        return _empty_summary(resolved)
    artifacts, meta = loaded

    titles = _load_doc_titles(resolved)
    items = verify_view.build_doc_items(artifacts, titles)
    high = sum(1 for item in items if item["level"] == "높음")
    medium = sum(1 for item in items if item["level"] == "중간")
    low = sum(1 for item in items if item["level"] == "낮음")
    average_pct = (
        round(sum(item["matchRatePercent"] for item in items) / len(items))
        if items
        else 0
    )

    # Sections that the LLM-planned outline asked for but the corpus does not
    # really support — fewer than 3 sentences assigned. The verify_view's
    # ``issues_overview`` enforces the same cutoff, so the count and the
    # drill-down list stay consistent.
    underweighted_sections = (
        sum(
            1
            for section in artifacts.sections.sections
            if len(section.sentence_assignments) < 3
        )
        if artifacts.sections
        else 0
    )

    return {
        "workspaceId": resolved,
        "available": True,
        "updatedAt": meta.get("updatedAt"),
        "completedTasks": list(meta.get("completedTasks") or []),
        "documentCount": len(items),
        "averageMatchPercent": average_pct,
        "highCount": high,
        "mediumCount": medium,
        "lowCount": low,
        "underweightedSectionCount": underweighted_sections,
        "intentGapCount": (
            len(artifacts.intent.coverage_gap) if artifacts.intent else 0
        ),
        "conflictCount": (
            len(artifacts.consensus.conflicts) if artifacts.consensus else 0
        ),
        # Flow outline summary — the section panel reads this directly.
        "flowSource": (
            artifacts.sections.flow_source if artifacts.sections else "empty"
        ),
        "sentenceCount": (
            artifacts.sections.sentence_count if artifacts.sections else 0
        ),
        # Drill-down payloads — kept inline so the issues dialog and the
        # sections panel render without a second round-trip.
        "sectionsOverview": verify_view.sections_overview(artifacts),
        "issues": verify_view.issues_overview(artifacts, titles),
    }


# ---------------------------------------------------------------------------
# Workspace + artifact loading
# ---------------------------------------------------------------------------


def _resolve_workspace_id(workspace_id: str | None) -> str | None:
    """Pick the workspace this request targets.

    Honours an explicit id; otherwise uses the runtime's active workspace,
    resolving the ``"default"`` placeholder to the most-recent real workspace
    on disk so verify on a fresh boot still lands somewhere meaningful.
    """
    runtime = get_runtime()
    explicit = (workspace_id or "").strip()
    if explicit and explicit != "default":
        return explicit

    active = (runtime.workspace_id or "").strip()
    if active and active != "default":
        return active

    initial = runtime._discover_initial_workspace()  # noqa: SLF001 (runtime is in-house)
    return initial.name if initial is not None else None


def _load_artifacts(workspace_id: str):
    runtime = get_runtime()
    persistence = VerificationPersistence(runtime.output_root)
    try:
        return persistence.load(workspace_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("verify: failed to load artifacts for %s: %s", workspace_id, exc)
        return None


def _load_doc_titles(workspace_id: str) -> dict[str, str]:
    """Cheap title lookup via index.json (no chunk reload, no embedding load)."""
    runtime = get_runtime()
    index_path = Path(runtime.output_root) / workspace_id / "summary" / "index.json"
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    records = data.get("records") if isinstance(data, dict) else []
    if not isinstance(records, list):
        return {}
    titles: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("duplicate_of"):
            continue
        doc_id = str(record.get("doc_id") or "").strip()
        if not doc_id:
            continue
        titles[doc_id] = str(
            record.get("title") or record.get("url") or f"문서 {doc_id}"
        ).strip()
    return titles


# ---------------------------------------------------------------------------
# Empty/placeholder responses
# ---------------------------------------------------------------------------


def _empty_list_response(workspace_id: str | None) -> dict[str, Any]:
    return {
        "items": [],
        "page": 1,
        "totalPages": 1,
        "totalItems": 0,
        "workspaceId": workspace_id,
        "available": False,
        "updatedAt": None,
        "completedTasks": [],
    }


def _empty_summary(workspace_id: str | None) -> dict[str, Any]:
    return {
        "workspaceId": workspace_id,
        "available": False,
        "updatedAt": None,
        "completedTasks": [],
        "documentCount": 0,
        "averageMatchPercent": 0,
        "highCount": 0,
        "mediumCount": 0,
        "lowCount": 0,
        "underweightedSectionCount": 0,
        "intentGapCount": 0,
        "conflictCount": 0,
        "flowSource": "empty",
        "sentenceCount": 0,
        "sectionsOverview": [],
        "issues": [],
    }
