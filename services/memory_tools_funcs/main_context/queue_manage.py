"""FIFO queue + Recall mirror."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryItem, MemoryRole, MemoryTier
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


def utc_now_iso() -> str:
    """UTC 현재 시각 ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


class QueueManager:
    """FIFO와 Recall mirror를 함께 관리"""

    def __init__(
        self,
        store: MemoryStore,
        token_counter: TokenCounter,
        recall: RecallStorage,
    ) -> None:
        self.store = store
        self.token_counter = token_counter
        self.recall = recall

    def append_event(
        self,
        *,
        role: MemoryRole,
        content: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryItem:
        """한 turn을 FIFO + Recall에 동시 기록"""
        item = MemoryItem(
            id=str(uuid.uuid4()),
            tier=MemoryTier.FIFO,
            role=role,
            content=content,
            source=source,
            created_at=utc_now_iso(),
            token_count=self.token_counter.count(content),
            metadata=metadata or {},
        )
        self.store.append_jsonl(self.store.fifo_path, self.store.item_to_dict(item))
        self.recall.append(
            MemoryItem(
                id=item.id,
                tier=MemoryTier.RECALL,
                role=role,
                content=content,
                source=source,
                created_at=item.created_at,
                token_count=item.token_count,
                metadata=item.metadata,
            )
        )
        return item

    def total_fifo_tokens(self, limit: int = 500) -> int:
        """FIFO 누적 token 합."""
        rows = self.store.read_jsonl_tail(self.store.fifo_path, limit=limit)
        return sum(int(row.get("token_count") or 0) for row in rows)

    def is_warning_pressure(self, budget: MemoryBudget) -> bool:
        """warning 임계값 초과 여부."""
        return self.total_fifo_tokens() >= budget.warning_tokens

    def is_flush_pressure(self, budget: MemoryBudget) -> bool:
        """flush 임계값 초과 여부."""
        return self.total_fifo_tokens() >= budget.flush_tokens

    def select_evicted_rows(self, *, keep_tail: int = 20) -> list[dict[str, Any]]:
        """오래된 prefix를 evict 대상으로 고른다 (tail keep_tail개 유지)."""
        rows = self.store.read_jsonl_tail(self.store.fifo_path, limit=2000)
        if len(rows) <= keep_tail:
            return []
        return rows[:-keep_tail]

    def as_chat_messages(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """FIFO를 OpenAI chat messages[] 형식으로 변환한다."""
        rows = self.store.read_jsonl_tail(self.store.fifo_path, limit=limit)
        messages: list[dict[str, Any]] = []
        for row in rows:
            raw_role = str(row.get("role") or "").lower()
            if raw_role == "assistant":
                chat_role = "assistant"
            elif raw_role == "tool":
                chat_role = "tool"
            elif raw_role == "system":
                chat_role = "system"
            else:
                chat_role = "user"
            content = str(row.get("content") or "")
            if not content:
                continue
            messages.append({"role": chat_role, "content": content})
        return messages

    def reset_fifo_with_summary(self, summary: str) -> None:
        """FIFO를 비우고 summary 한 줄을 summaries.jsonl에 추가한다."""
        self.store.truncate(self.store.fifo_path)
        self.store.append_jsonl(
            self.store.summaries_path,
            {
                "id": str(uuid.uuid4()),
                "summary": summary,
                "created_at": utc_now_iso(),
            },
        )
