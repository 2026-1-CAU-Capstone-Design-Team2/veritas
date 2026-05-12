from __future__ import annotations

from fastapi import HTTPException

from ..repositories import state_repository as repo


def get_document_summary(workspace_id: str) -> dict[str, str]:
    document = repo.get_document(workspace_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"workspace '{workspace_id}' not found")
    return {"workspaceId": workspace_id, "summary": document["summary"]}


def get_document_merged(workspace_id: str) -> dict[str, str]:
    document = repo.get_document(workspace_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"workspace '{workspace_id}' not found")
    return {"workspaceId": workspace_id, "mergedText": document["mergedText"]}
