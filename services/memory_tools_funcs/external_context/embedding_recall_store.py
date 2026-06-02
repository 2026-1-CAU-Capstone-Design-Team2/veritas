"""Dense recall index over conversation turns, backed by ChromaDB."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from core.memory.models import MemoryItem


class EmbeddingRecallStore:
    """Semantic recall over turns using ChromaDB cosine search.

    Mirrors every recall turn into a ``recall_turns`` collection so
    RecallStorage can fuse keyword (FTS) and dense (embedding) hits. Each
    turn is one document keyed by its turn id; role/source/created_at ride
    in chroma metadata so a hit reconstructs the same row shape the FTS
    path returns.

    Every method degrades to a no-op (add) or empty result (search) when
    the embedding endpoint or vector store is unavailable, and latches
    ``_disabled`` on the first hard failure so a missing embedding server
    leaves keyword recall fully intact.

    All ChromaDB access is serialized under one RLock. The background dense
    backfill and a live chat turn can both touch the collection at once, and
    ChromaDB's per-process SQLite/handle cache is not safe under concurrent
    init or write; embedding HTTP calls stay outside the lock so a slow
    embed never blocks another thread's ChromaDB access.
    """

    COLLECTION_NAME = "recall_turns"

    # Embedding-input guard. The embedding server rejects inputs over its
    # physical batch size (e.g. 512 tokens), so each text is truncated to a
    # leading prefix — a turn's opening text already represents its topic for
    # retrieval — and halved-and-retried if the server still refuses it.
    _EMBED_CHAR_CAP = 400
    _EMBED_RETRIES = 3
    _EMBED_MIN_CHARS = 80

    def __init__(
        self,
        workspace_root: Path,
        raw_llm: Any,
        *,
        collection_name: str | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.raw_llm = raw_llm
        self.collection_name = collection_name or self.COLLECTION_NAME
        self._store: Any | None = None
        self._disabled = False
        self._lock = threading.RLock()

    def _ensure_store(self) -> Any | None:
        """Lazily open the chroma collection; latch disabled on any failure.

        Caller must hold ``self._lock`` (init is not safe to race)."""
        if self._disabled:
            return None
        if self._store is not None:
            return self._store
        if not callable(getattr(self.raw_llm, "embed", None)):
            self._disabled = True
            return None
        try:
            from storage.vector_store import VectorStore

            self._store = VectorStore(
                persist_dir=self.workspace_root / "chromadb",
                collection_name=self.collection_name,
                embedding_fn=self.raw_llm.embed,
            )
        except Exception as e:
            print(f"[memory][embedding_recall][warn] disabled: {type(e).__name__}: {e}")
            self._disabled = True
            self._store = None
        return self._store

    def _embed_text(self, text: str) -> list[float] | None:
        """Embed a length-capped prefix, shrinking on oversize errors.

        Runs outside the store lock — it is a pure embedding HTTP call."""
        text = str(text or "").strip()[: self._EMBED_CHAR_CAP]
        if not text:
            return None
        for _ in range(self._EMBED_RETRIES):
            try:
                return self.raw_llm.embed(text)
            except Exception as e:
                if len(text) <= self._EMBED_MIN_CHARS:
                    print(f"[memory][embedding_recall][warn] embed failed: {type(e).__name__}: {e}")
                    return None
                text = text[: len(text) // 2]
        return None

    def add(self, item: MemoryItem) -> None:
        """Embed and upsert one turn. No-op when disabled or content empty."""
        content = str(getattr(item, "content", "") or "").strip()
        item_id = str(getattr(item, "id", "") or "").strip()
        if not content or not item_id:
            return
        with self._lock:
            if self._ensure_store() is None:
                return
        vec = self._embed_text(content)
        if vec is None:
            return
        with self._lock:
            store = self._ensure_store()
            if store is None:
                return
            try:
                store.add_document(
                    doc_id=item_id,
                    content=content,
                    embedding=vec,
                    metadata=self._item_metadata(item),
                )
            except Exception as e:
                print(f"[memory][embedding_recall][warn] add failed: {type(e).__name__}: {e}")
                self._disabled = True

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Cosine search, newest-relevant first. Empty list on failure/disable."""
        query = str(query or "").strip()
        if not query or int(limit) <= 0:
            return []
        with self._lock:
            if self._ensure_store() is None:
                return []
        qvec = self._embed_text(query)
        if qvec is None:
            return []
        with self._lock:
            store = self._ensure_store()
            if store is None:
                return []
            try:
                hits = store.query(query_embedding=qvec, n_results=int(limit))
            except Exception as e:
                print(f"[memory][embedding_recall][warn] search failed: {type(e).__name__}: {e}")
                return []
        return [self._hit_to_row(h) for h in hits]

    def count(self) -> int:
        """Indexed turn count (0 when disabled)."""
        with self._lock:
            store = self._ensure_store()
            if store is None:
                return 0
            try:
                return int(store.get_document_count())
            except Exception:
                return 0

    def backfill(self, rows: list[dict[str, Any]]) -> int:
        """Index recall rows that predate this store, one per request so a
        single oversize turn cannot abort the pass. Returns indexed count."""
        with self._lock:
            if self._ensure_store() is None:
                return 0
        indexed = 0
        for row in rows:
            item_id = str(row.get("id") or "").strip()
            content = str(row.get("content") or "").strip()
            if not item_id or not content:
                continue
            vec = self._embed_text(content)
            if vec is None:
                continue
            with self._lock:
                store = self._ensure_store()
                if store is None:
                    break
                try:
                    store.add_document(
                        doc_id=item_id,
                        content=content,
                        embedding=vec,
                        metadata=self._row_metadata(row),
                    )
                    indexed += 1
                except Exception as e:
                    print(f"[memory][embedding_recall][warn] backfill row failed: {type(e).__name__}: {e}")
        return indexed

    def close(self) -> None:
        """Release the chroma client handle."""
        with self._lock:
            store = self._store
            self._store = None
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    @staticmethod
    def _role_value(item: MemoryItem) -> str:
        role = getattr(item, "role", None)
        return role.value if hasattr(role, "value") else str(role or "")

    @classmethod
    def _item_metadata(cls, item: MemoryItem) -> dict[str, Any]:
        return {
            "role": cls._role_value(item),
            "source": str(getattr(item, "source", "") or ""),
            "created_at": str(getattr(item, "created_at", "") or ""),
            "token_count": int(getattr(item, "token_count", 0) or 0),
        }

    @staticmethod
    def _row_metadata(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": str(row.get("role") or ""),
            "source": str(row.get("source") or ""),
            "created_at": str(row.get("created_at") or ""),
            "token_count": int(row.get("token_count") or 0),
        }

    @staticmethod
    def _hit_to_row(hit: dict[str, Any]) -> dict[str, Any]:
        meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        return {
            "id": str(hit.get("doc_id") or ""),
            "tier": "recall",
            "role": str(meta.get("role") or ""),
            "content": str(hit.get("content") or ""),
            "source": str(meta.get("source") or ""),
            "created_at": str(meta.get("created_at") or ""),
            "token_count": int(meta.get("token_count") or 0),
            "metadata": {},
            "distance": float(hit.get("distance") or 0.0),
        }
