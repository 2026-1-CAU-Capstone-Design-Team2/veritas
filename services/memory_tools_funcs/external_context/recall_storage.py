"""모든 raw turn의 영구 보관소."""

from __future__ import annotations

from typing import Any

from core.memory.models import MemoryItem
from services.memory_tools_funcs.store import MemoryStore


class RecallStorage:
    """recall_storage.jsonl append/tail."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def append(self, item: MemoryItem) -> None:
        """record를 영구 append한다."""
        self.store.append_jsonl(
            self.store.recall_path,
            self.store.item_to_dict(item),
        )

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """최근 limit개를 반환한다."""
        return self.store.read_jsonl_tail(self.store.recall_path, limit=limit)

    def search(self, query: str, *, limit: int = 5, scan: int = 2000) -> list[dict[str, Any]]:
        """keyword 기반 recall search. scan개 record를 검사하여 score 상위 limit개 반환.

        1차 구현은 단순 단어 단위 일치 점수. semantic search는 후속.
        """
        q_words = [w.lower() for w in str(query or "").split() if w.strip()]
        if not q_words:
            return []
        rows = self.store.read_jsonl_tail(self.store.recall_path, limit=scan)
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            content = str(row.get("content") or "").lower()
            if not content:
                continue
            score = sum(1 for w in q_words if w in content)
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda pair: -pair[0])
        return [row for _, row in scored[:limit]]
