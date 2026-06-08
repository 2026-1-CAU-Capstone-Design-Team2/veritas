from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .db import get_connection, init_db

_DRAFT_SETTINGS_RE = re.compile(r"^draft_(\d+)_settings\.json$")


def get_dashboard_summary() -> dict[str, int]:
	# Self-initialize like the other db/ repositories — the frontend no longer
	# calls init_db() (it now reaches this over HTTP), so the API process must
	# not depend on a prior init for the dashboard's first read.
	init_db()
	conn = get_connection()
	try:
		row = conn.execute(
			"""
			SELECT
				COUNT(CASE WHEN status IN ('validated', 'feedback_completed', 'completed') THEN 1 END) AS processed_docs,
				COUNT(*) AS total_docs,
				COUNT(CASE WHEN status = 'feedback_completed' THEN 1 END) AS feedback_completed_docs
			FROM documents
			"""
		).fetchone()

		# A workspace counts as "validated" when its on-disk verification
		# artefact directory contains a meta.json marker. The `documents` table
		# status approach would require create_verify_job hooks to have fired
		# during this session; disk-based detection works for all runs.
		ws_rows = conn.execute(
			"SELECT path FROM workspaces WHERE path IS NOT NULL AND path != ''"
		).fetchall()
		validated_workspaces = sum(
			1
			for r in ws_rows
			if (Path(str(r["path"])) / "verification" / "meta.json").exists()
		)

		return {
			"processed_docs": int(row["processed_docs"] or 0),
			"total_docs": int(row["total_docs"] or 0),
			"feedback_completed_docs": int(row["feedback_completed_docs"] or 0),
			"validated_workspaces": validated_workspaces,
		}
	finally:
		conn.close()


def get_recent_workspaces(limit: int = 5) -> list[dict[str, object]]:
	conn = get_connection()
	try:
		rows = conn.execute(
			"""
			SELECT id, name, last_worked_at
			FROM workspaces
			WHERE last_worked_at IS NOT NULL
			ORDER BY datetime(last_worked_at) DESC
			LIMIT ?
			""",
			(limit,),
		).fetchall()
		return [dict(row) for row in rows]
	finally:
		conn.close()


def get_recent_drafts(limit: int = 5) -> list[dict[str, object]]:
	"""Most recently written built-in drafts across all workspaces.

	Drafts live on disk as ``<workspace path>/drafts/draft_<n>_settings.json``
	(see ``api.services.draft_service``). The workspace's on-disk location is
	the ``path`` column of the ``workspaces`` table, so this reads draft
	metadata straight from those folders, newest ``updatedAt`` first. Best-effort
	per file: an unreadable or malformed settings file is skipped.
	"""
	try:
		conn = get_connection()
		try:
			rows = conn.execute(
				"SELECT id, name, path FROM workspaces WHERE path IS NOT NULL AND path != ''"
			).fetchall()
		finally:
			conn.close()
	except Exception:
		return []

	drafts: list[dict[str, object]] = []
	for row in rows:
		workspace_path = str(row["path"] or "").strip()
		if not workspace_path:
			continue
		drafts_dir = Path(workspace_path) / "drafts"
		if not drafts_dir.exists():
			continue
		for settings_path in drafts_dir.glob("draft_*_settings.json"):
			match = _DRAFT_SETTINGS_RE.match(settings_path.name)
			if not match:
				continue
			try:
				record = json.loads(settings_path.read_text(encoding="utf-8"))
			except Exception:
				continue
			if not isinstance(record, dict):
				continue
			number = int(match.group(1))
			drafts.append(
				{
					"workspace_id": row["id"],
					"workspace_name": row["name"],
					"draft_number": number,
					"title": record.get("title") or record.get("docType") or f"초안 {number}",
					"updated_at": record.get("updatedAt") or record.get("createdAt") or "",
					"path": str(drafts_dir / f"draft_{number}.md"),
				}
			)
	drafts.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
	return drafts[:limit]


def get_recent_activities(limit: int = 5) -> list[dict[str, object]]:
	conn = get_connection()
	try:
		rows = conn.execute(
			"""
			SELECT action, description, created_at
			FROM activity_logs
			ORDER BY datetime(created_at) DESC
			LIMIT ?
			""",
			(limit,),
		).fetchall()
		return [dict(row) for row in rows]
	finally:
		conn.close()


def rename_workspace(workspace_id: str, name: str) -> int:
	"""Rename a workspace in the local ``workspaces`` table. Returns the number
	of rows updated (0 when the id was not found)."""
	init_db()
	conn = get_connection()
	try:
		updated = conn.execute(
			"UPDATE workspaces SET name = ?, updated_at = ? WHERE id = ?",
			(
				name,
				datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
				workspace_id,
			),
		).rowcount
		conn.commit()
		return int(updated or 0)
	finally:
		conn.close()

