from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .knowledge_models import RetrievedChunk


@dataclass(frozen=True)
class SectionKnowledgePack:
    section_title: str
    external_evidence: list[RetrievedChunk] = field(default_factory=list)
    local_evidence: list[RetrievedChunk] = field(default_factory=list)
    table_summaries: list[str] = field(default_factory=list)
    conflict_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DraftKnowledgePack:
    global_context: str
    section_packs: list[SectionKnowledgePack]
    source_map: dict[str, Any] = field(default_factory=dict)


__all__ = ["DraftKnowledgePack", "SectionKnowledgePack"]
