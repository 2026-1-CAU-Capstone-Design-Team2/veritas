"""Recall storage backed by SQLite FTS5."""

from __future__ import annotations

from core.memory.models import MemoryItem
from services.memory_tools_funcs.external_context.fts_memory_store import FtsMemoryStore
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class RecallStorage(FtsMemoryStore):
    """Persistent recall storage for raw conversation turns."""

    def __init__(
        self,
        store: MemoryStore,
        token_counter: TokenCounter | None = None,
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

    def append(self, item: MemoryItem) -> None:
        """Append a recall item to SQLite."""
        self.add(item)
