from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from core.knowledge_models import (
    KnowledgeSourceRecord,
    PrivacyLabel,
    SourceKind,
    SourceScope,
)
from core.local_corpus_models import LocalFileManifestEntry, TableProfile


class ManifestRepository:
    def __init__(self, output_root: str | Path = "runs") -> None:
        self._output_root = Path(output_root)

    def workspace_root(self, workspace_id: str) -> Path:
        workspace_id = str(workspace_id or "default").strip() or "default"
        return self._output_root / workspace_id if workspace_id != "default" else self._output_root / "api"

    def local_dir(self, workspace_id: str) -> Path:
        return self.workspace_root(workspace_id) / "local"

    def knowledge_dir(self, workspace_id: str) -> Path:
        return self.workspace_root(workspace_id) / "knowledge"

    def manifest_path(self, workspace_id: str) -> Path:
        return self.local_dir(workspace_id) / "manifest.json"

    def extracted_md_path(self, workspace_id: str, source_id: str) -> Path:
        return self.local_dir(workspace_id) / "extracted_md" / f"{source_id}.md"

    def table_profiles_path(self, workspace_id: str) -> Path:
        return self.local_dir(workspace_id) / "tables" / "table_profiles.json"

    def sources_path(self, workspace_id: str) -> Path:
        return self.knowledge_dir(workspace_id) / "sources.json"

    def prepare(self, workspace_id: str) -> None:
        self.local_dir(workspace_id).mkdir(parents=True, exist_ok=True)
        (self.local_dir(workspace_id) / "extracted_md").mkdir(parents=True, exist_ok=True)
        (self.local_dir(workspace_id) / "tables").mkdir(parents=True, exist_ok=True)
        self.knowledge_dir(workspace_id).mkdir(parents=True, exist_ok=True)

    def load_manifest(self, workspace_id: str) -> list[LocalFileManifestEntry]:
        payload = self._read_json(self.manifest_path(workspace_id), default=[])
        entries: list[LocalFileManifestEntry] = []
        for item in payload if isinstance(payload, list) else []:
            if isinstance(item, dict):
                entries.append(LocalFileManifestEntry(**item))
        return entries

    def save_manifest(self, workspace_id: str, entries: list[LocalFileManifestEntry]) -> None:
        self._write_json(self.manifest_path(workspace_id), [asdict(e) for e in entries])

    def load_sources(self, workspace_id: str) -> list[KnowledgeSourceRecord]:
        payload = self._read_json(self.sources_path(workspace_id), default=[])
        sources: list[KnowledgeSourceRecord] = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                sources.append(
                    KnowledgeSourceRecord(
                        source_id=str(item.get("source_id") or item.get("sourceId") or ""),
                        workspace_id=str(item.get("workspace_id") or item.get("workspaceId") or workspace_id),
                        source_scope=SourceScope(str(item.get("source_scope") or item.get("sourceScope") or "local")),
                        source_kind=SourceKind(str(item.get("source_kind") or item.get("sourceKind") or "unknown")),
                        title=str(item.get("title") or ""),
                        canonical_uri=str(item.get("canonical_uri") or item.get("canonicalUri") or ""),
                        display_path=str(item.get("display_path") or item.get("displayPath") or ""),
                        privacy_label=PrivacyLabel(str(item.get("privacy_label") or item.get("privacyLabel") or "local_private")),
                        content_hash=str(item.get("content_hash") or item.get("contentHash") or ""),
                        created_at=str(item.get("created_at") or item.get("createdAt") or ""),
                        modified_at=str(item.get("modified_at") or item.get("modifiedAt") or ""),
                        parser_version=str(item.get("parser_version") or item.get("parserVersion") or ""),
                        status=str(item.get("status") or "indexed"),
                        metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                    )
                )
            except Exception:
                continue
        return [source for source in sources if source.source_id]

    def save_sources(self, workspace_id: str, sources: list[KnowledgeSourceRecord]) -> None:
        payload = []
        for source in sources:
            item = asdict(source)
            item["source_scope"] = source.source_scope.value
            item["source_kind"] = source.source_kind.value
            item["privacy_label"] = source.privacy_label.value
            payload.append(item)
        self._write_json(self.sources_path(workspace_id), payload)

    def write_extracted_markdown(self, workspace_id: str, source_id: str, text: str) -> Path:
        path = self.extracted_md_path(workspace_id, source_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text or ""), encoding="utf-8")
        return path

    def read_extracted_markdown(self, workspace_id: str, source_id: str) -> str:
        path = self.extracted_md_path(workspace_id, source_id)
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def save_table_profiles(
        self,
        workspace_id: str,
        profiles_by_source: dict[str, list[TableProfile]],
    ) -> Path:
        payload: dict[str, Any] = {}
        for source_id, profiles in profiles_by_source.items():
            payload[source_id] = [asdict(profile) for profile in profiles]
        path = self.table_profiles_path(workspace_id)
        self._write_json(path, payload)
        return path

    def remove_sources(self, workspace_id: str, source_ids: set[str]) -> None:
        sources = [s for s in self.load_sources(workspace_id) if s.source_id not in source_ids]
        manifest = [e for e in self.load_manifest(workspace_id) if e.source_id not in source_ids]
        self.save_sources(workspace_id, sources)
        self.save_manifest(workspace_id, manifest)
        for source_id in source_ids:
            try:
                self.extracted_md_path(workspace_id, source_id).unlink(missing_ok=True)
            except Exception:
                pass

    def _read_json(self, path: Path, *, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = ["ManifestRepository"]
