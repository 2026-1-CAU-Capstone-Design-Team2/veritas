"""Dense recall index over conversation turns, backed by ChromaDB."""

from __future__ import annotations

import queue
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

    Lives under ``<workspace>/memory/chromadb`` — a memory-owned store
    separate from the research ``<workspace>/chromadb`` index, so the
    "chromadb dir exists ⇒ research was indexed" invariant other code relies
    on stays intact.

    Every method degrades to a no-op (add) or empty result (search) when the
    embedding endpoint or vector store is unavailable, latching ``_disabled``
    on the first hard failure so a missing embedding server leaves keyword
    recall fully intact. ChromaDB access is serialized under one RLock;
    embedding HTTP calls stay outside it so a slow embed never blocks another
    thread's ChromaDB access.

    ``add`` is asynchronous: it enqueues the turn and returns, so the chat
    hot path (prepare/commit) never waits on the store-side embedding HTTP
    call — that turn is only needed for *future* retrieval, not the current
    response. A single background worker drains the queue. ``search`` stays
    synchronous because its query embedding is needed for this turn's answer.
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
        # No embed method ⇒ permanently disabled; skip the wasted embed attempts.
        self._disabled = not callable(getattr(raw_llm, "embed", None))
        self._lock = threading.RLock()
        # Background dense-indexing queue + lazily-started worker. add() only
        # enqueues; the worker runs the embed+upsert off the hot path.
        self._index_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    def _ensure_store(self) -> Any | None:
        """Lazily open the chroma collection; latch disabled on any failure.

        Caller must hold ``self._lock`` (init is not safe to race)."""
        if self._disabled:
            return None
        if self._store is not None:
            return self._store
        try:
            from storage.vector_store import VectorStore

            self._store = VectorStore(
                persist_dir=self.workspace_root / "memory" / "chromadb",
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

    def _index_row(self, row: dict[str, Any]) -> bool:
        """Embed and upsert one recall-row dict. Returns whether it indexed.

        Shared by ``add`` (one live turn) and ``backfill`` (many old turns):
        embed outside the lock, then upsert under it. An oversize turn whose
        embed returns None is skipped; a ChromaDB write failure latches
        ``_disabled`` so the caller stops touching a broken store."""
        item_id = str(row.get("id") or "").strip()
        content = str(row.get("content") or "").strip()
        if not item_id or not content or self._disabled:
            return False
        vec = self._embed_text(content)
        if vec is None:
            return False
        with self._lock:
            store = self._ensure_store()
            if store is None:
                return False
            try:
                store.add_document(
                    doc_id=item_id,
                    content=content,
                    embedding=vec,
                    metadata=self._row_metadata(row),
                )
                return True
            except Exception as e:
                print(f"[memory][embedding_recall][warn] index failed: {type(e).__name__}: {e}")
                self._disabled = True
                return False

    def add(self, item: MemoryItem) -> None:
        """Enqueue one turn for background dense indexing (non-blocking).

        Returns immediately so the chat hot path never waits on the embedding
        HTTP call; the worker runs the embed+upsert. No-op when disabled."""
        if self._disabled:
            return
        self._ensure_worker()
        self._index_queue.put(self._item_to_row(item))

    def backfill(self, rows: list[dict[str, Any]]) -> int:
        """Index recall rows that predate this store, one per request so a
        single oversize turn cannot abort the pass. Returns indexed count.

        Synchronous on purpose: it already runs on the background backfill
        thread (``MemoryRuntime._embedding_backfill_worker``), so routing it
        through the live-turn queue would only add hand-offs."""
        return sum(1 for row in rows if self._index_row(row))

    def _ensure_worker(self) -> None:
        """Start the dense-index worker on first use (double-checked)."""
        if self._worker is not None:
            return
        with self._worker_lock:
            if self._worker is not None:
                return
            worker = threading.Thread(
                target=self._index_worker, name="memory-dense-index", daemon=True
            )
            self._worker = worker
            worker.start()

    def _index_worker(self) -> None:
        """Drain the queue, embedding+upserting each turn. ``None`` is the
        shutdown sentinel."""
        while True:
            row = self._index_queue.get()
            try:
                if row is None:
                    return
                self._index_row(row)
            except Exception as e:
                print(f"[memory][embedding_recall][warn] async index failed: {type(e).__name__}: {e}")
            finally:
                self._index_queue.task_done()

    def flush(self) -> None:
        """Block until all queued dense writes are indexed (tests / shutdown)."""
        self._index_queue.join()

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Cosine search, newest-relevant first. Empty list on failure/disable."""
        query = str(query or "").strip()
        if not query or int(limit) <= 0 or self._disabled:
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

    def close(self) -> None:
        """Drain pending dense writes, stop the worker, release the chroma handle."""
        with self._worker_lock:
            worker = self._worker
            self._worker = None
        if worker is not None:
            self._index_queue.put(None)  # shutdown sentinel after queued rows
            worker.join(timeout=10)
        with self._lock:
            store = self._store
            self._store = None
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    @classmethod
    def _item_to_row(cls, item: MemoryItem) -> dict[str, Any]:
        """Project a MemoryItem into the recall-row shape ``_index_row`` consumes."""
        role = getattr(item, "role", None)
        return {
            "id": str(getattr(item, "id", "") or ""),
            "content": str(getattr(item, "content", "") or ""),
            "role": role.value if hasattr(role, "value") else str(role or ""),
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
