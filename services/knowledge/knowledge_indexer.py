from __future__ import annotations

from dataclasses import asdict
from typing import Any

from core.knowledge_models import (
    KnowledgeChunkRecord,
    KnowledgeSourceRecord,
    PrivacyLabel,
    SourceKind,
    SourceScope,
)

from .chunker import chunk_markdown


class KnowledgeIndexer:
    """Index normalized knowledge sources into the shared vector store.

    The vector collection stays shared with AutoSurvey RAG; local/external
    separation is carried by metadata filters, not by separate collections.
    """

    def __init__(
        self,
        *,
        llm,
        vector_store,
        max_chunk_chars: int = 1200,
        overlap_chars: int = 150,
    ) -> None:
        self._llm = llm
        self._vector_store = vector_store
        self._max_chunk_chars = max(200, int(max_chunk_chars))
        self._overlap_chars = max(0, int(overlap_chars))

    def index_sources(
        self,
        sources: list[KnowledgeSourceRecord],
        documents: dict[str, str],
        *,
        clear_where: dict[str, Any] | None = None,
        replace_source_chunks: bool = True,
    ) -> int:
        doc_ids: list[str] = []
        contents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for source in sources:
            text = documents.get(source.source_id, "")
            chunks = self.build_chunks(source, text)
            for chunk in chunks:
                doc_ids.append(chunk.chunk_id)
                contents.append(chunk.text)
                metadata = self._metadata_for(source, chunk)
                metadatas.append(metadata)

        if not doc_ids:
            self._delete_existing_chunks(
                sources,
                clear_where=clear_where,
                replace_source_chunks=replace_source_chunks,
            )
            return 0

        embeddings = self._llm.embed_batch(contents)
        self._delete_existing_chunks(
            sources,
            clear_where=clear_where,
            replace_source_chunks=replace_source_chunks,
        )
        self._vector_store.add_documents(
            doc_ids=doc_ids,
            contents=contents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        return len(doc_ids)

    def _delete_existing_chunks(
        self,
        sources: list[KnowledgeSourceRecord],
        *,
        clear_where: dict[str, Any] | None,
        replace_source_chunks: bool,
    ) -> None:
        if clear_where:
            self._vector_store.delete_where(clear_where)
        elif replace_source_chunks:
            for source in sources:
                self._vector_store.delete_where({"source_id": source.source_id})

    def build_chunks(
        self,
        source: KnowledgeSourceRecord,
        text: str,
    ) -> list[KnowledgeChunkRecord]:
        chunks = chunk_markdown(
            text,
            max_chars=self._max_chunk_chars,
            overlap_chars=self._overlap_chars,
        )
        records: list[KnowledgeChunkRecord] = []
        for index, chunk in enumerate(chunks):
            records.append(
                KnowledgeChunkRecord(
                    chunk_id=f"{source.source_id}:chunk_{index:03d}",
                    source_id=source.source_id,
                    workspace_id=source.workspace_id,
                    source_scope=source.source_scope,
                    source_kind=source.source_kind,
                    text=chunk,
                    chunk_index=index,
                    chunk_count=len(chunks),
                    metadata=dict(source.metadata),
                )
            )
        return records

    def _metadata_for(
        self,
        source: KnowledgeSourceRecord,
        chunk: KnowledgeChunkRecord,
    ) -> dict[str, Any]:
        metadata = {
            "workspace_id": source.workspace_id,
            "source_id": source.source_id,
            "source_scope": source.source_scope.value,
            "source_kind": source.source_kind.value,
            "privacy_label": source.privacy_label.value,
            "title": source.title,
            "display_path": source.display_path,
            "canonical_uri": source.canonical_uri,
            "content_hash": source.content_hash,
            "parent_doc_id": source.source_id,
            "chunk_index": chunk.chunk_index,
            "chunk_count": chunk.chunk_count,
        }
        for key, value in source.metadata.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                metadata.setdefault(str(key), value)
        return metadata


def source_from_external_metadata(
    *,
    workspace_id: str,
    doc_id: str,
    title: str,
    url: str,
    domain: str = "",
    search_query: str = "",
) -> KnowledgeSourceRecord:
    return KnowledgeSourceRecord(
        source_id=str(doc_id),
        workspace_id=str(workspace_id),
        source_scope=SourceScope.EXTERNAL,
        source_kind=SourceKind.WEB_PAGE,
        title=title or str(doc_id),
        canonical_uri=url or str(doc_id),
        display_path=domain or url or str(doc_id),
        privacy_label=PrivacyLabel.PUBLIC_WEB,
        content_hash="",
        metadata={"url": url, "domain": domain, "search_query": search_query},
    )


def source_to_dict(source: KnowledgeSourceRecord) -> dict[str, Any]:
    payload = asdict(source)
    payload["source_scope"] = source.source_scope.value
    payload["source_kind"] = source.source_kind.value
    payload["privacy_label"] = source.privacy_label.value
    return payload


__all__ = [
    "KnowledgeIndexer",
    "source_from_external_metadata",
    "source_to_dict",
]
