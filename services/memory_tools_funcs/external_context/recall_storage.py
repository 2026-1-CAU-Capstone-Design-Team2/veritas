"""Recall storage backed by SQLite FTS5, optionally fused with dense recall."""

from __future__ import annotations

from typing import Any

from core.memory.models import MemoryItem
from services.memory_tools_funcs.external_context.embedding_recall_store import (
    EmbeddingRecallStore,
)
from services.memory_tools_funcs.external_context.fts_memory_store import FtsMemoryStore
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class RecallStorage(FtsMemoryStore):
    """Persistent recall over raw turns: FTS keyword search plus optional dense.

    When an ``EmbeddingRecallStore`` is attached, every turn is mirrored into
    the dense index on append and ``search`` fuses the keyword and dense
    result sets with Reciprocal Rank Fusion. Without one, behavior is the
    plain FTS path of the parent class.
    """

    # RRF damping constant. Rank-based fusion sidesteps the scale mismatch
    # between BM25 ranks and cosine distances; 60 is the standard default,
    # large enough that the top few ranks from each list dominate without one
    # list's tail crowding out the other's head.
    _RRF_K = 60

    def __init__(
        self,
        store: MemoryStore,
        token_counter: TokenCounter | None = None,
        embedding_store: EmbeddingRecallStore | None = None,
    ) -> None:
        super().__init__(
            store=store,
            legacy_path=store.recall_path,
            legacy_db_path=store.recall_db_path,
            table_name="recall_items",
            fts_name="recall_fts",
            default_tier="recall",
            migration_key="recall_migrated",
            token_counter=token_counter,
        )
        self.embedding_store = embedding_store

    def append(self, item: MemoryItem) -> None:
        """Append a recall item to SQLite FTS and the dense index (if attached)."""
        self.add(item)
        if self.embedding_store is not None:
            self.embedding_store.add(item)

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Return recall hits, fusing FTS and dense rankings when dense is on."""
        keyword_hits = super().search(query, limit=limit)
        if self.embedding_store is None:
            return keyword_hits
        dense_hits = self.embedding_store.search(query, limit=max(int(limit), 1))
        if not dense_hits:
            return keyword_hits
        return self._fuse(keyword_hits, dense_hits, limit=limit)

    @classmethod
    def _fuse(
        cls,
        keyword_hits: list[dict[str, Any]],
        dense_hits: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion over two ranked recall-row lists by id."""
        scores: dict[str, float] = {}
        rows: dict[str, dict[str, Any]] = {}
        for ranked in (keyword_hits, dense_hits):
            for rank, row in enumerate(ranked):
                row_id = str(row.get("id") or "").strip()
                if not row_id:
                    continue
                scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (cls._RRF_K + rank + 1)
                rows.setdefault(row_id, row)
        order = sorted(scores, key=lambda rid: scores[rid], reverse=True)
        return [rows[rid] for rid in order[: max(0, int(limit))]]
