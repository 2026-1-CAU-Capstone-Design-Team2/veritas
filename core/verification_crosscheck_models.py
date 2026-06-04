from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .knowledge_models import SourceScope


@dataclass(frozen=True)
class CrossCheckClaim:
    claim_id: str
    source_id: str
    source_scope: SourceScope
    text: str
    claim_type: str
    evidence_span: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrossCheckRelation:
    claim_a: str
    claim_b: str
    relation: str
    severity: str
    reason: str


@dataclass(frozen=True)
class CrossCheckArtifact:
    claims: list[CrossCheckClaim] = field(default_factory=list)
    relations: list[CrossCheckRelation] = field(default_factory=list)
    flags: list[dict[str, Any]] = field(default_factory=list)


__all__ = [
    "CrossCheckArtifact",
    "CrossCheckClaim",
    "CrossCheckRelation",
]
