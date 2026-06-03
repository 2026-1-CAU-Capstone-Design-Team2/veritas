"""Hybrid recall: FTS keyword fused with dense embedding recall (RRF)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.models import MemoryItem, MemoryRole, MemoryTier
from services.memory_tools_funcs.external_context.embedding_recall_store import (
    EmbeddingRecallStore,
)
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import utc_now_iso
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


def _item(content: str, item_id: str, *, role: MemoryRole = MemoryRole.USER) -> MemoryItem:
    return MemoryItem(
        id=item_id,
        tier=MemoryTier.RECALL,
        role=role,
        content=content,
        source="test",
        created_at=utc_now_iso(),
        token_count=TokenCounter().count(content),
    )


class _FakeEmbeddingStore:
    """In-memory stand-in for EmbeddingRecallStore.

    Records adds/backfills and returns a scripted dense ranking for search so
    fusion can be tested without ChromaDB or an embedding endpoint.
    """

    def __init__(self, *, ranking: list[dict] | None = None, fail: bool = False) -> None:
        self.added: list[str] = []
        self.backfilled: list[dict] = []
        self._ranking = ranking or []
        self._fail = fail
        self.closed = False

    def add(self, item: MemoryItem) -> None:
        if self._fail:
            return
        self.added.append(item.id)

    def search(self, query: str, *, limit: int = 5) -> list[dict]:
        if self._fail:
            return []
        return [dict(row) for row in self._ranking[: int(limit)]]

    def count(self) -> int:
        return len(self.added)

    def backfill(self, rows: list[dict]) -> int:
        self.backfilled.extend(rows)
        return len(rows)

    def close(self) -> None:
        self.closed = True


class RrfFusionTests(unittest.TestCase):
    def test_overlap_ranked_first(self) -> None:
        keyword = [{"id": "a", "content": "A"}, {"id": "b", "content": "B"}]
        dense = [{"id": "b", "content": "B"}, {"id": "c", "content": "C"}]

        fused = RecallStorage._fuse(keyword, dense, limit=3)
        ids = [row["id"] for row in fused]

        self.assertEqual(ids[0], "b")  # appears in both lists -> top
        self.assertEqual(set(ids), {"a", "b", "c"})

    def test_dense_only_hit_surfaces(self) -> None:
        keyword = [{"id": "a", "content": "A"}]
        dense = [{"id": "z", "content": "Z"}]

        fused = RecallStorage._fuse(keyword, dense, limit=5)

        self.assertEqual({row["id"] for row in fused}, {"a", "z"})

    def test_limit_truncates(self) -> None:
        keyword = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        dense = [{"id": "d"}, {"id": "e"}]

        fused = RecallStorage._fuse(keyword, dense, limit=2)

        self.assertEqual(len(fused), 2)

    def test_blank_ids_skipped(self) -> None:
        keyword = [{"id": "", "content": "blank"}, {"id": "a", "content": "A"}]
        dense: list[dict] = []

        fused = RecallStorage._fuse(keyword, dense, limit=5)

        self.assertEqual([row["id"] for row in fused], ["a"])


class HybridRecallSearchTests(unittest.TestCase):
    def test_append_mirrors_into_embedding_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeEmbeddingStore()
            recall = RecallStorage(MemoryStore(Path(tmp)), embedding_store=fake)

            recall.append(_item("alpha qwen memory", "a"))

            self.assertEqual(fake.added, ["a"])

    def test_search_without_embedding_is_keyword_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recall = RecallStorage(MemoryStore(Path(tmp)))

            recall.append(_item("alpha qwen memory note", "a"))
            recall.append(_item("unrelated beta content", "b"))
            results = recall.search("alpha qwen", limit=2)

            self.assertEqual([r["id"] for r in results], ["a"])

    def test_search_fuses_keyword_and_dense(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dense_row = {
                "id": "z",
                "tier": "recall",
                "role": "user",
                "content": "completely different semantic neighbor",
                "source": "test",
                "created_at": utc_now_iso(),
                "token_count": 5,
                "metadata": {},
            }
            fake = _FakeEmbeddingStore(ranking=[dense_row])
            recall = RecallStorage(MemoryStore(Path(tmp)), embedding_store=fake)

            recall.append(_item("alpha qwen memory note", "a"))
            results = recall.search("alpha qwen", limit=5)

            ids = {r["id"] for r in results}
            self.assertIn("a", ids)  # keyword hit
            self.assertIn("z", ids)  # dense-only hit fused in

    def test_dense_failure_falls_back_to_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeEmbeddingStore(fail=True)
            recall = RecallStorage(MemoryStore(Path(tmp)), embedding_store=fake)

            recall.append(_item("alpha qwen memory note", "a"))
            results = recall.search("alpha qwen", limit=5)

            self.assertEqual([r["id"] for r in results], ["a"])


class EmbeddingStoreDegradationTests(unittest.TestCase):
    class _NoEmbedLLM:
        """Raw-LLM stub without an ``embed`` method."""

    def test_disabled_when_llm_cannot_embed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EmbeddingRecallStore(Path(tmp), self._NoEmbedLLM())

            store.add(_item("x", "1"))

            self.assertEqual(store.search("x"), [])
            self.assertEqual(store.count(), 0)
            self.assertEqual(store.backfill([{"id": "1", "content": "x"}]), 0)

    def test_no_chromadb_dir_created_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EmbeddingRecallStore(Path(tmp), self._NoEmbedLLM())

            store.add(_item("x", "1"))

            self.assertFalse((Path(tmp) / "chromadb").exists())


class _FakeEmbedLLM:
    """Deterministic word-bucket embedding so EmbeddingRecallStore runs
    against real ChromaDB without an embedding server."""

    def embed(self, text, dim: int = 64):
        import math

        vec = [0.0] * dim
        for word in str(text).lower().split():
            vec[hash(word) % dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class AsyncDenseIndexTests(unittest.TestCase):
    def test_add_indexes_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EmbeddingRecallStore(Path(tmp), _FakeEmbedLLM())
            store.add(_item("samsung galaxy stock outlook", "a"))
            store.flush()  # wait for the worker to drain

            self.assertEqual(store.count(), 1)
            hits = store.search("samsung", limit=1)
            self.assertEqual(hits[0]["id"], "a")
            store.close()

    def test_close_drains_pending_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EmbeddingRecallStore(Path(tmp), _FakeEmbedLLM())
            for i in range(5):
                store.add(_item(f"turn number {i}", f"id{i}"))
            store.close()  # must drain the queue before releasing the handle

            reopened = EmbeddingRecallStore(Path(tmp), _FakeEmbedLLM())
            self.assertEqual(reopened.count(), 5)
            reopened.close()

    def test_disabled_add_never_starts_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EmbeddingRecallStore(
                Path(tmp), EmbeddingStoreDegradationTests._NoEmbedLLM()
            )
            store.add(_item("x", "1"))

            self.assertIsNone(store._worker)
            store.close()


if __name__ == "__main__":
    unittest.main()
