from __future__ import annotations

from typing import Iterable

from core.knowledge_models import PrivacyLabel, RetrievedChunk, SourceKind, SourceScope


class RetrievalService:
    def __init__(self, *, llm, vector_store) -> None:
        self._llm = llm
        self._vector_store = vector_store

    def retrieve(
        self,
        workspace_id: str,
        query: str,
        *,
        n_results: int = 8,
        source_scopes: set[SourceScope] | None = None,
        source_kinds: set[SourceKind] | None = None,
        include_private: bool = True,
    ) -> list[RetrievedChunk]:
        query_text = str(query or "").strip()
        if not query_text:
            return []

        where = self._where_for_scope(source_scopes)
        query_embedding = self._llm.embed(query_text)
        overfetch = max(n_results, n_results * 3)
        raw = self._vector_store.query(
            query_text=query_text,
            query_embedding=query_embedding,
            n_results=overfetch,
            where=where,
        )

        chunks = [
            chunk
            for chunk in (self._to_retrieved(item) for item in raw)
            if chunk is not None
        ]
        chunks = self._filter_chunks(
            chunks,
            workspace_id=workspace_id,
            source_scopes=source_scopes,
            source_kinds=source_kinds,
            include_private=include_private,
        )
        if any(c.source_scope == SourceScope.LOCAL for c in chunks):
            self._ensure_local_private_allowed(include_private=include_private)
        return chunks[:n_results]

    def _where_for_scope(self, scopes: set[SourceScope] | None) -> dict | None:
        if not scopes or len(scopes) != 1:
            return None
        return {"source_scope": next(iter(scopes)).value}

    def _filter_chunks(
        self,
        chunks: Iterable[RetrievedChunk],
        *,
        workspace_id: str,
        source_scopes: set[SourceScope] | None,
        source_kinds: set[SourceKind] | None,
        include_private: bool,
    ) -> list[RetrievedChunk]:
        filtered: list[RetrievedChunk] = []
        for chunk in chunks:
            if chunk.workspace_id and workspace_id and chunk.workspace_id != workspace_id:
                continue
            if source_scopes and chunk.source_scope not in source_scopes:
                continue
            if source_kinds and chunk.source_kind not in source_kinds:
                continue
            if not include_private and chunk.privacy_label == PrivacyLabel.LOCAL_PRIVATE:
                continue
            filtered.append(chunk)
        return filtered

    def _to_retrieved(self, item: dict) -> RetrievedChunk | None:
        if not isinstance(item, dict):
            return None
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        text = str(item.get("content") or "").strip()
        if not text:
            return None
        source_scope = self._enum_value(SourceScope, metadata.get("source_scope"), SourceScope.EXTERNAL)
        source_kind = self._enum_value(SourceKind, metadata.get("source_kind"), SourceKind.UNKNOWN)
        privacy_label = self._enum_value(
            PrivacyLabel,
            metadata.get("privacy_label"),
            PrivacyLabel.LOCAL_PRIVATE if source_scope == SourceScope.LOCAL else PrivacyLabel.PUBLIC_WEB,
        )
        source_id = str(metadata.get("source_id") or metadata.get("parent_doc_id") or item.get("doc_id") or "")
        return RetrievedChunk(
            chunk_id=str(item.get("doc_id") or ""),
            source_id=source_id,
            workspace_id=str(metadata.get("workspace_id") or ""),
            source_scope=source_scope,
            source_kind=source_kind,
            privacy_label=privacy_label,
            text=text,
            title=str(metadata.get("title") or source_id),
            display_path=str(metadata.get("display_path") or metadata.get("file_path") or metadata.get("url") or ""),
            distance=float(item.get("distance") or 0.0),
            metadata=dict(metadata),
        )

    def _enum_value(self, enum_cls, value, default):
        try:
            return enum_cls(str(value))
        except Exception:
            return default

    def _ensure_local_private_allowed(self, *, include_private: bool) -> None:
        if not include_private:
            raise RuntimeError("Local private chunks were retrieved while include_private=False.")
        # The retrieval service only assembles context. Generation callers must
        # use a local LLM when these chunks are included.


__all__ = ["RetrievalService"]
