from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..repositories import state_repository as repo


def list_verify_results(
    workspace_id: str | None,
    level: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    items = repo.list_verify_results()
    if workspace_id:
        items = [item for item in items if item["workspaceId"] == workspace_id]
    if level and level != "전체":
        items = [item for item in items if item["level"] == level]

    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = items[start:end]
    return {
        "items": [
            {
                "docId": item["docId"],
                "title": item["title"],
                "matchRate": item["matchRate"],
                "level": item["level"],
                "issues": item.get("issues", []),
            }
            for item in page_items
        ],
        "page": page,
        "totalPages": total_pages,
    }


def get_verify_detail(doc_id: str) -> dict[str, Any]:
    item = repo.find_verify_result(doc_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"document '{doc_id}' not found")
    return {
        "docId": item["docId"],
        "workspaceId": item["workspaceId"],
        "title": item["title"],
        "matchRate": item["matchRate"],
        "level": item["level"],
        "issues": item["issues"],
    }
