from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.knowledge_models import KnowledgeSourceRecord, PrivacyLabel, SourceScope
from core.local_corpus_models import (
    LocalCorpusIndexResult,
    LocalCorpusMutationResult,
    LocalFileManifestEntry,
)
from services.knowledge import KnowledgeIndexer

from .file_scanner import FileScanner
from .manifest_repository import ManifestRepository
from .parsers import ParserRegistry

ProgressCallback = Callable[..., None]


class LocalCorpusService:
    def __init__(
        self,
        *,
        output_root: str | Path = "runs",
        llm,
        scanner: FileScanner | None = None,
        parser: ParserRegistry | None = None,
        manifest_repository: ManifestRepository | None = None,
        vector_store=None,
    ) -> None:
        self._repo = manifest_repository or ManifestRepository(output_root)
        self._scanner = scanner or FileScanner()
        self._parser = parser or ParserRegistry()
        self._llm = llm
        self._vector_store = vector_store

    def index_workspace_sources(
        self,
        workspace_id: str,
        roots: list[str],
        *,
        clear_local_first: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> LocalCorpusIndexResult:
        workspace_id = str(workspace_id or "default").strip() or "default"
        self._repo.prepare(workspace_id)
        self._emit(progress_callback, "local_corpus_scan", "Scanning local files...")

        scanned = self._scanner.scan(roots)
        previous = {entry.source_id: entry for entry in self._repo.load_manifest(workspace_id)}
        existing_sources = {
            source.source_id: source for source in self._repo.load_sources(workspace_id)
        }

        entries: list[LocalFileManifestEntry] = []
        sources: list[KnowledgeSourceRecord] = []
        documents: dict[str, str] = {}
        profiles_by_source: dict[str, list] = {}
        skipped = 0
        failed = 0

        for entry in scanned:
            if entry.parser_status.startswith("skipped"):
                entries.append(entry)
                skipped += 1
                continue
            old = previous.get(entry.source_id)
            if (
                not clear_local_first
                and old
                and old.content_hash == entry.content_hash
                and old.parser_status == "indexed"
            ):
                entries.append(old)
                old_source = existing_sources.get(entry.source_id)
                if old_source:
                    sources.append(old_source)
                skipped += 1
                continue

            self._emit(
                progress_callback,
                "local_corpus_parse",
                f"Parsing local file: {entry.relative_path}",
                detail={"sourceId": entry.source_id, "path": entry.relative_path},
            )
            try:
                parsed = self._parser.parse(entry.source_id, entry.absolute_path)
                extracted_path = self._repo.write_extracted_markdown(
                    workspace_id,
                    entry.source_id,
                    parsed.markdown_text,
                )
                source = KnowledgeSourceRecord(
                    source_id=entry.source_id,
                    workspace_id=workspace_id,
                    source_scope=SourceScope.LOCAL,
                    source_kind=parsed.source_kind,
                    title=entry.file_name,
                    canonical_uri=entry.absolute_path,
                    display_path=entry.relative_path,
                    privacy_label=PrivacyLabel.LOCAL_PRIVATE,
                    content_hash=entry.content_hash,
                    created_at=self._now(),
                    modified_at=entry.modified_at,
                    parser_version=self._parser.parser_version,
                    status="indexed",
                    metadata={
                        "file_name": entry.file_name,
                        "extension": entry.extension,
                        "relative_path": entry.relative_path,
                        "size_bytes": entry.size_bytes,
                    },
                )
                entries.append(
                    replace(
                        entry,
                        parser_status="indexed",
                        parser_error="",
                        extracted_text_path=str(extracted_path),
                        table_profile_path=(
                            str(self._repo.table_profiles_path(workspace_id))
                            if parsed.table_profiles
                            else ""
                        ),
                    )
                )
                sources.append(source)
                documents[source.source_id] = parsed.markdown_text
                if parsed.table_profiles:
                    profiles_by_source[source.source_id] = parsed.table_profiles
            except Exception as exc:
                failed += 1
                entries.append(
                    replace(
                        entry,
                        parser_status="failed",
                        parser_error=str(exc)[:500],
                    )
                )

        if clear_local_first:
            existing_sources = {}

        merged_sources = list(existing_sources.values()) if not clear_local_first else []
        incoming_ids = {source.source_id for source in sources}
        merged_sources = [source for source in merged_sources if source.source_id not in incoming_ids]
        merged_sources.extend(sources)
        self._repo.save_manifest(workspace_id, entries)
        self._repo.save_sources(workspace_id, merged_sources)
        self._repo.save_table_profiles(workspace_id, profiles_by_source)

        self._emit(progress_callback, "local_corpus_index", "Indexing local corpus chunks...")
        vector_store = self._get_vector_store(workspace_id)
        indexer = KnowledgeIndexer(llm=self._llm, vector_store=vector_store)
        vector_count = indexer.index_sources(
            sources,
            documents,
            clear_where={"source_scope": SourceScope.LOCAL.value} if clear_local_first else None,
        )

        return LocalCorpusIndexResult(
            workspace_id=workspace_id,
            indexed_count=len(sources),
            skipped_count=skipped,
            failed_count=failed,
            vector_count=vector_count,
            sources=[self._source_payload(source) for source in merged_sources],
            manifest_path=str(self._repo.manifest_path(workspace_id)),
        )

    def list_sources(self, workspace_id: str) -> list[KnowledgeSourceRecord]:
        return self._repo.load_sources(workspace_id)

    def remove_sources(
        self,
        workspace_id: str,
        source_ids: list[str],
    ) -> LocalCorpusMutationResult:
        ids = {str(source_id).strip() for source_id in source_ids if str(source_id).strip()}
        if not ids:
            return LocalCorpusMutationResult(workspace_id=workspace_id, removed_count=0, source_ids=[])
        vector_store = self._get_vector_store(workspace_id)
        for source_id in ids:
            vector_store.delete_where({"source_id": source_id})
        self._repo.remove_sources(workspace_id, ids)
        return LocalCorpusMutationResult(
            workspace_id=workspace_id,
            removed_count=len(ids),
            source_ids=sorted(ids),
        )

    def _get_vector_store(self, workspace_id: str):
        if self._vector_store is not None:
            return self._vector_store
        from storage.vector_store import VectorStore

        return VectorStore(
            persist_dir=self._repo.workspace_root(workspace_id) / "chromadb",
            collection_name="research_docs",
        )

    def _source_payload(self, source: KnowledgeSourceRecord) -> dict[str, Any]:
        return {
            "sourceId": source.source_id,
            "workspaceId": source.workspace_id,
            "sourceScope": source.source_scope.value,
            "sourceKind": source.source_kind.value,
            "title": source.title,
            "displayPath": source.display_path,
            "privacyLabel": source.privacy_label.value,
            "status": source.status,
            "modifiedAt": source.modified_at,
        }

    def _emit(self, callback: ProgressCallback | None, stage: str, message: str, *, detail: dict | None = None) -> None:
        if callback is None:
            return
        try:
            callback(stage, message, detail=detail or {})
        except TypeError:
            callback(stage, message)
        except Exception:
            pass

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["LocalCorpusService"]
