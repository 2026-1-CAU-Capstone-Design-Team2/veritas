"""Best-effort writers that feed the dashboard's SQLite tables.

The dashboard reads ``documents`` / ``activity_logs`` / ``feedbacks`` (see
:mod:`db.dashboard_repository`) but only the demo seed used to populate them.
These helpers let the real workflows record what they did so the dashboard
reflects actual usage.

Every function is wrapped so a DB failure never breaks the caller's request —
recording activity is a side effect of the real work, not a precondition for
it (mirrors ``workspaces_service._persist_workspace_catalog``). The ``workspaces``
table is intentionally left alone: it is owned by the disk re-sync in
``workspaces_service`` and any status we wrote there would be clobbered on the
next list/switch. Workspace-level signals are derived from ``documents`` instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from .db import get_connection, init_db


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def record_documents(
    workspace_id: str,
    documents: Iterable[dict[str, Any]],
    *,
    status: str = "completed",
) -> None:
    """Insert one ``documents`` row per collected source document.

    Idempotent: re-recording the same document updates its title/path but
    never downgrades its ``status`` (so a doc already advanced to
    ``validated``/``feedback_completed`` is not reset to ``completed`` when the
    research job is reconstructed from disk on a later run).
    """
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return
    rows: list[tuple[str, str, str, str, str, str, str, str]] = []
    now = _now()
    for index, doc in enumerate(documents, start=1):
        if not isinstance(doc, dict) or doc.get("duplicateOf") or doc.get("duplicate_of"):
            continue
        doc_id = str(doc.get("docId") or doc.get("doc_id") or index).strip() or str(index)
        title = str(doc.get("title") or doc.get("url") or doc_id).strip() or doc_id
        file_path = str(doc.get("url") or doc.get("final_url") or "").strip()
        document_type = str(doc.get("domain") or "web").strip() or "web"
        rows.append(
            (f"{workspace_id}:{doc_id}", workspace_id, title, file_path, document_type, status, now, now)
        )
    if not rows:
        return
    try:
        init_db()
        conn = get_connection()
        try:
            conn.executemany(
                """
                INSERT INTO documents
                    (id, workspace_id, title, file_path, document_type, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    file_path = excluded.file_path,
                    document_type = excluded.document_type,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def update_workspace_documents_status(workspace_id: str, status: str) -> int:
    """Advance every document of a workspace to ``status`` (e.g. ``validated``).

    Returns the number of rows updated (0 when the workspace has no recorded
    documents yet, e.g. a verify run on a workspace researched before this
    feature existed).
    """
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return 0
    try:
        init_db()
        conn = get_connection()
        try:
            updated = conn.execute(
                "UPDATE documents SET status = ?, updated_at = ? WHERE workspace_id = ?",
                (status, _now(), workspace_id),
            ).rowcount
            conn.commit()
            return int(updated or 0)
        finally:
            conn.close()
    except Exception:
        return 0


def record_feedback(document_id: str, *, status: str = "completed", content_path: str | None = None) -> None:
    workspace_id = str(document_id or "").strip()
    if not workspace_id:
        return
    try:
        init_db()
        conn = get_connection()
        try:
            conn.execute(
                """
                INSERT INTO feedbacks (document_id, status, content_path, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (document_id, status, content_path, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def log_activity(
    workspace_id: str,
    action: str,
    description: str,
    *,
    document_id: str | None = None,
) -> None:
    try:
        init_db()
        conn = get_connection()
        try:
            conn.execute(
                """
                INSERT INTO activity_logs (workspace_id, document_id, action, description, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(workspace_id or "").strip() or None, document_id, action, description, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
