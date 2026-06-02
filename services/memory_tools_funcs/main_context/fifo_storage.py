"""SQLite-backed FIFO storage in memory.sqlite3."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from services.memory_tools_funcs.store import MemoryStore


class FifoStorage:
    """Persistent FIFO queue backed by the workspace memory SQLite database."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self._legacy_migrated = False

    def append(self, row: dict[str, Any]) -> None:
        """Append one FIFO row."""
        self._ensure_sqlite_from_legacy_if_needed()
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            self._upsert_row(conn, row)
            if (
                not self.store.fifo_path.exists()
                and not self.store.fifo_db_path.exists()
                and not self._is_migration_done(conn)
            ):
                self._mark_migration_done(conn)
            conn.commit()

    def all(self) -> list[dict[str, Any]]:
        """Return every FIFO row in insertion order."""
        if not self._has_storage():
            return []
        self._ensure_sqlite_from_legacy_if_needed()
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            rows = conn.execute("SELECT * FROM fifo_items ORDER BY seq ASC").fetchall()
        return [self._sqlite_row_to_dict(row) for row in rows]

    def tail(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return the latest FIFO rows in chronological order."""
        limit = int(limit)
        if limit <= 0 or not self._has_storage():
            return []
        self._ensure_sqlite_from_legacy_if_needed()
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT * FROM fifo_items
                ORDER BY seq DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._sqlite_row_to_dict(row) for row in reversed(rows)]

    def total_tokens(self, *, limit: int | None = None) -> int:
        """Return FIFO token totals."""
        if not self._has_storage():
            return 0
        self._ensure_sqlite_from_legacy_if_needed()
        if limit is not None:
            return sum(int(row.get("token_count") or 0) for row in self.tail(limit=limit))
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            value = conn.execute(
                "SELECT COALESCE(SUM(token_count), 0) FROM fifo_items"
            ).fetchone()[0]
        return int(value or 0)

    def delete_ids(self, ids: set[str]) -> None:
        """Delete rows by id."""
        clean_ids = [str(item_id) for item_id in ids if str(item_id or "").strip()]
        if not clean_ids or not self._has_storage():
            return
        self._ensure_sqlite_from_legacy_if_needed()
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            conn.executemany("DELETE FROM fifo_items WHERE id = ?", [(item_id,) for item_id in clean_ids])
            conn.commit()

    def count(self) -> int:
        """Return row count."""
        if not self._has_storage():
            return 0
        self._ensure_sqlite_from_legacy_if_needed()
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            value = conn.execute("SELECT COUNT(*) FROM fifo_items").fetchone()[0]
        return int(value or 0)

    def _connect(self) -> sqlite3.Connection:
        """Open an independent connection for tests and legacy utilities."""
        conn = self.store._connect()
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fifo_items (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                tier TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                token_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at TEXT,
                access_count INTEGER NOT NULL DEFAULT 0,
                importance_hint REAL NOT NULL DEFAULT 0.0,
                confidence REAL NOT NULL DEFAULT 1.0,
                tags_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fifo_items_created_at ON fifo_items(created_at)"
        )
        self.store.ensure_migration_meta(conn)
        conn.commit()

    def _upsert_row(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        item_id = str(row.get("id") or "").strip()
        if not item_id:
            return
        tags = row.get("tags")
        metadata = row.get("metadata")
        tags_json = json.dumps(tags if isinstance(tags, list) else [], ensure_ascii=False)
        metadata_json = json.dumps(
            metadata if isinstance(metadata, dict) else {},
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT INTO fifo_items
                (
                    id, tier, role, content, source, created_at, token_count,
                    last_accessed_at, access_count, importance_hint, confidence,
                    tags_json, metadata_json
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tier=excluded.tier,
                role=excluded.role,
                content=excluded.content,
                source=excluded.source,
                created_at=excluded.created_at,
                token_count=excluded.token_count,
                last_accessed_at=excluded.last_accessed_at,
                access_count=excluded.access_count,
                importance_hint=excluded.importance_hint,
                confidence=excluded.confidence,
                tags_json=excluded.tags_json,
                metadata_json=excluded.metadata_json
            """,
            (
                item_id,
                str(row.get("tier") or "fifo"),
                str(row.get("role") or ""),
                str(row.get("content") or ""),
                str(row.get("source") or ""),
                str(row.get("created_at") or ""),
                int(row.get("token_count") or 0),
                row.get("last_accessed_at"),
                int(row.get("access_count") or 0),
                float(row.get("importance_hint") or 0.0),
                float(row.get("confidence") or 1.0),
                tags_json,
                metadata_json,
            ),
        )

    def _ensure_sqlite_from_legacy_if_needed(self) -> None:
        if self._legacy_migrated:
            return
        if (
            not self.store.db_path.exists()
            and not self.store.fifo_db_path.exists()
            and not self.store.fifo_path.exists()
        ):
            self._legacy_migrated = True
            return
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            if self._is_migration_done(conn):
                self._legacy_migrated = True
                self._rename_legacy()
                return
            for row in self._read_legacy_sqlite_rows():
                self._upsert_row(conn, row)
            for row in self.store.read_jsonl(self.store.fifo_path):
                self._upsert_row(conn, row)
            self._mark_migration_done(conn)
            conn.commit()
        self._legacy_migrated = True
        self._rename_legacy()

    def _read_legacy_sqlite_rows(self) -> list[dict[str, Any]]:
        if not self.store.fifo_db_path.exists():
            return []
        try:
            with closing(sqlite3.connect(str(self.store.fifo_db_path))) as conn:
                conn.row_factory = sqlite3.Row
                exists = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'fifo_items'
                    """
                ).fetchone()
                if not exists:
                    return []
                rows = conn.execute("SELECT * FROM fifo_items ORDER BY seq ASC").fetchall()
        except Exception:
            return []
        return [self._sqlite_row_to_dict(row) for row in rows]

    def _is_migration_done(self, conn: sqlite3.Connection) -> bool:
        return self.store.is_migrated(conn, "fifo_migrated")

    def _mark_migration_done(self, conn: sqlite3.Connection) -> None:
        self.store.mark_migrated(conn, "fifo_migrated")

    def _rename_legacy(self) -> None:
        self.store.rename_legacy(self.store.fifo_path)
        self.store.rename_legacy(self.store.fifo_db_path)

    def _has_storage(self) -> bool:
        return (
            self.store.db_path.exists()
            or self.store.fifo_db_path.exists()
            or self.store.fifo_path.exists()
        )

    @staticmethod
    def _sqlite_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            tags = json.loads(str(row["tags_json"] or "[]"))
        except Exception:
            tags = []
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except Exception:
            metadata = {}
        return {
            "id": row["id"],
            "tier": row["tier"],
            "role": row["role"],
            "content": row["content"],
            "source": row["source"],
            "created_at": row["created_at"],
            "token_count": int(row["token_count"] or 0),
            "last_accessed_at": row["last_accessed_at"],
            "access_count": int(row["access_count"] or 0),
            "importance_hint": float(row["importance_hint"] or 0.0),
            "confidence": float(row["confidence"] or 1.0),
            "tags": tags if isinstance(tags, list) else [],
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
