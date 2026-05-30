from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryItem, MemoryRole, MemoryTier
from core.memory.request import CallRequest
from services.memory_tools_funcs.context_builder import build_messages
from services.memory_tools_funcs.external_context.archival_storage import ArchivalStorage
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager, utc_now_iso
from services.memory_tools_funcs.main_context.working_context import WorkingContextManager
from services.memory_tools_funcs.llm_tools import build_memory_tool_runner
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class _WordCounter:
    def count(self, text: str) -> int:
        return len(str(text or "").split())


class ArchivalStorageTests(unittest.TestCase):
    def _item(self, content: str, *, item_id: str = "archival-1") -> MemoryItem:
        return MemoryItem(
            id=item_id,
            tier=MemoryTier.ARCHIVAL,
            role=MemoryRole.USER,
            content=content,
            source="test",
            created_at=utc_now_iso(),
            token_count=TokenCounter().count(content),
        )

    def test_empty_search_does_not_create_archival_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            archival = ArchivalStorage(store)

            self.assertEqual(archival.search("anything"), [])
            self.assertFalse(store.archival_dir.exists())

    def test_insert_writes_only_sqlite_and_searches_archival(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            archival = ArchivalStorage(store)

            archival.insert(self._item("alpha archival durable memory", item_id="a"))
            archival.insert(self._item("unrelated beta note", item_id="b"))

            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.archival_db_path.exists())
            self.assertFalse(store.archival_path.exists())
            results = archival.search("alpha durable", limit=1)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "a")
            self.assertIn("alpha archival", results[0]["content"])

    def test_insert_sqlite_failure_raises_without_jsonl_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            archival = ArchivalStorage(store)

            def fail_append(_row):
                raise RuntimeError("sqlite unavailable")

            archival._append_sqlite = fail_append
            with self.assertRaisesRegex(RuntimeError, "sqlite unavailable"):
                archival.insert(self._item("alpha should not be mirrored", item_id="fail"))

            self.assertFalse(store.archival_path.exists())

    def test_search_migrates_legacy_jsonl_to_sqlite_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            row = store.item_to_dict(self._item("legacy archival alpha memory", item_id="legacy"))
            row["token_count"] = 999
            store.append_jsonl(store.archival_path, row)
            self.assertFalse(store.db_path.exists())
            self.assertFalse(store.archival_db_path.exists())

            archival = ArchivalStorage(store, _WordCounter())
            results = archival.search("alpha", limit=1)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "legacy")
            self.assertEqual(results[0]["token_count"], 4)
            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.archival_db_path.exists())
            self.assertFalse(store.archival_path.exists())
            self.assertTrue(Path(f"{store.archival_path}.migrated").exists())

    def test_tail_migrates_legacy_jsonl_to_sqlite_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            row = store.item_to_dict(self._item("legacy archival tail memory", item_id="tail"))
            store.append_jsonl(store.archival_path, row)
            self.assertFalse(store.db_path.exists())
            self.assertFalse(store.archival_db_path.exists())

            archival = ArchivalStorage(store)
            rows = archival.tail(limit=1)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "tail")
            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.archival_db_path.exists())
            self.assertFalse(store.archival_path.exists())

    def test_partial_migration_recovers_when_marker_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            row = store.item_to_dict(
                self._item("legacy archival partial alpha memory", item_id="partial")
            )
            store.append_jsonl(store.archival_path, row)

            seed = ArchivalStorage(store)
            with closing(seed._connect()) as conn:
                seed._ensure_schema(conn)

            restarted = ArchivalStorage(store)
            results = restarted.search("partial alpha", limit=1)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "partial")
            self.assertFalse(store.archival_path.exists())

    def test_marker_ignores_later_legacy_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            archival = ArchivalStorage(store)
            archival.insert(self._item("alpha archival sqlite row", item_id="sqlite"))
            store.append_jsonl(
                store.archival_path,
                store.item_to_dict(self._item("beta archival jsonl only row", item_id="jsonl")),
            )

            restarted = ArchivalStorage(store)
            results = restarted.search("beta jsonl", limit=1)

            self.assertEqual(results, [])
            self.assertFalse(store.archival_path.exists())
            self.assertTrue(Path(f"{store.archival_path}.migrated").exists())

    def test_korean_fts_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            archival = ArchivalStorage(store)
            archival.insert(self._item("프로젝트 alpha 장기 기억", item_id="ko"))

            results = archival.search("프로젝트 기억", limit=1)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "ko")

    def test_build_messages_injects_archival_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = TokenCounter()
            recall = RecallStorage(store)
            archival = ArchivalStorage(store)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)

            archival.insert(self._item("alpha archival durable memory", item_id="a"))
            messages = build_messages(
                req=CallRequest(
                    task_instruction="system",
                    user_content="what is alpha?",
                    record_content="alpha",
                    use_history=True,
                ),
                budget=MemoryBudget(max_context_tokens=4096, reserve_output_tokens=0),
                store=store,
                working=working,
                queue=queue,
                archival=archival,
            )

            system = str(messages[0]["content"])
            self.assertIn("Retrieved Archival Context", system)
            self.assertIn("alpha archival durable memory", system)

    def test_archival_memory_tools_insert_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = TokenCounter()
            archival = ArchivalStorage(store)
            runtime = SimpleNamespace(archival=archival, token_counter=counter)
            runner = build_memory_tool_runner(runtime)

            inserted = runner(
                "archival_insert",
                {"content": "alpha archival tool memory", "source": "test"},
            )
            searched = runner("archival_search", {"query": "alpha tool", "limit": 1})

            self.assertTrue(inserted["ok"])
            self.assertEqual(searched["count"], 1)
            self.assertIn("alpha archival tool memory", searched["matches"][0]["content"])


if __name__ == "__main__":
    unittest.main()
