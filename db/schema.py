from __future__ import annotations

SCHEMA_STATEMENTS = [
	"""
	CREATE TABLE IF NOT EXISTS workspaces (
		id TEXT PRIMARY KEY,
		name TEXT NOT NULL,
		path TEXT NOT NULL,
		status TEXT DEFAULT 'active',
		created_at TEXT NOT NULL,
		updated_at TEXT NOT NULL,
		last_worked_at TEXT
	)
	""",
	"""
	CREATE TABLE IF NOT EXISTS documents (
		id TEXT PRIMARY KEY,
		workspace_id TEXT,
		title TEXT NOT NULL,
		file_path TEXT,
		document_type TEXT,
		status TEXT,
		created_at TEXT NOT NULL,
		updated_at TEXT NOT NULL
	)
	""",
	"""
	CREATE TABLE IF NOT EXISTS activity_logs (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		workspace_id TEXT,
		document_id TEXT,
		action TEXT NOT NULL,
		description TEXT,
		created_at TEXT NOT NULL
	)
	""",
	"""
	CREATE TABLE IF NOT EXISTS feedbacks (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		document_id TEXT NOT NULL,
		status TEXT DEFAULT 'completed',
		content_path TEXT,
		created_at TEXT NOT NULL
	)
	""",
	"""
	CREATE TABLE IF NOT EXISTS app_state (
		key TEXT PRIMARY KEY,
		value TEXT NOT NULL,
		updated_at TEXT NOT NULL
	)
	""",
]


def create_schema(conn) -> None:
	for statement in SCHEMA_STATEMENTS:
		conn.execute(statement)

