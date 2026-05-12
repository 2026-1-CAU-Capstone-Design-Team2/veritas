from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..repositories import state_repository as repo


def list_workspaces(status: str | None) -> dict[str, list[dict[str, Any]]]:
    _sync_run_workspaces()
    items = repo.list_workspaces()
    if status:
        items = [item for item in items if item.get("status") == status]
    return {
        "items": [
            {
                "workspaceId": item["workspaceId"],
                "name": item["name"],
                "detail": item.get("detail", ""),
                "status": item.get("status"),
                "lastWorkedAt": item.get("lastWorkedAt"),
            }
            for item in items
        ]
    }


def switch_workspace(workspace_id: str) -> dict[str, str]:
    _sync_run_workspaces()
    workspace = repo.find_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"workspace '{workspace_id}' not found")

    repo.set_current_workspace(workspace["workspaceId"])
    return {"workspaceId": workspace["workspaceId"], "name": workspace["name"]}


def _sync_run_workspaces() -> None:
    for workspace in _scan_run_workspaces():
        repo.upsert_workspace(workspace)


def _scan_run_workspaces() -> list[dict[str, Any]]:
    root = Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()
    if not root.exists():
        return []

    workspaces: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_dir() or path.name.startswith("_") or path.name == "__pycache__":
            continue
        summary_dir = path / "summary"
        final_path = path / "final.md"
        if not summary_dir.exists() and not final_path.exists():
            continue
        document_count = _document_count(summary_dir / "index.json")
        workspaces.append(
            {
                "workspaceId": path.name,
                "name": path.name,
                "detail": f"문서 {document_count}개 · {path}",
                "status": "completed" if final_path.exists() else "running",
                "lastWorkedAt": _mtime_iso(final_path if final_path.exists() else path),
            }
        )
    return workspaces


def _document_count(index_path: Path) -> int:
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        records = payload.get("records", [])
        return len(records) if isinstance(records, list) else 0
    except Exception:
        return 0


def _mtime_iso(path: Path) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
