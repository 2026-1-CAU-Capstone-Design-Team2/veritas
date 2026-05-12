from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .schema import create_schema

APP_NAME = "VERITAS"
DB_NAME = "veritas.db"


def get_app_data_dir() -> Path:
	local_app_data = os.environ.get("LOCALAPPDATA")
	if local_app_data:
		return Path(local_app_data) / APP_NAME

	if os.name == "nt":
		return Path.home() / "AppData" / "Local" / APP_NAME

	return Path.home() / ".local" / "share" / APP_NAME


def get_db_path() -> Path:
	return get_app_data_dir() / DB_NAME


def get_connection() -> sqlite3.Connection:
	db_path = get_db_path()
	db_path.parent.mkdir(parents=True, exist_ok=True)

	conn = sqlite3.connect(db_path)
	conn.row_factory = sqlite3.Row
	conn.execute("PRAGMA journal_mode=WAL;")
	conn.execute("PRAGMA foreign_keys=ON;")
	return conn


def init_db() -> Path:
	conn = get_connection()
	try:
		create_schema(conn)
		conn.commit()
	finally:
		conn.close()
	return get_db_path()


def seed_demo_data() -> None:
	"""Insert dashboard preview data only when the local database is empty."""
	conn = get_connection()
	try:
		existing = conn.execute("SELECT COUNT(*) AS count FROM documents").fetchone()["count"]
		if existing:
			return

		now = datetime.now()
		workspaces = [
			("ws_demo_001", "AI 안전성 브리프 워크스페이스", "C:/VERITAS/workspaces/ai-safety", "validated", now - timedelta(minutes=10)),
			("ws_demo_002", "규제 대응 메모 워크스페이스", "C:/VERITAS/workspaces/regulatory-memo", "validated", now - timedelta(minutes=35)),
			("ws_demo_003", "기후 정책 검증 워크스페이스", "C:/VERITAS/workspaces/climate-policy", "validated", now - timedelta(hours=1)),
			("ws_demo_004", "시장 동향 분석 워크스페이스", "C:/VERITAS/workspaces/market", "validated", now - timedelta(hours=4)),
			("ws_demo_005", "공급망 리스크 워크스페이스", "C:/VERITAS/workspaces/supply-chain", "validated", now - timedelta(days=1)),
			("ws_demo_006", "보안 정책 워크스페이스", "C:/VERITAS/workspaces/security", "validated", now - timedelta(days=2)),
		]
		for workspace_id, name, path, status, worked_at in workspaces:
			created_at = (worked_at - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
			updated_at = worked_at.strftime("%Y-%m-%d %H:%M:%S")
			conn.execute(
				"""
				INSERT INTO workspaces (id, name, path, status, created_at, updated_at, last_worked_at)
				VALUES (?, ?, ?, ?, ?, ?, ?)
				""",
				(workspace_id, name, path, status, created_at, updated_at, updated_at),
			)

		statuses = ["feedback_completed"] * 48 + ["draft"]
		for index, status in enumerate(statuses, start=1):
			workspace_id = workspaces[index % len(workspaces)][0]
			created_at = (now - timedelta(days=index)).strftime("%Y-%m-%d %H:%M:%S")
			updated_at = (now - timedelta(hours=index)).strftime("%Y-%m-%d %H:%M:%S")
			title = "2026_Q2_리스크_브리프.docx" if index == 1 else f"데모 문서 {index:02d}"
			conn.execute(
				"""
				INSERT INTO documents (id, workspace_id, title, file_path, document_type, status, created_at, updated_at)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?)
				""",
				(f"doc_demo_{index:03d}", workspace_id, title, f"C:/VERITAS/docs/{title}", "docx", status, created_at, updated_at),
			)

		activities = [
			("ws_demo_001", "doc_demo_001", "feedback_completed", "2026_Q2_리스크_브리프.docx 피드백 완료", now - timedelta(minutes=5)),
			("ws_demo_002", "doc_demo_002", "draft_created", "규제 대응 메모 초안 v3 생성", now - timedelta(minutes=18)),
			("ws_demo_003", "doc_demo_003", "document_uploaded", "시장동향_요약_0406.pdf 업로드", now - timedelta(minutes=42)),
			("ws_demo_003", "doc_demo_004", "validation_completed", "기후 정책 검증 완료", now - timedelta(hours=2)),
			("ws_demo_001", None, "workspace_opened", "AI 안전성 브리프 워크스페이스 열림", now - timedelta(hours=3)),
		]
		for workspace_id, document_id, action, description, created_at in activities:
			conn.execute(
				"""
				INSERT INTO activity_logs (workspace_id, document_id, action, description, created_at)
				VALUES (?, ?, ?, ?, ?)
				""",
				(workspace_id, document_id, action, description, created_at.strftime("%Y-%m-%d %H:%M:%S")),
			)

		conn.execute(
			"""
			INSERT INTO feedbacks (document_id, status, content_path, created_at)
			VALUES (?, ?, ?, ?)
			""",
			("doc_demo_001", "completed", "C:/VERITAS/feedback/2026_Q2_리스크_브리프.md", now.strftime("%Y-%m-%d %H:%M:%S")),
		)
		conn.commit()
	finally:
		conn.close()
