"""Archival memory storage backed by SQLite FTS5."""

from __future__ import annotations

from core.memory.models import MemoryItem
from services.memory_tools_funcs.external_context.fts_memory_store import FtsMemoryStore
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class ArchivalStorage(FtsMemoryStore):
    """Persistent archival storage for durable memory records."""

    def __init__(
        self,
        store: MemoryStore,
        token_counter: TokenCounter | None = None,
    ) -> None:
        super().__init__(
            store=store,
            legacy_path=store.archival_path,
            legacy_db_path=store.archival_db_path,
            table_name="archival_items",
            fts_name="archival_fts",
            default_tier="archival",
            migration_key="archival_migrated",
            token_counter=token_counter,
        )

    def insert(self, item: MemoryItem) -> None:
        """Insert an archival item into SQLite."""
        self.add(item)
