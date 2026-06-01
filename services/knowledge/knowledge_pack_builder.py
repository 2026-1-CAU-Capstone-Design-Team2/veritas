from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.draft_knowledge_models import DraftKnowledgePack, SectionKnowledgePack
from core.knowledge_models import RetrievedChunk, SourceKind, SourceScope

from .retrieval_service import RetrievalService


class KnowledgePackBuilder:
    """Build bounded draft context from indexed local/private knowledge."""

    def __init__(
        self,
        *,
        retrieval_service: RetrievalService,
        workspace_root: Path,
        max_chunks_per_section: int = 4,
        max_chunk_chars: int = 900,
    ) -> None:
        self._retrieval = retrieval_service
        self._workspace_root = Path(workspace_root)
        self._max_chunks_per_section = max(1, int(max_chunks_per_section))
        self._max_chunk_chars = max(200, int(max_chunk_chars))

    def build_for_outline(
        self,
        workspace_id: str,
        outline: list[str],
        *,
        query_hint: str = "",
        include_external: bool = False,
        include_local: bool = True,
    ) -> DraftKnowledgePack:
        scopes: set[SourceScope] = set()
        if include_external:
            scopes.add(SourceScope.EXTERNAL)
        if include_local:
            scopes.add(SourceScope.LOCAL)
        if not scopes:
            return DraftKnowledgePack(global_context="", section_packs=[], source_map={})

        section_packs: list[SectionKnowledgePack] = []
        source_map: dict[str, Any] = {"sources": {}, "chunks": []}
        conflict_notes = self._load_conflict_notes()

        for title in [str(item).strip() for item in outline if str(item).strip()]:
            query = f"{query_hint}\n{title}".strip()
            chunks = self._retrieval.retrieve(
                workspace_id,
                query,
                n_results=self._max_chunks_per_section,
                source_scopes=scopes,
                include_private=True,
            )
            external = [c for c in chunks if c.source_scope == SourceScope.EXTERNAL]
            local = [c for c in chunks if c.source_scope == SourceScope.LOCAL]
            for chunk in chunks:
                self._record_source(source_map, chunk, section_title=title)
            section_packs.append(
                SectionKnowledgePack(
                    section_title=title,
                    external_evidence=external,
                    local_evidence=local,
                    table_summaries=[
                        c.text[: self._max_chunk_chars]
                        for c in local
                        if c.source_kind == SourceKind.TABLE_SUMMARY
                    ],
                    conflict_notes=conflict_notes[:5],
                )
            )

        context = self.render_markdown(section_packs)
        return DraftKnowledgePack(
            global_context=context,
            section_packs=section_packs,
            source_map=source_map,
        )

    def render_markdown(self, packs: list[SectionKnowledgePack]) -> str:
        parts: list[str] = []
        for pack in packs:
            lines = [f"## Section Evidence: {pack.section_title}"]
            if pack.local_evidence:
                lines.append("### Local Private Evidence")
                for chunk in pack.local_evidence:
                    lines.append(self._render_chunk(chunk))
            if pack.external_evidence:
                lines.append("### External Evidence")
                for chunk in pack.external_evidence:
                    lines.append(self._render_chunk(chunk))
            if pack.conflict_notes:
                lines.append("### Cross-check Notes")
                for note in pack.conflict_notes:
                    lines.append(f"- {note}")
            block = "\n\n".join(line for line in lines if line)
            if "Evidence]" in block:
                parts.append(block)
        return "\n\n---\n\n".join(parts).strip()

    def _render_chunk(self, chunk: RetrievedChunk) -> str:
        label = chunk.title or chunk.source_id
        path = f" ({chunk.display_path})" if chunk.display_path else ""
        text = chunk.text[: self._max_chunk_chars].strip()
        return f"[Evidence] {label}{path}\n{text}"

    def _record_source(
        self,
        source_map: dict[str, Any],
        chunk: RetrievedChunk,
        *,
        section_title: str,
    ) -> None:
        source_map["sources"].setdefault(
            chunk.source_id,
            {
                "sourceId": chunk.source_id,
                "sourceScope": chunk.source_scope.value,
                "sourceKind": chunk.source_kind.value,
                "privacyLabel": chunk.privacy_label.value,
                "title": chunk.title,
                "displayPath": chunk.display_path,
            },
        )
        source_map["chunks"].append(
            {
                "chunkId": chunk.chunk_id,
                "sourceId": chunk.source_id,
                "sectionTitle": section_title,
                "distance": chunk.distance,
            }
        )

    def _load_conflict_notes(self) -> list[str]:
        path = self._workspace_root / "verification" / "crosscheck.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        flags = payload.get("flags") if isinstance(payload, dict) else []
        notes: list[str] = []
        for flag in flags if isinstance(flags, list) else []:
            if not isinstance(flag, dict):
                continue
            message = str(flag.get("message") or flag.get("reason") or "").strip()
            if message:
                notes.append(message)
        return notes


def pack_to_source_map(pack: DraftKnowledgePack) -> dict[str, Any]:
    return dict(pack.source_map or {})


__all__ = ["KnowledgePackBuilder", "pack_to_source_map"]
