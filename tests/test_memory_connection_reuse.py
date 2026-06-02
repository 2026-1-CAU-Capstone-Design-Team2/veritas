from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.memory.models import MemoryRole
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager, utc_now_iso
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class MemoryConnectionReuseTests(unittest.TestCase):
    def test_reuse_connection_opens_memory_sqlite_once(self) -> None:
        real_connect = sqlite3.connect
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp), reuse_connection=True)
            calls: list[str] = []

            def counting_connect(path, *args, **kwargs):
                if str(path) == str(store.db_path):
                    calls.append(str(path))
                return real_connect(path, *args, **kwargs)

            try:
                with patch("sqlite3.connect", side_effect=counting_connect):
                    recall = RecallStorage(store)
                    queue = QueueManager(store, TokenCounter(), recall)

                    queue.append_event(
                        role=MemoryRole.USER,
                        content="alpha fifo recall",
                        source="test",
                    )
                    self.assertEqual(queue.recent_rows(limit=1)[0]["content"], "alpha fifo recall")
                    self.assertEqual(recall.search("alpha", limit=1)[0]["content"], "alpha fifo recall")
                    store.append_summary("alpha summary", created_at=utc_now_iso())
                    self.assertEqual(store.load_latest_summary(), "alpha summary")

                self.assertEqual(calls, [str(store.db_path)])
            finally:
                store.close()

    def test_reuse_connection_serializes_concurrent_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp), reuse_connection=True)
            try:
                recall = RecallStorage(store)
                queue = QueueManager(store, TokenCounter(), recall)
                errors: list[BaseException] = []

                def append_many(prefix: str) -> None:
                    try:
                        for index in range(25):
                            queue.append_event(
                                role=MemoryRole.USER,
                                content=f"{prefix}-{index}",
                                source="thread",
                            )
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        errors.append(exc)

                threads = [
                    threading.Thread(target=append_many, args=(f"t{thread_id}",))
                    for thread_id in range(4)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(errors, [])
                self.assertEqual(len(queue.recent_rows(limit=200)), 100)
                self.assertEqual(len(recall.search("t1", limit=50)), 25)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
