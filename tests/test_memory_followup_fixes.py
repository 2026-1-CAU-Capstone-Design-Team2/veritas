from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryItem, MemoryRole, MemoryTier
from core.memory.request import CallRequest
from services.memory_tools_funcs.context_builder import build_messages
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.fifo_storage import FifoStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager, utc_now_iso
from services.memory_tools_funcs.main_context.working_context import WorkingContextManager
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


def _recall_item(item_id: str, content: str) -> MemoryItem:
    return MemoryItem(
        id=item_id,
        tier=MemoryTier.RECALL,
        role=MemoryRole.USER,
        content=content,
        source="test",
        created_at=utc_now_iso(),
        token_count=3,
    )


class RecallDedupUnderNoHistoryTests(unittest.TestCase):
    """Finding 1: recall must not be deduped against FIFO rows that are not injected."""

    def test_recall_survives_when_use_history_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = TokenCounter()
            recall = RecallStorage(store)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)
            queue.append_event(
                role=MemoryRole.USER,
                content="alpha recent useful memory",
                source="test",
            )

            messages = build_messages(
                req=CallRequest(
                    task_instruction="system",
                    user_content="question",
                    record_content="alpha",
                    use_history=False,
                ),
                budget=MemoryBudget(max_context_tokens=4096, reserve_output_tokens=0),
                store=store,
                working=working,
                queue=queue,
            )

            system = str(messages[0]["content"])
            self.assertIn("Retrieved Recall Context", system)
            self.assertIn("alpha recent useful memory", system)

    def test_recall_deduped_against_injected_fifo_when_use_history_true(self) -> None:
        # The same row appears both as recent FIFO history and as a recall hit;
        # with use_history=True the FIFO copy is injected, so recall must drop it.
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = TokenCounter()
            recall = RecallStorage(store)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)
            queue.append_event(
                role=MemoryRole.USER,
                content="alpha recent useful memory",
                source="test",
            )

            messages = build_messages(
                req=CallRequest(
                    task_instruction="system",
                    user_content="question",
                    record_content="alpha",
                    use_history=True,
                ),
                budget=MemoryBudget(max_context_tokens=4096, reserve_output_tokens=0),
                store=store,
                working=working,
                queue=queue,
            )

            system = str(messages[0]["content"])
            self.assertNotIn("Retrieved Recall Context", system)


class SearchScanTests(unittest.TestCase):
    """Steady-state search must not read legacy JSONL."""

    def test_recall_search_does_not_full_scan_when_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            recall = RecallStorage(store)
            for index in range(10):
                recall.append(_recall_item(f"r{index}", f"alpha memory {index}"))

            scan_calls = {"n": 0}
            real_read = store.read_jsonl

            def counting_read(path):
                scan_calls["n"] += 1
                return real_read(path)

            store.read_jsonl = counting_read  # type: ignore[method-assign]
            try:
                results = recall.search("alpha", limit=3)
            finally:
                store.read_jsonl = real_read  # type: ignore[method-assign]

            self.assertTrue(results)
            self.assertEqual(scan_calls["n"], 0)

    def test_recall_external_jsonl_change_after_marker_is_ignored(self) -> None:
        # Phase A makes SQLite the single source. Once a DB has been created and
        # migration is marked complete, out-of-band legacy JSONL is renamed away
        # and must not change search results.
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            recall = RecallStorage(store)
            recall.append(_recall_item("a", "alpha sqlite row"))
            store.append_jsonl(
                store.recall_path,
                store.item_to_dict(_recall_item("b", "beta jsonl only row")),
            )

            restarted = RecallStorage(store)
            results = restarted.search("beta jsonl", limit=1)

            self.assertEqual(results, [])
            self.assertFalse(store.recall_path.exists())
            self.assertTrue(Path(f"{store.recall_path}.migrated").exists())


class FifoLegacyMigrationTests(unittest.TestCase):
    """Finding 3: legacy migration is marker-gated, not file-existence/count gated."""

    def _legacy_row(self, store: MemoryStore, item_id: str, content: str) -> None:
        store.append_jsonl(
            store.fifo_path,
            {
                "id": item_id,
                "tier": "fifo",
                "role": "user",
                "content": content,
                "source": "test",
                "created_at": utc_now_iso(),
                "token_count": 1,
            },
        )

    def test_partial_migration_recovers(self) -> None:
        # An empty DB (schema only, no marker) left by a crashed migration must
        # not permanently shadow the legacy JSONL.
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            for index in range(3):
                self._legacy_row(store, f"f{index}", f"m{index}")

            seed = FifoStorage(store)
            with closing(sqlite3.connect(str(store.fifo_db_path))) as conn:
                seed._ensure_schema(conn)  # empty DB, no migration marker

            recovered = FifoStorage(store)
            rows = recovered.all()
            self.assertEqual(len(rows), 3)
            self.assertEqual([r["content"] for r in rows], ["m0", "m1", "m2"])

    def test_compaction_not_undone_after_restart(self) -> None:
        # After migration + compaction, SQLite has fewer rows than legacy JSONL.
        # A restart must NOT re-import the evicted rows.
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            for index in range(5):
                self._legacy_row(store, f"f{index}", f"m{index}")

            first = FifoStorage(store)
            self.assertEqual(len(first.all()), 5)  # migrates + marks done
            self.assertFalse(store.fifo_path.exists())
            self.assertTrue(Path(f"{store.fifo_path}.migrated").exists())
            first.delete_ids({"f0", "f1", "f2"})  # compaction

            restarted = FifoStorage(store)
            rows = restarted.all()
            self.assertEqual(len(rows), 2)
            self.assertEqual([r["content"] for r in rows], ["m3", "m4"])


if __name__ == "__main__":
    unittest.main()
