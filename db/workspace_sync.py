"""Workspace ↔ filesystem reconciliation and deletion helpers.

The dashboard's "최근 작업" panel reads workspace rows directly from SQLite
(`db.dashboard_service`). Those rows can drift out of sync with the actual
`runs/<workspace_id>/` directories — e.g. when a user manually deletes a
workspace folder from disk. This module keeps the two in step:

- :func:`reconcile_workspaces_with_disk` runs at app launch (both PySide and
  the FastAPI runtime call it) and prunes any DB rows whose backing folder
  is gone.
- :func:`delete_workspace` performs an atomic-as-possible workspace removal:
  it deletes the `runs/<workspace_id>/` directory and its associated DB rows
  (workspaces, documents for that workspace, activity_logs for that
  workspace) and clears `app_state.current_workspace_id` if it pointed at
  the now-removed workspace.

Demo seed data (whose `path` is a fictitious location outside `runs/`) is
deliberately left alone — only rows whose recorded `path` is inside
`runs_root` are eligible for reconciliation.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .db import get_connection, init_db


def reconcile_workspaces_with_disk(runs_root: Path) -> int:
	"""Remove DB rows for workspaces whose `runs/<id>/` directory is gone.

	Returns the number of workspaces removed. Demo rows (paths outside
	`runs_root`) and rows without a recorded path are preserved.
	"""
	init_db()
	root = Path(runs_root).expanduser().resolve()
	removed = 0
	conn = get_connection()
	try:
		rows = conn.execute(
			"SELECT id, name, path FROM workspaces"
		).fetchall()
		stale_ids: list[str] = []
		for row in rows:
			path_str = str(row["path"] or "").strip()
			if not path_str:
				continue
			try:
				path = Path(path_str).resolve()
			except Exception:
				continue
			if not _is_within(path, root):
				# Not a runs/-managed workspace; leave it.
				continue
			if path.exists() and path.is_dir():
				continue
			stale_ids.append(str(row["id"]))
		for workspace_id in stale_ids:
			_delete_workspace_rows(conn, workspace_id)
			removed += 1
		if removed:
			_clear_current_workspace_if_stale(conn, stale_ids)
		conn.commit()
	finally:
		conn.close()
	return removed


def delete_workspace(workspace_id: str, runs_root: Path) -> dict[str, object]:
	"""Remove a workspace from disk and DB.

	Returns a small status dict so the API layer can pass it back to the UI.
	If the disk removal fails the DB rows are still cleared so the dashboard
	doesn't keep showing a workspace the user explicitly asked to delete.
	"""
	init_db()
	workspace_id = str(workspace_id or "").strip()
	if not workspace_id:
		raise ValueError("workspace_id must not be empty")

	root = Path(runs_root).expanduser().resolve()
	disk_removed = False
	disk_error: str | None = None

	conn = get_connection()
	try:
		row = conn.execute(
			"SELECT id, name, path FROM workspaces WHERE id = ?",
			(workspace_id,),
		).fetchone()
		recorded_path = str(row["path"] or "").strip() if row is not None else ""
		name = str(row["name"] or workspace_id) if row is not None else workspace_id

		candidate = Path(recorded_path) if recorded_path else (root / workspace_id)
		try:
			candidate = candidate.expanduser().resolve()
		except Exception:
			candidate = root / workspace_id

		if _is_within(candidate, root) and candidate.exists() and candidate.is_dir():
			try:
				shutil.rmtree(candidate)
				disk_removed = True
			except Exception as e:
				disk_error = f"{type(e).__name__}: {e}"

		_delete_workspace_rows(conn, workspace_id)
		_clear_current_workspace_if_stale(conn, [workspace_id])
		conn.commit()
	finally:
		conn.close()

	return {
		"workspaceId": workspace_id,
		"name": name,
		"diskRemoved": disk_removed,
		"diskError": disk_error,
	}


def _delete_workspace_rows(conn, workspace_id: str) -> None:
	conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
	conn.execute("DELETE FROM documents WHERE workspace_id = ?", (workspace_id,))
	conn.execute("DELETE FROM activity_logs WHERE workspace_id = ?", (workspace_id,))


def _clear_current_workspace_if_stale(conn, removed_ids: list[str]) -> None:
	if not removed_ids:
		return
	row = conn.execute(
		"SELECT value FROM app_state WHERE key = ?",
		("current_workspace_id",),
	).fetchone()
	if row is None:
		return
	current = str(row["value"] or "").strip()
	if current and current in set(removed_ids):
		conn.execute(
			"DELETE FROM app_state WHERE key = ?",
			("current_workspace_id",),
		)


def _is_within(child: Path, parent: Path) -> bool:
	try:
		child.relative_to(parent)
	except ValueError:
		return False
	return True
