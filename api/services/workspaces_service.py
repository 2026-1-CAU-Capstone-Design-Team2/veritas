from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from db.db import get_connection, init_db
from db.workspace_sync import delete_workspace as _delete_workspace_from_db

from ..repositories import state_repository as repo


def list_workspaces(status: str | None) -> dict[str, list[dict[str, Any]]]:
    _sync_run_workspaces()
    items = _without_default_if_real_workspaces_exist(repo.list_workspaces())
    _ensure_current_workspace(items)
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
    _ensure_current_workspace(_without_default_if_real_workspaces_exist(repo.list_workspaces()))
    workspace = repo.find_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"workspace '{workspace_id}' not found")

    repo.set_current_workspace(workspace["workspaceId"])
    _save_current_workspace_id(workspace["workspaceId"])
    # Switch the live runtime too, not just the persisted "current workspace".
    # Without this the runtime stays attached to the previous workspace until the
    # next chat message happens to call set_workspace, so every piece of
    # workspace-scoped runtime state keeps serving the OLD workspace: the RAG
    # store, the screen monitor, and the proactive screen-assist event buffer
    # (get_events_since filters by runtime.workspace_id). That stale id is why
    # the previous workspace's 실시간 보조 cards lingered in the 문서 보조 list
    # after switching. set_workspace is a no-op when already on this workspace.
    try:
        from .agent_runtime import get_runtime

        get_runtime().set_workspace(workspace["workspaceId"])
    except HTTPException:
        # Runtime unavailable (e.g. llama-server down): the persisted switch
        # above still stands and the runtime adopts it on next use.
        pass
    return {"workspaceId": workspace["workspaceId"], "name": workspace["name"]}


def remember_current_workspace(workspace_id: str) -> None:
    if not workspace_id:
        return
    _save_current_workspace_id(workspace_id)


def delete_workspace(workspace_id: str) -> dict[str, Any]:
    """Remove a workspace from disk and from the local SQLite DB.

    1. If the runtime is currently attached to this workspace, switch it
       to a fallback first so we don't delete the directory out from under
       an active registry.
    2. Delegate the filesystem + DB row removal to
       :func:`db.workspace_sync.delete_workspace`.
    3. Drop the in-memory repo cache for this workspace so subsequent
       dashboard refreshes don't resurrect it.
    """
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise HTTPException(status_code=422, detail="workspaceId must not be empty")

    _sync_run_workspaces()

    from .agent_runtime import get_runtime
    from ..api_common import STATE

    runs_root = Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()

    try:
        runtime = get_runtime()
        if getattr(runtime, "workspace_id", None) == workspace_id:
            runtime.set_workspace(_pick_fallback_workspace(workspace_id))
    except HTTPException:
        # If the runtime can't be acquired, proceed with DB+disk removal.
        pass

    result = _delete_workspace_from_db(workspace_id, runs_root=runs_root)

    STATE["workspaces"] = [
        item for item in STATE.get("workspaces", []) if item.get("workspaceId") != workspace_id
    ]
    if STATE.get("current_workspace_id") == workspace_id:
        fallback = _pick_fallback_workspace(workspace_id)
        STATE["current_workspace_id"] = fallback
        STATE["ui_state"]["workspaceId"] = fallback
        STATE["ui_state"]["workspaceName"] = next(
            (item["name"] for item in STATE["workspaces"] if item.get("workspaceId") == fallback),
            fallback,
        )

    return {
        "workspaceId": workspace_id,
        "name": result.get("name", workspace_id),
        "diskRemoved": bool(result.get("diskRemoved")),
        "diskError": result.get("diskError"),
    }


def _pick_fallback_workspace(excluded_workspace_id: str) -> str:
    """Return the most recent remaining real workspace id, or ``"default"``."""
    items = [
        item
        for item in repo.list_workspaces()
        if item.get("workspaceId") and item["workspaceId"] != excluded_workspace_id
    ]
    items.sort(key=lambda w: str(w.get("lastWorkedAt") or ""), reverse=True)
    for item in items:
        workspace_id = str(item.get("workspaceId") or "")
        if workspace_id and workspace_id != "default":
            return workspace_id
    return "default"


def _sync_run_workspaces() -> None:
    workspaces = _scan_run_workspaces()
    for workspace in workspaces:
        repo.upsert_workspace(workspace)
    _persist_workspace_catalog(workspaces)


