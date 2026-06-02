from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.draft_knowledge_models import DraftKnowledgePack, SectionKnowledgePack
from core.knowledge_models import RetrievedChunk, SourceKind, SourceScope
from core.prompts import DRAFT_CROSSCHECK_NOTES_HEADER

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
        # Cross-check findings are workspace-wide, not per-section: render them
        # once, ahead of the section evidence, so they survive downstream char-
        # budget clipping and read as a global writing constraint.
        notes = next((pack.conflict_notes for pack in packs if pack.conflict_notes), [])
        if notes:
            parts.append("\n\n".join([DRAFT_CROSSCHECK_NOTES_HEADER, *notes]))
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
        """Render cross-check flags as Korean notes for the draft prompt.

        Each note quotes the conflicting external/local claims verbatim with
        their source labels and leaves the interpretation (which numbers
        conflict, how to reconcile them) to the writing model — no value
        parsing or keyword matching happens here.
        """
        path = self._workspace_root / "verification" / "crosscheck.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(payload, dict):
            return []

        claims = payload.get("claims")
        claims_by_id: dict[str, dict[str, Any]] = {}
        for claim in claims if isinstance(claims, list) else []:
            if isinstance(claim, dict) and claim.get("claim_id"):
                claims_by_id[str(claim["claim_id"])] = claim

        flags = payload.get("flags")
        notes: list[str] = []
        for flag in flags if isinstance(flags, list) else []:
            if not isinstance(flag, dict):
                continue
            note = self._format_conflict_note(flag, claims_by_id)
            if note:
                notes.append(note)
        return notes

    def _format_conflict_note(
        self, flag: dict[str, Any], claims_by_id: dict[str, dict[str, Any]]
    ) -> str:
        # claimA is always the external claim, claimB the local one
        # (services.verification.crosscheck.pipeline._compare_claims).
        external = claims_by_id.get(str(flag.get("claimA") or ""))
        local = claims_by_id.get(str(flag.get("claimB") or ""))
        if not external or not local:
            # Older artifacts persisted flags without resolvable claims; keep
            # the raw pipeline message rather than dropping the conflict.
            return str(flag.get("message") or flag.get("reason") or "").strip()

        external_text = self._claim_quote(external)
        local_text = self._claim_quote(local)
        if not external_text or not local_text:
            return str(flag.get("message") or "").strip()

        external_label = self._claim_label(external, fallback="외부 조사 자료")
        local_label = self._claim_label(local, fallback="내부 문서")
        return (
            f'- 외부 자료({external_label}): "{external_text}"\n'
            f'  내부 자료({local_label}): "{local_text}"'
        )

    def _claim_quote(self, claim: dict[str, Any]) -> str:
        text = " ".join(str(claim.get("text") or "").split())
        return text[: self._max_chunk_chars]

    @staticmethod
    def _claim_label(claim: dict[str, Any], *, fallback: str) -> str:
        metadata = claim.get("metadata") if isinstance(claim.get("metadata"), dict) else {}
        for key in ("display_path", "title", "domain"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
        return fallback


def pack_to_source_map(pack: DraftKnowledgePack) -> dict[str, Any]:
    return dict(pack.source_map or {})


__all__ = ["KnowledgePackBuilder", "pack_to_source_map"]
