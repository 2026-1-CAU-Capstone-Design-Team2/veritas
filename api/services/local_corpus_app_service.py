from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from services.local_corpus import LocalCorpusService, ManifestRepository

from .agent_runtime import get_runtime


def index_workspace_sources(
    workspace_id: str,
    roots: list[str],
    *,
    clear_local_first: bool = False,
) -> dict[str, Any]:
    workspace_id = _resolve_workspace_id(workspace_id)
    normalized_roots = [str(root).strip() for root in roots if str(root).strip()]
    if not normalized_roots and not clear_local_first:
        raise HTTPException(status_code=422, detail="At least one local root path is required.")

    runtime = get_runtime()
    runtime.set_workspace(workspace_id)
    service = _service(runtime)
    result = service.index_workspace_sources(
        workspace_id,
        normalized_roots,
        clear_local_first=clear_local_first,
    )
    return {
        "workspaceId": result.workspace_id,
        "indexedCount": result.indexed_count,
        "skippedCount": result.skipped_count,
        "failedCount": result.failed_count,
        "vectorCount": result.vector_count,
        "sources": result.sources,
        "manifestPath": result.manifest_path,
    }


def list_sources(workspace_id: str) -> dict[str, Any]:
    workspace_id = _resolve_workspace_id(workspace_id)
    runtime = get_runtime()
    service = _service(runtime)
    sources = service.list_sources(workspace_id)
    return {
        "workspaceId": workspace_id,
        "sources": [
            {
                "sourceId": source.source_id,
                "workspaceId": source.workspace_id,
                "sourceScope": source.source_scope.value,
                "sourceKind": source.source_kind.value,
                "title": source.title,
                "displayPath": source.display_path,
                "privacyLabel": source.privacy_label.value,
                "status": source.status,
                "modifiedAt": source.modified_at,
            }
            for source in sources
        ],
    }


def remove_sources(workspace_id: str, source_ids: list[str]) -> dict[str, Any]:
    workspace_id = _resolve_workspace_id(workspace_id)
    runtime = get_runtime()
    runtime.set_workspace(workspace_id)
    service = _service(runtime)
    result = service.remove_sources(workspace_id, source_ids)
    return {
        "workspaceId": result.workspace_id,
        "removedCount": result.removed_count,
        "sourceIds": result.source_ids,
    }


def _service(runtime) -> LocalCorpusService:
    return LocalCorpusService(
        output_root=runtime.output_root,
        llm=runtime.llm,
        manifest_repository=ManifestRepository(runtime.output_root),
        vector_store=runtime.rag_service.vector_store if runtime.rag_service else None,
    )


def _resolve_workspace_id(workspace_id: str) -> str:
    workspace_id = str(workspace_id or "").strip()
    if workspace_id and workspace_id != "default":
        return workspace_id
    runtime = get_runtime()
    active = str(getattr(runtime, "workspace_id", "") or "").strip()
    if active and active != "default":
        return active
    return workspace_id or "default"


__all__ = ["index_workspace_sources", "list_sources", "remove_sources"]
