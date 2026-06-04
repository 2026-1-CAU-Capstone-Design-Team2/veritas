"""Shared workspace memory storage helpers."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from core.memory.models import MemoryItem


class MemoryStore:
    """Memory paths, legacy JSON helpers, and shared SQLite tables."""

    def __init__(self, workspace_root: Path, *, reuse_connection: bool = False) -> None:
        """Set up memory paths without creating directories."""
        self.workspace_root = Path(workspace_root)
        self.reuse_connection = bool(reuse_connection)
        self.memory_dir = self.workspace_root / "memory"

        self.db_path = self.memory_dir / "memory.sqlite3"

        # Legacy Phase A / JSON paths kept only as migration inputs.
        self.working_path = self.memory_dir / "working_context.json"
        self.fifo_path = self.memory_dir / "fifo_queue.jsonl"
        self.fifo_db_path = self.memory_dir / "fifo.sqlite3"
        self.recall_path = self.memory_dir / "recall_storage.jsonl"
        self.recall_db_path = self.memory_dir / "recall.sqlite3"
        self.summaries_path = self.memory_dir / "summaries.jsonl"

        self.invocations_path = self.memory_dir / "invocations.jsonl"
        self._db_lock = threading.RLock()
        self._db_conn: sqlite3.Connection | None = None

    def close(self) -> None:
        """Close the cached SQLite connection, if this store owns one."""
        with self._db_lock:
            conn = self._db_conn
            self._db_conn = None
            if conn is not None:
                conn.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        """Append one JSON object as a JSONL row."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        """Read every valid JSONL row from a file."""
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows

    def _connect(self) -> sqlite3.Connection:
        """Open a new unmanaged SQLite connection.

        Most runtime code should use ``connection()`` so persistent stores can
        reuse the cached connection under the store lock. This method remains
        available for legacy split-DB migration helpers and tests that need an
        independent connection.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _shared_connection(self) -> sqlite3.Connection:
        if self._db_conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=5.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._db_conn = conn
        return self._db_conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a locked SQLite connection for memory.sqlite3 operations."""
        with self._db_lock:
            if self.reuse_connection:
                conn = self._shared_connection()
                try:
                    yield conn
                except Exception:
                    conn.rollback()
                    raise
            else:
                with closing(self._connect()) as conn:
                    try:
                        yield conn
                    except Exception:
                        conn.rollback()
                        raise

    @staticmethod
    def ensure_migration_meta(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def is_migrated(conn: sqlite3.Connection, key: str) -> bool:
        MemoryStore.ensure_migration_meta(conn)
        row = conn.execute(
            "SELECT value FROM migration_meta WHERE key = ?",
            (str(key),),
        ).fetchone()
        return row is not None and str(row[0]) == "1"

    @staticmethod
    def mark_migrated(conn: sqlite3.Connection, key: str) -> None:
        MemoryStore.ensure_migration_meta(conn)
        conn.execute(
            "INSERT OR REPLACE INTO migration_meta (key, value) VALUES (?, '1')",
            (str(key),),
        )

    @staticmethod
    def rename_legacy(path: Path) -> None:
        """Rename a migrated legacy file/db while preserving it as a safety copy."""
        if not path.exists():
            return
        target = Path(f"{path}.migrated")
        if target.exists():
            target = Path(f"{path}.{uuid4().hex}.migrated")
        path.replace(target)

    def append_summary(
        self,
        summary: str,
        *,
        summary_id: str | None = None,
        created_at: str = "",
    ) -> None:
        """Append one recursive summary row to SQLite."""
        self._ensure_summaries_migrated()
        with self.connection() as conn:
            self._ensure_summaries_schema(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO summaries (id, summary, created_at)
                VALUES (?, ?, ?)
                """,
                (summary_id or str(uuid4()), str(summary or ""), str(created_at or "")),
            )
            conn.commit()

    """
        가장 최신의 summary 하나만 SQLite에 저장하는 구조.
        - append_summary()는 새로운 summary로 기존 summary를 덮어 쓴다. 
        - id는 매번 새로 생성하거나, 고유한 summary_id가 주어지면 그걸 사용
        
    """
    def load_latest_summary(self) -> str:
        """Return the latest summary from SQLite."""
        if not self.db_path.exists() and not self.summaries_path.exists():
            return ""
        self._ensure_summaries_migrated()
        with self.connection() as conn:
            self._ensure_summaries_schema(conn)
            row = conn.execute(
                """
                SELECT summary FROM summaries
                ORDER BY created_at DESC, seq DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row["summary"] or "") if row else ""

    def _ensure_summaries_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS summaries (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.ensure_migration_meta(conn)
        conn.commit()

    def _ensure_summaries_migrated(self) -> None:
        if not self.db_path.exists() and not self.summaries_path.exists():
            return
        with self.connection() as conn:
            self._ensure_summaries_schema(conn)
            if self.is_migrated(conn, "summaries_migrated"):
                conn.commit()
                self.rename_legacy(self.summaries_path)
                return
            for row in self.read_jsonl(self.summaries_path):
                summary = str(row.get("summary") or "")
                if not summary:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO summaries (id, summary, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        str(row.get("id") or uuid4()),
                        summary,
                        str(row.get("created_at") or ""),
                    ),
                )
            self.mark_migrated(conn, "summaries_migrated")
            conn.commit()
        self.rename_legacy(self.summaries_path)

    def load_working_context(self) -> str:
        """Return working context as prompt-ready bullet text."""
        return self.format_working_records(self.load_working_records())

    def load_working_records(self) -> list[dict[str, Any]]:
        """Return working context records from SQLite."""
        if not self.db_path.exists() and not self.working_path.exists():
            return []
        self._ensure_working_migrated()
        with self.connection() as conn:
            self._ensure_working_schema(conn)
            rows = conn.execute(
                """
                SELECT id, text, source, confidence, tags_json, updated_at
                FROM working
                ORDER BY seq ASC
                """
            ).fetchall()
        return [self._working_row_to_dict(row) for row in rows]

    def save_working_records(self, records: list[dict[str, Any]]) -> None:
        """Replace working-context records in SQLite."""
        self._ensure_working_migrated()
        normalized: list[dict[str, Any]] = []
        for row in records:
            record = self._normalize_working_record(row)
            if record.get("text"):
                normalized.append(record)
        with self.connection() as conn:
            self._ensure_working_schema(conn)
            conn.execute("DELETE FROM working")
            for seq, row in enumerate(normalized):
                self._insert_working_record(conn, row, seq=seq)
            conn.commit()

    def save_working_context(self, content: str) -> None:
        """Overwrite working context after converting flat text to records."""
        self.save_working_records(self.working_records_from_text(content))

    def _ensure_working_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS working (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                text TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                tags_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )
            """
        )
        self.ensure_migration_meta(conn)
        conn.commit()

    def _ensure_working_migrated(self) -> None:
        if not self.db_path.exists() and not self.working_path.exists():
            return
        with self.connection() as conn:
            self._ensure_working_schema(conn)
            if self.is_migrated(conn, "working_migrated"):
                conn.commit()
                self.rename_legacy(self.working_path)
                return
            for seq, row in enumerate(self._read_legacy_working_records()):
                self._insert_working_record(conn, row, seq=seq)
            self.mark_migrated(conn, "working_migrated")
            conn.commit()
        self.rename_legacy(self.working_path)

    def _read_legacy_working_records(self) -> list[dict[str, Any]]:
        if not self.working_path.exists():
            return []
        try:
            data = json.loads(self.working_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            records: list[dict[str, Any]] = []
            for row in data.get("records", []):
                record = self._normalize_working_record(row)
                if record.get("text"):
                    records.append(record)
            return records
        if isinstance(data, dict):
            return self.working_records_from_text(str(data.get("content") or ""))
        return []

    def _insert_working_record(
        self,
        conn: sqlite3.Connection,
        row: dict[str, Any],
        *,
        seq: int,
    ) -> None:
        record = self._normalize_working_record(row)
        if not record.get("text"):
            return
        conn.execute(
            """
            INSERT OR REPLACE INTO working
                (seq, id, text, source, confidence, tags_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(seq) + 1,
                str(record["id"]),
                str(record["text"]),
                str(record["source"]),
                float(record["confidence"]),
                json.dumps(record["tags"], ensure_ascii=False),
                str(record["updated_at"]),
            ),
        )

    @staticmethod
    def _working_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            tags = json.loads(str(row["tags_json"] or "[]"))
        except Exception:
            tags = []
        return {
            "id": row["id"],
            "text": row["text"],
            "source": row["source"],
            "confidence": float(row["confidence"] or 1.0),
            "tags": tags if isinstance(tags, list) else [],
            "updated_at": row["updated_at"],
        }

    def working_records_from_text(
        self,
        content: str,
        *,
        source: str = "legacy",
        confidence: float = 1.0,
        tags: list[str] | None = None,
        updated_at: str = "",
    ) -> list[dict[str, Any]]:
        """Convert legacy bullet text into working-context records."""
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in str(content or "").splitlines():
            text = raw.strip()
            if not text:
                continue
            if text.startswith("-"):
                text = text[1:].strip()
            key = " ".join(text.split()).casefold()
            if not key or key in seen:
                continue
            records.append(
                {
                    "id": str(uuid4()),
                    "text": text,
                    "source": source,
                    "confidence": float(confidence),
                    "tags": list(tags or []),
                    "updated_at": updated_at,
                }
            )
            seen.add(key)
        return records

    @staticmethod
    def format_working_records(records: list[dict[str, Any]]) -> str:
        """Format working-context records for system prompt injection.

        Each record renders as a ``- {text}`` bullet line.
        """
        lines: list[str] = []
        for row in records:
            text = str(row.get("text") or "").strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)

    @staticmethod
    def _normalize_working_record(row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        text = str(row.get("text") or row.get("content") or "").strip()
        if text.startswith("-"):
            text = text[1:].strip()
        if not text:
            return {}
        try:
            confidence = float(row.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        raw_tags = row.get("tags")
        tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
        return {
            "id": str(row.get("id") or uuid4()),
            "text": text,
            "source": str(row.get("source") or "unknown"),
            "confidence": confidence,
            "tags": tags,
            "updated_at": str(row.get("updated_at") or ""),
        }

    def item_to_dict(self, item: MemoryItem) -> dict[str, Any]:
        """Convert MemoryItem to JSON-serializable dict."""
        data = asdict(item)
        data["tier"] = item.tier.value
        data["role"] = item.role.value
        return data
