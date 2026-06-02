from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryItem, MemoryRole, MemoryTier
from core.memory.request import CallRequest
from services.memory_tools_funcs.context_builder import build_messages
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager, utc_now_iso
from services.memory_tools_funcs.main_context.working_context import WorkingContextManager
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class _WordCounter:
    def count(self, text: str) -> int:
        return len(str(text or "").split())


class RecallStorageTests(unittest.TestCase):
    def _item(self, content: str, *, item_id: str = "item-1") -> MemoryItem:
        return MemoryItem(
            id=item_id,
            tier=MemoryTier.RECALL,
            role=MemoryRole.USER,
            content=content,
            source="test",
            created_at=utc_now_iso(),
            token_count=TokenCounter().count(content),
        )

    def test_empty_search_does_not_create_memory_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            recall = RecallStorage(store)

            self.assertEqual(recall.search("anything"), [])
            self.assertFalse(store.memory_dir.exists())

    def test_append_writes_only_sqlite_and_searches_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            recall = RecallStorage(store)

            recall.append(self._item("project alpha uses local qwen memory", item_id="a"))
            recall.append(self._item("unrelated beta note", item_id="b"))

            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.recall_db_path.exists())
            self.assertFalse(store.recall_path.exists())
            results = recall.search("alpha qwen", limit=1)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "a")
            self.assertIn("alpha", results[0]["content"])

    def test_append_sqlite_failure_raises_without_jsonl_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            recall = RecallStorage(store)

            def fail_append(_row):
                raise RuntimeError("sqlite unavailable")

            recall._append_sqlite = fail_append
            with self.assertRaisesRegex(RuntimeError, "sqlite unavailable"):
                recall.append(self._item("alpha should not be mirrored", item_id="fail"))

            self.assertFalse(store.recall_path.exists())

    def test_search_migrates_legacy_jsonl_to_sqlite_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            row = store.item_to_dict(self._item("legacy jsonl alpha memory", item_id="legacy"))
            row["token_count"] = 999
            store.append_jsonl(store.recall_path, row)
            self.assertFalse(store.db_path.exists())
            self.assertFalse(store.recall_db_path.exists())

            recall = RecallStorage(store, _WordCounter())
            results = recall.search("alpha", limit=1)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "legacy")
            self.assertEqual(results[0]["token_count"], 4)
            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.recall_db_path.exists())
            self.assertFalse(store.recall_path.exists())
            self.assertTrue(Path(f"{store.recall_path}.migrated").exists())

    def test_tail_migrates_legacy_jsonl_to_sqlite_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            row = store.item_to_dict(self._item("legacy tail alpha memory", item_id="tail"))
            store.append_jsonl(store.recall_path, row)
            self.assertFalse(store.db_path.exists())
            self.assertFalse(store.recall_db_path.exists())

            recall = RecallStorage(store)
            rows = recall.tail(limit=1)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "tail")
            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.recall_db_path.exists())
            self.assertFalse(store.recall_path.exists())

    def test_partial_migration_recovers_when_marker_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            row = store.item_to_dict(self._item("legacy partial alpha memory", item_id="partial"))
            store.append_jsonl(store.recall_path, row)

            seed = RecallStorage(store)
            with closing(seed._connect()) as conn:
                seed._ensure_schema(conn)

            restarted = RecallStorage(store)
            results = restarted.search("partial alpha", limit=1)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "partial")
            self.assertFalse(store.recall_path.exists())

    def test_marker_ignores_later_legacy_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            recall = RecallStorage(store)
            recall.append(self._item("alpha sqlite row", item_id="sqlite"))
            store.append_jsonl(
                store.recall_path,
                store.item_to_dict(self._item("beta jsonl only row", item_id="jsonl")),
            )

            restarted = RecallStorage(store)
            results = restarted.search("beta jsonl", limit=1)

            self.assertEqual(results, [])
            self.assertFalse(store.recall_path.exists())
            self.assertTrue(Path(f"{store.recall_path}.migrated").exists())

    def test_korean_fts_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            recall = RecallStorage(store)
            recall.append(self._item("프로젝트 alpha 메모리 계층", item_id="ko"))

            results = recall.search("프로젝트 메모리", limit=1)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "ko")

    def test_build_messages_injects_deterministic_recall_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = TokenCounter()
            recall = RecallStorage(store)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)

            recall.append(
                self._item(
                    "user project name is alpha memory layer",
                    item_id="recall-alpha",
                )
            )
            messages = build_messages(
                req=CallRequest(
                    task_instruction="system",
                    user_content="what do you remember about alpha?",
                    record_content="alpha",
                    use_history=True,
                ),
                budget=MemoryBudget(max_context_tokens=4096, reserve_output_tokens=0),
                store=store,
                working=working,
                queue=queue,
            )

            system = str(messages[0]["content"])
            self.assertIn("Retrieved Recall Context", system)
            self.assertIn("alpha memory layer", system)

    def test_recall_context_excludes_recent_fifo_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = TokenCounter()
            recall = RecallStorage(store)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)

            recall.append(self._item("alpha durable profile preference", item_id="old"))
            queue.append_event(
                role=MemoryRole.USER,
                content="alpha recent fifo duplicate",
                source="test",
            )

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
            )

            system = str(messages[0]["content"])
            self.assertIn("Retrieved Recall Context", system)
            self.assertIn("alpha durable profile preference", system)
            self.assertNotIn("alpha recent fifo duplicate", system)
            self.assertIn(
                "alpha recent fifo duplicate",
                [str(m.get("content") or "") for m in messages],
            )

    def test_recall_context_deduplicates_same_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = TokenCounter()
            recall = RecallStorage(store)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)

            recall.append(self._item("alpha duplicate durable note", item_id="dupe-1"))
            recall.append(self._item("alpha duplicate durable note", item_id="dupe-2"))

            messages = build_messages(
                req=CallRequest(
                    task_instruction="system",
                    user_content="what is alpha duplicate?",
                    record_content="alpha duplicate",
                    use_history=True,
                ),
                budget=MemoryBudget(max_context_tokens=4096, reserve_output_tokens=0),
                store=store,
                working=working,
                queue=queue,
            )

            system = str(messages[0]["content"])
            self.assertEqual(system.count("alpha duplicate durable note"), 1)


if __name__ == "__main__":
    unittest.main()
