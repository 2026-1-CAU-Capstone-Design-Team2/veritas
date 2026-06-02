"""FIFO queue management with recall mirroring."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryItem, MemoryRole, MemoryTier
from core.memory.policy import EvictionPolicy, FIFOTailEvictionPolicy
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.fifo_storage import FifoStorage
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


def utc_now_iso() -> str:
    """Current UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


class QueueManager:
    """Manage FIFO rows and mirror each turn into recall storage."""

    def __init__(
        self,
        store: MemoryStore,
        token_counter: TokenCounter,
        recall: RecallStorage,
    ) -> None:
        self.store = store
        self.token_counter = token_counter
        self.recall = recall
        self.fifo = FifoStorage(store)
        self._fifo_token_total: int | None = None

    def append_event(
        self,
        *,
        role: MemoryRole,
        content: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryItem:
        """Record one turn in FIFO and recall."""
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
        self.fifo.append(self.store.item_to_dict(item))
        if self._fifo_token_total is not None:
            self._fifo_token_total += int(item.token_count or 0)
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

    def total_fifo_tokens(self, limit: int | None = None) -> int:
        """Return FIFO token total, caching the full total after first load."""
        if limit is None and self._fifo_token_total is not None:
            return self._fifo_token_total
        if limit is not None:
            return self.fifo.total_tokens(limit=limit)
        rows = self.fifo.all()
        total = sum(self._row_token_count(row) for row in rows)
        self._fifo_token_total = total
        return total

    def total_memory_tokens(self, budget: MemoryBudget) -> int:
        """Approximate prompt pressure from required text plus stored memory."""
        return (
            int(budget.system_tokens)
            + int(budget.current_request_tokens)
            + self.token_counter.count(self.store.load_working_context())
            + self.token_counter.count(self.store.load_latest_summary())
            + self.total_fifo_tokens()
        )

    def is_warning_pressure(self, budget: MemoryBudget) -> bool:
        """Return whether warning pressure is reached."""
        return self.total_memory_tokens(budget) >= budget.warning_tokens

    def is_flush_pressure(self, budget: MemoryBudget) -> bool:
        """Return whether flush pressure is reached."""
        return self.total_memory_tokens(budget) >= budget.flush_tokens

    def select_evicted_rows(
        self,
        *,
        keep_tail: int = 20,
        budget: MemoryBudget | None = None,
        eviction_policy: EvictionPolicy | None = None,
    ) -> list[dict[str, Any]]:
        """Select FIFO rows to evict using the configured eviction policy."""
        rows = self.fifo.all()
        if not rows:
            return []
        policy = eviction_policy or FIFOTailEvictionPolicy(keep_tail=keep_tail)
        decision = policy.select_eviction_candidates(
            [self._row_to_item(row) for row in rows],
            budget or MemoryBudget(max_context_tokens=4096),
        )
        evict_ids = {item.id for item in decision.evict_items if item.id}
        return [row for row in rows if str(row.get("id") or "") in evict_ids]

    def recent_rows(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent FIFO rows for retrieval de-duplication."""
        limit = int(limit)
        if limit <= 0:
            return []
        return self.fifo.tail(limit=limit)

    def as_chat_messages(
        self,
        *,
        limit: int = 200,
        max_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        """Convert FIFO rows into OpenAI chat messages."""
        rows = self.fifo.tail(limit=limit) if limit is not None else self.fifo.all()
        if max_tokens is not None:
            remaining = max(0, int(max_tokens))
            selected_reversed: list[dict[str, Any]] = []
            for row in reversed(rows):
                token_count = self._row_token_count(row)
                if token_count > remaining:
                    break
                selected_reversed.append(row)
                remaining -= token_count
            rows = list(reversed(selected_reversed))
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

    def reset_fifo_with_summary(
        self,
        summary: str,
        *,
        evicted_rows: list[dict[str, Any]],
    ) -> None:
        """Append summary and compact FIFO without dropping kept rows."""
        evicted_ids = {
            str(row.get("id") or "")
            for row in evicted_rows
            if str(row.get("id") or "")
        }
        self.store.append_summary(
            summary,
            summary_id=str(uuid.uuid4()),
            created_at=utc_now_iso(),
        )
        self.fifo.delete_ids(evicted_ids)
        self._fifo_token_total = self.fifo.total_tokens()

    def _row_token_count(self, row: dict[str, Any]) -> int:
        try:
            token_count = int(row.get("token_count") or 0)
        except (TypeError, ValueError):
            token_count = 0
        if token_count > 0:
            return token_count
        return self.token_counter.count(str(row.get("content") or ""))

    def _row_to_item(self, row: dict[str, Any]) -> MemoryItem:
        try:
            tier = MemoryTier(str(row.get("tier") or MemoryTier.FIFO.value))
        except ValueError:
            tier = MemoryTier.FIFO
        try:
            role = MemoryRole(str(row.get("role") or MemoryRole.USER.value))
        except ValueError:
            role = MemoryRole.USER
        return MemoryItem(
            id=str(row.get("id") or ""),
            tier=tier,
            role=role,
            content=str(row.get("content") or ""),
            source=str(row.get("source") or ""),
            created_at=str(row.get("created_at") or ""),
            token_count=self._row_token_count(row),
            last_accessed_at=row.get("last_accessed_at"),
            access_count=int(row.get("access_count") or 0),
            importance_hint=float(row.get("importance_hint") or 0.0),
            confidence=float(row.get("confidence") or 1.0),
            tags=list(row.get("tags") or []),
            metadata=dict(row.get("metadata") or {}),
        )
