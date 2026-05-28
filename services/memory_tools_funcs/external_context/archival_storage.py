"""의도 저장된 사실의 영구 보관소."""

from __future__ import annotations

from typing import Any

from core.memory.models import MemoryItem
from services.memory_tools_funcs.store import MemoryStore


class ArchivalStorage:
    """archival/items.jsonl insert/tail."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def insert(self, item: MemoryItem) -> None:
        """record를 영구 보관한다."""
        self.store.append_jsonl(
            self.store.archival_path,
            self.store.item_to_dict(item),
        )

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """최근 limit개를 반환한다."""
        return self.store.read_jsonl_tail(self.store.archival_path, limit=limit)

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """(미구현) semantic search — 항상 빈 list."""
        del query, limit
        return []
