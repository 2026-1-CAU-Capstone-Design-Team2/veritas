from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from core.memory.models import MemoryRole
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.fifo_storage import FifoStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager, utc_now_iso
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class UnifiedSqliteMemoryTests(unittest.TestCase):
    def test_all_memory_tiers_share_memory_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = TokenCounter()
            recall = RecallStorage(store)
            queue = QueueManager(store, counter, recall)

            queue.append_event(
                role=MemoryRole.USER,
                content="alpha fifo recall turn",
                source="test",
            )
            store.append_summary("alpha compacted summary", created_at=utc_now_iso())
            store.save_working_records(
                [
                    {
                        "id": "working-alpha",
                        "text": "alpha working fact",
                        "source": "test",
                        "confidence": 1.0,
                        "tags": ["test"],
                        "updated_at": utc_now_iso(),
                    }
                ]
            )

            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.fifo_db_path.exists())
            self.assertFalse(store.recall_db_path.exists())
            self.assertFalse(store.summaries_path.exists())
            self.assertFalse(store.working_path.exists())

            with closing(sqlite3.connect(str(store.db_path))) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')"
                    )
                }

            self.assertTrue(
                {
                    "fifo_items",
                    "recall_items",
                    "recall_fts",
                    "summaries",
                    "working",
                    "migration_meta",
                }.issubset(tables)
            )
            self.assertEqual(queue.recent_rows(limit=1)[0]["content"], "alpha fifo recall turn")
            self.assertEqual(recall.search("alpha fifo", limit=1)[0]["content"], "alpha fifo recall turn")
            self.assertEqual(store.load_latest_summary(), "alpha compacted summary")
            self.assertEqual(store.load_working_records()[0]["text"], "alpha working fact")

    def test_phase_a_split_sqlite_files_migrate_into_memory_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))

            legacy_fifo = FifoStorage(store)
            store.fifo_db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(str(store.fifo_db_path))) as conn:
                conn.row_factory = sqlite3.Row
                legacy_fifo._ensure_schema(conn)
                legacy_fifo._upsert_row(
                    conn,
                    {
                        "id": "fifo-old",
                        "tier": "fifo",
                        "role": "user",
                        "content": "alpha old fifo row",
                        "source": "test",
                        "created_at": utc_now_iso(),
                        "token_count": 4,
                    },
                )
                conn.commit()

            self._seed_fts_legacy_db(
                store.recall_db_path,
                "recall_items",
                "recall-old",
                "alpha old recall row",
                "recall",
            )

            queue = QueueManager(store, TokenCounter(), RecallStorage(store))

            self.assertEqual(queue.recent_rows(limit=1)[0]["id"], "fifo-old")
            self.assertEqual(queue.recall.search("old recall", limit=1)[0]["id"], "recall-old")
            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.fifo_db_path.exists())
            self.assertFalse(store.recall_db_path.exists())
            self.assertTrue(Path(f"{store.fifo_db_path}.migrated").exists())
            self.assertTrue(Path(f"{store.recall_db_path}.migrated").exists())

    def _seed_fts_legacy_db(
        self,
        path: Path,
        table_name: str,
        item_id: str,
        content: str,
        tier: str,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(path))) as conn:
            conn.execute(
                f"""
                CREATE TABLE {table_name} (
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
                INSERT INTO {table_name}
                    (id, tier, role, content, source, created_at, token_count, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, tier, "user", content, "test", utc_now_iso(), 4, "{}"),
            )
            conn.commit()


if __name__ == "__main__":
    unittest.main()
