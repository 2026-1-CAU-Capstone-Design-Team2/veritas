from __future__ import annotations

from .db import get_connection


def get_dashboard_summary() -> dict[str, int]:
	conn = get_connection()
	try:
		row = conn.execute(
			"""
			SELECT
				COUNT(CASE WHEN status IN ('validated', 'feedback_completed', 'completed') THEN 1 END) AS processed_docs,
				COUNT(*) AS total_docs,
				COUNT(CASE WHEN status IN ('feedback_completed', 'completed') THEN 1 END) AS feedback_completed_docs
			FROM documents
			"""
		).fetchone()

		workspace_row = conn.execute(
			"""
			SELECT COUNT(*) AS validated_workspaces
			FROM workspaces
			WHERE status = 'validated'
			"""
		).fetchone()

		return {
			"processed_docs": int(row["processed_docs"] or 0),
			"total_docs": int(row["total_docs"] or 0),
			"feedback_completed_docs": int(row["feedback_completed_docs"] or 0),
			"validated_workspaces": int(workspace_row["validated_workspaces"] or 0),
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