def _scan_run_workspaces() -> list[dict[str, Any]]:
    root = Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()
    if not root.exists():
        return []

    # The on-disk folder name is only the *initial* display name. A user can
    # rename a workspace (dashboard → 이름 변경), which persists to the
    # workspaces.name column; re-syncing from disk must preserve that rather
    # than reset it to the folder name. Seed the scan with the saved names and
    # fall back to the folder name only for workspaces not yet in the DB.
    persisted_names = _load_persisted_workspace_names()

    workspaces: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_dir() or path.name.startswith("_") or path.name == "__pycache__":
            continue

        summary_dir = path / "summary"
        final_path = path / "final.md"
        index_path = summary_dir / "index.json"
        request_path = summary_dir / "request.md"
        has_summaries = summary_dir.exists() and any(summary_dir.glob("doc_*.md"))
        # `request.md` is materialized by `_publish_new_workspace` as soon
        # as the workspace dir is reserved, so a workspace that's currently
        # being researched also shows up here (with status="running").
        has_request = request_path.exists()
        if (
            not final_path.exists()
            and not index_path.exists()
            and not has_summaries
            and not has_request
        ):
            continue

        document_count = _document_count(index_path)
        workspaces.append(
            {
                "workspaceId": path.name,
                "name": persisted_names.get(path.name) or path.name,
                "detail": f"documents {document_count} · {path}",
                "status": "completed" if final_path.exists() else "running",
                "lastWorkedAt": _mtime_iso(final_path if final_path.exists() else path),
                "path": str(path),
            }
        )
    return workspaces


def _without_default_if_real_workspaces_exist(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    real_items = [item for item in items if item.get("workspaceId") != "default"]
    return real_items or items


def _ensure_current_workspace(items: list[dict[str, Any]]) -> None:
    if not items:
        if repo.find_workspace("default") is None:
            repo.upsert_workspace(
                {
                    "workspaceId": "default",
                    "name": "default",
                    "detail": "기본 워크스페이스",
                    "status": "active",
                }
            )
        repo.set_current_workspace("default")
        return

    workspace_ids = {str(item.get("workspaceId")) for item in items}
    current_workspace_id = repo.get_current_workspace_id()
    persisted_workspace_id = _load_current_workspace_id()
    selected_workspace_id = current_workspace_id

    if persisted_workspace_id in workspace_ids:
        selected_workspace_id = persisted_workspace_id
    elif current_workspace_id not in workspace_ids or current_workspace_id == "default":
        selected_workspace_id = str(items[0].get("workspaceId"))

    if selected_workspace_id and selected_workspace_id in workspace_ids:
        repo.set_current_workspace(selected_workspace_id)
        _save_current_workspace_id(selected_workspace_id)


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


def _load_persisted_workspace_names() -> dict[str, str]:
    """Display names already saved in SQLite, keyed by workspace id.

    Used by :func:`_scan_run_workspaces` so a user's rename survives a disk
    re-sync. Read-only and best-effort — any failure yields an empty map, in
    which case the scan falls back to folder names (the pre-rename behavior).
    """
    try:
        init_db()
        conn = get_connection()
        try:
            rows = conn.execute("SELECT id, name FROM workspaces").fetchall()
            return {
                str(row["id"]): str(row["name"])
                for row in rows
                if row["id"] is not None and str(row["name"] or "").strip()
            }
        finally:
            conn.close()
    except Exception:
        return {}


def _load_current_workspace_id() -> str | None:
    try:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT value FROM app_state WHERE key = ?", ("current_workspace_id",)).fetchone()
            return str(row["value"]) if row and row["value"] else None
        finally:
            conn.close()
    except Exception:
        return None


def _save_current_workspace_id(workspace_id: str) -> None:
    try:
        init_db()
        conn = get_connection()
        try:
            conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                ("current_workspace_id", workspace_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _persist_workspace_catalog(workspaces: list[dict[str, Any]]) -> None:
    if not workspaces:
        return
    try:
        init_db()
        conn = get_connection()
        try:
            for workspace in workspaces:
                workspace_id = str(workspace.get("workspaceId") or "").strip()
                if not workspace_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO workspaces (id, name, path, status, created_at, updated_at, last_worked_at)
                    VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        path = excluded.path,
                        status = excluded.status,
                        updated_at = excluded.updated_at,
                        last_worked_at = excluded.last_worked_at
                    """,
                    (
                        workspace_id,
                        str(workspace.get("name") or workspace_id),
                        str(workspace.get("path") or ""),
                        str(workspace.get("status") or "active"),
                        workspace.get("lastWorkedAt"),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
