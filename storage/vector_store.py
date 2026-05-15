"""ChromaDB-based vector store for document embeddings."""

import gc
from pathlib import Path
from typing import Any, Callable

import chromadb
from chromadb.config import Settings


def _resolve_path(value: Any) -> Path | None:
    if not value:
        return None
    try:
        return Path(value).expanduser().resolve()
    except Exception:
        return None


def release_chromadb_handles_for(target_dir: Any) -> None:
    """Force-release ChromaDB SQLite handles for stores under ``target_dir``.

    ChromaDB keeps a process-wide cache of "systems" keyed by persist directory,
    each holding an open SQLite connection. On Windows that open handle prevents
    the workspace directory from being deleted. Unlike ``Client.close()``, this
    ignores client refcounts and stops the system outright -- it is meant for the
    workspace-deletion path, where every client for that path is being discarded
    anyway. Stores under other directories (other workspaces) are left untouched.
    """
    target = _resolve_path(target_dir)
    if target is None:
        gc.collect()
        return

    try:
        from chromadb.api.shared_system_client import SharedSystemClient
    except Exception:
        gc.collect()
        return

    cache = getattr(SharedSystemClient, "_identifier_to_system", None)
    refcounts = getattr(SharedSystemClient, "_identifier_to_refcount", None)
    if not isinstance(cache, dict):
        gc.collect()
        return

    for identifier, system in list(cache.items()):
        try:
            settings = getattr(system, "settings", None)
            persist_dir = getattr(settings, "persist_directory", None) if settings else None
        except Exception:
            persist_dir = None
        # For persistent clients the cache identifier IS the persist directory.
        candidate = _resolve_path(persist_dir) or _resolve_path(identifier)
        if candidate is None:
            continue
        if candidate == target or target in candidate.parents:
            try:
                system.stop()
            except Exception:
                pass
            cache.pop(identifier, None)
            if isinstance(refcounts, dict):
                refcounts.pop(identifier, None)
    gc.collect()


class VectorStore:
    """ChromaDB-based vector store for document embeddings."""

    def __init__(
        self,
        persist_dir: Path,
        collection_name: str = "research_docs",
        embedding_fn: Callable[[str], list[float]] | None = None,
    ):
        """
        Initialize ChromaDB vector store.

        Args:
            persist_dir: Directory for persistent storage
            collection_name: Name of the ChromaDB collection
            embedding_fn: Function to generate embeddings (e.g., LLMClient.embed)
        """
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )

        self.collection_name = collection_name
        self.embedding_fn = embedding_fn
        self.collection = self._get_or_create_collection()

    def _get_or_create_collection(self) -> chromadb.Collection:
        """Get existing collection or create new one."""
        return self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def close(self) -> None:
        """Release the ChromaDB client so its SQLite file handle is freed.

        Idempotent. Must run before the workspace directory is deleted -- on
        Windows an open ``chromadb/chroma.sqlite3`` handle makes the directory
        undeletable. ``Client.close()`` decrements the shared-system refcount and
        stops the system (closing SQLite) when the last client for that path is
        closed; the targeted ``release_chromadb_handles_for`` is the force-release
        fallback used by the workspace-deletion path.
        """
        client = getattr(self, "client", None)
        self.collection = None
        self.client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        gc.collect()

    def add_document(
        self,
        doc_id: str,
        content: str,
        embedding: list[float] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Add a single document to the vector store.

        Args:
            doc_id: Unique document identifier
            content: Document text content
            embedding: Pre-computed embedding (or computed via embedding_fn)
            metadata: Additional document metadata
        """
        if embedding is None and self.embedding_fn:
            embedding = self.embedding_fn(content)

        self.collection.upsert(
            ids=[doc_id],
            embeddings=[embedding] if embedding else None,
            documents=[content],
            metadatas=[metadata or {}],
        )

    def add_documents(
        self,
        doc_ids: list[str],
        contents: list[str],
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        """Add multiple documents in batch."""
        if not doc_ids:
            return

        self.collection.upsert(
            ids=doc_ids,
            embeddings=embeddings,
            documents=contents,
            metadatas=metadatas or [{} for _ in doc_ids],
        )

    def query(
        self,
        query_text: str | None = None,
        query_embedding: list[float] | None = None,
        n_results: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query the vector store for similar documents.

        Args:
            query_text: Query text (used if query_embedding not provided)
            query_embedding: Pre-computed query embedding
            n_results: Number of results to return
            where: Optional metadata filter

        Returns:
            List of matching documents with scores and metadata
        """
        if query_embedding is None and self.embedding_fn and query_text:
            query_embedding = self.embedding_fn(query_text)

        results = self.collection.query(
            query_embeddings=[query_embedding] if query_embedding else None,
            query_texts=[query_text] if query_text and not query_embedding else None,
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        formatted = []
        if results and results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                formatted.append({
                    "doc_id": doc_id,
                    "content": results["documents"][0][i] if results["documents"] else "",
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "distance": results["distances"][0][i] if results["distances"] else 0.0,
                })

        return formatted

    def get_document_count(self) -> int:
        """Return the number of documents in the collection."""
        return self.collection.count()

    def clear(self) -> None:
        """Clear all documents from the collection."""
        self.client.delete_collection(self.collection_name)
        self.collection = self._get_or_create_collection()

    def delete_documents(self, doc_ids: list[str]) -> None:
        """Delete specific documents by ID."""
        if doc_ids:
            self.collection.delete(ids=doc_ids)
