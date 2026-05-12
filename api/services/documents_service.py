from __future__ import annotations

import json
import os
from pathlib import Path

from ..repositories import state_repository as repo


def get_document_summary(workspace_id: str) -> dict[str, str]:
    document = repo.get_document(workspace_id)
    if document is None:
        return {"workspaceId": workspace_id, "summary": _read_final_markdown(workspace_id)}
    return {"workspaceId": workspace_id, "summary": str(document.get("summary") or "")}


def get_document_merged(workspace_id: str) -> dict[str, str]:
    document = repo.get_document(workspace_id)
    if document is None:
        return {"workspaceId": workspace_id, "mergedText": _read_document_list(workspace_id)}
    return {"workspaceId": workspace_id, "mergedText": str(document.get("mergedText") or "")}


def _workspace_dir(workspace_id: str) -> Path:
    return Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve() / workspace_id


def _read_final_markdown(workspace_id: str) -> str:
    path = _workspace_dir(workspace_id) / "final.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _read_document_list(workspace_id: str) -> str:
    index_path = _workspace_dir(workspace_id) / "summary" / "index.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    records = payload.get("records", [])
    if not isinstance(records, list):
        return ""
    lines = ["찾아낸 문서"]
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        title = str(record.get("title") or record.get("doc_id") or "Untitled")
        url = str(record.get("final_url") or record.get("url") or "")
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)
