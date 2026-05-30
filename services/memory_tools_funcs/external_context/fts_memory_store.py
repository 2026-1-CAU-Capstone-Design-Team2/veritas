"""Shared SQLite FTS5 storage for recall and archival memory."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from core.memory.models import MemoryItem
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class FtsMemoryStore:
    """SQLite-only FTS5 store with one-time legacy migration."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        legacy_path: Path,
        legacy_db_path: Path,
        table_name: str,
        fts_name: str,
        default_tier: str,
        migration_key: str,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.store = store
        self.db_path = store.db_path
        self.legacy_path = Path(legacy_path)
        self.legacy_db_path = Path(legacy_db_path)
        self.table_name = self._validate_identifier(table_name)
        self.fts_name = self._validate_identifier(fts_name)
        self.default_tier = str(default_tier or "")
        self.migration_key = str(migration_key or f"{self.default_tier}_migrated")
        self.token_counter = token_counter or TokenCounter()
        self._legacy_migrated = False

    def add(self, item: MemoryItem) -> None:
        """Add one memory item to SQLite."""
        self._ensure_migrated()
        self._append_sqlite(self.store.item_to_dict(item))

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return latest rows in chronological order."""
        limit = int(limit)
        if limit <= 0 or not self._has_storage():
            return []
        self._ensure_migrated()
        return self._tail_sqlite(limit=limit)

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Search rows by SQLite FTS5."""
        query = str(query or "").strip()
        if not query or not self._has_storage():
            return []
        self._ensure_migrated()
        return self._search_sqlite(query, limit=limit)

    def _connect(self) -> sqlite3.Connection:
        """Open an independent connection for tests and legacy utilities."""
        conn = self.store._connect()
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                id TEXT PRIMARY KEY,
                tier TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                token_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{{}}'
            )
            """
        )
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_name}
            USING fts5(id UNINDEXED, content, source, tokenize='unicode61')
            """
        )
        self.store.ensure_migration_meta(conn)
        conn.commit()

    def _append_sqlite(self, row: dict[str, Any]) -> None:
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            self._upsert_row(conn, row)
            if (
                not self.legacy_path.exists()
                and not self.legacy_db_path.exists()
                and not self._is_migration_done(conn)
            ):
                self._mark_migration_done(conn)
            conn.commit()

    def _upsert_row(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        item_id = str(row.get("id") or "").strip()
        if not item_id:
            return
        metadata = row.get("metadata")
        metadata_json = (
            json.dumps(metadata, ensure_ascii=False)
            if isinstance(metadata, dict)
            else "{}"
        )
        conn.execute(
            f"""
            INSERT OR REPLACE INTO {self.table_name}
                (id, tier, role, content, source, created_at, token_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                str(row.get("tier") or self.default_tier),
                str(row.get("role") or ""),
                str(row.get("content") or ""),
                str(row.get("source") or ""),
                str(row.get("created_at") or ""),
                int(row.get("token_count") or 0),
                metadata_json,
            ),
        )
        conn.execute(f"DELETE FROM {self.fts_name} WHERE id = ?", (item_id,))
        conn.execute(
            f"INSERT INTO {self.fts_name} (id, content, source) VALUES (?, ?, ?)",
            (
                item_id,
                str(row.get("content") or ""),
                str(row.get("source") or ""),
            ),
        )

    def _tail_sqlite(self, *, limit: int) -> list[dict[str, Any]]:
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT * FROM {self.table_name}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [self._sqlite_row_to_dict(row) for row in reversed(rows)]

    def _search_sqlite(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        match_query = self._fts_query(query)
        if not match_query:
            return []
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT {self.table_name}.*, bm25({self.fts_name}) AS rank
                FROM {self.fts_name}
                JOIN {self.table_name} ON {self.table_name}.id = {self.fts_name}.id
                WHERE {self.fts_name} MATCH ?
                ORDER BY rank ASC, {self.table_name}.created_at DESC
                LIMIT ?
                """,
                (match_query, max(1, int(limit))),
            ).fetchall()
        return [self._sqlite_row_to_dict(row) for row in rows]

    def _ensure_migrated(self) -> None:
        if self._legacy_migrated:
            return
        if (
            not self.store.db_path.exists()
            and not self.legacy_db_path.exists()
            and not self.legacy_path.exists()
        ):
            self._legacy_migrated = True
            return
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            if self._is_migration_done(conn):
                conn.commit()
                self._legacy_migrated = True
                self._rename_legacy()
                return
            for row in self._read_legacy_sqlite_rows():
                self._upsert_row(conn, row)
            for row in self.store.read_jsonl(self.legacy_path):
                migrated = dict(row)
                migrated["token_count"] = self.token_counter.count(
                    str(migrated.get("content") or "")
                )
                self._upsert_row(conn, migrated)
            self._mark_migration_done(conn)
            conn.commit()
        self._legacy_migrated = True
        self._rename_legacy()

    def _read_legacy_sqlite_rows(self) -> list[dict[str, Any]]:
        if not self.legacy_db_path.exists():
            return []
        try:
            with closing(sqlite3.connect(str(self.legacy_db_path), timeout=5.0)) as conn:
                conn.row_factory = sqlite3.Row
                exists = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (self.table_name,),
                ).fetchone()
                if not exists:
                    return []
                rows = conn.execute(f"SELECT * FROM {self.table_name}").fetchall()
        except Exception:
            return []
        return [self._sqlite_row_to_dict(row) for row in rows]

    def _is_migration_done(self, conn: sqlite3.Connection) -> bool:
        return self.store.is_migrated(conn, self.migration_key)

    def _mark_migration_done(self, conn: sqlite3.Connection) -> None:
        self.store.mark_migrated(conn, self.migration_key)

    def _rename_legacy(self) -> None:
        self.store.rename_legacy(self.legacy_path)
        self.store.rename_legacy(self.legacy_db_path)

    def _has_storage(self) -> bool:
        return (
            self.store.db_path.exists()
            or self.legacy_db_path.exists()
            or self.legacy_path.exists()
        )

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = [
            term
            for term in re.findall(r"[\w가-힣]+", str(query or "").lower())
            if term
        ]
        return " OR ".join(f'"{term}"' for term in terms[:16])

    @staticmethod
    def _sqlite_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
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
            "metadata": metadata if isinstance(metadata, dict) else {},
        }

    @staticmethod
    def _validate_identifier(value: str) -> str:
        text = str(value or "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
            raise ValueError(f"unsafe sqlite identifier: {value!r}")
        return text
